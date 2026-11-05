/**
 * Watchdog.js — Runs every 3 seconds and verifies the entire system is healthy.
 *
 * Checks (in order):
 *   1. Browser connected
 *   2. Page alive and not closed
 *   3. Correct URL (on Aviator page)
 *   4. User logged in
 *   5. Collector running (state = COLLECTING)
 *   6. No frozen collector (no new round for the configured stale interval)
 *
 * If any check fails, fires the onUnhealthy callback with a reason string.
 * The main loop handles the actual recovery — the watchdog only detects.
 */

import { log } from './Logger.js';
import { State } from './StateMachine.js';
import { sleep } from './RetryManager.js';
import { isAcknowledgedAuthRoute, isAviatorRoute, isExpiredAuthRoute, isLogoutPopupRoute } from './LoginManager.js';

function safeRoute(raw) {
  try { const url = new URL(raw); return `${url.origin}${url.pathname}`; }
  catch (error) { log.debug(`Page route could not be sanitized: ${error.message}`); return 'UNKNOWN_URL'; }
}

const WATCHDOG_INTERVAL_MS  = 3000;
const SESSION_MONITOR_URL = 'https://winner.rw/en/user/logout/popup';
// Re-open the popup well before 40 seconds so the injected prompt has time to
// render and the trusted NO click can be verified within the requested window.
const SESSION_MONITOR_NO_CLICK_INTERVAL_MS = 35_000;
const SESSION_MONITOR_NO_CLICK_RETRY_MS = 1000;
const SESSION_MONITOR_NO_CLICK_PROMPT_WAIT_MS = 10000;
const SESSION_MONITOR_NO_CLICK_NAVIGATION_TIMEOUT_MS = 7000;
const SESSION_MONITOR_NO_CLICK_FAILURES_BEFORE_RELOGIN = 2;
const SESSION_MONITOR_RELOGIN_COOLDOWN_MS = 120_000;
// Legitimate Aviator rounds can run beyond one minute. A 60-second threshold
// caused healthy high-multiplier rounds to be treated as collector failures.
const configuredSessionCheckSeconds = Number(process.env.SESSION_CHECK_INTERVAL_SECONDS || 10);
const LOGIN_CHECK_INTERVAL  = Number.isFinite(configuredSessionCheckSeconds)
  ? Math.min(60000, Math.max(1000, configuredSessionCheckSeconds * 1000)) : 10000;
const POST_RECOVERY_QUIET_MS = 15000; // silence watchdog for 15s after any recovery

export class Watchdog {
  constructor({ stateMachine, browserManager, loginManager, health, onUnhealthy }) {
    this.sm          = stateMachine;
    this.browser     = browserManager;
    this.login       = loginManager;
    this.health      = health;
    this.onUnhealthy = onUnhealthy;
    this._page       = null;
    this._running    = false;
    this._checking   = false;
    this._lastLoginCheck  = 0;
    this._loginFailures = 0;
    this._quietUntil      = 0;  // suppress alerts until this timestamp
    this._lastPageProbeWarning = 0;
    this._routeCheckPending = false;
    this._lastAlert = { reason: null, at: 0 };
    this._trackedPages = new WeakSet();
    this._monitorPage = null;
    this._monitorSetupPromise = null;
    this._lastMonitorAttempt = 0;
    this._lastMonitorRouteRestoreAt = 0;
    this._monitorRouteRestorePromise = null;
    this._monitorFailures = 0;
    this._logoutPopupTasks = new WeakMap();
    this._restoreMonitorTabAfterClose = false;
    this._lastVerifiedNoClickAt = null;
    this._lastVerifiedNoClickAtMs = 0;
    this._monitorNoClickFailures = 0;
    this._lastNoClickReloginAt = 0;
    this._nextMonitorNoClickAt = Date.now() + SESSION_MONITOR_NO_CLICK_INTERVAL_MS;
    this._periodicMonitorNoClickPromise = null;
    this._monitorNoClickTimer = null;
    this._lastPeriodicMonitorNavigationAt = 0;
    this._monitorPopupNeedsReopen = false;
  }

  setPage(page) {
    this._page = page;
    // Silence watchdog for POST_RECOVERY_QUIET_MS after page changes
    // (recovery just completed — give the page time to settle)
    this._quietUntil = Date.now() + POST_RECOVERY_QUIET_MS;
    this._routeCheckPending = true;
    if (page && !this._trackedPages.has(page)) {
      this._trackedPages.add(page);
      page.on?.('framenavigated', frame => {
        try {
          if (frame === page.mainFrame()) {
            this._routeCheckPending = true;
            this._lastLoginCheck = 0;
            if (isLogoutPopupRoute(frame.url())) {
              void this._recordLogoutPopup(page, 'history-page-route-change', { waitForPromptMs: 30000 });
            }
            else if (isExpiredAuthRoute(frame.url())) this._markLoginRoute('history-page-route-change');
          }
        } catch (error) { log.debug(`Main-page navigation evidence could not be read: ${error.message}`); }
      });
      page.on?.('crash', error => {
        void this._alert(`PAGE_CRASHED: ${error?.message || 'History page crashed'}`, page);
      });
      page.on?.('close', () => {
        void this._alert('History page closed', page);
      });
    }
    this._markLoginRoute('history-page-attached');
  }

