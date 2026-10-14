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
import { AutoCashoutInitializer } from './AutoCashoutInitializer.js';
import { BetAmountSynchronizer } from './BetAmountSynchronizer.js';
import { Collector }       from './Collector.js';
import { HealthMonitor }   from './HealthMonitor.js';
import { RecoveryManager } from './RecoveryManager.js';
import { Watchdog }        from './Watchdog.js';
import { PostgresRoundStore } from './PostgresRoundStore.js';
import { publishDirectRoundObservations, publishCommittedRoundEvents, publishCollectorGamePhase } from './CollectorRoundEventClient.js';
import { DurableRoundOutbox } from './DurableRoundOutbox.js';
import { RoundDeliveryPipeline } from './RoundDeliveryPipeline.js';
import { sleep, backoffMs, classifyError, recoveryRetryDelayMs } from './RetryManager.js';
import { waitForNetwork, probeBackendHealth, probeNetwork } from './NetworkMonitor.js';
import {
  readRoundHistory,
  inferNewMultipliers, appendRounds,
  snapshotSignature,
} from './HistoryManager.js';

const __dirname  = path.dirname(fileURLToPath(import.meta.url));
const CONFIG_PATH = path.join(__dirname, '..', '..', 'data', 'bot', 'config.json');

function conciseErrorMessage(error, maxLength = 200) {
  const message = String(error?.message || error || 'Unknown error').split(/\r?\n/, 1)[0].trim();
  return message.length > maxLength ? `${message.slice(0, maxLength - 1)}…` : message;
}

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

