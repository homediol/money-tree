import { useCallback, useEffect, useMemo, useState } from 'react';
import { Activity, Database, ListChecks, ShieldCheck } from 'lucide-react';
import { api } from '../services/api.js';
import { currentExecutionIntents, currentSelections } from '../services/rulesEngineView.js';

const MODE_LABELS = {
  START_CHECK: 'Start a Check',
  LEGACY_SEPARATE_TRACKER: 'Legacy Separate Tracker',
  FIVE_RULE_TRACKER: 'Five Rules · Shared SELECT',
  ALL_RULES_TOGETHER: 'All Rules Together',
};

function Metric({ label, value, detail, icon: Icon = Activity }) {
  return <article className="rounded-xl border border-zinc-800 bg-zinc-900/70 p-4">
    <div className="flex items-center gap-2 text-xs font-semibold uppercase tracking-wide text-zinc-400">
      <Icon size={15} className="text-emerald-300" />{label}
    </div>
    <div className="mt-3 text-lg font-semibold text-zinc-100">{value}</div>
    {detail && <div className="mt-1 text-xs text-zinc-500">{detail}</div>}
  </article>;
}

function Button({ children, onClick, disabled = false, tone = 'neutral' }) {
  const colors = tone === 'danger' ? 'border-rose-500/40 text-rose-200 hover:bg-rose-500/10'
    : tone === 'good' ? 'border-emerald-500/40 text-emerald-200 hover:bg-emerald-500/10'
      : 'border-zinc-700 text-zinc-200 hover:bg-zinc-800';
  return <button type="button" onClick={onClick} disabled={disabled}
    className={`rounded-lg border px-3 py-2 text-sm font-medium disabled:cursor-not-allowed disabled:opacity-40 ${colors}`}>
    {children}
  </button>;
}