  _markLoginRoute(source) {
    try {
      const candidates = [...new Set([this._page, ...this._contextPages()])]
        .filter(Boolean).map(page => ({ page, name: page === this._monitorPage ? 'session monitor' : 'Aviator' }));
      const detected = candidates.find(({ page }) => {
        try {
          return page && !page.isClosed?.() && isExpiredAuthRoute(page.url())
            && !isLogoutPopupRoute(page.url())
            && (page === this._page || !isAcknowledgedAuthRoute(page));
        }
        catch (error) { log.debug(`Page login-route evidence could not be checked: ${error.message}`); return false; }
      });
      if (!detected) {
        const popup = candidates.find(({ page }) => {
          try { return !page.isClosed?.() && isLogoutPopupRoute(page.url()); }
          catch (error) { log.debug(`Logout-popup route evidence could not be checked: ${error.message}`); return false; }
        });
        if (popup) {
          void this._recordLogoutPopup(popup.page, source, { waitForPromptMs: 30000 });
          return true;
        }
        return false;
      }
      const route = safeRoute(detected.page.url());
      this.health.setLoggedIn(false);
      this.health.setAuthRequired?.(`Winner ${detected.name} page is on a login or logout route`);
      this.health.setRecovery?.('AUTH_REQUIRED', {
        reasonCode: 'LOGIN_ROUTE_VISIBLE', evidence: { page: detected.name.toLowerCase(), route, source },
      });
      this._setSessionStatus('EXPIRED', { reasonCode: 'LOGIN_ROUTE_VISIBLE' });
      void this._alert(`AUTH_REQUIRED: login route visible on ${detected.name} page`, detected.page);
      if (detected.page === this._monitorPage) {
        this._scheduleMonitorRouteRestore(detected.page, source);
      }
      return true;
    } catch (error) {
      log.debug(`Managed-page login-route evidence could not be checked: ${error.message}`);
      return false;
    }
  }

  start(signal) {
    if (this._running) return;
    this._running = true;
    this._setSessionStatus('RECOVERING', {
      reasonCode: 'SESSION_CHECK_PENDING', monitorTabStatus: 'NOT_STARTED',
    });
    this._monitorNoClickTimer = setInterval(() => this._schedulePeriodicMonitorNoClick(), 1000);
    this._monitorNoClickTimer.unref?.();
    log.info('[WATCHDOG] Session monitor started');
    this._loop(signal);
  }

  stop() {
    this._running = false;
    if (this._timer) { clearTimeout(this._timer); this._timer = null; }
    if (this._monitorNoClickTimer) { clearInterval(this._monitorNoClickTimer); this._monitorNoClickTimer = null; }
    if (this.browser.closeSessionMonitorPage) void this.browser.closeSessionMonitorPage();
    this._monitorPage = null;
    this._setSessionStatus('INACTIVE', {
      reasonCode: 'WATCHDOG_STOPPED', monitorTabStatus: 'CLOSED',
    });
    log.info('Watchdog: stopped');
  }

  _contextPages() {
    try {
      return (this.browser?._context?.pages?.() || []).filter(page => page && !page.isClosed?.());
    } catch (error) {
      log.debug(`Winner session monitor page list could not be read: ${error.message}`);
      return [];
    }
  }

  _scheduleMonitorTabReopen(source) {
    if (!this._running || !this._restoreMonitorTabAfterClose
        || !this.browser.isAlive() || !this._page || this._page.isClosed?.()
        || [State.RELOGIN, State.RESTART_BROWSER, State.STARTING].includes(this.sm.current)) return;
    queueMicrotask(() => {
      if (!this._running || !this._restoreMonitorTabAfterClose
          || !this.browser.isAlive() || !this._page || this._page.isClosed?.()
          || [State.RELOGIN, State.RESTART_BROWSER, State.STARTING].includes(this.sm.current)) return;
      void this._ensureSessionMonitorPage(this._page).then(page => {
        if (!page && this._restoreMonitorTabAfterClose) {
          log.warn(`[WATCHDOG] Session monitor tab reopen is pending source=${source}`);
        }
      }).catch(error => {
        log.warn(`[WATCHDOG] Session monitor tab reopen failed source=${source}: ${error?.message || error}`);
      });
    });
  }

