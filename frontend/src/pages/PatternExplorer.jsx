import { useMemo, useState } from 'react';
import { ScanSearch } from 'lucide-react';
import Card from '../components/Card.jsx';
import Metric from '../components/Metric.jsx';
import { useLiveData } from '../hooks/useLiveData.js';
import { pct } from '../utils/format.js';
import PageHeader from '../components/PageHeader.jsx';
import { EmptyState, ErrorState, LoadingState } from '../components/PageState.jsx';

function ci(value) {
  if (value?.lower == null) return 'N/A';
  return `${pct(value.lower)}–${pct(value.upper)}`;
}

function points(value) {
  return value == null ? 'N/A' : `${(value * 100).toFixed(2)} pp`;
}

export default function PatternExplorer() {
  const { patternReport, loading, error } = useLiveData();
  const [kind, setKind] = useState('all');
  const [period, setPeriod] = useState('all');
  const [threshold, setThreshold] = useState('all');
  const filtered = useMemo(() => (patternReport?.patterns || []).filter((item) => {
    if (kind !== 'all' && item.kind !== kind) return false;
    if (threshold !== 'all' && String(item.definition?.threshold) !== threshold) return false;
    return true;
  }).slice(0, 100), [patternReport, kind, threshold]);

  if (loading) return <LoadingState label="Loading historical pattern evidence…" />;
  if (error) return <ErrorState message={error} />;
  const baseline = patternReport?.baseline;
  const selected = (item) => period === 'all' ? item : item.recent?.[period] || {};

  return (
    <div className="space-y-5">
      <PageHeader eyebrow="Historical evidence" title="Pattern Analysis" icon={ScanSearch}
        description="Conditional historical relationships with uncertainty and chronological out-of-sample checks—not next-round predictions." />
      <section className="grid gap-4 md:grid-cols-4">
        <Card><Metric label="2x+ Baseline" value={pct(baseline?.rate)} /></Card>
        <Card><Metric label="Baseline N" value={baseline?.sample_size ?? 0} /></Card>
        <Card><Metric label="Baseline Wilson 95% CI" value={ci(baseline?.confidence_interval)} /></Card>
        <Card><Metric label="Minimum Pattern N" value={patternReport?.minimum_sample_size ?? 30} /></Card>
      </section>
      <Card title="Filters">
        <div className="grid gap-3 sm:grid-cols-3">
          <select value={kind} onChange={(event) => setKind(event.target.value)} className="rounded border border-zinc-700 bg-zinc-950 px-3 py-2">
            <option value="all">All pattern types</option><option value="streak">Streaks</option><option value="sequence">Sequences</option>
          </select>
          <select value={threshold} onChange={(event) => setThreshold(event.target.value)} className="rounded border border-zinc-700 bg-zinc-950 px-3 py-2">
            <option value="all">All streak thresholds</option><option value="1.2">Below 1.20x</option><option value="1.5">Below 1.50x</option><option value="2">Below 2.00x</option><option value="3">Below 3.00x</option>
          </select>
          <select value={period} onChange={(event) => setPeriod(event.target.value)} className="rounded border border-zinc-700 bg-zinc-950 px-3 py-2">
            <option value="all">All history</option><option value="1000">Recent 1,000</option><option value="250">Recent 250</option>
          </select>
        </div>
      </Card>
      <Card title="Ranked Evidence">
        {filtered.length === 0 ? <EmptyState title="No matching evidence" description="No observed pattern matches the selected filters." /> :
        <div className="overflow-x-auto">
          <table className="w-full min-w-[1100px] text-left text-sm">
            <thead className="text-xs uppercase text-zinc-500">
              <tr><th className="py-2">Pattern</th><th>N</th><th>Historical rate</th><th>Baseline diff.</th><th>95% CI</th><th>Recent rate</th><th>Out-of-sample</th><th>Stability</th></tr>
            </thead>
            <tbody>{filtered.map((item) => {
              const view = selected(item);
              return <tr key={item.pattern_id} className="border-t border-zinc-800">
                <td className="max-w-sm py-3 text-zinc-200"><div>{item.pattern}</div><div className="mt-1 text-xs text-zinc-500">{item.evidence_state.replaceAll('_', ' ')}</div></td>
                <td>{view.sample_size ?? item.sample_size}</td>
                <td>{pct(view.success_rate ?? item.success_rate)}</td>
                <td>{points(view.difference_from_baseline ?? item.difference_from_baseline)}</td>
                <td>{ci(view.confidence_interval ?? item.confidence_interval)}</td>
                <td>{pct(item.recent_rate)}</td>
                <td>{pct(item.out_of_sample_rate)} (N={item.test?.sample_size ?? 0})</td>
                <td>{item.stability}</td>
              </tr>;
            })}</tbody>
          </table>
        </div>}
      </Card>
    </div>
  );
}
