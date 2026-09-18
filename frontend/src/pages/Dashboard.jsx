import { Area, AreaChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts';
import { Activity, Database, Gauge, Radio, Target } from 'lucide-react';
import Card from '../components/Card.jsx';
import EvidencePanel from '../components/EvidencePanel.jsx';
import Metric from '../components/Metric.jsx';
import StatusPill from '../components/StatusPill.jsx';
import { useLiveData } from '../hooks/useLiveData.js';
import { mult, pct } from '../utils/format.js';
import PageHeader from '../components/PageHeader.jsx';
import { ErrorState, LoadingState } from '../components/PageState.jsx';
import DataFreshness from '../components/DataFreshness.jsx';
import DecisionPanel from '../components/DecisionPanel.jsx';

export default function Dashboard() {
  const { loading, error, signal, stats, history, system, dataStatus, latestFeatures, evidence, decision } = useLiveData();
  const analysis = signal?.analysis;
  const quality = dataStatus?.quality || {};
  const dataset = dataStatus?.dataset || {};
  const features = latestFeatures?.features || {};
  const chart = (history?.rounds || []).slice(-80).map((r) => ({ round: r.round_index, multiplier: r.multiplier, target: r.target }));

  if (loading) return <LoadingState label="Loading statistical analysis…" />;
  if (error) return <ErrorState message={error} />;

  return (
    <div className="space-y-5">
      <PageHeader eyebrow="Operations overview" title="Dashboard" icon={Activity}
        description="Statistical context from the currently stored round dataset. Signals are analytical observations—not guaranteed outcomes."
        badge={<StatusPill status={analysis?.status} />}
        action={<DataFreshness timestamp={system?.dataset?.last_timestamp} records={system?.dataset?.valid_records} />} />

      <section className="grid gap-4 md:grid-cols-4">
        <Card><Target size={17} className="mb-4 text-emerald-400" /><Metric label="Observed Rate" value={pct(analysis?.final_probability)} tone="good" /></Card>
        <Card><Gauge size={17} className="mb-4 text-sky-400" /><Metric label="Evidence Confidence" value={analysis?.confidence || 'N/A'} /></Card>
        <Card><Database size={17} className="mb-4 text-violet-400" /><Metric label="Valid Rounds" value={stats?.data_quality?.valid_records ?? 0} /></Card>
        <Card><Radio size={17} className="mb-4 text-amber-400" /><Metric label="Latest Multiplier" value={mult(stats?.statistics?.latest_multiplier)} /></Card>
      </section>

      <Card title="Data / Features">
        <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
          <Metric label="Total / Valid Rounds" value={`${quality.total_rounds ?? 0} / ${quality.valid_rounds ?? 0}`} />
          <Metric label="Historical 2x+ Rate" value={pct(quality.rate_2x_plus)} />
          <Metric label="Current Low Streak" value={features.streak_below_1_5 ?? 0} />
          <Metric label="Feature Status" value={`${dataset.status || 'WAITING'} · ${dataset.feature_rows ?? 0} rows`} />
        </div>
        <div className="mt-4 rounded border border-zinc-800 bg-zinc-950 p-3 text-sm text-zinc-400">
          <div>Date range: {quality.date_range?.start || 'N/A'} → {quality.date_range?.end || 'N/A'}</div>
          <div className="mt-2">Latest sequence: {(features.sequence_last_10 || '').replaceAll('|', ' · ') || 'Insufficient history'}</div>
          <div className="mt-2">Recent features: last 1 = {mult(features.last_1)}, mean 10 = {mult(features.mean_last_10)}, std 10 = {features.std_last_10?.toFixed?.(3) || 'N/A'}, 2x count 10 = {features.count_2x_last_10 ?? 'N/A'}</div>
        </div>
      </Card>

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
        <Card title="Evidence snapshot">
          <div className="space-y-4">
            <p className="text-lg font-semibold text-zinc-100">{analysis?.current_pattern?.label || 'No active pattern'}</p>
            <Metric label="Historical Matches" value={analysis?.historical_evidence?.matches ?? 0} />
            <Metric label="Historical Rate" value={pct(analysis?.historical_evidence?.historical_rate)} />
            <p className="text-sm text-zinc-500">Target: {analysis?.target}. No output is guaranteed.</p>
          </div>
        </Card>
      </div>

      <EvidencePanel evidence={evidence} />
      <DecisionPanel decision={decision} />
    </div>
  );
}