export function canContinueReadOnlyCollection(sessionStatus) {
  return sessionStatus === 'AUTHENTICATED' || sessionStatus === 'UNKNOWN';
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
  } catch (error) {
    if (error?.code !== 'ENOENT') log.warn(`Collector config could not be read; using environment and manual-login fallback: ${error.message}`);
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
    this.frameMgr = new FrameManager({ pageManager: this.browser });
    this.collector= new Collector();
    this.health   = new HealthMonitor();
    this.roundStore = new PostgresRoundStore({ connectionString: credentials.databaseUrl || null });
    this.roundStore.sessionId = randomUUID();
    this.directEventOutbox = new DurableRoundOutbox({
      filePath: path.join(__dirname, '..', '..', 'data', 'bot', 'round-event-outbox.json'),
      eventOutbox: true,
    });
    this._roundDeliveryFailureLoggedAt = new Map();
    this.browser.setHealthSource(this.health);
    this.cashoutInitializer = new AutoCashoutInitializer({
      frameManager: this.frameMgr,
      health: this.health,
      coordinator: this.browser,
    });
    this.betAmountSynchronizer = new BetAmountSynchronizer({
      frameManager: this.frameMgr,
      health: this.health,
      coordinator: this.browser,
    });
    this._sharedPageSetupPromise = null;
    this.recovery = new RecoveryManager({
      stateMachine:  this.sm,
      browserManager:this.browser,
      loginManager:  this.loginMgr,
      frameManager:  this.frameMgr,
      health:        this.health,
      executionRecoveryCheck: () => this.roundStore.executionRecoveryStatus(),
    });
    this.watchdog = new Watchdog({
      stateMachine:  this.sm,
      browserManager:this.browser,
      loginManager:  this.loginMgr,
      health:        this.health,
      onUnhealthy:   (reason, page) => this._onWatchdogAlert(reason, page),
    });
    this.restoredCollectorState = null;
    this._storageTimer = null;
    this._storageRetryBusy = false;
    this._networkTimer = null;
    this._networkProbeBusy = false;

    // Round history state
    this._history     = [];
    this._prevSnapshot= null;
    this._prevSig     = null;
    this._lastSavedSig= null;
    this._totalAdded  = 0;
    this._unverifiedGapPending = false;
    this.roundDelivery = new RoundDeliveryPipeline({
      outbox: this.directEventOutbox,
      roundStore: this.roundStore,
      publishObserved: publishDirectRoundObservations,
      publishPersisted: publishCommittedRoundEvents,
      onCommitted: async (entries, result) => {
        const inserted = entries.filter(entry => entry.inserted);
        if (inserted.length) {
          const committed = inserted.map(entry => entry.round);
          this.health.recordRounds(committed, result.commitAcknowledgedAt, inserted);
          const byId = new Map(this._history.map(round => [String(round.round_id), round]));
          for (const round of committed) byId.set(String(round.round_id), { ...byId.get(String(round.round_id)), ...round });
          this._history = [...byId.values()].sort((a, b) => Number(a.round_index) - Number(b.round_index));
          this._totalAdded += inserted.length;
          await this.recovery.verifyRound(committed.at(-1));
          for (const round of committed) log.info('Round committed to PostgreSQL', {
            round_id: round.round_id, round_index: round.round_index,
            multiplier: round.multiplier, stored_at: round.stored_at,
          });
        }
        this.health.setStorage({ ...this.roundStore.storageSnapshot(), round_delivery: this.roundDelivery.snapshot() });
      },
      onFailure: failure => {
        this.health.setStorage({ ...this.roundStore.storageSnapshot(), round_delivery: this.roundDelivery.snapshot() });
        const key = `${failure.stage}:${failure.category}`;
        const now = Date.now();
        if (now - (this._roundDeliveryFailureLoggedAt.get(key) || 0) < 30_000) return;
        this._roundDeliveryFailureLoggedAt.set(key, now);
        log.warn('Round delivery is queued for retry; collection continues', {
          component: 'collector', stage: failure.stage, category: failure.category,
          backlog_size: failure.backlog_size ?? this.directEventOutbox.stats().backlog_size,
          next_attempt_at: failure.next_attempt_at || null,
          message: conciseErrorMessage(failure.message || 'delivery retry scheduled'),
        });
      },
    });

    // Current page reference (shared with watchdog)
    this._page = null;
    this._ownsBrowser = false;

    // Watchdog recovery signal — set when watchdog fires, cleared after recovery
    this._watchdogReason = null;
  }

  _stageDirectRoundObservations(rounds, source) {
    try {
      const deliveryMode = source === 'live_history' ? 'LIVE' : 'RECOVERED';
      const capacity = this.roundDelivery.enqueue(rounds, { deliveryMode });
      log.info('Direct Winner-page observations durably queued for backend staging', {
        component: 'collector', state: this.sm.current, source, count: rounds.length,
        backlog_size: capacity.backlog_size,
      });
      this.health.recordObservedRound(rounds.at(-1));
      this.health.setStorage({ ...this.roundStore.storageSnapshot(), round_delivery: this.roundDelivery.snapshot() });
      return true;
    } catch (error) {
      log.error('Direct Winner-page observations could not be written to the durable event outbox', {
        component: 'collector', state: this.sm.current,
        error_category: error?.category || error?.code || 'EVENT_OUTBOX_WRITE_FAILURE', error: conciseErrorMessage(error),
        recovery_action: 'hold_snapshot_until_outbox_worker_compaction_or_delivery_frees_capacity',
        source, count: rounds.length,
      });
      throw Object.assign(error, { code: error.code || 'EVENT_OUTBOX_WRITE_FAILURE' });
    }
  }

  async _publishGamePhase(phase) {
    if (!phase) return;
    const snapshot = this.health.snapshot();
    const sourceIdentity = {
      round_id: snapshot.lastRoundId,
      round_index: snapshot.lastRoundIndex,
    };
    const event = { ...phase };
    if (!event.event_id && sourceIdentity.round_id && sourceIdentity.round_index != null) {
      event.event_id = [event.phase, event.observed_at, sourceIdentity.round_id].join(':');
    }
    this.health.recordGamePhase(event, sourceIdentity);
    if (!sourceIdentity.round_id || !Number.isSafeInteger(Number(sourceIdentity.round_index))) return;
    try {
      await publishCollectorGamePhase(event, sourceIdentity);
    } catch (error) {
      log.warn('Read-only game-phase evidence was not acknowledged by the backend', {
        component: 'collector', phase: event.phase,
        error_category: error?.category || 'GAME_PHASE_EVENT_FAILURE',
        ...(error?.category === 'BACKEND_EVENT_TIMEOUT'
          ? { timeout_ms: error.timeoutMs || Number(process.env.COLLECTOR_BACKEND_EVENT_TIMEOUT_MS) || 3000 }
          : { error: conciseErrorMessage(error) }),
        source_round_id: sourceIdentity.round_id,
      });
    }
  }

  async _initRoundStore() {
    // Claim the PostgreSQL lease before touching the browser so a duplicate
    // collector start cannot create a second browser or compete for round IDs.
    await this.roundStore.init({ acquireCollectorLease: true });

    if (this.roundStore.pool && !this.roundStore.usingFileFallback) {
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
    const queued = this.roundStore.outbox.readAll();
    if (queued.length) {
      const ids = new Set(this._history.map(round => String(round.round_id)));
      this._history = [...this._history, ...queued.filter(round => !ids.has(String(round.round_id)))]
        .sort((a, b) => Number(a.round_index) - Number(b.round_index));
    }
    const eventQueued = this.directEventOutbox.readAll();
    if (eventQueued.length) {
      const ids = new Set(this._history.map(round => String(round.round_id)));
      this._history = [...this._history, ...eventQueued.filter(round => !ids.has(String(round.round_id)))]
        .sort((a, b) => Number(a.round_index) - Number(b.round_index));
    }
    this.health.setStorage(this.roundStore.storageSnapshot());
    this.restoredCollectorState = await this.roundStore.loadState();
    const latestRound = this._history.at(-1) || null;
    this.health.setPersistedRoundIdentity(latestRound);
    const restoredStateMatchesHistory = Boolean(this.restoredCollectorState
      && Number(this.restoredCollectorState.total_history) === this._history.length
      && String(this.restoredCollectorState.last_processed_round_id || '') === String(latestRound?.round_id || ''));
    if (this._history.length && !restoredStateMatchesHistory) {
      if (this.restoredCollectorState) {
        log.warn('Persisted collector continuity state does not match PostgreSQL tail; rebuilding from authoritative rounds');
      }
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
    const category = reason.startsWith('AUTH_REQUIRED:') ? 'AUTH_REQUIRED'
      : reason.startsWith('PAGE_CRASHED:') ? 'PAGE_CRASH'
        : reason.startsWith('NETWORK:') ? 'NETWORK_UNAVAILABLE' : 'PAGE_RECOVER';
    this.health.setError(reason, category, 'recovery_pending');
    if (category === 'AUTH_REQUIRED') {
      this.health.setAuthRequired(reason);
      this.sm.transition(State.AUTH_REQUIRED, 'verified-session-expiry');
    } else if (category === 'NETWORK_UNAVAILABLE') {
      const nextState = /DNS_UNAVAILABLE|OFFLINE/.test(reason) ? State.OFFLINE : State.SITE_UNAVAILABLE;
      this.sm.transition(nextState, 'network-probe-failed');
    } else {
      this.sm.transition(State.RECOVERING, category);
    }
  }

  async _ensureSharedAviatorPageSetup() {
    if (this._sharedPageSetupPromise) return this._sharedPageSetupPromise;
    this._sharedPageSetupPromise = this._configureSharedAviatorPage()
      .finally(() => { this._sharedPageSetupPromise = null; });
    return this._sharedPageSetupPromise;
  }

  async _configureSharedAviatorPage() {
    if (!this._page || this._page.isClosed?.()) return false;
    const health = this.health.snapshot?.() || {};
    if (health.authenticationState === 'AUTH_REQUIRED' || health.authRequired) return false;
    const aviatorPage = await this.browser.getAviatorPage();
    if (!aviatorPage || aviatorPage.isClosed?.() || aviatorPage !== this._page) {
      throw new Error('SHARED_AVIATOR_PAGE_IDENTITY_MISMATCH');
    }
    const pages = this.browser._context?.pages?.();
    if (Array.isArray(pages) && (!pages.includes(this._page) || !pages.includes(aviatorPage))) {
      throw new Error('SHARED_AVIATOR_PAGE_DETACHED_FROM_BROWSER_CONTEXT');
    }
    // UI setup is best effort and never blocks history collection. It shares
    // the collector's Aviator page and never submits a betting transaction.
    void this.cashoutInitializer.initialize(aviatorPage, {
      signal: this.signal,
    }).then(result => {
      if (result?.status === 'READY') {
        log.info('Both Aviator panels independently verified with Auto Cash Out 2.00x and Auto Bet OFF');
        return this.betAmountSynchronizer.initialize(aviatorPage, { signal: this.signal });
      } else {
        log.warn('Aviator cashout initialization is not READY; history collection continues', {
          state: result?.status || 'UNKNOWN',
          panel_1: result?.panel_1_cashout || 'UNKNOWN',
          panel_2: result?.panel_2_cashout || 'UNKNOWN',
          reason: result?.error_reason || null,
        });
      }
      return null;
    }).then(amountResult => {
      if (amountResult?.status === 'READY') {
        log.info('Configured Rules Engine stake read back on the selected execution panel');
      } else if (amountResult) {
        log.warn('Stake synchronization is blocked; no bet was submitted', {
          status: amountResult.status || 'UNKNOWN', reason: amountResult.error_reason || null,
          panel: amountResult.execution_panel == null ? null : amountResult.execution_panel + 1,
          requested_stake: amountResult.requested_stake,
          requested_currency: amountResult.requested_stake_currency,
          platform_currency: amountResult.platform_currency || amountResult.currency,
        });
      }
    }).catch(err => {
      this.health.setCashoutInitialization?.({
        target: '2.00', auto_bet: 'UNKNOWN', status: 'UNKNOWN',
        panel_1_cashout: 'UNKNOWN', panel_2_cashout: 'UNKNOWN', panels: [],
        updated_at: new Date().toISOString(),
        error_reason: `INITIALIZATION_SCHEDULING_FAILED:${conciseErrorMessage(err, 120)}`,
      });
      log.warn(`Aviator cashout initialization could not start; history collection continues: ${formatError(err)}`);
    });
    await this.browser.focusGamePage?.(aviatorPage);
    return true;
  }

  async _waitForNetwork() {
    this._watchdogReason = null;
    this.health.setRecovery?.('NETWORK_WAIT', { reasonCode: 'CONNECTIVITY_CHECK' });
    const online = await waitForNetwork(this.signal, status => {
      this.health.setNetwork(status);
      this.health.setRecovery?.(status.status === 'ONLINE' ? 'CHECKING_SESSION' : 'NETWORK_WAIT', {
        attempt: status.attempt || 0,
        nextRetryAt: status.next_retry_at || null,
        reasonCode: status.reason_code || status.status,
        evidence: status.evidence || null,
      });
      if (status.status === 'OFFLINE' || status.status === 'DNS_UNAVAILABLE') {
        if (!this.sm.is(State.OFFLINE)) this.sm.transition(State.OFFLINE, status.reason_code || status.status);
      } else if (['MAIN_SITE_UNAVAILABLE', 'WINNER_UNREACHABLE', 'GAME_PROVIDER_UNAVAILABLE'].includes(status.status)) {
        if (!this.sm.is(State.SITE_UNAVAILABLE)) this.sm.transition(State.SITE_UNAVAILABLE, status.reason_code || status.status);
      } else if (status.status === 'ONLINE' && [State.OFFLINE, State.SITE_UNAVAILABLE].includes(this.sm.current)) {
        this.sm.transition(State.CHECKING_SESSION, 'network-restored');
      }
    });
    if (online && [State.OFFLINE, State.SITE_UNAVAILABLE].includes(this.sm.current)) {
      this.sm.transition(State.CHECKING_SESSION, 'main-site-connectivity-restored');
    }
    return online;
  }

  _startNetworkMonitor() {
    if (this._networkTimer) return;
    const poll = async () => {
      if (this._networkProbeBusy || this.signal?.aborted) return;
      this._networkProbeBusy = true;
      try {
        const status = await probeNetwork();
        const previous = this.health.snapshot().network?.status || 'UNKNOWN';
        this.health.setNetwork(status);
        if (status.status !== previous) {
          this.health.setRecovery?.('NETWORK_PROBE', { reasonCode: status.reason_code || status.status, evidence: status.evidence || null });
          if (status.status !== 'ONLINE') {
            this.health.setError(status.reason || status.status, status.reason_code || status.status, 'wait_for_network');
            const nextState = ['OFFLINE', 'DNS_UNAVAILABLE'].includes(status.status) ? State.OFFLINE : State.SITE_UNAVAILABLE;
            if (this.sm.current !== nextState) this.sm.transition(nextState, status.reason_code || status.status);
            if (!this._watchdogReason) this._watchdogReason = `NETWORK:${status.status}:${status.reason || 'connectivity changed'}`;
          } else if (previous !== 'ONLINE') {
            if ([State.OFFLINE, State.SITE_UNAVAILABLE].includes(this.sm.current)) {
              this.sm.transition(State.CHECKING_SESSION, 'network-restored');
            }
            this.health.setRecovery?.('CHECKING_SESSION', { reasonCode: 'NETWORK_RESTORED', evidence: status.evidence || null });
            if (!this._watchdogReason) this._watchdogReason = 'NETWORK:ONLINE:network restored; verify session';
          }
        }
      } catch (error) {
        const evidence = { status: 'UNKNOWN', reason: formatError(error), checked_at: new Date().toISOString() };
        this.health.setNetwork(evidence);
        this.health.setError(evidence.reason, 'NETWORK_PROBE_UNKNOWN', 'retry_network_probe');
      } finally {
        this._networkProbeBusy = false;
      }
    };
    void poll();
    this._networkTimer = setInterval(poll, Number(process.env.BOT_NETWORK_POLL_MS || 15000));
    this._networkTimer.unref?.();
  }

  async run() {
    this.health.start();
    this.health.setCollectorRunning(true);
    this._storageTimer = setInterval(async () => {
      if (this._storageRetryBusy) return;
      this._storageRetryBusy = true;
      try {
        this.health.setBackend(await probeBackendHealth());
        const before = this.roundStore.outbox.readAll();
        const storage = await this.roundStore.retryAndDrain();
        this.health.setStorage(storage);
        if (storage.status !== 'UNAVAILABLE' && this.sm.is(State.STORAGE_UNAVAILABLE)) {
          this.sm.transition(State.COLLECTING, 'storage-restored');
        }
        if (before.length && storage.backlog_size === 0) {
          const last = before.at(-1);
          const committedRounds = storage.last_committed_rounds?.length
            ? storage.last_committed_rounds : [last];
          if (storage.last_committed_rounds?.length) {
            this.health.recordRounds(committedRounds, storage.last_commit_acknowledged_at || null);
          } else {
            this.health.recordRound(last);
          }
          this._history = [...this._history.filter(row => String(row.round_id) !== String(last.round_id)), last]
            .sort((a, b) => Number(a.round_index) - Number(b.round_index));
          this._totalAdded += before.length;
          await this.recovery.verifyRound(last);
        }
      } catch (error) {
        this.health.setError(formatError(error), 'STORAGE_ERROR', 'retry_outbox');
        this.health.setStorage(this.roundStore.storageSnapshot());
        log.error(`Storage retry cycle failed; collector will continue monitoring: ${formatError(error)}`);
      } finally {
        this._storageRetryBusy = false;
      }
    }, Number(process.env.COLLECTOR_STORAGE_RETRY_MS || 5000));
    this._storageTimer.unref?.();

    // Sync state machine transitions to health monitor
    this.sm.onTransition((state) => this.health.setState(state));

    log.info('=== Aviator Collector starting ===');

    // ── Step 0: Prepare PostgreSQL round store ────────────────────────────
    await this._initRoundStore();
    void this.roundDelivery.start({ recovered: true });
    if (!await this._waitForNetwork()) return;
    this._startNetworkMonitor();
    if (this.sm.is(State.SITE_UNAVAILABLE)) this.sm.transition(State.CHECKING_SESSION, 'main-site-reachable');

    // ── Step 1: Launch browser ────────────────────────────────────────────
    if (!this.browser.isAlive() || !this.browser.hasContext()) {
      const executionState = await this.roundStore.executionRecoveryStatus();
      if (!executionState.safe) {
        this.health.setRecovery?.('EXECUTION_RECONCILIATION_REQUIRED', {
          reasonCode: 'BROWSER_LAUNCH_BLOCKED_BY_EXECUTION_STATE', evidence: executionState,
        });
        throw Object.assign(new Error('Browser launch is paused until unresolved execution state is reconciled'), {
          code: 'EXECUTION_RECONCILIATION_REQUIRED', evidence: executionState,
        });
      }
    }
    await this.browser.launch(this.signal);
    this._ownsBrowser = Boolean(this.browser._ownsBrowser);
    this._page = await this.browser.getAviatorPage();
    this.browser.startHeartbeat({ health: this.health });
    this.health.setBrowserConnected(true);

    // ── Step 2: Login ─────────────────────────────────────────────────────
    this.sm.transition(State.LOGIN, 'initial-start');
    try {
      await this.loginMgr.ensureLoggedIn(this._page, this.signal);
    } catch (err) {
      if (classifyError(err) === 'NETWORK_RECOVER') {
        this.health.setError(formatError(err), 'NETWORK_UNAVAILABLE', 'wait_for_connectivity');
        if (!await this._waitForNetwork()) return;
        await this.loginMgr.ensureLoggedIn(this._page, this.signal);
      } else {
        if (err?.code !== 'AUTH_REQUIRED' && err?.name !== 'AuthRequiredError') throw err;
        this.health.setLoggedIn(false);
        this.health.setAuthRequired(err.message || 'Human login or verification is required');
        log.error('AUTH_REQUIRED: keeping the single browser open for human login; collector will not report healthy or collect until verified');
        // Preserve the live browser for operator login instead of closing and
        // relaunching it in a restart loop. Resume only after real page evidence
        // makes LoginManager confirm the authenticated session.
        while (!this.signal?.aborted) {
          await sleep(1500, this.signal);
          if (!await this.loginMgr.isLoggedIn(this._page)) continue;
          this.health.setAuthRequired(null);
          await this.loginMgr.ensureLoggedIn(this._page, this.signal);
          break;
        }
        if (this.signal?.aborted) return;
      }
    }
    this.health.setLoggedIn(true);
    this.health.setSiteStatus('AVAILABLE');
    this.health.recordLogin();
    await this.browser.persistSession(this._page);

    // ── Step 3: Navigate to Aviator ───────────────────────────────────────
    this.sm.transition(State.HOME, 'logged-in');
    this.sm.transition(State.GAME_LOADING, 'navigating');
    try {
      await this.loginMgr.goToAviator(this._page, this.signal);
    } catch (err) {
      log.warn(`Aviator did not open; starting one bounded in-place recovery cycle: ${formatError(err)}`);
      let recoveryError = err;
      if (['NETWORK_RECOVER', 'SITE_UNAVAILABLE', 'GAME_PROVIDER_UNAVAILABLE'].includes(classifyError(err))) {
        this.health.setError(formatError(err), classifyError(err), 'wait_for_connectivity');
        if (!await this._waitForNetwork()) return;
        recoveryError = Object.assign(new Error('Connectivity restored; verify session and reacquire Aviator'), {
          code: 'GAME_FRAME_MISSING', reasonCode: 'CONNECTIVITY_RESTORED',
        });
      }
      this._page = await this.recovery.recover(recoveryError, this._page, this.signal);
    }
    await this.browser.focusGamePage(this._page);
    this.browser._persistMetadata?.();
    await this._page?.bringToFront?.().catch(error => log.debug(`History page could not be focused: ${error.message}`));

    // ── Step 4: Start watchdog ────────────────────────────────────────────
    this.watchdog.setPage(this._page);
    this.watchdog.start(this.signal);

    // ── Step 5: Infinite collection loop ─────────────────────────────────
    await this._collectionLoop();
  }

  async shutdown({ deliberate = Boolean(this.signal?.aborted) } = {}) {
    if (this._storageTimer) { clearInterval(this._storageTimer); this._storageTimer = null; }
    if (this._networkTimer) { clearInterval(this._networkTimer); this._networkTimer = null; }
    await this.roundDelivery.stop();
    this.watchdog.stop();
    if (deliberate) {
      this.browser.stopHeartbeat();
      this.health.setState('STOPPING');
      this.health.setCollectorRunning(false);
      this.health.stop();
    } else {
      this.health.setState('RECOVERING');
      this.health.setCollectorRunning(true);
      this.health.start();
    }
    if (deliberate && this._ownsBrowser) {
      const executionState = await this.roundStore.executionRecoveryStatus();
      if (executionState.safe) {
        await this.browser.close().catch(err => log.warn(`Browser close failed: ${formatError(err)}`));
        this._ownsBrowser = false;
      } else {
        log.warn('Explicit stop is preserving the shared browser context because execution state is unresolved or unverifiable');
        await this.browser.preserveOpenContext?.().catch(err => log.warn(`Browser context preservation failed: ${formatError(err)}`));
        this._ownsBrowser = false;
      }
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
          const recoveryReason = reason.startsWith('NETWORK:')
            ? new Error('network connectivity is restored; verify session and reacquire Aviator')
            : reason.startsWith('AUTH_REQUIRED:') ? Object.assign(new Error(reason), { code: 'AUTH_REQUIRED' })
              : reason.startsWith('PAGE_CRASHED:') ? Object.assign(new Error(reason), { code: 'PAGE_CRASHED' }) : reason;
          this._page = await this.recovery.recover(recoveryReason, this._page, this.signal);
          void this._ensureSharedAviatorPageSetup().catch(err => {
            log.warn(`Betting page recovery is pending; History collection remains read-only: ${formatError(err)}`);
          });
        } catch (err) {
          log.error(`Recovery failed: ${formatError(err)}`);
          if (err?.code === 'AUTH_REQUIRED') {
            this.health.setLoggedIn(false);
            this.health.setAuthRequired(err.message || 'Human login or platform verification is required');
            if (!await this._waitForHumanLogin(err.message)) break;
            attempt = 0;
            continue;
          }
          if (err?.message?.includes('CIRCUIT_BREAKER_OPEN')) {
            const cooldown = Number(process.env.BOT_RECOVERY_CYCLE_COOLDOWN_MS || 60000);
            this.health.setError(formatError(err), 'BROWSER_RECOVERY', 'cooldown_then_retry');
            await sleep(cooldown, this.signal);
            this.browser.allowRecoveryCycle?.();
            attempt = 0;
            continue;
          }
          await sleep(backoffMs(attempt++), this.signal);
          continue;
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
          this.health.setAuthRequired(err.message || 'Human login or verification is required');
          this.health.setError(formatError(err), 'AUTH_REQUIRED', 'wait_for_manual_login');
          log.error('AUTH_REQUIRED: collector is waiting for human login or platform verification');
          if (!await this._waitForHumanLogin(err.message)) break;
          attempt = 0;
          continue;
        }

        if (err?.message?.includes('CIRCUIT_BREAKER_OPEN')) {
          const cooldown = Number(process.env.BOT_RECOVERY_CYCLE_COOLDOWN_MS || 60000);
          log.error(`CIRCUIT_BREAKER_OPEN: waiting ${cooldown}ms before a fresh bounded recovery cycle`);
          this.health.setError(formatError(err), 'BROWSER_RECOVERY', 'cooldown_then_retry');
          await sleep(cooldown, this.signal);
          this.browser.allowRecoveryCycle?.();
          attempt = 0;
          continue;
        }

        if (classifyError(err) === 'PERSISTENCE_ERROR') {
          log.error(`Persistence failed; preserving the observation and browser session: ${formatError(err)}`);
          this.health.setStorage(this.roundStore.storageSnapshot());
          this.health.setError(formatError(err), 'STORAGE_ERROR', 'retry_outbox');
          if (!this.sm.is(State.STORAGE_UNAVAILABLE)) this.sm.transition(State.STORAGE_UNAVAILABLE, 'postgresql-write-failed');
          await sleep(backoffMs(attempt++), this.signal);
          continue;
        }

        if (['SITE_UNAVAILABLE', 'GAME_PROVIDER_UNAVAILABLE'].includes(classifyError(err))) {
          this.health.setSiteStatus('UNAVAILABLE');
          this.health.setError(formatError(err), classifyError(err), 'retry_after_site_cooldown');
          this.health.setRecovery?.('SITE_UNAVAILABLE', { attempt: attempt + 1, reasonCode: err?.reasonCode || classifyError(err), evidence: err?.evidence || null });
          if (!this.sm.is(State.SITE_UNAVAILABLE)) this.sm.transition(State.SITE_UNAVAILABLE, err?.reasonCode || classifyError(err));
          await sleep(backoffMs(attempt++), this.signal);
          continue;
        }

        log.error(`Collection error: ${formatError(err)}`);
        if (['FRAME_RECOVER', 'PAGE_RECOVER', 'PAGE_CRASH', 'UNKNOWN'].includes(classifyError(err))) {
          this.health.setGameReadiness?.({ frameAttached: false, contentLoaded: false, payoutsReady: false, reason: err?.reasonCode || classifyError(err) });
          this.health.setFrameConnected(false);
        }
        if (!await this._waitForNetwork()) break;

        const recoveryAttempt = attempt++;
        log.warn(`Recovering after collection failure (attempt ${attempt})`);

        try {
          const recoveryError = classifyError(err) === 'NETWORK_RECOVER'
            ? new Error('network connectivity is restored; verify session and reacquire Aviator') : err;
          this._page = await this.recovery.recover(recoveryError, this._page, this.signal);
          void this._ensureSharedAviatorPageSetup().catch(pageErr => {
            log.warn(`Betting page recovery is pending; History collection remains read-only: ${formatError(pageErr)}`);
          });
        } catch (recErr) {
          log.error(`Recovery threw: ${formatError(recErr)}`);
        if (recErr?.code === 'AUTH_REQUIRED') {
            this.health.setAuthRequired(recErr.message || 'Human login or platform verification is required');
            this.health.setLoggedIn(false);
            if (this.sm.current !== State.AUTH_REQUIRED) this.sm.transition(State.AUTH_REQUIRED, 'AUTH_REQUIRED');
            if (!await this._waitForHumanLogin(recErr.message)) break;
            attempt = 0;
            continue;
          }
          if (recErr?.message?.includes('CIRCUIT_BREAKER_OPEN')) {
            const cooldown = Number(process.env.BOT_RECOVERY_CYCLE_COOLDOWN_MS || 60000);
            this.health.setError(formatError(recErr), 'BROWSER_RECOVERY', 'cooldown_then_retry');
            await sleep(cooldown, this.signal);
            this.browser.allowRecoveryCycle?.();
            attempt = 0;
            continue;
          }
          await sleep(backoffMs(attempt++), this.signal);
          continue;
        }
        this.watchdog.setPage(this._page);

        const delay = recoveryRetryDelayMs(err, recoveryAttempt, {
          recoveryPending: this.recovery._pendingVerification,
        });
        log.warn(`Retrying collection in ${delay}ms (attempt ${attempt})`);

        await sleep(delay, this.signal);
      }
    }

    this.watchdog.stop();
    this.health.setCollectorRunning(false);
    this.health.stop();
    await this.roundStore.close().catch(err => log.warn(`PostgreSQL close failed: ${formatError(err)}`));
    log.info(`=== Collector stopped. Total rounds saved: ${this._totalAdded} ===`);
  }

  async _waitForHumanLogin(reason) {
    this.health.setAuthRequired(reason || 'Human login or verification is required');
    this.health.setLoggedIn(false);
    this.browser.authRequired?.(reason || 'Human login or verification is required');
    if (this.sm.current !== State.AUTH_REQUIRED) this.sm.transition(State.AUTH_REQUIRED, 'AUTH_REQUIRED');
    let loginAttempt = 0;
    let nextLoginAttemptAt = 0;
    let routeAttempt = 0;
    let nextRouteAttemptAt = 0;
    let manualLoginPrompted = false;
    while (!this.signal?.aborted) {
      if (this._page && !this._page.isClosed?.()) {
        const session = typeof this.loginMgr.inspectSession === 'function'
          ? await this.loginMgr.inspectSession(this._page)
          : { status: await this.loginMgr.isLoggedIn(this._page) ? 'AUTHENTICATED'
            : await this.loginMgr.isExplicitlyLoggedOut?.(this._page) ? 'EXPIRED' : 'UNKNOWN' };
        if (session.status === 'AUTHENTICATED') {
          this.health.setLoggedIn(true);
          this.health.setAuthRequired(null);
          if (Date.now() < nextRouteAttemptAt) {
            await sleep(Math.min(1000, nextRouteAttemptAt - Date.now()), this.signal);
            continue;
          }
          try {
            this.health.setRecovery?.('NAVIGATING_AVIATOR', { attempt: routeAttempt + 1, reasonCode: 'AUTHENTICATED_WAITING_FOR_GAME' });
            if (!this.sm.is(State.NAVIGATING_AVIATOR)) this.sm.transition(State.NAVIGATING_AVIATOR, 'authenticated-session-resume');
            await this.loginMgr.goToAviator(this._page, this.signal);
            await this.browser.focusGamePage?.(this._page);
            this.browser._persistMetadata?.();
            this.health.recordLogin();
            this.health.setGameReadiness?.({ frameAttached: true, contentLoaded: false, payoutsReady: false, reason: 'WAITING_FOR_HISTORY' });
            this.sm.transition(State.WAITING_IFRAME, 'authenticated-awaiting-aviator');
            this.watchdog.setPage(this._page);
            this.health.setError(null);
            return true;
          } catch (error) {
            const category = classifyError(error);
            const retryDelay = backoffMs(routeAttempt++);
            nextRouteAttemptAt = Date.now() + retryDelay;
            const nextState = ['GAME_PROVIDER_UNAVAILABLE', 'SITE_UNAVAILABLE'].includes(error?.code)
              || category === 'NETWORK_RECOVER' ? State.SITE_UNAVAILABLE : State.WAITING_IFRAME;
            if (this.sm.current !== nextState) this.sm.transition(nextState, error?.reasonCode || 'AVIATOR_NAVIGATION_PENDING');
            this.health.setRecovery?.('NAVIGATING_AVIATOR', {
              attempt: routeAttempt, nextRetryAt: new Date(nextRouteAttemptAt).toISOString(),
              reasonCode: error?.reasonCode || error?.code || 'AVIATOR_NAVIGATION_FAILED',
              evidence: error?.evidence || null,
            });
            this.health.setError(formatError(error), error?.code || 'GAME_FRAME_MISSING', 'retry_aviator_navigation');
            log.warn(`Login is verified; Aviator navigation will retry in the existing page: ${formatError(error)}`);
          }
        } else if (session.status === 'EXPIRED' && Date.now() >= nextLoginAttemptAt) {
          this.health.setLoggedIn(false);
          if (this.loginMgr.hasAutomaticCredentials?.() && !this.signal?.aborted) {
            this.health.setAuthRequired(null);
            this.health.setRecovery?.('CHECKING_SESSION', { attempt: loginAttempt + 1, reasonCode: session.reasonCode, evidence: session.evidence });
            if (!this.sm.is(State.CHECKING_SESSION)) this.sm.transition(State.CHECKING_SESSION, session.reasonCode || 'verified-session-expiry');
            try {
              await this.loginMgr.ensureLoggedIn(this._page, this.signal);
              this.health.setLoggedIn(true);
              this.health.recordLogin();
              nextRouteAttemptAt = 0;
              routeAttempt = 0;
              continue;
            } catch (error) {
              loginAttempt += 1;
              nextLoginAttemptAt = Date.now() + backoffMs(loginAttempt - 1);
              this.health.setAuthRequired(error?.message || 'Automatic login did not complete; manual verification may be required');
              this.health.setError(formatError(error), error?.code || 'AUTH_REQUIRED', 'wait_for_authentication');
              this.health.setRecovery?.('AUTH_REQUIRED', {
                attempt: loginAttempt, nextRetryAt: new Date(nextLoginAttemptAt).toISOString(),
                reasonCode: error?.reasonCode || error?.code || session.reasonCode,
                evidence: session.evidence || null,
              });
              if (this.sm.current !== State.AUTH_REQUIRED) this.sm.transition(State.AUTH_REQUIRED, error?.reasonCode || 'manual-verification-required');
            }
          } else if (!manualLoginPrompted) {
            manualLoginPrompted = true;
            try { await this.loginMgr.ensureLoggedIn(this._page, this.signal); }
            catch (error) {
              this.health.setAuthRequired(error?.message || reason || 'Complete login or verification in the collector browser');
              this.health.setError(formatError(error), error?.code || 'AUTH_REQUIRED', 'wait_for_manual_login');
            }
          }
        } else if (session.status === 'MANUAL_VERIFICATION') {
          this.health.setLoggedIn(false);
          this.health.setAuthRequired('Manual CAPTCHA, 2FA, or platform verification is required in the collector browser');
          if (this.sm.current !== State.AUTH_REQUIRED) this.sm.transition(State.AUTH_REQUIRED, session.reasonCode || 'manual-verification-required');
          this.health.setRecovery?.('AUTH_REQUIRED', { reasonCode: session.reasonCode, evidence: session.evidence });
        } else if (session.status === 'UNKNOWN') {
          this.health.setLoggedIn(null);
          this.health.setAuthRequired(null);
          this.health.setRecovery?.('CHECKING_SESSION', { reasonCode: session.reasonCode || 'SESSION_STATUS_UNKNOWN', evidence: session.evidence || null });
          if (!this.sm.is(State.CHECKING_SESSION)) this.sm.transition(State.CHECKING_SESSION, session.reasonCode || 'session-status-unknown');
        }
      } else if (!this.browser.isAlive()) {
        try {
          const executionState = await this.roundStore.executionRecoveryStatus();
          if (!executionState.safe) {
            this.health.setRecovery?.('EXECUTION_RECONCILIATION_REQUIRED', { reasonCode: 'BROWSER_RESTART_BLOCKED_BY_EXECUTION_STATE', evidence: executionState });
            this.health.setError('Browser restart is paused until execution state is reconciled', 'EXECUTION_RECONCILIATION_REQUIRED', 'wait_for_execution_reconciliation');
            await sleep(backoffMs(loginAttempt++), this.signal);
            continue;
          }
          await this.browser.restart(this.signal);
          this._page = await this.browser.getAviatorPage();
          this.browser.setHealthSource?.(this.health);
          this.health.setBrowserConnected(true);
          this.health.setPageConnected(true);
          log.info('Managed browser recovered while waiting for manual login');
        } catch (error) {
          this.health.setError(formatError(error), 'BROWSER_RECOVERY', 'retry_browser_start');
          log.warn(`Browser recovery while awaiting login failed: ${formatError(error)}`);
        }
      }
      await sleep(Number(process.env.BOT_AUTH_POLL_MS || 2500), this.signal);
    }
    return false;
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
    // Provision/configure the second page out of band so slow navigation or
    // UI inspection cannot stall the independent History round collector.
    void this._ensureSharedAviatorPageSetup().catch(err => {
      log.warn(`Betting page is not yet provisioned in the collector context: ${formatError(err)}`);
    });

    // Always get a fresh frame — never reuse a cached reference
    const shouldInterrupt = () => {
      if (!this._watchdogReason) return null;
      const code = this._watchdogReason.startsWith('NETWORK:') ? 'NETWORK_UNAVAILABLE'
        : /login route|login controls prove session expiry/i.test(this._watchdogReason) ? 'AUTH_REQUIRED' : 'WATCHDOG_INTERRUPT';
      return Object.assign(new Error(this._watchdogReason), { code, reasonCode: code });
    };
    let frame;
    try {
      frame = await this.frameMgr.waitForFrame(this._page, {
        signal: this.signal,
        shouldInterrupt,
        timeoutMs: this.recovery.frameWaitTimeoutMs(
          Number(process.env.BOT_INITIAL_FRAME_TIMEOUT || process.env.BOT_FRAME_TIMEOUT || 120000),
        ),
      });
    } catch (error) {
      this.health.setGameReadiness?.(this.frameMgr.readiness());
      throw error;
    }
    this.health.setGameReadiness?.({ ...this.frameMgr.readiness(), contentLoaded: true, payoutsReady: true });
    this.health.setFrameConnected(true);
    this.health.setSiteStatus('AVAILABLE');

    await this.frameMgr.waitForPayouts(frame, this.signal, { shouldInterrupt });
    // The collector shares one authenticated context with Betting, so use its
    // current account-shell evidence when the History tab itself has no shell.
    // A game iframe or payout selector alone still does not prove a session.
    const session = typeof this.loginMgr.inspectSession === 'function'
      ? await this.loginMgr.inspectSession(this._page)
      : { status: await this.loginMgr.isLoggedIn(this._page) ? 'AUTHENTICATED'
        : await this.loginMgr.isExplicitlyLoggedOut(this._page) ? 'EXPIRED' : 'UNKNOWN' };
    if (!canContinueReadOnlyCollection(session.status)) {
      if (session.status === 'EXPIRED' || session.status === 'MANUAL_VERIFICATION') {
        this.health.setLoggedIn(false);
        throw Object.assign(new Error('Winner login or manual verification is required before collection'), {
          code: 'AUTH_REQUIRED', reasonCode: session.reasonCode || 'AUTH_REQUIRED', evidence: session.evidence || null,
        });
      }
      this.health.setLoggedIn(null);
      throw Object.assign(new Error(`Winner session is not verified after game-frame attachment (${session.reasonCode || 'SESSION_STATUS_UNKNOWN'}); continuing recovery without claiming authentication`), {
        code: 'SESSION_STATUS_UNKNOWN', reasonCode: session.reasonCode || 'SESSION_STATUS_UNKNOWN', evidence: session.evidence || null,
      });
    }
    if (session.status === 'UNKNOWN') {
      this.health.setLoggedIn(null);
      this.health.setRecovery?.('SESSION_UNKNOWN_READ_ONLY', {
        reasonCode: session.reasonCode || 'SESSION_STATUS_UNKNOWN', evidence: session.evidence || null,
      });
      log.warn(`Session authentication remains UNKNOWN (${session.reasonCode || 'SESSION_STATUS_UNKNOWN'}); continuing only read-only round collection from the payout-ready frame`);
    } else {
      this.health.setLoggedIn(true);
      this.health.setRecovery?.(null);
    }
    this.health.setAuthRequired(null);

    const { multipliers: initMults, signature: initSig } = await this.collector.install(frame);
    this.health.recordObserverInstalled();

    // Establish or update baseline snapshot
    if (!this._prevSnapshot) {
      if (this._history.length && initMults.length) {
        const persistedSnapshot = this._history.slice().reverse().slice(0, initMults.length)
          .map(round => Number(round.multiplier));
        const inferred = inferNewMultipliers(persistedSnapshot, initMults);
        if (inferred === null) {
          this._unverifiedGapPending = true;
          const maxSearchableShift = Math.max(0, initMults.length - 10);
          log.warn(`Startup continuity gap: no 10-round overlap found (PostgreSQL tail snapshot=${persistedSnapshot.length}, Aviator snapshot=${initMults.length}, largest searchable shift=${maxSearchableShift}); skipped uncertain catch-up and marked the next observed segment with a gap`);
        } else if (inferred.length) {
          const { history, added } = appendRounds(this._history, inferred, new Date().toISOString());
          if (added.length) {
            this._stageDirectRoundObservations(added, 'startup_catchup');
            this._history = history;
            log.info(`Startup catch-up: ${added.length} overlap-verified round(s) durably queued`);
          }
        }
      }
      this._prevSnapshot = initMults;
      this._prevSig      = initSig;
      log.info(`Baseline: ${initMults.length} rounds`);
    } else if (initSig !== this._prevSig) {
      // Reconnected after reload — catch up missed rounds
      const inferred = inferNewMultipliers(this._prevSnapshot, initMults);
      if (inferred === null) {
        this._unverifiedGapPending = true;
        const maxSearchableShift = Math.max(0, initMults.length - 10);
        log.warn(`History reconnect opened a continuity gap: no 10-round overlap found (previous snapshot=${this._prevSnapshot.length}, Aviator snapshot=${initMults.length}, largest searchable shift=${maxSearchableShift}); uncertain rounds were not inferred`);
      } else if (inferred.length > 0) {
          const { history, added } = appendRounds(this._history, inferred, new Date().toISOString(),
            { gapBefore: this._unverifiedGapPending });
        if (added.length > 0) {
          this._stageDirectRoundObservations(added, 'history_reconnect');
          this._history = history;
          this._unverifiedGapPending = false;
          log.info(`Catch-up: ${added.length} missed round(s) durably queued for PostgreSQL`);
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
        const installed = await this.collector.isInstalled(frame).catch(error => {
          log.debug(`Collector observer probe failed during idle check: ${formatError(error)}`);
          return false;
        });
        if (!installed) throw new Error('collector_not_installed');
        continue;
      }

      if (event?.phaseOnly) {
        void this._publishGamePhase(event.gamePhase);
        continue;
      }

      if (!event?.multipliers?.length) continue;

      const currSig = event.signature;
      if (currSig === this._prevSig) continue;

      const inferred = inferNewMultipliers(this._prevSnapshot, event.multipliers);
      if (inferred === null) {
        this._unverifiedGapPending = true;
        this._prevSnapshot = event.multipliers;
        this._prevSig = currSig;
        log.error('History snapshot changed without overlap proof; rounds are not fabricated and continuity is marked unknown');
        continue;
      }
      if (inferred.length === 0) {
        this._prevSnapshot = event.multipliers;
        this._prevSig      = currSig;
        continue;
      }

      // Save new rounds
      if (currSig !== this._lastSavedSig) {
        const { history, added } = appendRounds(this._history, inferred, event.timestamp || new Date().toISOString(),
          { gapBefore: this._unverifiedGapPending });
        if (added.length > 0) {
          if (event.gamePhase && added.length) {
            const phaseSource = added.at(-1);
            added.at(-1).game_phase = { ...event.gamePhase,
              event_id: [event.gamePhase.phase, event.gamePhase.observed_at, phaseSource.round_id].join(':') };
          }
          // Publish the real browser observation before the PostgreSQL write.
          // The observer must not treat a target as unknown merely because its
          // row is waiting behind an advisory lock held by score persistence.
          this._stageDirectRoundObservations(added, 'live_history');
          this._history = history;
          this._lastSavedSig = currSig;
          if (event.gamePhase) this.health.recordGamePhase(added.at(-1)?.game_phase, added.at(-1));
          this._unverifiedGapPending = false;
          log.info('Completed round durably observed; PostgreSQL persistence is running in the background', {
            count: added.length, backlog_size: this.directEventOutbox.stats().backlog_size,
          });
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
  log.info(`Starting independent collector (saved session available; automatic credentials ${creds.phone && creds.password ? 'configured' : 'not configured'}, headless=${creds.headless})`);
  let recoveryHeartbeat = null;

  // Infinite outer loop — never exits unless signal is aborted
  while (!signal?.aborted) {
    if (recoveryHeartbeat) {
      recoveryHeartbeat.health.stop();
      recoveryHeartbeat = null;
    }
    // A failed run may leave its state machine in any state. Each outer retry
    // therefore gets a clean lifecycle instead of attempting invalid backward
    // transitions such as GAME_LOADING → LOGIN.
    const collector = new AviatorCollector(creds, signal);
    try {
      await collector.run();
    } catch (err) {
      if (signal?.aborted) break;
      if (err?.code === 'AUTH_REQUIRED') {
        log.error('AUTH_REQUIRED: keeping monitoring alive for manual login or platform verification');
        await sleep(Number(process.env.BOT_AUTH_POLL_MS || 2500), signal);
        continue;
      }
      if (err?.code === 'COLLECTOR_ALREADY_ACTIVE') {
        log.error(`Collector start refused: ${err.message}`);
        break;
      }
      const cooldown = Number(process.env.BOT_RECOVERY_CYCLE_COOLDOWN_MS || 60000);
      log.error(`Outer collector cycle failed; preserving outbox and retrying after ${cooldown}ms: ${formatError(err)}`);
      await sleep(cooldown, signal);
    } finally {
      await collector.shutdown({ deliberate: Boolean(signal?.aborted) });
      if (!signal?.aborted) recoveryHeartbeat = collector;
    }
  }

  log.info('Collector shut down cleanly.');
}
