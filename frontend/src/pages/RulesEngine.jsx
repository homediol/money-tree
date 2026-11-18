import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Activity, Database, ListChecks, ShieldCheck } from 'lucide-react';
import { api } from '../services/api.js';
import { statusRequestError } from '../services/statusRequestError.js';
import {
  isRuleSelectionApplied,
  ruleSelectionRequest,
  sessionSelectionDetail,
  sessionSelectionLabel,
  updateSessionListAfterAction,
  updateStatusAfterSessionAction,
} from '../services/rulesSessionControls.js';
import { subscribeLiveEvents } from '../services/liveEvents.js';
import RoundPerformanceDashboard from '../components/RoundPerformanceDashboard.jsx';
import { BehaviourDetectionSelectionCard, BehaviourStrategySessionSummary } from './BehaviourDetection.jsx';

const MODE_LABELS = {
  START_CHECK: 'Start a Check',
  LEGACY_SEPARATE_TRACKER: 'Legacy Separate Tracker',
  FIVE_RULE_TRACKER: 'Five Rules · Shared SELECT',
  ALL_RULES_TOGETHER: 'All Rules Together',
};
const RULE_SELECTION_OPTIONS = [
  { id: 'automatic', label: 'Automatic', accent: 'accent-sky-400', tone: 'sky',
    idle: 'border-sky-400/15 bg-sky-950/10', selected: 'border-sky-300/45 bg-sky-400/[0.08] ring-1 ring-sky-300/15',
    detail: 'Evaluate enabled policies against verified performance.' },
  { id: 'manual', label: 'Manual', accent: 'accent-emerald-400', tone: 'emerald',
    idle: 'border-emerald-400/15 bg-emerald-950/10', selected: 'border-emerald-300/45 bg-emerald-400/[0.08] ring-1 ring-emerald-300/15',
    detail: 'Choose the enabled rules for the next session.' },
  { id: 'adaptive', label: 'Adaptive', accent: 'accent-violet-400', tone: 'violet',
    idle: 'border-violet-400/15 bg-violet-950/10', selected: 'border-violet-300/45 bg-violet-400/[0.08] ring-1 ring-violet-300/15',
    detail: 'Adjust the rule portfolio as verified evidence changes.' },
  { id: 'behaviour', label: 'Behaviour Detection', accent: 'accent-fuchsia-400', tone: 'fuchsia',
    idle: 'border-fuchsia-400/15 bg-fuchsia-950/10', selected: 'border-fuchsia-300/45 bg-fuchsia-400/[0.09] ring-1 ring-fuchsia-300/15',
    detail: 'Use verified Rolling HIGH eligibility for SHADOW signals.' },
];
const AUTO_RULE_REASON_LABELS = {
  POSITIVE_SETTLED_EDGE: 'Current-window settled wins exceed losses',
  MULTIPLE_RULES_QUALIFIED: 'Several rules qualify from improving outcomes, rising signals, or positive net wins',
  NO_QUALIFIED_RULE: 'No recent performance improvement, rising signal trend, or positive settled net qualifies',
  PERFORMANCE_MOMENTUM_RISING: 'Recent settled performance is improving; selected even if current NET is negative',
  PERFORMANCE_MOMENTUM_FALLING: 'Recent settled performance is falling; removed even if current NET is positive',
  PERFORMANCE_WINDOW_RESET: 'Fresh 100-round performance window started at the latest verified round',
  SIGNAL_MOMENTUM_RISING: 'TRUE-signal rate is rising; rule selected early',
  SIGNAL_MOMENTUM_FALLING: 'TRUE-signal rate is falling; rule removed',
  PERFORMANCE_OBSERVER_UNAVAILABLE: 'Performance tracking is temporarily unavailable',
  PERFORMANCE_FEED_LAGGING: 'Waiting for current verified performance results',
  RULE_DISABLED: 'Waiting for Automatic to re-enable it after performance qualifies',
  SESSION_RULE_CONFIGURATION_CHANGED: 'Rule settings changed; start a new session to use new performance evidence',
  NO_RECENT_SETTLED_RESULTS: 'No settled results in the current window yet',
  INSUFFICIENT_POSITIVE_EVIDENCE: 'Waiting for enough settled outcomes, a rising signal trend, or positive net wins',
  NO_POSITIVE_NET: 'No recent performance improvement and current-window wins do not exceed losses',
  RULE_LOSS_LIMIT_WAITING_FOR_NEW_ENTRY: 'Paused after two losses; waiting for this rule’s entry signal to reset',
  RULE_WAITING_FOR_NEW_ENTRY: 'This entry has already been used; waiting for its signal to reset',
  NO_RECENT_POSITIVE_EDGE: 'Previous selector rejected the current window',
};
const signalMomentumSummary = (candidate) => {
  if (!candidate || candidate.signal_sample_count == null) return 'TRUE-signal trend: collecting';
  const recent = `${candidate.recent_signal_hits ?? 0}/${candidate.recent_signal_checks ?? 0}`;
  const previous = `${candidate.previous_signal_hits ?? 0}/${candidate.previous_signal_checks ?? 0}`;
  if (candidate.signal_momentum_pp == null) {
    return `TRUE signals ${recent} latest · ${previous} previous · collecting 6 checks`;
  }
  const delta = Number(candidate.signal_momentum_pp);
  const direction = delta > 0 ? '+' : '';
  const trend = candidate.signal_trend === 'RISING' ? 'RISING'
    : candidate.signal_trend === 'FALLING' ? 'FALLING' : 'STEADY';
  return `TRUE signals ${recent} latest vs ${previous} previous · ${direction}${delta} pp · ${trend}`;
};
const performanceMomentumSummary = (candidate) => {
  if (!candidate || candidate.outcome_sample_count == null) return 'Settled outcome trend: collecting';
  const recent = `${candidate.recent_outcome_wins ?? 0}W/${candidate.recent_outcome_losses ?? 0}L`;
  const previous = `${candidate.previous_outcome_wins ?? 0}W/${candidate.previous_outcome_losses ?? 0}L`;
  if (candidate.performance_momentum_pp == null) {
    const minimum = Number(candidate.outcome_momentum_minimum_sample) || 2;
    return `Settled outcomes ${recent} latest · ${previous} previous · collecting ${minimum} per group`;
  }
  const delta = Number(candidate.performance_momentum_pp);
  const direction = delta > 0 ? '+' : '';
  const trend = candidate.performance_trend === 'RISING' ? 'RISING'
    : candidate.performance_trend === 'FALLING' ? 'FALLING' : 'STEADY';
  return `Settled outcomes ${recent} latest vs ${previous} previous · ${direction}${delta} pp · ${trend}`;
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
  SESSION_DELETE_PERSISTENCE_UNAVAILABLE: 'Session storage is unavailable. The record was not deleted.',
};
const RESTARTABLE_INTEGRITY_STOPS = new Set([
  'ROUND_INDEX_GAP', 'ROUND_IDENTITY_CONFLICT',
  'OBSERVED_COMMIT_IDENTITY_CONFLICT', 'STREAM_IDENTITY_CHANGED',
]);
const SHADOW_READINESS_MESSAGES = {
  SHADOW_ROUND_FEED_STALE: 'The latest game result is too old to safely start a session. Wait for a new result.',
  FRESH_DATA_WATCH_ROUND_REQUIRED: 'A new game result has not arrived yet. The session can start after it does.',
  LIVE_ROUND_COLLECTOR_UNAVAILABLE: 'The game results service is reconnecting. Wait a moment and try again.',
  VERIFIED_COMMITTED_ROUND_REQUIRED: 'The latest game result has not finished saving and verification yet.',
  COMMITTED_ROUND_FEED_UNAVAILABLE: 'The latest game result is not available yet. Wait for it to finish saving.',
  POSTGRESQL_UNAVAILABLE: 'The app cannot save game results right now. It will retry automatically.',
  AUTHORITATIVE_POSTGRESQL_NOT_CONFIGURED: 'Game result storage is not available. Contact the person who manages this app.',
  RULES_ENGINE_NOT_CONFIGURED: 'No rules are configured for Manual selection yet.',
  NO_ENABLED_RULES: 'No rules are enabled for Manual selection yet.',
};

function orbitSourceRecoveryMessage(orbit = {}) {
  const frameUnavailable = orbit.lastError === 'WATCH_SOURCE_ERROR'
    && /saved data watch page or frame is no longer open in Chrome/i.test(orbit.watchSourceMessage || '');
  return frameUnavailable
    ? 'Orbit is reachable, but its saved game page or frame is not open in connected Chrome. Reopen the Aviator page so the Data Watch can resume; SHADOW will be ready after a fresh verified result arrives.'
    : 'The game results service is reconnecting. Wait a moment and try again.';
}

function explainShadowBlockers(blockers = [], orbit = {}) {
  const messages = blockers.map((blocker) => blocker === 'LIVE_ROUND_COLLECTOR_UNAVAILABLE'
    ? orbitSourceRecoveryMessage(orbit)
    : SHADOW_READINESS_MESSAGES[blocker]
      || 'The app could not confirm that game results are ready. Wait a moment and try again.');
  return [...new Set(messages)].join(' ');
}

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

function shortTime(value) {
  if (!value) return '—';
  const date = new Date(value);
  return Number.isNaN(date.getTime())
    ? '—'
    : date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
}

function sessionOptionsFromSettings({ mode, stake, winEnabled, winCount, lossEnabled, lossCount,
  maxSelections, maxObservedRounds, autoNext, autoNextOnLimit, autoNextLimitsEnabled,
  autoNextWinCount, autoNextLossCount, profitEnabled, profitGoal,
  automaticRuleSelection, adaptiveRuleSelection = false, behaviourDetectionEnabled = false,
  runUntilStopped }) {
  const sessionRounds = runUntilStopped ? null : Number(maxObservedRounds);
  const options = mode === 'ALL_RULES_TOGETHER' ? {
    window_size: sessionRounds, max_observed_rounds: sessionRounds, fixed_stake_rwf: Number(stake),
    win_limit_enabled: winEnabled,
    win_limit_count: winEnabled ? Number(winCount) : null,
    loss_limit_enabled: lossEnabled,
    loss_limit_count: lossEnabled ? Number(lossCount) : null,
    max_selections: maxSelections === '' ? null : Number(maxSelections),
    auto_next_check_enabled: autoNext && !runUntilStopped,
    auto_next_on_limit_enabled: autoNextOnLimit && !runUntilStopped,
    auto_next_limits_enabled: autoNextLimitsEnabled && !runUntilStopped,
    auto_next_win_count: !runUntilStopped && autoNextLimitsEnabled && autoNextWinCount !== '' ? Number(autoNextWinCount) : null,
    auto_next_loss_count: !runUntilStopped && autoNextLimitsEnabled && autoNextLossCount !== '' ? Number(autoNextLossCount) : null,
  } : mode === 'START_CHECK' || mode === 'LEGACY_SEPARATE_TRACKER' ? {
    window_size: sessionRounds, max_observed_rounds: sessionRounds, fixed_stake_rwf: Number(stake), cashout_multiplier: 2.0,
    profit_target_enabled: profitEnabled, profit_target_rwf: Number(profitGoal),
    auto_next_check_enabled: autoNext && !runUntilStopped,
    auto_next_on_limit_enabled: autoNextOnLimit && !runUntilStopped,
  } : {
    window_size: sessionRounds, max_observed_rounds: sessionRounds, fixed_stake_rwf: Number(stake),
  };
  return { ...options, automatic_rule_selection: automaticRuleSelection,
    ...(adaptiveRuleSelection ? { adaptive_rule_selection: true } : {}),
    behaviour_detection_enabled: Boolean(behaviourDetectionEnabled),
    run_until_stopped: Boolean(runUntilStopped) };
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
  return <article className={`min-w-0 rounded-lg border border-zinc-800 bg-zinc-900/70 p-3 ${className}`}>
    <div className="flex items-center gap-2 text-xs font-semibold uppercase tracking-wide text-zinc-400">
      <Icon size={15} className={accentClassName} />{label}
    </div>
    <div className={`mt-2 break-words text-base font-semibold ${valueClassName}`}>{value}</div>
    {detail && <div className={`mt-1 line-clamp-2 text-[11px] leading-4 ${detailClassName}`}>{detail}</div>}
  </article>;
}

function CountProgress({ label, completed, remaining, total, detail, tone = 'emerald' }) {
  const knownTotal = Number.isFinite(total) && total >= 0;
  const knownCompleted = Number.isFinite(completed) && completed >= 0;
  const done = knownTotal && knownCompleted ? Math.min(completed, total) : 0;
  const percent = knownTotal && knownCompleted
    ? total === 0 ? 0 : Math.min(100, (done / total) * 100) : null;
  const color = { emerald: 'bg-emerald-400', sky: 'bg-sky-400', violet: 'bg-violet-400', amber: 'bg-amber-400' }[tone]
    || 'bg-emerald-400';
  return <article className="rounded-lg border border-zinc-800 bg-zinc-950/45 p-3">
    <div className="flex flex-wrap items-center justify-between gap-x-4 gap-y-1">
      <h4 className="text-xs font-semibold uppercase tracking-wide text-zinc-300">{label}</h4>
      <span className="text-xs tabular-nums text-zinc-300">
        Completed {knownCompleted ? done.toLocaleString() : '—'}
        {' · '}Remaining {remaining == null ? 'until stopped' : Number(remaining).toLocaleString()}
      </span>
    </div>
    <div className="mt-2 h-2 overflow-hidden rounded-full bg-zinc-800" role="progressbar"
      aria-label={label}
      aria-valuemin={knownTotal ? 0 : undefined}
      aria-valuemax={knownTotal ? Math.max(1, total) : undefined}
      aria-valuenow={knownTotal && knownCompleted ? done : undefined}
      aria-valuetext={knownTotal && knownCompleted
        ? `${done} completed, ${Number(remaining || 0)} remaining`
        : detail || 'Progress is not available yet'}>
      {percent == null
        ? <div className={`h-full w-1/3 rounded-full opacity-70 ${color} animate-pulse`} />
        : <div className={`h-full rounded-full transition-[width] duration-500 ${color}`}
          style={{ width: `${percent}%` }} />}
    </div>
    {detail && <p className="mt-1.5 text-[11px] leading-4 text-zinc-500">{detail}</p>}
  </article>;
}

function OutcomeProgress({ wins, losses, pending, total }) {
  const settled = wins + losses;
  const denominator = Math.max(total || 0, wins + losses + pending);
  const unknown = Math.max(0, denominator - wins - losses - pending);
  const remaining = Math.max(0, denominator - settled);
  const width = (value) => denominator > 0 ? `${(value / denominator) * 100}%` : '0%';
  return <article className="rounded-lg border border-zinc-800 bg-zinc-950/45 p-3">
    <div className="flex flex-wrap items-center justify-between gap-x-4 gap-y-1">
      <h4 className="text-xs font-semibold uppercase tracking-wide text-zinc-300">SHADOW result states</h4>
      <span className="text-xs tabular-nums text-zinc-300">
        Completed {settled.toLocaleString()} · Remaining {remaining.toLocaleString()}
      </span>
    </div>
    <div className="mt-2 flex h-2 overflow-hidden rounded-full bg-zinc-800" role="img"
      aria-label={`WIN ${wins}, LOSS ${losses}, PENDING ${pending}${unknown ? `, unclassified ${unknown}` : ''}`}>
      {denominator > 0 && <>
        <span className="h-full bg-emerald-400" style={{ width: width(wins) }} />
        <span className="h-full bg-rose-400" style={{ width: width(losses) }} />
        <span className="h-full bg-amber-300" style={{ width: width(pending) }} />
        <span className="h-full bg-zinc-500" style={{ width: width(unknown) }} />
      </>}
    </div>
    <div className="mt-1.5 flex flex-wrap gap-x-4 gap-y-1 text-[11px] tabular-nums">
      <span className="text-emerald-300">WIN {wins}</span>
      <span className="text-rose-300">LOSS {losses}</span>
      <span className="text-amber-200">PENDING {pending}</span>
      {unknown > 0 && <span className="text-zinc-400">Unclassified {unknown}</span>}
      {!denominator && <span className="text-zinc-500">No selections yet</span>}
    </div>
  </article>;
}

function ViewText({ children, className = '', label = 'View' }) {
  const [expanded, setExpanded] = useState(false);
  return <div className={className}>
    <button type="button" aria-expanded={expanded} onClick={() => setExpanded((value) => !value)}
      className="inline-flex text-xs font-semibold text-sky-300 underline decoration-sky-500/50 underline-offset-4 hover:text-sky-100 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sky-400">
      {expanded ? 'Hide' : label}
    </button>
    {expanded && <div className="mt-2">{children}</div>}
  </div>;
}

function Button({ children, onClick, disabled = false, tone = 'neutral', className = '' }) {
  const colors = {
    danger: 'border-rose-500/40 bg-rose-500/10 text-rose-200 hover:border-rose-400/70 hover:bg-rose-500/20 focus-visible:ring-rose-400/50',
    good: 'border-emerald-400/40 bg-emerald-400/10 text-emerald-100 shadow-sm shadow-emerald-950/30 hover:border-emerald-300/70 hover:bg-emerald-400/20 focus-visible:ring-emerald-300/50',
    info: 'border-sky-400/40 bg-sky-400/10 text-sky-100 hover:border-sky-300/70 hover:bg-sky-400/20 focus-visible:ring-sky-300/50',
    warning: 'border-amber-400/40 bg-amber-400/10 text-amber-100 hover:border-amber-300/70 hover:bg-amber-400/20 focus-visible:ring-amber-300/50',
    reset: 'border-violet-400/40 bg-violet-400/10 text-violet-100 hover:border-violet-300/70 hover:bg-violet-400/20 focus-visible:ring-violet-300/50',
    neutral: 'border-zinc-700 bg-zinc-800/40 text-zinc-200 hover:border-zinc-500 hover:bg-zinc-800 focus-visible:ring-zinc-400/50',
  }[tone] || 'border-zinc-700 bg-zinc-800/40 text-zinc-200 hover:border-zinc-500 hover:bg-zinc-800 focus-visible:ring-zinc-400/50';
  return <button type="button" onClick={onClick} disabled={disabled}
    className={`rounded-lg border px-3 py-2 text-sm font-semibold transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-offset-2 focus-visible:ring-offset-zinc-950 disabled:cursor-not-allowed disabled:opacity-40 ${colors} ${className}`}>
    {children}
  </button>;
}

function SelectionPreviewPanel({ preview, rules, selectionMode }) {
  if (!preview || preview.selection_mode !== selectionMode) return null;
  const ruleById = new Map((rules || []).map((rule) => [rule.rule_id, rule]));
  const selectedIds = new Set(preview.selected_rule_ids || []);
  return <div className={`mt-3 rounded-lg border p-3 ${selectionMode === 'adaptive'
    ? 'border-violet-400/25 bg-violet-950/15' : 'border-emerald-400/25 bg-emerald-950/15'}`}>
    <div className="flex flex-wrap items-center justify-between gap-2">
      <h4 className="text-xs font-bold uppercase tracking-wide text-zinc-200">
        {selectionMode === 'adaptive' ? 'Adaptive live selection preview' : 'Automatic live selection preview'}
      </h4>
      <span className="rounded-full border border-zinc-600 px-2.5 py-1 text-[10px] font-semibold text-zinc-200">
        {preview.status === 'READY' ? `SOURCE #${preview.source_round?.round_index ?? '—'}` : 'WAITING FOR VERIFIED ROUND'}
      </span>
    </div>
    <p className="mt-2 text-xs text-zinc-300">
      {preview.status === 'READY'
        ? `${selectedIds.size} rule${selectedIds.size === 1 ? '' : 's'} in the current preview · ${preview.reason || 'No selection reason reported'} · ${preview.source_round?.outcome || '—'} at #${preview.source_round?.round_index ?? '—'}.`
        : 'Rules and performance will be scored as soon as a fresh verified round is available.'}
      {' '}This is a read-only preview; the selector rechecks on every verified SHADOW round.
    </p>
    {preview.candidates?.length > 0 && <div className="mt-3 grid gap-2 sm:grid-cols-2 xl:grid-cols-3">
      {preview.candidates.map((candidate) => {
        const selected = selectedIds.has(candidate.rule_id);
        const rule = ruleById.get(candidate.rule_id);
        const window = candidate.rolling_50 || {};
        const wins = candidate.wins ?? window.wins;
        const losses = candidate.losses ?? window.losses;
        const net = candidate.net_wins ?? window.net;
        const progress = candidate.window_rounds != null && candidate.window_size
          ? `${candidate.window_rounds}/${candidate.window_size}` : null;
        const statusLabel = selected ? (candidate.evidence_status === 'EXPLORATORY'
          ? 'EXPLORATORY PICK' : 'SELECTED') : candidate.reason || 'OBSERVING';
        return <article key={candidate.rule_id} className="rounded-md border border-zinc-700/80 bg-zinc-950/60 p-2.5">
          <div className="flex items-start justify-between gap-2">
            <div className="min-w-0">
              <p className="truncate text-xs font-semibold text-zinc-100">{rule?.name || candidate.rule_id}</p>
              <code className="text-[10px] text-zinc-500">{candidate.rule_id}</code>
            </div>
            <span className={`shrink-0 rounded px-2 py-0.5 text-[9px] font-bold ${selected
              ? selectionMode === 'adaptive' ? 'bg-violet-400/15 text-violet-200' : 'bg-emerald-400/15 text-emerald-200'
              : 'bg-zinc-800 text-zinc-400'}`}>{statusLabel}</span>
          </div>
          <p className="mt-2 text-[10px] leading-4 text-zinc-300">{candidate.reason || 'Awaiting evidence'}</p>
          <div className="mt-2 flex flex-wrap gap-x-3 gap-y-1 text-[10px] text-zinc-400">
            {wins != null && <span>{wins}W / {losses ?? 0}L · net {net ?? '—'}</span>}
            {progress && <span>Window {progress}</span>}
            {candidate.performance_trend && <span>Outcome {candidate.performance_trend}</span>}
            {candidate.signal_trend && <span>Signal {candidate.signal_trend}</span>}
            {candidate.score != null && <span>Score {candidate.score}</span>}
            {candidate.confidence != null && <span>Confidence {candidate.confidence}</span>}
            {candidate.stability != null && <span>Stability {candidate.stability}</span>}
          </div>
        </article>;
      })}
    </div>}
    {!preview.candidates?.length && <p className="mt-2 text-xs text-zinc-500">No candidate details are available yet.</p>}
  </div>;
}

