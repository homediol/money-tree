import { useCallback, useEffect, useMemo, useState } from 'react';
import { Activity, BrainCircuit, RefreshCw, ShieldAlert, Sparkles } from 'lucide-react';
import { api } from '../services/api.js';

const fmt = (value, digits = 1) => value != null && value !== '' && Number.isFinite(Number(value)) ? Number(value).toFixed(digits) : '—';
const pct = (value) => value != null && value !== '' && Number.isFinite(Number(value)) ? `${(Number(value) * 100).toFixed(1)}%` : '—';
const tone = (value) => value === 'HEALTHY' ? 'text-emerald-300' : value === 'DEGRADED' ? 'text-amber-200' : 'text-zinc-400';
const contextName = (value) => ({
  LOW_DOMINANCE: 'LOW results are more common', HIGH_DOMINANCE: 'HIGH results are more common',
  BALANCED_TRANSITION: 'Balanced or changing', WARMING_UP: 'Gathering data',
}[value] || String(value || 'Gathering data').replaceAll('_', ' '));
const directionName = (value) => value === 'HIGH' ? 'HIGH · 2.00x counts as a win' : value === 'LOW' ? 'LOW · below 2.00x counts as a win' : value || '—';
const stateName = (value) => ({ LOCKED: 'Decision saved', WAITING: 'Waiting to start', ACTIVE: 'In progress', COMPLETED: 'Finished', INCOMPLETE: 'Incomplete', INVALIDATED: 'Cancelled: data issue' }[value] || value || '—');
const collectorStateName = (value) => ({
  HEALTHY: 'Working normally', COLLECTING: 'Receiving data', RECOVERING: 'Reconnecting',
  WAITING_FOR_SOURCE: 'Waiting for a data source', WAITING_FOR_AVIATOR: 'Waiting for Aviator',
  OFFLINE: 'No internet connection', STOPPED: 'Stopped', STALE: 'No recent update', UNKNOWN: 'Unknown',
}[value] || value || 'Status unavailable');

function Bar({ segments, label }) {
  const total = segments.reduce((sum, item) => sum + Math.max(0, Number(item.value) || 0), 0);
  return <div role="img" aria-label={label} className="flex h-3 w-full overflow-hidden rounded-full bg-zinc-800">
    {segments.map(item => {
      const amount = Math.max(0, Number(item.value) || 0);
      return amount > 0 && total > 0 ? <span key={item.label} className={item.color} style={{ width: `${amount / total * 100}%` }} /> : null;
    })}
  </div>;
}

function Legend({ items }) {
  return <div className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-xs">
    {items.map(item => <span key={item.label} className="inline-flex items-center gap-1.5 text-zinc-300"><i className={`h-2.5 w-2.5 rounded-full ${item.color}`} />{item.label}: <b className={item.textColor || 'text-zinc-100'}>{item.value}</b></span>)}
  </div>;
}

function SummaryCard({ label, value, detail, color = 'text-zinc-100' }) {
  return <div className="rounded-xl border border-zinc-800 bg-zinc-950/60 p-4">
    <p className="text-xs font-medium text-zinc-400">{label}</p>
    <p className={`mt-1 text-2xl font-bold tabular-nums ${color}`}>{value}</p>
    {detail && <p className="mt-1 text-xs leading-5 text-zinc-400">{detail}</p>}
  </div>;
}

