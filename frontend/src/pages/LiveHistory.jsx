import { Bar, BarChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts';
import Card from '../components/Card.jsx';
import Metric from '../components/Metric.jsx';
import { useLiveData } from '../hooks/useLiveData.js';
import { mult, pct } from '../utils/format.js';

export default function LiveHistory() {
  const { history, stats, loading, error } = useLiveData();
  if (loading) return <div className="text-zinc-400">Loading history...</div>;
  if (error) return <div className="text-rose-300">{error}</div>;
  const rounds = history?.rounds || [];
  const latest = rounds.slice(-40).reverse();
  const buckets = [
    { bucket: '<1.2x', value: stats?.statistics?.pct_below_1_2 || 0 },
    { bucket: '<1.5x', value: stats?.statistics?.pct_below_1_5 || 0 },
    { bucket: '>=2x', value: stats?.statistics?.base_rate || 0 },
    { bucket: '2-5x', value: stats?.statistics?.pct_between_2_5 || 0 },
    { bucket: '>=5x', value: stats?.statistics?.pct_above_5 || 0 },
  ];
  return (
    <div className="space-y-5">
      <h1 className="text-3xl font-semibold">Live Round History</h1>
      <section className="grid gap-4 md:grid-cols-4">
        <Card><Metric label="2x Rate" value={pct(stats?.statistics?.base_rate)} /></Card>
        <Card><Metric label="Mean" value={mult(stats?.statistics?.mean)} /></Card>
        <Card><Metric label="Max" value={mult(stats?.statistics?.max)} /></Card>
        <Card><Metric label="Std Dev" value={stats?.statistics?.std?.toFixed?.(2) || 'N/A'} /></Card>
      </section>
      <Card title="Recent Distribution">
        <div className="chart-surface">
          <ResponsiveContainer>
            <BarChart data={buckets}>
              <CartesianGrid stroke="#27272a" />
              <XAxis dataKey="bucket" stroke="#71717a" />
              <YAxis stroke="#71717a" tickFormatter={(v) => `${Math.round(v * 100)}%`} />
              <Tooltip formatter={(v) => pct(v)} contentStyle={{ background: '#18181b', border: '1px solid #3f3f46' }} />
              <Bar dataKey="value" fill="#22c55e" />
            </BarChart>
          </ResponsiveContainer>
        </div>
      </Card>
      <Card title="Latest Multipliers">
        <div className="grid grid-cols-2 gap-2 sm:grid-cols-4 md:grid-cols-8">
          {latest.map((r) => <div key={r.round_index} className={`rounded border p-2 text-center ${r.multiplier >= 2 ? 'border-emerald-500/30 bg-emerald-500/10' : 'border-zinc-800 bg-zinc-950'}`}>
            <div className="text-sm font-semibold">{mult(r.multiplier)}</div>
            <div className="text-xs text-zinc-500">#{r.round_index}</div>
          </div>)}
        </div>
      </Card>
    </div>
  );
}

