import { Area, AreaChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts';
import Card from '../components/Card.jsx';
import EvidencePanel from '../components/EvidencePanel.jsx';
import Metric from '../components/Metric.jsx';
import StatusPill from '../components/StatusPill.jsx';
import { useLiveData } from '../hooks/useLiveData.js';
import { mult, pct } from '../utils/format.js';

export default function Dashboard() {
  const { loading, error, signal, stats, history } = useLiveData();
  const analysis = signal?.analysis;
  const chart = (history?.rounds || []).slice(-80).map((r) => ({ round: r.round_index, multiplier: r.multiplier, target: r.target }));

  if (loading) return <div className="text-zinc-400">Loading statistical analysis...</div>;
  if (error) return <div className="rounded border border-rose-500/30 bg-rose-500/10 p-4 text-rose-200">{error}</div>;

  return (
    <div className="space-y-5">
      <div className="flex flex-col justify-between gap-3 md:flex-row md:items-end">
        <div>
          <div className="text-xs font-semibold uppercase tracking-wide text-emerald-300">STATISTICAL PATTERN ANALYSIS</div>
          <h1 className="mt-1 text-3xl font-semibold tracking-normal">Current Signal</h1>
        </div>
        <StatusPill status={analysis?.status} />
      </div>

      <section className="grid gap-4 md:grid-cols-4">
        <Card><Metric label="Final Probability" value={pct(analysis?.final_probability)} tone="good" /></Card>
        <Card><Metric label="Confidence" value={analysis?.confidence || 'N/A'} /></Card>
        <Card><Metric label="Valid Rounds" value={stats?.data_quality?.valid_records ?? 0} /></Card>
        <Card><Metric label="Latest Round" value={mult(stats?.statistics?.latest_multiplier)} /></Card>
      </section>

      <div className="grid gap-5 xl:grid-cols-[1.4fr_1fr]">
        <Card title="Multiplier History">
          <div className="chart-surface">
            <ResponsiveContainer>
              <AreaChart data={chart}>
                <XAxis dataKey="round" stroke="#71717a" tick={{ fontSize: 12 }} />
                <YAxis stroke="#71717a" tick={{ fontSize: 12 }} />
                <Tooltip contentStyle={{ background: '#18181b', border: '1px solid #3f3f46', borderRadius: 4 }} />
                <Area type="monotone" dataKey="multiplier" stroke="#34d399" fill="#34d39922" strokeWidth={2} />
              </AreaChart>
            </ResponsiveContainer>
          </div>
        </Card>
        <Card title="Current Pattern">
          <div className="space-y-4">
            <p className="text-lg font-medium text-zinc-100">{analysis?.current_pattern?.label}</p>
            <Metric label="Historical Matches" value={analysis?.historical_evidence?.matches ?? 0} />
            <Metric label="Historical Rate" value={pct(analysis?.historical_evidence?.historical_rate)} />
            <p className="text-sm text-zinc-500">Target: {analysis?.target}. No output is guaranteed.</p>
          </div>
        </Card>
      </div>

      <EvidencePanel analysis={analysis} />
    </div>
  );
}

