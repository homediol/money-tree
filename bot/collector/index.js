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
import { randomUUID } from 'crypto';
import { fileURLToPath } from 'url';

import { log, formatError } from './Logger.js';
import { StateMachine, State } from './StateMachine.js';
import { getBrowserSupervisor } from './BrowserManager.js';
import { LoginManager }    from './LoginManager.js';
import { FrameManager }    from './FrameManager.js';
import { Collector }       from './Collector.js';
import { HealthMonitor }   from './HealthMonitor.js';
import { RecoveryManager } from './RecoveryManager.js';
import { Watchdog }        from './Watchdog.js';
import { PostgresRoundStore } from './PostgresRoundStore.js';
import { sleep, backoffMs, classifyError } from './RetryManager.js';
import { waitForNetwork } from './NetworkMonitor.js';
import {
  readRoundHistory,
  inferNewMultipliers, appendRounds,
  snapshotSignature, fmt,
} from './HistoryManager.js';

const __dirname  = path.dirname(fileURLToPath(import.meta.url));
const CONFIG_PATH = path.join(__dirname, '..', '..', 'data', 'bot', 'config.json');

export function selectMissingLegacyRounds(fileHistory, persisted) {
  const persistedIds = new Set(persisted.map(round => String(round.round_id)));
  const persistedIndices = new Set(persisted.map(round => Number(round.round_index)));
  const missing = [];
  let indexConflicts = 0;
  for (const round of fileHistory) {
    if (persistedIds.has(String(round.round_id))) continue;
    if (persistedIndices.has(Number(round.round_index))) {
      indexConflicts += 1;
      continue;
    }
    missing.push(round);
  }
  return { missing, indexConflicts };
}

// ── Load credentials ──────────────────────────────────────────────────────────

function loadCredentials() {
  try {
    const cfg = JSON.parse(fs.readFileSync(CONFIG_PATH, 'utf-8'));
    return {
      phone:    process.env.WINNER_PHONE    || cfg.phone    || '',
      password: process.env.WINNER_PASSWORD || cfg.password || '',
      headless: (process.env.BOT_HEADLESS   || String(cfg.headless ?? 'false')).toLowerCase() === 'true',
      databaseUrl: process.env.DATABASE_URL || process.env.POSTGRES_URL || process.env.WINNER_DATABASE_URL || cfg.databaseUrl || cfg.database_url || '',
    };
  } catch {
    return {
      phone:    process.env.WINNER_PHONE    || '',
      password: process.env.WINNER_PASSWORD || '',
      headless: false,
      databaseUrl: process.env.DATABASE_URL || process.env.POSTGRES_URL || process.env.WINNER_DATABASE_URL || '',
    };
  }
}

// ── Main collector class ──────────────────────────────────────────────────────

class AviatorCollector {
  constructor(credentials, signal) {
    this.creds   = credentials;
    this.signal  = signal;

    this.sm       = new StateMachine();
    this.browser  = getBrowserSupervisor(credentials.headless);
    this.loginMgr = new LoginManager(credentials);
    this.frameMgr = new FrameManager();
    this.collector= new Collector();
    this.health   = new HealthMonitor();
    this.browser.setHealthSource(this.health);
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
    this.roundStore = new PostgresRoundStore({ connectionString: credentials.databaseUrl || null });
    this.roundStore.sessionId = randomUUID();
    this.restoredCollectorState = null;

    // Round history state
    this._history     = [];
    this._prevSnapshot= null;
    this._prevSig     = null;
    this._lastSavedSig= null;
    this._totalAdded  = 0;

    // Current page reference (shared with watchdog)
    this._page = null;
    this._ownsBrowser = false;

    // Watchdog recovery signal — set when watchdog fires, cleared after recovery
    this._watchdogReason = null;
  }

  async _initRoundStore() {
    // Claim the PostgreSQL lease before touching the browser so a duplicate
    // collector start cannot create a second browser or compete for round IDs.
    await this.roundStore.init({ acquireCollectorLease: true });

    if (!this.roundStore.usingFileFallback) {
      const fileHistory = readRoundHistory();
      const persisted = await this.roundStore.loadRounds();
      const { missing, indexConflicts } = selectMissingLegacyRounds(fileHistory, persisted);
      if (indexConflicts) {
        log.warn(`Legacy history has ${indexConflicts} conflicting round IDs at persisted indices; PostgreSQL kept its authoritative rows`);
      }
      if (missing.length) {
        const { saved } = await this.roundStore.importRounds(missing);
        log.info(`Imported ${saved} missing round(s) into ${this.roundStore.backendName()}`);
      }
    }

    this._history = await this.roundStore.loadRounds();
    this.restoredCollectorState = await this.roundStore.loadState();
    if (!this.restoredCollectorState && this._history.length) {
      this.restoredCollectorState = this.roundStore.computeState(this._history);
      await this.roundStore.saveState(this.restoredCollectorState);
    }
    log.info(`Loaded ${this._history.length} round(s) from ${this.roundStore.backendName()}`);
    if (this.restoredCollectorState) {
      log.info(`Restored collector continuity: ${this.restoredCollectorState.contiguous_rounds}/${100}`);
    }
  }