  async _authenticatedContextPage() {
    const candidates = [...new Set([this._page, ...this._contextPages()])]
      .filter(candidate => candidate && !candidate.isClosed?.()
        && !isLogoutPopupRoute(candidate.url?.() || '')
        && !isExpiredAuthRoute(candidate.url?.() || ''));
    for (const candidate of candidates) {
      const session = await this.login.inspectSession(candidate).catch(error => {
        log.debug(`Session evidence after logout-popup NO click was unavailable: ${error.message}`);
        return { status: 'UNKNOWN', reasonCode: 'SESSION_PROBE_FAILED' };
      });
      if (session.status === 'AUTHENTICATED') return session;
    }
    return null;
  }

  _setSessionStatus(status, { reasonCode = null, countdownSeconds = null,
    monitorTabStatus = null, successfulCheck = false } = {}) {
    const snapshot = this.health.snapshot?.() || {};
    this.health.setSessionWatchdog?.({
      status,
      monitor_tab_status: monitorTabStatus || (this._monitorPage && !this._monitorPage.isClosed?.() ? 'OPEN' : 'NOT_STARTED'),
      reason_code: reasonCode,
      last_successful_check_at: successfulCheck ? new Date().toISOString()
        : snapshot.sessionWatchdog?.last_successful_check_at || null,
      // No verified Winner idle deadline or official heartbeat is available;
      // keep activity and remaining-time fields explicitly unknown.
      last_successful_activity_at: null,
      estimated_remaining_seconds: null,
      next_scheduled_maintenance_at: null,
      last_successful_maintenance_at: null,
      last_successful_refresh_at: null,
      last_verified_no_click_at: this._lastVerifiedNoClickAt
        || snapshot.sessionWatchdog?.last_verified_no_click_at || null,
      logout_countdown_seconds: countdownSeconds,
      recovery_attempts: this._monitorFailures,
      collector_connected: Boolean(this.browser.isAlive() && this._page && !this._page.isClosed?.()
        && snapshot.frameConnected === true),
    });
  }