function ForecastRow({ forecast }) {
  const wins = Number(forecast.wins || 0), losses = Number(forecast.losses || 0), settled = wins + losses;
  const direction = forecast.direction === 'HIGH' ? 'HIGH' : 'LOW';
  return <article className="rounded-xl border border-zinc-800 bg-zinc-950/50 p-4">
    <div className="flex flex-wrap items-start justify-between gap-3">
      <div><div className="font-semibold text-zinc-100">{directionName(direction)} · {forecast.horizon} rounds</div>
        <div className="mt-1 text-xs text-zinc-400">Decision #{forecast.decision_position} · {stateName(forecast.status)}</div></div>
      <span className={`rounded-full border px-2.5 py-1 text-xs font-semibold ${forecast.selection_status === 'FORCED_EXPLORATORY' ? 'border-amber-500/30 bg-amber-950/40 text-amber-200' : 'border-cyan-500/30 bg-cyan-950/40 text-cyan-200'}`}>
        {forecast.selection_status === 'FORCED_EXPLORATORY' ? 'Coverage test' : 'Regular test'}
      </span>
    </div>
    <div className="mt-4 grid gap-4 sm:grid-cols-2">
      <div>
        <div className="flex justify-between text-xs"><span className="text-zinc-400">Rounds completed</span><b className="text-zinc-100">{settled} / {forecast.horizon}</b></div>
        <div className="mt-2"><Bar label={`${wins} wins and ${losses} losses`} segments={[{ label: 'Win', value: wins, color: 'bg-emerald-400' }, { label: 'Loss', value: losses, color: 'bg-rose-500' }]} /></div>
        <Legend items={[{ label: 'Win', value: wins, color: 'bg-emerald-400', textColor: 'text-emerald-300' }, { label: 'Loss', value: losses, color: 'bg-rose-500', textColor: 'text-rose-300' }]} />
      </div>
      <div className="flex items-center gap-3 rounded-lg border border-zinc-800 bg-zinc-900/50 px-3 py-2">
        <div className="min-w-0 flex-1"><div className="text-xs text-zinc-400">Window result</div><div className="mt-1 text-sm text-zinc-200">{wins} wins · {losses} losses</div></div>
        <strong className={`text-lg tabular-nums ${Number(forecast.net_units || 0) >= 0 ? 'text-emerald-300' : 'text-rose-300'}`}>{Number(forecast.net_units || 0) > 0 ? '+' : ''}{forecast.net_units ?? 0} points</strong>
      </div>
    </div>
    <details className="mt-3 rounded-lg border border-zinc-800/80 px-3 py-2">
      <summary className="cursor-pointer text-xs font-medium text-zinc-300">More details</summary>
      <div className="mt-2 space-y-1 break-words text-xs leading-5 text-zinc-400">
        <p>Target rounds: #{forecast.target_start_position || '—'} to #{forecast.target_end_position || '—'}.</p>
        <p>Round IDs: {forecast.target_start_round_id || 'not known yet'} → {forecast.target_end_round_id || 'not known yet'}.</p>
        <p>HIGH: {forecast.high_count ?? 0} · LOW: {forecast.low_count ?? 0} · EXACT 2.00x: {forecast.exact_count ?? 0}.</p>
        <p>Why it was selected: {forecast.reason || 'No reason provided.'}</p>
        <p>Similar past situations: {forecast.evidence?.comparable_context_count ?? 0} · estimated result per round: {fmt(forecast.evidence?.estimated_net_per_staked_round, 3)}.</p>
        <p>Saved: {forecast.committed_at || '—'} · target first seen: {forecast.target_start_observed_at || 'waiting'} · start delay: {forecast.start_offset ?? 0} rounds.</p>
      </div>
    </details>
  </article>;
}