  async _onWatchdogAlert(reason, page) {
    // Signal the main loop to recover on its next iteration
    this._watchdogReason = reason;
  }

  async _waitForNetwork() {
    this._watchdogReason = null;
    return waitForNetwork(this.signal, status => this.health.setNetwork(status));
  }

  async run() {
    this.health.start();
    this.health.setCollectorRunning(true);

    // Sync state machine transitions to health monitor
    this.sm.onTransition((state) => this.health.setState(state));

    log.info('=== Aviator Collector starting ===');

    // ── Step 0: Prepare PostgreSQL round store ────────────────────────────
    await this._initRoundStore();
    if (!await this._waitForNetwork()) return;

    // ── Step 1: Launch browser ────────────────────────────────────────────
    await this.browser.launch(this.signal);
    this._ownsBrowser = true;
    this._page = await this.browser.getHistoryPage();
    this.browser.startHeartbeat({ health: this.health });
    this.health.setBrowserConnected(true);

    // ── Step 2: Login ─────────────────────────────────────────────────────
    this.sm.transition(State.LOGIN, 'initial-start');
    await this.loginMgr.ensureLoggedIn(this._page, this.signal);
    this.health.setLoggedIn(true);
    this.health.recordLogin();
    await this.browser.persistSession(this._page);

    // ── Step 3: Navigate to Aviator ───────────────────────────────────────
    this.sm.transition(State.HOME, 'logged-in');
    this.sm.transition(State.GAME_LOADING, 'navigating');
    try {
      await this.loginMgr.goToAviator(this._page, this.signal);
    } catch (err) {
      log.warn(`Aviator did not open; recreating the history page in the existing context: ${formatError(err)}`);
      this._page = await this.recovery.recover(err, this._page, this.signal);
      await this.loginMgr.ensureLoggedIn(this._page, this.signal);
      await this.loginMgr.goToAviator(this._page, this.signal);
    }
    await this.browser.focusGamePage(this._page);
    this.browser._persistMetadata?.();

    // ── Step 4: Start watchdog ────────────────────────────────────────────
    this.watchdog.setPage(this._page);
    this.watchdog.start(this.signal);

    // ── Step 5: Infinite collection loop ─────────────────────────────────
    await this._collectionLoop();
  }

  async shutdown() {
    this.watchdog.stop();
    this.browser.stopHeartbeat();
    this.health.setCollectorRunning(false);
    this.health.stop();
    if (this._ownsBrowser) {
      await this.browser.close().catch(err => log.warn(`Browser close failed: ${formatError(err)}`));
      this._ownsBrowser = false;
    }
    // Release the singleton database lease only after the owned browser has
    // closed, so another collector cannot launch during our shutdown window.
    await this.roundStore.close().catch(err => log.warn(`Round store close failed: ${formatError(err)}`));
  }

