/**
 * HealthMonitor.js — Tracks collector health metrics and writes status.json.
 * Exposes a snapshot for the Watchdog and the Flask /health endpoint.
 */

import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';
import { log } from './Logger.js';

const __dirname    = path.dirname(fileURLToPath(import.meta.url));
const STATUS_PATH  = path.join(__dirname, '..', '..', 'data', 'bot', 'status.json');
const FLUSH_INTERVAL_MS = 5000;
const STALE_AFTER_MS = Number(process.env.HISTORY_STALE_AFTER_MS || 180000);

export class HealthMonitor {
  constructor() {
    this._startTime = Date.now();
    this._metrics   = {
      collectorRunning:   false,
      pid:                process.pid,
      browserConnected:   false,
      pageConnected:      false,
      frameConnected:     false,
      siteStatus:         'UNKNOWN',
      loggedIn:           false,
      authenticationState: 'UNKNOWN',
      authenticationReason: 'SESSION_NOT_VERIFIED',
      authRequired:       false,
      authRequiredReason: null,
      sessionWatchdog: {
        status: 'INACTIVE', monitor_tab_status: 'NOT_STARTED', reason_code: 'NOT_CHECKED',
        last_successful_check_at: null, last_successful_activity_at: null,
        estimated_remaining_seconds: null, next_scheduled_maintenance_at: null,
        last_successful_maintenance_at: null, last_successful_refresh_at: null,
        last_verified_no_click_at: null,
        logout_countdown_seconds: null,
        recovery_attempts: 0, collector_connected: false,
      },
      state:              'STARTING',
      activityState:      'STARTING',
      gameReadiness:      { frameAttached: false, contentLoaded: false, payoutsReady: false, reason: 'NOT_CHECKED' },
      recovery:           { stage: null, attempt: 0, nextRetryAt: null, reasonCode: null, evidence: null },
      lastRoundTime:      null,
      lastRoundId:        null,
      lastRoundIndex:     null,
      lastDetectedRoundId: null,
      lastDetectedAt: null,
      lastDetectedStatus: 'UNKNOWN',
      lastDetectedReason: null,
      lastObservedRoundId: null,
      lastObservedRoundIndex: null,
      lastObservedAt:     null,
      gamePhase:          'UNKNOWN',
      gamePhaseObservedAt: null,
      gamePhaseEventId:   null,
      gamePhaseEvidence:  null,
      gamePhaseSourceRoundId: null,
      gamePhaseSourceRoundIndex: null,
      lastSuccessfulCollection: null,
      roundCommitSamples: [],
      heartbeatAt:        null,
      lastObserverInstalledAt: null,
      network:            { status: 'UNKNOWN', reason: null, checked_at: null },
      backend:            { status: 'UNKNOWN', reason: null, checked_at: null },
      storage:            { status: 'UNKNOWN', backlog_size: 0, capacity_records: null, capacity_bytes: null },
      browser_pages:      { browser_count: 0, context_count: 0, page_count: 0,
        aviator_page_count: 0, aviator_target_id: null, aviator_page_state: 'UNKNOWN',
        game_frame_count: 0, game_frame_state: 'UNKNOWN', page_role: 'SHARED_AVIATOR',
        history_target_id: null, history_page_state: 'UNKNOWN' },
      lastError:          null,
      lastRecovery:       null,
      recoveryCount:      0,
      browserRestartCount:0,
      loginCount:         0,
      totalRoundsSaved:   0,
      uptime:             0,
    };
    this._flushTimer = null;
    this._deferredFlushTimer = null;
    this._lastFlushErrorAt = 0;
    this._flushBatchDepth = 0;
    this._flushPending = false;
  }

  start() {
    this._flushTimer = setInterval(() => this._flush(), FLUSH_INTERVAL_MS);
    this._flushTimer.unref?.(); // don't keep process alive
  }

  stop() {
    if (this._flushTimer) { clearInterval(this._flushTimer); this._flushTimer = null; }
    if (this._deferredFlushTimer) {
      clearTimeout(this._deferredFlushTimer);
      this._deferredFlushTimer = null;
    }
    this._flush();
  }

  batch(callback) {
    this._flushBatchDepth += 1;
    try {
      return callback();
    } finally {
      this._flushBatchDepth -= 1;
      if (this._flushBatchDepth === 0 && this._flushPending) {
        this._flushPending = false;
        this._flush();
      }
    }
  }