  async _recordLogoutPopup(page, source, { waitForPromptMs = 30000 } = {}) {
    if (!page || page.isClosed?.() || !isLogoutPopupRoute(page.url?.() || '')) return false;
    const pending = this._logoutPopupTasks.get(page);
    if (pending) return pending;
    const task = (async () => {
      let session = { status: 'UNKNOWN', reasonCode: 'LOGOUT_POPUP_UNVERIFIED' };
      try { session = await this.login.inspectSession(page); }
      catch (error) { log.debug(`Logout countdown verification failed: ${error.message}`); }

      const countdownSeconds = session.evidence?.countdown_seconds ?? null;
      // The popup countdown reaching zero must not prevent the NO click. The
      // route can still render its button after the splash screen; attempt the
      // dismissal first, then retain AUTH_REQUIRED if it cannot be verified.
      if (session.status === 'MANUAL_VERIFICATION') {
        const reasonCode = session.reasonCode || 'LOGOUT_COUNTDOWN_ELAPSED';
        this.health.setLoggedIn(false);
        this.health.setAuthRequired?.(session.status === 'EXPIRED'
          ? 'Winner session expired; automatic login recovery is required'
          : 'Winner requires manual verification before login can continue');
        this.health.setRecovery?.('AUTH_REQUIRED', { reasonCode, evidence: session.evidence || null });
        this._setSessionStatus('EXPIRED', { reasonCode, countdownSeconds: 0, successfulCheck: true });
        void this._alert(`AUTH_REQUIRED: ${reasonCode}`, this._page || page, {
          sessionRecovery: 'MANUAL_VERIFICATION', recoveryPage: 'MONITOR', reasonCode,
        });
        return false;
      }

      let dismissal = { status: 'NOT_PRESENT', reasonCode: 'LOGOUT_NO_BUTTON_NOT_CHECKED' };
      if (typeof this.login.dismissLogoutPopup === 'function') {
        try {
          dismissal = await this.login.dismissLogoutPopup(page, { waitForPromptMs });
        } catch (error) {
          log.warn(`[WATCHDOG] Logout popup NO click failed source=${source}: ${error.message}`);
          dismissal = { status: 'FAILED', reasonCode: 'LOGOUT_NO_CLICK_UNVERIFIED' };
        }
      }
      if (dismissal.status === 'DISMISSED') {
        this._monitorNoClickFailures = 0;
        this._lastNoClickReloginAt = 0;
        this._lastVerifiedNoClickAt = new Date().toISOString();
        this._lastVerifiedNoClickAtMs = Date.now();
        this._nextMonitorNoClickAt = this._lastVerifiedNoClickAtMs + SESSION_MONITOR_NO_CLICK_INTERVAL_MS;
        this._monitorPopupNeedsReopen = true;
        const authenticated = await this._authenticatedContextPage();
        if (authenticated) {
          this.health.setLoggedIn(true);
          this.health.setAuthRequired?.(null);
          this.health.setRecovery?.(null);
          this._setSessionStatus(this.sm.is(State.COLLECTING) ? 'ACTIVE' : 'AUTHENTICATED', {
            reasonCode: 'LOGOUT_PROMPT_NO_CLICK_SESSION_VERIFIED', successfulCheck: true,
          });
          log.info(`[WATCHDOG] Logout prompt dismissed with NO and Winner session verified source=${source}`);
          return true;
        }
        this.health.setLoggedIn(null);
        this._setSessionStatus('WARNING', {
          reasonCode: 'LOGOUT_PROMPT_DISMISSED_SESSION_UNVERIFIED', successfulCheck: true,
        });
        log.warn(`[WATCHDOG] NO click dismissed the logout prompt, but Winner session remains unverified source=${source}`);
        return false;
      }

      if (session.status === 'EXPIRED') {
        const reasonCode = session.reasonCode || 'LOGOUT_COUNTDOWN_ELAPSED';
        this.health.setLoggedIn(false);
        this.health.setAuthRequired?.('Winner session expired; automatic login recovery is required');
        this.health.setRecovery?.('AUTH_REQUIRED', { reasonCode, evidence: session.evidence || null });
        this._setSessionStatus('EXPIRED', { reasonCode, countdownSeconds: 0, successfulCheck: true });
        void this._alert(`AUTH_REQUIRED: ${reasonCode}`, this._page || page, {
          sessionRecovery: 'RELOGIN', reasonCode,
        });
        return false;
      }

      if (dismissal.status === 'FAILED') {
        this._monitorNoClickFailures += 1;
        const reloginCooldownElapsed = Date.now() - this._lastNoClickReloginAt
          >= SESSION_MONITOR_RELOGIN_COOLDOWN_MS;
        if (this._monitorNoClickFailures >= SESSION_MONITOR_NO_CLICK_FAILURES_BEFORE_RELOGIN
            && reloginCooldownElapsed) {
          const reasonCode = 'LOGOUT_NO_CLICK_RETRIES_EXHAUSTED';
          this._lastNoClickReloginAt = Date.now();
          this.health.setLoggedIn(false);
          this.health.setAuthRequired?.('Logout popup NO click failed twice; starting a fresh Winner login');
          this.health.setRecovery?.('AUTH_REQUIRED', {
            reasonCode,
            evidence: { failed_no_click_cycles: this._monitorNoClickFailures, source },
          });
          this._setSessionStatus('RECOVERING', { reasonCode, countdownSeconds });
          log.error(`[WATCHDOG] ${reasonCode}; requesting fresh login after failed popup handling source=${source}`);
          void this._alert(`AUTH_REQUIRED: ${reasonCode}`, this._page || page, {
            sessionRecovery: 'RELOGIN', reasonCode, recoveryPage: 'MONITOR',
          });
          return false;
        }
      }

      if (dismissal.status === 'NOT_PRESENT' && session.status !== 'EXPIRING') {
        const authenticated = await this._authenticatedContextPage();
        if (authenticated) {
          this.health.setLoggedIn(true);
          this.health.setAuthRequired?.(null);
          this.health.setRecovery?.(null);
          this._setSessionStatus(this.sm.is(State.COLLECTING) ? 'ACTIVE' : 'AUTHENTICATED', {
            reasonCode: 'SESSION_VERIFIED_WITHOUT_LOGOUT_PROMPT', successfulCheck: true,
          });
          return true;
        }
      }

      const verified = session.status === 'EXPIRING';
      this._setSessionStatus(verified ? 'EXPIRING' : 'WARNING', {
        reasonCode: session.reasonCode || dismissal.reasonCode || 'LOGOUT_POPUP_UNVERIFIED',
        countdownSeconds,
        successfulCheck: verified,
      });
      if (verified) log.warn(`[WATCHDOG] Logout countdown remains active seconds=${countdownSeconds} source=${source}`);
      else log.warn(`[WATCHDOG] Logout popup route requires verification (${dismissal.reasonCode}) source=${source}`);
      return verified;
    })();
    this._logoutPopupTasks.set(page, task);
    try { return await task; }
    finally { if (this._logoutPopupTasks.get(page) === task) this._logoutPopupTasks.delete(page); }
  }

