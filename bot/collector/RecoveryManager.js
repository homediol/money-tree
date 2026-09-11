/**
 * RecoveryManager.js — Orchestrates all recovery sequences.
 *
 * Recovery ladder (least disruptive → most disruptive):
 *   1. FRAME_RECOVER  — discard frame ref, re-scan, reinstall observer
 *   2. PAGE_RECOVER   — reload/re-navigate page, re-login if needed
 *   3. BROWSER_FATAL  — full browser restart
 *
 * Every recovery is logged with reason, duration, success, and retry count.
 */

import { log, formatError } from './Logger.js';
import { State } from './StateMachine.js';
import { classifyError, sleep, backoffMs } from './RetryManager.js';

const FROZEN_THRESHOLD_S = 60; // seconds without a new round before triggering recovery

export class RecoveryManager {
  constructor({ stateMachine, browserManager, loginManager, health }) {
    this.sm      = stateMachine;
    this.browser = browserManager;
    this.login   = loginManager;
    this.health  = health;
    this._recoveryInProgress = false;
  }

  /**
   * Main recovery entry point.
   * Called whenever an error is caught in the main loop or watchdog fires.
   *
   * @param {Error|string} errOrReason
   * @param {Page|null} page
   * @param {AbortSignal} signal
   * @returns {Promise<Page>} — a usable page after recovery
   */
  async recover(errOrReason, page, signal) {
    if (signal?.aborted) throw new Error('Aborted');
    if (this._recoveryInProgress) {
      // Another recovery is already running — wait for it
      await sleep(3000, signal);
      return page;
    }

    this._recoveryInProgress = true;
    const reason  = typeof errOrReason === 'string' ? errOrReason : formatError(errOrReason);
    const errClass = typeof errOrReason === 'string' ? 'FRAME_RECOVER' : classifyError(errOrReason);
    const start   = Date.now();
    let attempt   = 0;

    this.sm.transition(State.RECOVERING, reason);
    this.health.recordRecovery(errClass);

    try {
      log.warn(`RecoveryManager: starting recovery class=${errClass} reason="${reason}"`);

      if (errClass === 'BROWSER_FATAL') {
        page = await this._recoverBrowser(signal);
      } else if (errClass === 'PAGE_RECOVER') {
        page = await this._recoverPage(page, signal);
      } else {
        // FRAME_RECOVER — cheapest, try first
        page = await this._recoverFrame(page, signal);
      }

      const duration = Date.now() - start;
      log.recovery(errClass, reason, duration, true, attempt);
      log.info(`RecoveryManager: recovery complete in ${duration}ms`);
      return page;

    } catch (err) {
      const duration = Date.now() - start;
      log.recovery(errClass, reason, duration, false, attempt);
      log.error(`RecoveryManager: recovery failed — ${formatError(err)}`);

      // Escalate to browser restart as last resort
      if (errClass !== 'BROWSER_FATAL') {
        log.warn('RecoveryManager: escalating to browser restart');
        try {
          page = await this._recoverBrowser(signal);
          return page;
        } catch (fatal) {
          log.error(`RecoveryManager: browser restart also failed — ${formatError(fatal)}`);
          // Wait and let the outer loop retry
          await sleep(backoffMs(5), signal);
          throw fatal;
        }
      }
      throw err;
    } finally {
      this._recoveryInProgress = false;
    }
  }

  // ── Recovery levels ───────────────────────────────────────────────────────

  /** Level 1: Frame recovery — just discard the stale frame reference.
   *  The main loop will re-scan for a fresh frame on its next iteration. */
  async _recoverFrame(page, signal) {
    log.info('RecoveryManager: frame recovery — discarding stale frame ref');
    await sleep(1000, signal);

    // Verify the page is still alive
    if (!page || page.isClosed()) {
      log.warn('RecoveryManager: page is closed during frame recovery — escalating');
      return this._recoverPage(null, signal);
    }

    // Check if we're still on the Aviator page
    const url = page.url().toLowerCase();
    if (!url.includes('aviator') && !url.includes('crash')) {
      log.warn('RecoveryManager: not on Aviator page — navigating back');
      await this._ensureOnAviator(page, signal);
    }

    this.sm.transition(State.WAITING_IFRAME, 'frame-recovery');
    return page;
  }

  /** Level 2: Page recovery — reload or re-navigate, re-login if needed. */
  async _recoverPage(page, signal) {
    log.info('RecoveryManager: page recovery');

    if (!page || page.isClosed()) {
      log.warn('RecoveryManager: page closed — getting new page from browser');
      try {
        page = await this.browser.getPage();
      } catch {
        return this._recoverBrowser(signal);
      }
    }

    // Check login status
    this.sm.transition(State.RELOGIN, 'page-recovery');
    try {
      await this.login.ensureLoggedIn(page, signal);
      this.health.recordLogin();
    } catch (err) {
      log.error(`RecoveryManager: re-login failed — ${formatError(err)}`);
      return this._recoverBrowser(signal);
    }

    await this._ensureOnAviator(page, signal);
    return page;
  }

  /** Level 3: Browser restart — only when browser is truly dead. */
  async _recoverBrowser(signal) {
    log.warn('RecoveryManager: browser restart');
    this.sm.transition(State.RESTART_BROWSER, 'browser-fatal');
    this.health.recordBrowserRestart();

    await this.browser.restart(signal);
    const page = await this.browser.getPage();

    this.sm.transition(State.STARTING, 'browser-restarted');
    this.sm.transition(State.LOGIN, 'post-restart-login');

    await this.login.ensureLoggedIn(page, signal);
    this.health.recordLogin();

    await this._ensureOnAviator(page, signal);
    return page;
  }

  async _ensureOnAviator(page, signal) {
    this.sm.transition(State.GAME_LOADING, 'navigating-to-aviator');
    await this.login.goToAviator(page, signal);
    this.sm.transition(State.WAITING_IFRAME, 'on-aviator-page');
  }

  /**
   * Check if the collector appears frozen (no new rounds for too long).
   * Returns true if recovery should be triggered.
   */
  isFrozen(health) {
    const secs = health.secondsSinceLastRound();
    if (secs === null) return false; // no rounds yet — not frozen
    return secs > FROZEN_THRESHOLD_S;
  }
}
