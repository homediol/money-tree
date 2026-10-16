import { useCallback, useEffect, useState } from 'react';
import { Clock3 } from 'lucide-react';
import Card from '../components/Card.jsx';
import Metric from '../components/Metric.jsx';
import PageHeader from '../components/PageHeader.jsx';
import { EmptyState, ErrorState, LoadingState } from '../components/PageState.jsx';
import DataFreshness from '../components/DataFreshness.jsx';
import { api } from '../services/api.js';

const mult = value => value == null || !Number.isFinite(Number(value))
  ? '—' : `${Number(value).toFixed(2)}x`;

export default function LiveHistory() {
  const [snapshot, setSnapshot] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');

  const refresh = useCallback(async () => {
    try {
      const [historyResult, statusResult, statsResult] = await Promise.all([
        api.get('/api/history', { params: { limit: 250 } }),
        api.get('/api/history/status'),
        api.get('/api/history/stats'),
      ]);
      setSnapshot({ history: historyResult.data, status: statusResult.data, stats: statsResult.data });
      setError('');
    } catch {
      setError('The current process-local round buffer is unavailable.');
    } finally { setLoading(false); }
  }, []);

  useEffect(() => {
    refresh();
    const timer = window.setInterval(refresh, 5000);
    return () => window.clearInterval(timer);
  }, [refresh]);

  if (loading) return <LoadingState label="Loading recent rounds…" />;
  if (error && !snapshot) return <ErrorState message={error} />;
  const rounds = snapshot?.history?.rounds || [];
  const status = snapshot?.status || {};
  const stats = snapshot?.stats || {};

  return <div className="space-y-5">
    <PageHeader eyebrow="Live data" title="Recent Rounds" icon={Clock3}
      description="Recent Aviator observations held in the active process memory."
      action={<DataFreshness timestamp={status.last_update} records={status.count} />} />
    {error && <div role="status" className="rounded border border-amber-500/30 bg-amber-500/10 p-3 text-sm text-amber-200">{error}</div>}
    <Card title="Round history storage">
      <p className="text-sm text-zinc-300">New rounds are kept in a bounded in-memory buffer and reset on process restart. The old PostgreSQL and JSON history are retained, untouched, and are not loaded into current continuity. Cleanup is available only through the explicit operator command:</p>
      <code className="mt-3 block overflow-x-auto rounded bg-zinc-950 p-3 text-xs text-zinc-100">backend/.venv/bin/python backend/scripts/cleanup_round_history.py --help</code>
      <p className="mt-2 text-xs text-zinc-500">The help command only displays cleanup options. No records have been deleted.</p>
    </Card>
    <section className="grid gap-4 md:grid-cols-4">
      <Card><Metric label="Collector" value={status.status || 'UNKNOWN'} /></Card>
      <Card><Metric label="Latest" value={mult(status.latest?.multiplier)} /></Card>
      <Card><Metric label="Previous" value={mult(status.previous?.multiplier)} /></Card>
      <Card><Metric label="Buffered Rounds" value={`${status.count ?? 0}/${status.round_buffer?.capacity ?? '—'}`} /></Card>
    </section>
    <section className="grid gap-4 md:grid-cols-4">
      <Card><Metric label="Minimum" value={mult(stats.min)} /></Card>
      <Card><Metric label="Maximum" value={mult(stats.max)} /></Card>
      <Card><Metric label="Mean" value={mult(stats.mean)} /></Card>
      <Card><Metric label="Median" value={mult(stats.median)} /></Card>
    </section>
    <Card title="Latest multipliers">
      {rounds.length === 0 ? <EmptyState title="No rounds in this process" description="The rolling buffer starts empty after a restart." /> :
        <div className="grid grid-cols-2 gap-2 sm:grid-cols-4 md:grid-cols-8">
          {rounds.slice(-40).reverse().map(round => <div key={round.round_id || round.stream_sequence}
            className={`rounded border p-2 text-center ${Number(round.multiplier) >= 2
              ? 'border-emerald-500/30 bg-emerald-500/10' : 'border-zinc-800 bg-zinc-950'}`}>
            <div className="text-sm font-semibold">{mult(round.multiplier)}</div>
            <div className="text-xs text-zinc-500">#{round.round_index}</div>
          </div>)}
        </div>}
    </Card>
  </div>;
}
