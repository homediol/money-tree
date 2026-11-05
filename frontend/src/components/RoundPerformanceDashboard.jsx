import { useCallback, useEffect, useState } from 'react';
import { Activity, Gauge, RefreshCw } from 'lucide-react';
import { api } from '../services/api.js';

const POLL_INTERVAL_MS = 15000;

function duration(value) {
  return value !== null && value !== undefined && value !== '' && Number.isFinite(Number(value))
    ? `${Number(value).toFixed(2)} ms` : '—';
}

function sampleTime(value) {
  if (!value) return '—';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? 'unknown' : date.toLocaleTimeString();
}

function statusStyle(status) {
  if (status === 'GREEN') return 'border-emerald-400/30 bg-emerald-400/10 text-emerald-200';
  if (status === 'WARNING') return 'border-amber-400/30 bg-amber-400/10 text-amber-200';
  if (status === 'CRITICAL') return 'border-rose-400/30 bg-rose-400/10 text-rose-200';
  return 'border-zinc-700 bg-zinc-900 text-zinc-300';
}

export default function RoundPerformanceDashboard() {
  const [report, setReport] = useState(null);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(true);

  const refresh = useCallback(async () => {
    try {
      const response = await api.get('/api/rules-engine/performance');
      setReport(response.data);
      setError('');
    } catch (failure) {
      setError(failure?.response?.data?.detail?.error || failure?.message || 'Performance metrics unavailable');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
    const timer = window.setInterval(() => { void refresh(); }, POLL_INTERVAL_MS);
    return () => window.clearInterval(timer);
  }, [refresh]);

  const status = report?.end_to_end_target_status || 'UNKNOWN';
  const stages = Object.entries(report?.stages || {});
  const latest = report?.total_latest_ms;

  return <section aria-label="Round performance dashboard" className="rounded-xl border border-zinc-800 bg-zinc-900/60 p-5">
    <div className="flex flex-wrap items-start justify-between gap-4">
      <div>
        <div className="flex items-center gap-2 text-xs font-bold uppercase tracking-[0.16em] text-cyan-300">
          <Activity size={15} /> Live latency audit
        </div>
        <h2 className="mt-2 text-lg font-semibold text-zinc-100">Round processing performance</h2>
        <p className="mt-1 text-sm text-zinc-400">
          Correlated by round ID. Percentiles use measured samples from the running backend and collector.
        </p>
      </div>
      <div className={`flex items-center gap-2 rounded-lg border px-3 py-2 text-sm font-semibold ${statusStyle(status)}`}>
        <Gauge size={16} /> 2-second detection → Signal API target: {status}
      </div>
    </div>

    <p className="mt-2 text-sm text-zinc-400">
      Backend receive → final decision: <strong className="text-zinc-200">{report?.backend_path_status || 'UNKNOWN'}</strong>
      {' · '}latest sample {duration(latest)}
    </p>

    {error && <div role="status" className="mt-4 rounded-lg border border-amber-500/30 bg-amber-500/10 px-3 py-2 text-sm text-amber-200">{error}</div>}

    <div className="mt-4 flex flex-wrap gap-x-6 gap-y-2 text-sm text-zinc-300">
      <span>Slowest stage: <strong className="text-zinc-100">{report?.slowest_stage || 'No samples'}</strong></span>
      <span>Rule queue: <strong className="text-zinc-100">{report?.queue?.depth ?? '—'}/{report?.queue?.capacity ?? '—'}</strong></span>
      <span>Worker: <strong className="text-zinc-100">{report?.queue?.busy ? 'busy' : 'idle'}</strong></span>
      <span>Dashboard updates: <strong className="text-zinc-100">{report?.dashboard_notifications?.depth ?? '—'}/{report?.dashboard_notifications?.capacity ?? '—'}</strong>
        {' · '}coalesced {report?.dashboard_notifications?.coalesced ?? 0}</span>
      <span>Backend round: <code className="text-cyan-200">{report?.total_latest_correlation_id || '—'}</code></span>
      <span>Signal acceptance sample: <code className="text-cyan-200">{report?.end_to_end_latest_correlation_id || '—'}</code>
        {' · '}{sampleTime(report?.end_to_end_latest_sampled_at)}</span>
      <span>Collector: <strong className={report?.sample_freshness?.collector_current ? 'text-emerald-300' : 'text-amber-300'}>
        {report?.sample_freshness?.collector_current ? 'current' : 'stale'}
      </strong> · last round {sampleTime(report?.sample_freshness?.collector_last_round_at)}</span>
      <span className="inline-flex items-center gap-1 text-zinc-500">
        <RefreshCw size={12} /> Backend sample {sampleTime(report?.sample_freshness?.backend_latest_sample_at)}
        · report {report?.updated_at ? sampleTime(report.updated_at) : loading ? 'loading' : 'no sample'}
      </span>
    </div>

    <div className="mt-4 overflow-x-auto rounded-lg border border-zinc-800">
      <table className="w-full min-w-[740px] border-collapse text-left text-xs">
        <thead className="bg-zinc-950/70 text-zinc-400">
          <tr>
            <th className="px-3 py-2 font-medium">Stage</th>
            <th className="px-3 py-2 font-medium">Average</th>
            <th className="px-3 py-2 font-medium">P50</th>
            <th className="px-3 py-2 font-medium">P95</th>
            <th className="px-3 py-2 font-medium">P99</th>
            <th className="px-3 py-2 font-medium">Maximum</th>
            <th className="px-3 py-2 font-medium">Samples</th>
          </tr>
        </thead>
        <tbody>
          {stages.map(([name, sample]) => <tr key={name} className="border-t border-zinc-800 text-zinc-200">
            <td className="px-3 py-2">{name}</td>
            <td className="px-3 py-2">{duration(sample?.average_ms)}</td>
            <td className="px-3 py-2">{duration(sample?.p50_ms)}</td>
            <td className="px-3 py-2">{duration(sample?.p95_ms)}</td>
            <td className="px-3 py-2">{duration(sample?.p99_ms)}</td>
            <td className="px-3 py-2">{duration(sample?.max_ms)}</td>
            <td className="px-3 py-2">{sample?.samples ?? 0}</td>
          </tr>)}
          <tr className="border-t border-zinc-700 bg-zinc-950/50 font-semibold text-zinc-100">
            <td className="px-3 py-2">TOTAL · backend receive to final decision</td>
            <td className="px-3 py-2">{duration(report?.backend_latency_ms?.backend_receive_to_final_decision_ms?.average_ms)}</td>
            <td className="px-3 py-2">{duration(report?.backend_latency_ms?.backend_receive_to_final_decision_ms?.p50_ms)}</td>
            <td className="px-3 py-2">{duration(report?.backend_latency_ms?.backend_receive_to_final_decision_ms?.p95_ms)}</td>
            <td className="px-3 py-2">{duration(report?.backend_latency_ms?.backend_receive_to_final_decision_ms?.p99_ms)}</td>
            <td className="px-3 py-2">{duration(report?.backend_latency_ms?.backend_receive_to_final_decision_ms?.max_ms)}</td>
            <td className="px-3 py-2">{report?.backend_latency_ms?.backend_receive_to_final_decision_ms?.samples ?? 0}</td>
          </tr>
          {!stages.length && <tr><td colSpan="7" className="px-3 py-6 text-center text-zinc-500">{loading ? 'Loading measured samples…' : 'No measured round samples yet.'}</td></tr>}
        </tbody>
      </table>
    </div>

    <p className="mt-3 text-xs text-zinc-500">
      End-to-end uses the collector’s page-observation timestamp through durable Signal API acceptance on the local loopback path. Backend-only spans remain separate; the provider’s original result timestamp and downstream consumer acknowledgment are not exposed.
    </p>
  </section>;
}