  // ── Setters ───────────────────────────────────────────────────────────────

  setState(s) {
    const publicState = ({ LOGIN: 'CHECKING_SESSION', HOME: 'NAVIGATING_AVIATOR',
      GAME_LOADING: 'NAVIGATING_AVIATOR', RELOGIN: 'AUTH_REQUIRED',
      RESTART_BROWSER: 'RECOVERING', STOPPED: 'STOPPING' })[s] || s;
    this._metrics.activityState = publicState;
    const next = this._effectiveState(publicState);
    if (this._metrics.state === next) return;
    this._metrics.state = next;
    // Publish transitions immediately. The statistical observer uses this
    // file to decide whether a target can be scored prospectively, so leaving
    // WAITING/RECOVERING in the file after the collector resumed can create a
    // false pipeline miss until the next periodic flush.
    this._flush();
  }

  _effectiveState(activityState = this._metrics.activityState) {
    if (this._metrics.authRequired) return 'AUTH_REQUIRED';
    if (['OFFLINE', 'DNS_UNAVAILABLE'].includes(this._metrics.network.status)) return 'OFFLINE';
    if (['WINNER_UNREACHABLE', 'MAIN_SITE_UNAVAILABLE', 'GAME_PROVIDER_UNAVAILABLE'].includes(this._metrics.network.status)
        || this._metrics.siteStatus === 'UNAVAILABLE') return 'SITE_UNAVAILABLE';
    if (this._metrics.storage.status === 'UNAVAILABLE') return 'STORAGE_UNAVAILABLE';
    return activityState;
  }
  setCollectorRunning(v)     { this._metrics.collectorRunning = v; this._flush(); }
  setBrowserConnected(v)     { this._metrics.browserConnected = v; this._flush(); }
  setBrowserPages(value) {
    if (!value || typeof value !== 'object') return;
    const next = {
      browser_count: Number(value.browser_count) || 0,
      context_count: Number(value.context_count) || 0,
      page_count: Number(value.page_count) || 0,
      aviator_page_count: Number(value.aviator_page_count) || 0,
      aviator_target_id: typeof value.aviator_target_id === 'string' ? value.aviator_target_id : null,
      aviator_page_state: String(value.aviator_page_state || 'UNKNOWN'),
      game_frame_count: Number(value.game_frame_count) || 0,
      game_frame_state: String(value.game_frame_state || 'UNKNOWN'),
      page_role: String(value.page_role || 'SHARED_AVIATOR'),
      history_target_id: typeof value.history_target_id === 'string' ? value.history_target_id
        : typeof value.aviator_target_id === 'string' ? value.aviator_target_id : null,
      history_page_state: String(value.history_page_state || value.aviator_page_state || 'UNKNOWN'),
    };
    if (JSON.stringify(next) === JSON.stringify(this._metrics.browser_pages)) return;
    this._metrics.browser_pages = next;
    this._flush();
  }
  setAuthenticationState(state, reason = null) {
    const next = ['AUTHENTICATED', 'AUTH_REQUIRED', 'UNKNOWN'].includes(state) ? state : 'UNKNOWN';
    this._metrics.authenticationState = next;
    this._metrics.authenticationReason = reason ? String(reason).slice(0, 200)
      : next === 'UNKNOWN' ? 'SESSION_NOT_VERIFIED' : null;
    this._metrics.loggedIn = next === 'AUTHENTICATED' ? true : next === 'AUTH_REQUIRED' ? false : null;
    this._metrics.authRequired = next === 'AUTH_REQUIRED';
    this._metrics.authRequiredReason = next === 'AUTH_REQUIRED' ? this._metrics.authenticationReason : null;
    this._metrics.state = this._effectiveState();
    this._flush();
  }
  setPageConnected(v)        { this._metrics.pageConnected = v; this._flush(); }
  setFrameConnected(v)       { this._metrics.frameConnected = v; this._flush(); }
  setSiteStatus(value) {
    this._metrics.siteStatus = value || 'UNKNOWN';
    this._metrics.state = this._effectiveState();
    this._flush();
  }
  setLoggedIn(v) {
    this._metrics.loggedIn = v;
    this._metrics.authenticationState = v === true ? 'AUTHENTICATED'
      : v === false && this._metrics.authRequired ? 'AUTH_REQUIRED' : 'UNKNOWN';
    this._metrics.authenticationReason = this._metrics.authenticationState === 'UNKNOWN'
      ? 'SESSION_NOT_VERIFIED' : this._metrics.authRequiredReason;
    if (v && this._metrics.authRequired) this.setAuthRequired(null);
    this._flush();
  }
  setAuthRequired(reason = null) {
    this._metrics.authRequired = Boolean(reason);
    this._metrics.authRequiredReason = reason ? String(reason) : null;
    this._metrics.authenticationState = reason ? 'AUTH_REQUIRED'
      : this._metrics.loggedIn === true ? 'AUTHENTICATED' : 'UNKNOWN';
    this._metrics.authenticationReason = this._metrics.authenticationState === 'UNKNOWN'
      ? 'SESSION_NOT_VERIFIED' : this._metrics.authRequiredReason;
    this._metrics.state = this._effectiveState();
    this._flush();
  }
  setNetwork(value) {
    this._metrics.network = value;
    if (['WINNER_UNREACHABLE', 'MAIN_SITE_UNAVAILABLE'].includes(value?.status)) this._metrics.siteStatus = 'UNAVAILABLE';
    else if (value?.status === 'ONLINE') this._metrics.siteStatus = 'AVAILABLE';
    this._metrics.state = this._effectiveState();
    this._flush();
  }
  setBackend(value)        { this._metrics.backend = value || { status: 'UNKNOWN' }; this._flush(); }
  setStorage(value) {
    this._metrics.storage = { ...this._metrics.storage, ...(value || {}) };
    this._metrics.state = this._effectiveState();
    this._scheduleFlush();
  }
  setGameReadiness(value = {}) {
    this._metrics.gameReadiness = { ...this._metrics.gameReadiness, ...value };
    this._metrics.frameConnected = this._metrics.gameReadiness.frameAttached === true;
    this._scheduleFlush();
  }
  setRecovery(stage, { attempt = 0, nextRetryAt = null, reasonCode = null, evidence = null } = {}) {
    this._metrics.recovery = { stage: stage || null, attempt, nextRetryAt, reasonCode, evidence };
    this._flush();
  }
  setSessionWatchdog(value = {}) {
    const allowedStatuses = new Set(['AUTHENTICATED', 'ACTIVE', 'INACTIVE', 'WARNING',
      'EXPIRING', 'RECOVERING', 'EXPIRED', 'DISCONNECTED']);
    const allowedMonitorStates = new Set(['NOT_STARTED', 'OPEN', 'CLOSED', 'DISABLED', 'UNAVAILABLE']);
    const next = { ...this._metrics.sessionWatchdog, ...value };
    if (!allowedStatuses.has(next.status)) next.status = 'WARNING';
    if (!allowedMonitorStates.has(next.monitor_tab_status)) next.monitor_tab_status = 'UNAVAILABLE';
    for (const field of ['estimated_remaining_seconds', 'logout_countdown_seconds']) {
      const number = Number(next[field]);
      next[field] = next[field] !== null && next[field] !== undefined
        && Number.isFinite(number) && number >= 0 ? Math.floor(number) : null;
    }
    next.recovery_attempts = Number.isFinite(Number(next.recovery_attempts))
      ? Math.max(0, Math.floor(Number(next.recovery_attempts))) : 0;
    next.collector_connected = next.collector_connected === true;
    this._metrics.sessionWatchdog = next;
    this._flush();
  }
  setError(error, category = 'UNKNOWN', recoveryAction = null) {
    this._metrics.lastError = error ? { message: String(error), category, recovery_action: recoveryAction,
      at: new Date().toISOString() } : null;
    this._flush();
  }
  recordObserverInstalled()  { this._metrics.lastObserverInstalledAt = new Date().toISOString(); }
  setPersistedRoundIdentity(round = null) {
    if (round?.round_id == null) return;
    this._metrics.lastRoundId = String(round.round_id);
    this._metrics.lastRoundIndex = Number.isSafeInteger(Number(round.round_index))
      ? Number(round.round_index) : null;
    this._flush();
  }
  recordObservedRound(round = null) {
    if (round?.round_id == null) return;
    this._metrics.lastObservedRoundId = String(round.round_id);
    this._metrics.lastObservedRoundIndex = Number.isSafeInteger(Number(round.round_index))
      ? Number(round.round_index) : null;
    this._metrics.lastObservedAt = round.observed_at || round.timestamp || new Date().toISOString();
    // The fsynced round outbox is already committed before this status update.
    // Coalesce the large diagnostic snapshot so it cannot delay the urgent
    // backend request; the backend also checks its own durable observation
    // inbox before accepting a target decision.
    this._scheduleFlush();
  }
  recordRoundDetection({ round_id = null, detected_at = null, status = 'DETECTED', reason = null } = {},
    { flush = true } = {}) {
    this._metrics.lastDetectedRoundId = round_id == null ? null : String(round_id);
    this._metrics.lastDetectedAt = typeof detected_at === 'string' ? detected_at : null;
    this._metrics.lastDetectedStatus = String(status || 'UNKNOWN');
    this._metrics.lastDetectedReason = reason == null ? null : String(reason).slice(0, 200);
    if (flush) this._flush();
  }
  recordRounds(rounds = [], committedAt = null, monotonicSamples = null) {
    const batch = Array.isArray(rounds) ? rounds.filter(Boolean) : [];
    if (!batch.length) return;
    const exactCommitAcknowledgement = Boolean(committedAt);
    const acknowledgedAt = committedAt || new Date().toISOString();
    const measuredById = new Map((Array.isArray(monotonicSamples) ? monotonicSamples : [])
      .filter(item => item?.observationToCommitMs !== null
        && item?.observationToCommitMs !== undefined
        && Number.isFinite(Number(item.observationToCommitMs)))
      .map(item => [String(item.round_id), Number(item.observationToCommitMs)]));
    for (const round of batch) {
      if (monotonicSamples !== null) {
        const measuredMs = measuredById.get(String(round.round_id));
        if (Number.isFinite(measuredMs) && measuredMs >= 0) {
          this._metrics.roundCommitSamples.push({ round_id: String(round.round_id),
            observed_at: round.observed_at || round.timestamp || null,
            committed_at: acknowledgedAt, latency_ms: Math.round(measuredMs * 100) / 100,
            measurement_clock: 'COLLECTOR_MONOTONIC' });
        }
        continue;
      }
      const observedAt = round.observed_at || round.timestamp || null;
      const observedMs = observedAt ? Date.parse(observedAt) : NaN;
      const committedMs = Date.parse(acknowledgedAt);
      if (exactCommitAcknowledgement && round.round_id != null && Number.isFinite(observedMs) && Number.isFinite(committedMs)
          && committedMs >= observedMs) {
        this._metrics.roundCommitSamples.push({
          round_id: String(round.round_id),
          observed_at: new Date(observedMs).toISOString(),
          committed_at: new Date(committedMs).toISOString(),
          latency_ms: Math.round((committedMs - observedMs) * 100) / 100,
          measurement_clock: 'COLLECTOR_SYSTEM_UTC',
        });
      }
    }
    this._metrics.roundCommitSamples = this._metrics.roundCommitSamples.slice(-1000);
    const round = batch.at(-1);
    this._metrics.lastRoundTime = round.timestamp || acknowledgedAt;
    if (round.round_id != null) {
      this._metrics.lastRoundId = String(round.round_id);
      this._metrics.lastRoundIndex = Number.isSafeInteger(Number(round.round_index))
        ? Number(round.round_index) : null;
    }
    this._metrics.lastSuccessfulCollection = acknowledgedAt;
    this._metrics.heartbeatAt = this._metrics.lastSuccessfulCollection;
    this._metrics.totalRoundsSaved += batch.length;
    // The prospective scorer gates on this identity matching PostgreSQL's
    // latest persisted round. Waiting for the periodic heartbeat flush could
    // leave the status file up to five seconds behind every successful write.
    // Flush the new persisted identity immediately so it can be scored while
    // the next target is still unknown.
    this._flush();
  }
  recordRound(round = null, committedAt = null) {
    if (round) this.recordRounds([round], committedAt);
  }
  recordGamePhase(phase = null, sourceRound = null) {
    if (!phase || !['BETTING_OPEN', 'BETTING_CLOSED', 'UNKNOWN'].includes(phase.phase)) return;
    this._metrics.gamePhase = phase.phase;
    this._metrics.gamePhaseObservedAt = phase.observed_at || new Date().toISOString();
    this._metrics.gamePhaseEventId = phase.event_id || null;
    this._metrics.gamePhaseEvidence = phase.evidence || null;
    this._metrics.gamePhaseSourceRoundId = sourceRound?.round_id == null
      ? null : String(sourceRound.round_id);
    this._metrics.gamePhaseSourceRoundIndex = Number.isSafeInteger(Number(sourceRound?.round_index))
      ? Number(sourceRound.round_index) : null;
    this._flush();
  }
  recordRecovery(action)     { this._metrics.recoveryCount++; this._metrics.lastRecovery = { action, ts: new Date().toISOString() }; }
  recordBrowserRestart()     { this._metrics.browserRestartCount++; }
  recordLogin()              { this._metrics.loginCount++; }

