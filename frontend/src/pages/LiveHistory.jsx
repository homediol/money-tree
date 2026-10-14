import { useCallback, useEffect, useState } from 'react';
import { Clock3 } from 'lucide-react';
import Card from '../components/Card.jsx';
import Metric from '../components/Metric.jsx';
import PageHeader from '../components/PageHeader.jsx';
import { EmptyState, ErrorState, LoadingState } from '../components/PageState.jsx';
import DataFreshness from '../components/DataFreshness.jsx';
import { api } from '../services/api.js';

const mult = (value) => value == null || !Number.isFinite(Number(value)) ? '—' : `${Number(value).toFixed(2)}x`;

export default function LiveHistory() {
  const [snapshot, setSnapshot] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');

  const refresh = useCallback(async () => {
    try {
      const [historyResult, statusResult, statsResult, systemResult] = await Promise.allSettled([
        api.get('/api/history', { params: { limit: 250 } }),
        api.get('/api/history/status'),
        api.get('/api/history/stats'),
        api.get('/api/system/status'),
      ]);
      // The older history route can fail independently of the collector's
      // canonical recent-round route. Keep verified rounds visible while the
      // backend update is being rolled out.
      const history = historyResult.status === 'fulfilled'
        ? historyResult.value.data
        : (await api.get('/api/history/recent', { params: { limit: 250 } })).data;
      const rounds = history.rounds || [];
      const fallbackStatus = { status: 'UNKNOWN', count: history.count,
        latest: rounds.at(-1) || null, previous: rounds.at(-2) || null,
        last_update: rounds.at(-1)?.timestamp || null };
      setSnapshot({ history,
        status: statusResult.status === 'fulfilled' ? statusResult.value.data : fallbackStatus,
        stats: statsResult.status === 'fulfilled' ? statsResult.value.data : {},
        system: systemResult.status === 'fulfilled' ? systemResult.value.data : {} });
      setError([historyResult, statusResult, statsResult, systemResult]
        .some((result) => result.status === 'rejected')
        ? 'Some history services are unavailable.' : '');
    } catch {
      setError('Round history is unavailable.');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    refresh();
    const timer = window.setInterval(refresh, 15000);
    return () => window.clearInterval(timer);
  }, [refresh]);

  if (loading) return <LoadingState label="Loading round history…" />;
  if (error && !snapshot) return <ErrorState message={error} />;
  const rounds = snapshot?.history?.rounds || [];
  const status = snapshot?.status || {};
  const stats = snapshot?.stats || {};
  const system = snapshot?.system || {};

  return <div className="space-y-5">
    <PageHeader eyebrow="Live data" title="Round History" icon={Clock3}
      description="Real Aviator outcomes stored by the history collector."
      action={<DataFreshness timestamp={status.last_update || system.dataset?.last_timestamp} records={status.count} />} />
    {error && <div role="status" className="rounded border border-amber-500/30 bg-amber-500/10 p-3 text-sm text-amber-200">{error} Showing the last available history.</div>}
    <section className="grid gap-4 md:grid-cols-4">
      <Card><Metric label="Collector" value={status.status || 'UNKNOWN'} /></Card>
      <Card><Metric label="Latest" value={mult(status.latest?.multiplier)} /></Card>
      <Card><Metric label="Previous" value={mult(status.previous?.multiplier)} /></Card>
      <Card><Metric label="Round Count" value={status.count ?? 0} /></Card>
    </section>
    <section className="grid gap-4 md:grid-cols-4">
      <Card><Metric label="Minimum" value={mult(stats.min)} /></Card>
      <Card><Metric label="Maximum" value={mult(stats.max)} /></Card>
      <Card><Metric label="Mean" value={mult(stats.mean)} /></Card>
      <Card><Metric label="Median" value={mult(stats.median)} /></Card>
    </section>
    <Card title="Threshold Counts"><div className="grid grid-cols-2 gap-3 md:grid-cols-4">
      {Object.entries(stats.threshold_counts || {}).map(([label, value]) => <Metric key={label} label={label.replaceAll('_', ' ')} value={value} />)}
    </div></Card>
    <Card title="Latest Multipliers">
      {rounds.length === 0 ? <EmptyState title="No rounds recorded" description="The collector has not supplied round data yet." /> :
        <div className="grid grid-cols-2 gap-2 sm:grid-cols-4 md:grid-cols-8">
          {rounds.slice(-40).reverse().map((round) => <div key={round.round_id || round.round_index}
            className={`rounded border p-2 text-center ${round.multiplier >= 2 ? 'border-emerald-500/30 bg-emerald-500/10' : 'border-zinc-800 bg-zinc-950'}`}>
            <div className="text-sm font-semibold">{mult(round.multiplier)}</div><div className="text-xs text-zinc-500">#{round.round_index}</div>
          </div>)}
        </div>}
    </Card>
  </div>;
}