  async _restoreSessionMonitorRoute(page, source, {
    forceNavigation = false, timeoutMs = 10000,
  } = {}) {
    if (!page || page.isClosed?.() || !this.browser.isSessionMonitorPage?.(page)) return false;
    if ((forceNavigation || !isLogoutPopupRoute(page.url?.() || '')) && typeof page.goto === 'function') {
      try {
        await page.goto(SESSION_MONITOR_URL, { waitUntil: 'commit', timeout: timeoutMs });
      } catch (error) {
        if (isLogoutPopupRoute(page.url?.() || '')) {
          const bodyReady = await page.locator('body').count().then(count => count > 0).catch(() => false);
          if (bodyReady) {
            log.info(`[WATCHDOG] Logout popup route is available after navigation timeout source=${source}`);
          } else {
            log.warn(`[WATCHDOG] Logout popup link could not be restored source=${source}: ${error.message}`);
            return false;
          }
        } else {
          log.warn(`[WATCHDOG] Logout popup link could not be restored source=${source}: ${error.message}`);
          return false;
        }
      }
    }
    if (!isLogoutPopupRoute(page.url?.() || '')) {
      log.warn(`[WATCHDOG] Logout popup link redirected source=${source} route=${safeRoute(page.url?.() || '')}`);
      return false;
    }
    try { await page.bringToFront?.(); }
    catch (error) { log.debug(`Restored logout popup tab could not be brought forward: ${error.message}`); }
    log.info(`[WATCHDOG] Restored the logout popup link in the monitor tab source=${source}`);
    return true;
  }

  _schedulePeriodicMonitorNoClick() {
    if (this._periodicMonitorNoClickPromise || Date.now() < this._nextMonitorNoClickAt) return;
    const page = this._monitorPage;
    if (!page || page.isClosed?.() || !this.browser.isAlive()
        || this._monitorRouteRestorePromise || this._logoutPopupTasks.get(page)
        || this.health.snapshot().network?.status !== 'ONLINE'
        || ([State.RELOGIN, State.RESTART_BROWSER, State.STARTING].includes(this.sm.current))) return;
    if (isExpiredAuthRoute(page.url?.() || '') && !isLogoutPopupRoute(page.url?.() || '')) return;

    const previousClickAt = this._lastVerifiedNoClickAt;
    log.info('[WATCHDOG] Starting scheduled logout popup NO click cycle in the monitor tab');
    const task = (async () => {
      const onPopupRoute = isLogoutPopupRoute(page.url?.() || '');
      const promptVisible = await page.locator('xpath=//*[contains(normalize-space(text()), "Log out?")]')
        .first().isVisible().catch(() => false);
      const shouldReopenPopup = !promptVisible && (this._monitorPopupNeedsReopen || !onPopupRoute
        || Date.now() - this._lastPeriodicMonitorNavigationAt >= 30000);
      if (shouldReopenPopup) {
        const restored = await this._restoreSessionMonitorRoute(page, 'periodic-no-click-cycle', {
          forceNavigation: true,
          timeoutMs: SESSION_MONITOR_NO_CLICK_NAVIGATION_TIMEOUT_MS,
        });
        if (!restored) return false;
        this._lastPeriodicMonitorNavigationAt = Date.now();
        this._monitorPopupNeedsReopen = false;
      }

      await this._recordLogoutPopup(page, 'periodic-no-click-cycle', {
        waitForPromptMs: SESSION_MONITOR_NO_CLICK_PROMPT_WAIT_MS,
      });
      return this._lastVerifiedNoClickAt !== previousClickAt;
    })();

    this._periodicMonitorNoClickPromise = task;
    void task.catch(error => {
      log.warn(`[WATCHDOG] Periodic monitor NO click cycle failed: ${error?.message || error}`);
    }).finally(() => {
      if (this._periodicMonitorNoClickPromise !== task) return;
      this._periodicMonitorNoClickPromise = null;
      if (!this._running) return;
      const clicked = this._lastVerifiedNoClickAt !== previousClickAt;
      if (!clicked) {
        this._nextMonitorNoClickAt = Date.now() + SESSION_MONITOR_NO_CLICK_RETRY_MS;
        log.warn('[WATCHDOG] Periodic monitor cycle did not verify a NO click; retrying shortly');
      }
    });
  }

  _scheduleMonitorRouteRestore(page, source) {
    if (!page || page !== this._monitorPage || page.isClosed?.()
        || this._monitorRouteRestorePromise
        || Date.now() - this._lastMonitorRouteRestoreAt < 30000) return;

    this._lastMonitorRouteRestoreAt = Date.now();
    const task = (async () => {
      const restored = await this._restoreSessionMonitorRoute(page, `login-route-${source}`, {
        forceNavigation: true,
      });
      if (restored) {
        await this._recordLogoutPopup(page, 'monitor-route-restored', { waitForPromptMs: 30000 });
      }
      return restored;
    })();
    this._monitorRouteRestorePromise = task;
    void task.catch(error => {
      log.warn(`[WATCHDOG] Session monitor route recovery failed source=${source}: ${error?.message || error}`);
    }).finally(() => {
      if (this._monitorRouteRestorePromise === task) this._monitorRouteRestorePromise = null;
    });
  }

