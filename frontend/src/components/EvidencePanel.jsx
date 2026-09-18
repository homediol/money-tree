import Card from './Card.jsx';
import Metric from './Metric.jsx';
import { pct } from '../utils/format.js';

export default function EvidencePanel({ evidence }) {
  if (!evidence) return <Card title="Prediction Evidence"><p className="text-sm text-zinc-500">No immutable evidence snapshot is available. The model may be untrained, stale, or below its deployment baseline.</p></Card>;
  const pattern = evidence.pattern || {};
  const ci = pattern.confidence_interval || {};
  const baseline = evidence.baselines?.historical || {};
  const positives = (evidence.explanations || []).filter((item) => item.type === 'supporting');
  const negatives = (evidence.explanations || []).filter((item) => item.type !== 'supporting');
  return (
    <Card title="Prediction Evidence">
      <div className="grid gap-4 md:grid-cols-3">
        <Metric label="Next ≥2x model estimate" value={pct(evidence.ml_probability)} />
        <Metric label="Confidence" value={`${evidence.confidence} (${evidence.confidence_score})`} />
        <Metric label="Evidence Strength" value={evidence.evidence_strength} />
      </div>
      <div className="mt-5 grid gap-4 md:grid-cols-2 xl:grid-cols-4">
        <Metric label="Pattern Rate / N" value={`${pct(pattern.success_rate)} / ${pattern.sample_size ?? 0}`} />
        <Metric label="Wilson 95% CI" value={`${pct(ci.lower)} – ${pct(ci.upper)}`} />
        <Metric label="Historical Baseline" value={pct(baseline.rate)} />
        <Metric label="Stability / Agreement" value={`${evidence.stability} / ${evidence.agreement}`} />
      </div>
      <div className="mt-4 grid gap-4 md:grid-cols-2">
        <div className="rounded border border-emerald-900/60 p-3"><div className="text-sm font-semibold text-emerald-300">Supporting factors</div>{positives.map((x, i) => <p key={`${x.evidence_ref}-${i}`} className="mt-2 text-sm text-zinc-400">{x.text}</p>)}</div>
        <div className="rounded border border-amber-900/60 p-3"><div className="text-sm font-semibold text-amber-300">Confidence reducers</div>{negatives.map((x, i) => <p key={`${x.evidence_ref}-${i}`} className="mt-2 text-sm text-zinc-400">{x.text}</p>)}</div>
      </div>
      <details className="mt-4 rounded border border-zinc-800 p-3"><summary className="cursor-pointer text-sm font-medium">Why?</summary>{(evidence.explanations || []).map((x, i) => <p key={i} className="mt-2 text-sm text-zinc-400">{x.text} <span className="text-xs text-zinc-600">[{x.evidence_ref}]</span></p>)}</details>
      <p className="mt-4 text-xs text-zinc-600">Snapshot {evidence.evidence_id} · source round {evidence.source_round_id} · immutable</p>
    </Card>
  );
}
