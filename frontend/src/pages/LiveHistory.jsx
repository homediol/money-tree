import { Clock3 } from 'lucide-react';
import Card from '../components/Card.jsx';
import Metric from '../components/Metric.jsx';
import { useLiveData } from '../hooks/useLiveData.js';
import { mult } from '../utils/format.js';
import PageHeader from '../components/PageHeader.jsx';
import { EmptyState, ErrorState, LoadingState } from '../components/PageState.jsx';
import DataFreshness from '../components/DataFreshness.jsx';

export default function LiveHistory() {
  const { history, historyStatus, historyStats, system, loading, error } = useLiveData();
  if (loading) return <LoadingState label="Loading round history…" />;
  if (error) return <ErrorState message={error} />;
  const rounds = history?.rounds || [];
  const latest = rounds.slice(-40).reverse();
  const latestRound = historyStatus?.latest;
  const previousRound = historyStatus?.previous;
  return (
    <div className="space-y-5">
      <PageHeader eyebrow="Live data" title="Round History" icon={Clock3}
        description="Live, deduplicated rounds observed by the independent history collector."
        action={<DataFreshness timestamp={historyStatus?.last_update || system?.dataset?.last_timestamp} records={historyStatus?.count} />} />
      <section className="grid gap-4 md:grid-cols-4">
        <Card><Metric label="Collector" value={historyStatus?.status || 'WAITING'} /></Card>
        <Card><Metric label="Latest" value={mult(latestRound?.multiplier)} /></Card>
        <Card><Metric label="Previous" value={mult(previousRound?.multiplier)} /></Card>
        <Card><Metric label="Round Count" value={historyStatus?.count ?? 0} /></Card>
      </section>
      <section className="grid gap-4 md:grid-cols-4">
        <Card><Metric label="Minimum" value={mult(historyStats?.min)} /></Card>
        <Card><Metric label="Maximum" value={mult(historyStats?.max)} /></Card>
        <Card><Metric label="Mean" value={mult(historyStats?.mean)} /></Card>
        <Card><Metric label="Median" value={mult(historyStats?.median)} /></Card>
      </section>
      <Card title="Threshold Counts">
        <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
          {Object.entries(historyStats?.threshold_counts || {}).map(([label, value]) => (
            <Metric key={label} label={label.replaceAll('_', ' ')} value={value} />
          ))}
        </div>
      </Card>
      <Card title="Latest Multipliers">
        {latest.length === 0 ? <EmptyState title="No rounds recorded" description="The collector has not supplied round data yet." /> : <div className="grid grid-cols-2 gap-2 sm:grid-cols-4 md:grid-cols-8">
          {latest.map((r) => <div key={r.round_id || r.round_index} className={`rounded border p-2 text-center ${r.multiplier >= 2 ? 'border-emerald-500/30 bg-emerald-500/10' : 'border-zinc-800 bg-zinc-950'}`}>
            <div className="text-sm font-semibold">{mult(r.multiplier)}</div>
            <div className="text-xs text-zinc-500">#{r.round_index}</div>
          </div>)}
        </div>}
      </Card>
    </div>
  );
}
