import { useCallback, useEffect, useState } from 'react';
import { api } from '../services/api.js';

const tones = {
  REPEATEDLY_WINNING: 'border-emerald-500/30 bg-emerald-400/10 text-emerald-200',
  WINNING_LATEST_WINDOW: 'border-emerald-500/30 bg-emerald-400/10 text-emerald-200',
  LOSING_LATEST_WINDOW: 'border-rose-500/30 bg-rose-400/10 text-rose-200',
  EVEN_OR_WAITING: 'border-amber-500/30 bg-amber-400/10 text-amber-200',
  NO_SETTLED_RESULTS: 'border-zinc-700 bg-zinc-800/70 text-zinc-300',
};

function WindowScore({ window }) {
  const positive = window.net > 0;
  const negative = window.net < 0;
  const tone = positive ? 'border-emerald-500/25 bg-emerald-400/10 text-emerald-200'
    : negative ? 'border-rose-500/25 bg-rose-400/10 text-rose-200'
      : 'border-zinc-700 bg-zinc-800/60 text-zinc-300';
  return <span title={`${window.session_status} · ${window.observed_rounds}/${window.window_size} rounds · ${window.wins} WIN / ${window.losses} LOSS${window.pending ? ` · ${window.pending} pending` : ''}`}
    className={`inline-flex min-w-20 flex-col rounded-lg border px-2 py-1.5 text-center text-[10px] ${tone}`}>
    <b className="font-semibold"><span className="text-emerald-200">W {window.wins}</span><span className="px-1 text-zinc-500">·</span><span className="text-rose-200">L {window.losses}</span></b>
    <span>{window.rolling_window
      ? `Rolling · ${window.observed_rounds}/${window.window_size}`
      : window.completed_window ? 'Completed' : `${window.observed_rounds}/${window.window_size} rounds`}</span>
  </span>;
}

function OutcomeMixBar({ wins, losses, pending = 0, includePending = false, label }) {
  const values = [Math.max(0, Number(wins) || 0), Math.max(0, Number(losses) || 0)];
  if (includePending) values.push(Math.max(0, Number(pending) || 0));
  const total = values.reduce((sum, value) => sum + value, 0);
  const widths = total ? values.map((value) => (value / total) * 100) : [0, 0, 0];
  return <div>
    <div role="img" aria-label={label} className="flex h-2.5 overflow-hidden rounded-full bg-zinc-800 ring-1 ring-inset ring-white/5">
      {total > 0 && <>
        {values[0] > 0 && <div className="bg-gradient-to-r from-emerald-500 to-emerald-300 transition-[width] duration-300" style={{ width: `${widths[0]}%` }} />}
        {values[1] > 0 && <div className="bg-gradient-to-r from-rose-600 to-rose-400 transition-[width] duration-300" style={{ width: `${widths[1]}%` }} />}
        {includePending && values[2] > 0 && <div className="bg-gradient-to-r from-amber-500 to-amber-300 transition-[width] duration-300" style={{ width: `${widths[2]}%` }} />}
      </>}
    </div>
    <div className="mt-1.5 flex flex-wrap gap-x-3 gap-y-1 text-[10px] font-medium tabular-nums">
      <span className="text-emerald-300">WIN {values[0]}</span>
      <span className="text-rose-300">LOSS {values[1]}</span>
      {includePending && <span className="text-amber-200">PENDING {values[2]}</span>}
    </div>
  </div>;
}