  async _ensureSessionMonitorPage(mainPage) {
    if (process.env.SESSION_MONITOR_TAB_ENABLED === 'false') {
      this._setSessionStatus('AUTHENTICATED', { monitorTabStatus: 'DISABLED' });
      return null;
    }
    if (this._monitorPage && !this._monitorPage.isClosed?.()) {
      // The periodic NO-click cycle owns monitor navigation while it restores
      // or reloads the popup. Do not race it with the authenticated-session
      // check, which can otherwise interrupt the same navigation.
      if (this._periodicMonitorNoClickPromise || this._monitorRouteRestorePromise) {
        return this._monitorPage;
      }
      if (!isLogoutPopupRoute(this._monitorPage.url?.() || '')) {
        await this._restoreSessionMonitorRoute(this._monitorPage, 'authenticated-session-check');
      }
      return this._monitorPage;
    }
    if (typeof this.browser.getSessionMonitorPage !== 'function') return null;
    if (!this._restoreMonitorTabAfterClose && Date.now() - this._lastMonitorAttempt < 30000) return null;
    if (this._monitorSetupPromise) return this._monitorSetupPromise;
    this._lastMonitorAttempt = Date.now();
    this._setSessionStatus('RECOVERING', { reasonCode: 'MONITOR_TAB_STARTING' });
    this._monitorSetupPromise = (async () => {
      try {
        this._monitorPage = await this.browser.getSessionMonitorPage(mainPage);
        const monitorPage = this._monitorPage;
        monitorPage.on?.('close', () => {
          if (this._monitorPage !== monitorPage) return;
          this._monitorPage = null;
          this._restoreMonitorTabAfterClose = true;
          this._setSessionStatus('DISCONNECTED', { reasonCode: 'MONITOR_TAB_CLOSED', monitorTabStatus: 'CLOSED' });
          this._scheduleMonitorTabReopen('monitor-tab-closed');
        });
        monitorPage.on?.('crash', () => {
          if (this._monitorPage !== monitorPage) return;
          this._monitorPage = null;
          this._restoreMonitorTabAfterClose = true;
          log.warn('[WATCHDOG] Session monitor tab crashed; retry will use the existing browser context');
          this._setSessionStatus('RECOVERING', { reasonCode: 'MONITOR_TAB_CRASHED', monitorTabStatus: 'UNAVAILABLE' });
          this._scheduleMonitorTabReopen('monitor-tab-crashed');
        });
        monitorPage.on?.('framenavigated', frame => {
          try {
            if (frame !== monitorPage.mainFrame()) return;
            this._routeCheckPending = true;
            this._lastLoginCheck = 0;
            if (isLogoutPopupRoute(frame.url())) {
              const waitForPromptMs = this._periodicMonitorNoClickPromise
                ? SESSION_MONITOR_NO_CLICK_PROMPT_WAIT_MS : 30000;
              void this._recordLogoutPopup(monitorPage, 'monitor-tab-route-change', { waitForPromptMs });
            } else if (isExpiredAuthRoute(frame.url())) {
              this._markLoginRoute('monitor-tab-route-change');
            }
          } catch (error) {
            log.debug(`Session monitor route evidence could not be checked: ${error.message}`);
          }
        });
        if (this._restoreMonitorTabAfterClose) {
          const restored = await this._restoreSessionMonitorRoute(monitorPage, 'monitor-tab-reopened-after-close', {
            forceNavigation: true,
          });
          if (restored) this._restoreMonitorTabAfterClose = false;
        }
        const monitorSession = typeof this.login.inspectSession === 'function'
          ? await this.login.inspectSession(monitorPage, { relatedPages: [mainPage] })
          : { status: await this.login.isLoggedIn(monitorPage) ? 'AUTHENTICATED' : 'UNKNOWN' };
        if (isLogoutPopupRoute(monitorPage.url?.() || '')) {
          await this._recordLogoutPopup(monitorPage, 'monitor-tab-initial-check', { waitForPromptMs: 30000 });
          return monitorPage;
        }
        if (monitorSession.status === 'EXPIRED' || monitorSession.status === 'MANUAL_VERIFICATION') {
          this._setSessionStatus('EXPIRED', { reasonCode: monitorSession.reasonCode || 'MONITOR_SESSION_EXPIRED' });
          this.health.setLoggedIn(false);
          this.health.setAuthRequired?.('Winner session requires operator authentication or verification');
          void this._alert(`AUTH_REQUIRED: ${monitorSession.reasonCode || monitorSession.status}`, mainPage);
          return monitorPage;
        }
        this._monitorFailures = 0;
        log.info('[WATCHDOG] Same-context Winner logout monitor opened; visible logout prompts are handled with NO');
        this._setSessionStatus(monitorSession.status === 'AUTHENTICATED'
          ? (this.sm.is(State.COLLECTING) ? 'ACTIVE' : 'AUTHENTICATED') : 'WARNING', {
          reasonCode: monitorSession.reasonCode || 'MONITOR_SESSION_UNVERIFIED', monitorTabStatus: 'OPEN',
          successfulCheck: monitorSession.status === 'AUTHENTICATED',
        });
        return this._monitorPage;
      } catch (error) {
        this._monitorFailures += 1;
        log.warn(`[WATCHDOG] Session monitor tab unavailable attempt=${this._monitorFailures} reason=${error?.message || error}`);
        this._setSessionStatus('WARNING', { reasonCode: 'MONITOR_TAB_UNAVAILABLE', monitorTabStatus: 'UNAVAILABLE' });
        return null;
      } finally {
        this._monitorSetupPromise = null;
      }
    })();
    return this._monitorSetupPromise;
  }

