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
import { isAcknowledgedAuthRoute, isExpiredAuthRoute } from './LoginManager.js';

function safeRoute(raw) {
  try { const url = new URL(raw); return `${url.origin}${url.pathname}`; }
  catch (error) { log.debug(`Page route could not be sanitized: ${error.message}`); return 'UNKNOWN_URL'; }
}

const WATCHDOG_INTERVAL_MS  = 3000;
// Legitimate Aviator rounds can run beyond one minute. A 60-second threshold
// caused healthy high-multiplier rounds to be treated as collector failures.
const LOGIN_CHECK_INTERVAL  = 10000; // bounded session check while frame/history waits may be long
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
            if (isExpiredAuthRoute(frame.url())) this._markLoginRoute('history-page-route-change');
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
      const candidates = [{ page: this._page, name: 'Aviator' }];
      const detected = candidates.find(({ page }) => {
        try {
          return page && !page.isClosed?.() && isExpiredAuthRoute(page.url())
            && (page === this._page || !isAcknowledgedAuthRoute(page));
        }
        catch (error) { log.debug(`Page login-route evidence could not be checked: ${error.message}`); return false; }
      });
      if (!detected) return false;
      const route = safeRoute(detected.page.url());
      this.health.setLoggedIn(false);
      this.health.setAuthRequired?.(`Winner ${detected.name} page is on a login or logout route`);
      this.health.setRecovery?.('AUTH_REQUIRED', {
        reasonCode: 'LOGIN_ROUTE_VISIBLE', evidence: { page: detected.name.toLowerCase(), route, source },
      });
      void this._alert(`AUTH_REQUIRED: login route visible on ${detected.name} page`, detected.page);
      return true;
    } catch (error) {
      log.debug(`Managed-page login-route evidence could not be checked: ${error.message}`);
      return false;
    }
  }

  start(signal) {
    if (this._running) return;
    this._running = true;
    log.info('Watchdog: started');
    this._loop(signal);
  }

  stop() {
    this._running = false;
    if (this._timer) { clearTimeout(this._timer); this._timer = null; }
    log.info('Watchdog: stopped');
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
      return this._alert('browser disconnected', page);
    }

    // 2. Page alive
    if (!page || page.isClosed()) {
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

    // 3. Correct URL — only check when COLLECTING
    if (this.sm.is(State.COLLECTING)) {
      const url = page.url().toLowerCase();
      if (!url.includes('winner.rw')) {
        return this._alert(`wrong URL: ${safeRoute(url)}`, page);
      }
    }

    // 4. Login check — only when COLLECTING
    const shouldCheckSession = [State.COLLECTING, State.WAITING_IFRAME, State.WAITING_HISTORY,
      State.NAVIGATING_AVIATOR, State.CHECKING_SESSION, State.RECOVERING].includes(this.sm.current);
    if (shouldCheckSession &&
        (this._loginFailures || Date.now() - this._lastLoginCheck >= LOGIN_CHECK_INTERVAL)) {
      this._lastLoginCheck = Date.now();
      const session = typeof this.login.inspectSession === 'function'
        ? await this.login.inspectSession(page).catch(error => {
          log.debug(`Watchdog authentication evidence probe failed: ${error.message}`);
          return { status: 'UNKNOWN', reasonCode: 'SESSION_PROBE_FAILED' };
        })
        : { status: await this.login.isLoggedIn(page).catch(error => {
          log.debug(`Watchdog authentication evidence probe failed: ${error.message}`);
          return false;
        }) ? 'AUTHENTICATED' : await this.login.isExplicitlyLoggedOut(page).catch(error => {
          log.debug(`Watchdog explicit logout probe failed: ${error.message}`);
          return false;
        }) ? 'EXPIRED' : 'UNKNOWN' };
      if (session.status === 'EXPIRED' || session.status === 'MANUAL_VERIFICATION') {
        this.health.setLoggedIn(false);
        this.health.setAuthRequired?.(session.status === 'EXPIRED'
          ? 'Winner session expiry was verified by page evidence'
          : 'Manual platform verification is required');
        this.health.setRecovery?.('AUTH_REQUIRED', { reasonCode: session.reasonCode, evidence: session.evidence || null });
        return this._alert(`AUTH_REQUIRED: ${session.reasonCode || session.status}`, page);
      }
      if (session.status === 'AUTHENTICATED') {
        this.health.setLoggedIn(true);
        this.health.setAuthRequired?.(null);
        this.health.setRecovery?.(null);
        this._loginFailures = 0;
      } else {
        this.health.setLoggedIn(null);
        this._loginFailures = 0;
        log.debug(`Watchdog session state is UNKNOWN: ${session.reasonCode || 'no explicit evidence'}`);
      }
      this._routeCheckPending = false;
    }

    // No new round by itself is not a browser fault. Heartbeat and the
    // collector mutation loop detect actual page/frame/observer failures.
    this.health.setBrowserConnected(true);
    this.health.setPageConnected(true);
  }

  async _alert(reason, page) {
    if (this._lastAlert.reason === reason && Date.now() - this._lastAlert.at < 30000) return;
    this._lastAlert = { reason, at: Date.now() };
    log.warn(`Watchdog: unhealthy — ${reason}`);
    this.health.setBrowserConnected(this.browser.isAlive());
    this.health.setPageConnected(!!(page && !page.isClosed()));
    try {
      await this.onUnhealthy(reason, page);
    } catch (err) {
      log.error(`Watchdog: onUnhealthy callback threw — ${err.message}`);
    }
  }
}
