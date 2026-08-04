/**
 * index.js — Production-grade Aviator collector orchestrator.
 *
 * Wires together all modules and runs the infinite collection loop.
 * Never calls process.exit(). Every error is caught and recovered.
 *
 * State flow:
 *   STARTING → LOGIN → HOME → GAME_LOADING → WAITING_IFRAME → COLLECTING
 *                                                    ↑               |
 *                                                    └── RECOVERING ←┘
 *                                                          ↓
 *                                                    RELOGIN / RESTART_BROWSER
 */

import path from 'path';
import fs from 'fs';
import { fileURLToPath } from 'url';

import { log }             from './Logger.js';
import { StateMachine, State } from './StateMachine.js';
import { BrowserManager }  from './BrowserManager.js';
import { LoginManager }    from './LoginManager.js';
import { FrameManager }    from './FrameManager.js';
import { Collector }       from './Collector.js';
import { HealthMonitor }   from './HealthMonitor.js';
import { RecoveryManager } from './RecoveryManager.js';
import { Watchdog }        from './Watchdog.js';
import { sleep, backoffMs } from './RetryManager.js';
import {
  readRoundHistory, writeRoundHistory,
  inferNewMultipliers, appendRounds,
  snapshotSignature, fmt,
} from './HistoryManager.js';

const __dirname  = path.dirname(fileURLToPath(import.meta.url));
const CONFIG_PATH = path.join(__dirname, '..', '..', 'data', 'bot', 'config.json');

// ── Load credentials ──────────────────────────────────────────────────────────

function loadCredentials() {
  try {
    const cfg = JSON.parse(fs.readFileSync(CONFIG_PATH, 'utf-8'));
    return {
      phone:    process.env.WINNER_PHONE    || cfg.phone    || '',
      password: process.env.WINNER_PASSWORD || cfg.password || '',
      headless: (process.env.BOT_HEADLESS   || String(cfg.headless ?? 'false')).toLowerCase() === 'true',
    };
  } catch {
    return {
      phone:    process.env.WINNER_PHONE    || '',
      password: process.env.WINNER_PASSWORD || '',
      headless: false,
    };
  }
}

// ── Main collector class ──────────────────────────────────────────────────────

class AviatorCollector {
  constructor(credentials, signal) {
    this.creds   = credentials;
    this.signal  = signal;

    this.sm       = new StateMachine();
    this.browser  = new BrowserManager(credentials.headless);
    this.loginMgr = new LoginManager(credentials);
    this.frameMgr = new FrameManager();
    this.collector= new Collector();
    this.health   = new HealthMonitor();
    this.recovery = new RecoveryManager({
      stateMachine:  this.sm,
      browserManager:this.browser,
      loginManager:  this.loginMgr,
      health:        this.health,
    });
    this.watchdog = new Watchdog({
      stateMachine:  this.sm,
      browserManager:this.browser,
      loginManager:  this.loginMgr,
      health:        this.health,
      onUnhealthy:   (reason, page) => this._onWatchdogAlert(reason, page),
    });

    // Round history state
    this._history     = readRoundHistory();
    this._prevSnapshot= null;
    this._prevSig     = null;
    this._lastSavedSig= null;
    this._totalAdded  = 0;

    // Current page reference (shared with watchdog)
    this._page = null;

    // Watchdog recovery signal — set when watchdog fires, cleared after recovery
    this._watchdogReason = null;
  }

  async _onWatchdogAlert(reason, page) {
    // Signal the main loop to recover on its next iteration
    this._watchdogReason = reason;
  }

  async run() {
    this.health.start();
    this.health.setCollectorRunning(true);

    // Sync state machine transitions to health monitor
    this.sm.onTransition((state) => this.health.setState(state));

    log.info('=== Aviator Collector starting ===');

    // ── Step 1: Launch browser ────────────────────────────────────────────
    await this.browser.launch(this.signal);
    this._page = await this.browser.getPage();
    this.health.setBrowserConnected(true);

    // ── Step 2: Login ─────────────────────────────────────────────────────
    this.sm.transition(State.LOGIN, 'initial-start');
    await this.loginMgr.ensureLoggedIn(this._page, this.signal);
    this.health.setLoggedIn(true);
    this.health.recordLogin();

    // ── Step 3: Navigate to Aviator ───────────────────────────────────────
    this.sm.transition(State.HOME, 'logged-in');
    this.sm.transition(State.GAME_LOADING, 'navigating');
    await this.loginMgr.goToAviator(this._page, this.signal);

    // ── Step 4: Start watchdog ────────────────────────────────────────────
    this.watchdog.setPage(this._page);
    this.watchdog.start(this.signal);

    // ── Step 5: Infinite collection loop ─────────────────────────────────
    await this._collectionLoop();
  }