  getSessionMonitorPage() {
    return this._monitorPage && !this._monitorPage.isClosed?.() ? this._monitorPage : null;
  }

  async refreshSessionMonitorAfterLogin(mainPage = this._page) {
    const page = await this._ensureSessionMonitorPage(mainPage);
    if (!page || page.isClosed?.()) return false;
    const restored = await this._restoreSessionMonitorRoute(page, 'verified-fresh-login', {
      forceNavigation: true,
      timeoutMs: SESSION_MONITOR_NO_CLICK_NAVIGATION_TIMEOUT_MS,
    });
    if (!restored) return false;
    this._lastPeriodicMonitorNavigationAt = Date.now();
    this._nextMonitorNoClickAt = Date.now() + SESSION_MONITOR_NO_CLICK_INTERVAL_MS;
    this._monitorPopupNeedsReopen = false;
    await this._recordLogoutPopup(page, 'verified-fresh-login', {
      waitForPromptMs: SESSION_MONITOR_NO_CLICK_PROMPT_WAIT_MS,
    });
    return true;
  }

  async _loop(signal) {
    while (this._running && !signal?.aborted) {
      await sleep(WATCHDOG_INTERVAL_MS, signal);
      if (!this._running || signal?.aborted) break;
      if (this._markLoginRoute('periodic-route-watchdog')) continue;
      if (Date.now() < this._quietUntil) continue;
      if (this.health.snapshot().network?.status !== 'ONLINE') continue;

      // Don't run checks while a recovery is already in progress
      const state = this.sm.current;
      if ([State.RELOGIN, State.RESTART_BROWSER, State.STARTING].includes(state)) {
        continue;
      }

      if (this._checking) continue;
      this._checking = true;
      try {
        await this._check(signal);
      } catch (err) {
        log.warn(`Watchdog: check threw — ${err.message}`);
      } finally {
        this._checking = false;
      }
    }
  }