function RuleRow({ rule }) {
  const latest = rule.latest_window;
  const windowSize = Math.max(1, Number(latest?.window_size || 100));
  const completedRounds = Math.min(windowSize, Math.max(0, Number(latest?.observed_rounds || 0)));
  const remainingRounds = Math.max(0, windowSize - completedRounds);
  const windowProgress = (completedRounds / windowSize) * 100;
  const continuation = rule.continuation || {};
  const totalWins = Math.max(0, Number(rule.wins) || 0);
  const totalLosses = Math.max(0, Number(rule.losses) || 0);
  const latestWins = Math.max(0, Number(latest?.wins) || 0);
  const latestLosses = Math.max(0, Number(latest?.losses) || 0);
  const latestPending = Math.max(0, Number(latest?.pending) || 0);
  const latestNet = latestWins - latestLosses;
  const settled = Number(rule.wins || 0) + Number(rule.losses || 0);
  const windowFillTone = latestNet > 0 ? 'from-emerald-500 to-emerald-300'
    : latestNet < 0 ? 'from-rose-600 to-rose-400' : 'from-sky-500 to-cyan-300';
  const statusTone = tones[rule.status] || tones.NO_SETTLED_RESULTS;
  const continuationTone = continuation.status === 'REPEATEDLY_POSITIVE'
    ? 'text-emerald-300' : continuation.status === 'MIXED_OR_NEGATIVE'
      ? 'text-amber-200' : 'text-zinc-400';

  return <article className="rounded-xl border border-zinc-800 bg-zinc-950/45 p-4">
    <div className="flex flex-wrap items-start justify-between gap-3">
      <div className="min-w-0"><h3 className="font-semibold text-zinc-100">{rule.name}</h3>
        <p className="mt-1 break-all font-mono text-[11px] text-zinc-500">{rule.rule_id}</p>
        <p className={`mt-1 text-[10px] font-semibold uppercase tracking-wide ${rule.enabled ? 'text-emerald-300' : 'text-zinc-500'}`}>
          {rule.enabled ? 'Enabled' : rule.configured ? 'Configured · disabled' : 'Registry only'}
        </p></div>
      <span className={`rounded-full border px-2.5 py-1 text-[10px] font-bold uppercase tracking-wide ${statusTone}`}>{rule.status.replaceAll('_', ' ')}</span>
    </div>

    <div className="mt-3 grid gap-2 sm:grid-cols-4">
      <div className="rounded-lg bg-zinc-900/70 p-2.5"><p className="text-[10px] uppercase tracking-wide text-zinc-500">All scanned windows</p>
        <p className="mt-1 text-sm font-semibold tabular-nums"><span className="text-emerald-200">W {totalWins}</span><span className="px-1.5 text-zinc-600">·</span><span className="text-rose-200">L {totalLosses}</span></p>
        <p className={`text-xs font-medium ${rule.net > 0 ? 'text-emerald-300' : rule.net < 0 ? 'text-rose-300' : 'text-zinc-400'}`}>Net {rule.net > 0 ? '+' : ''}{rule.net}</p></div>
      <div className="rounded-lg bg-zinc-900/70 p-2.5"><p className="text-[10px] uppercase tracking-wide text-zinc-500">Latest window</p>
        {latest ? <>
          <p className="mt-1 text-sm font-semibold tabular-nums"><span className="text-emerald-200">W {latestWins}</span><span className="px-1.5 text-zinc-600">·</span><span className="text-rose-200">L {latestLosses}</span></p>
          <p className="text-xs text-zinc-500">{latest.rolling_window
            ? `Rolling latest ${latest.window_size} · ${latest.session_status}`
            : `${latest.observed_rounds}/${latest.window_size} rounds · ${latest.session_status}`}</p>
          <p className="text-xs text-sky-200/80">{latest.recovered_rounds ?? 0} recovered rounds included</p>
        </> : <p className="mt-1 text-sm text-zinc-400">No window data</p>}</div>
      <div className="rounded-lg bg-zinc-900/70 p-2.5"><p className="text-[10px] uppercase tracking-wide text-zinc-500">Recent full windows</p>
        <p className={`mt-1 text-sm font-semibold ${continuationTone}`}>{continuation.positive_windows ?? 0}/{continuation.completed_windows ?? 0} net-positive</p>
        <p className="text-xs text-zinc-500">{continuation.settled_results ?? 0} settled results in up to 3 windows</p></div>
      <div className="rounded-lg bg-zinc-900/70 p-2.5"><p className="text-[10px] uppercase tracking-wide text-zinc-500">Rule checks</p>
        <p className="mt-1 text-sm font-semibold tabular-nums text-zinc-100">{latest?.eligible_checks ?? 0} matched</p>
        <p className="text-xs text-zinc-500">Latest window · {latest?.false_checks ?? 0} did not match</p></div>
    </div>

    <div className="mt-3 rounded-xl border border-sky-400/10 bg-gradient-to-br from-zinc-900/90 to-zinc-950/80 p-3">
      <div className="mb-2 flex flex-wrap items-end justify-between gap-2">
        <div><p className="text-[10px] font-semibold uppercase tracking-[0.14em] text-sky-200/80">Rule window progress</p>
          <p className="mt-0.5 font-mono text-lg font-bold tabular-nums text-zinc-100">{completedRounds}<span className="px-1 text-zinc-600">/</span><span className="text-zinc-400">{windowSize}</span><span className="ml-1 text-xs font-medium text-zinc-500">rounds</span></p></div>
        <div className="text-right"><p className="text-xs font-semibold tabular-nums text-zinc-200">{Math.round(windowProgress)}% complete</p>
          <p className="mt-0.5 text-[10px] text-zinc-500">{latest?.rolling_window && completedRounds === windowSize
            ? `Rolling latest ${windowSize} · ${latest?.session_status || 'COLLECTING'}`
            : `${remainingRounds} remaining · ${latest?.session_status || 'WAITING'}`}</p></div>
      </div>
      <div role="progressbar" aria-label={`${rule.name} collector window progress`}
        aria-valuemin={0} aria-valuemax={windowSize} aria-valuenow={completedRounds}
        aria-valuetext={latest?.rolling_window && completedRounds === windowSize
          ? `Rolling latest ${windowSize} verified rounds`
          : `${completedRounds} of ${windowSize} verified rounds; ${remainingRounds} remaining`}
        className="relative h-3 overflow-hidden rounded-full bg-zinc-800 ring-1 ring-inset ring-white/5">
        <div className={`h-full rounded-full bg-gradient-to-r ${windowFillTone} shadow-[0_0_12px_rgba(56,189,248,0.22)] transition-[width] duration-500 ease-out`}
          style={{ width: `${windowProgress}%` }} />
        <div aria-hidden="true" className="pointer-events-none absolute inset-0 grid grid-cols-4">
          {[0, 1, 2, 3].map((tick) => <span key={tick} className="border-r border-zinc-950/50" />)}
        </div>
      </div>
      <div className="mt-3 rounded-lg border border-zinc-800/80 bg-black/15 p-2.5">
        <p className="mb-2 text-[10px] font-semibold uppercase tracking-wide text-zinc-500">Latest window outcomes</p>
        <OutcomeMixBar wins={latestWins} losses={latestLosses} pending={latestPending} includePending
          label={`${rule.name} latest window: ${latestWins} wins, ${latestLosses} losses, ${latestPending} pending`} />
      </div>
    </div>

    <div className="mt-3 grid gap-3 lg:grid-cols-[1fr_auto] lg:items-center">
      <div><div className="mb-1 flex justify-between text-[10px] text-zinc-500"><span>Settled outcomes across scanned windows</span><span>{settled ? `${Math.round((totalWins / settled) * 100)}% WIN` : 'No settled outcomes'}</span></div>
        <OutcomeMixBar wins={totalWins} losses={totalLosses}
          label={`${rule.name} scanned windows: ${totalWins} wins and ${totalLosses} losses`} />
      </div>
      <div className="flex flex-wrap gap-1.5">{(rule.recent_windows || []).map(window => <WindowScore key={window.session_id} window={window} />)}</div>
    </div>
    <p className="mt-3 text-[11px] text-zinc-500">{rule.direct_selections ?? 0} direct · {rule.shared_attributions ?? 0} shared outcome attributions · {rule.settled_total ?? settled} settled total</p>
  </article>;
}

