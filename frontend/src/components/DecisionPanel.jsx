import Card from './Card.jsx';
import Metric from './Metric.jsx';
import { pct } from '../utils/format.js';

export default function DecisionPanel({ decision }) {
  if (!decision) return <Card title="Decision Engine"><p className="text-sm text-zinc-500">Waiting for a prediction and immutable evidence snapshot.</p></Card>;
  return <Card title="Decision Engine">
    <div className="grid gap-4 md:grid-cols-4">
      <Metric label="Decision" value={decision.status} />
      <Metric label="Model estimate" value={pct(decision.probability)} />
      <Metric label="Evidence" value={`${decision.confidence} / ${decision.evidence_strength}`} />
      <Metric label="Risk / Execution" value={`${decision.risk_status} / ${decision.execution_status}`} />
    </div>
    <div className="mt-4 grid gap-4 md:grid-cols-2">
      <div className="rounded border border-zinc-800 p-3"><div className="text-sm font-semibold">Qualification reasons</div>{(decision.reasons || []).map((x) => <p key={x} className="mt-2 text-sm text-emerald-300">{x}</p>)}{!decision.reasons?.length && <p className="mt-2 text-sm text-zinc-500">No qualifying reasons.</p>}</div>
      <div className="rounded border border-zinc-800 p-3"><div className="text-sm font-semibold">Block reasons</div>{(decision.block_reasons || []).map((x) => <p key={x} className="mt-2 text-sm text-amber-300">{x}</p>)}{!decision.block_reasons?.length && <p className="mt-2 text-sm text-zinc-500">No blocking reasons.</p>}</div>
    </div>
    <p className="mt-4 text-xs text-zinc-600">Source {decision.source_round_id} → target {decision.target_round_id} · profile {decision.profile} · expires {decision.expires_at}</p>
  </Card>;
}
