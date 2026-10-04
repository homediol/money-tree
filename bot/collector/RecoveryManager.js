/** Controlled, single-flight recovery for collector-owned browser components. */
import { log, formatError } from './Logger.js';
import { State } from './StateMachine.js';
import { classifyError } from './RetryManager.js';

export class RecoveryManager {
  constructor({ stateMachine, browserManager, loginManager, health }) {
    this.sm = stateMachine;
    this.browser = browserManager;
    this.login = loginManager;
    this.health = health;
    this._recoveryPromise = null;
    this._pendingVerification = false;
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
      this.sm.transition(State.RELOGIN, 'AUTH_REQUIRED');
      this.browser.authRequired?.(reason);
      throw Object.assign(new Error('AUTH_REQUIRED: human login or verification is required'), { code: 'AUTH_REQUIRED' });
    }
    if (errClass === 'PERSISTENCE_ERROR') throw error || new Error(reason);
    if (this.browser._circuitBreaker?.state === 'OPEN') {
      this.sm.transition(State.RECOVERING, 'CIRCUIT_BREAKER_OPEN');
      throw new Error('CIRCUIT_BREAKER_OPEN: automatic browser recovery paused');
    }

    this.sm.transition(State.RECOVERING, reason);
    this.health.recordRecovery(errClass);
    this.browser.beginRecovery?.(reason, { playwrightError: error?.message || null });
    const started = Date.now();
    try {
      log.warn(`RecoveryManager: starting class=${errClass} reason="${reason}"`);
      if (!this.browser.isAlive()) {
        // Level 5 is allowed only after browser disconnection is observable.
        this.sm.transition(State.RESTART_BROWSER, 'browser-disconnected-confirmed');
        this.health.recordBrowserRestart();
        await this.browser.restart(signal);
        this.sm.transition(State.STARTING, 'browser-connection-restored');
        this.sm.transition(State.LOGIN, 'verify-restored-session');
        page = await this.browser.getHistoryPage();
      } else if (!page || page.isClosed()) {
        // Level 3: replace the failed page in the same context.
        page = await this.browser.recoverHistoryPage(signal);
      } else {
        // Level 1: discard cached frame references and rescan on the next pass.
        const onAviator = /aviator|crash-games|\/crash/i.test(page.url());
        if (/page unresponsive/i.test(reason)) {
          // Level 2: reload only the page whose responsiveness probe failed.
          await page.reload({ waitUntil: 'domcontentloaded', timeout: 20000 });
        } else if (!onAviator) {
          // Level 2: only reload the affected page when it navigated away.
          const current = page.url();
          const last = this.browser._safeUrl?.(current) || '';
          if (/authentication\/login|\/login/i.test(last)) {
            this.health.setLoggedIn(false);
            throw Object.assign(new Error('AUTH_REQUIRED: platform redirected to login'), { code: 'AUTH_REQUIRED' });
          }
          await page.reload({ waitUntil: 'domcontentloaded', timeout: 20000 });
        }
      }

      if (!this.browser.isAlive() || !page || page.isClosed()) throw new Error('recovery evidence check failed: browser/page unavailable');
      const loggedIn = await this.login.isLoggedIn(page).catch(() => false);
      this.health.setLoggedIn(loggedIn);
      if (!loggedIn) throw Object.assign(new Error('AUTH_REQUIRED: session could not be verified after recovery'), { code: 'AUTH_REQUIRED' });
      this.health.setAuthRequired?.(null);
      this.health.setBrowserConnected(true);
      this.health.setPageConnected(true);
      this.health.setFrameConnected(false);
      this._pendingVerification = true;
      if (this.sm.is(State.LOGIN)) this.sm.transition(State.HOME, 'restored-session-verified');
      if (this.sm.is(State.HOME)) this.sm.transition(State.GAME_LOADING, 'browser-restarted');
      this.sm.transition(State.WAITING_IFRAME, 'page-ready-awaiting-real-round');
      this.browser._persistMetadata?.();
      log.warn(`RecoveryManager: browser/page available; waiting for persisted real round verification (${Date.now() - started}ms)`);
      return page;
    } catch (recoveryError) {
      if (recoveryError?.code === 'AUTH_REQUIRED') this.browser.authRequired?.(recoveryError.message);
      else this.browser.failRecovery?.(reason, recoveryError);
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
