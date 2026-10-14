/** Controlled, single-flight recovery for collector-owned browser components. */
import { log, formatError } from './Logger.js';
import { State } from './StateMachine.js';
import { classifyError } from './RetryManager.js';
import { isAviatorRoute } from './LoginManager.js';

export class RecoveryManager {
  constructor({ stateMachine, browserManager, loginManager, frameManager = null, health, executionRecoveryCheck = null }) {
    this.sm = stateMachine;
    this.browser = browserManager;
    this.login = loginManager;
    this.frameManager = frameManager;
    this.health = health;
    this.executionRecoveryCheck = executionRecoveryCheck;
    this.browser.setExecutionSafetyCheck?.(executionRecoveryCheck);
    this._recoveryPromise = null;
    this._pendingVerification = false;
  }

  frameWaitTimeoutMs(startupTimeoutMs, recoveryTimeoutMs = process.env.BOT_RECOVERY_FRAME_TIMEOUT) {
    if (!this._pendingVerification) return startupTimeoutMs;
    const configured = Number(recoveryTimeoutMs);
    const recoveryTimeout = Number.isFinite(configured) && configured > 0 ? configured : 30000;
    return Math.min(startupTimeoutMs, recoveryTimeout);
  }

  async recover(errOrReason, page, signal) {
    if (signal?.aborted) throw new Error('Aborted');
    if (this._recoveryPromise) return this._recoveryPromise;
    this._recoveryPromise = this.browser.withRecoveryLock
      ? this.browser.withRecoveryLock(() => this._recover(errOrReason, page, signal))
      : this._recover(errOrReason, page, signal);
    try { return await this._recoveryPromise; }
    finally { this._recoveryPromise = null; }
  }

