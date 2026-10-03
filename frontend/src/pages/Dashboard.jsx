import { Area, AreaChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts';
import { Activity, Database, Gauge, Radio, RefreshCw, Target, TrendingUp } from 'lucide-react';
import Card from '../components/Card.jsx';
import EvidencePanel from '../components/EvidencePanel.jsx';
import Metric from '../components/Metric.jsx';
import StatusPill from '../components/StatusPill.jsx';
import { useLiveData } from '../hooks/useLiveData.js';
import { mult, pct } from '../utils/format.js';
import PageHeader from '../components/PageHeader.jsx';
import { LoadingState } from '../components/PageState.jsx';
import DataFreshness from '../components/DataFreshness.jsx';
import DecisionPanel from '../components/DecisionPanel.jsx';

const numberOrUnknown = (value) => value == null || !Number.isFinite(Number(value)) ? 'Unknown' : Number(value).toLocaleString();

function SummaryCard({ icon: Icon, label, value, detail, tint = 'emerald' }) {
  const palette = {
    emerald: 'border-emerald-500/20 bg-emerald-500/5 text-emerald-300',
    sky: 'border-sky-500/20 bg-sky-500/5 text-sky-300',
    violet: 'border-violet-500/20 bg-violet-500/5 text-violet-300',
    amber: 'border-amber-500/20 bg-amber-500/5 text-amber-300',
  };
  return <Card><div className={`mb-3 grid h-9 w-9 place-items-center rounded-lg border ${palette[tint]}`}><Icon size={18} /></div>
    <div className="text-xs font-semibold uppercase tracking-wide text-zinc-500">{label}</div>
    <div className="mt-1 break-words text-2xl font-bold text-zinc-100">{value}</div>
    {detail && <p className="mt-1 text-xs leading-5 text-zinc-500">{detail}</p>}
  </Card>;
}

export default function Dashboard() {
  const { loading, error, refresh, signal, stats, history, system, dataStatus, latestFeatures, evidence, decision } = useLiveData();
  const analysis = signal?.analysis;
  const quality = dataStatus?.quality || {};
  const dataset = dataStatus?.dataset || {};
  const features = latestFeatures?.features || {};
  const chart = (history?.rounds || []).slice(-80).map((r) => ({ round: r.round_index, multiplier: r.multiplier, target: r.target }));
  const hasSnapshot = Boolean(signal || stats || history || system || dataStatus || evidence || decision);

  if (loading && !hasSnapshot) return <LoadingState label="Loading saved round analysis…" />;

  return (
    <div className="space-y-5">
      <PageHeader eyebrow="Stored round analysis" title="Dashboard" icon={Activity}
        description="A summary of recorded Aviator outcomes and current analysis. Historical patterns describe past rounds; they do not guarantee future outcomes."
        badge={<StatusPill status={analysis?.status} />}
        action={<div className="flex flex-col items-end gap-2"><DataFreshness timestamp={system?.dataset?.last_timestamp} records={system?.dataset?.valid_records} />
          <button onClick={refresh} disabled={loading} className="inline-flex items-center gap-2 rounded-lg border border-zinc-700 px-3 py-2 text-xs text-zinc-300 hover:bg-zinc-800 disabled:opacity-50"><RefreshCw size={14} className={loading ? 'animate-spin' : ''} />{loading ? 'Refreshing' : 'Refresh data'}</button></div>} />

      {error && <div role="status" className="rounded-lg border border-amber-500/30 bg-amber-500/10 p-3 text-sm text-amber-200">
        <strong>Showing the last received data.</strong> This snapshot may be stale while the backend reconnects. {error}
      </div>}

      {!hasSnapshot && !loading && <Card title="Analysis is not available yet"><p className="text-sm text-zinc-400">No usable analysis snapshot has been received. The page will stay open while you retry.</p>
        <button onClick={refresh} className="mt-3 rounded-lg border border-zinc-700 px-3 py-2 text-sm text-zinc-200 hover:bg-zinc-800">Try again</button></Card>}

      <section className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4" aria-label="Analysis summary">
        <SummaryCard icon={Target} label="Historical rounds at 2x or above" value={pct(analysis?.final_probability)} detail="Observed rate in the analysis sample; not a next-round guarantee." tint="emerald" />
        <SummaryCard icon={Gauge} label="Evidence confidence" value={analysis?.confidence || 'Unknown'} detail="Confidence label reported by the analysis service." tint="sky" />
        <SummaryCard icon={Database} label="Valid saved rounds" value={numberOrUnknown(stats?.data_quality?.valid_records ?? system?.dataset?.valid_records)} detail="Rounds included in the stored dataset." tint="violet" />
        <SummaryCard icon={Radio} label="Latest recorded multiplier" value={mult(stats?.statistics?.latest_multiplier)} detail="Most recent multiplier returned by the statistics service." tint="amber" />
      </section>

      <section className="grid gap-4 xl:grid-cols-[1.2fr_0.8fr]">
        <Card title="Stored round history">
          <p className="mb-3 text-sm text-zinc-400">Each point is a recorded round, shown in round order.</p>
          {chart.length ? <div className="chart-surface">
            <ResponsiveContainer>
              <AreaChart data={chart}>
                <XAxis dataKey="round" stroke="#71717a" tick={{ fontSize: 12 }} />
                <YAxis stroke="#71717a" tick={{ fontSize: 12 }} />
                <Tooltip contentStyle={{ background: '#18181b', border: '1px solid #3f3f46', borderRadius: 8 }} labelFormatter={(round) => `Round ${round}`} formatter={(value) => [`${Number(value).toFixed(2)}x`, 'Multiplier']} />
                <Area type="monotone" dataKey="multiplier" name="Multiplier" stroke="#34d399" fill="#34d39922" strokeWidth={2} />
              </AreaChart>
            </ResponsiveContainer>
          </div> : <p className="rounded-lg border border-dashed border-zinc-700 p-6 text-center text-sm text-zinc-500">No round history is available in this snapshot.</p>}
        </Card>

        <Card title="What the current analysis found">
          <div className="rounded-lg border border-zinc-800 bg-zinc-950/70 p-4">
            <div className="text-xs font-semibold uppercase tracking-wide text-zinc-500">Most relevant historical pattern</div>
            <p className="mt-2 text-lg font-semibold text-zinc-100">{analysis?.current_pattern?.label || 'No pattern reported'}</p>
            <p className="mt-1 text-sm text-zinc-400">This describes similar past observations only. It is not a promise about the next round.</p>
          </div>
          <div className="mt-4 grid gap-4 sm:grid-cols-2">
            <Metric label="Times seen in history" value={numberOrUnknown(analysis?.historical_evidence?.matches)} />
            <Metric label="2x+ rate after this pattern" value={pct(analysis?.historical_evidence?.historical_rate)} />
          </div>
          <p className="mt-4 text-xs text-zinc-500">Analysis target: {analysis?.target == null ? 'Unknown' : `${analysis.target}x`}</p>
        </Card>
      </section>

      <Card title="Round data coverage">
        <p className="mb-4 text-sm text-zinc-400">How much stored data is available and the date range it covers.</p>
        <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
          <Metric label="All recorded rounds" value={numberOrUnknown(quality.total_rounds)} />
          <Metric label="Valid rounds used" value={numberOrUnknown(quality.valid_rounds)} />
          <Metric label="Rounds reaching 2x+" value={pct(quality.rate_2x_plus)} />
          <Metric label="Dataset processing" value={dataset.status ? dataset.status.toLowerCase().replaceAll('_', ' ') : 'Unknown'} />
        </div>
        <div className="mt-4 grid gap-3 sm:grid-cols-2">
          <div className="rounded-lg border border-zinc-800 bg-zinc-950/70 p-3 text-sm"><div className="text-xs text-zinc-500">Stored date range</div><div className="mt-1 text-zinc-200">{quality.date_range?.start || 'Unknown'} <span className="text-zinc-500">to</span> {quality.date_range?.end || 'Unknown'}</div></div>
          <div className="rounded-lg border border-zinc-800 bg-zinc-950/70 p-3 text-sm"><div className="text-xs text-zinc-500">Recent sequence</div><div className="mt-1 font-mono text-zinc-200">{(features.sequence_last_10 || '').replaceAll('|', ' · ') || 'Not enough recent history'}</div></div>
        </div>
        <details className="mt-4 rounded-lg border border-zinc-800 bg-zinc-950/40 p-3">
          <summary className="cursor-pointer text-sm font-medium text-zinc-300">Recent calculation details</summary>
          <div className="mt-3 grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
            <Metric label="Last recorded round" value={mult(features.last_1)} />
            <Metric label="Average of last 10" value={mult(features.mean_last_10)} />
            <Metric label="Multiplier variation (last 10)" value={features.std_last_10 == null ? 'Unknown' : Number(features.std_last_10).toFixed(3)} />
            <Metric label="2x+ count in last 10" value={numberOrUnknown(features.count_2x_last_10)} />
            <Metric label="Rounds used for features" value={numberOrUnknown(dataset.feature_rows)} />
            <Metric label="Consecutive rounds below 1.50x" value={numberOrUnknown(features.streak_below_1_5)} />
          </div>
        </details>
      </Card>

      <EvidencePanel evidence={evidence} />
      <DecisionPanel decision={decision} />
    </div>
  );
}
