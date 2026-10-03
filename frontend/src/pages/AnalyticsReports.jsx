import { useCallback, useEffect, useMemo, useState } from 'react';
import { Download, RefreshCw } from 'lucide-react';
import { downloadAnalyticsReport, getAnalyticsCurrent, getAnalyticsPastRound, getAnalyticsProgress, getAnalyticsReport, getAnalyticsReports, getAnalyticsRounds, getPatternReport, API_READ_RETRY_EVENT, API_READ_RETRY_DONE_EVENT } from '../services/api.js';
import { getWebSocketUrl } from '../auth.js';

const pct = (value) => Number.isFinite(value) ? `${value.toFixed(1)}%` : 'UNKNOWN';
const safeCount = (value) => Number.isFinite(value) ? value.toLocaleString() : 'UNKNOWN';
const Panel = ({ title, help, children }) => <section className="rounded-xl border border-zinc-800 bg-zinc-900/60 p-5"><h2 className="text-base font-semibold text-zinc-100">{title}</h2>{help && <p className="mt-1 text-sm text-zinc-400">{help}</p>}<div className="mt-4">{children}</div></section>;
const Card = ({ label, value, note }) => <div className="rounded-xl border border-zinc-800 bg-zinc-900/60 p-4"><div className="text-sm text-zinc-400">{label}</div><div className="mt-1 text-2xl font-semibold">{value}</div>{note && <div className="mt-1 text-xs text-zinc-500">{note}</div>}</div>;

function AnalysisReportCard({ title, analysis, generatedAt, source, firstRound, lastRound, kind }) {
  const tones = kind === 'saved'
    ? 'border-violet-700/60 bg-violet-950/30 text-violet-200'
    : kind === 'past'
      ? 'border-amber-700/60 bg-amber-950/30 text-amber-200'
      : 'border-emerald-700/60 bg-emerald-950/30 text-emerald-200';
  return <section className={`rounded-xl border p-5 ${tones}`}>
    <div className="text-xs font-bold uppercase tracking-wider">Analysis report · {kind === 'saved' ? 'saved snapshot' : kind === 'past' ? 'past rounds' : 'current view'}</div>
    <h2 className="mt-2 text-lg font-semibold">{title}</h2>
    <div className="mt-3 grid gap-3 sm:grid-cols-2 lg:grid-cols-4 text-sm">
      <div><span className="opacity-70">Valid rounds</span><div className="mt-1 font-semibold">{safeCount(analysis?.round_count)}</div></div>
      <div><span className="opacity-70">Below 2x / 2x or higher</span><div className="mt-1 font-semibold">{pct(analysis?.under_2x?.percentage)} / {pct(analysis?.at_least_2x?.percentage)}</div></div>
      <div><span className="opacity-70">Round range</span><div className="mt-1 font-semibold">{firstRound && lastRound ? `${firstRound} → ${lastRound}` : lastRound ? `Stored history through ${lastRound}` : 'UNKNOWN'}</div></div>
      <div><span className="opacity-70">Report time · source</span><div className="mt-1 font-semibold">{generatedAt || 'UNKNOWN'}</div><div className="text-xs opacity-70">{source || 'UNKNOWN'}</div></div>
    </div>
    <p className="mt-3 text-xs opacity-75">Describes verified stored rounds. This is historical analysis, not a promise about the next round.</p>
  </section>;
}

const reportTypes = [
  { value: 'ROUND_100_REPORT', label: 'Every 100 rounds' },
  { value: 'ROUND_REPORT', label: 'Every 1,000 rounds' },
  { value: 'DAILY_REPORT', label: 'Daily reports' },
];

