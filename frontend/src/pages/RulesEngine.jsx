import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Activity, Database, ListChecks, ShieldCheck } from 'lucide-react';
import { api } from '../services/api.js';
import { statusRequestError } from '../services/statusRequestError.js';
import { getWebSocketAuthProtocols, getWebSocketUrl } from '../auth.js';
import LiveActivation from './LiveActivation.jsx';
import RoundPerformanceDashboard from '../components/RoundPerformanceDashboard.jsx';

const MODE_LABELS = {
  START_CHECK: 'Start a Check',
  LEGACY_SEPARATE_TRACKER: 'Legacy Separate Tracker',
  FIVE_RULE_TRACKER: 'Five Rules · Shared SELECT',
  ALL_RULES_TOGETHER: 'All Rules Together',
};
const RULE_MANAGER_DRAFT_KEY = 'winner-predict.rules-manager.draft.v1';
const RULE_MANAGER_WRITE_TIMEOUT_MS = 45000;
const SESSION_DELETE_BATCH_SIZE = 1000;
const MANAGER_INPUT_CLASS = 'mt-1 block h-10 w-full rounded-lg border border-zinc-700 bg-zinc-950 px-3 text-sm text-zinc-100 outline-none transition focus:border-emerald-400 focus:ring-2 focus:ring-emerald-400/20 disabled:opacity-50';
const DELETABLE_SESSION_STATUSES = new Set(['COMPLETED', 'SETTLING', 'PAUSED', 'MANUALLY_STOPPED', 'RESET', 'RECONFIGURED']);
const SESSION_DELETE_ERROR_MESSAGES = {
  SESSION_NOT_FOUND: 'This session is already gone or no longer available. Refresh the saved session list.',
  SESSION_NOT_FINISHED: 'The backend still sees this session as active. Refresh its status and retry after the backend has loaded the current Rules Engine code.',
  SESSION_AUTO_NEXT_ARMED: 'Automatic next check is armed. Disarm it before deleting this record.',
  SESSION_HAS_PENDING_EVENTS: 'Round events are still being saved. Refresh and try again after persistence completes.',
  SESSION_HAS_OPEN_EXECUTIONS: 'An execution is still open and must be reconciled before this record can be removed.',
  SESSION_HAS_EXECUTION_EVIDENCE: 'Verified execution evidence is attached to this session, so its record must be retained.',
  SESSION_HAS_RECONCILED_OUTCOMES: 'A platform settlement is attached to this session, so its record must be retained.',
  SESSION_HAS_RECONCILED_FINANCIALS: 'Verified financial data is attached to this session, so its record must be retained.',
  SESSION_DELETE_PERSISTENCE_UNAVAILABLE: 'Session storage is unavailable. The record was not deleted.',
};
const RESTARTABLE_INTEGRITY_STOPS = new Set([
  'ROUND_INDEX_GAP', 'ROUND_IDENTITY_CONFLICT',
  'OBSERVED_COMMIT_IDENTITY_CONFLICT', 'STREAM_IDENTITY_CHANGED',
]);

function readRulesManagerDraft() {
  if (typeof window === 'undefined') return {};
  try {
    const draft = JSON.parse(window.localStorage.getItem(RULE_MANAGER_DRAFT_KEY) || '{}');
    return draft && typeof draft === 'object' && !Array.isArray(draft) ? draft : {};
  } catch {
    return {};
  }
}

function storedValue(value, fallback) {
  return typeof value === 'string' || typeof value === 'number' ? value : fallback;
}

function storedBoolean(value, fallback) {
  return typeof value === 'boolean' ? value : fallback;
}

function displayEvaluationStatus(status) {
  // Historical rows used this label before the observed-stage decision barrier.
  return status === 'INVALID_LATE' ? 'TARGET_ALREADY_COMPLETED' : status || 'UNKNOWN';
}

function sessionOptionsFromSettings({ mode, stake, winEnabled, winCount, lossEnabled, lossCount,
  maxSelections, maxObservedRounds, autoNext, autoNextOnLimit, profitEnabled, profitGoal, executionPanel }) {
  const options = mode === 'ALL_RULES_TOGETHER' ? {
    window_size: 50, max_observed_rounds: Number(maxObservedRounds), fixed_stake_rwf: Number(stake),
    win_limit_enabled: winEnabled, win_limit_count: winEnabled ? Number(winCount) : null,
    loss_limit_enabled: lossEnabled, loss_limit_count: lossEnabled ? Number(lossCount) : null,
    max_selections: maxSelections === '' ? null : Number(maxSelections),
    auto_next_check_enabled: autoNext,
    auto_next_on_limit_enabled: autoNextOnLimit,
  } : mode === 'START_CHECK' || mode === 'LEGACY_SEPARATE_TRACKER' ? {
    window_size: 50, max_observed_rounds: Number(maxObservedRounds), fixed_stake_rwf: Number(stake), cashout_multiplier: 2.0,
    profit_target_enabled: profitEnabled, profit_target_rwf: Number(profitGoal),
    auto_next_check_enabled: autoNext,
    auto_next_on_limit_enabled: autoNextOnLimit,
  } : {
    window_size: 50, max_observed_rounds: Number(maxObservedRounds), fixed_stake_rwf: Number(stake),
  };
  return { ...options, ...([0, 1].includes(executionPanel) ? { execution_panel: executionPanel } : {}) };
}

function canDeleteFinishedSession(session) {
  return DELETABLE_SESSION_STATUSES.has(session?.status)
    && session?.auto_next_armed !== true;
}

function storedSelections(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return {};
  return Object.fromEntries(Object.entries(value)
    .filter(([mode, ids]) => Object.hasOwn(MODE_LABELS, mode) && Array.isArray(ids))
    .map(([mode, ids]) => [mode, [...new Set(ids.filter((id) => typeof id === 'string'))]]));
}

function Metric({ label, value, detail, icon: Icon = Activity, valueClassName = 'text-zinc-100', className = '', accentClassName = 'text-emerald-300', detailClassName = 'text-zinc-500' }) {
  return <article className={`rounded-xl border border-zinc-800 bg-zinc-900/70 p-4 ${className}`}>
    <div className="flex items-center gap-2 text-xs font-semibold uppercase tracking-wide text-zinc-400">
      <Icon size={15} className={accentClassName} />{label}
    </div>
    <div className={`mt-3 text-lg font-semibold ${valueClassName}`}>{value}</div>
    {detail && <div className={`mt-1 text-xs ${detailClassName}`}>{detail}</div>}
  </article>;
}

function Button({ children, onClick, disabled = false, tone = 'neutral' }) {
  const colors = {
    danger: 'border-rose-500/40 bg-rose-500/10 text-rose-200 hover:border-rose-400/70 hover:bg-rose-500/20 focus-visible:ring-rose-400/50',
    emergency: 'border-red-400/60 bg-red-500/20 text-red-100 shadow-sm shadow-red-950/40 hover:border-red-300 hover:bg-red-500/30 focus-visible:ring-red-300/60',
    good: 'border-emerald-400/40 bg-emerald-400/10 text-emerald-100 shadow-sm shadow-emerald-950/30 hover:border-emerald-300/70 hover:bg-emerald-400/20 focus-visible:ring-emerald-300/50',
    info: 'border-sky-400/40 bg-sky-400/10 text-sky-100 hover:border-sky-300/70 hover:bg-sky-400/20 focus-visible:ring-sky-300/50',
    warning: 'border-amber-400/40 bg-amber-400/10 text-amber-100 hover:border-amber-300/70 hover:bg-amber-400/20 focus-visible:ring-amber-300/50',
    reset: 'border-violet-400/40 bg-violet-400/10 text-violet-100 hover:border-violet-300/70 hover:bg-violet-400/20 focus-visible:ring-violet-300/50',
    neutral: 'border-zinc-700 bg-zinc-800/40 text-zinc-200 hover:border-zinc-500 hover:bg-zinc-800 focus-visible:ring-zinc-400/50',
  }[tone] || 'border-zinc-700 bg-zinc-800/40 text-zinc-200 hover:border-zinc-500 hover:bg-zinc-800 focus-visible:ring-zinc-400/50';
  return <button type="button" onClick={onClick} disabled={disabled}
    className={`rounded-lg border px-3 py-2 text-sm font-semibold transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-offset-2 focus-visible:ring-offset-zinc-950 disabled:cursor-not-allowed disabled:opacity-40 ${colors}`}>
    {children}
  </button>;
}