export default function RulesEngine() {
  const [status, setStatus] = useState(null);
  const [executionMode, setExecutionMode] = useState(null);
  const [dashboard, setDashboard] = useState(null);
  const [registry, setRegistry] = useState(null);
  const [sessions, setSessions] = useState([]);
  const [detailSession, setDetailSession] = useState(null);
  const [mode, setMode] = useState('ALL_RULES_TOGETHER');
  const [selectedIds, setSelectedIds] = useState([]);
  const [error, setError] = useState('');
  const [statusError, setStatusError] = useState('');
  const [busy, setBusy] = useState(false);
  const [busyAction, setBusyAction] = useState('');
  const [presetName, setPresetName] = useState('');
  const [presetChoice, setPresetChoice] = useState('');
  const [winEnabled, setWinEnabled] = useState(false);
  const [winCount, setWinCount] = useState(1);
  const [lossEnabled, setLossEnabled] = useState(false);
  const [lossCount, setLossCount] = useState(2);
  const [maxSelections, setMaxSelections] = useState('');
  const [maxObservedRounds, setMaxObservedRounds] = useState(50);
  const [autoNext, setAutoNext] = useState(false);

  const refresh = useCallback(async () => {
    try {
      const [s, r, active, modeStatus, dashboardStatus] = await Promise.all([
        api.get('/api/rules-engine/status'), api.get('/api/rules-engine/registry'), api.get('/api/rules-engine/sessions'),
        api.get('/api/betting-mode/status').catch(() => null),
        api.get('/api/rules-engine/dashboard').catch(() => null),
      ]);
      setStatus(s.data);
      setExecutionMode(modeStatus?.data?.mode || null);
      setDashboard(dashboardStatus?.data || null);
      setRegistry(r.data);
      setSessions(active.data.sessions || []);
      const selectedSession = s.data.active_session || active.data.sessions?.[0];
      if (selectedSession?.session_id) {
        const detail = await api.get(`/api/rules-engine/sessions/${encodeURIComponent(selectedSession.session_id)}`);
        setDetailSession(detail.data);
      } else setDetailSession(null);
      setStatusError('');
    } catch {
      setStatusError('Rules Engine status is unavailable. Monitoring data may be stale; check the backend and PostgreSQL health.');
    }
  }, []);

  useEffect(() => {
    refresh();
    const timer = window.setInterval(refresh, 5000);
    return () => window.clearInterval(timer);
  }, [refresh]);

  const modeDefinition = useMemo(() => registry?.modes?.find((item) => item.id === mode), [registry, mode]);
  const availableRules = useMemo(() => {
    const ids = new Set(modeDefinition?.rule_ids || []);
    return (registry?.rules || []).filter((rule) => ids.has(rule.rule_id));
  }, [modeDefinition, registry]);
  const presets = useMemo(() => Object.values(status?.presets || {}), [status]);
  const activeSession = status?.active_session;
  const settlementIntegrity = dashboard?.settlement_integrity || activeSession?.settlement_integrity || {};
  const liveModeActive = executionMode?.mode === 'LIVE_REAL';
  const current = status?.latest_real_round;
  const modeConflict = sessions.find((session) => {
    const isBlocking = session.blocks_new_session === true
      || (session.blocks_new_session == null && session.selection_status === 'ACTIVE'
        && (session.status === 'RUNNING'
          || (session.status === 'PAUSED'
            && [null, undefined, 'MANUAL_PAUSE', 'UNKNOWN_TRANSACTION_REQUIRES_RECONCILIATION']
              .includes(session.stop_reason))));
    return session.mode === mode && isBlocking
      && (!current?.stream_id || !session.stream_id || session.stream_id === current.stream_id);
  }) || (activeSession?.mode === mode && activeSession?.selection_status === 'ACTIVE'
    ? activeSession : null);
  const collector = status?.collector_status || {};
  const shadowReadiness = status?.shadow_readiness;
  const hasEnabledSelection = selectedIds.some((id) => availableRules.find((rule) => rule.rule_id === id)?.enabled);
  const readinessBlockers = shadowReadiness?.ready === false
    ? (shadowReadiness.blockers?.length ? shadowReadiness.blockers : ['SHADOW_NOT_READY']) : [];
  const collectorState = collector.state || collector.health || collector.status || 'UNKNOWN';
  const latestLabel = current
    ? `${Number(current.multiplier).toFixed(2)}x${current.round_index != null ? ` · #${current.round_index}` : ''}`
    : 'NONE';
  const latestEvaluation = detailSession?.evaluations?.slice(-1)[0] || null;
  const executionIntents = currentExecutionIntents(dashboard?.execution_intents || []);
  const latestIntent = executionIntents[0] || null;
  const targetRound = latestIntent?.target_round_index
    ?? latestEvaluation?.target_round?.roundIndex ?? 'UNKNOWN';
  const sourceObservedAt = latestEvaluation?.source_round?.observed_at
    || latestEvaluation?.source_round?.timestamp;
  const decisionLatencyMs = sourceObservedAt && latestEvaluation?.evaluated_at
    ? Date.parse(latestEvaluation.evaluated_at) - Date.parse(sourceObservedAt) : null;
  const runMode = liveModeActive ? 'LIVE_REAL · execution disabled'
    : activeSession ? 'Rules observing · executor disabled'
      : executionMode?.mode || 'UNCONFIGURED';

  const withBusy = async (operation, action = 'request') => {
    setBusy(true);
    setBusyAction(action);
    setError('');
    try {
      await operation();
      await refresh();
    } catch (failure) {
      const detail = failure?.response?.data?.detail;
      setError(detail === 'SESSION_ALREADY_ACTIVE_FOR_MODE'
        ? 'This mode already has an active session. Refresh the session status and use “Stop current session & start new”.'
        : typeof detail === 'string' ? detail
        : detail?.reason || detail?.message || failure?.response?.data?.error
        || (!failure?.response
          ? 'The API response was unavailable; the session start is not confirmed. Check current session status before retrying.'
          : 'The Rules Engine request failed.'));
    } finally {
      setBusy(false);
      setBusyAction('');
    }
  };

  const toggleRule = (rule, enabled) => withBusy(async () => {
    const parameters = rule.rule_id === 'USER_PLAYING_POLICY_START_CHECK'
      || rule.rule_id === 'USER_PLAYING_POLICY_LEGACY_TRACKER'
      ? { window_size: 50, auto_next_check_enabled: autoNext } : {};
    await api.put(`/api/rules-engine/configuration/${encodeURIComponent(rule.rule_id)}`, { enabled, parameters });
    setSelectedIds((previous) => enabled ? [...new Set([...previous, rule.rule_id])]
      : previous.filter((id) => id !== rule.rule_id));
  });

  const applyPreset = (name) => {
    setPresetChoice(name);
    const preset = presets.find((item) => item.name === name);
    if (!preset) return;
    setMode(preset.mode);
    setSelectedIds(preset.rule_ids || []);
    const options = preset.options || {};
    setWinEnabled(Boolean(options.win_limit_enabled));
    setWinCount(options.win_limit_count ?? 1);
    setLossEnabled(Boolean(options.loss_limit_enabled));
    setLossCount(options.loss_limit_count ?? 2);
    setMaxSelections(options.max_selections ?? '');
    setMaxObservedRounds(options.max_observed_rounds ?? 50);
    setAutoNext(Boolean(options.auto_next_check_enabled));
    const disabled = (preset.rule_ids || []).filter((id) => !registry?.rules?.find((rule) => rule.rule_id === id)?.enabled);
    setError(disabled.length ? `Preset loaded. Enable its disabled rules before starting: ${disabled.join(', ')}` : '');
  };

  const start = () => {
    return withBusy(async () => {
    const participatingRuleIds = selectedIds.filter((id) => availableRules.find((rule) => rule.rule_id === id)?.enabled);
    const options = mode === 'ALL_RULES_TOGETHER' ? {
      window_size: 50, max_observed_rounds: Number(maxObservedRounds),
      win_limit_enabled: winEnabled, win_limit_count: winEnabled ? Number(winCount) : null,
      loss_limit_enabled: lossEnabled, loss_limit_count: lossEnabled ? Number(lossCount) : null,
      max_selections: maxSelections === '' ? null : Number(maxSelections),
      auto_next_check_enabled: autoNext,
    } : mode === 'START_CHECK' || mode === 'LEGACY_SEPARATE_TRACKER' ? {
      window_size: 50, max_observed_rounds: Number(maxObservedRounds),
      auto_next_check_enabled: autoNext,
    } : { window_size: 50, max_observed_rounds: Number(maxObservedRounds) };
    const response = await api.post('/api/rules-engine/sessions', { mode, rule_ids: participatingRuleIds,
      options, replace_active_session: Boolean(modeConflict) });
    if (response.data?.status === 'RUNNING' && response.data?.session_id) {
      setStatus((previous) => previous ? { ...previous, status: 'RUNNING',
        active_session: response.data } : previous);
    }
  }, 'start-shadow');
  };

  const sessionAction = (sessionId, action) => withBusy(() =>
    api.post(`/api/rules-engine/sessions/${encodeURIComponent(sessionId)}/${action}`), action);

  const savePreset = () => withBusy(async () => {
    const options = { window_size: 50, max_observed_rounds: Number(maxObservedRounds),
      auto_next_check_enabled: autoNext };
    if (mode === 'ALL_RULES_TOGETHER') Object.assign(options, {
      win_limit_enabled: winEnabled,
      win_limit_count: winEnabled ? Number(winCount) : null,
      loss_limit_enabled: lossEnabled, loss_limit_count: lossEnabled ? Number(lossCount) : null,
      max_selections: maxSelections === '' ? null : Number(maxSelections),
    });
    await api.post('/api/rules-engine/presets', { name: presetName, mode, rule_ids: selectedIds,
      options });
    setPresetName('');
  });

  const recentEvaluations = detailSession?.evaluations?.slice(-12).reverse() || [];
  const allSelections = currentSelections(detailSession?.mode === 'ALL_RULES_TOGETHER'
    ? [...(detailSession.selections || [])].reverse()
    : Object.values(detailSession?.rule_selections || {}).flat().reverse());

  return <main className="space-y-6">
    <header>
      <div className="text-xs font-bold uppercase tracking-[0.18em] text-emerald-300">Component 2 · Collector and Rules Engine</div>
      <h1 className="mt-2 text-2xl font-bold text-zinc-100">Collector &amp; Rules Engine</h1>
      <p className="mt-2 max-w-3xl text-sm text-zinc-400">Independent rules evaluate verified completed rounds. A shared coordinator writes at most one deduplicated SELECT for each target. This component observes and records decisions only.</p>
    </header>

    {statusError && <div role="status" className="rounded-lg border border-amber-500/30 bg-amber-500/10 px-4 py-3 text-sm text-amber-200">{statusError}</div>}
    {error && <div role="status" className="rounded-lg border border-amber-500/30 bg-amber-500/10 px-4 py-3 text-sm text-amber-200">{error}</div>}

    <section className="rounded-xl border border-zinc-800 bg-zinc-900/60 p-5">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div><h2 className="text-lg font-semibold">Collector and decision status</h2>
          <p className="mt-1 text-sm text-zinc-400">No balance targets or platform control settings are owned by a Rules Engine session.</p>
        </div>
        <div className="rounded border border-amber-500/30 bg-amber-500/10 px-3 py-2 text-xs text-amber-100">Execution disabled · selections are not bets</div>
      </div>
      <div className="mt-4 grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
        <Metric label="Collector" value={String(collectorState).replaceAll('_', ' ')} />
        <Metric label="Latest verified round" value={latestLabel} detail={current?.round_id || 'No persisted round'} icon={Database} />
        <Metric label="Total history" value={status?.total_history ?? '—'} icon={Database} />
        <Metric label="SHADOW observation readiness" value={shadowReadiness?.status || 'UNKNOWN'}
          detail={shadowReadiness?.blockers?.join(', ') || 'Requires current collector data and a committed PostgreSQL boundary'} icon={Activity} />
        <Metric label="Rules configured" value={status?.rules_configured ?? 0} icon={ListChecks} />
        <Metric label="Rules enabled" value={status?.rules_enabled ?? 0} icon={ListChecks} />
        <Metric label="Shared decision" value={dashboard?.shared_decision?.action || 'NOT_EVALUATED'}
          detail={dashboard?.shared_decision?.true_rule_ids?.join(', ') || dashboard?.shared_decision?.reason || 'No committed rule evaluation yet'} icon={ShieldCheck} />
        <Metric label="Target round" value={targetRound}
          detail={latestIntent?.target_round_id || latestEvaluation?.target_round?.roundId || ''} />
        <Metric label="Decision latency" value={decisionLatencyMs == null || !Number.isFinite(decisionLatencyMs)
          || decisionLatencyMs < 0 ? 'UNKNOWN' : `${decisionLatencyMs} ms`}
          detail="Completed-round observation to Rules Engine evaluation." />
        <Metric label="Settlement integrity" value={settlementIntegrity.status || 'CLEAR'}
          detail={settlementIntegrity.reason || 'Unknown transactions remain paused until reconciliation.'} />
        <Metric label="Rules session" value={status?.active_session_status || (activeSession?.status ?? 'NONE')}
          detail={`${status?.rules_definition_state || 'UNKNOWN'} · ${status?.rules_enabled_state || 'UNKNOWN'} · latest ${status?.latest_session_status || 'NONE'}`} />
      </div>
      <div className="mt-4 rounded-lg border border-zinc-800 bg-zinc-950/50 p-4">
        <h3 className="text-sm font-semibold">Latest independent rule results</h3>
        <div className="mt-3 space-y-2 text-xs">
          {(dashboard?.rule_evaluations || []).map((row) => <div key={row.rule_id} className="flex flex-wrap justify-between gap-2 border-b border-zinc-800 pb-2">
            <code>{row.rule_id}</code><span className={row.eligible === true ? 'text-emerald-300' : row.eligible === false ? 'text-zinc-300' : 'text-amber-200'}>{row.eligible === true ? 'TRUE' : row.eligible === false ? 'FALSE' : row.status || 'UNAVAILABLE'}</span>
          </div>)}
          {!dashboard?.rule_evaluations?.length && <p className="text-zinc-500">No committed evaluation is available.</p>}
        </div>
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
      <Metric label="Execution" value="DISABLED" icon={ShieldCheck} />
      <Metric label="Active session state" value={status?.active_session_status || 'NONE'}
        detail={`Latest session: ${status?.latest_session_status || 'NONE'} · definitions are independent of session state.`} />
    </section>

    <section className="rounded-xl border border-zinc-800 bg-zinc-900/60 p-5">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <h2 className="text-lg font-semibold">Rules Manager</h2>
          <p className="mt-1 text-sm text-zinc-400">Choose a source mode, enable only the participating policies, then start a versioned SHADOW session.</p>
        </div>
        <label className="text-sm text-zinc-300">Session mode
          <select value={mode} onChange={(event) => { setMode(event.target.value); setSelectedIds([]); }}
            className="ml-2 rounded border border-zinc-700 bg-zinc-950 px-3 py-2">
            {(registry?.modes || []).map((item) => <option key={item.id} value={item.id}>{MODE_LABELS[item.id] || item.id}</option>)}
          </select>
        </label>
        <label className="text-sm text-zinc-300">Saved preset
          <select value={presetChoice} onChange={(event) => applyPreset(event.target.value)} disabled={!presets.length}
            className="ml-2 rounded border border-zinc-700 bg-zinc-950 px-3 py-2">
            <option value="">Choose preset</option>
            {presets.map((item) => <option key={item.name} value={item.name}>{item.name} · {MODE_LABELS[item.mode] || item.mode}</option>)}
          </select>
        </label>
      </div>

      <div className="mt-4 max-w-xs">
        <label className="text-sm text-zinc-400">Maximum verified rounds (default 50)<input type="number" min="1" max="50" value={maxObservedRounds}
          onChange={(e) => setMaxObservedRounds(e.target.value)} className="mt-1 block w-full rounded border border-zinc-700 bg-zinc-950 px-3 py-2 text-zinc-100" /></label>
      </div>

      <div className="mt-4 space-y-3">
        {availableRules.map((rule) => <div key={rule.rule_id}
          className="flex flex-wrap items-start gap-3 rounded-lg border border-zinc-800 bg-zinc-950/60 p-4">
          <div className="flex min-w-44 flex-col gap-2 text-xs text-zinc-400">
            <label className="flex items-center gap-2"><input type="checkbox" checked={Boolean(rule.enabled)} disabled={busy}
              onChange={(event) => toggleRule(rule, event.target.checked)} className="accent-emerald-400" />Enabled</label>
            <label className="flex items-center gap-2"><input type="checkbox" checked={selectedIds.includes(rule.rule_id)} disabled={!rule.enabled || busy}
              onChange={(event) => setSelectedIds((previous) => event.target.checked
                ? [...new Set([...previous, rule.rule_id])] : previous.filter((id) => id !== rule.rule_id))}
              className="accent-sky-400" />Participate in this session</label>
          </div>
          <span className="min-w-0 flex-1">
            <span className="flex flex-wrap items-center gap-2 font-semibold text-zinc-100">
              {rule.name}<code className="text-xs text-emerald-300">{rule.rule_id}</code>
              <span className="text-xs text-zinc-500">v{rule.version}</span>
            </span>
            <details className="mt-2 text-xs text-zinc-400">
              <summary className="cursor-pointer">Condition and state behavior</summary>
              <pre className="mt-2 whitespace-pre-wrap text-zinc-500">{JSON.stringify(rule.condition, null, 2)}</pre>
              <p className="mt-2 text-zinc-400">{rule.transition_rules?.join(' ')}</p>
            </details>
          </span>
        </div>)}
        {!availableRules.length && <p className="text-sm text-zinc-500">No rules are registered for this mode.</p>}
      </div>

      {mode === 'START_CHECK' || mode === 'LEGACY_SEPARATE_TRACKER' ? <div className="mt-4 grid gap-3 sm:grid-cols-2">
        <label className="flex items-center gap-2 text-sm text-zinc-300"><input type="checkbox" checked={autoNext} onChange={(e) => setAutoNext(e.target.checked)} /> Start the next check after the full 50-round window</label>
      </div> : null}

      {mode === 'ALL_RULES_TOGETHER' ? <div className="mt-4 grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
        <label className="flex items-center gap-2 text-sm text-zinc-300"><input type="checkbox" checked={winEnabled} onChange={(e) => setWinEnabled(e.target.checked)} /> Stop selections after total WIN</label>
        <label className="text-sm text-zinc-400">WIN count<input type="number" min="1" value={winCount} disabled={!winEnabled} onChange={(e) => setWinCount(e.target.value)} className="mt-1 block w-full rounded border border-zinc-700 bg-zinc-950 px-3 py-2 text-zinc-100" /></label>
        <label className="flex items-center gap-2 text-sm text-zinc-300"><input type="checkbox" checked={lossEnabled} onChange={(e) => setLossEnabled(e.target.checked)} /> Stop observing after this many LOSS outcomes</label>
        <label className="text-sm text-zinc-400">Rule LOSS outcome count<input type="number" min="1" value={lossCount} disabled={!lossEnabled} onChange={(e) => setLossCount(e.target.value)} className="mt-1 block w-full rounded border border-zinc-700 bg-zinc-950 px-3 py-2 text-zinc-100" /></label>
        <label className="text-sm text-zinc-400">Maximum combined selections (optional)<input type="number" min="1" value={maxSelections} onChange={(e) => setMaxSelections(e.target.value)} className="mt-1 block w-full rounded border border-zinc-700 bg-zinc-950 px-3 py-2 text-zinc-100" /></label>
        <label className="flex items-center gap-2 text-sm text-zinc-300 sm:col-span-2 xl:col-span-3"><input type="checkbox" checked={autoNext} onChange={(e) => setAutoNext(e.target.checked)} /> Start the next check after the full 50-round window</label>
      </div> : null}

      <div className="mt-5 flex flex-wrap gap-2">
        <Button tone={modeConflict ? 'danger' : 'good'} onClick={start} disabled={busy
          || shadowReadiness?.ready === false
          || !hasEnabledSelection}>
          {busyAction === 'start-shadow' ? 'Starting observation session…'
            : modeConflict ? 'Stop current session & start new' : 'Start observation session'}
        </Button>
        <input aria-label="Preset name" value={presetName} onChange={(e) => setPresetName(e.target.value)} placeholder="Preset name" className="min-w-36 rounded border border-zinc-700 bg-zinc-950 px-3 py-2 text-sm" />
        <Button onClick={savePreset} disabled={busy || !presetName.trim()}>Save preset</Button>
        <span className="self-center text-xs text-zinc-500">Rules Engine observations do not use stake or balance strategy settings.</span>
      </div>
      {modeConflict && <p className="mt-3 text-sm text-amber-200">
        This will stop session {modeConflict.session_id} for this mode and start a fresh observation. Its history will be retained.
      </p>}
      {readinessBlockers.length > 0 && <p role="status" className="mt-3 text-sm text-amber-200">
        Observation is blocked: {readinessBlockers.join(', ')}.
      </p>}
      {!hasEnabledSelection && registry && <p className="mt-2 text-sm text-zinc-400">Enable and select at least one rule for this mode before starting.</p>}
      {Boolean(activeSession) && <p className="mt-2 text-sm text-zinc-400">An observation session is already active.</p>}
    </section>

    <section className="rounded-xl border border-zinc-800 bg-zinc-900/60 p-5">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div><h2 className="text-lg font-semibold">Current session</h2>
          <p className="mt-1 text-sm text-zinc-400">{activeSession ? `${MODE_LABELS[activeSession.mode] || activeSession.mode} · ${activeSession.session_id}` : 'No active session'}</p>
        </div>
        {activeSession && <div className="flex flex-wrap gap-2">
          {activeSession.status === 'RUNNING' && <Button onClick={() => sessionAction(activeSession.session_id, 'pause')} disabled={busy}>{busyAction === 'pause' ? 'Pausing…' : 'Pause'}</Button>}
          {activeSession.status === 'PAUSED' && ['MANUAL_PAUSE', 'UNKNOWN_TRANSACTION_REQUIRES_RECONCILIATION'].includes(activeSession.stop_reason)
            && <Button tone="good" onClick={() => sessionAction(activeSession.session_id, 'resume')} disabled={busy}>{busyAction === 'resume' ? 'Resuming…' : 'Resume'}</Button>}
          <Button tone="danger" onClick={() => sessionAction(activeSession.session_id, 'stop')} disabled={busy}>{busyAction === 'stop' ? 'Stopping…' : 'Stop'}</Button>
          <Button tone="danger" onClick={() => sessionAction(activeSession.session_id, 'emergency_stop')} disabled={busy}>{busyAction === 'emergency_stop' ? 'Stopping…' : 'Emergency stop'}</Button>
          <Button onClick={() => sessionAction(activeSession.session_id, 'reset')} disabled={busy}>{busyAction === 'reset' ? 'Resetting…' : 'Reset'}</Button>
        </div>}
      </div>
      {activeSession && <div className="mt-4 grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
        <Metric label="Status" value={activeSession.status} detail={activeSession.selection_status} />
        <Metric label="Current round" value={current?.round_index ?? '—'} detail={`${activeSession.observed_rounds}/${activeSession.window_size} observed`} />
        <Metric label={activeSession.mode === 'FIVE_RULE_TRACKER' ? 'Independent tracker selections' : 'Combined selections'} value={activeSession.selection_count} detail={`Shadow target outcomes · WIN ${activeSession.results?.WIN ?? 0} · LOSS ${activeSession.results?.LOSS ?? 0} · PENDING ${activeSession.results?.PENDING ?? 0}`} />
        <Metric label="Stop reason" value={activeSession.stop_reason || 'NONE'} />
        {activeSession.integrity_stop_reason && <Metric label="Preserved integrity reason" value={activeSession.integrity_stop_reason} />}
      </div>}
      {detailSession && <>
        <details className="mt-5 rounded-lg border border-zinc-800 p-4">
          <summary className="cursor-pointer text-sm font-semibold text-zinc-200">Selections and resolved target outcomes</summary>
          <div className="mt-3 overflow-x-auto">
            <table className="w-full min-w-[720px] text-left text-xs">
              <thead className="text-zinc-500"><tr><th className="py-2">Target round</th><th>Triggered rules</th><th>Selection time</th><th>Actual multiplier</th><th>Shadow target result</th><th>Settlement evidence</th></tr></thead>
              <tbody>{allSelections.slice(0, 100).map((selection) => <tr key={selection.selection_id} className="border-t border-zinc-800 align-top">
                <td className="py-2">#{selection.expected_target?.roundIndex ?? '—'}<br /><span className="text-zinc-500">{selection.actual_target?.roundId || 'pending identity'}</span></td>
                <td>{selection.contributing_rule_ids?.join(', ') || selection.rule_id || '—'}</td>
                <td>{selection.selected_at || '—'}</td>
                <td>{selection.actual_multiplier == null ? 'PENDING' : `${selection.actual_multiplier}x`}</td>
                <td className={selection.status === 'WIN' ? 'text-emerald-300' : selection.status === 'LOSS' ? 'text-rose-300' : 'text-amber-200'}>{selection.status}</td>
                <td>{selection.financial_result?.status || 'NOT RECONCILED'}</td>
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
  </main>;
}