export default function RulePerformancePanel() {
  const [data, setData] = useState(null);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(true);
  const refresh = useCallback(async () => {
    try {
      const response = await api.get('/api/rules-engine/rule-performance');
      setData(response.data);
      setError('');
    } catch (reason) {
      setError(reason?.response?.data?.detail || reason?.message || 'Request failed');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
    const timer = window.setInterval(refresh, 30000);
    const onFocus = () => { if (document.visibilityState === 'visible') void refresh(); };
    document.addEventListener('visibilitychange', onFocus);
    return () => { window.clearInterval(timer); document.removeEventListener('visibilitychange', onFocus); };
  }, [refresh]);

  const rules = data?.rules || [];
  const repeated = rules.filter(rule => rule.status === 'REPEATEDLY_WINNING');
  const enabledCount = rules.filter(rule => rule.enabled === true).length;
  const observer = data?.observer || {};
  const observerTone = observer.status === 'ERROR' ? 'text-rose-300'
    : observer.status === 'COLLECTING' ? 'text-emerald-300'
    : observer.status === 'NO_RECENT_ROUND' ? 'text-amber-200' : 'text-zinc-300';
  return <section className="rounded-xl border border-emerald-500/25 bg-gradient-to-br from-emerald-950/20 via-zinc-900/80 to-zinc-900/70 p-4 shadow-lg shadow-black/10 sm:p-5">
    <div className="flex flex-wrap items-start justify-between gap-3">
      <div><p className="text-[10px] font-bold uppercase tracking-[0.18em] text-emerald-300">Collector results · per rule</p>
        <h2 className="mt-1 text-lg font-semibold text-zinc-100">Rule performance across windows</h2>
        <p className="mt-1 max-w-3xl text-xs leading-5 text-zinc-400">Every registered rule is scored independently from verified collector rounds, including disabled and unconfigured rules. This report keeps updating without a Start SHADOW session.</p></div>
      <button type="button" onClick={() => { setLoading(true); void refresh(); }} className="rounded-lg border border-zinc-700 bg-zinc-950/50 px-3 py-2 text-xs font-medium text-zinc-200 hover:bg-zinc-800">Refresh</button>
    </div>
    {loading && !data && <p className="mt-4 text-sm text-zinc-400">Loading rule results…</p>}
    {error && <div role="alert" className="mt-4 rounded-lg border border-amber-500/30 bg-amber-950/30 p-3 text-xs text-amber-100">
      <b>Winner Predict rules API unavailable</b><p className="mt-1">GET /api/rules-engine/rule-performance · {error}</p></div>}
    {data && <>
      <div className="mt-4 flex flex-wrap items-center gap-x-4 gap-y-1 rounded-lg border border-zinc-800 bg-zinc-950/50 px-3 py-2 text-xs">
        <span className={`font-semibold ${observerTone}`}>Collector: {(observer.status || 'UNKNOWN').replaceAll('_', ' ')}</span>
        <span className="text-zinc-400">Last committed round {observer.last_round_index ? `#${observer.last_round_index}` : '—'}</span>
        <span className="text-zinc-500">{observer.rules_monitored ?? rules.length} rules monitored · {observer.window_size ?? 100} rounds per window</span>
        {observer.error && <span className="text-rose-300">Observer error: {observer.error}</span>}
      </div>
      <div className="mt-4 grid gap-2 sm:grid-cols-2 xl:grid-cols-4">
        <div className="rounded-lg border border-zinc-800 bg-zinc-950/55 p-3"><p className="text-[10px] uppercase tracking-wide text-zinc-500">Rules in report</p><p className="mt-1 text-xl font-bold tabular-nums text-zinc-100">{rules.length}</p></div>
        <div className="rounded-lg border border-emerald-500/20 bg-emerald-950/20 p-3"><p className="text-[10px] uppercase tracking-wide text-emerald-200/70">Enabled</p><p className="mt-1 text-xl font-bold tabular-nums text-emerald-200">{enabledCount}</p></div>
        <div className="rounded-lg border border-zinc-700 bg-zinc-950/55 p-3"><p className="text-[10px] uppercase tracking-wide text-zinc-500">Not enabled</p><p className="mt-1 text-xl font-bold tabular-nums text-zinc-200">{rules.length - enabledCount}</p></div>
        <div className="rounded-lg border border-zinc-800 bg-zinc-950/55 p-3"><p className="text-[10px] uppercase tracking-wide text-zinc-500">Rule windows scanned</p><p className="mt-1 text-xl font-bold tabular-nums text-zinc-100">{data.windows_scanned ?? 0}</p></div>
        <div className="rounded-lg border border-sky-500/20 bg-sky-950/20 p-3"><p className="text-[10px] uppercase tracking-wide text-sky-200/70">Repeatedly net-positive</p><p className="mt-1 text-xl font-bold tabular-nums text-sky-200">{repeated.length}</p></div>
      </div>
      <div className="mt-3 space-y-2">{rules.map(rule => <RuleRow key={rule.rule_id} rule={rule} />)}</div>
      <p className="mt-3 rounded-lg border border-amber-500/20 bg-amber-950/15 p-3 text-[11px] leading-5 text-amber-100/80">{data.interpretation}</p>
    </>}
  </section>;
}