export default function RulesEngine() {
  const [initialDraft] = useState(readRulesManagerDraft);
  const [status, setStatus] = useState(null);
  const [dashboard, setDashboard] = useState(null);
  const [showDashboardDetails, setShowDashboardDetails] = useState(false);
  const [registry, setRegistry] = useState(null);
  const [sessions, setSessions] = useState([]);
  const [selectedFinishedSessionIds, setSelectedFinishedSessionIds] = useState(() => new Set());
  const [detailSession, setDetailSession] = useState(null);
  const [historyDetail, setHistoryDetail] = useState(null);
  const refreshSequence = useRef(0);
  const refreshInFlight = useRef(null);
  const auxiliaryRefreshAt = useRef(0);
  const sessionDetailRefresh = useRef({ sessionId: null, at: 0 });
  const [mode, setMode] = useState(() => Object.hasOwn(MODE_LABELS, initialDraft.mode) ? initialDraft.mode : 'ALL_RULES_TOGETHER');
  const [selectedIdsByMode, setSelectedIdsByMode] = useState(() => storedSelections(initialDraft.selected_ids_by_mode));
  const selectedIds = Object.hasOwn(selectedIdsByMode, mode)
    ? selectedIdsByMode[mode]
    : sessions.find((session) => session.mode === mode && Array.isArray(session.rule_ids))?.rule_ids || [];
  const [error, setError] = useState('');
  const [loadError, setLoadError] = useState('');
  const [busy, setBusy] = useState(false);
  const [busyAction, setBusyAction] = useState('');
  const [presetName, setPresetName] = useState('');
  const [presetChoice, setPresetChoice] = useState('');
  const [stake, setStake] = useState(() => storedValue(initialDraft.stake, 100));
  const [executionPanel, setExecutionPanel] = useState(() => [0, 1].includes(initialDraft.execution_panel)
    ? initialDraft.execution_panel : null);
  const [winEnabled, setWinEnabled] = useState(() => storedBoolean(initialDraft.win_enabled, false));
  const [winCount, setWinCount] = useState(() => storedValue(initialDraft.win_count, 1));
  const [lossEnabled, setLossEnabled] = useState(() => storedBoolean(initialDraft.loss_enabled, false));
  const [lossCount, setLossCount] = useState(() => storedValue(initialDraft.loss_count, 2));
  const [maxSelections, setMaxSelections] = useState(() => storedValue(initialDraft.max_selections, ''));
  const [maxObservedRounds, setMaxObservedRounds] = useState(() => storedValue(initialDraft.max_observed_rounds, 50));
  const [autoNext, setAutoNext] = useState(() => storedBoolean(initialDraft.auto_next, false));
  const [autoNextOnLimit, setAutoNextOnLimit] = useState(() =>
    !storedBoolean(initialDraft.auto_next, false) && storedBoolean(initialDraft.auto_next_on_limit, false));
  const [profitEnabled, setProfitEnabled] = useState(() => storedBoolean(initialDraft.profit_enabled, true));
  const [profitGoal, setProfitGoal] = useState(() => storedValue(initialDraft.profit_goal, 400));
  const [draftSaveState, setDraftSaveState] = useState('saved');

  const updateSelectedIds = (update) => setSelectedIdsByMode((previous) => {
    const currentIds = Object.hasOwn(previous, mode) ? previous[mode] : selectedIds;
    const nextIds = typeof update === 'function' ? update(currentIds) : update;
    return { ...previous, [mode]: [...new Set(nextIds)] };
  });

  useEffect(() => {
    try {
      window.localStorage.setItem(RULE_MANAGER_DRAFT_KEY, JSON.stringify({
        mode,
        selected_ids_by_mode: selectedIdsByMode,
        stake,
        execution_panel: executionPanel,
        win_enabled: winEnabled,
        win_count: winCount,
        loss_enabled: lossEnabled,
        loss_count: lossCount,
        max_selections: maxSelections,
        max_observed_rounds: maxObservedRounds,
        auto_next: autoNext,
        auto_next_on_limit: autoNextOnLimit,
        profit_enabled: profitEnabled,
        profit_goal: profitGoal,
      }));
      setDraftSaveState('saved');
    } catch {
      setDraftSaveState('unavailable');
    }
  }, [mode, selectedIdsByMode, stake, executionPanel, winEnabled, winCount, lossEnabled,
    lossCount, maxSelections, maxObservedRounds, autoNext, autoNextOnLimit, profitEnabled, profitGoal]);

  const refresh = useCallback((force = false) => {
    if (refreshInFlight.current) {
      const inFlight = refreshInFlight.current;
      return force ? inFlight.then(() => refresh(true)) : inFlight;
    }
    const requestId = ++refreshSequence.current;
    const now = Date.now();
    const refreshAuxiliary = force || !auxiliaryRefreshAt.current
      || now - auxiliaryRefreshAt.current >= 60000;
    let request;
    request = (async () => {
      try {
        const jobs = [api.get('/api/rules-engine/status'), api.get('/api/rules-engine/dashboard')];
        if (refreshAuxiliary) {
          jobs.push(api.get('/api/rules-engine/registry'), api.get('/api/rules-engine/sessions'));
        }
        const results = await Promise.allSettled(jobs);
        const [statusResult, dashboardResult] = results;
        const registryResult = refreshAuxiliary ? results[2] : null;
        const sessionsResult = refreshAuxiliary ? results[3] : null;
        if (requestId !== refreshSequence.current) return;
        const failures = [];
        const statusData = statusResult.status === 'fulfilled' ? statusResult.value.data : null;
        const sessionsData = sessionsResult?.status === 'fulfilled' ? sessionsResult.value.data : null;
        if (statusData) {
          setStatus(statusData);
          if (statusData.active_session) {
            setExecutionPanel(Number.isInteger(statusData.active_session.execution_panel)
              && [0, 1].includes(statusData.active_session.execution_panel)
              ? statusData.active_session.execution_panel : null);
          }
        } else failures.push(statusRequestError(statusResult.reason, 'Rules Engine status'));
        if (registryResult?.status === 'fulfilled') setRegistry(registryResult.value.data);
        else if (registryResult) failures.push(statusRequestError(registryResult.reason, 'Rules registry'));
        if (sessionsData) setSessions(sessionsData.sessions || []);
        else if (sessionsResult) failures.push(statusRequestError(sessionsResult.reason, 'Rules Engine sessions'));
        if (dashboardResult.status === 'fulfilled') setDashboard(dashboardResult.value.data);
        else setDashboard(null);

        const selectedSession = statusData?.active_session || statusData?.latest_session
          || sessionsData?.sessions?.[0];
        const detailRefresh = sessionDetailRefresh.current;
        const shouldRefreshDetail = selectedSession?.session_id
          && (detailRefresh.sessionId !== selectedSession.session_id
            || force || Date.now() - detailRefresh.at >= 30000);
        if (shouldRefreshDetail) {
          try {
            const detail = await api.get(`/api/rules-engine/sessions/${encodeURIComponent(selectedSession.session_id)}`);
            if (requestId !== refreshSequence.current) return;
            setDetailSession(detail.data);
            sessionDetailRefresh.current = { sessionId: selectedSession.session_id, at: Date.now() };
          } catch (failure) {
            failures.push(statusRequestError(failure, 'Selected Rules Engine session'));
          }
        } else if (!selectedSession?.session_id && statusData && sessionsData) setDetailSession(null);

        if (refreshAuxiliary) auxiliaryRefreshAt.current = Date.now();

        if (requestId === refreshSequence.current) {
          setLoadError(failures.join(' '));
        }
      } catch (failure) {
        if (requestId === refreshSequence.current) {
          setLoadError(statusRequestError(failure, 'Rules Engine status'));
        }
      } finally {
        if (refreshInFlight.current === request) refreshInFlight.current = null;
      }
    })();
    refreshInFlight.current = request;
    return request;
  }, []);

  useEffect(() => {
    refresh();
    const timer = window.setInterval(refresh, 15000);
    return () => window.clearInterval(timer);
  }, [refresh]);

  useEffect(() => {
    let closed = false;
    let attempts = 0;
    let retryTimer = null;
    let refreshTimer = null;
    let socket;
    const scheduleRefresh = () => {
      if (refreshTimer) window.clearTimeout(refreshTimer);
      refreshTimer = window.setTimeout(() => {
        refreshTimer = null;
        void refresh();
      }, 300);
    };
    const connect = () => {
      if (closed) return;
      socket = new WebSocket(getWebSocketUrl(), getWebSocketAuthProtocols());
      socket.onopen = () => { attempts = 0; };
      socket.onmessage = (message) => {
        if (closed) return;
        try {
          if (JSON.parse(message.data)?.type === 'rules-engine:evaluated') scheduleRefresh();
        } catch { /* Ignore non-JSON socket frames. */ }
      };
      socket.onerror = () => {};
      socket.onclose = () => {
        if (closed) return;
        const delay = Math.min(1000 * (2 ** attempts), 15000);
        attempts += 1;
        retryTimer = window.setTimeout(connect, delay);
      };
    };
    connect();
    return () => {
      closed = true;
      if (retryTimer) window.clearTimeout(retryTimer);
      if (refreshTimer) window.clearTimeout(refreshTimer);
      if (socket && socket.readyState < WebSocket.CLOSING) socket.close();
    };
  }, [refresh]);

  const modeDefinition = useMemo(() => registry?.modes?.find((item) => item.id === mode), [registry, mode]);
  const availableRules = useMemo(() => {
    const ids = new Set(modeDefinition?.rule_ids || []);
    return (registry?.rules || []).filter((rule) => ids.has(rule.rule_id));
  }, [modeDefinition, registry]);
  const presets = useMemo(() => Object.values(status?.presets || {}), [status]);
  const activeSession = status?.active_session;
  const pausedSession = sessions.find((session) => session.status === 'PAUSED')
    || (status?.latest_session?.status === 'PAUSED' ? status.latest_session : null);
  const controlSession = activeSession || pausedSession;
  const restartableSession = sessions.find((session) => session.mode === mode
    && session.status === 'PAUSED' && session.selection_status === 'ACTIVE'
    && RESTARTABLE_INTEGRITY_STOPS.has(session.stop_reason))
    || (status?.latest_session?.mode === mode && status.latest_session.status === 'PAUSED'
      && status.latest_session.selection_status === 'ACTIVE'
      && RESTARTABLE_INTEGRITY_STOPS.has(status.latest_session.stop_reason)
      ? status.latest_session : null);
  const displaySession = controlSession || status?.latest_session || sessions[0] || null;
  const displayedSessionConfig = detailSession && displaySession
    && detailSession.session_id === displaySession.session_id
    ? detailSession.configuration_snapshot || {} : {};
  const autoNextCheckEnabled = displaySession?.auto_next_check_enabled
    ?? displayedSessionConfig.auto_next_check_enabled;
  const autoNextOnLimitEnabled = displaySession?.auto_next_on_limit_enabled
    ?? displayedSessionConfig.auto_next_on_limit_enabled;
  const autoNextCheckIsOn = autoNextCheckEnabled === true || autoNextOnLimitEnabled === true;
  const autoNextCheckIsOff = autoNextCheckEnabled === false && autoNextOnLimitEnabled === false;
  const autoNextWindowRounds = displaySession?.window_size
    ?? displayedSessionConfig.window_size ?? 50;
  const autoNextTriggerLabel = autoNextCheckEnabled === true && autoNextOnLimitEnabled === true
    ? 'Multiple triggers'
    : autoNextCheckEnabled === true ? `Full ${autoNextWindowRounds}-round window`
      : autoNextOnLimitEnabled === true ? 'Configured limit'
        : autoNextCheckIsOff ? 'Off' : 'Unknown';
  const current = status?.latest_real_round;
  const receivedRound = detailSession?.last_received_round || displaySession?.last_received_round;
  const lastSettledTarget = detailSession?.last_settled_target || displaySession?.last_settled_target;
  const eventLatencyMetrics = status?.event_latency_metrics?.stages_ms || {};
  const settlementLatencySummary = eventLatencyMetrics.target_settlement_durable_ms || {};
  const collector = status?.collector_status || {};
  const shadowReadiness = status?.shadow_readiness;
  const collectorState = collector.state || collector.health || collector.status || 'UNKNOWN';
  const pendingRoundDeliveries = Number(collector.storage?.round_delivery?.backlog_size) || 0;
  const deliveryFailure = collector.storage?.round_delivery?.last_failure;
  const backendStatus = String(collector.backend?.status || 'UNKNOWN').toUpperCase();
  const roundDeliveryStatus = [
    pendingRoundDeliveries > 0
      ? `${pendingRoundDeliveries} round observations are queued for ordered delivery.` : '',
    backendStatus !== 'AVAILABLE' ? `Collector reports backend health ${backendStatus}.` : '',
    collector.auth_required ? 'The collector is waiting for platform authentication.' : '',
    deliveryFailure?.category
      ? `Last delivery failure: ${deliveryFailure.category}${deliveryFailure.stage ? ` during ${deliveryFailure.stage}` : ''}.`
      : '',
  ].filter(Boolean).join(' ');
  const staleFeedDetail = [
    roundDeliveryStatus,
    pendingRoundDeliveries > 0
      ? ''
      : 'No round observations are queued; the feed needs a fresh verified round.',
    'The button will enable automatically after a fresh verified round is committed.',
  ].filter(Boolean).join(' ');
  const committedBoundary = shadowReadiness?.committed_boundary || {};
  const verifiedBoundaryDetail = shadowReadiness?.blockers?.includes('VERIFIED_COMMITTED_ROUND_REQUIRED')
    ? [
      `Latest boundary${committedBoundary.round_index != null ? ` #${committedBoundary.round_index}` : ''}: ${committedBoundary.event_type || 'UNKNOWN'} / ${committedBoundary.durability || 'UNKNOWN'} / ${committedBoundary.continuity_status || 'UNKNOWN'}.`,
      'Start requires a round.persisted event with COMMITTED durability and VERIFIED continuity.',
      roundDeliveryStatus,
      'The button will enable automatically when that boundary is available.',
    ].filter(Boolean).join(' ')
    : '';
  const blockerHintDetail = [
    ...(shadowReadiness?.blockers?.includes('SHADOW_ROUND_FEED_STALE') ? [staleFeedDetail] : []),
    ...(verifiedBoundaryDetail ? [verifiedBoundaryDetail] : []),
  ].join(' ');
  const hasSelectedEnabledRule = selectedIds.some((id) =>
    availableRules.find((rule) => rule.rule_id === id)?.enabled);
  const startHint = activeSession
    ? 'A SHADOW session is already active. Use “Restart with these settings” to apply this configuration, or stop the session from its panel.'
    : restartableSession
      ? restartableSession.stop_reason === 'ROUND_INDEX_GAP'
        ? 'This session will automatically skip the discontinuous round, reset stale rule history, and continue on the next fresh verified round. Pending shadow targets can still settle.'
        : !shadowReadiness || shadowReadiness.ready === false
        ? `The previous session paused after ${restartableSession.stop_reason} and cannot safely resume across the gap. A new session is blocked: ${[
          ...(!shadowReadiness ? ['READINESS_CHECK_PENDING'] : shadowReadiness.ready === false ? shadowReadiness.blockers || ['READINESS_UNAVAILABLE'] : []),
        ].join(', ')}.${blockerHintDetail ? ` ${blockerHintDetail}` : ''} Its history and pending outcomes will be preserved.`
        : `The previous session paused after ${restartableSession.stop_reason}. It cannot safely resume across a continuity gap. Start a new SHADOW session; its history and pending outcomes will be preserved.${hasSelectedEnabledRule ? '' : ' No enabled policies are selected, so this session will record no rule decisions.'}`
    : !shadowReadiness
      ? 'Checking SHADOW readiness…'
      : !shadowReadiness.ready
        ? `Cannot start yet: ${(shadowReadiness.blockers || []).join(', ') || 'readiness unavailable'}.${blockerHintDetail ? ` ${blockerHintDetail}` : ''}`
        : !hasSelectedEnabledRule
          ? 'No enabled policies are selected. The SHADOW session can start, but it will record no rule decisions.'
          : 'SHADOW readiness is clear. Starting will use the selected enabled rules.';
  const latestLabel = current
    ? `${Number(current.multiplier).toFixed(2)}x${current.round_index != null ? ` · #${current.round_index}` : ''}`
    : 'NONE';

  const withBusy = async (operation, action = 'request') => {
    setError('');
    setBusy(true);
    setBusyAction(action);
    try {
      await operation();
      await refresh(true);
    } catch (failure) {
      const detail = failure?.response?.data?.detail;
      const backendMessage = typeof detail === 'string' ? detail
        : detail?.reason || detail?.message || failure?.response?.data?.error
        || 'The Rules Engine request failed.';
      const message = backendMessage === 'SESSION_NOT_RESTARTABLE'
        ? 'The paused session changed before it could be replaced. Its history and pending outcomes are still saved; refresh the session list and try again.'
        : backendMessage === 'SESSION_ALREADY_ACTIVE_FOR_MODE'
          ? 'A SHADOW session for this mode is already running or settling. Check the current session panel before starting another.'
          : backendMessage === 'SESSION_REQUIRES_RECONCILIATION'
            ? 'This paused session still has round or execution records awaiting reconciliation. No replacement was started, and its history remains saved.'
            : backendMessage === 'SESSION_RECONCILIATION_STATUS_UNAVAILABLE'
              ? 'The paused session’s reconciliation status could not be checked. No replacement was started; try again when reconciliation is available.'
              : backendMessage;
      // A start request can be rejected because the verified boundary moved
      // after the last poll, or can time out after the server committed it.
      // Refresh the authoritative status so the button cannot stay enabled
      // against stale readiness or allow a duplicate start against stale state.
      if (action === 'start-shadow') {
        try {
          const latestStatus = await api.get('/api/rules-engine/status');
          setStatus(latestStatus.data);
          if (latestStatus.data?.active_session) {
            setExecutionPanel(Number.isInteger(latestStatus.data.active_session.execution_panel)
              && [0, 1].includes(latestStatus.data.active_session.execution_panel)
              ? latestStatus.data.active_session.execution_panel : null);
          }
        } catch { /* The regular status poll will retry if the backend is offline. */ }
      }
      setError(message);
    } finally {
      setBusy(false);
      setBusyAction('');
    }
  };

  const toggleRule = (rule, enabled) => withBusy(async () => {
    const parameters = rule.rule_id === 'USER_PLAYING_POLICY_START_CHECK'
      || rule.rule_id === 'USER_PLAYING_POLICY_LEGACY_TRACKER'
      ? { profit_target_enabled: profitEnabled, profit_target_rwf: Number(profitGoal),
          window_size: 50, auto_next_check_enabled: autoNext,
          auto_next_on_limit_enabled: autoNextOnLimit }
      : {};
    await api.put(`/api/rules-engine/configuration/${encodeURIComponent(rule.rule_id)}`,
      { enabled, parameters }, { timeout: RULE_MANAGER_WRITE_TIMEOUT_MS });
    // Enabling a rule makes it available, but does not opt it into the next
    // session. Participation is controlled only by its separate checkbox.
    if (!enabled) setSelectedIdsByMode((previous) => {
      const next = Object.fromEntries(Object.entries(previous).map(([selectedMode, ids]) =>
        [selectedMode, ids.filter((id) => id !== rule.rule_id)]));
      if (!Object.hasOwn(previous, mode)) next[mode] = selectedIds.filter((id) => id !== rule.rule_id);
      return next;
    });
  });

  const applyPreset = (name) => {
    setPresetChoice(name);
    const preset = presets.find((item) => item.name === name);
    if (!preset) return;
    setMode(preset.mode);
    setSelectedIdsByMode((previous) => ({ ...previous, [preset.mode]: preset.rule_ids || [] }));
    const options = preset.options || {};
    setStake(options.fixed_stake_rwf ?? 100);
    setExecutionPanel(Number.isInteger(options.execution_panel) && [0, 1].includes(options.execution_panel)
      ? options.execution_panel : null);
    setWinEnabled(Boolean(options.win_limit_enabled));
    setWinCount(options.win_limit_count ?? 1);
    setLossEnabled(Boolean(options.loss_limit_enabled));
    setLossCount(options.loss_limit_count ?? 2);
    setMaxSelections(options.max_selections ?? '');
    setMaxObservedRounds(options.max_observed_rounds ?? 50);
    const presetAutoNext = Boolean(options.auto_next_check_enabled);
    setAutoNext(presetAutoNext);
    setAutoNextOnLimit(!presetAutoNext && Boolean(options.auto_next_on_limit_enabled));
    setProfitEnabled(options.profit_target_enabled ?? true);
    setProfitGoal(options.profit_target_rwf ?? 400);
    const disabled = (preset.rule_ids || []).filter((id) => !registry?.rules?.find((rule) => rule.rule_id === id)?.enabled);
    setError(disabled.length ? `Preset loaded. Enable its disabled rules before starting: ${disabled.join(', ')}` : '');
  };

  const start = () => {
    if (shadowReadiness?.ready !== true) {
      const blockers = shadowReadiness?.blockers || [];
      setError(shadowReadiness
        ? `SHADOW session was not started: ${blockers.join(', ') || 'readiness is unavailable'}.${blockerHintDetail ? ` ${blockerHintDetail}` : ''}`
        : 'SHADOW session was not started because readiness is still loading. Try again shortly.');
      return;
    }
    const replaceableSession = restartableSession;
    if (replaceableSession && !window.confirm(
      replaceableSession.stop_reason === 'ROUND_INDEX_GAP'
        ? 'The paused session can recover automatically on a fresh verified round. Start a separate session and archive it anyway? Its history and pending shadow outcomes will be preserved.'
        : `The previous session is paused after ${replaceableSession.stop_reason}. Start a new session and archive it? Its history and any pending shadow outcomes will be preserved as pending. The start will be blocked if uncommitted round events or unreconciled executions remain.`
    )) {
      setError('Start cancelled. The paused session and its pending outcomes were left unchanged.');
      return;
    }
    return withBusy(async () => {
      const participatingRuleIds = selectedIds.filter((id) => availableRules.find((rule) => rule.rule_id === id)?.enabled);
      const response = await api.post('/api/rules-engine/sessions', {
        mode, rule_ids: participatingRuleIds,
        ...(replaceableSession ? { replace_session_id: replaceableSession.session_id } : {}),
        options: sessionOptionsFromSettings({ mode, stake, winEnabled, winCount, lossEnabled, lossCount,
          maxSelections, maxObservedRounds, autoNext, autoNextOnLimit, profitEnabled, profitGoal, executionPanel }),
      }, { timeout: RULE_MANAGER_WRITE_TIMEOUT_MS });
      if (response.data?.status === 'RUNNING' && response.data?.session_id) {
        setStatus((previous) => ({ ...(previous || status || {}), status: 'RUNNING',
          active_session: response.data }));
      }
    }, 'start-shadow');
  };

  const reconfigureCurrentSession = () => {
    if (!controlSession?.session_id) return;
    const expectedConfirmation = `RECONFIGURE SESSION ${controlSession.session_id}`;
    const confirmation = window.prompt(
      `This will preserve the current session in history and start a replacement with the Session settings below. Type exactly: ${expectedConfirmation}`);
    if (confirmation !== expectedConfirmation) {
      if (confirmation !== null) setError('Settings were not applied. The confirmation text did not match.');
      return;
    }
    return withBusy(async () => {
      const participatingRuleIds = selectedIds.filter((id) => availableRules.find((rule) => rule.rule_id === id)?.enabled);
      await api.post(`/api/rules-engine/sessions/${encodeURIComponent(controlSession.session_id)}/reconfigure`, {
        confirmation, mode, rule_ids: participatingRuleIds,
        options: sessionOptionsFromSettings({ mode, stake, winEnabled, winCount, lossEnabled, lossCount,
          maxSelections, maxObservedRounds, autoNext, autoNextOnLimit, profitEnabled, profitGoal, executionPanel }),
      }, { timeout: RULE_MANAGER_WRITE_TIMEOUT_MS });
    }, 'reconfigure-shadow');
  };

  const sessionAction = (sessionId, action) => withBusy(() =>
    api.post(`/api/rules-engine/sessions/${encodeURIComponent(sessionId)}/${action}`, undefined,
      { timeout: RULE_MANAGER_WRITE_TIMEOUT_MS }), action);

  const viewFinishedSession = (sessionId) => withBusy(async () => {
    const response = await api.get(`/api/rules-engine/sessions/${encodeURIComponent(sessionId)}`);
    setHistoryDetail(response.data);
  }, 'view-finished-shadow');

  useEffect(() => {
    if (!historyDetail) return undefined;
    const previousOverflow = document.body.style.overflow;
    const closeOnEscape = (event) => {
      if (event.key === 'Escape') setHistoryDetail(null);
    };
    document.body.style.overflow = 'hidden';
    window.addEventListener('keydown', closeOnEscape);
    return () => {
      document.body.style.overflow = previousOverflow;
      window.removeEventListener('keydown', closeOnEscape);
    };
  }, [historyDetail?.session_id]);

  const deleteFinishedSessions = async (targetSessions) => {
    const uniqueSessions = [...new Map(targetSessions.map((session) => [session.session_id, session])).values()];
    if (!uniqueSessions.length || busy) return;
    const pending = uniqueSessions.reduce((total, session) => total + Number(session.results?.PENDING || 0), 0);
    const settling = uniqueSessions.some((session) => session.status === 'SETTLING');
    const accepted = window.confirm(
      `Delete ${uniqueSessions.length} stored SHADOW session${uniqueSessions.length === 1 ? '' : 's'}?${settling ? ' Settling sessions will stop observing immediately.' : ''}${pending > 0 ? ` This includes ${pending} pending simulated outcome${pending === 1 ? '' : 's'}; they will be removed without being settled.` : ''} Pending session events and simulated results will also be removed. Canonical rounds, platform balance observations, platform execution evidence, and audit records are retained.`,
    );
    if (!accepted) return;
    setBusy(true);
    setBusyAction('delete-finished-shadow');
    setError('');
    const deletedIds = [];
    const failures = [];
    try {
      const sessionById = new Map(uniqueSessions.map((session) => [session.session_id, session]));
      for (let offset = 0; offset < uniqueSessions.length; offset += SESSION_DELETE_BATCH_SIZE) {
        const batchSessions = uniqueSessions.slice(offset, offset + SESSION_DELETE_BATCH_SIZE);
        const batchSessionIds = new Set(batchSessions.map((session) => session.session_id));
        try {
          const response = await api.post('/api/rules-engine/sessions/delete-batch', {
            session_ids: batchSessions.map((session) => session.session_id),
          }, { timeout: RULE_MANAGER_WRITE_TIMEOUT_MS });
          const batchDeletedIds = new Set(
            (Array.isArray(response.data?.deleted_session_ids) ? response.data.deleted_session_ids : [])
              .filter((sessionId) => batchSessionIds.has(sessionId)),
          );
          deletedIds.push(...batchDeletedIds);
          const batchFailedIds = new Set();
          for (const failure of Array.isArray(response.data?.failures) ? response.data.failures : []) {
            const session = sessionById.get(failure?.session_id);
            if (session && batchSessionIds.has(session.session_id) && !batchDeletedIds.has(session.session_id)) {
              batchFailedIds.add(session.session_id);
              failures.push({ session, reason: failure.reason || 'DELETE_FAILED' });
            }
          }
          for (const session of batchSessions) {
            if (!batchDeletedIds.has(session.session_id) && !batchFailedIds.has(session.session_id)) {
              failures.push({ session, reason: 'DELETE_FAILED' });
            }
          }
        } catch (failure) {
          const detail = failure?.response?.data?.detail;
          const reason = typeof detail === 'string' ? detail
            : detail?.reason || detail?.message || detail?.error || detail?.code
              || failure?.response?.data?.error || 'DELETE_FAILED';
          failures.push(...batchSessions.map((session) => ({ session, reason })));
        }
      }
      if (deletedIds.length) {
        refreshSequence.current += 1;
        const deletedIdSet = new Set(deletedIds);
        setSessions((previous) => previous.filter((item) => !deletedIdSet.has(item.session_id)));
        setSelectedFinishedSessionIds((previous) => new Set([...previous].filter((id) => !deletedIdSet.has(id))));
        setHistoryDetail((previous) => previous && deletedIdSet.has(previous.session_id) ? null : previous);
      }
      await refresh(true);
      if (failures.length) {
        const first = failures[0];
        const reason = SESSION_DELETE_ERROR_MESSAGES[first.reason] || first.reason;
        setError(`${deletedIds.length} deleted; ${failures.length} could not be deleted. ${first.session.session_id}: ${reason}`);
      }
    } finally {
      setBusy(false);
      setBusyAction('');
    }
  };

  const deleteFinishedSession = (session) => deleteFinishedSessions([session]);

  const savePreset = () => withBusy(async () => {
    const options = { window_size: 50, max_observed_rounds: Number(maxObservedRounds),
      auto_next_check_enabled: autoNext, auto_next_on_limit_enabled: autoNextOnLimit,
      fixed_stake_rwf: Number(stake),
      ...([0, 1].includes(executionPanel) ? { execution_panel: executionPanel } : {}) };
    if (mode === 'ALL_RULES_TOGETHER') Object.assign(options, {
      fixed_stake_rwf: Number(stake), win_limit_enabled: winEnabled,
      win_limit_count: winEnabled ? Number(winCount) : null,
      loss_limit_enabled: lossEnabled, loss_limit_count: lossEnabled ? Number(lossCount) : null,
      max_selections: maxSelections === '' ? null : Number(maxSelections),
    });
    if (mode === 'START_CHECK' || mode === 'LEGACY_SEPARATE_TRACKER') Object.assign(options, {
      profit_target_enabled: profitEnabled, profit_target_rwf: Number(profitGoal),
    });
    await api.post('/api/rules-engine/presets', { name: presetName, mode, rule_ids: selectedIds,
      options }, { timeout: RULE_MANAGER_WRITE_TIMEOUT_MS });
    setPresetName('');
  });

  const recentEvaluations = detailSession?.evaluations?.slice(-12).reverse() || [];
  const allSelections = detailSession?.mode === 'ALL_RULES_TOGETHER'
    ? [...(detailSession.selections || [])].reverse()
    : Object.values(detailSession?.rule_selections || {}).flat().reverse();
  const finishedSessions = sessions
    .filter((session) => session?.session_id && session.status !== 'RUNNING')
    .sort((a, b) => {
      const aTime = Date.parse(a.ended_at || a.last_activity_at || a.started_at || '') || 0;
      const bTime = Date.parse(b.ended_at || b.last_activity_at || b.started_at || '') || 0;
      return bTime - aTime;
    });
  const deletableFinishedSessions = finishedSessions.filter(canDeleteFinishedSession);
  const selectedDeletableSessions = deletableFinishedSessions
    .filter((session) => selectedFinishedSessionIds.has(session.session_id));
  const allDeletableSelected = deletableFinishedSessions.length > 0
    && selectedDeletableSessions.length === deletableFinishedSessions.length;
  const historySelections = historyDetail?.mode === 'ALL_RULES_TOGETHER'
    ? [...(historyDetail.selections || [])].reverse()
    : Object.values(historyDetail?.rule_selections || {}).flat().reverse();
  const historyEvaluations = historyDetail?.evaluations || [];
  const historyRuleEvaluations = historyEvaluations.flatMap((evaluation) => evaluation.rule_evaluations || []);
  const historyEvaluationStats = historyRuleEvaluations.reduce((counts, evaluation) => {
    if (['TARGET_ALREADY_COMPLETED', 'INVALID_LATE'].includes(evaluation.status)) counts.targetAlreadyCompleted += 1;
    else if (evaluation.eligible === true) counts.matched += 1;
    else if (evaluation.eligible === false) counts.false += 1;
    else counts.unavailable += 1;
    return counts;
  }, { matched: 0, false: 0, targetAlreadyCompleted: 0, unavailable: 0 });
  const historyPausedRounds = Object.values(historyDetail?.event_records || {})
    .filter((record) => record.decision?.reason === 'MANUALLY_PAUSED').length;
  const historyStake = Number(historyDetail?.requested_stake
    ?? historyDetail?.configuration_snapshot?.requested_stake
    ?? historyDetail?.configuration_snapshot?.fixed_stake_rwf ?? 0);
  const historyCurrency = historyDetail?.requested_stake_currency
    || historyDetail?.configuration_snapshot?.stake_currency || 'RWF';
  const configuredWindowRounds = Math.max(1, Math.min(50,
    Number.parseInt(maxObservedRounds, 10) || 50));
  const autoNextLimitTargets = mode === 'ALL_RULES_TOGETHER'
    ? [
      winEnabled ? `WIN_LIMIT ${winCount || 'not set'}` : null,
      lossEnabled ? `LOSS_LIMIT ${lossCount || 'not set'}` : null,
      maxSelections !== '' ? `maximum ${maxSelections || 'not set'} selections` : null,
    ].filter(Boolean)
    : profitEnabled ? [`profit stop ${profitGoal || 'not set'} RWF`] : [];
  const autoNextLimitHelp = autoNextLimitTargets.length
    ? mode === 'ALL_RULES_TOGETHER'
      ? `Configured limits: ${autoNextLimitTargets.join(' · ')}. WIN/LOSS limits use settled outcomes; the selection cap triggers on its final selection.`
      : `Configured limit: ${autoNextLimitTargets[0]}. It triggers after the target settles.`
    : 'No applicable limit is enabled, so this option cannot start a next check yet.';

  return <main className="space-y-6">
    <header className="flex flex-wrap items-end justify-between gap-4">
      <div>
        <div className="text-xs font-bold uppercase tracking-[0.18em] text-emerald-300">Shadow decision interface</div>
        <h1 className="mt-2 text-2xl font-bold text-zinc-100">Rules Engine</h1>
        <p className="mt-2 max-w-3xl text-sm text-zinc-400">
          Independent rule evaluations use verified, completed history. The coordinator records selections only.
        </p>
      </div>
    </header>

    <section aria-label="LIVE REAL session controls">
      <LiveActivation dashboard={dashboard} />
    </section>

    <RoundPerformanceDashboard />

    {loadError && <div role="status" className="rounded-lg border border-amber-500/30 bg-amber-500/10 px-4 py-3 text-sm text-amber-200">{loadError}</div>}
    {error && <div role="alert" className="flex items-start justify-between gap-3 rounded-lg border border-rose-500/30 bg-rose-500/10 px-4 py-3 text-sm text-rose-200">
      <span>{error}</span>
      <button type="button" onClick={() => setError('')} className="shrink-0 underline underline-offset-2">Dismiss</button>
    </div>}

    <section className={`relative overflow-hidden rounded-xl border p-5 shadow-lg shadow-black/10 ${
      controlSession?.status === 'PAUSED'
        ? 'border-amber-400/40 bg-gradient-to-br from-amber-950/40 via-zinc-900/90 to-zinc-900/70 shadow-amber-950/20'
        : controlSession?.status === 'RUNNING'
          ? 'border-emerald-400/35 bg-gradient-to-br from-emerald-950/30 via-zinc-900/90 to-zinc-900/70 shadow-emerald-950/20'
          : displaySession?.integrity_stop_reason
            ? 'border-rose-400/35 bg-gradient-to-br from-rose-950/30 via-zinc-900/90 to-zinc-900/70 shadow-rose-950/20'
            : 'border-zinc-800 bg-zinc-900/60'
    }`}>
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div><h2 className="text-lg font-semibold">Rules Betting dashboard</h2>
          <p className="mt-1 text-sm text-zinc-400">Each enabled rule evaluates independently. Overlapping TRUE results produce one shared selection per target.</p>
        </div>
        <button type="button" aria-expanded={showDashboardDetails} aria-controls="rules-dashboard-details"
          onClick={() => setShowDashboardDetails((visible) => !visible)}
          className="rounded-lg border border-zinc-700 px-3 py-2 text-sm font-medium text-zinc-200 hover:bg-zinc-800">
          {showDashboardDetails ? 'Hide details' : 'View details'}
        </button>
      </div>
      <div className="mt-4 grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
        <Metric label="Selected rules" value={dashboard?.selected_rules?.length || 0}
          detail={dashboard?.selected_rules?.join(', ') || 'No active rule session'} icon={ListChecks} />
        <Metric label="Current round" value={current?.round_index ?? '—'} detail={current?.round_id || 'No persisted round'} icon={Database} />
        <Metric label="Shared decision" value={dashboard?.shared_decision?.action || 'NOT_EVALUATED'}
          detail={dashboard?.shared_decision?.true_rule_ids?.join(', ')
            || dashboard?.shared_decision?.reason || 'No committed rule evaluation yet'} icon={ShieldCheck} />
        <Metric label="SHADOW startup readiness" value={shadowReadiness?.status || 'UNKNOWN'}
          detail={shadowReadiness?.blockers?.join(', ') || 'Requires a current collector feed, committed PostgreSQL boundary, and enabled Rules Engine configuration'} icon={Activity} />
      </div>
      <div id="rules-dashboard-details" hidden={!showDashboardDetails}>
        <article className="mt-3 rounded-lg border border-zinc-800 bg-zinc-950/50 p-4">
          <h3 className="text-sm font-semibold">Latest independent rule results</h3>
          <div className="mt-3 space-y-2 text-xs">
            {(dashboard?.rule_evaluations || []).map((row) => <div key={row.rule_id} className="flex flex-wrap justify-between gap-2 border-b border-zinc-800 pb-2">
              <code>{row.rule_id}</code><span className={row.eligible === true ? 'text-emerald-300' : row.eligible === false ? 'text-zinc-300' : 'text-amber-200'}>{row.eligible === true ? 'TRUE' : row.eligible === false ? 'FALSE' : row.status || 'UNAVAILABLE'}</span>
            </div>)}
            {!dashboard?.rule_evaluations?.length && <p className="text-zinc-500">No committed evaluation is available.</p>}
          </div>
        </article>
      </div>
    </section>

    <section className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
      <Metric label="Collector" value={String(collectorState).replaceAll('_', ' ')} />
      <Metric label="Latest real round" value={latestLabel} detail={current?.round_id || 'No persisted round'} icon={Database} />
      <Metric label="Total history" value={status?.total_history ?? '—'} icon={Database} />
      <Metric label="Rules configured" value={status?.rules_configured ?? 0} icon={ListChecks} />
      <Metric label="Rules enabled" value={status?.rules_enabled ?? 0} icon={ListChecks} />
      <Metric label="Last rule evaluation" value={status?.last_rule_evaluation || 'NONE'} />
      <Metric label={activeSession?.mode === 'FIVE_RULE_TRACKER' ? 'Independent tracker decision' : 'Session decision'} value={status?.decision || 'NO_ACTION'} />
    </section>

    <section className="overflow-hidden rounded-2xl border border-emerald-500/25 bg-gradient-to-br from-emerald-950/25 via-zinc-900/80 to-sky-950/20 p-4 shadow-lg shadow-black/20 sm:p-6">
      <div className="flex items-start gap-3 border-b border-emerald-500/15 pb-4">
        <span className="flex h-10 w-10 shrink-0 items-center justify-center rounded-xl bg-emerald-400/10 text-emerald-300 ring-1 ring-emerald-300/20">
          <ListChecks size={19} />
        </span>
        <div className="min-w-0">
          <div className="text-[11px] font-bold uppercase tracking-[0.18em] text-emerald-300">Session configuration</div>
          <h2 className="mt-1 text-xl font-bold text-zinc-50">Rules Manager</h2>
          <p className="mt-1 max-w-3xl text-sm leading-6 text-zinc-300">Choose a mode and participating policies, then start a versioned SHADOW session.</p>
        </div>
      </div>

      <div className="mt-5 grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
        <label className="block text-xs font-semibold uppercase tracking-wide text-sky-200">Session mode
          <select value={mode} onChange={(event) => setMode(event.target.value)}
            className={`${MANAGER_INPUT_CLASS} border-sky-500/30 bg-zinc-950/80 font-normal normal-case tracking-normal`}>
            {(registry?.modes || []).map((item) => <option key={item.id} value={item.id}>{MODE_LABELS[item.id] || item.id}</option>)}
          </select>
        </label>
        <label className="block text-xs font-semibold uppercase tracking-wide text-violet-200">Saved preset
          <select value={presetChoice} onChange={(event) => applyPreset(event.target.value)} disabled={!presets.length}
            className={`${MANAGER_INPUT_CLASS} border-violet-500/30 bg-zinc-950/80 font-normal normal-case tracking-normal`}>
            <option value="">Choose preset</option>
            {presets.map((item) => <option key={item.name} value={item.name}>{item.name} · {MODE_LABELS[item.mode] || item.mode}</option>)}
          </select>
        </label>
        <label className="block text-xs font-semibold uppercase tracking-wide text-amber-200">Maximum verified rounds
          <input type="number" min="1" max="50" value={maxObservedRounds}
            onChange={(e) => setMaxObservedRounds(e.target.value)} className={`${MANAGER_INPUT_CLASS} border-amber-500/25 bg-zinc-950/80`} />
          <span className="mt-1 block text-[11px] font-normal normal-case tracking-normal text-zinc-500">From 1 to 50 completed rounds.</span>
        </label>
      </div>

      <div className="mt-6 flex flex-wrap items-end justify-between gap-3 border-b border-zinc-700/70 pb-3">
        <div>
          <h3 className="text-sm font-semibold text-zinc-100">Registered rules</h3>
          <p className="mt-1 text-xs text-zinc-400">Enable a rule, then choose whether it participates in this session.</p>
        </div>
        <div className="flex flex-wrap gap-2 text-xs font-semibold">
          <span className="rounded-full border border-emerald-400/25 bg-emerald-400/10 px-3 py-1 text-emerald-200">{availableRules.filter((rule) => rule.enabled).length} enabled</span>
          <span className="rounded-full border border-sky-400/25 bg-sky-400/10 px-3 py-1 text-sky-200">{availableRules.filter((rule) => selectedIds.includes(rule.rule_id)).length} selected</span>
        </div>
      </div>

      <div className="mt-3 space-y-3">
        {availableRules.map((rule) => <div key={rule.rule_id}
          className={`grid gap-4 rounded-xl border border-l-4 p-4 transition-colors md:grid-cols-[minmax(13rem,0.7fr)_minmax(0,1.8fr)] ${rule.enabled && selectedIds.includes(rule.rule_id)
            ? 'border-emerald-500/30 border-l-emerald-400 bg-emerald-950/20'
            : rule.enabled ? 'border-zinc-700 border-l-sky-400 bg-zinc-950/55'
              : 'border-zinc-800 border-l-zinc-600 bg-zinc-950/30'}`}>
          <div className="flex flex-wrap items-center gap-2 md:content-center">
            <label className={`inline-flex min-h-10 items-center gap-2 rounded-lg border px-3 text-xs font-semibold ${rule.enabled
              ? 'border-emerald-400/25 bg-emerald-400/10 text-emerald-200'
              : 'border-zinc-700 bg-zinc-900 text-zinc-400'}`}>
              <input type="checkbox" checked={Boolean(rule.enabled)} disabled={busy}
                onChange={(event) => toggleRule(rule, event.target.checked)} className="accent-emerald-400" />Enabled</label>
            <label className={`inline-flex min-h-10 items-center gap-2 rounded-lg border px-3 text-xs font-semibold ${selectedIds.includes(rule.rule_id)
              ? 'border-sky-400/25 bg-sky-400/10 text-sky-200'
              : 'border-zinc-700 bg-zinc-900/70 text-zinc-400'} ${!rule.enabled ? 'opacity-50' : ''}`}>
              <input type="checkbox" checked={selectedIds.includes(rule.rule_id)} disabled={!rule.enabled || busy}
              onChange={(event) => updateSelectedIds((previous) => event.target.checked
                ? [...new Set([...previous, rule.rule_id])] : previous.filter((id) => id !== rule.rule_id))}
              className="accent-sky-400" />Participate</label>
          </div>
          <span className="min-w-0 flex-1">
            <span className="flex flex-wrap items-center gap-2 font-semibold text-zinc-100">
              {rule.name}<code className="rounded-md border border-emerald-400/15 bg-emerald-400/5 px-2 py-1 text-[11px] text-emerald-200">{rule.rule_id}</code>
              <span className="rounded-full bg-zinc-800 px-2 py-0.5 text-[10px] text-zinc-400">v{rule.version}</span>
            </span>
            <details className="mt-2 text-xs text-zinc-400">
              <summary className="cursor-pointer">Condition and state behavior</summary>
              {rule.condition?.h_loss_win_special_skip === true && <p className="mt-2 text-amber-200">
                Special sequence: H → bet LOSS → bet WIN starts one-round skips. After skipped L (≤2.00), bet next round and repeat after WIN. One special bet LOSS or skipped H (&gt;2.00) returns to WAIT_LOW. Applies only when enabled and participating.
              </p>}
              <pre className="mt-2 whitespace-pre-wrap text-zinc-500">{JSON.stringify(rule.condition, null, 2)}</pre>
              <p className="mt-2 text-zinc-400">{rule.transition_rules?.join(' ')}</p>
            </details>
          </span>
        </div>)}
        {!availableRules.length && <p className="text-sm text-zinc-500">No rules are registered for this mode.</p>}
      </div>

      <div className="mt-6 rounded-xl border border-sky-500/20 bg-sky-950/10 p-4 sm:p-5">
        <div className="mb-4 flex flex-wrap items-center justify-between gap-2 border-b border-sky-400/15 pb-3">
          <div>
            <h3 className="text-sm font-semibold text-sky-100">Session settings</h3>
            <p className="mt-1 text-xs text-zinc-400">These values are saved automatically in this browser. A running session keeps its original settings until you restart it with this configuration.</p>
          </div>
          <span className="rounded-full border border-sky-400/20 bg-sky-400/10 px-3 py-1 text-[11px] font-semibold text-sky-200">SHADOW only</span>
        </div>
      <div className="grid items-stretch gap-4 sm:grid-cols-2 xl:grid-cols-5">
        <label htmlFor="rules-manager-stake" className="flex min-w-0 flex-col text-sm text-zinc-300">
          <span className="flex min-h-10 items-end pb-1">Fixed simulated stake (RWF)</span>
          <input id="rules-manager-stake" type="number" min="1" value={stake}
            onChange={(e) => setStake(e.target.value)} className={MANAGER_INPUT_CLASS} />
        </label>
        <label htmlFor="rules-manager-panel" className="flex min-w-0 flex-col text-sm text-zinc-300">
          <span className="flex min-h-10 items-end pb-1">Optional SHADOW stake panel</span>
          <select aria-label="Optional SHADOW stake panel" value={executionPanel ?? ''}
            id="rules-manager-panel"
            onChange={(event) => setExecutionPanel(event.target.value === '' ? null : Number(event.target.value))}
            disabled={busy} className={MANAGER_INPUT_CLASS}>
            <option value="">Choose one panel</option>
            <option value={0}>Panel 1</option>
            <option value={1}>Panel 2</option>
          </select>
        </label>

        {(mode === 'START_CHECK' || mode === 'LEGACY_SEPARATE_TRACKER') && <div className="min-w-0">
          <div className="flex min-h-10 items-end justify-between gap-2 pb-1">
            <label htmlFor="rules-manager-profit-goal" className="text-sm text-zinc-300">Profit stop (RWF)</label>
            <label className="inline-flex items-center gap-1.5 rounded-full border border-emerald-500/20 bg-emerald-500/5 px-2 py-0.5 text-[11px] text-emerald-100">
              <input type="checkbox" checked={profitEnabled} onChange={(e) => setProfitEnabled(e.target.checked)} className="accent-emerald-400" />Enable
            </label>
          </div>
          <input id="rules-manager-profit-goal" type="number" min="1" value={profitGoal}
            onChange={(e) => setProfitGoal(e.target.value)} disabled={!profitEnabled}
            className={`${MANAGER_INPUT_CLASS} border-emerald-500/25`} />
        </div>}

        {mode === 'ALL_RULES_TOGETHER' && <>
          <div className="min-w-0">
            <div className="flex min-h-10 items-end justify-between gap-2 pb-1">
              <label htmlFor="rules-manager-win-limit" className="text-sm text-emerald-100">WIN_LIMIT</label>
              <label className="inline-flex items-center gap-1.5 rounded-full border border-emerald-500/20 bg-emerald-500/5 px-2 py-0.5 text-[11px] text-emerald-100">
                <input type="checkbox" checked={winEnabled} onChange={(e) => setWinEnabled(e.target.checked)} className="accent-emerald-400" />Enable
              </label>
            </div>
            <input id="rules-manager-win-limit" type="number" min="1" value={winCount} disabled={!winEnabled}
              onChange={(e) => setWinCount(e.target.value)} className={`${MANAGER_INPUT_CLASS} border-emerald-500/25`} />
          </div>
          <div className="min-w-0">
            <div className="flex min-h-10 items-end justify-between gap-2 pb-1">
              <label htmlFor="rules-manager-loss-limit" className="text-sm text-rose-100">LOSS_LIMIT</label>
              <label className="inline-flex items-center gap-1.5 rounded-full border border-rose-500/20 bg-rose-500/5 px-2 py-0.5 text-[11px] text-rose-100">
                <input type="checkbox" checked={lossEnabled} onChange={(e) => setLossEnabled(e.target.checked)} className="accent-rose-400" />Enable
              </label>
            </div>
            <input id="rules-manager-loss-limit" type="number" min="1" value={lossCount} disabled={!lossEnabled}
              onChange={(e) => setLossCount(e.target.value)} className={`${MANAGER_INPUT_CLASS} border-rose-500/25`} />
          </div>
          <label htmlFor="rules-manager-max-selections" className="flex min-w-0 flex-col text-sm text-zinc-300">
            <span className="flex min-h-10 items-end pb-1">Maximum combined selections</span>
            <input id="rules-manager-max-selections" type="number" min="1" value={maxSelections}
              onChange={(e) => setMaxSelections(e.target.value)} className={MANAGER_INPUT_CLASS} />
          </label>
        </>}
      </div>

      {mode === 'ALL_RULES_TOGETHER' && <p className="mt-3 text-xs text-zinc-500">WIN/Loss limits use settled net outcomes; pending selections are excluded.</p>}

      {(mode === 'START_CHECK' || mode === 'LEGACY_SEPARATE_TRACKER' || mode === 'ALL_RULES_TOGETHER') && <fieldset className="mt-4">
        <legend className="mb-2 text-xs font-semibold uppercase tracking-wide text-zinc-300">Automatic next check</legend>
        <p className="mb-3 text-xs text-zinc-400">Choose one trigger, or select Off to disable automatic next checks.</p>
        <p className="mb-3 text-xs leading-5 text-zinc-500">
          Card badges show the settings in this form. {controlSession
            ? `Current session trigger: ${autoNextTriggerLabel}. Use Restart with these settings to apply changes.`
            : 'These settings apply to the next session you start.'}
        </p>
        <label className="mb-3 inline-flex cursor-pointer items-center gap-2 rounded-lg border border-zinc-700 bg-zinc-950/40 px-3 py-2 text-xs text-zinc-300">
          <input type="radio" name="rules-auto-next-trigger" checked={!autoNext && !autoNextOnLimit}
            onChange={() => { setAutoNext(false); setAutoNextOnLimit(false); }}
            className="h-4 w-4 accent-zinc-400" />
          Off — no automatic next check
        </label>
        <div className="grid gap-3 md:grid-cols-2">
          <label className={`flex cursor-pointer items-start gap-3 rounded-xl border p-4 transition ${autoNext
            ? 'border-sky-400/50 bg-sky-500/10 ring-1 ring-sky-400/20'
            : 'border-zinc-700 bg-zinc-950/40 hover:border-sky-400/30 hover:bg-sky-500/5'}`}>
            <input type="radio" name="rules-auto-next-trigger" checked={autoNext} onChange={() => {
              setAutoNext(true);
              setAutoNextOnLimit(false);
            }}
              aria-describedby="rules-auto-next-window-help" className="mt-1 h-4 w-4 shrink-0 accent-sky-400" />
            <span className="min-w-0 flex-1">
              <span className="flex flex-wrap items-center justify-between gap-2">
                <span className="text-sm font-semibold text-sky-100">Start the next check after the full {configuredWindowRounds}-round window</span>
                <span className={`rounded-full px-2 py-0.5 text-[10px] font-bold uppercase tracking-wide ${autoNext
                  ? 'bg-sky-400/15 text-sky-200' : 'bg-zinc-800 text-zinc-400'}`}>{autoNext ? 'On' : 'Off'}</span>
              </span>
              <span id="rules-auto-next-window-help" className="mt-1 block text-xs leading-5 text-zinc-400">
                Wait for all {configuredWindowRounds} verified rounds in this session’s configured window.
              </span>
            </span>
          </label>
          <label className={`flex cursor-pointer items-start gap-3 rounded-xl border p-4 transition ${autoNextOnLimit
            ? 'border-amber-400/50 bg-amber-500/10 ring-1 ring-amber-400/20'
            : 'border-zinc-700 bg-zinc-950/40 hover:border-amber-400/30 hover:bg-amber-500/5'}`}>
            <input type="radio" name="rules-auto-next-trigger" checked={autoNextOnLimit} onChange={() => {
              setAutoNext(false);
              setAutoNextOnLimit(true);
            }}
              aria-describedby="rules-auto-next-limit-help" className="mt-1 h-4 w-4 shrink-0 accent-amber-400" />
            <span className="min-w-0 flex-1">
              <span className="flex flex-wrap items-center justify-between gap-2">
                <span className="text-sm font-semibold text-amber-100">Start the next check immediately when the configured limit is reached</span>
                <span className={`rounded-full px-2 py-0.5 text-[10px] font-bold uppercase tracking-wide ${autoNextOnLimit
                  ? 'bg-amber-400/15 text-amber-200' : 'bg-zinc-800 text-zinc-400'}`}>{autoNextOnLimit ? 'On' : 'Off'}</span>
              </span>
              <span id="rules-auto-next-limit-help" className="mt-1 block text-xs leading-5 text-zinc-400">
                {autoNextLimitHelp}
              </span>
            </span>
          </label>
        </div>
      </fieldset>}
      {(mode === 'START_CHECK' || mode === 'LEGACY_SEPARATE_TRACKER' || mode === 'ALL_RULES_TOGETHER')
        && <p className="mt-2 text-xs text-zinc-500">The next check starts on the next verified, contiguous round after its selected trigger.</p>}
      </div>

      <div className="mt-5 flex flex-col gap-3 border-t border-emerald-500/15 pt-4 sm:flex-row sm:flex-wrap sm:items-center">
        <Button tone="good" onClick={start} disabled={busy || Boolean(activeSession)}>
          {busyAction === 'start-shadow' ? 'Starting SHADOW session…' : 'Start SHADOW session'}
        </Button>
        {controlSession && <Button tone="warning" onClick={reconfigureCurrentSession} disabled={busy}>
          {busyAction === 'reconfigure-shadow' ? 'Applying settings…' : 'Restart with these settings'}
        </Button>}
        <div className="flex w-full gap-2 sm:ml-auto sm:w-auto sm:min-w-[22rem]">
          <input aria-label="Preset name" value={presetName} onChange={(e) => setPresetName(e.target.value)} placeholder="Preset name" className="h-10 min-w-0 flex-1 rounded-lg border border-violet-500/25 bg-zinc-950/80 px-3 text-sm text-zinc-100 outline-none transition placeholder:text-zinc-500 focus:border-violet-400 focus:ring-2 focus:ring-violet-400/20" />
          <Button onClick={savePreset} disabled={busy || !presetName.trim()}>Save preset</Button>
        </div>
        <span role="status" aria-live="polite" className={`inline-flex self-start items-center rounded-full border px-3 py-1 text-xs font-medium sm:self-center ${draftSaveState === 'saved'
          ? 'border-emerald-400/20 bg-emerald-400/10 text-emerald-200'
          : 'border-amber-400/20 bg-amber-400/10 text-amber-200'}`}>
          {draftSaveState === 'saved' ? 'Changes saved in this browser'
            : 'Draft could not be saved in this browser'}
        </span>
        <p className="basis-full text-xs text-zinc-500">SHADOW outcomes and real platform reconciliation are recorded separately.</p>
        <p role="status" className={`basis-full rounded-lg border px-3 py-2 text-xs ${activeSession
          ? 'border-amber-400/25 bg-amber-400/10 text-amber-200'
          : shadowReadiness?.ready === false ? 'border-rose-400/25 bg-rose-400/10 text-rose-200'
            : shadowReadiness?.ready && hasSelectedEnabledRule ? 'border-emerald-400/25 bg-emerald-400/10 text-emerald-200'
              : 'border-sky-400/20 bg-sky-400/5 text-sky-100'}`}>{startHint}</p>
      </div>
    </section>

    <section className="rounded-xl border border-zinc-800 bg-zinc-900/60 p-5">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div><h2 className="text-lg font-semibold">{controlSession?.status === 'PAUSED' ? 'Paused session' : controlSession ? 'Current session' : displaySession ? 'Latest session' : 'Current session'}</h2>
          <p className="mt-1 text-sm text-zinc-400">{displaySession ? `${MODE_LABELS[displaySession.mode] || displaySession.mode} · ${displaySession.session_id}` : 'No active or saved session'}</p>
        </div>
        {controlSession && <div className="flex flex-wrap gap-2">
          {controlSession.status === 'RUNNING' && <Button tone="info" onClick={() => sessionAction(controlSession.session_id, 'pause')} disabled={busy}>{busyAction === 'pause' ? 'Pausing…' : 'Pause'}</Button>}
          {controlSession.status === 'PAUSED' && ['MANUAL_PAUSE', 'UNKNOWN_TRANSACTION_REQUIRES_RECONCILIATION'].includes(controlSession.stop_reason)
            && <Button tone="good" onClick={() => sessionAction(controlSession.session_id, 'resume')} disabled={busy}>{busyAction === 'resume' ? 'Resuming…' : 'Resume'}</Button>}
          <Button tone="danger" onClick={() => sessionAction(controlSession.session_id, 'stop')} disabled={busy}>{busyAction === 'stop' ? 'Stopping…' : 'Stop'}</Button>
          <Button tone="emergency" onClick={() => sessionAction(controlSession.session_id, 'emergency_stop')} disabled={busy}>{busyAction === 'emergency_stop' ? 'Stopping…' : 'Emergency stop'}</Button>
          <Button tone="reset" onClick={() => sessionAction(controlSession.session_id, 'reset')} disabled={busy}>{busyAction === 'reset' ? 'Resetting…' : 'Reset'}</Button>
        </div>}
      </div>
      {displaySession && <div className="mt-4 grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
        <Metric label="Status" value={displaySession.status} valueClassName={displaySession.status === 'RUNNING'
          ? 'text-emerald-300' : displaySession.status === 'PAUSED' ? 'text-amber-200'
            : displaySession.integrity_stop_reason ? 'text-rose-300' : 'text-sky-200'}
          className={displaySession.status === 'RUNNING' ? 'border-emerald-400/25 bg-emerald-950/20'
            : displaySession.status === 'PAUSED' ? 'border-amber-400/30 bg-amber-950/20'
              : displaySession.integrity_stop_reason ? 'border-rose-400/25 bg-rose-950/20' : ''}
          detail={displaySession.selection_status} />
        <Metric label="Current round" value={receivedRound?.round_index ?? detailSession?.last_round_index ?? current?.round_index ?? '—'} valueClassName="text-sky-200" accentClassName="text-sky-300" className="border-sky-400/25 bg-sky-950/20" detailClassName="text-sky-100/60" detail={`${displaySession.observed_rounds ?? 'UNKNOWN'}/${displaySession.window_size ?? 'UNKNOWN'} verified`} />
        <Metric label={displaySession.mode === 'FIVE_RULE_TRACKER' ? 'Independent tracker selections' : 'Combined selections'} value={displaySession.selection_count} valueClassName="text-violet-200" accentClassName="text-violet-300" className="border-violet-400/25 bg-violet-950/20" detailClassName="text-violet-100/60" detail={`Session outcomes · WIN ${displaySession.results?.WIN ?? 0} · LOSS ${displaySession.results?.LOSS ?? 0} · PENDING ${displaySession.results?.PENDING ?? 0} · API signals count accepted fresh targets across all sessions`} />
        <Metric label="Net outcome" value={`${displaySession.net_outcome > 0 ? '+' : ''}${displaySession.net_outcome ?? 0}`} valueClassName={displaySession.net_outcome > 0 ? 'text-emerald-300' : displaySession.net_outcome < 0 ? 'text-rose-300' : 'text-cyan-200'} accentClassName={displaySession.net_outcome > 0 ? 'text-emerald-300' : displaySession.net_outcome < 0 ? 'text-rose-300' : 'text-cyan-300'} className={displaySession.net_outcome > 0 ? 'border-emerald-400/25 bg-emerald-950/20' : displaySession.net_outcome < 0 ? 'border-rose-400/25 bg-rose-950/20' : 'border-cyan-400/25 bg-cyan-950/20'} detailClassName="text-zinc-400" detail={`Settled WINs − LOSSes · WIN_LIMIT ${displaySession.win_limit_enabled ? displaySession.win_limit_count : 'OFF'} · LOSS_LIMIT ${displaySession.loss_limit_enabled ? displaySession.loss_limit_count : 'OFF'}`} />
        <Metric label="Shadow profit / loss" value={`${Number(displaySession.net_profit_rwf ?? 0) > 0 ? '+' : ''}${Number(displaySession.net_profit_rwf ?? 0).toLocaleString()} RWF`} valueClassName={Number(displaySession.net_profit_rwf ?? 0) > 0 ? 'text-emerald-300' : Number(displaySession.net_profit_rwf ?? 0) < 0 ? 'text-rose-300' : 'text-cyan-200'} accentClassName={Number(displaySession.net_profit_rwf ?? 0) > 0 ? 'text-emerald-300' : Number(displaySession.net_profit_rwf ?? 0) < 0 ? 'text-rose-300' : 'text-cyan-300'} className={Number(displaySession.net_profit_rwf ?? 0) > 0 ? 'border-emerald-400/25 bg-emerald-950/20' : Number(displaySession.net_profit_rwf ?? 0) < 0 ? 'border-rose-400/25 bg-rose-950/20' : 'border-cyan-400/25 bg-cyan-950/20'} detailClassName="text-zinc-400" detail={`Fixed stake ${Number(displaySession.requested_stake ?? 0).toLocaleString()} ${displaySession.requested_stake_currency || 'RWF'} · 2.00x model · pending excluded`} />
        <Metric label="Stop reason" value={displaySession.stop_reason || 'NONE'} valueClassName={displaySession.stop_reason && displaySession.stop_reason !== 'NONE' ? 'text-amber-200' : 'text-emerald-200'} accentClassName={displaySession.stop_reason && displaySession.stop_reason !== 'NONE' ? 'text-amber-300' : 'text-emerald-300'} className={displaySession.stop_reason && displaySession.stop_reason !== 'NONE' ? 'border-amber-400/25 bg-amber-950/20' : 'border-emerald-400/25 bg-emerald-950/20'} detailClassName="text-zinc-400" detail={`${displaySession.stop_reason_source || 'Source unknown'} · ${displaySession.stop_reason_at || 'timestamp unavailable'}${displaySession.stop_reason_round_id ? ` · round ${displaySession.stop_reason_round_id}` : ''}`} />
        <Metric label="Automatic next check" value={autoNextCheckIsOn ? 'ON'
          : autoNextCheckIsOff ? 'OFF' : 'UNKNOWN'}
          valueClassName={autoNextCheckIsOn ? 'text-emerald-300'
            : autoNextCheckIsOff ? 'text-zinc-200' : 'text-amber-200'}
          accentClassName={autoNextCheckIsOn ? 'text-emerald-300'
            : autoNextCheckIsOff ? 'text-zinc-400' : 'text-amber-300'}
          className={autoNextCheckIsOn ? 'border-emerald-400/25 bg-emerald-950/20'
            : autoNextCheckIsOff ? '' : 'border-amber-400/25 bg-amber-950/20'}
          detailClassName="text-zinc-400"
          detail={`Selected trigger: ${autoNextTriggerLabel} · Runtime: ${displaySession.auto_next_armed ? 'ARMED'
            : displaySession.auto_next_session_id ? 'STARTED'
              : displaySession.auto_next_block_reason || 'INACTIVE'}${displaySession.auto_next_trigger ? ` · ${displaySession.auto_next_trigger}` : ''}${displaySession.auto_next_armed_at ? ` · ${displaySession.auto_next_armed_at}` : ''}${displaySession.auto_next_session_id ? ` · session ${displaySession.auto_next_session_id}` : ''}`} />
        {displaySession.integrity_stop_reason && <Metric label="Preserved integrity reason" value={displaySession.integrity_stop_reason} />}
      </div>}
      {displaySession && <div className="mt-3 grid gap-2 rounded-lg border border-sky-400/15 bg-sky-950/10 p-3 text-xs text-zinc-300 sm:grid-cols-2 xl:grid-cols-4">
        <div><span className="text-zinc-500">Last received round</span><div className="mt-1 break-all">{receivedRound?.round_id || 'UNKNOWN'}{receivedRound?.round_index != null ? ` · #${receivedRound.round_index}` : ''}</div><div className="mt-1 text-zinc-500">Verified at {receivedRound?.observed_at || 'UNKNOWN'} · received {receivedRound?.received_at || 'UNKNOWN'}</div></div>
        <div><span className="text-zinc-500">Last settled target</span><div className="mt-1 break-all">{lastSettledTarget?.round_id || 'NONE'}{lastSettledTarget?.round_index != null ? ` · #${lastSettledTarget.round_index}` : ''}</div><div className="mt-1 text-zinc-500">{lastSettledTarget ? `${lastSettledTarget.settlement_count} selection(s) · ${lastSettledTarget.settled_at || 'settlement time UNKNOWN'}` : 'No target settlement recorded'}</div></div>
        <div><span className="text-zinc-500">Last evaluation</span><div className="mt-1">{displaySession.last_evaluation_at || 'UNKNOWN'}</div><div className="mt-1 text-zinc-500">Session {displaySession.is_active === true ? 'active' : displaySession.is_active === false ? 'not active' : 'activity UNKNOWN'}</div></div>
        <div><span className="text-zinc-500">Event processing latency</span><div className="mt-1">{displaySession.event_processing_latency_ms != null && Number.isFinite(Number(displaySession.event_processing_latency_ms)) ? `${Number(displaySession.event_processing_latency_ms).toFixed(3)} ms` : 'UNKNOWN'}</div><div className="mt-1 text-zinc-500">Settlement {displaySession.settlement_processing_latency_ms != null && Number.isFinite(Number(displaySession.settlement_processing_latency_ms)) ? `${Number(displaySession.settlement_processing_latency_ms).toFixed(3)} ms` : 'No settlement sample'} · durable p50/p95/p99 {settlementLatencySummary.samples ? `${settlementLatencySummary.p50_ms}/${settlementLatencySummary.p95_ms}/${settlementLatencySummary.p99_ms} ms` : 'UNKNOWN'} (${settlementLatencySummary.samples || 0} samples)</div></div>
      </div>}
      {detailSession && <>
        <details className="mt-5 rounded-lg border border-zinc-800 p-4">
          <summary className="cursor-pointer text-sm font-semibold text-zinc-200">Selections and resolved target outcomes</summary>
          <div className="mt-3 overflow-x-auto">
            <table className="w-full min-w-[1320px] text-left text-xs">
              <thead className="text-zinc-500"><tr><th className="py-2">Decision / source</th><th>Target round</th><th>Matched rules</th><th>Stake / model</th><th>Settlement</th><th>Verified target evidence</th><th>Shadow P/L</th></tr></thead>
              <tbody>{allSelections.slice(0, 100).map((selection) => <tr key={selection.selection_id} className="border-t border-zinc-800 align-top">
                <td className="py-2"><code className="break-all text-zinc-300">{selection.decision_id || selection.selection_id || 'UNKNOWN'}</code><div className="mt-2">Source #{selection.source_round?.roundIndex ?? 'UNKNOWN'}</div><div className="break-all text-zinc-500">{selection.source_round_id || selection.source_round?.roundId || 'UNKNOWN'}</div><div className="mt-1 text-zinc-500">{selection.selected_at || 'Selection time UNKNOWN'}</div></td>
                <td>#{selection.expected_target?.roundIndex ?? 'UNKNOWN'}<br /><span className="break-all text-zinc-400">{selection.target_round_id || selection.actual_target?.roundId || 'AWAITING_VERIFIED_TARGET'}</span></td>
                <td>{(selection.matched_rule_ids || selection.contributing_rule_ids || (selection.rule_id ? [selection.rule_id] : [])).join(', ') || 'UNKNOWN'}</td>
                <td>{(selection.stake_rwf ?? detailSession.configuration_snapshot?.fixed_stake_rwf) == null ? 'UNKNOWN' : `${Number(selection.stake_rwf ?? detailSession.configuration_snapshot?.fixed_stake_rwf).toLocaleString()} RWF`}<div className="mt-1 text-zinc-500">{selection.payout_multiplier ?? detailSession.configuration_snapshot?.cashout_multiplier ?? 2.0}x</div></td>
                <td className={selection.status === 'WIN' ? 'text-emerald-300' : selection.status === 'LOSS' ? 'text-rose-300' : 'text-amber-200'}>{selection.settlement_status || selection.status || 'UNKNOWN'}<div className="mt-1 text-zinc-400">{selection.settlement_block_reason || (selection.status === 'PENDING' ? `Waiting for verified target #${selection.expected_target?.roundIndex ?? 'UNKNOWN'}` : selection.settlement_phase || 'UNKNOWN')}</div><div className="mt-1">{selection.actual_multiplier == null ? 'Multiplier PENDING' : `${selection.actual_multiplier}x`}</div><div className="mt-1 text-zinc-500">{selection.settled_at || selection.resolved_at || 'Settlement time UNKNOWN'}</div></td>
                <td><div>{selection.target_persistence_status || selection.settlement_evidence?.durability || 'UNKNOWN'}</div><div className="text-zinc-500">{selection.settlement_evidence?.persistence_confirmation?.event_type || selection.settlement_evidence?.event_type || 'Event type UNKNOWN'}</div><div className="break-all text-zinc-500">{selection.settlement_evidence?.target_round_id || selection.target_round_id || 'Target ID UNKNOWN'}</div><div className="text-zinc-500">Observed {selection.target_round_completed_at || selection.settlement_evidence?.target_observed_at || 'UNKNOWN'}</div></td>
                <td className={Number(selection.profit_loss_rwf) > 0 ? 'text-emerald-300' : Number(selection.profit_loss_rwf) < 0 ? 'text-rose-300' : 'text-zinc-300'}>{selection.profit_loss_rwf == null ? 'PENDING' : `${Number(selection.profit_loss_rwf) > 0 ? '+' : ''}${Number(selection.profit_loss_rwf).toLocaleString()} RWF`}{selection.payout_rwf != null && <div className="mt-1 text-zinc-500">Payout {Number(selection.payout_rwf).toLocaleString()} RWF</div>}</td>
              </tr>)}</tbody>
            </table>
          </div>
        </details>
        <details className="mt-3 rounded-lg border border-zinc-800 p-4">
          <summary className="cursor-pointer text-sm font-semibold text-zinc-200">Per-rule diagnostics and source attribution</summary>
          <div className="mt-3 space-y-3">
            {recentEvaluations.map((entry, index) => <article key={`${entry.source_round?.roundId}-${index}`} className="rounded-lg border border-zinc-800 bg-zinc-950/60 p-3 text-xs">
              <div className="flex flex-wrap justify-between gap-2 text-zinc-300">
                <span>Source #{entry.source_round?.roundIndex} · {entry.source_round?.roundId} · {entry.source_round?.multiplier}x</span>
                <span>Target #{entry.target_round?.roundIndex} · {entry.evaluated_at}</span>
              </div>
              <div className="mt-2 font-semibold text-emerald-200">{entry.decision?.action || 'WAIT'} · {entry.decision?.true_rule_ids?.join(', ') || 'no contributing rules'}</div>
              <div className="mt-2 space-y-1">{(entry.rule_evaluations || []).map((evaluation) => <div key={evaluation.rule_id} className="flex flex-wrap gap-x-2">
                <b className={evaluation.eligible === true ? 'text-emerald-300' : evaluation.eligible === false ? 'text-zinc-300' : 'text-amber-300'}>{evaluation.status}</b>
                <code className="text-zinc-300">{evaluation.rule_id}</code>
                <span className="text-zinc-400">{evaluation.reason}</span>
              </div>)}</div>
              <details className="mt-2 text-zinc-500"><summary className="cursor-pointer">Private state details</summary><pre className="mt-2 max-h-72 overflow-auto whitespace-pre-wrap">{JSON.stringify(entry.rule_evaluations?.map((row) => ({ rule_id: row.rule_id, state_snapshot: row.state_snapshot })), null, 2)}</pre></details>
            </article>)}
            {!recentEvaluations.length && <p className="text-sm text-zinc-500">No source evaluations recorded.</p>}
          </div>
        </details>
      </>}
    </section>
    <section aria-label="Saved and inactive SHADOW sessions" className="rounded-xl border border-zinc-800 bg-zinc-900/60 p-5">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h2 className="text-lg font-semibold">Saved and inactive SHADOW sessions</h2>
          <p className="mt-1 text-sm text-zinc-400">Newest completed, settling, paused, and stopped sessions appear here. Deleting a settling session stops its observation and removes its simulated results.</p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-sm text-zinc-500">{finishedSessions.length} saved</span>
          <Button onClick={() => setSelectedFinishedSessionIds(allDeletableSelected
            ? new Set() : new Set(deletableFinishedSessions.map((session) => session.session_id)))}
            disabled={busy || !deletableFinishedSessions.length}>
            {allDeletableSelected ? 'Clear selection' : `Select all removable (${deletableFinishedSessions.length})`}
          </Button>
          <Button tone="danger" onClick={() => deleteFinishedSessions(selectedDeletableSessions)}
            disabled={busy || !selectedDeletableSessions.length}>
            {busyAction === 'delete-finished-shadow'
              ? 'Deleting sessions…' : `Delete selected (${selectedDeletableSessions.length})`}
          </Button>
          {busyAction === 'delete-finished-shadow' && <span role="status" aria-live="polite" className="text-xs text-rose-200">
            Removing selected inactive sessions…
          </span>}
        </div>
      </div>
      <div className="mt-4 space-y-3">
        {finishedSessions.map((session) => {
          const profitValue = session.net_profit_rwf;
          const profit = Number(profitValue);
          const profitKnown = profitValue !== null && profitValue !== undefined && Number.isFinite(profit);
          const pendingValue = session.results?.PENDING;
          const pendingKnown = pendingValue !== null && pendingValue !== undefined
            && Number.isFinite(Number(pendingValue));
          const pending = pendingKnown ? Number(pendingValue) : null;
          const canDelete = canDeleteFinishedSession(session);
          const deleteBlockReason = session.auto_next_armed === true
            ? 'Disarm automatic next check before deleting this record.' : null;
          const pendingDeleteWarning = canDelete && pending > 0
            ? `Delete will remove ${pending} pending simulated outcome${pending === 1 ? '' : 's'} without assuming a result.` : null;
          const statusStyle = session.status === 'COMPLETED'
            ? 'border-emerald-400/30 bg-emerald-400/10 text-emerald-200'
            : session.status === 'SETTLING' || session.status === 'PAUSED'
              ? 'border-amber-400/30 bg-amber-400/10 text-amber-200'
              : session.status === 'RECONFIGURED'
              ? 'border-sky-400/30 bg-sky-400/10 text-sky-200'
              : 'border-zinc-700 bg-zinc-800/70 text-zinc-300';
          return <article key={session.session_id} className="grid gap-4 rounded-xl border border-zinc-800 bg-gradient-to-br from-zinc-950/80 to-zinc-900/50 p-4 shadow-sm shadow-black/20 sm:grid-cols-[minmax(0,1fr)_auto] sm:items-center">
            <div className="min-w-0">
              <div className="flex flex-wrap items-center gap-2">
                <input type="checkbox" aria-label={`Select session ${session.session_id} for deletion`}
                  checked={selectedFinishedSessionIds.has(session.session_id)}
                  disabled={busy || !canDelete}
                  onChange={(event) => setSelectedFinishedSessionIds((previous) => {
                    const next = new Set(previous);
                    if (event.target.checked) next.add(session.session_id);
                    else next.delete(session.session_id);
                    return next;
                  })}
                  className="h-4 w-4 rounded border-zinc-600 bg-zinc-950 text-rose-400 focus:ring-rose-400/50 disabled:opacity-40" />
                <span className="font-semibold text-zinc-100">{MODE_LABELS[session.mode] || session.mode}</span>
                <span className={`rounded-full border px-2.5 py-1 text-[11px] font-bold tracking-wide ${statusStyle}`}>{session.status}</span>
                <span className={`text-sm font-semibold ${!profitKnown ? 'text-zinc-500' : profit > 0 ? 'text-emerald-300' : profit < 0 ? 'text-rose-300' : 'text-zinc-300'}`}>
                  {profitKnown ? `${profit > 0 ? '+' : ''}${profit.toLocaleString()} RWF` : 'P/L UNKNOWN'}
                </span>
              </div>
              <p className="mt-2 break-all font-mono text-[11px] text-zinc-500">{session.session_id}</p>
              <p className="mt-2 flex flex-wrap gap-x-3 gap-y-1 text-xs text-zinc-400">
                <span>Started {session.started_at || 'unknown'}</span>
                {session.ended_at && <span>Ended {session.ended_at}</span>}
                <span>{session.selection_count ?? '—'} selections</span>
                <span className="text-emerald-300">WIN {session.results?.WIN ?? '—'}</span>
                <span className="text-rose-300">LOSS {session.results?.LOSS ?? '—'}</span>
                <span className={pending ? 'text-amber-300' : 'text-zinc-500'}>PENDING {pending ?? '—'}</span>
              </p>
            </div>
            <div className="flex flex-wrap gap-2 sm:justify-end">
              <Button onClick={() => viewFinishedSession(session.session_id)} disabled={busy}>
                {historyDetail?.session_id === session.session_id ? 'Viewed' : 'View'}
              </Button>
              <Button tone="danger" onClick={() => deleteFinishedSession(session)} disabled={busy || !canDelete}>
                Delete
              </Button>
            </div>
            {deleteBlockReason && <p className="text-xs text-amber-300 sm:col-span-2">{deleteBlockReason}{session.status === 'SETTLING' && Number.isFinite(Number(session.window_size)) ? ` (${session.observed_rounds ?? 0}/${session.window_size} rounds observed).` : ''}</p>}
            {pendingDeleteWarning && <p className="text-xs text-amber-300 sm:col-span-2">{pendingDeleteWarning}</p>}
          </article>;
        })}
        {!finishedSessions.length && <p className="rounded-lg border border-dashed border-zinc-800 p-4 text-sm text-zinc-500">Finished shadow sessions will appear here after they are stored.</p>}
      </div>
      {historyDetail && <div className="fixed inset-0 z-[100] flex items-center justify-center bg-black/75 p-3 backdrop-blur-sm sm:p-6"
        onMouseDown={(event) => { if (event.target === event.currentTarget) setHistoryDetail(null); }}>
        <section role="dialog" aria-modal="true" aria-labelledby="saved-shadow-detail-title"
          className="max-h-[calc(100vh-1.5rem)] w-full max-w-7xl overflow-y-auto rounded-xl border border-emerald-500/30 bg-zinc-950 p-4 shadow-2xl shadow-black/60 sm:max-h-[calc(100vh-3rem)] sm:p-6">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <h3 id="saved-shadow-detail-title" className="font-semibold text-zinc-100">Saved session details</h3>
            <p className="mt-1 break-all text-xs text-zinc-500">{historyDetail.session_id} · {historyDetail.status} · {historyDetail.ended_at || historyDetail.started_at}</p>
          </div>
          <Button onClick={() => setHistoryDetail(null)}>Close</Button>
        </div>
        <div className="mt-3 grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
          <Metric label="Net outcome" value={`${historyDetail.net_outcome > 0 ? '+' : ''}${historyDetail.net_outcome ?? 0}`} />
          <Metric label="Shadow profit / loss" value={`${Number(historyDetail.net_profit_rwf ?? 0) > 0 ? '+' : ''}${Number(historyDetail.net_profit_rwf ?? 0).toLocaleString()} RWF`} valueClassName={Number(historyDetail.net_profit_rwf ?? 0) > 0 ? 'text-emerald-300' : Number(historyDetail.net_profit_rwf ?? 0) < 0 ? 'text-rose-300' : 'text-zinc-100'} detail={`Fixed stake ${historyStake.toLocaleString()} ${historyCurrency} · 2.00x model`} />
          <Metric label="Settled results" value={`WIN ${historyDetail.total_wins ?? historyDetail.results?.WIN ?? 0} · LOSS ${historyDetail.total_losses ?? historyDetail.results?.LOSS ?? 0}`} detail={`PENDING ${historyDetail.results?.PENDING ?? historySelections.filter((item) => item.status === 'PENDING').length}`} />
          <Metric label="Stop reason" value={historyDetail.stop_reason || historyDetail.selection_stop_reason || 'NONE'} detail={`${historyDetail.stop_reason_source || 'Source unknown'} · ${historyDetail.stop_reason_at || 'timestamp unavailable'}${historyDetail.stop_reason_round_id ? ` · round ${historyDetail.stop_reason_round_id}` : ''}`} />
        </div>
        <section className="mt-4 rounded-lg border border-sky-400/20 bg-sky-950/10 p-4" aria-label="Saved rule evaluation results">
          <div className="flex flex-wrap items-start justify-between gap-2">
            <div>
              <h4 className="text-sm font-semibold text-sky-100">Rule evaluation results</h4>
              <p className="mt-1 text-xs text-zinc-400">
                {historyEvaluations.length} source rounds evaluated out of {historyDetail.observed_rounds ?? historyDetail.window_size ?? '—'} observed.
                {historyPausedRounds > 0 ? ` ${historyPausedRounds} round${historyPausedRounds === 1 ? '' : 's'} occurred during manual pause and were not evaluated.` : ''}
              </p>
              <p className="mt-1 text-xs text-zinc-500">
                Rules in this session: {(historyDetail.manifest?.rules || []).map((rule) => rule.name || rule.rule_id).join(' · ')
                  || (historyDetail.rule_ids || []).join(' · ') || 'None'}
              </p>
            </div>
            <span className="rounded-full border border-sky-400/30 bg-sky-400/10 px-2.5 py-1 text-xs font-semibold text-sky-200">
              {historyRuleEvaluations.length} rule checks
            </span>
          </div>
          <div className="mt-3 grid gap-2 sm:grid-cols-2 xl:grid-cols-4">
            <div className="rounded-lg border border-emerald-400/20 bg-emerald-950/20 p-3">
              <div className="text-xs text-zinc-400">Matched in time</div>
              <div className="mt-1 text-lg font-semibold text-emerald-200">{historyEvaluationStats.matched}</div>
            </div>
            <div className="rounded-lg border border-zinc-700 bg-zinc-900/50 p-3">
              <div className="text-xs text-zinc-400">FALSE</div>
              <div className="mt-1 text-lg font-semibold text-zinc-200">{historyEvaluationStats.false}</div>
            </div>
            <div className="rounded-lg border border-amber-400/25 bg-amber-950/20 p-3">
              <div className="text-xs text-amber-100/70">Target already completed</div>
              <div className="mt-1 text-lg font-semibold text-amber-200">{historyEvaluationStats.targetAlreadyCompleted}</div>
            </div>
            <div className="rounded-lg border border-violet-400/20 bg-violet-950/20 p-3">
              <div className="text-xs text-zinc-400">Unavailable / other</div>
              <div className="mt-1 text-lg font-semibold text-violet-200">{historyEvaluationStats.unavailable}</div>
            </div>
          </div>
          {historyEvaluationStats.targetAlreadyCompleted > 0 && <p className="mt-3 rounded-md border border-amber-400/20 bg-amber-400/5 px-3 py-2 text-xs leading-5 text-amber-100/90">
            These historical rounds were replayed after their targets had finished. They did not create backdated selections; verified target results synchronize the rule state.
          </p>}
          {historyEvaluations.length > 0 && <details className="mt-3 rounded-lg border border-zinc-800 bg-zinc-950/40 p-3">
            <summary className="cursor-pointer text-xs font-semibold text-zinc-200">View source-by-source evaluations</summary>
            <div className="mt-3 max-h-[34rem] space-y-2 overflow-y-auto pr-1">
              {[...historyEvaluations].reverse().map((entry, index) => <article key={`${entry.source_round?.roundId || 'source'}-${index}`} className="rounded-md border border-zinc-800 bg-zinc-900/50 p-3">
                <div className="flex flex-wrap justify-between gap-2 text-xs text-zinc-300">
                  <span>Source #{entry.source_round?.roundIndex ?? '—'} · {entry.source_round?.multiplier ?? '—'}x · {entry.source_round?.roundId || 'unknown round'}</span>
                  <span>{entry.decision?.action || 'WAIT'} · target #{entry.target_round?.roundIndex ?? '—'}</span>
                </div>
                <div className="mt-2 space-y-1.5">
                  {(entry.rule_evaluations || []).map((evaluation) => {
                    const displayStatus = displayEvaluationStatus(evaluation.status);
                    return <div key={evaluation.rule_id} className="flex flex-wrap gap-x-2 text-xs">
                      <b className={displayStatus === 'TARGET_ALREADY_COMPLETED' ? 'text-amber-200' : evaluation.eligible === true ? 'text-emerald-300' : evaluation.eligible === false ? 'text-zinc-300' : 'text-violet-200'}>{displayStatus}</b>
                      <code className="text-zinc-300">{evaluation.rule_id || 'unknown rule'}</code>
                      <span className="text-zinc-400">{evaluation.reason || 'No reason recorded.'}</span>
                    </div>;
                  })}
                  {!entry.rule_evaluations?.length && <span className="text-xs text-zinc-500">No rule check was run for this source round.</span>}
                </div>
              </article>)}
            </div>
          </details>}
        </section>
        <div className="mt-4 overflow-x-auto">
          <table className="w-full min-w-[1320px] text-left text-xs">
            <thead className="text-zinc-500"><tr><th className="py-2">Decision / source</th><th>Target round</th><th>Matched rules</th><th>Stake / model</th><th>Settlement</th><th>Verified target evidence</th><th>Shadow P/L</th></tr></thead>
            <tbody>{historySelections.map((selection) => {
              const selectionStake = Number(selection.stake_rwf ?? historyStake);
              const selectionProfit = selection.profit_loss_rwf ?? (selection.status === 'WIN'
                ? selectionStake : selection.status === 'LOSS' ? -selectionStake : null);
              return <tr key={selection.selection_id} className="border-t border-zinc-800 align-top">
                <td className="py-2"><code className="break-all text-zinc-300">{selection.decision_id || selection.selection_id || 'UNKNOWN'}</code><div className="mt-2">Source #{selection.source_round?.roundIndex ?? 'UNKNOWN'}</div><div className="break-all text-zinc-500">{selection.source_round_id || selection.source_round?.roundId || 'UNKNOWN'}</div><div className="mt-1 text-zinc-500">{selection.selected_at || 'Selection time UNKNOWN'}</div></td>
                <td>#{selection.expected_target?.roundIndex ?? 'UNKNOWN'}<br /><span className="break-all text-zinc-400">{selection.target_round_id || selection.actual_target?.roundId || 'AWAITING_VERIFIED_TARGET'}</span></td>
                <td>{(selection.matched_rule_ids || selection.contributing_rule_ids || (selection.rule_id ? [selection.rule_id] : [])).join(', ') || 'UNKNOWN'}</td>
                <td>{(selection.stake_rwf ?? historyDetail.configuration_snapshot?.fixed_stake_rwf) == null ? 'UNKNOWN' : `${selectionStake.toLocaleString()} ${historyCurrency}`}<div className="mt-1 text-zinc-500">{selection.payout_multiplier ?? historyDetail.configuration_snapshot?.cashout_multiplier ?? 2.0}x</div></td>
                <td className={selection.status === 'WIN' ? 'text-emerald-300' : selection.status === 'LOSS' ? 'text-rose-300' : 'text-amber-200'}>{selection.settlement_status || selection.status || 'UNKNOWN'}<div className="mt-1 text-zinc-400">{selection.settlement_block_reason || (selection.status === 'PENDING' ? `Waiting for verified target #${selection.expected_target?.roundIndex ?? 'UNKNOWN'}` : selection.settlement_phase || 'UNKNOWN')}</div><div className="mt-1">{selection.actual_multiplier == null ? 'Multiplier PENDING' : `${selection.actual_multiplier}x`}</div><div className="mt-1 text-zinc-500">{selection.settled_at || selection.resolved_at || 'Settlement time UNKNOWN'}</div></td>
                <td><div>{selection.target_persistence_status || selection.settlement_evidence?.durability || 'UNKNOWN'}</div><div className="text-zinc-500">{selection.settlement_evidence?.persistence_confirmation?.event_type || selection.settlement_evidence?.event_type || 'Event type UNKNOWN'}</div><div className="break-all text-zinc-500">{selection.settlement_evidence?.target_round_id || selection.target_round_id || 'Target ID UNKNOWN'}</div><div className="text-zinc-500">Observed {selection.target_round_completed_at || selection.settlement_evidence?.target_observed_at || 'UNKNOWN'}</div></td>
                <td className={selectionProfit > 0 ? 'text-emerald-300' : selectionProfit < 0 ? 'text-rose-300' : 'text-zinc-400'}>{selectionProfit == null ? 'PENDING' : `${selectionProfit > 0 ? '+' : ''}${Number(selectionProfit).toLocaleString()} ${historyCurrency}`}{selection.payout_rwf != null && <div className="mt-1 text-zinc-500">Payout {Number(selection.payout_rwf).toLocaleString()} {historyCurrency}</div>}</td>
              </tr>;
            })}</tbody>
          </table>
          {!historySelections.length && <p className="py-4 text-sm text-zinc-500">No SHADOW selections were recorded. The evaluation audit above shows which rules returned FALSE and which matches arrived after their target rounds had completed.</p>}
        </div>
        </section>
      </div>}
    </section>
  </main>;
}