function ReportProgressCard({ title, color, progress }) {
  const report = progress?.latest_report;
  const styles = color === 'green'
    ? { border: 'border-emerald-700/60', background: 'bg-emerald-950/30', bar: 'bg-emerald-400', label: 'text-emerald-300' }
    : { border: 'border-sky-700/60', background: 'bg-sky-950/30', bar: 'bg-sky-400', label: 'text-sky-300' };
  const collected = progress?.rounds_collected;
  const size = progress?.block_size;
  return <section className={`rounded-xl border ${styles.border} ${styles.background} p-5`}>
    <div className={`text-xs font-bold uppercase tracking-wider ${styles.label}`}>{title}</div>
    <div className="mt-2 text-lg font-semibold">{progress?.status === 'REPORT_PENDING' && collected === size ? 'Full block collected · report pending' : Number.isFinite(progress?.rounds_remaining) ? `${progress.rounds_remaining} rounds until next report${progress?.status === 'REPORT_PENDING' ? ' · earlier report pending' : ''}` : 'Progress unknown'}</div>
    <div className="mt-1 text-sm text-zinc-400">{Number.isFinite(collected) && Number.isFinite(size) ? `${collected} of ${size} valid rounds collected` : 'Waiting for verified round count'}</div>
    {Number.isFinite(progress?.progress_percent) && <div className="mt-3 h-2 overflow-hidden rounded bg-black/30"><div className={`h-full ${styles.bar} transition-[width] duration-300`} style={{ width: `${Math.max(0, Math.min(100, progress.progress_percent))}%` }} /></div>}
    {report ? <div className="mt-4 border-t border-white/10 pt-3 text-sm"><div className="text-zinc-300">Latest saved report · {safeCount(report.round_count)} rounds</div><div className="mt-1 flex flex-wrap gap-x-4 gap-y-1 text-xs text-zinc-400"><span>Below 2x: {pct(report.under_2x?.percentage)}</span><span>2x or higher: {pct(report.at_least_2x?.percentage)}</span></div><div className="mt-1 text-xs text-zinc-500">{report.first_round} → {report.last_round} · {report.generated_at}</div></div>
      : <div className="mt-4 border-t border-white/10 pt-3 text-sm text-zinc-400">No saved report for a completed block yet.</div>}
  </section>;
}