  // ── Getters ───────────────────────────────────────────────────────────────

  snapshot() {
    let health = 'WAITING';
    // Use local persistence time for freshness. Platform timestamps can reverse
    // slightly or arrive late and must not mark a newly stored round stale.
    const activityTime = this._metrics.lastSuccessfulCollection || this._metrics.lastObserverInstalledAt;
    const staleSince = activityTime ? Date.parse(activityTime) : this._startTime;
    const collectorStale = this._metrics.collectorRunning
      && Number.isFinite(staleSince) && Date.now() - staleSince > STALE_AFTER_MS;
    const networkCheckedAt = this._metrics.network?.checked_at ? Date.parse(this._metrics.network.checked_at) : NaN;
    const networkStale = !Number.isFinite(networkCheckedAt) || Date.now() - networkCheckedAt > 30000;
    if (!this._metrics.collectorRunning) health = 'STOPPED';
    else if (this._metrics.authRequired) health = 'AUTH_REQUIRED';
    else if (['OFFLINE', 'DNS_UNAVAILABLE'].includes(this._metrics.network.status)) health = 'OFFLINE';
    else if (['WINNER_UNREACHABLE', 'MAIN_SITE_UNAVAILABLE', 'GAME_PROVIDER_UNAVAILABLE'].includes(this._metrics.network.status)
        || this._metrics.siteStatus === 'UNAVAILABLE') health = 'SITE_UNAVAILABLE';
    else if (this._metrics.storage.status === 'UNAVAILABLE') health = 'STORAGE_UNAVAILABLE';
    else if (this._metrics.network.status !== 'ONLINE') health = 'UNKNOWN';
    else if (networkStale) health = 'STALE';
    else if (!this._metrics.browserConnected) health = 'CONNECTING';
    else if (!this._metrics.frameConnected) health = 'DISCONNECTED';
    else if (collectorStale) health = 'STALE';
    else if (this._metrics.state === 'COLLECTING') health = 'HEALTHY';
    const sourceUnavailable = this._metrics.authRequired || this._metrics.siteStatus === 'UNAVAILABLE'
      || this._metrics.browserConnected === false || this._metrics.pageConnected === false
      || this._metrics.frameConnected === false;
    const sourceUnknown = this._metrics.authenticationState !== 'AUTHENTICATED'
      || this._metrics.browserConnected !== true || this._metrics.pageConnected !== true
      || this._metrics.frameConnected !== true;
    const roundSourceStatus = sourceUnavailable ? 'SOURCE_UNAVAILABLE'
      : sourceUnknown ? 'UNKNOWN' : 'AVAILABLE';
    const delivery = this._metrics.storage?.round_delivery || {};
    const lastDeliveryFailure = delivery.last_failure || null;
    const backlogSize = Number(delivery.backlog_size || this._metrics.storage?.backlog_size || 0);
    const successfulAt = this._metrics.lastSuccessfulCollection
      ? Date.parse(this._metrics.lastSuccessfulCollection) : NaN;
    const recentSuccessfulCollection = Number.isFinite(successfulAt)
      && Date.now() - successfulAt <= STALE_AFTER_MS;
    let roundCollectionStatus = 'UNKNOWN';
    if (!this._metrics.collectorRunning) roundCollectionStatus = 'STOPPED';
    else if (roundSourceStatus === 'SOURCE_UNAVAILABLE') roundCollectionStatus = 'SOURCE_UNAVAILABLE';
    else if (this._metrics.storage?.status === 'UNAVAILABLE' || lastDeliveryFailure?.stage === 'persistence') {
      roundCollectionStatus = 'PERSISTENCE_FAILED';
    } else if (this._metrics.lastDetectedStatus === 'INVALID') roundCollectionStatus = 'ROUND_DETECTED_INVALID';
    else if (this._metrics.lastDetectedStatus === 'DETECTED') roundCollectionStatus = 'ROUND_DETECTED';
    else if (roundSourceStatus === 'UNKNOWN') roundCollectionStatus = 'UNKNOWN';
    else if (backlogSize > 0 || (this._metrics.lastObservedRoundId
        && this._metrics.lastRoundId !== this._metrics.lastObservedRoundId)) {
      roundCollectionStatus = 'ROUND_PENDING_PERSISTENCE';
    } else if (recentSuccessfulCollection && this._metrics.state === 'COLLECTING') {
      roundCollectionStatus = 'HEALTHY_CAUGHT_UP';
    } else if (this._metrics.lastObserverInstalledAt && this._metrics.state === 'COLLECTING') {
      roundCollectionStatus = 'NO_NEW_ROUND_YET';
    }
    const reportedState = health === 'AUTH_REQUIRED' ? 'AUTH_REQUIRED'
      : health === 'OFFLINE' ? 'OFFLINE'
        : health === 'SITE_UNAVAILABLE' ? 'SITE_UNAVAILABLE'
          : health === 'STORAGE_UNAVAILABLE' ? 'STORAGE_UNAVAILABLE'
            : health === 'UNKNOWN' ? 'UNKNOWN'
              : health === 'STALE' && networkStale ? 'STALE'
                : collectorStale && this._metrics.state === 'COLLECTING' ? 'COLLECTOR_STALE'
                  : health === 'STALE' ? 'STALE' : this._metrics.state;
    return {
      ...this._metrics,
      state: reportedState,
      health,
      roundSourceStatus,
      roundCollectionStatus,
      staleAfterMs: STALE_AFTER_MS,
      uptime: Math.floor((Date.now() - this._startTime) / 1000),
    };
  }

