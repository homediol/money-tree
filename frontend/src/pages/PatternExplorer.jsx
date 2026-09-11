import { Bar, BarChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts';
import Card from '../components/Card.jsx';
import { useLiveData } from '../hooks/useLiveData.js';
import { pct } from '../utils/format.js';

export default function PatternExplorer() {
  const { patterns, loading, error } = useLiveData();
  if (loading) return <div className="text-zinc-400">Discovering patterns...</div>;
  if (error) return <div className="text-rose-300">{error}</div>;
  const chart = (patterns || []).map((p) => ({ name: p.pattern.replace(' consecutive rounds ', ' low '), rate: p.probability || 0, occurrences: p.occurrences }));
  return (
    <div className="space-y-5">
      <h1 className="text-3xl font-semibold">Historical Pattern Explorer</h1>
      <Card title="Pattern Success Rates">
        <div className="chart-surface">
          <ResponsiveContainer>
            <BarChart data={chart}>
              <XAxis dataKey="name" hide />
              <YAxis stroke="#71717a" tickFormatter={(v) => `${Math.round(v * 100)}%`} />
              <Tooltip formatter={(v, name) => name === 'rate' ? pct(v) : v} contentStyle={{ background: '#18181b', border: '1px solid #3f3f46' }} />
              <Bar dataKey="rate" fill="#38bdf8" />
            </BarChart>
          </ResponsiveContainer>
        </div>
      </Card>
      <Card title="Discovered Patterns">
        <div className="overflow-x-auto">
          <table className="w-full min-w-[760px] text-left text-sm">
            <thead className="text-xs uppercase text-zinc-500">
              <tr><th className="py-2">Pattern</th><th>Occurrences</th><th>Next {'>=2x'}</th><th>Next {'<2x'}</th><th>Rate</th><th>Evidence</th></tr>
            </thead>
            <tbody>
              {(patterns || []).map((p) => (
                <tr key={p.pattern_id} className="border-t border-zinc-800">
                  <td className="py-3 text-zinc-200">{p.pattern}</td>
                  <td>{p.occurrences}</td>
                  <td className="text-emerald-300">{p.success_count}</td>
                  <td className="text-rose-300">{p.failure_count}</td>
                  <td>{pct(p.probability)}</td>
                  <td>{p.sufficient_data ? 'Sufficient' : 'Insufficient data'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Card>
    </div>
  );
}