export default function RulesEngine() {
  const [initialDraft] = useState(readRulesManagerDraft);
  const [status, setStatus] = useState(null);
  const [collectorSnapshot, setCollectorSnapshot] = useState(null);
  const [collectorReadAt, setCollectorReadAt] = useState(null);
  const collectorReadInFlight = useRef(null);
  const collector = collectorSnapshot || status?.collector_status || {};
  const roundHistory = collector.round_history_health || {};
  const roundStream = collector.round_stream || {};
  const browserObservedAtMs = Date.parse(roundHistory.lastObservationAt || '');
  const browserObservationAgeSeconds = Number.isFinite(browserObservedAtMs)
    ? Math.max(0, (Date.now() - browserObservedAtMs) / 1000) : null;
  const collectorStorage = collector.storage || {};
  const localJournal = collectorStorage.local_journal || {};
  const postgresStorage = collectorStorage.postgres || {};
  const [dashboard, setDashboard] = useState(null);
  const [registry, setRegistry] = useState(null);
  const [sessions, setSessions] = useState([]);
  const [selectedFinishedSessionIds, setSelectedFinishedSessionIds] = useState(() => new Set());
  const [detailSession, setDetailSession] = useState(null);
  const [historyDetail, setHistoryDetail] = useState(null);
  const refreshSequence = useRef(0);
  const refreshInFlight = useRef(null);
  const dashboardRefreshInFlight = useRef(null);
  const auxiliaryRefreshAt = useRef(0);
  const sessionDetailRefresh = useRef({ sessionId: null, at: 0 });
  const focusedSessionIdRef = useRef(null);
  const [focusedSessionId, setFocusedSessionId] = useState(null);
  const [mode, setMode] = useState(() => Object.hasOwn(MODE_LABELS, initialDraft.mode) ? initialDraft.mode : 'ALL_RULES_TOGETHER');
  const [ruleSelectionMode, setRuleSelectionMode] = useState(() => {
    if (initialDraft.rule_selection_mode === 'behaviour') return 'behaviour';
    if (initialDraft.rule_selection_mode === 'adaptive') return 'adaptive';
    if (initialDraft.rule_selection_mode === 'automatic') return 'automatic';
    if (initialDraft.rule_selection_mode === 'manual') return 'manual';
    return Object.values(storedSelections(initialDraft.selected_ids_by_mode))
      .some((ids) => ids.length > 0) ? 'manual' : 'automatic';
  });
  const [selectedIdsByMode, setSelectedIdsByMode] = useState(() => storedSelections(initialDraft.selected_ids_by_mode));
  const selectedIds = Object.hasOwn(selectedIdsByMode, mode)
    ? selectedIdsByMode[mode]
    : sessions.find((session) => session.mode === mode
      && session.automatic_rule_selection !== true && session.adaptive_rule_selection !== true
      && Array.isArray(session.rule_ids))?.rule_ids || [];
  const [error, setError] = useState('');
  const [performanceResetNotice, setPerformanceResetNotice] = useState('');
  const [rulePerformanceReport, setRulePerformanceReport] = useState(null);
  const [loadError, setLoadError] = useState('');
  const [busy, setBusy] = useState(false);
  const [busyAction, setBusyAction] = useState('');
  const [appliedSelectionKey, setAppliedSelectionKey] = useState(() =>
    typeof initialDraft.applied_selection_key === 'string' ? initialDraft.applied_selection_key : '');
  const [selectionApplyNotice, setSelectionApplyNotice] = useState(null);
  const [selectionPreview, setSelectionPreview] = useState(null);
  const [selectionPreviewError, setSelectionPreviewError] = useState('');
  const operationInFlightRef = useRef(false);
  const [presetName, setPresetName] = useState('');
  const [presetChoice, setPresetChoice] = useState('');
  const [stake, setStake] = useState(() => storedValue(initialDraft.stake, 100));
  const [winEnabled, setWinEnabled] = useState(() => storedBoolean(initialDraft.win_enabled, false));
  const [winCount, setWinCount] = useState(() => storedValue(initialDraft.win_count, 1));
  const [lossEnabled, setLossEnabled] = useState(() => storedBoolean(initialDraft.loss_enabled, false));
  const [lossCount, setLossCount] = useState(() => storedValue(initialDraft.loss_count, 2));
  const [maxSelections, setMaxSelections] = useState(() => storedValue(initialDraft.max_selections, ''));
  const initialRunUntilStopped = storedBoolean(initialDraft.run_until_stopped,
    initialDraft.max_observed_rounds === null);
  const [maxObservedRounds, setMaxObservedRounds] = useState(() => storedValue(initialDraft.max_observed_rounds, 50));
  const [runUntilStopped, setRunUntilStopped] = useState(() => initialRunUntilStopped);
  const [autoNext, setAutoNext] = useState(() => storedBoolean(initialDraft.auto_next, false)
    && !initialRunUntilStopped);
  const [autoNextOnLimit, setAutoNextOnLimit] = useState(() =>
    storedBoolean(initialDraft.auto_next_on_limit, false));
  const [autoNextLimitsEnabled, setAutoNextLimitsEnabled] = useState(() =>
    storedBoolean(initialDraft.auto_next_limits_enabled, false));
  const [autoNextWinCount, setAutoNextWinCount] = useState(() =>
    storedValue(initialDraft.auto_next_win_count, ''));
  const [autoNextLossCount, setAutoNextLossCount] = useState(() =>
    storedValue(initialDraft.auto_next_loss_count, ''));
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
        rule_selection_mode: ruleSelectionMode,
        selected_ids_by_mode: selectedIdsByMode,
        applied_selection_key: appliedSelectionKey,
        stake,
        win_enabled: winEnabled,
        win_count: winCount,
        loss_enabled: lossEnabled,
        loss_count: lossCount,
        max_selections: maxSelections,
        max_observed_rounds: runUntilStopped ? null : maxObservedRounds,
        run_until_stopped: runUntilStopped,
        auto_next: autoNext,
        auto_next_on_limit: autoNextOnLimit,
        auto_next_limits_enabled: autoNextLimitsEnabled,
        auto_next_win_count: autoNextWinCount,
        auto_next_loss_count: autoNextLossCount,
        profit_enabled: profitEnabled,
        profit_goal: profitGoal,
      }));
      setDraftSaveState('saved');
    } catch {
      setDraftSaveState('unavailable');
    }
  }, [mode, ruleSelectionMode, selectedIdsByMode, stake, winEnabled, winCount, lossEnabled,
    lossCount, maxSelections, maxObservedRounds, runUntilStopped, autoNext, autoNextOnLimit, autoNextLimitsEnabled,
    autoNextWinCount, autoNextLossCount, profitEnabled, profitGoal, appliedSelectionKey]);

  const refreshDashboard = useCallback(() => {
    if (dashboardRefreshInFlight.current) return dashboardRefreshInFlight.current;
    let request;
    request = api.get('/api/rules-engine/dashboard')
      .then((response) => setDashboard(response.data))
      .catch(() => setDashboard(null))
      .finally(() => {
        if (dashboardRefreshInFlight.current === request) dashboardRefreshInFlight.current = null;
      });
    dashboardRefreshInFlight.current = request;
    return request;
  }, []);

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
        // Dashboard readiness can involve a live browser read and take seconds.
        // Keep it current without putting it on the critical control/status path.
        void refreshDashboard();
        const jobs = [api.get('/api/rules-engine/status')];
        if (refreshAuxiliary) {
          jobs.push(api.get('/api/rules-engine/registry'), api.get('/api/rules-engine/sessions'));
        }
        const results = await Promise.allSettled(jobs);
        const [statusResult] = results;
        const registryResult = refreshAuxiliary ? results[1] : null;
        const sessionsResult = refreshAuxiliary ? results[2] : null;
        if (requestId !== refreshSequence.current) return;
        const failures = [];
        const statusData = statusResult.status === 'fulfilled' ? statusResult.value.data : null;
        const sessionsData = sessionsResult?.status === 'fulfilled' ? sessionsResult.value.data : null;
        if (statusData) {
          setStatus(statusData);
          if (statusData.active_session) {
          }
        } else failures.push(statusRequestError(statusResult.reason, 'Rules Engine status'));
        if (registryResult?.status === 'fulfilled') setRegistry(registryResult.value.data);
        else if (registryResult) failures.push(statusRequestError(registryResult.reason, 'Rules registry'));
        if (sessionsData) setSessions(sessionsData.sessions || []);
        else if (sessionsResult) failures.push(statusRequestError(sessionsResult.reason, 'Rules Engine sessions'));
        const focusedSessionId = focusedSessionIdRef.current;
        const focusedSession = focusedSessionId
          ? (statusData?.sessions || sessionsData?.sessions || [])
            .find((session) => session.session_id === focusedSessionId)
          : null;
        const selectedSession = statusData?.active_session || focusedSession
          || statusData?.latest_session || sessionsData?.sessions?.[0];
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
  }, [refreshDashboard]);

  const refreshCollectorSnapshot = useCallback(() => {
    if (collectorReadInFlight.current) return collectorReadInFlight.current;
    let request;
    request = api.get('/api/history/status')
      .then(({ data }) => {
        if (data && typeof data === 'object') {
          setCollectorSnapshot(data);
          setCollectorReadAt(new Date().toISOString());
        }
      })
      .catch(() => {
        // Keep the last successful collector report. Rules Engine readiness
        // has a larger database read path and must not erase source health.
      })
      .finally(() => {
        if (collectorReadInFlight.current === request) collectorReadInFlight.current = null;
      });
    collectorReadInFlight.current = request;
    return request;
  }, []);

  useEffect(() => {
    refreshCollectorSnapshot();
    const timer = window.setInterval(refreshCollectorSnapshot, 5000);
    const onVisibilityChange = () => {
      if (document.visibilityState === 'visible') refreshCollectorSnapshot();
    };
    document.addEventListener('visibilitychange', onVisibilityChange);
    return () => {
      window.clearInterval(timer);
      document.removeEventListener('visibilitychange', onVisibilityChange);
    };
  }, [refreshCollectorSnapshot]);

  useEffect(() => {
    refresh();
    const timer = window.setInterval(refresh, 15000);
    const refreshVisible = () => {
      if (document.visibilityState === 'visible') void refresh();
    };
    window.addEventListener('focus', refreshVisible);
    document.addEventListener('visibilitychange', refreshVisible);
    return () => {
      window.clearInterval(timer);
      window.removeEventListener('focus', refreshVisible);
      document.removeEventListener('visibilitychange', refreshVisible);
    };
  }, [refresh]);

  useEffect(() => {
    let refreshTimer = null;
    const scheduleRefresh = () => {
      if (refreshTimer) window.clearTimeout(refreshTimer);
      refreshTimer = window.setTimeout(() => {
        refreshTimer = null;
        void refresh();
      }, 300);
    };
    const unsubscribe = subscribeLiveEvents((event) => {
      if (event?.type === 'rules-engine:evaluated') scheduleRefresh();
    });
    return () => {
      if (refreshTimer) window.clearTimeout(refreshTimer);
      unsubscribe();
    };
  }, [refresh]);

  const modeDefinition = useMemo(() => registry?.modes?.find((item) => item.id === mode), [registry, mode]);
  const availableRules = useMemo(() => {
    const ids = new Set(modeDefinition?.rule_ids || []);
    return (registry?.rules || []).filter((rule) => ids.has(rule.rule_id));
  }, [modeDefinition, registry]);
  useEffect(() => {
    if (ruleSelectionMode !== 'automatic') {
      setRulePerformanceReport(null);
      return undefined;
    }
    let cancelled = false;
    let inFlight = false;
    let timer;
    const refreshRulePerformance = async () => {
      if (cancelled || inFlight) return;
      inFlight = true;
      try {
        const response = await api.get('/api/rules-engine/rule-performance', {
          timeout: RULE_MANAGER_WRITE_TIMEOUT_MS,
        });
        if (!cancelled) setRulePerformanceReport(response.data);
      } catch {
        // Existing chooser details remain visible if this auxiliary report is
        // temporarily unavailable; the next poll will retry it.
      } finally {
        inFlight = false;
        if (!cancelled) timer = window.setTimeout(refreshRulePerformance, 15000);
      }
    };
    void refreshRulePerformance();
    return () => { cancelled = true; window.clearTimeout(timer); };
  }, [ruleSelectionMode]);
  const automaticCandidateRules = availableRules;
  const presets = useMemo(() => Object.values(status?.presets || {}), [status]);
  const activeSession = status?.active_session;
  useEffect(() => {
    const activeSessionId = activeSession?.session_id;
    if (!activeSessionId || activeSessionId === focusedSessionIdRef.current) return;
    focusedSessionIdRef.current = activeSessionId;
    setFocusedSessionId(activeSessionId);
  }, [activeSession?.session_id]);
  const focusedSession = focusedSessionId
    ? (activeSession?.session_id === focusedSessionId ? activeSession
      : sessions.find((session) => session.session_id === focusedSessionId)
        || (status?.latest_session?.session_id === focusedSessionId ? status.latest_session : null))
    : null;
  const fallbackPausedSession = !focusedSessionId
    ? sessions.find((session) => session.status === 'PAUSED')
      || (status?.latest_session?.status === 'PAUSED' ? status.latest_session : null)
    : null;
  const controlSession = activeSession
    || (focusedSession?.status === 'PAUSED' ? focusedSession : null)
    || fallbackPausedSession;
  const restartableSession = sessions.find((session) => session.mode === mode
    && session.status === 'PAUSED' && session.selection_status === 'ACTIVE'
    && RESTARTABLE_INTEGRITY_STOPS.has(session.stop_reason))
    || (status?.latest_session?.mode === mode && status.latest_session.status === 'PAUSED'
      && status.latest_session.selection_status === 'ACTIVE'
      && RESTARTABLE_INTEGRITY_STOPS.has(status.latest_session.stop_reason)
      ? status.latest_session : null);
  const displaySession = activeSession || focusedSession || controlSession
    || status?.latest_session || sessions[0] || null;
  const displaySessionRunsUntilStopped = displaySession?.run_until_stopped === true
    || (displaySession?.run_until_stopped == null
      && Object.hasOwn(displaySession || {}, 'window_size')
      && displaySession.window_size === null);
  const selectionModeNeedsApply = Boolean(controlSession) && !isRuleSelectionApplied(controlSession, {
    mode, selectionMode: ruleSelectionMode, selectedIds, availableRules,
  });
  const selectionRequest = ruleSelectionRequest(ruleSelectionMode, selectedIds, availableRules);
  const selectionDraftKey = JSON.stringify({ mode, selectionMode: ruleSelectionMode,
    ruleIds: selectionRequest.rule_ids });
  const selectionDraftApplied = appliedSelectionKey === selectionDraftKey;
  useEffect(() => {
    // Keep Automatic's established apply/start flow independent from the
    // Adaptive-only preview endpoint.
    if (!selectionDraftApplied || ruleSelectionMode !== 'adaptive') {
      setSelectionPreview(null);
      setSelectionPreviewError('');
      return undefined;
    }
    let cancelled = false;
    const updatePreview = async () => {
      try {
        const response = await api.post('/api/rules-engine/rule-selection/preview', {
          mode, selection_mode: ruleSelectionMode,
        }, { timeout: RULE_MANAGER_WRITE_TIMEOUT_MS });
        if (!cancelled) {
          setSelectionPreview(response.data);
          setSelectionPreviewError('');
        }
      } catch (failure) {
        if (!cancelled) setSelectionPreviewError(statusRequestError(failure, 'Rule selection preview'));
      }
    };
    void updatePreview();
    const timer = window.setInterval(updatePreview, 15000);
    return () => { cancelled = true; window.clearInterval(timer); };
  }, [mode, ruleSelectionMode, selectionDraftKey, selectionDraftApplied]);
  const manualSelectionApplied = isRuleSelectionApplied(controlSession, {
    mode, selectionMode: 'manual', selectedIds, availableRules,
  });
  const automaticPerformanceWindows = new Map((rulePerformanceReport?.rules || [])
    .map((row) => [row.rule_id, row.latest_window || null]));
  const automaticCandidates = new Map((displaySession?.automatic_rule_choice?.candidates || [])
    .map((candidate) => {
      const liveWindow = automaticPerformanceWindows.get(candidate.rule_id);
      if (!liveWindow) return [candidate.rule_id, candidate];
      const decisionPerformanceRound = Number(candidate.performance_round_index);
      const currentPerformanceRound = Number(liveWindow.last_round_index);
      return [candidate.rule_id, {
        ...candidate,
        last_choice_performance_round_index: candidate.performance_round_index,
        last_choice_source_round_index: displaySession.automatic_rule_choice?.source_round_index,
        decision_performance_lag_rounds: Number.isFinite(decisionPerformanceRound)
          && Number.isFinite(currentPerformanceRound)
          ? Math.max(0, currentPerformanceRound - decisionPerformanceRound) : null,
        window_rounds: candidate.window_rounds ?? liveWindow.observed_rounds,
        window_size: candidate.window_size ?? liveWindow.window_size,
        wins: candidate.wins ?? liveWindow.wins,
        losses: candidate.losses ?? liveWindow.losses,
        net_wins: candidate.net_wins ?? liveWindow.net,
        recovered_rounds: candidate.recovered_rounds ?? liveWindow.recovered_rounds,
      }];
    }));
  const displayedSessionConfig = detailSession && displaySession
    && detailSession.session_id === displaySession.session_id
    ? detailSession.configuration_snapshot || {} : {};
  const savedSessionStops = { ...displayedSessionConfig, ...(displaySession || {}) };
  const savedStopLimitLabels = [
    savedSessionStops.win_limit_enabled === true ? `WIN ${savedSessionStops.win_limit_count}` : null,
    savedSessionStops.loss_limit_enabled === true ? `LOSS ${savedSessionStops.loss_limit_count}` : null,
    savedSessionStops.profit_target_enabled === true
      ? `profit ${savedSessionStops.profit_target_rwf} RWF` : null,
    savedSessionStops.max_selections != null ? `${savedSessionStops.max_selections} selections` : null,
  ].filter(Boolean);
  const latestSessionEvaluation = detailSession && displaySession
    && detailSession.session_id === displaySession.session_id
    ? detailSession.evaluations?.at(-1) ?? null : null;
  const latestRuleRuntime = new Map((latestSessionEvaluation?.rule_evaluations || [])
    .map((check) => [check.rule_id, check.state_snapshot?.after || {}]));
  const automaticChoice = displaySession?.automatic_rule_choice || null;
  const automaticChoiceCandidates = Array.isArray(automaticChoice?.candidates)
    ? automaticChoice.candidates : [];
  const adaptiveChoice = displaySession?.adaptive_rule_choice || null;
  const adaptiveCandidates = Array.isArray(adaptiveChoice?.candidates) ? adaptiveChoice.candidates : [];
  const adaptiveSelectedCandidates = adaptiveCandidates.filter((candidate) => candidate.selected === true);
  const adaptiveCandidateMap = new Map(adaptiveCandidates.map((candidate) => [candidate.rule_id, candidate]));
  const adaptiveRulePerformance = displaySession?.adaptive_rule_performance || {};
  const displaySessionIsActive = ['RUNNING', 'SETTLING'].includes(displaySession?.status);
  const automaticChoiceRuleIds = Array.isArray(automaticChoice?.rule_ids)
    ? automaticChoice.rule_ids
    : Array.isArray(displaySession?.active_rule_ids) ? displaySession.active_rule_ids : [];
  const autoNextCheckEnabled = displaySession?.auto_next_check_enabled
    ?? displayedSessionConfig.auto_next_check_enabled;
  const autoNextOnLimitEnabled = displaySession?.auto_next_on_limit_enabled
    ?? displayedSessionConfig.auto_next_on_limit_enabled;
  const autoNextCheckIsOn = autoNextCheckEnabled === true || autoNextOnLimitEnabled === true;
  const autoNextCheckIsOff = autoNextCheckEnabled === false && autoNextOnLimitEnabled === false;
  const configuredWindowRounds = runUntilStopped ? null
    : Math.max(1, Number.parseInt(maxObservedRounds, 10) || 50);
  const autoNextWindowRounds = displaySession?.window_size !== undefined
    ? displaySession.window_size
    : Object.hasOwn(displayedSessionConfig, 'window_size')
      ? displayedSessionConfig.window_size : configuredWindowRounds;
  const autoNextTriggerLabel = autoNextCheckEnabled === true && autoNextOnLimitEnabled === true
    ? (autoNextWindowRounds == null ? 'Configured limit' : 'Limit immediately; full-window fallback')
    : autoNextCheckEnabled === true ? (autoNextWindowRounds == null
      ? 'Full-window trigger unavailable' : `Full ${autoNextWindowRounds}-round window`)
      : autoNextOnLimitEnabled === true ? 'Configured limit'
        : autoNextCheckIsOff ? 'Off' : 'Unknown';
  const savedAutoNextRuleLimits = displaySession?.auto_next_limits_enabled === true
    ? ` · custom WIN ${displaySession.auto_next_win_count ?? 'OFF'} / LOSS ${displaySession.auto_next_loss_count ?? 'OFF'}`
    : '';
  const current = collectorSnapshot?.latest || status?.latest_real_round;
  const latestDatabaseRound = current || collector.latest || status?.collector_status?.latest || null;
  const latestDatabaseRoundVerified = latestDatabaseRound?.continuity_verified === true;
  const latestPersistedRoundId = roundHistory.latestPersistedRoundId || latestDatabaseRound?.round_id || null;
  const latestPersistedAt = roundHistory.lastPersistedAt || latestDatabaseRound?.stored_at
    || latestDatabaseRound?.timestamp || null;
  // A database row may be hours old while still being historically verified.
  // Keep it out of the live metric; the Hub field is populated only by a
  // continuity-verified LIVE Orbit event beyond the live boundary.
  const latestVerifiedMultiplier = roundStream.latest_multiplier ?? null;
  const liveRoundAgeSeconds = Number.isFinite(Number(roundStream.observed_age_seconds))
    ? Number(roundStream.observed_age_seconds) : null;
  const liveRoundCadenceP95Seconds = Number.isFinite(Number(roundStream.freshness_limit_ms))
    ? Number(roundStream.freshness_limit_ms) / 1000 : null;
  const receivedRound = detailSession?.last_received_round || displaySession?.last_received_round;
  const lastSettledTarget = detailSession?.last_settled_target || displaySession?.last_settled_target;
  const eventLatencyMetrics = status?.event_latency_metrics?.stages_ms || {};
  const settlementLatencySummary = eventLatencyMetrics.target_settlement_durable_ms || {};
  const shadowReadiness = status?.shadow_readiness;
  const collectorState = collector.state || collector.health || collector.status || 'UNKNOWN';
  const backlogValue = collector.storage?.round_delivery?.backlog_size;
  const backlogKnown = backlogValue !== null && backlogValue !== undefined && Number.isFinite(Number(backlogValue));
  const pendingRoundDeliveries = backlogKnown ? Number(backlogValue) : null;
  const deliveryFailure = collector.storage?.round_delivery?.last_failure;
  const backendStatus = String(collector.backend?.status || 'UNKNOWN').toUpperCase();
  const orbit = collector.orbit_api || {};
  const latestDeliveryTiming = collector.storage?.round_delivery?.last_round_timing;
  const latestTiming = latestDeliveryTiming?.timestamps || {};
  const mutationAtMs = Date.parse(latestTiming.mutation_observed_at || '');
  const extractionStartedAtMs = Date.parse(latestTiming.extraction_started_at || '');
  const publicationAtMs = Date.parse(latestTiming.persisted_event_acknowledged_at || '');
  const databaseCommitAtMs = Date.parse(latestTiming.database_commit_acknowledged_at || '');
  const sourceAtMs = Number.isFinite(mutationAtMs) ? mutationAtMs : extractionStartedAtMs;
  const completionAtMs = Number.isFinite(publicationAtMs) ? publicationAtMs : databaseCommitAtMs;
  const measuredCollectorLatencyMs = Number.isFinite(sourceAtMs) && Number.isFinite(completionAtMs)
    ? Math.max(0, completionAtMs - sourceAtMs) : null;
  const collectorLatencyLabel = Number.isFinite(mutationAtMs) ? 'Mutation to publish' : 'Extraction to DB commit';
  const orbitSourceStatus = String(orbit.sourceStatus || 'UNKNOWN').toUpperCase();
  const orbitSourceUnhealthy = orbit.status === 'CONNECTED' && orbitSourceStatus !== 'HEALTHY';
  const selectorMatchSummary = orbit.latestReadingMatchCount != null && Number.isFinite(Number(orbit.latestReadingMatchCount))
    ? orbit.latestReadingWindowSize != null
      ? `Latest selector window ${orbit.latestReadingReturnedCount ?? '—'}/${orbit.latestReadingMatchCount}`
      : `Selector matches ${orbit.latestReadingReturnedCount ?? '—'}/${orbit.latestReadingMatchCount}` : null;
  const selectorDiagnostics = roundHistory.selectorDiagnostics || {};
  const selectorCandidateSummary = selectorDiagnostics.reason === 'IDENTICAL_VISIBLE_ORDERED_CONTAINERS'
    ? `${selectorDiagnostics.accepted_containers?.length ?? 0} identical visible containers reconciled by ordered consensus`
    : selectorDiagnostics.reason
      ? `Selector candidates: ${selectorDiagnostics.reason.replaceAll('_', ' ').toLowerCase()}` : null;
  const collectorMetricDetail = [
    selectorMatchSummary,
    selectorCandidateSummary,
    orbit.latestReadingValidationError || roundHistory.lastIntegrityReason || null,
    backendStatus !== 'AVAILABLE' ? `Round delivery backend ${backendStatus}` : null,
    collectorReadAt ? `Health checked ${shortTime(collectorReadAt)}` : null,
  ].filter(Boolean).join(' · ');
  const roundDeliveryStatus = [
    pendingRoundDeliveries > 0
      ? `${pendingRoundDeliveries} recent game result${pendingRoundDeliveries === 1 ? ' is' : 's are'} waiting to be saved.` : '',
    backendStatus !== 'AVAILABLE' ? 'The app has not yet confirmed that recent results were received.' : '',
    collector.auth_required ? 'The game site needs you to sign in again before results can be collected.' : '',
    deliveryFailure?.category
      ? deliveryFailure.category === 'POSTGRES_NOT_READY' || deliveryFailure.category === 'POSTGRES_COMMIT_UNCONFIRMED'
        ? 'Some recent results could not be saved yet. They are being kept and will be retried automatically.'
        : 'The app is retrying delivery of recent game results.'
      : '',
  ].filter(Boolean).join(' ');
  const staleFeedDetail = [
    roundDeliveryStatus,
    'Your session has not started. Wait for a new game result, then try again.',
  ].filter(Boolean).join(' ');
  const verifiedBoundaryDetail = shadowReadiness?.blockers?.includes('VERIFIED_COMMITTED_ROUND_REQUIRED')
    ? [
      'The latest game result has not finished saving and verification yet.',
      orbitSourceUnhealthy ? orbitSourceRecoveryMessage(orbit) : '',
      roundDeliveryStatus,
      'Try again after a new result appears.',
    ].filter(Boolean).join(' ')
    : '';
  const blockerHintDetail = [
    ...(shadowReadiness?.blockers?.includes('SHADOW_ROUND_FEED_STALE') ? [staleFeedDetail] : []),
    ...(verifiedBoundaryDetail ? [verifiedBoundaryDetail] : []),
  ].join(' ');
  const hasSelectedEnabledRule = ruleSelectionMode === 'behaviour'
    || ((ruleSelectionMode === 'automatic' || ruleSelectionMode === 'adaptive')
      && automaticCandidateRules.length > 0)
    || (ruleSelectionMode === 'manual'
      && selectedIds.some((id) => availableRules.find((rule) => rule.rule_id === id)?.enabled));
  const startHint = activeSession
    ? 'A SHADOW session is already active. Stop it before starting a new session with the applied settings.'
    : restartableSession
      ? restartableSession.stop_reason === 'ROUND_INDEX_GAP'
        ? 'A game result was missed, so this session will skip ahead and continue when new results arrive. Pending results will still be completed.'
        : !shadowReadiness || shadowReadiness.ready === false
        ? `The previous session paused and cannot safely continue from the current results. ${!shadowReadiness
          ? 'The app is still checking whether it is safe to start.'
          : explainShadowBlockers(shadowReadiness.blockers || [], orbit)} ${blockerHintDetail} Its history and pending results are saved.`
        : `The previous session paused and cannot safely continue. Start a new SHADOW session; its history and pending results will be preserved.${hasSelectedEnabledRule ? '' : ruleSelectionMode === 'manual' ? ' No rules are selected, so this session will record no rule decisions.' : ' No policies are available in this selection mode.'}`
    : !shadowReadiness
      ? 'Checking SHADOW readiness…'
      : !shadowReadiness.ready
        ? `We couldn't start this SHADOW session yet. ${explainShadowBlockers(shadowReadiness.blockers || [], orbit)}${blockerHintDetail ? ` ${blockerHintDetail}` : ''}`
          : ruleSelectionMode === 'behaviour'
            ? 'SHADOW readiness is clear. Behaviour Detection will submit only eligible future-round HIGH signals.'
          : !hasSelectedEnabledRule
          ? ruleSelectionMode === 'automatic'
            ? 'No policies are available in this Automatic mode.'
            : 'No enabled rules are selected. The manual session can start, but it will record no rule decisions.'
          : ruleSelectionMode === 'automatic'
            ? 'SHADOW readiness is clear. Automatic selection will choose a rule only when verified performance supports it.'
            : 'SHADOW readiness is clear. Starting will use your selected enabled rules.';

  const withBusy = async (operation, action = 'request', { refreshAfter = true } = {}) => {
    // React state updates land after this event handler returns. Lock in the
    // same tick so rapid clicks cannot enqueue duplicate session actions.
    if (operationInFlightRef.current) return;
    operationInFlightRef.current = true;
    setError('');
    setBusy(true);
    setBusyAction(action);
    try {
      const result = await operation();
      if (refreshAfter) await refresh(true);
      return result;
    } catch (failure) {
      const detail = failure?.response?.data?.detail;
      const backendMessage = typeof detail === 'string' ? detail
        : detail?.reason || detail?.message || failure?.response?.data?.error
        || 'The Rules Engine request failed.';
      const message = typeof backendMessage === 'string'
        && backendMessage.startsWith('SHADOW_SESSION_START_BLOCKED:')
        ? 'We could not start the session because one or more checks are still in progress. Your session has not started. See the message below the Start button for what to do next.'
        : backendMessage === 'RULE_PERFORMANCE_OBSERVER_NOT_READY'
        ? 'Wait for a verified game round before restarting the 100-round performance window.'
        : backendMessage === 'RULE_PERFORMANCE_OBSERVERS_OUT_OF_SYNC'
        ? 'Rule performance feeds are catching up. Wait for the next verified round, then restart the window.'
        : backendMessage === 'RULE_PERFORMANCE_OBSERVER_UNAVAILABLE'
        ? 'Rule performance tracking is unavailable right now. Try again after the backend reconnects.'
        : backendMessage === 'SESSION_NOT_RESTARTABLE'
        ? 'The paused session changed before it could be replaced. Its history and pending outcomes are still saved; refresh the session list and try again.'
        : backendMessage === 'SESSION_ALREADY_ACTIVE_FOR_MODE'
          ? 'A SHADOW session for this mode is already running or settling. Check the current session panel before starting another.'
          : backendMessage;
      // A start request can be rejected because the verified boundary moved
      // after the last poll, or can time out after the server committed it.
      // Refresh authoritative readiness and active-session state either way.
      if (action === 'start-shadow') {
        try {
          const latestStatus = await api.get('/api/rules-engine/status');
          setStatus(latestStatus.data);
          if (latestStatus.data?.active_session) {
          }
        } catch { /* The regular status poll will retry if the backend is offline. */ }
      }
      setError(message);
    } finally {
      operationInFlightRef.current = false;
      setBusy(false);
      setBusyAction('');
    }
  };

  const restartRulePerformance = () => {
    setPerformanceResetNotice('');
    return withBusy(async () => {
      const response = await api.post('/api/rules-engine/rule-performance/reset', {}, {
        timeout: RULE_MANAGER_WRITE_TIMEOUT_MS,
      });
      const reset = response.data || {};
      setPerformanceResetNotice(
        `Fresh 100-round observer window started after round #${reset.reset_at_round_index}. Previous results stay saved; ${reset.active_automatic_sessions_updated || 0} Automatic and ${reset.active_adaptive_sessions_updated || 0} Adaptive session(s) will rescore on the next verified round. Rule loss pauses remain in place until a fresh entry.`);
      return reset;
    }, 'reset-rule-performance');
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
    // Enabling a rule makes it eligible for automatic selection.
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
    setRuleSelectionMode(preset.options?.adaptive_rule_selection === true ? 'adaptive'
      : preset.options?.automatic_rule_selection === true ? 'automatic' : 'manual');
    if (preset.options?.behaviour_detection_enabled === true) setRuleSelectionMode('behaviour');
    setSelectedIdsByMode((previous) => ({ ...previous, [preset.mode]: preset.rule_ids || [] }));
    const options = preset.options || {};
    setStake(options.fixed_stake_rwf ?? 100);
    setWinEnabled(Boolean(options.win_limit_enabled));
    setWinCount(options.win_limit_count ?? 1);
    setLossEnabled(Boolean(options.loss_limit_enabled));
    setLossCount(options.loss_limit_count ?? 2);
    setMaxSelections(options.max_selections ?? '');
    const presetRounds = Object.hasOwn(options, 'max_observed_rounds')
      ? options.max_observed_rounds : options.window_size ?? 50;
    const presetUnlimited = presetRounds === null;
    setRunUntilStopped(presetUnlimited);
    if (!presetUnlimited) setMaxObservedRounds(presetRounds ?? 50);
    const presetAutoNext = Boolean(options.auto_next_check_enabled) && !presetUnlimited;
    setAutoNext(presetAutoNext);
    setAutoNextOnLimit(Boolean(options.auto_next_on_limit_enabled));
    setAutoNextLimitsEnabled(Boolean(options.auto_next_limits_enabled));
    setAutoNextWinCount(options.auto_next_win_count ?? '');
    setAutoNextLossCount(options.auto_next_loss_count ?? '');
    setProfitEnabled(options.profit_target_enabled ?? true);
    setProfitGoal(options.profit_target_rwf ?? 400);
    const disabled = preset.options?.automatic_rule_selection === true ? []
      : (preset.rule_ids || []).filter((id) => !registry?.rules?.find((rule) => rule.rule_id === id)?.enabled);
    setError(disabled.length ? `Preset loaded. Enable its disabled rules before starting: ${disabled.join(', ')}` : '');
  };

  const validateCustomAutoNextLimits = () => {
    if (!runUntilStopped && (!Number.isSafeInteger(Number(maxObservedRounds))
        || Number(maxObservedRounds) < 1)) {
      setError('Maximum verified rounds must be a positive whole number, or choose “Until I stop it”.');
      return false;
    }
    if (!runUntilStopped && mode === 'ALL_RULES_TOGETHER' && autoNextLimitsEnabled) {
      const hasCount = (value) => String(value).trim() !== '';
      const validCount = (value) => !hasCount(value)
        || (Number.isInteger(Number(value)) && Number(value) >= 1);
      if (!hasCount(autoNextWinCount) && !hasCount(autoNextLossCount)) {
        setError('Enter a WIN count, a LOSS count, or both for the new automatic next-check limits.');
        return false;
      }
      if (!validCount(autoNextWinCount) || !validCount(autoNextLossCount)) {
        setError('The new WIN/LOSS limits must be whole numbers of at least 1.');
        return false;
      }
    }
    const positiveWhole = (value) => Number.isSafeInteger(Number(value)) && Number(value) >= 1;
    if (mode === 'ALL_RULES_TOGETHER'
        && ((winEnabled && !positiveWhole(winCount))
          || (lossEnabled && !positiveWhole(lossCount))
          || (String(maxSelections).trim() !== '' && !positiveWhole(maxSelections)))) {
      setError('Enabled WIN, LOSS, and selection limits must be positive whole numbers.');
      return false;
    }
    if ((mode === 'START_CHECK' || mode === 'LEGACY_SEPARATE_TRACKER')
        && profitEnabled && !positiveWhole(profitGoal)) {
      setError('The enabled profit stop must be a positive whole number.');
      return false;
    }
    return true;
  };

  const start = () => {
    if (ruleSelectionMode !== 'behaviour' && !validateCustomAutoNextLimits()) return;
    const replaceableSession = restartableSession;
    if (replaceableSession && !window.confirm(
      replaceableSession.stop_reason === 'ROUND_INDEX_GAP'
        ? 'The paused session can recover automatically on a fresh verified round. Start a separate session and archive it anyway? Its history and pending shadow outcomes will be preserved.'
        : `The previous session is paused after ${replaceableSession.stop_reason}. Start a new session and archive it? Its history and pending SHADOW outcomes will be preserved. The start will wait until uncommitted round events are saved.`
    )) {
      setError('Start cancelled. The paused session and its pending outcomes were left unchanged.');
      return;
    }
    return withBusy(async () => {
      const selection = ruleSelectionRequest(ruleSelectionMode, selectedIds, availableRules);
      const response = await api.post('/api/rules-engine/sessions', {
        mode, rule_ids: selection.rule_ids,
        ...(replaceableSession ? { replace_session_id: replaceableSession.session_id } : {}),
        options: sessionOptionsFromSettings({ mode, stake, winEnabled, winCount, lossEnabled, lossCount,
          maxSelections, maxObservedRounds, autoNext, autoNextOnLimit, autoNextLimitsEnabled,
          autoNextWinCount, autoNextLossCount, profitEnabled, profitGoal,
          automaticRuleSelection: selection.automatic_rule_selection,
          adaptiveRuleSelection: selection.adaptive_rule_selection === true,
          behaviourDetectionEnabled: ruleSelectionMode === 'behaviour',
          runUntilStopped: ruleSelectionMode === 'behaviour' || runUntilStopped }),
      }, { timeout: RULE_MANAGER_WRITE_TIMEOUT_MS });
      if (response.data?.status === 'RUNNING' && response.data?.session_id) {
        setStatus((previous) => ({ ...(previous || status || {}), status: 'RUNNING',
          active_session: response.data }));
      }
    }, 'start-shadow');
  };

  const applySelectionDraft = () => {
    if (!validateCustomAutoNextLimits()) return;
    return withBusy(async () => {
      // Only Adaptive needs a setup-time portfolio preview. Automatic chooses
      // from its normal verified-round flow after SHADOW starts.
      if (ruleSelectionMode === 'adaptive') {
        const response = await api.post('/api/rules-engine/rule-selection/preview', {
          mode, selection_mode: ruleSelectionMode,
        }, { timeout: RULE_MANAGER_WRITE_TIMEOUT_MS });
        setSelectionPreview(response.data);
        setSelectionPreviewError('');
      } else setSelectionPreview(null);
      setAppliedSelectionKey(selectionDraftKey);
      setSelectionApplyNotice({ key: selectionDraftKey, selectionMode: ruleSelectionMode });
      void refresh(true);
    }, 'apply-rule-selection', { refreshAfter: false });
  };

  const sessionAction = (sessionId, action) => {
    focusedSessionIdRef.current = sessionId;
    setFocusedSessionId(sessionId);
    return withBusy(async () => {
      const response = await api.post(
        `/api/rules-engine/sessions/${encodeURIComponent(sessionId)}/${action}`,
        undefined, { timeout: RULE_MANAGER_WRITE_TIMEOUT_MS },
      );
      const updated = response.data;
      if (updated?.session_id === sessionId) {
        setStatus((previous) => updateStatusAfterSessionAction(previous, updated));
        setSessions((previous) => updateSessionListAfterAction(previous, updated));
        setDetailSession((previous) => previous?.session_id === sessionId
          ? { ...previous, ...updated } : previous);
      }
      // Reconcile with the server in the background. The durable action response
      // already contains the authoritative summary needed by this panel.
      void refresh(true);
    }, action, { refreshAfter: false });
  };

  const viewFinishedSession = (sessionId) => withBusy(async () => {
    const response = await api.get(`/api/rules-engine/sessions/${encodeURIComponent(sessionId)}?include_history=true`);
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
      `Delete ${uniqueSessions.length} stored SHADOW session${uniqueSessions.length === 1 ? '' : 's'}?${settling ? ' Settling sessions will stop observing immediately.' : ''}${pending > 0 ? ` This includes ${pending} pending simulated outcome${pending === 1 ? '' : 's'}; they will be removed without being settled.` : ''} Pending session events and simulated results will also be removed. Canonical rounds and audit records are retained.`,
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
    if (ruleSelectionMode !== 'behaviour' && !validateCustomAutoNextLimits()) return;
    const presetRunsUntilStopped = ruleSelectionMode === 'behaviour' || runUntilStopped;
    const sessionRounds = presetRunsUntilStopped ? null : Number(maxObservedRounds);
    const options = { window_size: sessionRounds, max_observed_rounds: sessionRounds,
      automatic_rule_selection: ruleSelectionMode === 'automatic',
      ...(ruleSelectionMode === 'adaptive' ? { adaptive_rule_selection: true } : {}),
      behaviour_detection_enabled: ruleSelectionMode === 'behaviour',
      run_until_stopped: presetRunsUntilStopped,
      auto_next_check_enabled: autoNext && !presetRunsUntilStopped,
      auto_next_on_limit_enabled: autoNextOnLimit && !presetRunsUntilStopped,
      fixed_stake_rwf: Number(stake) };
    if (mode === 'ALL_RULES_TOGETHER') Object.assign(options, {
      auto_next_limits_enabled: autoNextLimitsEnabled && !presetRunsUntilStopped,
      auto_next_win_count: !presetRunsUntilStopped && autoNextLimitsEnabled && autoNextWinCount !== '' ? Number(autoNextWinCount) : null,
      auto_next_loss_count: !presetRunsUntilStopped && autoNextLimitsEnabled && autoNextLossCount !== '' ? Number(autoNextLossCount) : null,
      fixed_stake_rwf: Number(stake), win_limit_enabled: winEnabled,
      win_limit_count: winEnabled ? Number(winCount) : null,
      loss_limit_enabled: lossEnabled,
      loss_limit_count: lossEnabled ? Number(lossCount) : null,
      max_selections: maxSelections === '' ? null : Number(maxSelections),
    });
    if (mode === 'START_CHECK' || mode === 'LEGACY_SEPARATE_TRACKER') Object.assign(options, {
      profit_target_enabled: profitEnabled, profit_target_rwf: Number(profitGoal),
    });
    await api.post('/api/rules-engine/presets', { name: presetName, mode,
      rule_ids: ['automatic', 'adaptive', 'behaviour'].includes(ruleSelectionMode) ? [] : selectedIds,
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
  const autoNextLimitTargets = mode === 'ALL_RULES_TOGETHER'
    ? [
      winEnabled ? `WIN_LIMIT ${winCount || 'not set'}` : null,
      lossEnabled ? `LOSS_LIMIT ${lossCount || 'not set'}` : null,
      maxSelections !== '' ? `maximum ${maxSelections || 'not set'} selections` : null,
    ].filter(Boolean)
    : profitEnabled ? [`profit stop ${profitGoal || 'not set'} RWF`] : [];
  const autoNextLimitHelp = autoNextLimitTargets.length
    ? mode === 'ALL_RULES_TOGETHER'
      ? `Configured limits: ${autoNextLimitTargets.join(' · ')}. WIN/LOSS limits use settled net outcome (WINs − LOSSes, or LOSSes − WINs); the selection cap triggers on its final selection.`
      : `Configured limit: ${autoNextLimitTargets[0]}. It triggers after the target settles.`
    : 'No applicable limit is enabled, so this option cannot start a next check yet.';

  const progressStats = useMemo(() => {
    if (!displaySession) return null;
    const observedRaw = Number(displaySession.observed_rounds);
    const observed = Number.isFinite(observedRaw) ? Math.max(0, observedRaw) : null;
    const windowRaw = Number(displaySession.window_size);
    const windowSize = displaySession.window_size != null && Number.isFinite(windowRaw)
      ? Math.max(0, windowRaw) : null;
    const resultStates = displaySession.results || {};
    const wins = Math.max(0, Number(resultStates.WIN) || 0);
    const losses = Math.max(0, Number(resultStates.LOSS) || 0);
    const pending = Math.max(0, Number(resultStates.PENDING) || 0);
    const selectionCount = Math.max(0, Number(displaySession.selection_count) || 0);
    const displayMode = registry?.modes?.find((item) => item.id === displaySession.mode);
    const participants = displaySession.automatic_rule_selection
      ? (displayMode?.rule_ids || []) : (displaySession.rule_ids || []);
    const evaluationRows = Array.isArray(latestSessionEvaluation?.rule_evaluations)
      ? latestSessionEvaluation.rule_evaluations : null;
    const ruleTotal = Math.max(participants.length, evaluationRows?.length || 0);
    const ruleCompleted = evaluationRows ? evaluationRows.length : 0;
    const maxRaw = Number(displaySession.max_selections);
    const maxSelections = displaySession.max_selections != null
      && Number.isFinite(maxRaw) && maxRaw > 0 ? maxRaw : null;
    return { observed, windowSize, wins, losses, pending, selectionCount,
      ruleTotal, ruleCompleted, maxSelections,
      outcomeTotal: Math.max(selectionCount, wins + losses + pending) };
  }, [displaySession, latestSessionEvaluation, registry]);

  return <main className="space-y-4">
    <header className="flex flex-wrap items-end justify-between gap-4">
      <div>
        <div className="text-xs font-bold uppercase tracking-[0.18em] text-emerald-300">Shadow decision interface</div>
        <h1 className="mt-2 text-2xl font-bold text-zinc-100">Rules Engine</h1>
        <ViewText className="mt-2" label="View overview">
          <p className="max-w-3xl text-sm text-zinc-400">
            Independent rule evaluations use verified, completed history. The coordinator records selections only.
          </p>
        </ViewText>
      </div>
    </header>

    <details className="rounded-xl border border-zinc-800 bg-zinc-900/50 p-3">
      <summary className="cursor-pointer text-sm font-semibold text-zinc-200">Round performance overview · open for trends and latency detail</summary>
      <div className="mt-3"><RoundPerformanceDashboard /></div>
    </details>

    {loadError && <div role="status" className="rounded-lg border border-amber-500/30 bg-amber-500/10 px-4 py-3 text-sm text-amber-200">{loadError}</div>}
    {error && <div role="alert" className="flex items-start justify-between gap-3 rounded-lg border border-rose-500/30 bg-rose-500/10 px-4 py-3 text-sm text-rose-200">
      <span>{error}</span>
      <button type="button" onClick={() => setError('')} className="shrink-0 underline underline-offset-2">Dismiss</button>
    </div>}

    <section className={`relative overflow-hidden rounded-xl border p-3 shadow-lg shadow-black/10 ${
      controlSession?.status === 'PAUSED'
        ? 'border-amber-400/40 bg-gradient-to-br from-amber-950/40 via-zinc-900/90 to-zinc-900/70 shadow-amber-950/20'
        : controlSession?.status === 'RUNNING'
          ? 'border-emerald-400/35 bg-gradient-to-br from-emerald-950/30 via-zinc-900/90 to-zinc-900/70 shadow-emerald-950/20'
          : displaySession?.integrity_stop_reason
            ? 'border-rose-400/35 bg-gradient-to-br from-rose-950/30 via-zinc-900/90 to-zinc-900/70 shadow-rose-950/20'
            : 'border-zinc-800 bg-zinc-900/60'
    }`}>
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div><h2 className="text-base font-semibold">Rules session dashboard</h2>
          <ViewText className="mt-1">
            <p className="text-sm text-zinc-400">Each enabled rule evaluates independently. Overlapping TRUE results produce one shared SHADOW selection per target.</p>
          </ViewText>
        </div>
      </div>
      <div className="mt-3 grid gap-2 sm:grid-cols-2">
        <Metric label="Shared decision" value={dashboard?.shared_decision?.action || 'NOT_EVALUATED'}
          detail={dashboard?.shared_decision?.true_rule_ids?.join(', ')
            || dashboard?.shared_decision?.reason || 'No committed rule evaluation yet'} icon={ShieldCheck} />
        <Metric label="SHADOW startup readiness" value={shadowReadiness?.status || 'UNKNOWN'}
          detail={shadowReadiness?.blockers?.length
            ? explainShadowBlockers(shadowReadiness.blockers, orbit)
            : 'Ready for a fresh, verified game result.'} icon={Activity} />
      </div>
    </section>

    <section className="collector-panel" aria-labelledby="collector-panel-title">
      <header className="collector-panel__header">
        <div className="collector-panel__title-wrap">
          <span className="collector-panel__icon"><Database size={18} /></span>
          <div>
            <h2 id="collector-panel-title">Collector &amp; verified round history</h2>
            <p>Orbit → shared verified stream → independent module consumers → history persistence</p>
          </div>
        </div>
        <span className="collector-state-badge" data-state={String(collectorState).toLowerCase()}>
          <span className="collector-state-badge__dot" />{String(collectorState).replaceAll('_', ' ')}
        </span>
      </header>
      <div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-4">
        <div className="collector-summary-card"><span>Orbit source</span><b>{orbitSourceStatus}</b></div>
        <div className="collector-summary-card"><span>Latest verified</span><b>{latestVerifiedMultiplier == null ? '—' : `${latestVerifiedMultiplier}x`}</b></div>
        <div className="collector-summary-card"><span>Latest persisted</span><b>{latestPersistedRoundId || roundHistory.latestPublishedRoundId || 'Awaiting acknowledgement'}</b></div>
        <div className="collector-summary-card"><span>Delivery backlog</span><b>{roundHistory.backlogCount ?? pendingRoundDeliveries ?? '—'}</b></div>
      </div>
      <details className="collector-details">
        <summary>Collector diagnostics · 13 metrics</summary>
      <div className="collector-grid">
        <Metric className="collector-metric collector-metric--source" label="Orbit source" value={orbitSourceStatus} detail={collectorMetricDetail || undefined} />
        <Metric className="collector-metric collector-metric--health" label="Round history health" value={roundHistory.state || 'UNKNOWN'} detail={[
          roundHistory.lastIntegrityReason || roundHistory.coverage || null,
          roundHistory.selectorStatus ? `Selector ${roundHistory.selectorStatus}` : null,
          selectorCandidateSummary,
          !roundHistory.lastIntegrityReason && !roundHistory.coverage
            ? (latestDatabaseRoundVerified
              ? `Live integrity report unavailable · latest saved round #${latestDatabaseRound?.round_index} is verified`
              : 'Live integrity report is not available yet') : null,
        ].filter(Boolean).join(' · ')} />
        <Metric className="collector-metric" label="Browser latest multiplier" value={roundHistory.latestBrowserObservedMultiplier == null ? '—' : `${roundHistory.latestBrowserObservedMultiplier}x`}
          detail={roundHistory.lastObservationAt
            ? `Observed ${roundHistory.lastObservationAt} · ${browserObservationAgeSeconds == null ? 'age unavailable' : `${Math.floor(browserObservationAgeSeconds)}s ago`}`
            : 'Waiting for a current source observation'} />
        <Metric className="collector-metric" label="Latest live verified multiplier" value={latestVerifiedMultiplier == null ? '—' : `${latestVerifiedMultiplier}x`}
          detail={roundStream.latest_verified_round_uid
            ? `${roundStream.latest_verified_round_uid} · Verified ${roundStream.latest_verified_event_at || roundStream.latest_observed_at || 'time unavailable'} · ${liveRoundAgeSeconds == null ? 'age unavailable' : `${liveRoundAgeSeconds.toFixed(1)}s old`}${liveRoundCadenceP95Seconds == null ? '' : ` · observed p95 interval ${liveRoundCadenceP95Seconds.toFixed(1)}s`}`
            : roundStream.latest_hub_published_round_uid
              ? `No fresh LIVE_VERIFIED event · latest publication ${roundStream.latest_hub_published_round_uid} is ${roundStream.state || 'not continuity-confirmed'}`
              : 'No fresh verified Orbit round received'} />
        <Metric className="collector-metric collector-metric--receipt" label="Latest persisted round"
          value={latestPersistedRoundId || roundHistory.latestPublishedRoundId || 'Awaiting acknowledgement'}
          detail={`${latestDatabaseRound?.multiplier == null ? 'Multiplier unavailable' : `${latestDatabaseRound.multiplier}x canonical record`} · ${latestPersistedAt ? `Stored ${latestPersistedAt}` : 'No committed round is available'} · backend acknowledgement ${roundHistory.latestPublishedRoundId || 'not reported'}`} />
        <Metric className="collector-metric" label={collectorLatencyLabel}
          value={measuredCollectorLatencyMs == null ? 'Not measured' : `${Math.round(measuredCollectorLatencyMs)} ms`}
          detail={measuredCollectorLatencyMs == null
            ? 'Timing is shown when the source and commit timestamps are available.'
            : Number.isFinite(mutationAtMs) ? 'From source mutation to backend acknowledgement.'
              : 'From multiplier extraction to PostgreSQL commit acknowledgement.'} />
        <Metric className="collector-metric" label="Delivery backlog" value={roundHistory.backlogCount ?? pendingRoundDeliveries ?? '—'}
          detail={`Collector outbox ${localJournal.pending_delivery_count ?? '—'} · API SQLite pending ${roundStream.hub_backlog ?? '—'} · Rules observed ${roundStream.rules_observed_pending ?? '—'} · Rules commit ${roundStream.rules_committed_pending ?? '—'} · Behaviour ${roundStream.behaviour_backlog ?? '—'}`} />
        <Metric className="collector-metric" label="Shared live round hub" value={roundStream.state || 'UNKNOWN'}
          detail={`${roundStream.latest_verified_round_uid || 'No fresh verified round'} · ${roundStream.latest_multiplier == null ? '—' : `${roundStream.latest_multiplier}x`} · published ${roundStream.hub_last_observed_publish_at || 'not reported'} · subscribers ${roundStream.hub_subscriber_count ?? '—'}`} />
        <Metric className="collector-metric" label="Behaviour consumer" value={roundStream.behaviour_consumer_state || 'UNKNOWN'}
          detail={`${roundStream.behaviour_latest_round_uid || 'No acknowledged canonical round'} · inbox sequence checkpoint ${roundStream.behaviour_checkpoint ?? '—'} · pending ${roundStream.behaviour_backlog ?? '—'}`} />
        <Metric className="collector-metric" label="Behaviour delivery" value={roundStream.behaviour_delivery_latency_ms == null
          ? 'Not measured' : `${Math.round(roundStream.behaviour_delivery_latency_ms)} ms`}
          detail={`Rules inbox sequence checkpoint ${roundStream.rules_checkpoint ?? '—'} · ${roundStream.checkpoint_namespace || 'checkpoint namespace unavailable'} · delivery ack ${roundStream.hub_delivery_ack_protocol || 'unknown'}`} />
        <Metric className="collector-metric" label="Local Orbit journal" value={localJournal.state || (localJournal.healthy ? 'HEALTHY' : 'UNKNOWN')}
          detail={`${localJournal.pending_delivery_count ?? '—'} pending · ${localJournal.bytes == null ? '—' : `${(localJournal.bytes / 1048576).toFixed(1)} MB`} / ${localJournal.capacity_bytes == null ? '—' : `${(localJournal.capacity_bytes / 1048576).toFixed(0)} MB`}`} />
        <Metric className="collector-metric" label="PostgreSQL sync" value={postgresStorage.status || 'UNKNOWN'}
          detail={`${postgresStorage.backlog_size ?? '—'} queued · last commit ${postgresStorage.last_commit_acknowledged_at || 'not confirmed'}`} />
        <Metric className="collector-metric" label="Total history" value={collectorSnapshot?.count ?? status?.total_history ?? '—'} icon={Database} />
      </div>
      </details>
    </section>

    <section className="overflow-hidden rounded-xl border border-emerald-500/25 bg-gradient-to-br from-emerald-950/25 via-zinc-900/80 to-sky-950/20 p-3 shadow-lg shadow-black/20 sm:p-4">
      <div className="flex items-start gap-2 border-b border-emerald-500/15 pb-3">
        <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-emerald-400/10 text-emerald-300 ring-1 ring-emerald-300/20">
          <ListChecks size={17} />
        </span>
        <div className="min-w-0">
          <div className="text-[11px] font-bold uppercase tracking-[0.18em] text-emerald-300">Session configuration</div>
          <h2 className="mt-1 text-lg font-bold text-zinc-50">Rules Manager</h2>
          <ViewText className="mt-1">
            <p className="max-w-3xl text-sm leading-6 text-zinc-300">Choose a strategy for the existing SHADOW session. Behaviour Detection supplies verified HIGH eligibility; Rules Engine creates signals and SHADOW owns settlement.</p>
          </ViewText>
        </div>
      </div>

      <fieldset className={`mt-3 rounded-xl border p-3 transition-colors ${ruleSelectionMode === 'behaviour'
        ? 'border-fuchsia-300/25 bg-gradient-to-br from-fuchsia-950/15 via-zinc-950/40 to-violet-950/15'
        : 'border-sky-400/20 bg-sky-400/[0.035]'}`}>
        <legend className={`px-1 text-[11px] font-bold uppercase tracking-[0.15em] ${ruleSelectionMode === 'behaviour' ? 'text-fuchsia-200' : 'text-sky-200'}`}>
          SHADOW source / rule selection</legend>
        <div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-4" role="radiogroup" aria-label="Select SHADOW source">
          {RULE_SELECTION_OPTIONS.map((option) => {
            const selected = ruleSelectionMode === option.id;
            return <label key={option.id}
              className={`source-choice flex min-h-[72px] cursor-pointer items-start gap-2 rounded-lg border p-2.5 text-left focus-within:ring-2 focus-within:ring-offset-2 focus-within:ring-offset-zinc-950 ${selected ? option.selected : option.idle}`}>
              <input type="radio" name="rule-selection-mode" value={option.id} checked={selected}
                onChange={() => setRuleSelectionMode(option.id)} className={`mt-0.5 h-4 w-4 shrink-0 ${option.accent}`} />
              <span className="min-w-0 flex-1">
                <span className="flex flex-wrap items-center gap-2 text-xs font-bold text-zinc-100">
                  {option.label}
                  {selected && <span className="rounded-full border border-white/10 bg-black/20 px-1.5 py-0.5 text-[8px] font-bold uppercase tracking-wide text-zinc-200">Selected</span>}
                </span>
                <span className="mt-1 block text-[10px] leading-3.5 text-zinc-400">{option.detail}</span>
                {option.id === 'behaviour' && <span className="mt-1 block text-[9px] font-semibold text-fuchsia-200">Rolling HIGH · verified</span>}
              </span>
            </label>;
          })}
        </div>
        <ViewText className="mt-3 rounded-xl border border-white/[0.05] bg-black/15 px-3 py-2.5">
          <p className="text-xs leading-5 text-zinc-400">{ruleSelectionMode === 'adaptive'
            ? 'Adaptive scores every eligible rule from verified outcomes and current H/L context. It can choose a changing portfolio of distinct rules, or keep one best candidate exploratory while evidence is weak.'
            : ruleSelectionMode === 'automatic'
            ? 'In a running Automatic session, every policy in this mode is checked, including rules currently marked Disabled. A disabled rule is automatically enabled when it meets the signal or settled-performance criteria, then it can join the session.'
            : ruleSelectionMode === 'behaviour'
              ? 'Behaviour Detection provides Part 1 / Part 2 eligibility. Rules Engine sends one future-round HIGH signal through the standard SHADOW lifecycle and owns its acceptance; SHADOW settles it.'
            : 'You choose which enabled rules participate. Those rules stay fixed for this session.'}</p>
        </ViewText>
        {controlSession && <p className="mt-3 rounded-lg border border-amber-300/15 bg-amber-300/[0.045] px-3 py-2 text-xs leading-5 text-amber-100">
          Current session: <b>{sessionSelectionLabel(controlSession)}</b>. Selection shown here applies to the next session; stop the current session before starting it.
        </p>}
      </fieldset>

      <div className="mt-3 grid gap-2 sm:grid-cols-2 xl:grid-cols-3">
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
            {presets.map((item) => <option key={item.name} value={item.name}>{item.name} · {MODE_LABELS[item.mode] || item.mode} · {item.options?.behaviour_detection_enabled ? 'Behaviour Detection' : item.options?.adaptive_rule_selection ? 'Adaptive' : item.options?.automatic_rule_selection ? 'Automatic' : 'Manual'}</option>)}
          </select>
        </label>
        <label className="block text-xs font-semibold uppercase tracking-wide text-amber-200">Maximum verified rounds
          <input type="number" min="1" step="1" value={maxObservedRounds} disabled={runUntilStopped}
            onChange={(e) => setMaxObservedRounds(e.target.value)} className={`${MANAGER_INPUT_CLASS} border-amber-500/25 bg-zinc-950/80`} />
          <span className="mt-1 block text-[11px] font-normal normal-case tracking-normal text-zinc-500">Choose any positive whole number. There is no 50-round cap.</span>
        </label>
        <label className={`flex cursor-pointer items-start gap-3 rounded-lg border p-3 text-xs transition ${runUntilStopped
          ? 'border-emerald-400/40 bg-emerald-500/10 text-emerald-100'
          : 'border-zinc-700 bg-zinc-950/40 text-zinc-300'}`}>
          <input type="checkbox" checked={runUntilStopped} onChange={(event) => {
            const untilStopped = event.target.checked;
            setRunUntilStopped(untilStopped);
            if (untilStopped) setAutoNext(false);
          }} className="mt-0.5 h-4 w-4 shrink-0 accent-emerald-400" />
          <span>
        <span className="block font-semibold">Until I stop it</span>
            <span className="mt-1 block leading-5 text-zinc-400">No round cap. Enabled WIN/LOSS, profit, and selection limits still stop the session when reached. If none is enabled, press Stop when you want it to end.</span>
          </span>
        </label>
      </div>
      {runUntilStopped && <p className="mt-3 rounded-lg border border-emerald-400/20 bg-emerald-950/20 px-3 py-2 text-xs text-emerald-100">
        The round cap is off. Any enabled WIN/LOSS, profit, or selection limit remains active and stops the session when reached. If no session limit is enabled, it continues until you press Stop. Individual rule limits pause that rule; round-integrity protection can pause the session if input cannot be verified.
      </p>}

      {ruleSelectionMode === 'automatic' ? <section className="mt-6 rounded-xl border border-emerald-400/30 bg-gradient-to-br from-emerald-950/30 via-zinc-900/80 to-sky-950/30 p-4 sm:p-5" aria-label="Automatic rule selection status">
        <div className="flex flex-wrap items-start justify-between gap-3 border-b border-emerald-400/15 pb-3">
          <div>
            <h3 className="text-base font-semibold text-emerald-100">Automatic rule chooser</h3>
            <ViewText className="mt-1" label="View chooser details">
              <p className="max-w-3xl text-sm leading-5 text-zinc-300">Every policy allowed in this mode is checked on each verified round, including rules marked Disabled. The chooser compares the latest settled outcomes with the preceding outcomes and also tracks signal momentum. Improving outcomes can qualify a rule even while its 100-round NET is negative; falling outcomes can remove it while NET is still positive. Rising signals or positive settled NET can also qualify it. Disabled rules stay under review and can be enabled again when they qualify. Several qualifying rules can run together, or none, and matching rules share one SHADOW target.</p>
            </ViewText>
          </div>
          {selectionDraftApplied && <span role="status"
            className="rounded-full border border-emerald-400/30 bg-emerald-400/10 px-3 py-1 text-xs font-semibold text-emerald-200">
            READY FOR NEXT SESSION
          </span>}
          <Button tone={selectionDraftApplied ? 'reset' : 'good'}
            onClick={applySelectionDraft} disabled={busy} className="min-w-40">
            {busyAction === 'apply-rule-selection' ? 'Preparing Automatic…'
              : selectionDraftApplied ? 'AUTOMATIC APPLIED' : 'APPLY AUTOMATIC'}
          </Button>
        </div>
        {selectionApplyNotice?.key === selectionDraftKey && <p role="status" aria-live="polite"
          className="mt-2 rounded-lg border border-emerald-400/25 bg-emerald-950/20 px-3 py-2 text-sm text-emerald-100">
          Automatic settings applied for the next SHADOW session. Performance is up to date; rule selection starts when you press Start SHADOW session.
        </p>}
        {selectionModeNeedsApply && controlSession && <p className="mt-2 text-xs text-amber-200">
          Current session remains {sessionSelectionLabel(controlSession)}; applied Automatic settings are for the next session.
        </p>}
        {!displaySession?.automatic_rule_selection && !controlSession && <p className="mt-3 text-sm text-zinc-300">No Automatic session is active. Start one to enable automatic selection.</p>}
        {displaySession?.automatic_rule_selection && <div className="mt-4 grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
          <Metric label="Current rule choice" value={displaySessionIsActive && displaySession.active_rule_ids?.length
            ? displaySession.active_rule_ids.map((id) => availableRules.find((rule) => rule.rule_id === id)?.name || id).join(', ')
            : displaySessionIsActive ? 'No rule selected' : 'No active session'} detail={AUTO_RULE_REASON_LABELS[displaySession.automatic_rule_choice?.reason]
              || 'Waiting for the next verified round'} />
          <Metric label="Last source round" value={displaySession.automatic_rule_choice?.source_round_index != null
            ? `#${displaySession.automatic_rule_choice.source_round_index}` : 'Waiting'}
            detail={displaySession.last_received_round?.round_id || 'No checked round yet'} />
          <Metric label="Latest SHADOW decision" value={latestSessionEvaluation?.decision?.action || 'WAITING'}
            detail={latestSessionEvaluation?.decision?.reason
              || (displaySession.active_rule_ids?.length ? 'Chooser active; waiting for a rule match' : 'No rule has qualified yet')} />
          <Metric label="Automatic candidates" value={displaySession?.automatic_rule_choice
            ? automaticChoiceCandidates.length : availableRules.length}
            detail={`All policies in ${MODE_LABELS[mode] || mode} are checked, including disabled rules`} />
        </div>}
        <div className="mt-4 space-y-2">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <h4 className="text-xs font-semibold uppercase tracking-wide text-zinc-300">What the chooser is evaluating</h4>
            <Button tone="reset" onClick={restartRulePerformance} disabled={busy}>
              {busyAction === 'reset-rule-performance' ? 'Restarting 100-round window…' : 'Restart 100-round performance'}
            </Button>
          </div>
          {performanceResetNotice && <p role="status" className="rounded-lg border border-violet-400/25 bg-violet-950/20 px-3 py-2 text-xs leading-5 text-violet-100">{performanceResetNotice}</p>}
          <p className="text-[11px] leading-4 text-zinc-500">This starts a fresh 100-verified-round chooser window after the latest verified round. It keeps previous results and does not clear a rule’s two-loss pause.</p>
          {availableRules.map((rule) => {
            const candidate = automaticCandidates.get(rule.rule_id);
            const reportedWindow = automaticPerformanceWindows.get(rule.rule_id);
            const runtime = latestRuleRuntime.get(rule.rule_id);
            const lossLimit = Number(rule.condition?.loss_limit) || 2;
            const lossCount = Number(runtime?.loss_limit_losses ?? candidate?.loss_limit_losses ?? 0);
            const cycle = runtime?.cycle || {};
            const lossLimitBlocked = runtime?.loss_limit_blocked ?? candidate?.loss_limit_blocked;
            const cycleActive = Boolean(cycle.active ?? candidate?.cycle_active)
              || runtime?.rule_state === 'PLAYING' || candidate?.rule_state === 'PLAYING';
            const cycleTargets = Number(cycle.requested_count ?? candidate?.cycle_requested_targets
              ?? runtime?.selected_outcomes?.length ?? 0);
            const cycleMaximum = Number(cycle.maximum_targets ?? rule.condition?.maximum_selections);
            const cycleTargetLabel = Number.isFinite(cycleMaximum) && cycleMaximum > 0
              ? `${cycleTargets}/${cycleMaximum} targets` : `${cycleTargets} targets`;
            const entrySignalLatched = runtime?.entry_signal_latched ?? candidate?.entry_signal_latched;
            const cycleEndCondition = cycle.end_condition || candidate?.cycle_end_condition
              || 'ends when this rule reaches its stop condition';
            const windowRounds = candidate?.window_rounds ?? reportedWindow?.observed_rounds;
            const hasWindow = windowRounds != null;
            const ruleWindowSize = Math.max(1,
              Number(candidate?.window_size ?? reportedWindow?.window_size) || 100);
            const ruleWindowRounds = Math.min(ruleWindowSize,
              Math.max(0, Number(windowRounds) || 0));
            const ruleWindowProgress = (ruleWindowRounds / ruleWindowSize) * 100;
            const ruleWins = Math.max(0, Number(candidate?.wins ?? reportedWindow?.wins) || 0);
            const ruleLosses = Math.max(0, Number(candidate?.losses ?? reportedWindow?.losses) || 0);
            const ruleSettled = ruleWins + ruleLosses;
            const ruleWinWidth = ruleSettled ? (ruleWins / ruleSettled) * 100 : 0;
            const ruleNet = Number(candidate?.net_wins ?? reportedWindow?.net) || 0;
            const ruleProgressTone = ruleNet > 0 ? 'from-emerald-500 to-emerald-300'
              : ruleNet < 0 ? 'from-rose-600 to-rose-400' : 'from-sky-500 to-cyan-300';
            const runtimeState = lossLimitBlocked
              ? `Rule loss-limit pause · ${lossCount}/${lossLimit} losses · waiting for its entry signal to reset`
              : cycleActive
                ? `Cycle active · ${cycleTargetLabel} · ${cycleEndCondition} · losses ${lossCount}/${lossLimit}`
                : entrySignalLatched
                  ? `Entry used · waiting for the signal to clear · losses ${lossCount}/${lossLimit}`
                  : `Ready for a fresh entry · losses ${lossCount}/${lossLimit}`;
            const explicitlyDisabled = rule.configured === true && rule.enabled === false;
            const automaticSession = displaySession?.automatic_rule_selection === true;
            const selected = displaySession?.automatic_rule_selection
              && displaySession.active_rule_ids?.includes(rule.rule_id);
            const state = selected
              ? `Selected for current round · ${candidate?.automatically_reenabled ? 'automatically re-enabled · ' : ''}${AUTO_RULE_REASON_LABELS[candidate?.reason] || 'qualified'}`
              : explicitlyDisabled && automaticSession
                ? `${AUTO_RULE_REASON_LABELS[candidate?.reason] || AUTO_RULE_REASON_LABELS.RULE_DISABLED} · Automatic will enable it when it qualifies`
              : explicitlyDisabled ? AUTO_RULE_REASON_LABELS.RULE_DISABLED
              : AUTO_RULE_REASON_LABELS[candidate?.reason]
                || 'Waiting for a signal trend or settled results';
            return <div key={rule.rule_id} className="rounded-lg border border-zinc-700/80 bg-zinc-950/50 px-3 py-2.5">
              <div className="flex flex-wrap items-start justify-between gap-2">
                <div className="min-w-0 flex-1">
                  <span className="font-medium text-zinc-100">{rule.name}</span>
                  <code className="ml-2 text-[10px] text-zinc-500">{rule.rule_id}</code>
                  <div className="mt-1 text-xs text-zinc-400">{state}
                    {(candidate?.settled != null || reportedWindow?.observed_rounds != null) && <span className="ml-1 font-medium tabular-nums">
                      <span className="text-emerald-300">{ruleWins} WIN</span><span className="px-1 text-zinc-600">·</span>
                      <span className="text-rose-300">{ruleLosses} LOSS</span><span className={`ml-1 ${ruleNet > 0 ? 'text-emerald-200' : ruleNet < 0 ? 'text-rose-200' : 'text-zinc-400'}`}>· NET {ruleNet > 0 ? '+' : ''}{ruleNet}</span>
                    </span>}
                    <div className={runtime?.loss_limit_blocked || candidate?.loss_limit_blocked
                      ? 'mt-1 text-rose-300' : 'mt-1 text-sky-200'}>{runtimeState}</div>
                    {(!explicitlyDisabled || automaticSession) && <div className={`mt-1 ${candidate?.performance_trend === 'RISING' ? 'text-emerald-300' : candidate?.performance_trend === 'FALLING' ? 'text-rose-300' : 'text-sky-200'}`}>
                      {performanceMomentumSummary(candidate)}
                    </div>}
                    {(!explicitlyDisabled || automaticSession) && <div className={`mt-1 ${candidate?.signal_trend === 'RISING' ? 'text-emerald-300' : candidate?.signal_trend === 'FALLING' ? 'text-rose-300' : 'text-sky-200'}`}>
                      {signalMomentumSummary(candidate)}
                    </div>}
                  </div>
                </div>
                <span className={`rounded-full px-2 py-1 text-[10px] font-semibold ${explicitlyDisabled && automaticSession ? 'bg-amber-400/10 text-amber-200' : explicitlyDisabled ? 'bg-zinc-700/60 text-zinc-400' : selected ? 'bg-emerald-400/10 text-emerald-200' : 'bg-sky-400/10 text-sky-200'}`}>
                  {explicitlyDisabled && automaticSession ? 'AUTO RECHECK' : explicitlyDisabled ? 'DISABLED' : selected ? 'CHOSEN' : 'AUTO CANDIDATE'}
                </span>
              </div>
              {hasWindow && <div className="mt-2.5 rounded-lg border border-zinc-800/80 bg-black/15 p-2.5">
                <div className="mb-1 flex flex-wrap items-center justify-between gap-2 text-[10px]">
                  <span className="font-semibold uppercase tracking-wide text-zinc-500">Rolling latest 100</span>
                  <span className="font-mono tabular-nums text-zinc-300">{ruleWindowRounds}/{ruleWindowSize} rounds · {Math.round(ruleWindowProgress)}% · {candidate?.recovered_rounds ?? 0} recovered</span>
                </div>
                <div role="progressbar" aria-label={`${rule.name} chooser window progress`}
                  aria-valuemin={0} aria-valuemax={ruleWindowSize} aria-valuenow={ruleWindowRounds}
                  aria-valuetext={`Rolling latest ${ruleWindowRounds} of ${ruleWindowSize} rounds`}
                  className="relative h-2.5 overflow-hidden rounded-full bg-zinc-800 ring-1 ring-inset ring-white/5">
                  <div className={`h-full rounded-full bg-gradient-to-r ${ruleProgressTone} transition-[width] duration-500 ease-out`}
                    style={{ width: `${ruleWindowProgress}%` }} />
                </div>
                <div className="mt-2 flex h-1.5 overflow-hidden rounded-full bg-zinc-800"
                  role="img" aria-label={`${rule.name} results in this window: ${ruleWins} wins and ${ruleLosses} losses`}>
                  {ruleSettled > 0 && <><div className="bg-emerald-400" style={{ width: `${ruleWinWidth}%` }} />
                    <div className="bg-rose-400" style={{ width: `${100 - ruleWinWidth}%` }} /></>}
                </div>
              </div>}
            </div>;
          })}
          {!availableRules.length && <p className="text-sm text-zinc-400">No rules are available for this session mode.</p>}
        </div>
        {displaySession?.automatic_rule_selection && latestSessionEvaluation?.automatic_rule_choice && <details className="mt-3 rounded-lg border border-zinc-700 p-3">
          <summary className="cursor-pointer text-xs font-semibold text-zinc-300">Latest round decision details</summary>
          <pre className="mt-2 max-h-64 overflow-auto whitespace-pre-wrap text-xs text-zinc-400">{JSON.stringify({
            source_round: latestSessionEvaluation.source_round,
            decision: latestSessionEvaluation.decision,
            rule_choice: latestSessionEvaluation.automatic_rule_choice,
            rule_evaluations: latestSessionEvaluation.rule_evaluations?.map((item) => ({
              rule_id: item.rule_id, eligible: item.eligible, status: item.status, reason: item.reason,
              selected_by_automatic_rule: item.selected_by_automatic_rule,
            })),
          }, null, 2)}</pre>
        </details>}
      </section> : ruleSelectionMode === 'adaptive' ? <section className="mt-6 rounded-xl border border-violet-400/30 bg-gradient-to-br from-violet-950/30 via-zinc-900/80 to-sky-950/30 p-4 sm:p-5" aria-label="Adaptive rule selection status">
        <div className="flex flex-wrap items-start justify-between gap-3 border-b border-violet-400/15 pb-3">
          <div>
            <h3 className="text-base font-semibold text-violet-100">Adaptive rule portfolio</h3>
            <ViewText className="mt-1" label="View Adaptive selection details">
              <p className="max-w-3xl text-sm leading-5 text-zinc-300">After each settled outcome, Adaptive reviews verified per-rule history, rolling 50-round results, recent and longer-term stability, baseline-adjusted evidence, similar H/L contexts, and signal overlap. It can activate any number of distinct rules. When evidence is weak, it labels the best available rule Exploratory. Each rule keeps an independent SHADOW prediction ledger; shared target outcomes count once in the portfolio total.</p>
            </ViewText>
          </div>
          {selectionDraftApplied && <span role="status"
            className="rounded-full border border-emerald-400/30 bg-emerald-400/10 px-3 py-1 text-xs font-semibold text-emerald-200">
            READY FOR NEXT SESSION
          </span>}
          <Button tone={selectionDraftApplied ? 'reset' : 'good'}
            onClick={applySelectionDraft} disabled={busy} className="min-w-40">
            {busyAction === 'apply-rule-selection' ? 'Preparing Adaptive…'
              : selectionDraftApplied ? 'ADAPTIVE APPLIED' : 'APPLY ADAPTIVE'}
          </Button>
        </div>
        {selectionApplyNotice?.key === selectionDraftKey && <p role="status" aria-live="polite"
          className="mt-2 rounded-lg border border-emerald-400/25 bg-emerald-950/20 px-3 py-2 text-sm text-emerald-100">
          Adaptive settings applied for the next SHADOW session. Performance is up to date; the portfolio selector starts when you press Start SHADOW session.
        </p>}
        {selectionPreviewError && ruleSelectionMode === 'adaptive' && <p role="status" className="mt-2 text-xs text-amber-200">{selectionPreviewError}</p>}
        {selectionDraftApplied && <SelectionPreviewPanel preview={selectionPreview}
          rules={availableRules} selectionMode="adaptive" />}
        {controlSession && selectionModeNeedsApply && <p className="mt-2 text-xs text-amber-200">
          Current session remains {sessionSelectionLabel(controlSession)}; applied Adaptive settings are for the next session.
        </p>}
        {!displaySession?.adaptive_rule_selection && !controlSession && <p className="mt-3 text-sm text-zinc-300">No Adaptive session is active. Start one to enable automatic portfolio selection.</p>}
        {displaySession?.adaptive_rule_selection && <>
          <div className="mt-4 grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
            <Metric label="Selected rules" value={`${adaptiveSelectedCandidates.length} · ${adaptiveSelectedCandidates.map((item) => availableRules.find((rule) => rule.rule_id === item.rule_id)?.name || item.rule_id).join(', ') || 'none'}`}
              detail={adaptiveChoice?.reason || 'Waiting for the first verified rule check'} />
            <Metric label="Last portfolio update" value={adaptiveChoice?.evaluated_at || 'Waiting'}
              detail={`Selection version ${displaySession.adaptive_selection_version ?? adaptiveChoice?.selection_version ?? 0} · source #${adaptiveChoice?.source_round_index ?? '—'}`} />
            <Metric label="Portfolio forward results" value={`WIN ${displaySession.adaptive_portfolio_performance?.wins ?? 0} · LOSS ${displaySession.adaptive_portfolio_performance?.losses ?? 0} · PENDING ${displaySession.adaptive_portfolio_performance?.pending ?? 0}`}
              detail={`Unique-target net ${displaySession.adaptive_portfolio_performance?.net ?? 0} · ${displaySession.adaptive_portfolio_performance?.independent_rule_predictions ?? 0} per-rule predictions`} />
            <Metric label="Candidate evidence" value={`${adaptiveCandidates.length} checked`}
              detail={`${adaptiveCandidates.filter((item) => item.evidence_status === 'VALIDATED').length} validated · ${adaptiveCandidates.filter((item) => item.evidence_status !== 'VALIDATED').length} developing or exploratory`} />
          </div>
          <div className="mt-4 space-y-2">
            <div className="flex flex-wrap items-center justify-between gap-3">
              <h4 className="text-xs font-semibold uppercase tracking-wide text-zinc-300">Adaptive candidates and forward outcomes</h4>
              <Button tone="reset" onClick={restartRulePerformance} disabled={busy}>
                {busyAction === 'reset-rule-performance' ? 'Restarting performance window…' : 'Restart performance window'}
              </Button>
            </div>
            {adaptiveCandidates.map((candidate) => {
              const performance = adaptiveRulePerformance[candidate.rule_id] || {};
              const window = candidate.rolling_50 || {};
              return <article key={candidate.rule_id} className="rounded-lg border border-zinc-800 bg-black/20 p-3">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <div className="min-w-0">
                    <p className="text-sm font-semibold text-zinc-100">{availableRules.find((rule) => rule.rule_id === candidate.rule_id)?.name || candidate.rule_id}</p>
                    <p className="mt-1 text-xs text-zinc-400">{candidate.reason} · score {candidate.score ?? '—'} · {candidate.evidence_status || 'EXPLORATORY'}</p>
                  </div>
                  <span className={`rounded-full px-2 py-1 text-[10px] font-semibold ${candidate.selected ? 'bg-violet-400/15 text-violet-200' : 'bg-zinc-800 text-zinc-400'}`}>
                    {candidate.selected ? 'SELECTED' : 'OBSERVING'}
                  </span>
                </div>
                <div className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-[11px] text-zinc-400">
                  <span>Rolling 50: {window.wins ?? 0}W / {window.losses ?? 0}L · net {window.net ?? 0}</span>
                  <span>Baseline {candidate.baseline_win_rate ?? '—'} · confidence {candidate.confidence ?? '—'} · stability {candidate.stability ?? '—'}</span>
                  <span>Context samples {candidate.context_evidence?.similar_samples ?? 0}</span>
                  <span>Forward rule ledger: {performance.wins ?? 0}W / {performance.losses ?? 0}L / {performance.pending ?? 0}P · net {performance.net ?? 0}</span>
                </div>
              </article>;
            })}
            {!adaptiveCandidates.length && <p className="text-sm text-zinc-400">Waiting for the first verified round to score and select rules.</p>}
          </div>
          <div className="mt-3 rounded-lg border border-sky-400/15 bg-sky-950/10 p-3 text-xs text-zinc-300">
            Baselines: the best fixed rule uses each rule’s independent forward ledger. Automatic uses a shadow-only counterfactual on matching targets, plus saved Automatic session results when available.
            <span className="ml-1 text-zinc-500">{displaySession.adaptive_baseline_comparison?.automatic?.status || 'Automatic comparison is waiting for overlapping forward targets.'}</span>
          </div>
          {adaptiveChoice && <details className="mt-3 rounded-lg border border-zinc-700 p-3">
            <summary className="cursor-pointer text-xs font-semibold text-zinc-300">View complete Adaptive decision, evidence, and change reasons</summary>
            <pre className="mt-2 max-h-80 overflow-auto whitespace-pre-wrap text-xs text-zinc-400">{JSON.stringify(adaptiveChoice, null, 2)}</pre>
          </details>}
        </>}
      </section> : ruleSelectionMode === 'behaviour'
        ? <BehaviourDetectionSelectionCard selectedForNextSession={ruleSelectionMode === 'behaviour'} /> : <>
      <div className="mt-6 flex flex-wrap items-end justify-between gap-3 border-b border-zinc-700/70 pb-3">
        <div>
          <h3 className="text-sm font-semibold text-zinc-100">Registered rules</h3>
          <p className="mt-1 text-xs text-zinc-400">Choose enabled rules for the next SHADOW session.</p>
        </div>
        <div className="flex flex-wrap gap-2 text-xs font-semibold">
          <span className="rounded-full border border-emerald-400/25 bg-emerald-400/10 px-3 py-1 text-emerald-200">{availableRules.filter((rule) => rule.enabled).length} enabled</span>
          <span className="rounded-full border border-sky-400/25 bg-sky-400/10 px-3 py-1 text-sky-200">{ruleSelectionMode === 'manual'
            ? `${availableRules.filter((rule) => selectedIds.includes(rule.rule_id)).length} selected`
            : `Active: ${displaySession?.automatic_rule_selection ? displaySession.active_rule_ids?.join(', ') || 'none' : 'waiting'}`}</span>
          {ruleSelectionMode === 'manual' && selectionDraftApplied && <span role="status"
            className="rounded-full border border-emerald-400/30 bg-emerald-400/10 px-3 py-1 text-xs font-semibold text-emerald-200">
            READY FOR NEXT SESSION
          </span>}
          {ruleSelectionMode === 'manual' && <Button tone={selectionDraftApplied ? 'reset' : 'good'}
            onClick={applySelectionDraft} disabled={busy} className="min-w-40">
            {busyAction === 'apply-rule-selection' ? 'Applying Manual…'
              : selectionDraftApplied ? 'MANUAL APPLIED' : 'APPLY MANUAL'}
          </Button>}
        </div>
      </div>
      {selectionApplyNotice?.key === selectionDraftKey && ruleSelectionMode === 'manual' && <p role="status" aria-live="polite"
        className="mt-2 rounded-lg border border-emerald-400/25 bg-emerald-950/20 px-3 py-2 text-sm text-emerald-100">
        Manual selection applied for the next SHADOW session: {selectedIds.map((id) => availableRules.find((rule) => rule.rule_id === id)?.name || id).join(', ') || 'no rules selected'}. Press Start SHADOW session when you are ready.
      </p>}
      {controlSession && ruleSelectionMode === 'manual' && selectionModeNeedsApply && <p className="mt-2 text-xs text-amber-200">
        Current session remains {sessionSelectionLabel(controlSession)}; applied Manual settings are for the next session.
      </p>}
      <div className="mt-3 space-y-3">
        {availableRules.map((rule) => {
          const runtime = latestRuleRuntime.get(rule.rule_id);
          const cycle = runtime?.cycle || {};
          const lossLimit = Number(rule.condition?.loss_limit) || 2;
          const lossCount = Number(runtime?.loss_limit_losses || 0);
          const cycleTargets = Number(cycle.requested_count ?? runtime?.selected_outcomes?.length ?? 0);
          const cycleMaximum = Number(cycle.maximum_targets ?? rule.condition?.maximum_selections);
          const targetCount = Number.isFinite(cycleMaximum) && cycleMaximum > 0
            ? `${cycleTargets}/${cycleMaximum} targets` : `${cycleTargets} targets`;
          const lifecycle = !runtime
            ? `Waiting for a verified rule check · loss cap ${lossLimit}`
            : runtime.loss_limit_blocked
              ? `Paused after ${lossCount}/${lossLimit} losses · waiting for a fresh entry`
              : cycle.active || runtime.rule_state === 'PLAYING'
                ? `Cycle active · ${targetCount} · ${cycle.end_condition || 'ends on this rule’s stop condition'} · losses ${lossCount}/${lossLimit}`
                : runtime.entry_signal_latched
                  ? `Entry used · waiting for its signal to clear · losses ${lossCount}/${lossLimit}`
                  : `Ready for a fresh entry · losses ${lossCount}/${lossLimit}`;
          return <div key={rule.rule_id}
          className={`grid gap-4 rounded-xl border border-l-4 p-4 transition-colors md:grid-cols-[minmax(13rem,0.7fr)_minmax(0,1.8fr)] ${rule.enabled && (ruleSelectionMode === 'manual'
            ? selectedIds.includes(rule.rule_id) : displaySession?.active_rule_ids?.includes(rule.rule_id))
            ? 'border-emerald-500/30 border-l-emerald-400 bg-emerald-950/20'
            : rule.enabled ? 'border-zinc-700 border-l-sky-400 bg-zinc-950/55'
              : 'border-zinc-800 border-l-zinc-600 bg-zinc-950/30'}`}>
          <div className="flex flex-wrap items-center gap-2 md:content-center">
            <label className={`inline-flex min-h-10 items-center gap-2 rounded-lg border px-3 text-xs font-semibold ${rule.enabled
              ? 'border-emerald-400/25 bg-emerald-400/10 text-emerald-200'
              : 'border-zinc-700 bg-zinc-900 text-zinc-400'}`}>
              <input type="checkbox" checked={Boolean(rule.enabled)} disabled={busy}
                onChange={(event) => toggleRule(rule, event.target.checked)} className="accent-emerald-400" />Enabled</label>
            {ruleSelectionMode === 'manual' && <label className={`inline-flex min-h-10 items-center gap-2 rounded-lg border px-3 text-xs font-semibold ${selectedIds.includes(rule.rule_id)
              ? 'border-sky-400/25 bg-sky-400/10 text-sky-200'
              : 'border-zinc-700 bg-zinc-900/70 text-zinc-400'} ${!rule.enabled ? 'opacity-50' : ''}`}>
              <input type="checkbox" checked={selectedIds.includes(rule.rule_id)} disabled={!rule.enabled || busy}
                onChange={(event) => updateSelectedIds((previous) => event.target.checked
                  ? [...new Set([...previous, rule.rule_id])] : previous.filter((id) => id !== rule.rule_id))}
                className="accent-sky-400" />Participate</label>}
            {ruleSelectionMode === 'automatic' && displaySession?.active_rule_ids?.includes(rule.rule_id)
              && <span className="rounded-lg border border-sky-400/25 bg-sky-400/10 px-3 py-2 text-xs font-semibold text-sky-200">Chosen now</span>}
            {ruleSelectionMode === 'adaptive' && displaySession?.active_rule_ids?.includes(rule.rule_id)
              && <span className="rounded-lg border border-violet-400/25 bg-violet-400/10 px-3 py-2 text-xs font-semibold text-violet-200">Adaptive selected</span>}
          </div>
          <span className="min-w-0 flex-1">
            <span className="flex flex-wrap items-center gap-2 font-semibold text-zinc-100">
              {rule.name}<code className="rounded-md border border-emerald-400/15 bg-emerald-400/5 px-2 py-1 text-[11px] text-emerald-200">{rule.rule_id}</code>
              <span className="rounded-full bg-zinc-800 px-2 py-0.5 text-[10px] text-zinc-400">v{rule.version}</span>
            </span>
            <p className={`mt-1 text-xs ${runtime?.loss_limit_blocked ? 'text-rose-300' : 'text-sky-200'}`}>{lifecycle}</p>
            {ruleSelectionMode === 'automatic' && automaticCandidates.has(rule.rule_id) && <p className="mt-1 text-xs text-zinc-400">
              {AUTO_RULE_REASON_LABELS[automaticCandidates.get(rule.rule_id).reason]
                || automaticCandidates.get(rule.rule_id).reason}
              {automaticCandidates.get(rule.rule_id).settled != null
                ? ` · ${automaticCandidates.get(rule.rule_id).wins} wins / ${automaticCandidates.get(rule.rule_id).losses} losses · net ${automaticCandidates.get(rule.rule_id).net_wins}` : ''}
            </p>}
            {ruleSelectionMode === 'adaptive' && adaptiveCandidateMap.has(rule.rule_id) && <p className="mt-1 text-xs text-violet-200">
              {adaptiveCandidateMap.get(rule.rule_id).reason} · score {adaptiveCandidateMap.get(rule.rule_id).score ?? '—'} · {adaptiveCandidateMap.get(rule.rule_id).evidence_status || 'EXPLORATORY'}
            </p>}
            <details className="mt-2 text-xs text-zinc-400">
              <summary className="cursor-pointer">View condition and state behavior</summary>
              {rule.condition?.h_loss_win_special_skip === true && <p className="mt-2 text-amber-200">
                Special sequence: H → bet LOSS → bet WIN starts one-round skips. After skipped L (≤2.00), bet next round and repeat after WIN. One special bet LOSS or skipped H (&gt;2.00) returns to WAIT_LOW. Applies when enabled and chosen for the session or round.
              </p>}
              <pre className="mt-2 whitespace-pre-wrap text-zinc-500">{JSON.stringify(rule.condition, null, 2)}</pre>
              <p className="mt-2 text-zinc-400">{rule.transition_rules?.join(' ')}</p>
            </details>
          </span>
        </div>;
        })}
        {!availableRules.length && <p className="text-sm text-zinc-500">No rules are registered for this mode.</p>}
      </div>
      </>}

      <div className="mt-6 rounded-xl border border-sky-500/20 bg-sky-950/10 p-4 sm:p-5">
        <div className="mb-4 flex flex-wrap items-center justify-between gap-2 border-b border-sky-400/15 pb-3">
          <div>
            <h3 className="text-sm font-semibold text-sky-100">Session settings</h3>
            <ViewText className="mt-1" label="View settings details">
              <p className="text-xs text-zinc-400">These are browser draft settings. A running session keeps its saved settings until you restart it.</p>
            </ViewText>
          </div>
          <span className="rounded-full border border-sky-400/20 bg-sky-400/10 px-3 py-1 text-[11px] font-semibold text-sky-200">SHADOW only</span>
        </div>
      <div className="grid items-stretch gap-4 sm:grid-cols-2 xl:grid-cols-5">
        <label htmlFor="rules-manager-stake" className="flex min-w-0 flex-col text-sm text-zinc-300">
          <span className="flex min-h-10 items-end pb-1">Fixed simulated stake (RWF)</span>
          <input id="rules-manager-stake" type="number" min="1" value={stake}
            onChange={(e) => setStake(e.target.value)} className={MANAGER_INPUT_CLASS} />
        </label>
        {(mode === 'START_CHECK' || mode === 'LEGACY_SEPARATE_TRACKER') && <div className="min-w-0">
          <div className="flex min-h-10 items-end justify-between gap-2 pb-1">
            <label htmlFor="rules-manager-profit-goal" className="text-sm text-zinc-300">Profit stop (RWF)</label>
            <label className="inline-flex items-center gap-1.5 rounded-full border border-emerald-500/20 bg-emerald-500/5 px-2 py-0.5 text-[11px] text-emerald-100">
              <input type="checkbox" checked={profitEnabled}
                onChange={(e) => setProfitEnabled(e.target.checked)} className="accent-emerald-400" />Enable
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
              <input type="checkbox" checked={winEnabled}
                  onChange={(e) => setWinEnabled(e.target.checked)} className="accent-emerald-400" />Enable
              </label>
            </div>
            <input id="rules-manager-win-limit" type="number" min="1" value={winCount} disabled={!winEnabled}
              onChange={(e) => setWinCount(e.target.value)} className={`${MANAGER_INPUT_CLASS} border-emerald-500/25`} />
          </div>
          <div className="min-w-0">
            <div className="flex min-h-10 items-end justify-between gap-2 pb-1">
              <label htmlFor="rules-manager-loss-limit" className="text-sm text-rose-100">LOSS_LIMIT</label>
              <label className="inline-flex items-center gap-1.5 rounded-full border border-rose-500/20 bg-rose-500/5 px-2 py-0.5 text-[11px] text-rose-100">
              <input type="checkbox" checked={lossEnabled}
                  onChange={(e) => setLossEnabled(e.target.checked)} className="accent-rose-400" />Enable
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

      {mode === 'ALL_RULES_TOGETHER' && <p className="mt-3 text-xs text-zinc-500">{runUntilStopped
        ? 'No round cap. Enabled WIN/LOSS and selection-count limits still stop this session when reached.'
        : autoNextLimitsEnabled
          ? 'Legacy WIN/LOSS net-outcome limits pause while custom per-window limits are enabled.'
          : 'WIN/LOSS limits use settled results; pending selections are excluded.'}</p>}

      {(mode === 'START_CHECK' || mode === 'LEGACY_SEPARATE_TRACKER' || mode === 'ALL_RULES_TOGETHER') && <fieldset className="mt-4">
        <legend className="mb-2 text-xs font-semibold uppercase tracking-wide text-zinc-300">Automatic next check</legend>
        <p className="mb-3 text-xs text-zinc-400">{runUntilStopped
          ? 'Automatic next checks stay off. Enabled session limits still stop this session when reached.'
          : 'Choose when to start the next check.'}</p>
        {!runUntilStopped && <ViewText className="mb-3" label="View trigger details">
          <p className="text-xs leading-5 text-zinc-500">The immediate option applies to legacy profit, selection-count, and net-outcome stops. Custom per-window WIN/LOSS limits wait until the full window finishes, even when both triggers are enabled.</p>
        </ViewText>}
        <ViewText className="mb-3" label="View next-check details">
          <p className="text-xs leading-5 text-zinc-500">Card badges show the settings in this form. {controlSession
            ? `Current session trigger: ${autoNextTriggerLabel}${savedAutoNextRuleLimits}. Changes apply to the next session you start.`
            : 'These settings apply to the next session you start.'}</p>
        </ViewText>
        <button type="button" aria-pressed={runUntilStopped || (!autoNext && !autoNextOnLimit)}
          onClick={() => { setAutoNext(false); setAutoNextOnLimit(false); }}
          className={`mb-3 inline-flex cursor-pointer items-center gap-2 rounded-lg border px-3 py-2 text-xs ${runUntilStopped || (!autoNext && !autoNextOnLimit)
            ? 'border-zinc-500 bg-zinc-800 text-zinc-100'
            : 'border-zinc-700 bg-zinc-950/40 text-zinc-300'}`}>
          Off — no automatic next check
        </button>
        <div className="grid gap-3 md:grid-cols-2">
          <div className={`rounded-xl border p-4 transition ${runUntilStopped
            ? 'cursor-not-allowed border-zinc-800 bg-zinc-950/30 opacity-60'
            : autoNext ? 'cursor-pointer border-sky-400/50 bg-sky-500/10 ring-1 ring-sky-400/20'
              : 'cursor-pointer border-zinc-700 bg-zinc-950/40 hover:border-sky-400/30 hover:bg-sky-500/5'}`}>
            <label className="flex cursor-pointer items-start gap-3">
              <input type="checkbox" checked={autoNext && !runUntilStopped} disabled={runUntilStopped}
                onChange={(event) => setAutoNext(event.target.checked)}
                className="mt-1 h-4 w-4 shrink-0 accent-sky-400" />
              <span className="min-w-0 flex-1">
              <span className="flex flex-wrap items-center justify-between gap-2">
                <span className="text-sm font-semibold text-sky-100">{configuredWindowRounds == null
                  ? 'Start the next check after a full session window' : `Start the next check after ${configuredWindowRounds} verified rounds`}</span>
                <span className={`rounded-full px-2 py-0.5 text-[10px] font-bold uppercase tracking-wide ${autoNext && !runUntilStopped
                  ? 'bg-sky-400/15 text-sky-200' : 'bg-zinc-800 text-zinc-400'}`}>{autoNext && !runUntilStopped ? 'On' : 'Off'}</span>
              </span>
              </span>
            </label>
            <ViewText className="ml-7 mt-2" label="View timing details">
                <p id="rules-auto-next-window-help" className="text-xs leading-5 text-zinc-400">
                  {runUntilStopped ? 'Automatic successors stay off. Enabled session limits stop this session when reached.'
                    : `Wait for all ${configuredWindowRounds} verified rounds in this session’s configured window.`}
                </p>
            </ViewText>
          </div>
          <div className={`rounded-xl border p-4 transition ${runUntilStopped
            ? 'cursor-not-allowed border-zinc-800 bg-zinc-950/30 opacity-60'
            : autoNextOnLimit ? 'cursor-pointer border-amber-400/50 bg-amber-500/10 ring-1 ring-amber-400/20'
              : 'cursor-pointer border-zinc-700 bg-zinc-950/40 hover:border-amber-400/30 hover:bg-amber-500/5'}`}>
            <label className="flex cursor-pointer items-start gap-3">
              <input type="checkbox" checked={autoNextOnLimit && !runUntilStopped} disabled={runUntilStopped}
                onChange={(event) => setAutoNextOnLimit(event.target.checked)}
                className="mt-1 h-4 w-4 shrink-0 accent-amber-400" />
              <span className="min-w-0 flex-1">
              <span className="flex flex-wrap items-center justify-between gap-2">
                <span className="text-sm font-semibold text-amber-100">Start the next check immediately when the configured limit is reached</span>
                <span className={`rounded-full px-2 py-0.5 text-[10px] font-bold uppercase tracking-wide ${autoNextOnLimit && !runUntilStopped
                  ? 'bg-amber-400/15 text-amber-200' : 'bg-zinc-800 text-zinc-400'}`}>{runUntilStopped ? 'Paused' : autoNextOnLimit ? 'On' : 'Off'}</span>
              </span>
              </span>
            </label>
            <ViewText className="ml-7 mt-2" label="View limit details">
                <p id="rules-auto-next-limit-help" className="text-xs leading-5 text-zinc-400">
                  {runUntilStopped ? 'Per-window next-check limits are paused; the WIN/LOSS stop limits above remain active.' : autoNextLimitHelp}
                </p>
            </ViewText>
          </div>
        </div>
        {mode === 'ALL_RULES_TOGETHER' && <section className={`mt-3 rounded-xl border p-4 ${runUntilStopped
          ? 'border-zinc-800 bg-zinc-950/30 opacity-60'
          : autoNextLimitsEnabled ? 'border-violet-400/50 bg-violet-500/10 ring-1 ring-violet-400/20'
            : 'border-zinc-700 bg-zinc-950/40'}`} aria-labelledby="rules-auto-next-custom-limits-title">
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div className="flex min-w-0 items-start gap-3">
              <label className="flex min-w-0 flex-1 cursor-pointer items-start gap-3">
                <input type="checkbox" checked={autoNextLimitsEnabled && !runUntilStopped} disabled={runUntilStopped}
                  onChange={(event) => setAutoNextLimitsEnabled(event.target.checked)}
                  className="mt-1 h-4 w-4 shrink-0 accent-violet-400" />
                <span className="min-w-0">
                  <span id="rules-auto-next-custom-limits-title" className="block text-sm font-semibold text-violet-100">
                    New automatic next-check WIN/LOSS limits for this window
                  </span>
                  <span className="mt-1 block text-xs leading-5 text-zinc-400">
                    {runUntilStopped ? 'Per-window next-check thresholds are paused; session WIN/LOSS limits remain active.' : 'Set a net WIN or LOSS threshold.'}
                  </span>
                </span>
              </label>
              {!runUntilStopped && <ViewText label="View limit rules">
                <p className="max-w-2xl text-xs leading-5 text-zinc-400">A result of exactly 2.00x or less is a LOSS; only a result above 2.00x is a WIN. Limits use settled net outcome: WINs − LOSSes or LOSSes − WINs. Reaching a limit stops new selections; the next check waits for the window to finish.</p>
              </ViewText>}
            </div>
            <span className={`rounded-full px-2 py-0.5 text-[10px] font-bold uppercase tracking-wide ${autoNextLimitsEnabled && !runUntilStopped
              ? 'bg-violet-400/15 text-violet-200' : 'bg-zinc-800 text-zinc-400'}`}>{runUntilStopped ? 'Paused' : autoNextLimitsEnabled ? 'On' : 'Off'}</span>
          </div>
          <div className="mt-3 grid gap-3 sm:grid-cols-2">
            <label htmlFor="rules-auto-next-win-count" className="flex flex-col gap-1 text-xs text-zinc-300">
              Net WIN limit in this window
              <input id="rules-auto-next-win-count" type="number" min="1" step="1"
                value={autoNextWinCount} disabled={!autoNextLimitsEnabled || runUntilStopped}
                onChange={(event) => setAutoNextWinCount(event.target.value)}
                className={`${MANAGER_INPUT_CLASS} border-violet-500/25`} />
            </label>
            <label htmlFor="rules-auto-next-loss-count" className="flex flex-col gap-1 text-xs text-zinc-300">
              Net LOSS limit in this window
              <input id="rules-auto-next-loss-count" type="number" min="1" step="1"
                value={autoNextLossCount} disabled={!autoNextLimitsEnabled || runUntilStopped}
                onChange={(event) => setAutoNextLossCount(event.target.value)}
                className={`${MANAGER_INPUT_CLASS} border-violet-500/25`} />
            </label>
          </div>
        </section>}
      </fieldset>}
      {(mode === 'START_CHECK' || mode === 'LEGACY_SEPARATE_TRACKER' || mode === 'ALL_RULES_TOGETHER')
        && <ViewText className="mt-2" label="View session timing details">
          <p className="text-xs text-zinc-500">After the full-window trigger, the next SHADOW session starts from that final committed round; its first observation is the following verified, contiguous round.</p>
        </ViewText>}
      </div>

      <div className="mt-5 flex flex-col gap-3 border-t border-emerald-500/15 pt-4 sm:flex-row sm:flex-wrap sm:items-center">
        <Button tone="good" onClick={start} disabled={busy || Boolean(activeSession)}>
          {busyAction === 'start-shadow' ? 'Starting SHADOW session…' : 'Start SHADOW session'}
        </Button>
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
        <p className="basis-full text-xs text-zinc-500">WIN and LOSS results come from verified completed rounds; these are SHADOW observations.</p>
        <p role="status" className={`basis-full rounded-lg border px-3 py-2 text-xs ${activeSession
          ? 'border-amber-400/25 bg-amber-400/10 text-amber-200'
          : shadowReadiness?.ready === false ? 'border-rose-400/25 bg-rose-400/10 text-rose-200'
            : shadowReadiness?.ready && hasSelectedEnabledRule ? 'border-emerald-400/25 bg-emerald-400/10 text-emerald-200'
              : 'border-sky-400/20 bg-sky-400/5 text-sky-100'}`}>{startHint}</p>
      </div>
    </section>

    <section className="rounded-xl border border-zinc-800 bg-zinc-900/60 p-3">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div><h2 className="text-base font-semibold">{controlSession?.status === 'PAUSED' ? 'Paused session' : controlSession ? 'Current session' : displaySession ? 'Latest session' : 'Current session'}</h2>
          <p className="mt-1 text-xs text-zinc-400">{displaySession ? `${MODE_LABELS[displaySession.mode] || displaySession.mode} · ${sessionSelectionLabel(displaySession)}` : 'No active or saved session'}</p>
        </div>
        {controlSession && <div className="flex flex-wrap gap-2">
          {controlSession.status === 'RUNNING' && <Button tone="info" onClick={() => sessionAction(controlSession.session_id, 'pause')} disabled={busy || operationInFlightRef.current}>{busyAction === 'pause' ? 'Pausing…' : 'Pause'}</Button>}
          {controlSession.status === 'PAUSED' && controlSession.stop_reason === 'MANUAL_PAUSE'
            && <Button tone="good" onClick={() => sessionAction(controlSession.session_id, 'resume')} disabled={busy || operationInFlightRef.current}>{busyAction === 'resume' ? 'Resuming…' : 'Resume'}</Button>}
          <Button tone="danger" onClick={() => sessionAction(controlSession.session_id, 'stop')} disabled={busy || operationInFlightRef.current}>{busyAction === 'stop' ? 'Stopping…' : 'Stop'}</Button>
          <Button tone="reset" onClick={() => sessionAction(controlSession.session_id, 'reset')} disabled={busy || operationInFlightRef.current}>{busyAction === 'reset' ? 'Resetting…' : 'Reset'}</Button>
        </div>}
      </div>
      {displaySession && <>
        <div className="mt-3 grid gap-2 sm:grid-cols-2 xl:grid-cols-4">
          <Metric label="SHADOW source" value={sessionSelectionLabel(displaySession)}
            valueClassName={sessionSelectionLabel(displaySession) === 'Behaviour Detection' ? 'text-fuchsia-100' : 'text-zinc-100'}
            accentClassName={sessionSelectionLabel(displaySession) === 'Behaviour Detection' ? 'text-fuchsia-300' : 'text-emerald-300'}
            detail={sessionSelectionLabel(displaySession) === 'Behaviour Detection' ? 'Behaviour Detection · Rolling HIGH' : sessionSelectionDetail(displaySession)} />
          <Metric label="Session status" value={displaySession.status}
            valueClassName={displaySession.status === 'RUNNING' ? 'text-emerald-300' : displaySession.status === 'PAUSED' ? 'text-amber-200' : 'text-sky-200'}
            detail={displaySession.selection_status || displaySession.stop_reason || 'No stop reason'} />
          <Metric label="Verified rounds" value={`${displaySession.observed_rounds ?? 0}${displaySession.window_size == null ? '' : ` / ${displaySession.window_size}`}`}
            valueClassName="text-sky-200" accentClassName="text-sky-300"
            detail={`Current round ${receivedRound?.round_index ?? detailSession?.last_round_index ?? current?.round_index ?? '—'} · ${displaySession.window_size == null ? 'until you stop it' : 'verified window'}`} />
          <Metric label="SHADOW outcomes"
            value={`WIN ${displaySession.results?.WIN ?? 0} · LOSS ${displaySession.results?.LOSS ?? 0} · PENDING ${displaySession.results?.PENDING ?? 0}`}
            valueClassName="text-emerald-200" accentClassName="text-emerald-300"
            detail={`Net ${displaySession.net_outcome > 0 ? '+' : ''}${displaySession.net_outcome ?? 0} · ${Number(displaySession.net_profit_rwf ?? 0).toLocaleString()} RWF`} />
          {sessionSelectionLabel(displaySession) === 'Behaviour Detection' && <Metric label="HIGH eligibility"
            value={displaySession.behaviour_strategy?.eligible === true ? 'ELIGIBLE' : displaySession.behaviour_strategy ? 'NOT ELIGIBLE' : 'WAITING'}
            valueClassName={displaySession.behaviour_strategy?.eligible === true ? 'text-emerald-300' : 'text-amber-200'}
            detail={displaySession.behaviour_strategy?.reason || 'Awaiting the latest verified eligibility update.'} />}
        </div>
        <details className="mt-2 rounded-lg border border-zinc-800 bg-black/10 p-2.5">
          <summary className="cursor-pointer text-xs font-semibold text-zinc-300">Additional session metrics and limits</summary>
      <div className="mt-3 grid gap-2 sm:grid-cols-2 xl:grid-cols-4">
        <Metric label={sessionSelectionLabel(displaySession) === 'Behaviour Detection'
          ? 'Selected SHADOW source' : 'Rule selection'}
          value={sessionSelectionLabel(displaySession) === 'Behaviour Detection'
            ? 'Behaviour Detection · Rolling HIGH' : sessionSelectionLabel(displaySession)}
          valueClassName={sessionSelectionLabel(displaySession) === 'Behaviour Detection'
            ? 'text-fuchsia-100' : 'text-zinc-100'}
          className={sessionSelectionLabel(displaySession) === 'Behaviour Detection'
            ? 'border-fuchsia-400/25 bg-fuchsia-950/20' : ''}
          accentClassName={sessionSelectionLabel(displaySession) === 'Behaviour Detection'
            ? 'text-fuchsia-300' : 'text-emerald-300'}
          detail={sessionSelectionLabel(displaySession) === 'Behaviour Detection'
            ? `${displaySession.strategy_id || 'BEHAVIOUR_ROLLING_HIGH'} · ${sessionSelectionDetail(displaySession)}`
            : sessionSelectionDetail(displaySession)} />
        <Metric label="Session stop" value={displaySessionRunsUntilStopped
          ? (savedStopLimitLabels.length ? 'UNTIL LIMIT OR STOP' : 'UNTIL YOU STOP') : 'CONFIGURED LIMIT'}
          valueClassName={displaySessionRunsUntilStopped ? 'text-emerald-300' : 'text-amber-200'}
          className={displaySessionRunsUntilStopped ? 'border-emerald-400/25 bg-emerald-950/20' : 'border-amber-400/20 bg-amber-950/10'}
          detail={displaySessionRunsUntilStopped
            ? `${savedStopLimitLabels.length ? `Enabled limits: ${savedStopLimitLabels.join(' · ')}. A reached limit stops this session.` : 'No session limit is enabled.'} Use Stop to end it earlier; unverified round input can still pause it.`
            : `Maximum verified rounds: ${displaySession.window_size ?? 'not reported'}`} />
        <Metric label="Status" value={displaySession.status} valueClassName={displaySession.status === 'RUNNING'
          ? 'text-emerald-300' : displaySession.status === 'PAUSED' ? 'text-amber-200'
            : displaySession.integrity_stop_reason ? 'text-rose-300' : 'text-sky-200'}
          className={displaySession.status === 'RUNNING' ? 'border-emerald-400/25 bg-emerald-950/20'
            : displaySession.status === 'PAUSED' ? 'border-amber-400/30 bg-amber-950/20'
              : displaySession.integrity_stop_reason ? 'border-rose-400/25 bg-rose-950/20' : ''}
          detail={displaySession.selection_status} />
        <Metric label="Current round" value={receivedRound?.round_index ?? detailSession?.last_round_index ?? current?.round_index ?? '—'} valueClassName="text-sky-200" accentClassName="text-sky-300" className="border-sky-400/25 bg-sky-950/20" detailClassName="text-sky-100/60" detail={displaySession.window_size === null
          ? `${displaySession.observed_rounds ?? 0} verified · until you stop it`
          : `${displaySession.observed_rounds ?? 'UNKNOWN'}/${displaySession.window_size ?? 'UNKNOWN'} verified`} />
        <Metric label={displaySession.mode === 'FIVE_RULE_TRACKER' ? 'Independent tracker selections' : 'Combined selections'} value={displaySession.selection_count} valueClassName="text-violet-200" accentClassName="text-violet-300" className="border-violet-400/25 bg-violet-950/20" detailClassName="text-violet-100/60" detail="Created SHADOW targets for this session. API signal count includes accepted fresh targets across all sessions." />
        <Metric label="SHADOW wins" value={displaySession.results?.WIN ?? 0}
          valueClassName="text-emerald-300" accentClassName="text-emerald-300"
          className="border-emerald-400/25 bg-emerald-950/20"
          detail="Verified target rounds above 2.00x." />
        <Metric label="SHADOW losses" value={displaySession.results?.LOSS ?? 0}
          valueClassName="text-rose-300" accentClassName="text-rose-300"
          className="border-rose-400/25 bg-rose-950/20"
          detail="Verified target rounds at or below 2.00x." />
        <Metric label="Pending SHADOW signals" value={displaySession.results?.PENDING ?? 0}
          valueClassName="text-amber-200" accentClassName="text-amber-300"
          className="border-amber-400/25 bg-amber-950/20"
          detail="Accepted targets awaiting verified settlement." />
        {sessionSelectionLabel(displaySession) === 'Behaviour Detection' && <Metric
          label="HIGH eligibility"
          value={displaySession.behaviour_strategy?.eligible === true ? 'ELIGIBLE'
            : displaySession.behaviour_strategy ? 'NOT ELIGIBLE' : 'WAITING'}
          valueClassName={displaySession.behaviour_strategy?.eligible === true ? 'text-emerald-300'
            : displaySession.behaviour_strategy ? 'text-amber-200' : 'text-zinc-300'}
          detail={displaySession.behaviour_strategy?.reason || 'No eligibility update has reached this session yet.'} />}
        <Metric label="Net outcome" value={`${displaySession.net_outcome > 0 ? '+' : ''}${displaySession.net_outcome ?? 0}`} valueClassName={displaySession.net_outcome > 0 ? 'text-emerald-300' : displaySession.net_outcome < 0 ? 'text-rose-300' : 'text-cyan-200'} accentClassName={displaySession.net_outcome > 0 ? 'text-emerald-300' : displaySession.net_outcome < 0 ? 'text-rose-300' : 'text-cyan-300'} className={displaySession.net_outcome > 0 ? 'border-emerald-400/25 bg-emerald-950/20' : displaySession.net_outcome < 0 ? 'border-rose-400/25 bg-rose-950/20' : 'border-cyan-400/25 bg-cyan-950/20'} detailClassName="text-zinc-400" detail={`Settled WINs − LOSSes · WIN_LIMIT ${displaySession.win_limit_enabled ? displaySession.win_limit_count : 'OFF'} · LOSS_LIMIT ${displaySession.loss_limit_enabled ? displaySession.loss_limit_count : 'OFF'}`} />
        <Metric label="Shadow profit / loss" value={`${Number(displaySession.net_profit_rwf ?? 0) > 0 ? '+' : ''}${Number(displaySession.net_profit_rwf ?? 0).toLocaleString()} RWF`} valueClassName={Number(displaySession.net_profit_rwf ?? 0) > 0 ? 'text-emerald-300' : Number(displaySession.net_profit_rwf ?? 0) < 0 ? 'text-rose-300' : 'text-cyan-200'} accentClassName={Number(displaySession.net_profit_rwf ?? 0) > 0 ? 'text-emerald-300' : Number(displaySession.net_profit_rwf ?? 0) < 0 ? 'text-rose-300' : 'text-cyan-300'} className={Number(displaySession.net_profit_rwf ?? 0) > 0 ? 'border-emerald-400/25 bg-emerald-950/20' : Number(displaySession.net_profit_rwf ?? 0) < 0 ? 'border-rose-400/25 bg-rose-950/20' : 'border-cyan-400/25 bg-cyan-950/20'} detailClassName="text-zinc-400" detail={`Fixed stake ${Number(displaySession.requested_stake ?? 0).toLocaleString()} ${displaySession.requested_stake_currency || 'RWF'} · 2.00x target: ≤2.00 LOSS, >2.00 WIN · pending excluded`} />
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
      </div>
        </details>
      </>}
      {displaySession && sessionSelectionLabel(displaySession) === 'Behaviour Detection'
        && <BehaviourStrategySessionSummary />}
      {displaySession && progressStats && <section aria-label="Session progress" className="mt-4 grid gap-3 lg:grid-cols-2">
        <CountProgress label="Verified round window"
          completed={progressStats.observed}
          remaining={progressStats.windowSize == null ? null
            : Math.max(0, progressStats.windowSize - (progressStats.observed || 0))}
          total={progressStats.windowSize}
          detail={progressStats.windowSize == null
            ? 'This session keeps observing rounds until you stop it.'
            : `${progressStats.observed ?? 'UNKNOWN'} of ${progressStats.windowSize} verified rounds in this session.`}
          tone="sky" />
        <CountProgress label="Rule checks in latest round"
          completed={progressStats.ruleCompleted}
          remaining={Math.max(0, progressStats.ruleTotal - progressStats.ruleCompleted)}
          total={progressStats.ruleTotal}
          detail={latestSessionEvaluation
            ? `${progressStats.ruleTotal} participating rules checked for the latest verified source round.`
            : 'No verified rule evaluation has been recorded for this session yet.'}
          tone="violet" />
        <OutcomeProgress wins={progressStats.wins} losses={progressStats.losses}
          pending={progressStats.pending} total={progressStats.outcomeTotal} />
        {progressStats.maxSelections != null && <CountProgress label="Selection limit"
          completed={progressStats.selectionCount}
          remaining={Math.max(0, progressStats.maxSelections - progressStats.selectionCount)}
          total={progressStats.maxSelections}
          detail="Selections made out of the configured session maximum."
          tone="amber" />}
      </section>}
      {displaySession && <details className="mt-3 rounded-lg border border-zinc-800 bg-black/10 p-2.5">
        <summary className="cursor-pointer text-xs font-semibold text-zinc-300">
          {displaySession.automatic_rule_selection ? 'Automatic selection evidence' : displaySession.adaptive_rule_selection ? 'Adaptive selection evidence' : 'Rule selection evidence'}
          {' · '}{automaticChoiceCandidates.length || adaptiveSelectedCandidates.length || 0} candidates
        </summary>
      <section aria-label="Rule selection evidence" className={`mt-2 rounded-lg border p-3 ${displaySession.adaptive_rule_selection
        ? 'border-violet-400/25 bg-violet-950/15' : displaySession.automatic_rule_selection
          ? 'border-emerald-400/25 bg-emerald-950/15' : 'border-amber-400/25 bg-amber-950/15'}`}>
        {displaySession.automatic_rule_selection ? <>
          <div className="flex flex-wrap items-center justify-between gap-2">
            <h3 className="text-sm font-semibold text-emerald-100">Automatic selection evidence</h3>
            <span className="rounded-full border border-emerald-400/25 px-2.5 py-1 text-[11px] font-semibold text-emerald-200">
              {automaticChoice?.source_round_index != null
                ? `CHECKED SOURCE #${automaticChoice.source_round_index}` : 'WAITING FOR FIRST CHECK'}
            </span>
          </div>
          <p className="mt-2 text-sm text-zinc-200">
            {automaticChoice
              ? automaticChoiceCandidates.length > 0
                ? `${automaticChoiceCandidates.length} policies checked · ${automaticChoiceRuleIds.length} passed the performance gate · ${AUTO_RULE_REASON_LABELS[automaticChoice.reason] || automaticChoice.reason || 'status unavailable'}.`
                : `No candidate checks completed · ${AUTO_RULE_REASON_LABELS[automaticChoice.reason] || automaticChoice.reason || 'status unavailable'}.`
              : 'The session has not recorded an Automatic rule check yet.'}
          </p>
          <ViewText className="mt-1" label="View target count details">
          <p className="text-xs text-zinc-400">
            {latestSessionEvaluation?.decision
              ? `Latest rule decision: ${latestSessionEvaluation.decision.action || 'UNKNOWN'} · ${latestSessionEvaluation.decision.reason || 'reason unavailable'}. `
              : 'Latest per-rule decision details are still loading. '}
            WIN, LOSS and PENDING count created SHADOW targets; they stay at zero until an active rule matches and creates a target.
          </p>
          </ViewText>
          {automaticChoiceCandidates.length > 0 && <details className="mt-3">
            <summary className="cursor-pointer text-xs font-semibold text-zinc-300">View candidate results and reasons</summary>
            <div className="mt-2 space-y-1.5">
              {automaticChoiceCandidates.map((candidate) => {
                const displayCandidate = automaticCandidates.get(candidate.rule_id) || candidate;
                const ruleName = registry?.rules?.find((rule) => rule.rule_id === candidate.rule_id)?.name
                  || candidate.rule_id;
                const reason = AUTO_RULE_REASON_LABELS[displayCandidate.reason] || displayCandidate.reason || 'Awaiting evaluation';
                const hasWindow = displayCandidate.window_rounds != null;
                const windowSize = Math.max(1, Number(displayCandidate.window_size) || 100);
                const windowRounds = Math.min(windowSize, Math.max(0, Number(displayCandidate.window_rounds) || 0));
                const roundProgress = (windowRounds / windowSize) * 100;
                const wins = Math.max(0, Number(displayCandidate.wins) || 0);
                const losses = Math.max(0, Number(displayCandidate.losses) || 0);
                const settled = wins + losses;
                const winWidth = settled ? (wins / settled) * 100 : 0;
                const net = Number(displayCandidate.net_wins) || 0;
                const progressTone = net > 0 ? 'from-emerald-500 to-emerald-300'
                  : net < 0 ? 'from-rose-600 to-rose-400' : 'from-sky-500 to-cyan-300';
                return <div key={candidate.rule_id} className="rounded-lg border border-zinc-700/70 bg-zinc-950/35 p-2.5">
                  <div className="flex flex-wrap items-start justify-between gap-x-4 gap-y-1 text-xs">
                    <div className="min-w-0"><span className="font-semibold text-zinc-100">{ruleName}</span>
                      {hasWindow && <span className="ml-2 font-mono text-[10px] tabular-nums text-zinc-500">Rolling {windowRounds}/{windowSize} · {displayCandidate.recovered_rounds ?? 0} recovered</span>}
                      <span className={`ml-2 ${displayCandidate.performance_trend === 'RISING' ? 'text-emerald-200' : displayCandidate.performance_trend === 'FALLING' ? 'text-rose-200' : 'text-sky-200'}`}>{performanceMomentumSummary(displayCandidate)}</span>
                      <span className="ml-2 text-sky-200">{signalMomentumSummary(displayCandidate)}</span></div>
                    <span className="text-zinc-400">{reason}</span>
                  </div>
                  {hasWindow && <div className="mt-2">
                    <div className="mb-1 flex flex-wrap items-center justify-between gap-2 text-[10px] tabular-nums">
                      <span className="text-zinc-500">{Math.round(roundProgress)}% · latest 100 rounds</span>
                      <span className="flex gap-2 font-semibold">
                        <span className="text-emerald-300">{wins} WIN</span>
                        <span className="text-rose-300">{losses} LOSS</span>
                        <span className={net > 0 ? 'text-emerald-200' : net < 0 ? 'text-rose-200' : 'text-zinc-400'}>NET {net > 0 ? '+' : ''}{net}</span>
                      </span>
                    </div>
                    <div role="progressbar" aria-label={`${ruleName} 100-round rule window`}
                      aria-valuemin={0} aria-valuemax={windowSize} aria-valuenow={windowRounds}
                      aria-valuetext={`Rolling latest ${windowRounds} of ${windowSize} rounds`}
                      className="h-2.5 overflow-hidden rounded-full bg-zinc-800 ring-1 ring-inset ring-white/5">
                      <div className={`h-full rounded-full bg-gradient-to-r ${progressTone} transition-[width] duration-500 ease-out`}
                        style={{ width: `${roundProgress}%` }} />
                    </div>
                    <div role="img" aria-label={`${ruleName} settled results: ${wins} wins and ${losses} losses`}
                      className="mt-1.5 flex h-1.5 overflow-hidden rounded-full bg-zinc-800">
                      {settled > 0 && <><div className="bg-emerald-400" style={{ width: `${winWidth}%` }} />
                        <div className="bg-rose-400" style={{ width: `${100 - winWidth}%` }} /></>}
                    </div>
                  </div>}
                </div>;
              })}
            </div>
          </details>}
        </> : displaySession.adaptive_rule_selection ? <>
          <div className="flex flex-wrap items-center justify-between gap-2">
            <h3 className="text-sm font-semibold text-violet-100">Adaptive portfolio evidence</h3>
            <span className="rounded-full border border-violet-400/25 px-2.5 py-1 text-[11px] font-semibold text-violet-200">
              {adaptiveChoice?.evaluated_at || 'WAITING FOR FIRST CHECK'} · version {adaptiveChoice?.selection_version ?? 0}
            </span>
          </div>
          <p className="mt-2 text-sm text-zinc-200">
            {adaptiveChoice ? `${adaptiveSelectedCandidates.length} rules selected · ${adaptiveChoice.reason}. `
              : 'Adaptive has not completed a rule check yet. '}
            The selected rules are listed in the session summary above.
          </p>
          <p className="mt-1 text-xs text-zinc-400">
            Forward portfolio: {displaySession.adaptive_portfolio_performance?.wins ?? 0} WIN · {displaySession.adaptive_portfolio_performance?.losses ?? 0} LOSS · {displaySession.adaptive_portfolio_performance?.pending ?? 0} PENDING, counting each shared target once. Per-rule prediction results remain separate in each rule ledger.
          </p>
          {displaySession.adaptive_baseline_comparison && <p className="mt-1 text-xs text-sky-200">
            Baseline status: Automatic {displaySession.adaptive_baseline_comparison.automatic?.status || 'not comparable yet'} · fixed rule {displaySession.adaptive_baseline_comparison.fixed_rule?.status || 'collecting forward outcomes'}.
          </p>}
          {adaptiveChoice && <details className="mt-3 rounded-lg border border-zinc-700 p-3">
            <summary className="cursor-pointer text-xs font-semibold text-zinc-300">View selection reason and candidate evidence</summary>
            <pre className="mt-2 max-h-72 overflow-auto whitespace-pre-wrap text-xs text-zinc-400">{JSON.stringify(adaptiveChoice, null, 2)}</pre>
          </details>}
        </> : null}
      </section>
      </details>}
      {displaySession && <details className="mt-2 rounded-lg border border-sky-400/15 bg-sky-950/10 p-2.5 text-xs text-zinc-300">
        <summary className="cursor-pointer font-semibold text-sky-200">Round receipt, settlement and timing detail</summary>
      <div className="mt-2 grid gap-2 sm:grid-cols-2 xl:grid-cols-4">
        <div><span className="text-zinc-500">Last received round</span><div className="mt-1 break-all">{receivedRound?.round_id || 'UNKNOWN'}{receivedRound?.round_index != null ? ` · #${receivedRound.round_index}` : ''}</div><div className="mt-1 text-zinc-500">Verified at {receivedRound?.observed_at || 'UNKNOWN'} · received {receivedRound?.received_at || 'UNKNOWN'}</div></div>
        <div><span className="text-zinc-500">Last settled target</span><div className="mt-1 break-all">{lastSettledTarget?.round_id || 'NONE'}{lastSettledTarget?.round_index != null ? ` · #${lastSettledTarget.round_index}` : ''}</div><div className="mt-1 text-zinc-500">{lastSettledTarget ? `${lastSettledTarget.settlement_count} selection(s) · ${lastSettledTarget.settled_at || 'settlement time UNKNOWN'}` : 'No target settlement recorded'}</div></div>
        <div><span className="text-zinc-500">Last evaluation</span><div className="mt-1">{displaySession.last_evaluation_at || 'UNKNOWN'}</div><div className="mt-1 text-zinc-500">Session {displaySession.is_active === true ? 'active' : displaySession.is_active === false ? 'not active' : 'activity UNKNOWN'}</div></div>
        <div><span className="text-zinc-500">Event processing latency</span><div className="mt-1">{displaySession.event_processing_latency_ms != null && Number.isFinite(Number(displaySession.event_processing_latency_ms)) ? `${Number(displaySession.event_processing_latency_ms).toFixed(3)} ms` : 'UNKNOWN'}</div><div className="mt-1 text-zinc-500">Settlement {displaySession.settlement_processing_latency_ms != null && Number.isFinite(Number(displaySession.settlement_processing_latency_ms)) ? `${Number(displaySession.settlement_processing_latency_ms).toFixed(3)} ms` : 'No settlement sample'} · durable p50/p95/p99 {settlementLatencySummary.samples ? `${settlementLatencySummary.p50_ms}/${settlementLatencySummary.p95_ms}/${settlementLatencySummary.p99_ms} ms` : 'UNKNOWN'} (${settlementLatencySummary.samples || 0} samples)</div></div>
      </div>
      </details>}
      {detailSession?.source_performance && <details className="mt-2 rounded-lg border border-violet-400/15 bg-violet-950/10 p-2.5">
        <summary className="cursor-pointer text-xs font-semibold text-violet-200">Per-source performance</summary>
      <div className="mt-2 grid gap-2 sm:grid-cols-2">
        {Object.values(detailSession.source_performance).map((source) => <div key={source.source_label} className="rounded-lg border border-violet-400/15 bg-violet-950/10 p-3 text-xs">
          <b className="text-violet-200">{source.source_label}</b>
          <p className="mt-1 text-zinc-300">{source.selections} signals · WIN {source.wins} · LOSS {source.losses} · PENDING {source.pending} · net {Number(source.net_profit_rwf) > 0 ? '+' : ''}{Number(source.net_profit_rwf).toLocaleString()} RWF</p>
        </div>)}
      </div>
      </details>}
      {detailSession && <>
        <details className="mt-5 rounded-lg border border-zinc-800 p-4">
          <summary className="cursor-pointer text-sm font-semibold text-zinc-200">View selections and resolved target outcomes</summary>
          <div className="mt-3 overflow-x-auto">
            <table className="w-full min-w-[1320px] text-left text-xs">
              <thead className="text-zinc-500"><tr><th className="py-2">Decision / source</th><th>Target round</th><th>Matched rules</th><th>Stake / model</th><th>Settlement</th><th>Verified target evidence</th><th>Shadow P/L</th></tr></thead>
              <tbody>{allSelections.slice(0, 100).map((selection) => <tr key={selection.selection_id} className="border-t border-zinc-800 align-top">
                <td className="py-2"><code className="break-all text-zinc-300">{selection.decision_id || selection.selection_id || 'UNKNOWN'}</code>{selection.source_label && <div className="mt-1 font-semibold text-emerald-200">{selection.source_label}</div>}{selection.source_id === 'BEHAVIOUR_DETECTION' && <div className="mt-1 text-sky-200">{selection.part} · {selection.batch_id} · {selection.direction} · horizon {selection.trigger_horizon}</div>}{selection.series_id && <div className="break-all text-zinc-500">Series {selection.series_id}</div>}<div className="mt-2">Source #{selection.source_round?.roundIndex ?? 'UNKNOWN'}</div><div className="break-all text-zinc-500">{selection.source_round_id || selection.source_round?.roundId || 'UNKNOWN'}</div><div className="mt-1 text-zinc-500">{selection.selected_at || 'Selection time UNKNOWN'}</div></td>
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
          <summary className="cursor-pointer text-sm font-semibold text-zinc-200">View per-rule diagnostics and source attribution</summary>
          <div className="mt-3 space-y-3">
            {recentEvaluations.map((entry, index) => <article key={`${entry.source_round?.roundId}-${index}`} className="rounded-lg border border-zinc-800 bg-zinc-950/60 p-3 text-xs">
              <div className="flex flex-wrap justify-between gap-2 text-zinc-300">
                <span>Source #{entry.source_round?.roundIndex} · {entry.source_round?.roundId} · {entry.source_round?.multiplier}x</span>
                <span>Target #{entry.target_round?.roundIndex} · {entry.evaluated_at}</span>
              </div>
              <div className="mt-2 font-semibold text-emerald-200">{entry.decision?.action || 'WAIT'} · {entry.decision?.true_rule_ids?.join(', ') || 'no contributing rules'}</div>
              {entry.automatic_rule_choice && <p className="mt-1 text-zinc-400">
                Automatic choice: {entry.automatic_rule_choice.rule_ids?.join(', ') || 'none'}.
                A rule can be TRUE but won’t contribute unless the chooser selected it for this round.
              </p>}
              <div className="mt-2 space-y-1">{(entry.rule_evaluations || []).map((evaluation) => {
                const candidate = entry.automatic_rule_choice?.candidates?.find(
                  (item) => item.rule_id === evaluation.rule_id);
                return <div key={evaluation.rule_id} className="flex flex-wrap gap-x-2">
                  <b className={evaluation.eligible === true ? 'text-emerald-300' : evaluation.eligible === false ? 'text-zinc-300' : 'text-amber-300'}>{evaluation.status}</b>
                  <code className="text-zinc-300">{evaluation.rule_id}</code>
                  {evaluation.eligible === true && evaluation.selected_by_automatic_rule === false
                    && <span className="font-semibold text-amber-200">TRUE · NOT CHOSEN BY AUTO · {AUTO_RULE_REASON_LABELS[candidate?.reason] || candidate?.reason || 'Not qualified this round'}</span>}
                  {evaluation.eligible === true && evaluation.selected_by_automatic_rule === true
                    && <span className="font-semibold text-emerald-200">CHOSEN BY AUTO</span>}
                  <span className="text-zinc-400">{evaluation.reason}</span>
                </div>;
              })}</div>
              <details className="mt-2 text-zinc-500"><summary className="cursor-pointer">View private state</summary><pre className="mt-2 max-h-72 overflow-auto whitespace-pre-wrap">{JSON.stringify(entry.rule_evaluations?.map((row) => ({ rule_id: row.rule_id, state_snapshot: row.state_snapshot })), null, 2)}</pre></details>
            </article>)}
            {!recentEvaluations.length && <p className="text-sm text-zinc-500">No source evaluations recorded.</p>}
          </div>
        </details>
      </>}
    </section>
    <section aria-label="Saved and inactive SHADOW sessions" className="rounded-xl border border-zinc-800 bg-zinc-900/60 p-3">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h2 className="text-base font-semibold">Saved and inactive SHADOW sessions</h2>
            <ViewText className="mt-1" label="View session history details">
              <p className="text-sm text-zinc-400">Newest completed, settling, paused, and stopped sessions appear here. Deleting a settling session stops its observation and removes its simulated results.</p>
            </ViewText>
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
      <details className="mt-3 rounded-lg border border-zinc-800 bg-black/10 p-2.5">
        <summary className="cursor-pointer text-xs font-semibold text-zinc-300">Show saved session records · {finishedSessions.length}</summary>
      <div className="mt-2 space-y-2">
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
          return <article key={session.session_id} className="grid gap-3 rounded-lg border border-zinc-800 bg-gradient-to-br from-zinc-950/80 to-zinc-900/50 p-3 shadow-sm shadow-black/20 sm:grid-cols-[minmax(0,1fr)_auto] sm:items-center">
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
                <span className="rounded-full border border-sky-400/20 px-2 py-1 text-[11px] text-sky-200">{sessionSelectionLabel(session)}</span>
                <span className={`rounded-full border px-2.5 py-1 text-[11px] font-bold tracking-wide ${statusStyle}`}>{session.status}</span>
                <span className={`text-sm font-semibold ${!profitKnown ? 'text-zinc-500' : profit > 0 ? 'text-emerald-300' : profit < 0 ? 'text-rose-300' : 'text-zinc-300'}`}>
                  {profitKnown ? `${profit > 0 ? '+' : ''}${profit.toLocaleString()} RWF` : 'P/L UNKNOWN'}
                </span>
              </div>
              <details className="mt-2 text-[11px] text-zinc-500">
                <summary className="cursor-pointer">Details · {session.selection_count ?? '—'} selections · WIN {session.results?.WIN ?? '—'} / LOSS {session.results?.LOSS ?? '—'} / PENDING {pending ?? '—'}</summary>
                <p className="mt-1 break-all font-mono">{session.session_id}</p>
                <p className="mt-1 flex flex-wrap gap-x-3 gap-y-1">
                <span>Started {session.started_at || 'unknown'}</span>
                {session.ended_at && <span>Ended {session.ended_at}</span>}
                </p>
              </details>
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
      </details>
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
          <Metric label="Shadow profit / loss" value={`${Number(historyDetail.net_profit_rwf ?? 0) > 0 ? '+' : ''}${Number(historyDetail.net_profit_rwf ?? 0).toLocaleString()} RWF`} valueClassName={Number(historyDetail.net_profit_rwf ?? 0) > 0 ? 'text-emerald-300' : Number(historyDetail.net_profit_rwf ?? 0) < 0 ? 'text-rose-300' : 'text-zinc-100'} detail={`Fixed stake ${historyStake.toLocaleString()} ${historyCurrency} · 2.00x target: ≤2.00 LOSS, >2.00 WIN`} />
          <Metric label="Settled results" value={`WIN ${historyDetail.total_wins ?? historyDetail.results?.WIN ?? 0} · LOSS ${historyDetail.total_losses ?? historyDetail.results?.LOSS ?? 0}`} detail={`PENDING ${historyDetail.results?.PENDING ?? historySelections.filter((item) => item.status === 'PENDING').length}`} />
          <Metric label="Stop reason" value={historyDetail.stop_reason || historyDetail.selection_stop_reason || 'NONE'} detail={`${historyDetail.stop_reason_source || 'Source unknown'} · ${historyDetail.stop_reason_at || 'timestamp unavailable'}${historyDetail.stop_reason_round_id ? ` · round ${historyDetail.stop_reason_round_id}` : ''}`} />
        </div>
        <section className="mt-4 rounded-lg border border-sky-400/20 bg-sky-950/10 p-4" aria-label="Saved rule evaluation results">
          <div className="flex flex-wrap items-start justify-between gap-2">
            <div>
              <h4 className="text-sm font-semibold text-sky-100">Rule evaluation results</h4>
              <p className="mt-1 text-xs text-zinc-400">
                {historyEvaluations.length} source rounds evaluated out of {historyDetail.observed_rounds ?? historyDetail.window_size ?? '—'} observed.
              </p>
              {historyPausedRounds > 0 && <ViewText className="mt-1" label="View pause details">
                <p className="text-xs text-zinc-500">{historyPausedRounds} round{historyPausedRounds === 1 ? '' : 's'} occurred during manual pause and were not evaluated.</p>
              </ViewText>}
              <ViewText className="mt-1" label={`View ${historyDetail.automatic_rule_selection ? 'monitored rules' : 'session rules'}`}>
                <p className="text-xs text-zinc-500">{(historyDetail.manifest?.rules || []).map((rule) => rule.name || rule.rule_id).join(' · ')
                  || (historyDetail.rule_ids || []).join(' · ') || 'None'}</p>
              </ViewText>
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
          {historyEvaluationStats.targetAlreadyCompleted > 0 && <ViewText className="mt-3" label="View replay details">
            <p className="rounded-md border border-amber-400/20 bg-amber-400/5 px-3 py-2 text-xs leading-5 text-amber-100/90">
              These historical rounds were replayed after their targets had finished. They did not create backdated selections; verified target results synchronize the rule state.
            </p>
          </ViewText>}
          {historyEvaluations.length > 0 && <details className="mt-3 rounded-lg border border-zinc-800 bg-zinc-950/40 p-3">
            <summary className="cursor-pointer text-xs font-semibold text-zinc-200">View source-by-source evaluations</summary>
            <div className="mt-3 max-h-[34rem] space-y-2 overflow-y-auto pr-1">
              {[...historyEvaluations].reverse().map((entry, index) => <article key={`${entry.source_round?.roundId || 'source'}-${index}`} className="rounded-md border border-zinc-800 bg-zinc-900/50 p-3">
                <div className="flex flex-wrap justify-between gap-2 text-xs text-zinc-300">
                  <span>Source #{entry.source_round?.roundIndex ?? '—'} · {entry.source_round?.multiplier ?? '—'}x · {entry.source_round?.roundId || 'unknown round'}</span>
                  <span>{entry.decision?.action || 'WAIT'} · target #{entry.target_round?.roundIndex ?? '—'}</span>
                </div>
                {entry.automatic_rule_choice && <p className="mt-1 text-xs text-sky-200">
                  Automatic choice: {entry.automatic_rule_choice.rule_ids?.join(', ') || 'none'} · {AUTO_RULE_REASON_LABELS[entry.automatic_rule_choice.reason] || entry.automatic_rule_choice.reason}
                </p>}
                <div className="mt-2 space-y-1.5">
                  {(entry.rule_evaluations || []).map((evaluation) => {
                    const displayStatus = displayEvaluationStatus(evaluation.status);
                    const candidate = entry.automatic_rule_choice?.candidates?.find(
                      (item) => item.rule_id === evaluation.rule_id);
                    return <div key={evaluation.rule_id} className="flex flex-wrap gap-x-2 text-xs">
                      <b className={displayStatus === 'TARGET_ALREADY_COMPLETED' ? 'text-amber-200' : evaluation.eligible === true ? 'text-emerald-300' : evaluation.eligible === false ? 'text-zinc-300' : 'text-violet-200'}>{displayStatus}</b>
                      <code className="text-zinc-300">{evaluation.rule_id || 'unknown rule'}</code>
                      {evaluation.eligible === true && evaluation.selected_by_automatic_rule === false
                        && <span className="font-semibold text-amber-200">TRUE · NOT CHOSEN BY AUTO · {AUTO_RULE_REASON_LABELS[candidate?.reason] || candidate?.reason || 'Not qualified this round'}</span>}
                      {evaluation.eligible === true && evaluation.selected_by_automatic_rule === true
                        && <span className="font-semibold text-emerald-200">CHOSEN BY AUTO</span>}
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
                <td className="py-2"><code className="break-all text-zinc-300">{selection.decision_id || selection.selection_id || 'UNKNOWN'}</code>{selection.source_label && <div className="mt-1 font-semibold text-emerald-200">{selection.source_label}</div>}{selection.source_id === 'BEHAVIOUR_DETECTION' && <div className="mt-1 text-sky-200">{selection.part} · {selection.batch_id} · {selection.direction} · horizon {selection.trigger_horizon}</div>}{selection.series_id && <div className="break-all text-zinc-500">Series {selection.series_id}</div>}<div className="mt-2">Source #{selection.source_round?.roundIndex ?? 'UNKNOWN'}</div><div className="break-all text-zinc-500">{selection.source_round_id || selection.source_round?.roundId || 'UNKNOWN'}</div><div className="mt-1 text-zinc-500">{selection.selected_at || 'Selection time UNKNOWN'}</div></td>
                <td>#{selection.expected_target?.roundIndex ?? 'UNKNOWN'}<br /><span className="break-all text-zinc-400">{selection.target_round_id || selection.actual_target?.roundId || 'AWAITING_VERIFIED_TARGET'}</span></td>
                <td>{(selection.matched_rule_ids || selection.contributing_rule_ids || (selection.rule_id ? [selection.rule_id] : [])).join(', ') || 'UNKNOWN'}</td>
                <td>{(selection.stake_rwf ?? historyDetail.configuration_snapshot?.fixed_stake_rwf) == null ? 'UNKNOWN' : `${selectionStake.toLocaleString()} ${historyCurrency}`}<div className="mt-1 text-zinc-500">{selection.payout_multiplier ?? historyDetail.configuration_snapshot?.cashout_multiplier ?? 2.0}x</div></td>
                <td className={selection.status === 'WIN' ? 'text-emerald-300' : selection.status === 'LOSS' ? 'text-rose-300' : 'text-amber-200'}>{selection.settlement_status || selection.status || 'UNKNOWN'}<div className="mt-1 text-zinc-400">{selection.settlement_block_reason || (selection.status === 'PENDING' ? `Waiting for verified target #${selection.expected_target?.roundIndex ?? 'UNKNOWN'}` : selection.settlement_phase || 'UNKNOWN')}</div><div className="mt-1">{selection.actual_multiplier == null ? 'Multiplier PENDING' : `${selection.actual_multiplier}x`}</div><div className="mt-1 text-zinc-500">{selection.settled_at || selection.resolved_at || 'Settlement time UNKNOWN'}</div></td>
                <td><div>{selection.target_persistence_status || selection.settlement_evidence?.durability || 'UNKNOWN'}</div><div className="text-zinc-500">{selection.settlement_evidence?.persistence_confirmation?.event_type || selection.settlement_evidence?.event_type || 'Event type UNKNOWN'}</div><div className="break-all text-zinc-500">{selection.settlement_evidence?.target_round_id || selection.target_round_id || 'Target ID UNKNOWN'}</div><div className="text-zinc-500">Observed {selection.target_round_completed_at || selection.settlement_evidence?.target_observed_at || 'UNKNOWN'}</div></td>
                <td className={selectionProfit > 0 ? 'text-emerald-300' : selectionProfit < 0 ? 'text-rose-300' : 'text-zinc-400'}>{selectionProfit == null ? 'PENDING' : `${selectionProfit > 0 ? '+' : ''}${Number(selectionProfit).toLocaleString()} ${historyCurrency}`}{selection.payout_rwf != null && <div className="mt-1 text-zinc-500">Payout {Number(selection.payout_rwf).toLocaleString()} {historyCurrency}</div>}</td>
              </tr>;
            })}</tbody>
          </table>
          {!historySelections.length && <div className="py-4 text-sm text-zinc-500">
            <p>No SHADOW selections were recorded.</p>
            <ViewText className="mt-1" label="View evaluation details">
              <p>The evaluation audit above shows which rules returned FALSE and which matches arrived after their target rounds had completed.</p>
            </ViewText>
          </div>}
        </div>
        </section>
      </div>}
    </section>
  </main>;
}