  async _collectionLoop() {
    let attempt = 0;

    while (!this.signal?.aborted) {
      // Check if watchdog flagged an issue
      if (this._watchdogReason) {
        const reason = this._watchdogReason;
        this._watchdogReason = null;
        log.warn(`Collection loop: watchdog alert — ${reason}`);
        this._page = await this.recovery.recover(reason, this._page, this.signal).catch(err => {
          log.error(`Recovery failed: ${err.message}`);
          return this._page;
        });
        this.watchdog.setPage(this._page);
        attempt = 0;
        continue;
      }

      try {
        await this._collectOnce();
        attempt = 0; // reset backoff on success
      } catch (err) {
        if (this.signal?.aborted) break;

        log.error(`Collection error: ${err.message}`);
        const delay = backoffMs(attempt++);
        log.warn(`Recovering in ${delay}ms (attempt ${attempt})`);

        this._page = await this.recovery.recover(err, this._page, this.signal).catch(recErr => {
          log.error(`Recovery threw: ${recErr.message}`);
          return this._page;
        });
        this.watchdog.setPage(this._page);

        await sleep(delay, this.signal);
      }
    }

    this.watchdog.stop();
    this.health.setCollectorRunning(false);
    this.health.stop();
    log.info(`=== Collector stopped. Total rounds saved: ${this._totalAdded} ===`);
  }

  /**
   * One full collection cycle:
   *   1. Get fresh frame
   *   2. Wait for payouts
   *   3. Install observer
   *   4. Run mutation loop until frame dies
   */
  async _collectOnce() {
    // Only transition to WAITING_IFRAME if not already there (recovery may have set it)
    if (!this.sm.is(State.WAITING_IFRAME)) {
      this.sm.transition(State.WAITING_IFRAME, 'seeking-frame');
    }

    // Always get a fresh frame — never reuse a cached reference
    const frame = await this.frameMgr.waitForFrame(this._page, { signal: this.signal });
    this.health.setFrameConnected(true);

    await this.frameMgr.waitForPayouts(frame, this.signal);

    const { multipliers: initMults, signature: initSig } = await this.collector.install(frame);

    // Establish or update baseline snapshot
    if (!this._prevSnapshot) {
      this._prevSnapshot = initMults;
      this._prevSig      = initSig;
      log.info(`Baseline: ${initMults.length} rounds`);
    } else if (initSig !== this._prevSig) {
      // Reconnected after reload — catch up missed rounds
      const inferred = inferNewMultipliers(this._prevSnapshot, initMults);
      if (inferred.length > 0) {
        const { history, added } = appendRounds(this._history, inferred);
        if (added.length > 0) {
          this._history = history;
          writeRoundHistory(this._history);
          this._totalAdded += added.length;
          added.forEach(() => this.health.recordRound());
          log.info(`Catch-up: saved ${added.length} missed round(s)`);
        }
      }
      this._prevSnapshot = initMults;
      this._prevSig      = initSig;
    }

    this.sm.transition(State.COLLECTING, 'observer-installed');
    this.health.setCollectorRunning(true);
    log.info('Collecting...');

    // ── Mutation loop ─────────────────────────────────────────────────────
    while (!this.signal?.aborted) {
      // Check watchdog alert inside the inner loop too
      if (this._watchdogReason) throw new Error(this._watchdogReason);

      // Verify frame is still alive before waiting
      if (!this.frameMgr.isFrameAlive(frame)) {
        throw new Error('Frame detached');
      }

      const event = await this.collector.waitForMutation(frame);

      if (this.signal?.aborted) break;

      if (event?.error) {
        throw new Error(event.error);
      }

      if (event?.timeout) {
        // No mutation in idle window — verify frame is still alive
        if (!this.frameMgr.isFrameAlive(frame)) {
          throw new Error('Frame detached during idle');
        }
        // Re-verify observer is still installed
        const installed = await this.collector.isInstalled(frame).catch(() => false);
        if (!installed) throw new Error('collector_not_installed');
        continue;
      }

      if (!event?.multipliers?.length) continue;

      const currSig = event.signature;
      if (currSig === this._prevSig) continue;

      const inferred = inferNewMultipliers(this._prevSnapshot, event.multipliers);
      if (inferred.length === 0) {
        this._prevSnapshot = event.multipliers;
        this._prevSig      = currSig;
        continue;
      }

      // Save new rounds
      if (currSig !== this._lastSavedSig) {
        const newest = inferred[0];
        log.info(`New round: ${fmt(newest)}`);
        process.stdout.write(`  ✔ New round detected: ${fmt(newest)}\n`);

        const { history, added } = appendRounds(this._history, inferred);
        if (added.length > 0) {
          this._history      = history;
          this._lastSavedSig = currSig;
          this._totalAdded  += added.length;
          writeRoundHistory(this._history);
          added.forEach(() => this.health.recordRound());
          log.info(`History updated: ${this._history.length} rounds saved`);
          process.stdout.write(`  ✔ History updated: ${this._history.length} rounds saved\n`);
        }
      }

      this._prevSnapshot = event.multipliers;
      this._prevSig      = currSig;
    }
  }
}

// ── Entry point ───────────────────────────────────────────────────────────────

export async function startCollector(signal) {
  const creds = loadCredentials();

  if (!creds.phone || !creds.password) {
    log.error('Phone or password missing. Set in data/bot/config.json or WINNER_PHONE/WINNER_PASSWORD env vars.');
    return;
  }

  const masked = creds.phone.slice(-4).padStart(creds.phone.length, '*');
  log.info(`Starting collector for ${masked} (headless=${creds.headless})`);

  const collector = new AviatorCollector(creds, signal);

  // Infinite outer loop — never exits unless signal is aborted
  while (!signal?.aborted) {
    try {
      await collector.run();
    } catch (err) {
      if (signal?.aborted) break;
      log.error(`Outer loop error: ${err.message} — restarting in 10s`);
      await sleep(10000, signal);
    }
  }

  log.info('Collector shut down cleanly.');
}
