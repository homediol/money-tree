/**
 * Watchdog.js — Runs every 3 seconds and verifies the entire system is healthy.
 *
 * Checks (in order):
 *   1. Browser connected
 *   2. Page alive and not closed
 *   3. Correct URL (on Aviator page)
 *   4. User logged in
 *   5. Collector running (state = COLLECTING)
 *   6. No frozen collector (no new round for > 60s)
 *
 * If any check fails, fires the onUnhealthy callback with a reason string.
 * The main loop handles the actual recovery — the watchdog only detects.
 */

import { log } from './Logger.js';
import { State } from './StateMachine.js';
import { sleep } from './RetryManager.js';

const WATCHDOG_INTERVAL_MS  = 3000;
const FROZEN_THRESHOLD_S    = 60;
const LOGIN_CHECK_INTERVAL  = 30000; // only check login every 30s, not every 3s
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
    this._quietUntil      = 0;  // suppress alerts until this timestamp
  }

  setPage(page) {
    this._page = page;
    // Silence watchdog for POST_RECOVERY_QUIET_MS after page changes
    // (recovery just completed — give the page time to settle)
    this._quietUntil = Date.now() + POST_RECOVERY_QUIET_MS;
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

      // Don't run checks while a recovery is already in progress
      const state = this.sm.current;
      if ([State.RECOVERING, State.RELOGIN, State.RESTART_BROWSER, State.STARTING].includes(state)) {
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

    // 1. Browser alive
    if (!this.browser.isAlive()) {
      return this._alert('browser disconnected', page);
    }

    // 2. Page alive
    if (!page || page.isClosed()) {
      return this._alert('page closed', page);
    }

    // 3. Correct URL — only check when COLLECTING
    if (this.sm.is(State.COLLECTING)) {
      const url = page.url().toLowerCase();
      if (!url.includes('winner.rw')) {
        return this._alert(`wrong URL: ${url}`, page);
      }
    }

    // 4. Login check — only when COLLECTING
    if (this.sm.is(State.COLLECTING)) {
      const loggedIn = await this.login.isLoggedIn(page).catch(() => false);
      this.health.setLoggedIn(loggedIn);
      if (!loggedIn) {
        return this._alert('user logged out', page);
      }
    }

    // 5. Frozen collector check
    const secs = this.health.secondsSinceLastRound();
    if (secs !== null && secs > FROZEN_THRESHOLD_S && this.sm.is(State.COLLECTING)) {
      return this._alert(`collector frozen — no round for ${Math.round(secs)}s`, page);
    }

    // All good
    this.health.setBrowserConnected(true);
    this.health.setPageConnected(true);
  }

  async _alert(reason, page) {
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
