import Card from './Card.jsx';
import Metric from './Metric.jsx';
import { pct } from '../utils/format.js';

export default function EvidencePanel({ analysis }) {
  if (!analysis) return null;
  const evidence = analysis.historical_evidence || {};
  const seq = analysis.sequence_similarity || {};
  const ml = analysis.ml_estimate || {};
  return (
    <Card title="Why This Signal?">
      <div className="grid gap-4 md:grid-cols-3">
        <Metric label="Historical Matches" value={evidence.matches ?? 0} />
        <Metric label="Pattern Rate" value={pct(evidence.historical_rate)} />
        <Metric label="Similar Cases" value={seq.similar_cases ?? 0} />
      </div>
      <div className="mt-5 grid gap-4 md:grid-cols-2">
        <div className="rounded border border-zinc-800 p-3">
          <div className="text-sm font-medium text-zinc-200">Current Pattern</div>
          <p className="mt-2 text-sm text-zinc-400">{analysis.current_pattern?.label}</p>
          <p className="mt-2 text-sm text-zinc-500">Recent multipliers: {(analysis.recent_multipliers || []).join(' -> ')}</p>
        </div>
        <div className="rounded border border-zinc-800 p-3">
          <div className="text-sm font-medium text-zinc-200">Model Evidence</div>
          <p className="mt-2 text-sm text-zinc-400">ML status: {ml.status}</p>
          <p className="mt-2 text-sm text-zinc-500">ML probability: {pct(ml.probability)}</p>
        </div>
      </div>
      <p className="mt-4 text-sm text-zinc-500">This is statistical evidence only. It does not guarantee the next outcome.</p>
    </Card>
  );
}

