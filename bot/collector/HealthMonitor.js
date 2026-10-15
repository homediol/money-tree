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
      state:              'STARTING',
      activityState:      'STARTING',
      gameReadiness:      { frameAttached: false, contentLoaded: false, payoutsReady: false, reason: 'NOT_CHECKED' },
      recovery:           { stage: null, attempt: 0, nextRetryAt: null, reasonCode: null, evidence: null },
      lastRoundTime:      null,
      lastRoundId:        null,
      lastRoundIndex:     null,
      lastObservedRoundId: null,
      lastObservedRoundIndex: null,
      lastObservedAt:     null,
      gamePhase:          'UNKNOWN',
      roundPhase:         'UNKNOWN',
      gamePhaseObservedAt: null,
      gamePhaseEventId:   null,
      gamePhaseEvidence:  null,
      gamePhaseSourceRoundId: null,
      gamePhaseSourceRoundIndex: null,
      panelObservations:   [],
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
        history_target_id: null, betting_target_id: null,
        history_page_state: 'UNKNOWN', betting_page_state: 'SHARED_PAGE_UNAVAILABLE' },
      cashout_initialization: {
        target: '2.00', auto_bet: 'UNKNOWN', status: 'CASHOUT_UNAVAILABLE',
        panel_1_cashout: 'CASHOUT_UNAVAILABLE', panel_2_cashout: 'CASHOUT_UNAVAILABLE',
        panels: [], updated_at: new Date().toISOString(),
        error_reason: 'WAITING_FOR_AUTHENTICATED_SHARED_AVIATOR_PAGE',
      },
      bet_amount_initialization: {
        status: 'STAKE_UNAVAILABLE', execution_panel: null, requested_stake: null,
        currency: null, requested_stake_currency: null, platform_currency: null,
        observed_value: null, session_id: null,
        updated_at: null, error_reason: 'NOT_INITIALIZED',
      },
      betting_ui_configuration: { status: 'UNKNOWN', updated_at: null, error_reason: 'NOT_INITIALIZED' },
      lastError:          null,
      lastRecovery:       null,
      recoveryCount:      0,
      browserRestartCount:0,
      loginCount:         0,
      totalRoundsSaved:   0,
      uptime:             0,
    };
    this._flushTimer = null;
    this._lastFlushErrorAt = 0;
  }

  start() {
    this._flushTimer = setInterval(() => this._flush(), FLUSH_INTERVAL_MS);
    this._flushTimer.unref?.(); // don't keep process alive
  }

  stop() {
    if (this._flushTimer) { clearInterval(this._flushTimer); this._flushTimer = null; }
    this._flush();
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
      betting_target_id: typeof value.betting_target_id === 'string' ? value.betting_target_id
        : typeof value.aviator_target_id === 'string' ? value.aviator_target_id : null,
      history_page_state: String(value.history_page_state || value.aviator_page_state || 'UNKNOWN'),
      betting_page_state: String(value.betting_page_state || value.aviator_page_state || 'SHARED_PAGE_UNAVAILABLE'),
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
  setCashoutInitialization(value) {
    if (!value || typeof value !== 'object') return;
    const priorUpdatedAt = Date.parse(this._metrics.cashout_initialization?.updated_at || '');
    const nextUpdatedAt = Date.parse(value.updated_at || '');
    if (Number.isFinite(priorUpdatedAt) && Number.isFinite(nextUpdatedAt)
        && nextUpdatedAt < priorUpdatedAt) return;
    const cashoutStatuses = ['CASHOUT_VERIFIED', 'CASHOUT_MISMATCH', 'CASHOUT_UNAVAILABLE'];
    const status = cashoutStatuses.includes(value.status) ? value.status : 'CASHOUT_UNAVAILABLE';
    const target = '2.00';
    const panels = Array.isArray(value.panels) ? value.panels.slice(0, 2).map((panel, slot) => ({
      slot,
      status: cashoutStatuses.includes(panel?.status) ? panel.status : 'CASHOUT_UNAVAILABLE',
      panel_id: panel?.panel_id || `PANEL_${slot + 1}`,
      frame_identity: typeof panel?.frame_identity === 'string' ? panel.frame_identity.slice(0, 240) : null,
      auto_bet: ['OFF', 'ON', 'UNKNOWN'].includes(panel?.auto_bet) ? panel.auto_bet : 'UNKNOWN',
      auto_cashout: ['OFF', 'ON', 'UNKNOWN'].includes(panel?.auto_cashout) ? panel.auto_cashout : 'UNKNOWN',
      multiplier: panel?.multiplier == null ? null : String(panel.multiplier).slice(0, 16),
      auto_tab_selected: panel?.auto_tab_selected === true,
      observed_at: typeof panel?.observed_at === 'string' ? panel.observed_at : new Date().toISOString(),
      error_reason: typeof panel?.error_reason === 'string' ? panel.error_reason.slice(0, 200) : null,
      manual_correction: typeof panel?.manual_correction === 'string' ? panel.manual_correction.slice(0, 240) : null,
    })) : [];
    const next = {
      target, auto_bet: ['OFF', 'ON', 'UNKNOWN'].includes(value.auto_bet) ? value.auto_bet : 'UNKNOWN',
      status,
      panel_1_cashout: cashoutStatuses.includes(value.panel_1_cashout) ? value.panel_1_cashout : 'CASHOUT_UNAVAILABLE',
      panel_2_cashout: cashoutStatuses.includes(value.panel_2_cashout) ? value.panel_2_cashout : 'CASHOUT_UNAVAILABLE',
      panels,
      updated_at: typeof value.updated_at === 'string' ? value.updated_at : new Date().toISOString(),
      error_reason: typeof value.error_reason === 'string' ? value.error_reason.slice(0, 300) : null,
    };
    this._metrics.cashout_initialization = next;
    this._refreshBettingUiConfiguration();
    this._flush();
  }
  setBetAmountInitialization(value) {
    if (!value || typeof value !== 'object') return;
    const stakeStatuses = ['STAKE_VERIFIED', 'STAKE_MISMATCH', 'STAKE_UNAVAILABLE'];
    const status = stakeStatuses.includes(value.status) ? value.status : 'STAKE_UNAVAILABLE';
    const panel = Number.isInteger(value.execution_panel) && [0, 1].includes(value.execution_panel)
      ? value.execution_panel : null;
    this._metrics.bet_amount_initialization = {
      status,
      execution_panel: panel,
      requested_stake: value.requested_stake == null ? null : String(value.requested_stake).slice(0, 32),
      currency: typeof value.currency === 'string' ? value.currency.slice(0, 3) : null,
      requested_stake_currency: typeof value.requested_stake_currency === 'string'
        ? value.requested_stake_currency.slice(0, 3) : null,
      platform_currency: typeof value.platform_currency === 'string'
        ? value.platform_currency.slice(0, 3) : null,
      observed_value: value.observed_value == null ? null : String(value.observed_value).slice(0, 32),
      observed_currency: typeof value.observed_currency === 'string' ? value.observed_currency.slice(0, 3) : null,
      minimum_stake: value.minimum_stake == null ? null : String(value.minimum_stake).slice(0, 32),
      pending_bet_state: ['CLEAR', 'PENDING', 'UNKNOWN'].includes(value.pending_bet_state) ? value.pending_bet_state : 'UNKNOWN',
      session_id: typeof value.session_id === 'string' ? value.session_id.slice(0, 80) : null,
      updated_at: typeof value.updated_at === 'string' ? value.updated_at : new Date().toISOString(),
      error_reason: typeof value.error_reason === 'string' ? value.error_reason.slice(0, 200) : null,
    };
    this._refreshBettingUiConfiguration();
    this._flush();
  }
  _refreshBettingUiConfiguration() {
    const cashout = this._metrics.cashout_initialization || {};
    const amount = this._metrics.bet_amount_initialization || {};
    const verified = cashout.status === 'CASHOUT_VERIFIED' && amount.status === 'STAKE_VERIFIED';
    const mismatch = cashout.status === 'CASHOUT_MISMATCH' || amount.status === 'STAKE_MISMATCH';
    const status = verified ? 'READY' : mismatch ? 'BLOCKED' : 'UNKNOWN';
    const reasons = [cashout.error_reason, amount.error_reason].filter(Boolean);
    this._metrics.betting_ui_configuration = {
      status,
      updated_at: new Date().toISOString(),
      error_reason: reasons.length ? reasons.join('; ').slice(0, 300) : null,
    };
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
    this._flush();
  }
  setGameReadiness(value = {}) {
    this._metrics.gameReadiness = { ...this._metrics.gameReadiness, ...value };
    this._metrics.frameConnected = this._metrics.gameReadiness.frameAttached === true;
    this._flush();
  }
  setRecovery(stage, { attempt = 0, nextRetryAt = null, reasonCode = null, evidence = null } = {}) {
    this._metrics.recovery = { stage: stage || null, attempt, nextRetryAt, reasonCode, evidence };
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
    // Publish the observation before PostgreSQL persistence can wait on the
    // scorer's advisory lock. The backend uses this marker to reject a score
    // if the target has already become visible to the collector.
    this._flush();
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
    this._metrics.roundPhase = ['ROUND_ENDED', 'INTERMISSION', 'BETTING_OPEN',
      'BETTING_CLOSED', 'ROUND_RUNNING', 'UNKNOWN'].includes(phase.round_phase)
      ? phase.round_phase : 'UNKNOWN';
    this._metrics.gamePhaseObservedAt = phase.observed_at || new Date().toISOString();
    this._metrics.gamePhaseEventId = phase.event_id || null;
    this._metrics.gamePhaseEvidence = phase.evidence || null;
    this._metrics.gamePhaseSourceRoundId = sourceRound?.round_id == null
      ? null : String(sourceRound.round_id);
    this._metrics.gamePhaseSourceRoundIndex = Number.isSafeInteger(Number(sourceRound?.round_index))
      ? Number(sourceRound.round_index) : null;
    this._flush();
  }
  recordPanelObservations(observations = [], sourceRound = null) {
    if (!Array.isArray(observations)) return;
    const previous = new Map((this._metrics.panelObservations || [])
      .map(row => [String(row.panel_id || ''), row]));
    const latest = new Map(previous);
    for (const row of observations) {
      if (!row || !['PANEL_1', 'PANEL_2'].includes(String(row.panel_id || ''))) continue;
      const observedAt = Date.parse(row.observed_at || '');
      const existingAt = Date.parse(latest.get(row.panel_id)?.observed_at || '');
      if (!Number.isFinite(observedAt) || (Number.isFinite(existingAt) && observedAt < existingAt)) continue;
      latest.set(row.panel_id, {
        ...row,
        source_round_id: sourceRound?.round_id == null ? null : String(sourceRound.round_id),
        source_round_index: Number.isSafeInteger(Number(sourceRound?.round_index))
          ? Number(sourceRound.round_index) : null,
      });
    }
    this._metrics.panelObservations = ['PANEL_1', 'PANEL_2']
      .map(panelId => latest.get(panelId)).filter(Boolean);
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
    try {
      if (this._metrics.collectorRunning) this._metrics.heartbeatAt = new Date().toISOString();
      const snap = this.snapshot();
      fs.mkdirSync(path.dirname(STATUS_PATH), { recursive: true });
      const tmp = STATUS_PATH + '.tmp';
      fs.writeFileSync(tmp, JSON.stringify(snap, null, 2), 'utf-8');
      fs.renameSync(tmp, STATUS_PATH);
    } catch (error) {
      if (Date.now() - this._lastFlushErrorAt > 60000) {
        this._lastFlushErrorAt = Date.now();
        log.error(`Collector heartbeat write failed: ${error?.message || error}`);
      }
    }
  }
}