export default function DominanceIntelligence() {
  const [status, setStatus] = useState(null);
  const [performance, setPerformance] = useState(null);
  const [forecasts, setForecasts] = useState([]);
  const [filter, setFilter] = useState('ALL');
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const refresh = useCallback(async () => {
    try {
      const [s, p, f] = await Promise.all([
        api.get('/api/adie/status'), api.get('/api/adie/performance'),
        api.get('/api/adie/forecasts', { params: { limit: 100, ...(filter === 'ALL' ? {} : { status: filter }) } }),
      ]);
      setStatus(s.data); setPerformance(p.data); setForecasts(f.data.items || []); setError('');
    } catch (reason) {
      setError(reason?.response?.data?.detail?.error || reason?.message || 'Could not load the latest information.');
    } finally { setLoading(false); }
  }, [filter]);
  useEffect(() => {
    void refresh();
    const timer = window.setInterval(refresh, 10000);
    const onFocus = () => { if (document.visibilityState === 'visible') void refresh(); };
    document.addEventListener('visibilitychange', onFocus);
    return () => { window.clearInterval(timer); document.removeEventListener('visibilitychange', onFocus); };
  }, [refresh]);

  const current = status?.active_forecast;
  const ratios = status?.latest_round?.fingerprint?.rolling || status?.last_context?.rolling || {};
  const completed = Number(performance?.totals?.completed || 0);
  const wins = Number(performance?.totals?.wins || 0);
  const losses = Number(performance?.totals?.losses || 0);
  const net = Number(performance?.totals?.net || 0);
  const positive = completed ? Number(performance?.totals?.positive || 0) / completed : null;
  const context = status?.last_context || status?.latest_round?.fingerprint?.context_state || 'WARMING_UP';
  const activity = Number(status?.eligible_since_selection || 0);
  const collector = status?.collector || {};
  const roundAge = Number(status?.freshness_seconds);
  const hasRecentRound = Number.isFinite(roundAge) && roundAge >= 0 && roundAge <= 120;
  const collectorRunning = collector.collector_process_status === 'RUNNING';
  const collectorTitle = collector.browser_connected === true ? 'Browser connected'
    : hasRecentRound ? 'New rounds are arriving'
      : collectorRunning ? 'Collector is reconnecting'
        : collector.collector_process_status === 'STOPPED' ? 'Collector stopped' : 'Status unknown';
  const collectorDetail = hasRecentRound
    ? `Latest verified round arrived ${fmt(roundAge)} seconds ago${collector.browser_connected === false ? '; browser connection is not confirmed' : ''}.`
    : `${collectorStateName(collector.status)}${collector.browser_connected === false ? ' · browser connection not confirmed' : ''}.`;
  const contextRows = useMemo(() => Object.entries(ratios).filter(([key]) => ['5', '10', '20', '30', '50', '100', '200'].includes(key)), [ratios]);
  const exact = Number(status?.latest_round?.fingerprint?.rolling?.['20']?.exact || 0);

  return <main className="space-y-5">
    <header className="rounded-xl border border-cyan-500/20 bg-gradient-to-br from-cyan-950/25 via-zinc-900/80 to-zinc-900/80 p-5">
      <div className="flex flex-wrap items-start justify-between gap-3"><div>
        <p className="flex items-center gap-2 text-xs font-bold uppercase tracking-wide text-cyan-200"><BrainCircuit size={15} /> SHADOW monitoring</p>
        <h1 className="mt-1 text-2xl font-bold text-zinc-100">Dominance Intelligence</h1>
        <p className="mt-2 max-w-3xl text-sm leading-6 text-zinc-300">See which result has appeared more often and how SHADOW test windows are doing. This page does not place bets or change your existing rules.</p>
        <p className="mt-2 text-xs text-zinc-400">HIGH is above 2.00x · LOW is below 2.00x · EXACT is exactly 2.00x. For win/loss tracking, exactly 2.00x counts as a HIGH win.</p>
      </div><button type="button" onClick={() => { setLoading(true); void refresh(); }} className="flex items-center gap-2 rounded-lg border border-zinc-700 bg-zinc-950/50 px-3 py-2 text-xs font-medium text-zinc-100 hover:bg-zinc-800"><RefreshCw size={14} />Refresh</button></div>
    </header>
    {error && <div role="alert" className="rounded-lg border border-amber-500/30 bg-amber-950/25 p-3 text-sm text-amber-100"><b>Information unavailable</b><div className="mt-1 text-xs">{error}. Existing SHADOW rules continue independently.</div></div>}
    {loading && !status && <p className="text-sm text-zinc-400">Loading the latest information…</p>}

    {status && <>
      <section className="rounded-xl border border-zinc-800 bg-zinc-900/65 p-4 sm:p-5">
        <div className="flex flex-wrap items-center justify-between gap-2"><div><p className="text-xs font-bold uppercase tracking-wide text-cyan-200">Current picture</p><h2 className="mt-1 text-xl font-semibold text-zinc-100">{contextName(context)}</h2></div>
          <span className={`rounded-full border border-zinc-700 bg-zinc-950/50 px-3 py-1.5 text-xs font-semibold ${tone(status.health)}`}><Activity className="mr-1 inline" size={14} />{status.health === 'HEALTHY' ? 'Data is current' : status.health === 'DEGRADED' ? 'Data quality needs attention' : 'Waiting for data'}{status.freshness_seconds == null ? '' : ` · ${fmt(status.freshness_seconds)} sec ago`}</span></div>
        <div className="mt-3 grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
          <SummaryCard label="Latest result" value={status.latest_round ? `${fmt(status.latest_round.multiplier, 2)}x` : '—'} detail={status.latest_round ? `${status.latest_round.outcome || 'Result'} · ${status.latest_round.round_id}` : 'Waiting for a verified round'} color={status.latest_round?.outcome === 'HIGH' ? 'text-emerald-300' : status.latest_round?.outcome === 'LOW' ? 'text-rose-300' : 'text-zinc-100'} />
          <SummaryCard label="Uko amakuru atugeraho" value={collectorTitle} detail={collectorDetail} color={collector.browser_connected === true || hasRecentRound ? 'text-emerald-300' : 'text-amber-200'} />
          <SummaryCard label="Test coverage" value={`${activity} / 100 rounds`} detail={activity >= 80 ? 'Backup test selection is due soon' : `${Math.max(0, 80 - activity)} rounds until the backup watch starts`} color={activity >= 80 ? 'text-amber-200' : 'text-zinc-100'} />
          <SummaryCard label="Rounds checked" value={status.position || 0} detail="Counted by this page" />
        </div>
        <div className="mt-4 rounded-lg border border-zinc-800 bg-zinc-950/40 p-3">
          <h3 className="text-sm font-semibold text-zinc-100">Recent HIGH and LOW results</h3>
          <p className="mt-1 text-xs text-zinc-400">Green = HIGH · red = LOW · gray = exactly 2.00x.</p>
          <div className="mt-3 grid gap-3 sm:grid-cols-2 lg:grid-cols-3">{contextRows.map(([key, value]) => {
            const h = Number(value.high || 0), l = Number(value.low || 0), e = Number(value.exact || 0);
            return <div key={key} className="rounded-lg border border-zinc-800/80 bg-zinc-900/50 p-3">
              <div className="mb-2 flex items-center justify-between text-xs"><b className="text-zinc-100">Last {key} rounds</b><span className="text-zinc-400">HIGH {pct(value.high_share_nonexact)}</span></div>
              <Bar label={`Previous ${key} rounds: HIGH ${h}, LOW ${l}, EXACT ${e}`} segments={[{ label: 'HIGH', value: h, color: 'bg-emerald-400' }, { label: 'LOW', value: l, color: 'bg-rose-500' }, { label: 'EXACT', value: e, color: 'bg-zinc-500' }]} />
              <Legend items={[{ label: 'HIGH', value: h, color: 'bg-emerald-400', textColor: 'text-emerald-300' }, { label: 'LOW', value: l, color: 'bg-rose-500', textColor: 'text-rose-300' }, { label: 'EXACT', value: e, color: 'bg-zinc-500' }]} />
            </div>;
          })}</div>
        </div>
        <details className="mt-3 rounded-lg border border-zinc-800 px-3 py-2">
          <summary className="cursor-pointer text-xs font-medium text-zinc-300">More context details</summary>
          <div className="mt-3 grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
            <SummaryCard label="Interrupted streak patterns" value={`HIGH ${status.latest_round?.fingerprint?.interrupted_clusters_50?.high ?? 0} · LOW ${status.latest_round?.fingerprint?.interrupted_clusters_50?.low ?? 0}`} detail="Cases where a short opposite run interrupts a longer streak. For research only." />
            <SummaryCard label="Dominance stage" value={status.last_lifecycle?.phase || 'Gathering data'} detail={`${status.last_lifecycle?.strength_trend || '—'} · current run ${status.last_lifecycle?.context_run_length ?? 0}`} color="text-cyan-200" />
            <SummaryCard label="EXACT results in last 20 rounds" value={exact} detail="Exactly 2.00x is tracked separately." />
          </div>
          <p className="mt-2 break-words text-[11px] text-zinc-500">Collector state: {collectorStateName(status.collector?.status)} · Round ID: {status.latest_round?.round_id || '—'} · received: {status.latest_round?.received_at || '—'} · verified: {status.latest_round?.verified_at || '—'} · policy {status.policy_version} · test group {status.cohort}.</p>
        </details>
      </section>

      <section className="rounded-xl border border-zinc-800 bg-zinc-900/65 p-4 sm:p-5">
        <p className="text-xs font-bold uppercase tracking-wide text-cyan-200">Current test</p>
        <h2 className="mt-1 text-lg font-semibold text-zinc-100">What is being checked</h2>
        {current ? <div className="mt-3 grid gap-3 md:grid-cols-[1fr_2fr]">
          <SummaryCard label="Direction and length" value={`${current.direction} · ${current.horizon} rounds`} detail={`${current.selection_status === 'FORCED_EXPLORATORY' ? 'Coverage test' : 'Regular test'} · ${stateName(current.status)}`} color={current.direction === 'HIGH' ? 'text-emerald-300' : 'text-rose-300'} />
          <div className="rounded-xl border border-zinc-800 bg-zinc-950/50 p-4"><div className="flex flex-wrap justify-between gap-2 text-sm"><b className="text-zinc-100">Progress</b><span className="text-zinc-300">{Number(current.wins || 0) + Number(current.losses || 0)} / {current.horizon} rounds</span></div>
            <div className="mt-3"><Bar label={`Forecast progress: ${current.wins || 0} wins, ${current.losses || 0} losses`} segments={[{ label: 'Win', value: current.wins, color: 'bg-emerald-400' }, { label: 'Loss', value: current.losses, color: 'bg-rose-500' }, { label: 'Remaining', value: Math.max(0, Number(current.horizon || 0) - Number(current.wins || 0) - Number(current.losses || 0)), color: 'bg-zinc-700' }]} /></div>
            <Legend items={[{ label: 'Win', value: current.wins || 0, color: 'bg-emerald-400', textColor: 'text-emerald-300' }, { label: 'Loss', value: current.losses || 0, color: 'bg-rose-500', textColor: 'text-rose-300' }, { label: 'Remaining', value: Math.max(0, Number(current.horizon || 0) - Number(current.wins || 0) - Number(current.losses || 0)), color: 'bg-zinc-700' }]} />
            <p className="mt-3 text-xs leading-5 text-zinc-300">{current.reason}</p>
            <details className="mt-3 text-xs text-zinc-400"><summary className="cursor-pointer">Show target rounds and decision details</summary><p className="mt-2">Decision #{current.decision_position} · target #{current.target_start_position} → #{current.target_end_position} · IDs: {current.target_start_round_id || 'not known yet'} → {current.target_end_round_id || 'not known yet'} · delay {current.start_offset} · saved {current.committed_at || '—'}.</p></details>
          </div>
        </div> : <div className="mt-3 rounded-lg border border-zinc-800 bg-zinc-950/50 p-4 text-sm leading-6 text-zinc-300">No test is active right now. The system waits for enough reliable data and records at least one test for every 100 eligible rounds.</div>}
        <div className="mt-4 flex items-start gap-2 rounded-lg border border-amber-500/20 bg-amber-950/15 p-3 text-xs leading-5 text-amber-100"><ShieldAlert size={16} className="mt-0.5 shrink-0" /><p><b>Important:</b> These are SHADOW test results, not real money won or lost. Earlier research did not show reliable HIGH or LOW predictions. Do not treat these results as betting advice.</p></div>
      </section>

      <section className="rounded-xl border border-zinc-800 bg-zinc-900/65 p-4 sm:p-5">
        <div className="flex flex-wrap items-end justify-between gap-3"><div><p className="text-xs font-bold uppercase tracking-wide text-cyan-200">Test results</p><h2 className="mt-1 text-lg font-semibold text-zinc-100">SHADOW performance</h2></div><span className="text-xs text-zinc-400">Simulated scores · not real money</span></div>
        <div className="mt-3 grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
          <SummaryCard label="Completed tests" value={completed} detail="Test windows that have finished" />
          <SummaryCard label="Wins" value={wins} detail="Green means a win" color="text-emerald-300" />
          <SummaryCard label="Losses" value={losses} detail="Red means a loss" color="text-rose-300" />
          <SummaryCard label="Net score" value={`${net > 0 ? '+' : ''}${net}`} detail="Above zero is positive; below zero is negative" color={net >= 0 ? 'text-emerald-300' : 'text-rose-300'} />
        </div>
        <div className="mt-4 rounded-xl border border-zinc-800 bg-zinc-950/50 p-4">
          <div className="flex flex-wrap items-center justify-between gap-2"><div><h3 className="text-sm font-semibold text-zinc-100">Wins compared with losses</h3><p className="mt-1 text-xs text-zinc-400">Green shows wins; red shows losses.</p></div><b className="text-sm text-zinc-100">{pct(completed ? wins / Math.max(1, wins + losses) : null)} wins</b></div>
          <div className="mt-3"><Bar label={`Total outcome results: ${wins} wins and ${losses} losses`} segments={[{ label: 'Wins', value: wins, color: 'bg-emerald-400' }, { label: 'Losses', value: losses, color: 'bg-rose-500' }]} /></div>
          <Legend items={[{ label: 'Win', value: wins, color: 'bg-emerald-400', textColor: 'text-emerald-300' }, { label: 'Loss', value: losses, color: 'bg-rose-500', textColor: 'text-rose-300' }, { label: 'Tests with positive score', value: pct(completed ? positive : null), color: 'bg-cyan-400', textColor: 'text-cyan-300' }]} />
        </div>
        <details className="mt-4 rounded-xl border border-zinc-800 bg-zinc-950/40 p-4">
          <summary className="cursor-pointer text-sm font-semibold text-zinc-200">Compare with simple baselines</summary>
          <p className="mt-2 text-xs leading-5 text-zinc-400">This comparison checks whether the tests did better than simple historical averages. Test windows overlap, so they are not independent.</p>
          <div className="mt-3 flex flex-wrap gap-2">{(performance?.fixed_horizon_baselines?.groups || []).map(row => <span key={`${row.direction}-${row.horizon}`} className="rounded-lg border border-zinc-800 px-3 py-2 text-xs text-zinc-300">{row.direction} {row.horizon}: {row.completed}/{row.windows} finished · score {Number(row.net) > 0 ? '+' : ''}{row.net ?? 0} · {row.positive ?? 0} positive</span>)}{!performance?.fixed_horizon_baselines?.groups?.length && <span className="text-xs text-zinc-500">These comparisons start when new verified rounds arrive.</span>}</div>
          <p className="mt-3 text-xs text-zinc-400">Historical rates: HIGH {pct(performance?.baseline?.prior_high_rate)} · LOW {pct(performance?.baseline?.prior_low_rate)} · average score per round: HIGH {fmt(performance?.baseline?.prior_expected_net_per_round_high, 3)} / LOW {fmt(performance?.baseline?.prior_expected_net_per_round_low, 3)}.</p>
          <p className="mt-2 text-xs text-zinc-500">Phase 2 reference: {status.reference_manifest?.rows ?? 0} rounds · data check: {status.reference_manifest?.status || 'unknown'}. Historical results do not guarantee future results.</p>
        </details>

        <div className="mt-4"><div className="flex flex-wrap items-end justify-between gap-3"><div><h3 className="text-sm font-semibold text-zinc-100">Test history</h3><p className="mt-1 text-xs text-zinc-400">Green means win; red means loss. Open a test for its target rounds and selection reason.</p></div>
          <label className="flex items-center gap-2 text-xs text-zinc-300">Show<select aria-label="Filter tests" value={filter} onChange={event => setFilter(event.target.value)} className="rounded-lg border border-zinc-700 bg-zinc-950 px-2 py-2 text-xs text-zinc-100"><option value="ALL">All tests</option>{['LOCKED','WAITING','ACTIVE','COMPLETED','INCOMPLETE','INVALIDATED'].map(value => <option key={value} value={value}>{stateName(value)}</option>)}</select></label></div>
          <div className="mt-3 space-y-3">{forecasts.map(item => <ForecastRow key={item.forecast_id} forecast={item} />)}{!forecasts.length && <div className="rounded-lg border border-zinc-800 p-4 text-sm text-zinc-400">No tests in this group yet. The system may be waiting for enough reliable data.</div>}</div>
        </div>
        <p className="mt-4 flex items-center gap-2 border-t border-zinc-800 pt-3 text-xs text-zinc-500"><Sparkles size={14} />Policy {status.policy_version} · {status.decision_count || 0} recorded decisions. Test results do not automatically change the policy.</p>
      </section>
    </>}
  </main>;
}