  async _recover(errOrReason, page, signal) {
    const reason = typeof errOrReason === 'string' ? errOrReason : formatError(errOrReason);
    const errClass = classifyError(errOrReason);
    const error = typeof errOrReason === 'object' ? errOrReason : null;
    if (errClass === 'AUTH_REQUIRED') {
      this.health.setLoggedIn(false);
      this.sm.transition(State.AUTH_REQUIRED, 'AUTH_REQUIRED');
      this.health.setRecovery?.('AUTH_REQUIRED', { reasonCode: error?.reasonCode || 'AUTH_REQUIRED', evidence: error?.evidence || null });
      this.browser.authRequired?.(reason);
      throw Object.assign(new Error('AUTH_REQUIRED: human login or verification is required'), { code: 'AUTH_REQUIRED' });
    }
    if (errClass === 'PERSISTENCE_ERROR') throw error || new Error(reason);
    if (errClass === 'NETWORK_RECOVER') {
      const status = error?.reasonCode === 'WINNER_DNS_FAILED' ? 'DNS_UNAVAILABLE' : 'OFFLINE';
      this.sm.transition(State.OFFLINE, error?.reasonCode || 'NETWORK_UNAVAILABLE');
      this.health.setNetwork?.({ status, reason: reason, reason_code: error?.reasonCode || 'NETWORK_UNAVAILABLE', checked_at: new Date().toISOString(), evidence: error?.evidence || null });
      this.health.setRecovery?.('NETWORK_WAIT', { reasonCode: error?.reasonCode || 'NETWORK_UNAVAILABLE', evidence: error?.evidence || null });
      throw error || Object.assign(new Error(reason), { code: 'NETWORK_UNAVAILABLE' });
    }
    if (['SITE_UNAVAILABLE', 'GAME_PROVIDER_UNAVAILABLE'].includes(errClass)) {
      this.sm.transition(State.SITE_UNAVAILABLE, error?.reasonCode || errClass);
      this.health.setSiteStatus?.('UNAVAILABLE');
      this.health.setRecovery?.('SITE_UNAVAILABLE', { reasonCode: error?.reasonCode || errClass, evidence: error?.evidence || null });
      throw error || Object.assign(new Error(reason), { code: errClass });
    }
    if (this.browser._circuitBreaker?.state === 'OPEN') {
      this.sm.transition(State.RECOVERING, 'CIRCUIT_BREAKER_OPEN');
      throw new Error('CIRCUIT_BREAKER_OPEN: automatic browser recovery paused');
    }

    this.sm.transition(State.RECOVERING, reason);
    this.health.recordRecovery(errClass);
    this.browser.beginRecovery?.(reason, { playwrightError: error?.message || null });
    const started = Date.now();
    const waitingForPayouts = error?.code === 'PAYOUTS_UNAVAILABLE';
    let frameReadyDuringRecovery = false;
    try {
      log.warn(`RecoveryManager: starting class=${errClass} reason="${reason}"`);
      if (errClass === 'FRAME_RECOVER' && !waitingForPayouts && this.frameManager
          && this.browser.isAlive() && page && !page.isClosed?.()) {
        try {
          await this.frameManager.waitForFrame(page, { timeoutMs: 1200, signal });
          frameReadyDuringRecovery = true;
          this.health.setGameReadiness?.({ ...this.frameManager.readiness(), contentLoaded: true, payoutsReady: true });
          log.info('RecoveryManager: fresh frame scan reacquired a payout-ready game frame without navigation');
        } catch (frameError) {
          this.health.setGameReadiness?.(this.frameManager.readiness());
          log.debug(`RecoveryManager: fast frame reacquisition did not complete: ${formatError(frameError)}`);
        }
      }
      if (!this.browser.isAlive()) {
        if (this.executionRecoveryCheck) {
          const executionState = await this.executionRecoveryCheck();
          if (!executionState?.safe) {
            this.health.setRecovery?.('EXECUTION_RECONCILIATION_REQUIRED', {
              reasonCode: 'BROWSER_RESTART_BLOCKED_BY_EXECUTION_STATE', evidence: executionState || { status: 'UNKNOWN' },
            });
            throw Object.assign(new Error('Browser restart is paused until unresolved execution state is reconciled'), {
              code: 'EXECUTION_RECONCILIATION_REQUIRED', evidence: executionState || { status: 'UNKNOWN' },
            });
          }
        }
        // Level 5 is allowed only after browser disconnection is observable.
        this.sm.transition(State.RESTART_BROWSER, 'browser-disconnected-confirmed');
        this.health.recordBrowserRestart();
        await this.browser.restart(signal);
        this.sm.transition(State.STARTING, 'browser-connection-restored');
        this.sm.transition(State.LOGIN, 'verify-restored-session');
        const getSharedPage = this.browser.getAviatorPage || this.browser.getHistoryPage;
        page = await getSharedPage.call(this.browser);
      } else if (!page || page.isClosed()) {
        // Level 3: replace the failed page in the same context.
        page = await this.browser.recoverHistoryPage(signal);
      } else if (error?.code === 'PAGE_CRASHED' || /page crashed|renderer process gone/i.test(reason)) {
        page = await this.browser.recoverHistoryPage(signal);
      } else {
        // Level 1: discard cached frame references and rescan on the next pass.
        const onAviator = isAviatorRoute(page.url());
        const waitingForGameFrame = error?.code === 'GAME_FRAME_MISSING'
          || /timed out waiting for Aviator iframe with payouts|game frame attachment/i.test(reason);
        if (frameReadyDuringRecovery) {
          log.info('RecoveryManager: using the newly reacquired frame; no route or reload action is needed');
        } else if (/page unresponsive/i.test(reason)) {
          // The old heartbeat probe evaluated Winner's outer document, not
          // the separate Aviator game frame. A slow outer document is not
          // enough evidence to reload the user's live game tab; leave it in
          // place and let the collection loop reacquire the actual frame.
          log.warn('RecoveryManager: outer-page probe was delayed; keeping the Aviator page open and rescanning its game frame');
        } else if (waitingForGameFrame) {
          const session = typeof this.login.inspectSession === 'function'
            ? await this.login.inspectSession(page)
            : { status: await this.login.isLoggedIn(page) ? 'AUTHENTICATED'
              : await this.login.isExplicitlyLoggedOut?.(page) ? 'EXPIRED' : 'UNKNOWN' };
          this.health.setRecovery?.('CHECKING_SESSION', { reasonCode: session.reasonCode || 'FRAME_MISSING', evidence: session.evidence || null });
          if (session.status === 'MANUAL_VERIFICATION') {
            throw Object.assign(new Error('AUTH_REQUIRED: human verification is required before Aviator recovery'), { code: 'AUTH_REQUIRED', evidence: session.evidence });
          }
          if (session.status === 'EXPIRED') {
            this.sm.transition(State.CHECKING_SESSION, session.reasonCode || 'verified-session-expiry');
            if (typeof this.login.ensureLoggedIn !== 'function') {
              throw Object.assign(new Error('AUTH_REQUIRED: verified expired session needs LoginManager'), { code: 'AUTH_REQUIRED' });
            }
            await this.login.ensureLoggedIn(page, signal);
            this.health.setLoggedIn(true);
          } else if (session.status !== 'AUTHENTICATED') {
            throw Object.assign(new Error('Session status is UNKNOWN; frame failure is not evidence of logout'), {
              code: 'SESSION_STATUS_UNKNOWN', evidence: session.evidence || null,
            });
          }

          const networkStatus = this.health.snapshot?.().network?.status;
          if (networkStatus === 'GAME_PROVIDER_UNAVAILABLE') {
            this.sm.transition(State.SITE_UNAVAILABLE, 'GAME_PROVIDER_UNAVAILABLE');
            throw Object.assign(new Error('GAME_PROVIDER_UNAVAILABLE: authenticated Winner page is reachable but the game provider probe is failing'), {
              code: 'GAME_PROVIDER_UNAVAILABLE', reasonCode: 'GAME_PROVIDER_PROBE_FAILED',
            });
          }

          this.sm.transition(State.NAVIGATING_AVIATOR, 'authenticated-game-frame-missing');
          this.health.setRecovery?.('NAVIGATING_AVIATOR', { reasonCode: 'GAME_FRAME_NOT_ATTACHED', evidence: session.evidence || null });
          const openAviator = this.login.openAviatorRoute || this.login.goToAviator;
          const opened = onAviator || (typeof openAviator === 'function'
            ? Boolean(await openAviator.call(this.login, page, signal)) : false);
          if (!opened) {
            throw Object.assign(new Error('GAME_FRAME_MISSING: shared Aviator route did not attach its frame; no reload or second session was started'), {
              code: 'GAME_FRAME_MISSING', reasonCode: 'GAME_FRAME_RECOVERY_EXHAUSTED',
              evidence: { route: this.browser._safeUrl?.(page.url()) || null, session: session.status },
            });
          }
        } else if (waitingForPayouts) {
          this.health.setGameReadiness?.({ frameAttached: true, contentLoaded: true, payoutsReady: false, reason: 'PAYOUT_SOURCE_NOT_READY' });
          this.sm.transition(State.WAITING_HISTORY, 'frame-attached-awaiting-payouts');
          log.warn('RecoveryManager: game frame is attached; leaving it in place while payout/history content initializes');
        } else if (!onAviator) {
          // Return the existing authenticated History tab to Aviator directly;
          // reloading an unrelated route can never reacquire the game frame.
          const current = page.url();
          const last = this.browser._safeUrl?.(current) || '';
          if (/authentication\/login|\/login/i.test(last)) {
            this.health.setLoggedIn(false);
            throw Object.assign(new Error('AUTH_REQUIRED: platform redirected to login'), { code: 'AUTH_REQUIRED' });
          }
          let session = typeof this.login.inspectSession === 'function'
            ? await this.login.inspectSession(page)
            : { status: await this.login.isLoggedIn(page) ? 'AUTHENTICATED'
              : await this.login.isExplicitlyLoggedOut?.(page) ? 'EXPIRED' : 'UNKNOWN' };
          if (session.status === 'EXPIRED') {
            this.sm.transition(State.CHECKING_SESSION, session.reasonCode || 'verified-session-expiry');
            await this.login.ensureLoggedIn(page, signal);
            session = { status: 'AUTHENTICATED', reasonCode: 'LOGIN_FLOW_VERIFIED' };
          }
          if (session.status === 'MANUAL_VERIFICATION') {
            throw Object.assign(new Error('AUTH_REQUIRED: manual verification is required on the current page'), { code: 'AUTH_REQUIRED', evidence: session.evidence });
          }
          if (session.status !== 'AUTHENTICATED') {
            throw Object.assign(new Error('Session evidence is UNKNOWN on a non-Aviator page; refusing to claim logout or navigate blindly'), {
              code: 'SESSION_STATUS_UNKNOWN', evidence: session.evidence || null,
            });
          }
          const networkStatus = this.health.snapshot?.().network?.status;
          if (networkStatus === 'GAME_PROVIDER_UNAVAILABLE') {
            throw Object.assign(new Error('GAME_PROVIDER_UNAVAILABLE: authenticated site is reachable but provider is offline'), {
              code: 'GAME_PROVIDER_UNAVAILABLE', reasonCode: 'GAME_PROVIDER_PROBE_FAILED',
            });
          }
          this.sm.transition(State.NAVIGATING_AVIATOR, 'authenticated-wrong-page');
          const route = this.login.openAviatorRoute || this.login.goToAviator;
          if (typeof route !== 'function') throw new Error('LoginManager cannot navigate the History page to Aviator');
          let routeResult = await route.call(this.login, page, signal);
          if (routeResult === false && typeof page.reload === 'function') {
            log.warn('RecoveryManager: authenticated wrong-page route attempt failed; reloading only the History page once');
            await page.reload({ waitUntil: 'domcontentloaded', timeout: 20000 });
            routeResult = await route.call(this.login, page, signal);
          }
          if (routeResult === false) throw Object.assign(new Error('GAME_FRAME_MISSING: authenticated Aviator route attempt failed'), {
            code: 'GAME_FRAME_MISSING', reasonCode: 'GAME_FRAME_NOT_ATTACHED',
          });
        }
      }

      if (!this.browser.isAlive() || !page || page.isClosed()) throw new Error('recovery evidence check failed: browser/page unavailable');
      let loggedIn = await this.login.isLoggedIn(page).catch(error => {
        log.debug(`Recovery could not read authenticated session evidence: ${formatError(error)}`);
        return false;
      });
      if (!loggedIn) {
        const explicitlyLoggedOut = (typeof this.login.isExplicitlyLoggedOut === 'function')
          ? await this.login.isExplicitlyLoggedOut(page).catch(error => {
            log.debug(`Recovery explicit logout check failed: ${formatError(error)}`);
            return false;
          })
          : false;
        this.health.setLoggedIn(false);
        if (explicitlyLoggedOut) {
          // A transient browser/CDP failure can leave the single managed page
          // on Winner's logout popup even though the saved session is still
          // valid. Recover authentication in this same browser context, then
          // return to Aviator; do not tear down a working collector just to
          // repeat its normal login startup path.
          log.warn('RecoveryManager: logout route detected after browser recovery; verifying session through LoginManager');
          try {
            if (!loggedIn) {
              const sessionRestored = await this.login.ensureLoggedIn(page, signal);
              if (sessionRestored) {
                let aviatorOpened = false;
                const openAviator = this.login.openAviatorRoute || this.login.goToAviator;
                if (typeof openAviator === 'function') {
                  try {
                    aviatorOpened = Boolean(await openAviator.call(this.login, page, signal));
                  } catch (routeError) {
                    log.warn(`RecoveryManager: authenticated session restored; Aviator frame navigation will retry: ${formatError(routeError)}`);
                  }
                }
                const loggedOutAgain = await this.login.isExplicitlyLoggedOut(page).catch(error => {
                  log.debug(`Post-login logout check failed: ${formatError(error)}`);
                  return false;
                });
                // Login validity and iframe readiness are separate evidence.
                // A delayed game frame must not be reported as an auth failure.
                loggedIn = !loggedOutAgain;
                if (loggedIn && !aviatorOpened) {
                  log.warn('RecoveryManager: authenticated session restored; game-frame attachment remains pending');
                }
              }
            }
          } catch (loginError) {
            throw Object.assign(new Error(`AUTH_REQUIRED: session could not be restored in the managed browser (${loginError?.message || 'login verification failed'})`), { code: 'AUTH_REQUIRED' });
          }
          if (!loggedIn) {
            throw Object.assign(new Error('AUTH_REQUIRED: LoginManager could not verify the restored Aviator session'), { code: 'AUTH_REQUIRED' });
          }
          log.info('RecoveryManager: authenticated session restored in the existing browser context');
        } else {
          // A failed probe after a transient page, frame, or CDP error is not
          // proof of logout. Only explicit login-page evidence (or the
          // AUTH_REQUIRED classifier above) may stop collection for a human.
          // Keep the existing context and retry the bounded frame scan.
          if (!loggedIn) {
            log.warn(`RecoveryManager: login state is not yet verifiable after ${errClass}; no explicit login page is visible, retrying without declaring AUTH_REQUIRED`);
          }
        }
      } else {
        this.health.setLoggedIn(true);
      }
      if (loggedIn) this.health.setLoggedIn(true);
      this.health.setAuthRequired?.(null);
      this.health.setBrowserConnected(true);
      this.health.setPageConnected(true);
      if (waitingForPayouts) {
        this.health.setGameReadiness?.({ frameAttached: true, contentLoaded: true, payoutsReady: false, reason: 'PAYOUT_SOURCE_NOT_READY' });
      } else {
        this.health.setGameReadiness?.({ frameAttached: false, contentLoaded: false, payoutsReady: false, reason: 'WAITING_FOR_GAME_FRAME' });
        this.health.setFrameConnected(false);
      }
      this._pendingVerification = true;
      if (this.sm.is(State.LOGIN)) this.sm.transition(State.HOME, 'restored-session-verified');
      if (this.sm.is(State.HOME)) this.sm.transition(State.GAME_LOADING, 'browser-restarted');
      if (!waitingForPayouts) this.sm.transition(State.WAITING_IFRAME, 'page-ready-awaiting-real-round');
      this.browser._persistMetadata?.();
      log.warn(`RecoveryManager: browser/page available; waiting for persisted real round verification (${Date.now() - started}ms)`);
      return page;
    } catch (recoveryError) {
      if (recoveryError?.code === 'AUTH_REQUIRED') {
        this.health.setAuthRequired?.(recoveryError.message);
        this.sm.transition(State.AUTH_REQUIRED, 'AUTH_REQUIRED');
        this.browser.authRequired?.(recoveryError.message);
      } else if (recoveryError?.code === 'GAME_PROVIDER_UNAVAILABLE' || recoveryError?.code === 'SITE_UNAVAILABLE') {
        this.health.setSiteStatus?.('UNAVAILABLE');
        this.sm.transition(State.SITE_UNAVAILABLE, recoveryError.code);
        this.browser.failRecovery?.(reason, recoveryError);
      } else {
        this.health.setRecovery?.('RECOVERY_FAILED', { reasonCode: recoveryError?.code || errClass, evidence: recoveryError?.evidence || null });
        this.browser.failRecovery?.(reason, recoveryError);
      }
      throw recoveryError;
    }
  }

  async verifyRound(round) {
    if (!this._pendingVerification) return;
    if (!round || !this.browser.isAlive()) return;
    this._pendingVerification = false;
    this.browser.completeRecovery?.(round);
    log.recovery('ROUND_VERIFIED', 'real round persisted after recovery', 0, true, 0);
  }
}