  async _check(signal) {
    const page = this._page;
    if (this._markLoginRoute('watchdog-check')) return;

    // 1. Browser alive
    if (!this.browser.isAlive()) {
      this._setSessionStatus('DISCONNECTED', { reasonCode: 'BROWSER_DISCONNECTED', monitorTabStatus: 'UNAVAILABLE' });
      return this._alert('browser disconnected', page);
    }

    // 2. Page alive
    if (!page || page.isClosed()) {
      this._setSessionStatus('DISCONNECTED', { reasonCode: 'AVIATOR_PAGE_UNAVAILABLE' });
      return this._alert('page closed', page);
    }

    // A heartbeat probe runs against Winner's outer page, while Aviator is
    // hosted in a separate (often out-of-process) game frame. An outer-page
    // evaluate timeout is useful diagnostics, but is not evidence that the
    // game or its frame is dead. The collection loop checks the actual game
    // frame and observer; using this weak probe to recover reloads a working
    // Aviator page and can create an endless navigation loop.
    if (this.browser.isPageResponsive && !this.browser.isPageResponsive()) {
      if (Date.now() - this._lastPageProbeWarning >= 30000) {
        this._lastPageProbeWarning = Date.now();
        log.warn('Watchdog: outer-page heartbeat probe is delayed; preserving the Aviator tab and relying on game-frame checks');
      }
    }

    // 3. Correct canonical Aviator route — only check when COLLECTING.
    // Checking only the Winner hostname allowed a retired route alias to
    // appear healthy and prevented recovery from opening the current route.
    if (this.sm.is(State.COLLECTING) && !isLogoutPopupRoute(page.url())) {
      const url = page.url();
      if (!isAviatorRoute(url)) {
        return this._alert(`wrong Aviator route: ${safeRoute(url)}`, page);
      }
    }

    // 4. Login check — only when COLLECTING
    const shouldCheckSession = [State.HOME, State.GAME_LOADING, State.COLLECTING,
      State.WAITING_IFRAME, State.WAITING_HISTORY, State.NAVIGATING_AVIATOR,
      State.CHECKING_SESSION, State.RECOVERING].includes(this.sm.current);
    if (shouldCheckSession &&
        (this._loginFailures || Date.now() - this._lastLoginCheck >= LOGIN_CHECK_INTERVAL)) {
      this._lastLoginCheck = Date.now();
      const pages = this._contextPages();
      const sessionPage = this._monitorPage && !this._monitorPage.isClosed?.() ? this._monitorPage : page;
      const relatedPages = pages.filter(candidate => candidate !== sessionPage);
      const session = typeof this.login.inspectSession === 'function'
        ? await this.login.inspectSession(sessionPage, { relatedPages }).catch(error => {
          log.debug(`Watchdog authentication evidence probe failed: ${error.message}`);
          return { status: 'UNKNOWN', reasonCode: 'SESSION_PROBE_FAILED' };
        })
        : { status: await this.login.isLoggedIn(sessionPage).catch(error => {
          log.debug(`Watchdog authentication evidence probe failed: ${error.message}`);
          return false;
        }) ? 'AUTHENTICATED' : await this.login.isExplicitlyLoggedOut(sessionPage).catch(error => {
          log.debug(`Watchdog explicit logout probe failed: ${error.message}`);
          return false;
        }) ? 'EXPIRED' : 'UNKNOWN' };
      if (isLogoutPopupRoute(sessionPage.url?.() || '')) {
        await this._recordLogoutPopup(sessionPage, 'watchdog-session-check');
        return;
      }
      if (session.status === 'EXPIRED' || session.status === 'MANUAL_VERIFICATION') {
        this.health.setLoggedIn(false);
        this.health.setAuthRequired?.(session.status === 'EXPIRED'
          ? 'Winner session expiry was verified by page evidence'
          : 'Manual platform verification is required');
        this.health.setRecovery?.('AUTH_REQUIRED', { reasonCode: session.reasonCode, evidence: session.evidence || null });
        this._setSessionStatus('EXPIRED', { reasonCode: session.reasonCode || 'SESSION_EXPIRED', successfulCheck: true });
        return this._alert(`AUTH_REQUIRED: ${session.reasonCode || session.status}`, page);
      }
      if (session.status === 'EXPIRING') return;
      if (session.status === 'AUTHENTICATED') {
        this.health.setLoggedIn(true);
        this.health.setAuthRequired?.(null);
        this.health.setRecovery?.(null);
        this._loginFailures = 0;
        this._setSessionStatus(this.sm.is(State.COLLECTING) ? 'ACTIVE' : 'AUTHENTICATED', {
          reasonCode: session.reasonCode || 'SESSION_VERIFIED', successfulCheck: true,
        });
        await this._ensureSessionMonitorPage(page);
      } else {
        this.health.setLoggedIn(null);
        this._loginFailures = 0;
        this._setSessionStatus('WARNING', { reasonCode: session.reasonCode || 'SESSION_UNVERIFIED' });
        log.debug(`Watchdog session state is UNKNOWN: ${session.reasonCode || 'no explicit evidence'}`);
      }
      this._routeCheckPending = false;
    }

    // No new round by itself is not a browser fault. Heartbeat and the
    // collector mutation loop detect actual page/frame/observer failures.
    this.health.setBrowserConnected(true);
    this.health.setPageConnected(true);
  }

  async _alert(reason, page, details = {}) {
    if (this._lastAlert.reason === reason && Date.now() - this._lastAlert.at < 30000) return;
    this._lastAlert = { reason, at: Date.now() };
    log.warn(`Watchdog: unhealthy — ${reason}`);
    this.health.setBrowserConnected(this.browser.isAlive());
    this.health.setPageConnected(!!(page && !page.isClosed()));
    try {
      await this.onUnhealthy(reason, page, details);
    } catch (err) {
      log.error(`Watchdog: onUnhealthy callback threw — ${err.message}`);
    }
  }
}

/** Named production service for Winner session monitoring. */
export class WinnerSessionWatchdog extends Watchdog {}