  /** Seconds since the last round was saved. null if no round yet. */
  secondsSinceLastRound() {
    if (!this._metrics.lastSuccessfulCollection) return null;
    return (Date.now() - new Date(this._metrics.lastSuccessfulCollection).getTime()) / 1000;
  }

  secondsSinceCollectionActivity() {
    const times = [this._metrics.lastSuccessfulCollection, this._metrics.lastObserverInstalledAt]
      .map(value => value ? Date.parse(value) : NaN).filter(Number.isFinite);
    return times.length ? (Date.now() - Math.max(...times)) / 1000 : null;
  }

  // ── Persistence ───────────────────────────────────────────────────────────

  _flush() {
    if (this._deferredFlushTimer) {
      clearTimeout(this._deferredFlushTimer);
      this._deferredFlushTimer = null;
    }
    if (this._flushBatchDepth > 0) {
      this._flushPending = true;
      return;
    }
    try {
      if (this._metrics.collectorRunning) this._metrics.heartbeatAt = new Date().toISOString();
      this._writeSnapshot(this.snapshot());
    } catch (error) {
      if (Date.now() - this._lastFlushErrorAt > 60000) {
        this._lastFlushErrorAt = Date.now();
        log.error(`Collector heartbeat write failed: ${error?.message || error}`);
      }
    }
  }

  _scheduleFlush() {
    if (this._flushBatchDepth > 0) {
      this._flushPending = true;
      return;
    }
    if (this._deferredFlushTimer) return;
    // Round delivery already has an fsynced durable outbox. Coalesce status
    // file writes so JSON formatting and filesystem latency never sit between
    // extraction and the urgent backend request.
    this._deferredFlushTimer = setTimeout(() => {
      this._deferredFlushTimer = null;
      this._flush();
    }, 100);
    this._deferredFlushTimer.unref?.();
  }

  _writeSnapshot(snap) {
    fs.mkdirSync(path.dirname(STATUS_PATH), { recursive: true });
    const tmp = STATUS_PATH + '.tmp';
    fs.writeFileSync(tmp, JSON.stringify(snap, null, 2), 'utf-8');
    fs.renameSync(tmp, STATUS_PATH);
  }
}
