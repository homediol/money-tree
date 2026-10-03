import Card from './Card.jsx';
import Metric from './Metric.jsx';
import { pct } from '../utils/format.js';

const shown = (value) => value == null || value === '' ? 'Unknown' : String(value).replaceAll('_', ' ').toLowerCase();
const count = (value) => value == null ? 'Unknown' : Number(value).toLocaleString();

export default function EvidencePanel({ evidence }) {
  if (!evidence) return <Card title="Analysis evidence"><p className="text-sm text-zinc-400">No verified evidence snapshot has arrived yet. Evidence can be unavailable while the model is untrained, stale or below its validation requirements.</p></Card>;
  const pattern = evidence.pattern || {};
  const ci = pattern.confidence_interval || {};
  const baseline = evidence.baselines?.historical || {};
  const positives = (evidence.explanations || []).filter((item) => item.type === 'supporting');
  const negatives = (evidence.explanations || []).filter((item) => item.type !== 'supporting');
  return <Card title="Why the analysis has this result">
    <p className="mb-4 text-sm text-zinc-400">Evidence summarizes information available from earlier stored rounds. It does not guarantee the next outcome.</p>
    <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
      <div className="rounded-lg border border-sky-500/20 bg-sky-500/5 p-3"><Metric label="Model estimate of reaching 2x+" value={pct(evidence.ml_probability)} /></div>
      <div className="rounded-lg border border-violet-500/20 bg-violet-500/5 p-3"><Metric label="Evidence confidence" value={evidence.confidence_score == null ? shown(evidence.confidence) : `${shown(evidence.confidence)} · ${evidence.confidence_score}`} /></div>
      <div className="rounded-lg border border-emerald-500/20 bg-emerald-500/5 p-3"><Metric label="Evidence strength" value={shown(evidence.evidence_strength)} /></div>
    </div>
    <div className="mt-3 grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
      <Metric label="Pattern's 2x+ rate" value={pct(pattern.success_rate)} />
      <Metric label="Rounds matching pattern" value={count(pattern.sample_size)} />
      <Metric label="95% uncertainty range" value={ci.lower == null || ci.upper == null ? 'Unknown' : `${pct(ci.lower)} to ${pct(ci.upper)}`} />
      <Metric label="Historical 2x+ rate" value={pct(baseline.rate)} />
    </div>
    <div className="mt-4 grid gap-3 lg:grid-cols-2">
      <div className="rounded-lg border border-emerald-500/20 bg-emerald-500/5 p-4"><div className="text-sm font-semibold text-emerald-300">Factors supporting the analysis</div>
        {positives.length ? positives.map((item, i) => <p key={`${item.evidence_ref}-${i}`} className="mt-2 text-sm leading-6 text-zinc-300">{item.text}</p>) : <p className="mt-2 text-sm text-zinc-500">No supporting factors were recorded.</p>}
      </div>
      <div className="rounded-lg border border-amber-500/20 bg-amber-500/5 p-4"><div className="text-sm font-semibold text-amber-300">Factors that lower confidence</div>
        {negatives.length ? negatives.map((item, i) => <p key={`${item.evidence_ref}-${i}`} className="mt-2 text-sm leading-6 text-zinc-300">{item.text}</p>) : <p className="mt-2 text-sm text-zinc-500">No confidence reducers were recorded.</p>}
      </div>
    </div>
    <details className="mt-4 rounded-lg border border-zinc-800 bg-zinc-950/40 p-3"><summary className="cursor-pointer text-sm font-medium text-zinc-300">Evidence notes and source</summary>
      {(evidence.explanations || []).length ? (evidence.explanations || []).map((item, i) => <p key={i} className="mt-2 text-sm leading-6 text-zinc-400">{item.text} {item.evidence_ref && <span className="text-xs text-zinc-600">(source: {item.evidence_ref})</span>}</p>) : <p className="mt-2 text-sm text-zinc-500">No written evidence notes were included in this snapshot.</p>}
      <p className="mt-3 break-all text-xs text-zinc-600">Snapshot ID: {evidence.evidence_id || 'Unknown'} · data through round: {evidence.source_round_id || 'Unknown'} · saved snapshot</p>
    </details>
  </Card>;
}