  async _collectionLoop() {
    let attempt = 0;

    while (!this.signal?.aborted) {
      // Check if watchdog flagged an issue
      if (this._watchdogReason) {
        const reason = this._watchdogReason;
        this._watchdogReason = null;
        log.warn(`Collection loop: watchdog alert — ${reason}`);
        if (!await this._waitForNetwork()) break;
        try {
          this._page = await this.recovery.recover(reason, this._page, this.signal);
        } catch (err) {
          log.error(`Recovery failed: ${formatError(err)}`);
          if (err?.code === 'AUTH_REQUIRED' || err?.message?.includes('CIRCUIT_BREAKER_OPEN')) {
            if (err?.code === 'AUTH_REQUIRED') {
              this.health.setLoggedIn(false);
              if (this.sm.current !== State.RELOGIN) this.sm.transition(State.RELOGIN, 'AUTH_REQUIRED');
              if (this.sm.current === State.RELOGIN) this.sm.transition(State.STOPPED, 'AUTH_REQUIRED');
            }
            break;
          }
        }
        this.watchdog.setPage(this._page);
        attempt = 0;
        continue;
      }

      try {
        await this._collectOnce();
        attempt = 0; // reset backoff on success
      } catch (err) {
        if (this.signal?.aborted) break;

        if (err?.code === 'AUTH_REQUIRED') {
          this.health.setLoggedIn(false);
          log.error('AUTH_REQUIRED: collector paused for human login or platform verification');
          if (this.sm.current !== State.RELOGIN) this.sm.transition(State.RELOGIN, 'AUTH_REQUIRED');
          this.sm.transition(State.STOPPED, 'AUTH_REQUIRED');
          break;
        }

        if (err?.message?.includes('CIRCUIT_BREAKER_OPEN')) {
          log.error('CIRCUIT_BREAKER_OPEN: collector stopped automatic recovery for investigation');
          if (this.sm.current === State.COLLECTING) this.sm.transition(State.STOPPED, 'CIRCUIT_BREAKER_OPEN');
          break;
        }

        if (classifyError(err) === 'PERSISTENCE_ERROR') {
          log.error(`Persistence failed; preserving the observation and browser session: ${formatError(err)}`);
          await sleep(backoffMs(attempt++), this.signal);
          continue;
        }

        log.error(`Collection error: ${formatError(err)}`);
        if (classifyError(err) === 'FRAME_RECOVER') this.health.setFrameConnected(false);
        if (!await this._waitForNetwork()) break;

        const delay = backoffMs(attempt++);
        log.warn(`Recovering in ${delay}ms (attempt ${attempt})`);

        this._page = await this.recovery.recover(err, this._page, this.signal).catch(recErr => {
          log.error(`Recovery threw: ${formatError(recErr)}`);
          return this._page;
        });
        this.watchdog.setPage(this._page);

        await sleep(delay, this.signal);
      }
    }

    this.watchdog.stop();
    this.health.setCollectorRunning(false);
    this.health.stop();
    await this.roundStore.close().catch(err => log.warn(`PostgreSQL close failed: ${formatError(err)}`));
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
    const frame = await this.frameMgr.waitForFrame(this._page, {
      signal: this.signal,
      timeoutMs: Number(process.env.BOT_INITIAL_FRAME_TIMEOUT || process.env.BOT_FRAME_TIMEOUT || 30000),
    });
    this.health.setFrameConnected(true);

    await this.frameMgr.waitForPayouts(frame, this.signal);

    const { multipliers: initMults, signature: initSig } = await this.collector.install(frame);
    this.health.recordObserverInstalled();

    // Establish or update baseline snapshot
    if (!this._prevSnapshot) {
      this._prevSnapshot = initMults;
      this._prevSig      = initSig;
      log.info(`Baseline: ${initMults.length} rounds`);
    } else if (initSig !== this._prevSig) {
      // Reconnected after reload — catch up missed rounds
      const inferred = inferNewMultipliers(this._prevSnapshot, initMults);
      if (inferred.length > 0) {
        const { history, added } = appendRounds(this._history, inferred, new Date().toISOString());
        if (added.length > 0) {
          const result = await this.roundStore.saveRounds(added, 'collector_catchup');
          this._history = result.rounds || history;
          this._totalAdded += result.saved;
          for (const round of added.slice(-result.saved)) this.health.recordRound(round);
          if (result.saved > 0) await this.recovery.verifyRound(added.at(-1));
          log.info(`Catch-up: saved ${result.saved} missed round(s)${result.duplicates ? `, skipped ${result.duplicates} duplicate observation(s)` : ''}`);
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

        const { history, added } = appendRounds(this._history, inferred, event.timestamp || new Date().toISOString());
        if (added.length > 0) {
          const result = await this.roundStore.saveRounds(added, 'collector');
          this._history = result.rounds || history;
          this._lastSavedSig = currSig;
          this._totalAdded += result.saved;
          for (const round of added.slice(-result.saved)) this.health.recordRound(round);
          if (result.saved > 0) await this.recovery.verifyRound(added.at(-1));
          log.info(`History updated: ${this._history.length} rounds persisted${result.duplicates ? `, skipped ${result.duplicates} duplicate observation(s)` : ''}`);
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

  // Infinite outer loop — never exits unless signal is aborted
  while (!signal?.aborted) {
    // A failed run may leave its state machine in any state. Each outer retry
    // therefore gets a clean lifecycle instead of attempting invalid backward
    // transitions such as GAME_LOADING → LOGIN.
    const collector = new AviatorCollector(creds, signal);
    try {
      await collector.run();
    } catch (err) {
      if (signal?.aborted) break;
      if (err?.code === 'AUTH_REQUIRED') {
        log.error('AUTH_REQUIRED: automatic collector restart stopped; human login or platform verification is required');
        break;
      }
      if (err?.code === 'COLLECTOR_ALREADY_ACTIVE') {
        log.error(`Collector start refused: ${err.message}`);
        break;
      }
      if (err?.message?.includes('CIRCUIT_BREAKER_OPEN')) {
        log.error('CIRCUIT_BREAKER_OPEN: automatic collector restart stopped for investigation');
        break;
      }
      log.error(`Outer loop error: ${formatError(err)} — restarting in 10s`);
      await sleep(10000, signal);
    } finally {
      await collector.shutdown();
    }
  }

  log.info('Collector shut down cleanly.');
}
