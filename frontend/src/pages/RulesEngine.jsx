import { useCallback, useEffect, useMemo, useState } from 'react';
import { Activity, Database, ListChecks, ShieldCheck } from 'lucide-react';
import { api } from '../services/api.js';
import LiveActivation from './LiveActivation.jsx';

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
  const [busy, setBusy] = useState(false);
  const [busyAction, setBusyAction] = useState('');
  const [presetName, setPresetName] = useState('');
  const [presetChoice, setPresetChoice] = useState('');
  const [stake, setStake] = useState(100);
  const [executionPanel, setExecutionPanel] = useState(null);
  const [winEnabled, setWinEnabled] = useState(false);
  const [winCount, setWinCount] = useState(1);
  const [lossEnabled, setLossEnabled] = useState(false);
  const [lossCount, setLossCount] = useState(2);
  const [maxSelections, setMaxSelections] = useState('');
  const [maxObservedRounds, setMaxObservedRounds] = useState(50);
  const [autoNext, setAutoNext] = useState(false);
  const [profitEnabled, setProfitEnabled] = useState(true);
  const [profitGoal, setProfitGoal] = useState(400);
  const [lossLimitEnabled, setLossLimitEnabled] = useState(true);
  const [lossLimit, setLossLimit] = useState(400);
  const [reconciledProfitEnabled, setReconciledProfitEnabled] = useState(false);
  const [reconciledProfitTarget, setReconciledProfitTarget] = useState(400);
  const [reconciledLossEnabled, setReconciledLossEnabled] = useState(false);
  const [reconciledLossLimit, setReconciledLossLimit] = useState(400);
  const [reconciledWinEnabled, setReconciledWinEnabled] = useState(false);
  const [reconciledWinCount, setReconciledWinCount] = useState(1);
  const [reconciledLossCountEnabled, setReconciledLossCountEnabled] = useState(false);
  const [reconciledLossCount, setReconciledLossCount] = useState(2);

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
        setExecutionPanel(Number.isInteger(selectedSession.execution_panel)
          && [0, 1].includes(selectedSession.execution_panel) ? selectedSession.execution_panel : null);
        const detail = await api.get(`/api/rules-engine/sessions/${encodeURIComponent(selectedSession.session_id)}`);
        setDetailSession(detail.data);
      } else setDetailSession(null);
      setError('');
    } catch {
      setError('Rules Engine status is unavailable.');
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
  const financialStop = dashboard?.financial_stop || activeSession?.financial_stop || {};
  const liveModeActive = executionMode?.mode === 'LIVE_REAL';
  const current = status?.latest_real_round;
  const collector = status?.collector_status || {};
  const shadowReadiness = status?.shadow_readiness;
  const liveReadiness = dashboard?.execution_readiness;
  const aviatorReadiness = dashboard?.shared_aviator_readiness;
  const collectorState = collector.state || collector.health || collector.status || 'UNKNOWN';
  const latestLabel = current
    ? `${Number(current.multiplier).toFixed(2)}x${current.round_index != null ? ` · #${current.round_index}` : ''}`
    : 'NONE';

  const withBusy = async (operation, action = 'request') => {
    setBusy(true);
    setBusyAction(action);
    try {
      await operation();
      await refresh();
    } catch (failure) {
      const detail = failure?.response?.data?.detail;
      setError(typeof detail === 'string' ? detail
        : detail?.reason || detail?.message || failure?.response?.data?.error
        || 'The Rules Engine request failed.');
    } finally {
      setBusy(false);
      setBusyAction('');
    }
  };

  const toggleRule = (rule, enabled) => withBusy(async () => {
    const parameters = rule.rule_id === 'USER_PLAYING_POLICY_START_CHECK'
      || rule.rule_id === 'USER_PLAYING_POLICY_LEGACY_TRACKER'
      ? { profit_target_enabled: profitEnabled, profit_target_rwf: Number(profitGoal),
          loss_limit_enabled: lossLimitEnabled, loss_limit_rwf: Number(lossLimit),
          window_size: 50, auto_next_check_enabled: autoNext }
      : {};
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
    setStake(options.fixed_stake_rwf ?? 100);
    setExecutionPanel(Number.isInteger(options.execution_panel) && [0, 1].includes(options.execution_panel)
      ? options.execution_panel : null);
    setWinEnabled(Boolean(options.win_limit_enabled));
    setWinCount(options.win_limit_count ?? 1);
    setLossEnabled(Boolean(options.loss_limit_enabled));
    setLossCount(options.loss_limit_count ?? 2);
    setMaxSelections(options.max_selections ?? '');
    setMaxObservedRounds(options.max_observed_rounds ?? 50);
    setAutoNext(Boolean(options.auto_next_check_enabled));
    setProfitEnabled(options.profit_target_enabled ?? true);
    setProfitGoal(options.profit_target_rwf ?? 400);
    setLossLimitEnabled(options.loss_limit_enabled ?? true);
    setLossLimit(options.loss_limit_rwf ?? 400);
    setReconciledProfitEnabled(Boolean(options.reconciled_profit_target_enabled));
    setReconciledProfitTarget(options.reconciled_profit_target_rwf ?? 400);
    setReconciledLossEnabled(Boolean(options.reconciled_loss_limit_enabled));
    setReconciledLossLimit(options.reconciled_loss_limit_rwf ?? 400);
    setReconciledWinEnabled(Boolean(options.reconciled_win_limit_enabled));
    setReconciledWinCount(options.reconciled_win_limit_count ?? 1);
    setReconciledLossCountEnabled(Boolean(options.reconciled_loss_count_limit_enabled));
    setReconciledLossCount(options.reconciled_loss_count_limit ?? 2);
    const disabled = (preset.rule_ids || []).filter((id) => !registry?.rules?.find((rule) => rule.rule_id === id)?.enabled);
    setError(disabled.length ? `Preset loaded. Enable its disabled rules before starting: ${disabled.join(', ')}` : '');
  };

  const start = () => withBusy(async () => {
    const participatingRuleIds = selectedIds.filter((id) => availableRules.find((rule) => rule.rule_id === id)?.enabled);
    const optionalPanel = [0, 1].includes(executionPanel) ? { execution_panel: executionPanel } : {};
    const reconciledOptions = {
      reconciled_profit_target_enabled: reconciledProfitEnabled,
      reconciled_profit_target_rwf: Number(reconciledProfitTarget),
      reconciled_loss_limit_enabled: reconciledLossEnabled,
      reconciled_loss_limit_rwf: Number(reconciledLossLimit),
      reconciled_win_limit_enabled: reconciledWinEnabled,
      reconciled_win_limit_count: reconciledWinEnabled ? Number(reconciledWinCount) : null,
      reconciled_loss_count_limit_enabled: reconciledLossCountEnabled,
      reconciled_loss_count_limit: reconciledLossCountEnabled ? Number(reconciledLossCount) : null,
    };
    const options = mode === 'ALL_RULES_TOGETHER' ? {
      window_size: 50, max_observed_rounds: Number(maxObservedRounds), fixed_stake_rwf: Number(stake),
      win_limit_enabled: winEnabled, win_limit_count: winEnabled ? Number(winCount) : null,
      loss_limit_enabled: lossEnabled, loss_limit_count: lossEnabled ? Number(lossCount) : null,
      max_selections: maxSelections === '' ? null : Number(maxSelections),
      auto_next_check_enabled: autoNext,
    } : mode === 'START_CHECK' || mode === 'LEGACY_SEPARATE_TRACKER' ? {
      window_size: 50, max_observed_rounds: Number(maxObservedRounds), stake_rwf: 100, cashout_multiplier: 2.0,
      profit_target_enabled: profitEnabled, profit_target_rwf: Number(profitGoal),
      loss_limit_enabled: lossLimitEnabled, loss_limit_rwf: Number(lossLimit),
      auto_next_check_enabled: autoNext,
    } : { window_size: 50, max_observed_rounds: Number(maxObservedRounds) };
    const response = await api.post('/api/rules-engine/sessions', { mode, rule_ids: participatingRuleIds,
      options: { ...options, ...reconciledOptions, ...optionalPanel } });
    if (response.data?.status === 'RUNNING' && response.data?.session_id) {
      setStatus((previous) => previous ? { ...previous, status: 'RUNNING',
        active_session: response.data } : previous);
    }
  }, 'start-shadow');

  const sessionAction = (sessionId, action) => withBusy(() =>
    api.post(`/api/rules-engine/sessions/${encodeURIComponent(sessionId)}/${action}`), action);

  const disarmLive = () => withBusy(() => api.post('/api/live/stop'));
  const emergencyStop = () => withBusy(() => api.post('/api/live/emergency-stop'));
  const readBalance = () => withBusy(() => api.get('/api/live/balance'));
  const refreshCashout = () => withBusy(() => api.post('/api/live/cashout/refresh'), 'cashout-refresh');

  const savePreset = () => withBusy(async () => {
    const options = { window_size: 50, max_observed_rounds: Number(maxObservedRounds),
      auto_next_check_enabled: autoNext,
      ...([0, 1].includes(executionPanel) ? { execution_panel: executionPanel } : {}) };
    Object.assign(options, {
      reconciled_profit_target_enabled: reconciledProfitEnabled,
      reconciled_profit_target_rwf: Number(reconciledProfitTarget),
      reconciled_loss_limit_enabled: reconciledLossEnabled,
      reconciled_loss_limit_rwf: Number(reconciledLossLimit),
      reconciled_win_limit_enabled: reconciledWinEnabled,
      reconciled_win_limit_count: reconciledWinEnabled ? Number(reconciledWinCount) : null,
      reconciled_loss_count_limit_enabled: reconciledLossCountEnabled,
      reconciled_loss_count_limit: reconciledLossCountEnabled ? Number(reconciledLossCount) : null,
    });
    if (mode === 'ALL_RULES_TOGETHER') Object.assign(options, {
      fixed_stake_rwf: Number(stake), win_limit_enabled: winEnabled,
      win_limit_count: winEnabled ? Number(winCount) : null,
      loss_limit_enabled: lossEnabled, loss_limit_count: lossEnabled ? Number(lossCount) : null,
      max_selections: maxSelections === '' ? null : Number(maxSelections),
    });
    if (mode === 'START_CHECK' || mode === 'LEGACY_SEPARATE_TRACKER') Object.assign(options, {
      profit_target_enabled: profitEnabled, profit_target_rwf: Number(profitGoal),
      loss_limit_enabled: lossLimitEnabled, loss_limit_rwf: Number(lossLimit),
    });
    await api.post('/api/rules-engine/presets', { name: presetName, mode, rule_ids: selectedIds,
      options });
    setPresetName('');
  });

  const recentEvaluations = detailSession?.evaluations?.slice(-12).reverse() || [];
  const allSelections = detailSession?.mode === 'ALL_RULES_TOGETHER'
    ? [...(detailSession.selections || [])].reverse()
    : Object.values(detailSession?.rule_selections || {}).flat().reverse();

  return <main className="space-y-6">
    <header className="flex flex-wrap items-end justify-between gap-4">
      <div>
        <div className="text-xs font-bold uppercase tracking-[0.18em] text-emerald-300">Shadow decision interface</div>
        <h1 className="mt-2 text-2xl font-bold text-zinc-100">Rules Engine</h1>
        <p className="mt-2 max-w-3xl text-sm text-zinc-400">
          Independent rule evaluations use verified, completed history. The coordinator records selections only; automatic execution is off.
        </p>
      </div>
      <div className="rounded-lg border border-amber-500/30 bg-amber-500/10 px-3 py-2 text-sm text-amber-200">
        <div className="font-semibold">REAL-MONEY EXECUTION: DISABLED</div>
        <p className="mt-1 text-xs">Requires separate operator authorization and verified platform controls. Rule selections are not bets.</p>
        <div className="mt-3 flex flex-wrap gap-2">
          <button type="button" onClick={disarmLive} disabled={busy}
            className="rounded border border-amber-400/50 px-3 py-1 text-xs font-semibold hover:bg-amber-500/20 disabled:opacity-50">OFF / Disarm</button>
          <button type="button" onClick={emergencyStop} disabled={busy}
            className="rounded border border-rose-500/50 px-3 py-1 text-xs font-semibold text-rose-100 hover:bg-rose-500/20 disabled:opacity-50">Emergency Stop</button>
          {liveModeActive && <span className="self-center text-xs">Backend LIVE mode reports active; execution remains disabled here.</span>}
        </div>
      </div>

      <label className="min-w-52 text-sm text-zinc-300">Optional UI setup panel
        <select aria-label="Optional UI setup panel" value={executionPanel ?? ''}
          onChange={(event) => setExecutionPanel(event.target.value === '' ? null : Number(event.target.value))}
          disabled={busy || Boolean(activeSession)}
          className="mt-1 block w-full rounded border border-zinc-700 bg-zinc-950 px-3 py-2 text-zinc-100">
          <option value="">Choose one panel</option>
          <option value={0}>Panel 1</option>
          <option value={1}>Panel 2</option>
        </select>
        <span className="mt-1 block text-xs text-zinc-500">Optional for SHADOW. This only selects a panel for later stake synchronization.</span>
      </label>
    </header>

    <section aria-label="LIVE REAL session controls">
      <LiveActivation />
    </section>

    {error && <div role="status" className="rounded-lg border border-amber-500/30 bg-amber-500/10 px-4 py-3 text-sm text-amber-200">{error}</div>}

    <section className="rounded-xl border border-zinc-800 bg-zinc-900/60 p-5">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div><h2 className="text-lg font-semibold">Rules Betting dashboard</h2>
          <p className="mt-1 text-sm text-zinc-400">Each enabled rule evaluates independently. Overlapping TRUE results produce one shared selection per target. Platform financial results appear only after reconciliation.</p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <div className="rounded border border-amber-500/30 bg-amber-500/10 px-3 py-2 text-xs text-amber-100">
            Cashout target 2.00x · platform acknowledgement {dashboard?.cashout_configuration?.platform_acknowledged === true ? 'confirmed' : 'unverified'}
          </div>
          <Button onClick={readBalance} disabled={busy}>Read balance</Button>
          <Button onClick={refreshCashout} disabled={busy || liveModeActive}>
            {busyAction === 'cashout-refresh' ? 'Verifying panels…' : 'Set and verify both panels · 2.00x'}
          </Button>
        </div>
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
        <Metric label="LIVE_REAL execution readiness" value={liveReadiness?.status || 'UNKNOWN'}
          detail={liveReadiness?.blockers?.join(', ') || 'Real-money execution remains disabled'} icon={ShieldCheck} />
        <Metric label="Shared browser" value={aviatorReadiness?.browser_connected || 'UNKNOWN'} />
        <Metric label="Aviator page" value={aviatorReadiness?.aviator_page_ready || 'UNKNOWN'} />
        <Metric label="Game iframe" value={aviatorReadiness?.game_iframe_ready || 'UNKNOWN'} />
        <Metric label="History observer" value={aviatorReadiness?.history_observer_ready || 'UNKNOWN'} />
        <Metric label="Betting controls" value={aviatorReadiness?.betting_controls_ready || 'UNKNOWN'}
          detail={aviatorReadiness?.betting_ui_configuration?.error_reason || ''} />
        <Metric label="Cashout configuration" value={aviatorReadiness?.cashout_configured || 'UNKNOWN'}
          detail={`Readback age ${aviatorReadiness?.cashout_initialization?.age_seconds ?? 'unknown'}s · platform acknowledgement ${dashboard?.cashout_configuration?.platform_acknowledged === true ? 'confirmed' : 'unverified'}`} />
        <Metric label="Panel 1 cashout" value={dashboard?.cashout_configuration?.panels?.[0]?.status || 'UNKNOWN'}
          detail={`Auto Bet ${dashboard?.cashout_configuration?.panels?.[0]?.auto_bet || 'unknown'} · Auto Cashout ${dashboard?.cashout_configuration?.panels?.[0]?.auto_cashout || 'unknown'} · ${dashboard?.cashout_configuration?.panels?.[0]?.multiplier || '—'}x`} />
        <Metric label="Panel 2 cashout" value={dashboard?.cashout_configuration?.panels?.[1]?.status || 'UNKNOWN'}
          detail={`Auto Bet ${dashboard?.cashout_configuration?.panels?.[1]?.auto_bet || 'unknown'} · Auto Cashout ${dashboard?.cashout_configuration?.panels?.[1]?.auto_cashout || 'unknown'} · ${dashboard?.cashout_configuration?.panels?.[1]?.multiplier || '—'}x`} />
        <Metric label="Starting balance" value={dashboard?.financials?.starting_balance ?? 'UNAVAILABLE'}
          detail={dashboard?.financials?.currency || dashboard?.balance?.currency || 'Currency unverified'} />
        <Metric label="Current balance" value={dashboard?.balance?.verified ? dashboard.balance.balance : 'UNVERIFIED'}
          detail={`${dashboard?.balance?.currency || 'Currency unverified'} · observed ${dashboard?.balance?.observed_at || 'never'}`} />
        <Metric label="Balance change" value={dashboard?.financials?.balance_change ?? 'UNAVAILABLE'}
          detail={`Current minus starting balance · other movement ${dashboard?.financials?.non_betting_balance_movement ?? 'unclassified'}`} />
        <Metric label="Realized betting P/L" value={dashboard?.realized_betting_pl ?? 'UNAVAILABLE'}
          detail={`${dashboard?.financials?.currency || 'Currency unverified'} · receipt-backed only`} />
        <Metric label="Confirmed WIN count" value={dashboard?.financials?.win_count ?? 0}
          detail={`${dashboard?.financials?.reconciled_bets ?? 0} reconciled shared bets`} />
        <Metric label="Confirmed LOSS count" value={dashboard?.financials?.loss_count ?? 0} />
        <Metric label="Financial stop status" value={financialStop.status || 'ACTIVE'}
          detail={financialStop.reason || 'Limits evaluate reconciled transactions only'} />
        <Metric label="Profit target" value={dashboard?.session_controls?.profit_target ?? 'Not set'}
          detail={dashboard?.session_controls?.profit_target_enabled ? 'Enabled' : 'Disabled'} />
        <Metric label="Loss limit" value={dashboard?.session_controls?.loss_limit ?? 'Not set'}
          detail={dashboard?.session_controls?.loss_limit_enabled ? 'Enabled' : 'Disabled'} />
        <Metric label="Financial WIN stop" value={dashboard?.session_controls?.reconciled_win_limit_count ?? 'Disabled'}
          detail="Confirmed reconciled wins" />
        <Metric label="Financial LOSS stop" value={dashboard?.session_controls?.reconciled_loss_count_limit ?? 'Disabled'}
          detail="Confirmed reconciled losses" />
      </div>
      <p className="mt-3 text-xs text-zinc-500">
        Balance provider: {dashboard?.balance?.provider_identity || 'unverified'} · session: {dashboard?.balance?.application_session_id || dashboard?.balance?.session_identity || 'unassigned'}
      </p>
      <div className="mt-4 grid gap-4 lg:grid-cols-2">
        <article className="rounded-lg border border-zinc-800 bg-zinc-950/50 p-4">
          <h3 className="text-sm font-semibold">Latest independent rule results</h3>
          <div className="mt-3 space-y-2 text-xs">
            {(dashboard?.rule_evaluations || []).map((row) => <div key={row.rule_id} className="flex flex-wrap justify-between gap-2 border-b border-zinc-800 pb-2">
              <code>{row.rule_id}</code><span className={row.eligible === true ? 'text-emerald-300' : row.eligible === false ? 'text-zinc-300' : 'text-amber-200'}>{row.eligible === true ? 'TRUE' : row.eligible === false ? 'FALSE' : row.status || 'UNAVAILABLE'}</span>
            </div>)}
            {!dashboard?.rule_evaluations?.length && <p className="text-zinc-500">No committed evaluation is available.</p>}
          </div>
        </article>
        <article className="rounded-lg border border-amber-500/20 bg-amber-500/5 p-4">
          <h3 className="text-sm font-semibold">Current execution blockers</h3>
          <ul className="mt-3 list-disc space-y-1 pl-5 text-xs text-amber-100/90">
            {(dashboard?.execution_readiness?.blockers || ['Backend readiness unavailable']).map((reason) => <li key={reason}>{reason}</li>)}
          </ul>
        </article>
      </div>
      {dashboard?.execution_intents?.length > 0 && <details className="mt-4 rounded-lg border border-zinc-800 p-4">
        <summary className="cursor-pointer text-sm font-semibold">Durable execution intents (blocked)</summary>
        <div className="mt-3 space-y-2 text-xs">{dashboard.execution_intents.map((intent) => <div key={intent.intent_id} className="rounded bg-zinc-950 p-3">
          <div>{intent.execution_status} · target round #{intent.target_round_index} · stake {intent.requested_stake ?? '—'} · cashout {intent.cashout_target}x</div>
          <div className="mt-1 text-zinc-500">Rules: {intent.selected_rule_ids?.join(', ') || '—'} · blockers: {intent.blockers?.join(', ')}</div>
        </div>)}</div>
      </details>}
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
        <label className="flex items-center gap-2 text-sm text-zinc-300"><input type="checkbox" checked={profitEnabled} onChange={(e) => setProfitEnabled(e.target.checked)} /> NET profit stop</label>
        <label className="flex items-center gap-2 text-sm text-zinc-300"><input type="checkbox" checked={lossLimitEnabled} onChange={(e) => setLossLimitEnabled(e.target.checked)} /> NET loss stop</label>
        <label className="text-sm text-zinc-400">Profit stop RWF<input type="number" min="1" value={profitGoal} onChange={(e) => setProfitGoal(e.target.value)} disabled={!profitEnabled} className="mt-1 block w-full rounded border border-zinc-700 bg-zinc-950 px-3 py-2 text-zinc-100" /></label>
        <label className="text-sm text-zinc-400">Loss stop RWF<input type="number" min="1" value={lossLimit} onChange={(e) => setLossLimit(e.target.value)} disabled={!lossLimitEnabled} className="mt-1 block w-full rounded border border-zinc-700 bg-zinc-950 px-3 py-2 text-zinc-100" /></label>
        <label className="flex items-center gap-2 text-sm text-zinc-300 sm:col-span-2"><input type="checkbox" checked={autoNext} onChange={(e) => setAutoNext(e.target.checked)} /> Start the next check after the full 50-round window</label>
      </div> : null}

      {mode === 'ALL_RULES_TOGETHER' ? <div className="mt-4 grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
        <label className="text-sm text-zinc-400">Fixed simulated stake (RWF)<input type="number" min="1" value={stake} onChange={(e) => setStake(e.target.value)} className="mt-1 block w-full rounded border border-zinc-700 bg-zinc-950 px-3 py-2 text-zinc-100" /></label>
        <label className="flex items-center gap-2 text-sm text-zinc-300"><input type="checkbox" checked={winEnabled} onChange={(e) => setWinEnabled(e.target.checked)} /> Stop selections after total WIN</label>
        <label className="text-sm text-zinc-400">WIN count<input type="number" min="1" value={winCount} disabled={!winEnabled} onChange={(e) => setWinCount(e.target.value)} className="mt-1 block w-full rounded border border-zinc-700 bg-zinc-950 px-3 py-2 text-zinc-100" /></label>
        <label className="flex items-center gap-2 text-sm text-zinc-300"><input type="checkbox" checked={lossEnabled} onChange={(e) => setLossEnabled(e.target.checked)} /> Stop selections after total LOSS</label>
        <label className="text-sm text-zinc-400">LOSS count<input type="number" min="1" value={lossCount} disabled={!lossEnabled} onChange={(e) => setLossCount(e.target.value)} className="mt-1 block w-full rounded border border-zinc-700 bg-zinc-950 px-3 py-2 text-zinc-100" /></label>
        <label className="text-sm text-zinc-400">Maximum combined selections (optional)<input type="number" min="1" value={maxSelections} onChange={(e) => setMaxSelections(e.target.value)} className="mt-1 block w-full rounded border border-zinc-700 bg-zinc-950 px-3 py-2 text-zinc-100" /></label>
        <label className="flex items-center gap-2 text-sm text-zinc-300 sm:col-span-2 xl:col-span-3"><input type="checkbox" checked={autoNext} onChange={(e) => setAutoNext(e.target.checked)} /> Start the next check after the full 50-round window</label>
      </div> : null}

      <div className="mt-4 rounded-lg border border-emerald-500/20 bg-emerald-500/5 p-4">
        <h3 className="text-sm font-semibold">Receipt-backed session stops</h3>
        <p className="mt-1 text-xs text-zinc-400">Only accepted, settled bets with a verified matching balance count. Pending and unknown transactions do not change these totals.</p>
        <div className="mt-3 grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
          <label className="flex items-center gap-2 text-sm text-zinc-300"><input type="checkbox" checked={reconciledProfitEnabled} onChange={(e) => setReconciledProfitEnabled(e.target.checked)} /> Stop at profit target</label>
          <label className="text-sm text-zinc-400">Profit target (RWF)<input type="number" min="1" value={reconciledProfitTarget} disabled={!reconciledProfitEnabled} onChange={(e) => setReconciledProfitTarget(e.target.value)} className="mt-1 block w-full rounded border border-zinc-700 bg-zinc-950 px-3 py-2 text-zinc-100" /></label>
          <label className="flex items-center gap-2 text-sm text-zinc-300"><input type="checkbox" checked={reconciledLossEnabled} onChange={(e) => setReconciledLossEnabled(e.target.checked)} /> Stop at loss limit</label>
          <label className="text-sm text-zinc-400">Loss limit (RWF)<input type="number" min="1" value={reconciledLossLimit} disabled={!reconciledLossEnabled} onChange={(e) => setReconciledLossLimit(e.target.value)} className="mt-1 block w-full rounded border border-zinc-700 bg-zinc-950 px-3 py-2 text-zinc-100" /></label>
          <label className="flex items-center gap-2 text-sm text-zinc-300"><input type="checkbox" checked={reconciledWinEnabled} onChange={(e) => setReconciledWinEnabled(e.target.checked)} /> Stop after confirmed WIN count</label>
          <label className="text-sm text-zinc-400">Confirmed WIN count<input type="number" min="1" value={reconciledWinCount} disabled={!reconciledWinEnabled} onChange={(e) => setReconciledWinCount(e.target.value)} className="mt-1 block w-full rounded border border-zinc-700 bg-zinc-950 px-3 py-2 text-zinc-100" /></label>
          <label className="flex items-center gap-2 text-sm text-zinc-300"><input type="checkbox" checked={reconciledLossCountEnabled} onChange={(e) => setReconciledLossCountEnabled(e.target.checked)} /> Stop after confirmed LOSS count</label>
          <label className="text-sm text-zinc-400">Confirmed LOSS count<input type="number" min="1" value={reconciledLossCount} disabled={!reconciledLossCountEnabled} onChange={(e) => setReconciledLossCount(e.target.value)} className="mt-1 block w-full rounded border border-zinc-700 bg-zinc-950 px-3 py-2 text-zinc-100" /></label>
        </div>
      </div>

      <div className="mt-5 flex flex-wrap gap-2">
        <Button tone="good" onClick={start} disabled={busy || Boolean(activeSession)
          || shadowReadiness?.ready === false
          || !selectedIds.some((id) => availableRules.find((rule) => rule.rule_id === id)?.enabled)}>
          {busyAction === 'start-shadow' ? 'Starting SHADOW session…' : 'Start SHADOW session'}
        </Button>
        <input aria-label="Preset name" value={presetName} onChange={(e) => setPresetName(e.target.value)} placeholder="Preset name" className="min-w-36 rounded border border-zinc-700 bg-zinc-950 px-3 py-2 text-sm" />
        <Button onClick={savePreset} disabled={busy || !presetName.trim()}>Save preset</Button>
        <span className="self-center text-xs text-zinc-500">Shadow selection outcomes do not update receipt-backed P/L or financial stop counts.</span>
      </div>
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
              <thead className="text-zinc-500"><tr><th className="py-2">Target round</th><th>Triggered rules</th><th>Selection time</th><th>Actual multiplier</th><th>Shadow target result</th><th>Financial result</th></tr></thead>
              <tbody>{allSelections.slice(0, 100).map((selection) => <tr key={selection.selection_id} className="border-t border-zinc-800 align-top">
                <td className="py-2">#{selection.expected_target?.roundIndex ?? '—'}<br /><span className="text-zinc-500">{selection.actual_target?.roundId || 'pending identity'}</span></td>
                <td>{selection.contributing_rule_ids?.join(', ') || selection.rule_id || '—'}</td>
                <td>{selection.selected_at || '—'}</td>
                <td>{selection.actual_multiplier == null ? 'PENDING' : `${selection.actual_multiplier}x`}</td>
                <td className={selection.status === 'WIN' ? 'text-emerald-300' : selection.status === 'LOSS' ? 'text-rose-300' : 'text-amber-200'}>{selection.status}</td>
                <td>{selection.financial_result?.status === 'RECONCILED'
                  ? `${selection.financial_result.result} · ${selection.financial_result.profit_loss} ${selection.financial_result.currency || ''}`
                  : 'NOT RECONCILED'}</td>
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