export default function AnalyticsReports() {
  const [snapshot, setSnapshot] = useState(null);
  const [reportProgress, setReportProgress] = useState(null);
  const [progressStale, setProgressStale] = useState(false);
  const [patternReport, setPatternReport] = useState(null);
  const [reports, setReports] = useState([]);
  const [reportType, setReportType] = useState('ROUND_100_REPORT');
  const [selected, setSelected] = useState(null);
  const [pastRound, setPastRound] = useState(null);
  const [roundOptions, setRoundOptions] = useState([]);
  const [roundCursor, setRoundCursor] = useState(null);
  const [hasOlderRounds, setHasOlderRounds] = useState(false);
  const [reportOffset, setReportOffset] = useState(null);
  const [hasOlderReports, setHasOlderReports] = useState(false);
  const [scope, setScope] = useState('latest_100');
  const [loading, setLoading] = useState(true);
  const [snapshotStale, setSnapshotStale] = useState(false);
  const [error, setError] = useState('');
  const [retryMessage, setRetryMessage] = useState('');

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const results = await Promise.allSettled([
        getAnalyticsCurrent(), getAnalyticsReports(reportType, 100), getAnalyticsRounds(100), getAnalyticsProgress(),
      ]);
      const [currentResult, reportsResult, roundsResult, progressResult] = results;
      const failed = results.find((result) => result.status === 'rejected');
      if (currentResult.status === 'fulfilled') {
        setSnapshot(currentResult.value);
        setReportProgress(currentResult.value.report_progress || null);
        setSnapshotStale(false);
      } else setSnapshotStale(true);
      if (reportsResult.status === 'fulfilled') {
        setReports(reportsResult.value.reports || []);
        setReportOffset(reportsResult.value.next_offset ?? null);
        setHasOlderReports(Boolean(reportsResult.value.has_more));
      }
      if (roundsResult.status === 'fulfilled') {
        setRoundOptions(roundsResult.value.rounds || []);
        setRoundCursor(roundsResult.value.next_before_round_index ?? null);
        setHasOlderRounds(Boolean(roundsResult.value.has_older));
      }
      if (progressResult.status === 'fulfilled') {
        setReportProgress(progressResult.value);
        setProgressStale(false);
      }
      if (failed) {
        const cause = failed.reason;
        const path = cause?.config?.url || 'a research endpoint';
        const status = cause?.response?.status;
        const detail = cause?.response?.data?.detail;
        if (status === 422 && path.includes('/api/analytics/reports')) {
          setError('The backend needs a restart to support 100-round reports. From the project root run: source ~/.nvm/nvm.sh && node scripts/start-all.js. No report values were filled in.');
        } else if (status === 404) {
          setError(`The running backend does not provide ${path}. Restart it from the project root, then refresh. No values were filled in.`);
        } else {
          setError(typeof detail === 'string' ? detail : detail ? JSON.stringify(detail) : cause.message || 'Some research data could not be refreshed. Previously loaded values are kept.');
        }
      } else setError('');
      // Pattern discovery can scan the full history. Load it after the compact
      // summary so it cannot delay the main report response or first paint.
      try {
        setPatternReport(await getPatternReport());
      } catch (cause) {
        setError((current) => current || (cause?.response?.status === 404
          ? 'Pattern analysis is not available in this backend version.'
          : 'Pattern analysis timed out or could not be refreshed. Other loaded results are still shown.'));
      }
    } finally {
      setLoading(false);
    }
  }, [reportType]);

  useEffect(() => { refresh(); }, [refresh]);

  useEffect(() => {
    const onRetry = (event) => {
      const url = String(event.detail?.url || '');
      if (url.includes('/api/analytics') || url.includes('/api/patterns')) {
        setRetryMessage(`Request timed out or briefly disconnected. Retrying (${event.detail.attempt})…`);
      }
    };
    const onRetryDone = (event) => {
      const url = String(event.detail?.url || '');
      if (url.includes('/api/analytics') || url.includes('/api/patterns')) {
        setRetryMessage(event.detail.recovered ? 'Connection restored; latest data loaded.' : 'Automatic retry ended. Last verified values are kept and marked stale.');
      }
    };
    window.addEventListener(API_READ_RETRY_EVENT, onRetry);
    window.addEventListener(API_READ_RETRY_DONE_EVENT, onRetryDone);
    return () => {
      window.removeEventListener(API_READ_RETRY_EVENT, onRetry);
      window.removeEventListener(API_READ_RETRY_DONE_EVENT, onRetryDone);
    };
  }, []);

  useEffect(() => {
    let socket = null;
    let reconnectTimer = null;
    let fallbackTimer = null;
    let stopped = false;
    let attempts = 0;

    const loadProgress = async () => {
      try {
        const current = await getAnalyticsProgress();
        setReportProgress(current);
        setProgressStale(false);
      } catch { setProgressStale(true); }
    };
    const scheduleReconnect = () => {
      if (stopped || reconnectTimer) return;
      const delay = Math.min(1000 * (2 ** attempts), 15000);
      attempts += 1;
      reconnectTimer = window.setTimeout(() => { reconnectTimer = null; connect(); }, delay);
    };
    const connect = () => {
      if (stopped) return;
      try { socket = new WebSocket(getWebSocketUrl()); }
      catch { scheduleReconnect(); return; }
      socket.onopen = () => { attempts = 0; };
      socket.onmessage = (event) => {
        try {
          const message = JSON.parse(event.data);
          if (message.type === 'analytics:progress' || message.type === 'analytics:reports_updated') {
            if (message.report_progress) {
              setReportProgress(message.report_progress);
              setProgressStale(false);
            }
          }
        } catch { /* Ignore malformed frames; REST remains authoritative. */ }
      };
      socket.onerror = () => {};
      socket.onclose = () => {
        if (stopped) return;
        setProgressStale(true);
        scheduleReconnect();
      };
    };
    connect();
    fallbackTimer = window.setInterval(() => {
      if (socket?.readyState !== WebSocket.OPEN) loadProgress();
    }, 15000);
    return () => {
      stopped = true;
      if (reconnectTimer) window.clearTimeout(reconnectTimer);
      if (fallbackTimer) window.clearInterval(fallbackTimer);
      if (socket) { socket.onclose = null; socket.close(); }
    };
  }, []);

  const analysis = selected?.analysis || pastRound?.analysis || snapshot?.[scope];
  const bins = useMemo(() => Object.entries(analysis?.multiplier_histogram || {}), [analysis]);
  const patterns = useMemo(() => patternReport?.patterns || [], [patternReport]);

  async function openReport(reportId) {
    if (!reportId) { setSelected(null); setPastRound(null); return; }
    try {
      const report = await getAnalyticsReport(reportId);
      setSelected(report);
      setPastRound(null);
      setError('');
    } catch (cause) {
      setError(cause?.response?.data?.detail || cause.message || 'That saved report could not be opened.');
    }
  }

  async function selectPastRound(roundId) {
    if (!roundId) { setPastRound(null); return; }
    try {
      setSelected(null);
      setPastRound(await getAnalyticsPastRound(roundId));
      setError('');
    } catch (cause) {
      setError(cause?.response?.data?.detail || cause.message || 'This past round could not be analyzed.');
    }
  }

  async function loadOlderRounds() {
    if (roundCursor == null) return;
    try {
      const page = await getAnalyticsRounds(100, roundCursor);
      setRoundOptions((current) => [...current, ...(page.rounds || [])]);
      setRoundCursor(page.next_before_round_index ?? null);
      setHasOlderRounds(Boolean(page.has_older));
    } catch (cause) { setError(cause?.message || 'Older rounds could not be loaded.'); }
  }

  async function loadOlderReports() {
    if (reportOffset == null) return;
    try {
      const page = await getAnalyticsReports(reportType, 100, reportOffset);
      setReports((current) => [...current, ...(page.reports || [])]);
      setReportOffset(page.next_offset ?? null);
      setHasOlderReports(Boolean(page.has_more));
    } catch (cause) { setError(cause?.message || 'Older reports could not be loaded.'); }
  }

  const title = selected
    ? `${selected.round_count} round saved report`
    : pastRound ? `History through round ${pastRound.last_round}`
      : scope === 'latest_100' ? 'Latest 100 rounds'
        : scope === 'latest_1000' ? 'Latest 1,000 rounds'
          : scope === 'last_24_hours' ? 'Last 24 hours' : scope === 'today' ? 'Today so far' : 'All stored rounds';

  return <div className="space-y-5">
    <header className="flex flex-wrap items-end justify-between gap-4">
      <div>
        <div className="text-xs font-bold uppercase tracking-[0.2em] text-emerald-300">Past rounds research</div>
        <h1 className="mt-2 text-3xl font-semibold">Patterns & Analytics</h1>
        <p className="mt-2 max-w-3xl text-sm text-zinc-400">A simple summary of real stored Aviator rounds. It describes what happened before; it does not predict the next round.</p>
      </div>
      <button onClick={refresh} className="inline-flex items-center gap-2 rounded-lg border border-zinc-700 px-4 py-2 text-sm hover:bg-zinc-800"><RefreshCw size={16} className={loading ? 'animate-spin' : ''} />Refresh</button>
    </header>

    {error && <div role="status" className="rounded-lg border border-amber-800 bg-amber-950/30 p-3 text-sm text-amber-200">{error}</div>}
    {retryMessage && <div role="status" className="rounded-lg border border-sky-800 bg-sky-950/30 p-3 text-sm text-sky-200">{retryMessage}</div>}
    {!snapshot && loading && <div className="rounded-lg border border-zinc-800 p-6 text-sm text-zinc-400">Loading verified round history…</div>}
    {!snapshot && !loading && !error && <div className="rounded-lg border border-zinc-800 p-6 text-sm text-zinc-400">Analytics summary is unavailable. Automatic read retries are active; refresh when the backend reconnects.</div>}
    {reportProgress && <div className={`text-xs ${progressStale ? 'text-amber-300' : 'text-zinc-500'}`}>{progressStale ? 'Round progress is STALE until connection returns.' : 'Round progress is based on validated stored rounds and updates as collection reports new rounds.'}</div>}
    {reportProgress && <div className="grid gap-4 md:grid-cols-2">
      <ReportProgressCard title="100-round report" color="green" progress={reportProgress.every_100} />
      <ReportProgressCard title="1,000-round report" color="blue" progress={reportProgress.every_1000} />
    </div>}
    {snapshot && <>
      <div className="text-xs text-zinc-500">{snapshotStale ? 'STALE — ' : 'Last verified snapshot — '}{snapshot.generated_at || 'timestamp unknown'} · source {snapshot.data_source || 'UNKNOWN'}</div>
      <Panel title="Choose what to look at" help="These buttons change the round history used in the summary below.">
        <div className="flex flex-wrap gap-2">
          {[["latest_100", "Last 100 rounds"], ["latest_1000", "Last 1,000 rounds"], ["last_24_hours", "Last 24 hours"], ["today", "Today"], ["historical", "All history"]].map(([value, label]) =>
            <button key={value} onClick={() => { setSelected(null); setPastRound(null); setScope(value); }} aria-pressed={!selected && !pastRound && scope === value}
              className={`rounded-lg border px-3 py-2 text-sm ${!selected && !pastRound && scope === value ? 'border-emerald-500 bg-emerald-950/40 text-emerald-200' : 'border-zinc-700 text-zinc-300 hover:bg-zinc-800'}`}>{label}</button>)}
        </div>
        <div className="mt-3 text-sm text-zinc-400">Showing: <strong className="text-zinc-200">{title}</strong> · {safeCount(analysis?.round_count)} real rounds</div>
      </Panel>

      <AnalysisReportCard
        title={selected ? `${selected.round_count} round saved report` : pastRound ? `History through round ${pastRound.last_round}` : title}
        analysis={analysis}
        generatedAt={selected?.generated_at || pastRound?.generated_at || snapshot.generated_at}
        source={selected?.data_source || pastRound?.data_source || snapshot.data_source}
        firstRound={selected?.first_round || pastRound?.first_round || analysis?.first_round}
        lastRound={selected?.last_round || pastRound?.last_round || analysis?.last_round}
        kind={selected ? 'saved' : pastRound ? 'past' : 'current'}
      />

      <div className="grid gap-3 md:grid-cols-3">
        <Card label="Rounds in this view" value={safeCount(analysis?.round_count)} note={`Stored through: ${snapshot.data_available_through_round || 'UNKNOWN'}`} />
        <Card label="Below 2.00x" value={pct(analysis?.under_2x?.percentage)} note={`${safeCount(analysis?.under_2x?.count)} rounds in this view`} />
        <Card label="2.00x or higher" value={pct(analysis?.at_least_2x?.percentage)} note={`${safeCount(analysis?.at_least_2x?.count)} rounds in this view`} />
      </div>

      <Panel title="Multiplier results" help="Each bar shows the share of recorded rounds that ended in that range.">
        {bins.length ? <div className="space-y-3">{bins.map(([label, item]) => <div key={label} className="grid grid-cols-[7rem_1fr_5rem] items-center gap-3 text-sm"><span className="text-zinc-300">{label}</span><div className="h-3 overflow-hidden rounded bg-zinc-800"><div className="h-full bg-emerald-400" style={{ width: `${Math.max(0, Math.min(100, item.percentage || 0))}%` }} /></div><span className="text-right tabular-nums">{pct(item.percentage)}</span></div>)}</div> : <p className="text-sm text-zinc-500">No validated rounds are available for this view.</p>}
        <div className="mt-4 grid gap-3 sm:grid-cols-3"><Card label="Average multiplier" value={analysis?.multiplier?.mean == null ? 'UNKNOWN' : `${analysis.multiplier.mean.toFixed(2)}x`} /><Card label="Middle result (median)" value={analysis?.multiplier?.median == null ? 'UNKNOWN' : `${analysis.multiplier.median.toFixed(2)}x`} /><Card label="Data quality" value={analysis?.data_quality?.status || 'UNKNOWN'} note="Only validated stored rounds are included." /></div>
      </Panel>

      <Panel title="Patterns seen in history" help="‘After this pattern’ describes the next recorded result in the stored data. Small samples can be misleading.">
        <div className="mb-3 grid gap-3 sm:grid-cols-3"><Card label="Overall 2x+ rate" value={pct(patternReport?.baseline?.rate == null ? null : patternReport.baseline.rate * 100)} note={`${safeCount(patternReport?.baseline?.sample_size)} rounds in the pattern baseline`} /><Card label="Minimum sample used" value={safeCount(patternReport?.minimum_sample_size)} note="Below this count, evidence is marked insufficient." /><Card label="Patterns listed" value={safeCount(patterns.length)} note="Patterns are historical evidence, not betting instructions." /></div>
        {patterns.length ? <div className="overflow-x-auto"><table className="w-full min-w-[720px] text-left text-sm"><thead className="text-xs uppercase text-zinc-500"><tr><th className="py-2 pr-3">What happened before</th><th className="pr-3">Times observed</th><th className="pr-3">Next result ≥2x</th><th className="pr-3">95% range</th><th>Status</th></tr></thead><tbody>{patterns.slice(0, 20).map((item) => <tr key={item.pattern_id} className="border-t border-zinc-800"><td className="py-3 pr-3"><div>{item.pattern}</div><div className="text-xs text-zinc-500">{item.kind}</div></td><td className="pr-3">{safeCount(item.sample_size)}</td><td className="pr-3">{pct(item.success_rate == null ? null : item.success_rate * 100)}</td><td className="pr-3">{item.confidence_interval?.lower == null ? 'UNKNOWN' : `${pct(item.confidence_interval.lower * 100)}–${pct(item.confidence_interval.upper * 100)}`}</td><td>{item.evidence_state?.replaceAll('_', ' ') || item.stability || 'UNKNOWN'}</td></tr>)}</tbody></table></div> : <p className="text-sm text-zinc-500">Pattern evidence is unavailable or there are not enough valid stored rounds.</p>}
      </Panel>

      <Panel title="Reports and older rounds" help="Saved reports are fixed snapshots. New reports are created after each complete 100 or 1,000 valid rounds and after a completed UTC day.">
        <div className="flex flex-wrap gap-3">
          <label className="flex items-center gap-2 text-sm text-zinc-300">Report type<select aria-label="Report type" value={reportType} onChange={(event) => { setReportType(event.target.value); setSelected(null); }} className="rounded border border-zinc-700 bg-zinc-950 px-3 py-2">{reportTypes.map((type) => <option key={type.value} value={type.value}>{type.label}</option>)}</select></label>
          <label className="flex items-center gap-2 text-sm text-zinc-300">Saved reports<select aria-label="Saved reports" value={selected?.report_id || ''} onChange={(event) => openReport(event.target.value)} className="min-w-64 rounded border border-zinc-700 bg-zinc-950 px-3 py-2"><option value="">Choose a saved report…</option>{reports.map((report) => <option key={report.report_id} value={report.report_id}>{report.first_round} to {report.last_round} · {report.round_count} rounds</option>)}</select></label>
          {selected && <><button onClick={() => downloadAnalyticsReport(selected.report_id, 'json')} className="inline-flex items-center gap-2 rounded border border-zinc-700 px-3 py-2 text-sm hover:bg-zinc-800"><Download size={15} />Download JSON</button><button onClick={() => downloadAnalyticsReport(selected.report_id, 'csv')} className="inline-flex items-center gap-2 rounded border border-zinc-700 px-3 py-2 text-sm hover:bg-zinc-800"><Download size={15} />Download CSV</button></>}
          <label className="flex items-center gap-2 text-sm text-zinc-300">Past round<select aria-label="Analyze through a past round" value={pastRound?.last_round || ''} onChange={(event) => selectPastRound(event.target.value)} className="min-w-64 rounded border border-zinc-700 bg-zinc-950 px-3 py-2"><option value="">Choose an older round…</option>{roundOptions.map((round) => <option key={round.round_id} value={round.round_id}>Round {round.round_index} · {round.multiplier}x · {round.timestamp || 'time unknown'}</option>)}</select></label>
          {hasOlderReports && <button onClick={loadOlderReports} className="rounded border border-zinc-700 px-3 py-2 text-sm hover:bg-zinc-800">Load older reports</button>}
          {hasOlderRounds && <button onClick={loadOlderRounds} className="rounded border border-zinc-700 px-3 py-2 text-sm hover:bg-zinc-800">Load older rounds</button>}
        </div>
        {!reports.length && <p className="mt-3 text-sm text-zinc-500">No saved reports of this type yet. A 100-round report needs a complete group of 100 validated rounds.</p>}
        {(selected || pastRound) && <p className="mt-3 text-xs text-zinc-500">Rounds {selected?.first_round || pastRound?.first_round}–{selected?.last_round || pastRound?.last_round} · {safeCount(selected?.round_count || pastRound?.round_count)} rounds · {selected?.generated_at || pastRound?.generated_at} · {selected?.data_source || pastRound?.data_source}</p>}
      </Panel>

      <details className="rounded-xl border border-zinc-800 bg-zinc-900/60 p-5">
        <summary className="cursor-pointer text-base font-semibold">More detail (streaks, transitions and data quality)</summary>
        <p className="mt-2 text-sm text-zinc-400">These statistics summarize past rounds. A pattern does not make any next result certain.</p>
        <div className="mt-4 grid gap-4 lg:grid-cols-2">
          <section><h3 className="mb-2 font-medium">Streak lengths</h3><div className="grid grid-cols-2 gap-2 text-sm">{['<2x', '>=2x'].map((side) => <div key={side} className="rounded bg-zinc-950 p-3"><div className="text-zinc-400">{side}</div><div className="mt-1">Longest: {analysis?.streaks?.longest?.[side] ?? 'UNKNOWN'}</div><div className="text-zinc-500">Average / median: {analysis?.streaks?.average_length?.[side] == null ? 'UNKNOWN' : `${analysis.streaks.average_length[side].toFixed(1)} / ${analysis.streaks.median_length[side].toFixed(1)}`}</div><div className="mt-2 text-xs text-zinc-500">{Object.entries(analysis?.streaks?.length_distribution?.[side] || {}).map(([length, count]) => `${length}: ${count}`).join(' · ') || 'No rounds'}</div></div>)}</div></section>
          <section><h3 className="mb-2 font-medium">After a short sequence</h3><div className="max-h-56 overflow-auto"><table className="w-full text-left text-xs"><thead className="text-zinc-500"><tr><th className="py-2">Previous results</th><th>Samples</th><th>Next result ≥2x</th><th>95% range</th><th>Compared with baseline</th></tr></thead><tbody>{Object.entries(analysis?.transitions?.conditional_sequences || {}).filter(([sequence]) => sequence.length <= 4).slice(0, 30).map(([sequence, item]) => <tr key={sequence} className="border-t border-zinc-800"><td className="py-2 font-mono">{sequence}</td><td>{safeCount(item.count)}</td><td>{pct(item.percentage)}</td><td>{item.confidence_95?.lower == null ? 'UNKNOWN' : `${pct(item.confidence_95.lower * 100)}–${pct(item.confidence_95.upper * 100)}`}</td><td>{item.difference_from_baseline == null ? 'UNKNOWN' : `${item.difference_from_baseline >= 0 ? '+' : ''}${(item.difference_from_baseline * 100).toFixed(1)} points`}</td></tr>)}</tbody></table></div></section>
          <section><h3 className="mb-2 font-medium">Recent 2x+ rate</h3><div className="space-y-1 text-sm">{Object.entries(snapshot.historical?.windows || {}).map(([window, item]) => <div key={window} className="flex justify-between rounded bg-zinc-950 px-3 py-2"><span>Last {window} rounds</span><span>{item.at_least_2x?.rate == null ? 'UNKNOWN' : pct(item.at_least_2x.rate * 100)} · {safeCount(item.round_count)} rounds</span></div>)}</div></section>
          <section><h3 className="mb-2 font-medium">Other report details</h3><div className="rounded bg-zinc-950 p-3 text-sm"><div>Change vs earlier rounds: {analysis?.drift?.status || 'Shown in a saved report'}</div><div className="mt-1">Pattern stability across 1,000-round blocks: {analysis?.sequence_pattern_persistence?.sequences?.L?.status || 'Shown in a saved report'}</div><div className="mt-1">Time-of-day coverage: {analysis?.time_of_day_status || 'UNKNOWN'}</div><div className="mt-1">90th / 95th percentile: {analysis?.multiplier?.quantiles?.p90 == null ? 'Shown in a saved report' : `${analysis.multiplier.quantiles.p90.toFixed(2)}x / ${analysis.multiplier.quantiles.p95.toFixed(2)}x`}</div></div></section>
          <section><h3 className="mb-2 font-medium">Data notes</h3><div className="rounded bg-zinc-950 p-3 text-sm"><div>Source: {selected?.data_source || pastRound?.data_source || snapshot.data_source || 'UNKNOWN'}</div><div className="mt-1">Duplicate records excluded: {safeCount(analysis?.data_quality?.duplicates_excluded ?? 0)}</div><div className="mt-1">Timestamp analysis: {analysis?.time_of_day_status || 'UNKNOWN'}</div>{analysis?.warnings?.map((warning) => <div key={warning} className="mt-2 text-amber-300">{warning}</div>)}</div></section>
        </div>
      </details>
    </>}
  </div>;
}
