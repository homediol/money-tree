import Card from './Card.jsx';
import Metric from './Metric.jsx';
import { pct } from '../utils/format.js';

const human = (value) => value == null || value === '' ? 'Unknown' : String(value).replaceAll('_', ' ').toLowerCase();
const tone = (value) => ['READY_FOR_EXECUTION', 'APPROVED', 'ALLOWED'].includes(String(value || '').toUpperCase())
  ? 'border-emerald-500/20 bg-emerald-500/5 text-emerald-300'
  : ['WAITING', 'PENDING', 'NONE', 'NOT_EVALUATED'].includes(String(value || '').toUpperCase())
    ? 'border-amber-500/20 bg-amber-500/5 text-amber-300' : 'border-zinc-700 bg-zinc-950/50 text-zinc-200';

export default function DecisionPanel({ decision }) {
  if (!decision) return <Card title="Decision and safety checks"><p className="text-sm text-zinc-400">No current decision snapshot is available yet. No bet is authorized from this empty state.</p></Card>;
  return <Card title="Decision and safety checks">
    <p className="mb-4 text-sm text-zinc-400">This section reports whether the current analysis passed the decision and risk checks. A positive analysis estimate alone does not authorize a bet.</p>
    <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
      <div className={`rounded-lg border p-3 ${tone(decision.status)}`}><Metric label="Decision result" value={human(decision.status)} /></div>
      <Metric label="Analysis estimate for 2x+" value={pct(decision.probability)} />
      <Metric label="Evidence confidence" value={`${human(decision.confidence)} · ${human(decision.evidence_strength)}`} />
      <div className="rounded-lg border border-zinc-800 bg-zinc-950/50 p-3"><div className="text-[11px] font-semibold uppercase tracking-[0.12em] text-zinc-500">Risk check</div><div className="mt-2 text-sm font-semibold capitalize text-zinc-200">{human(decision.risk_status)}</div><div className="mt-2 text-[11px] font-semibold uppercase tracking-[0.12em] text-zinc-500">Execution</div><div className="mt-1 text-sm font-semibold capitalize text-zinc-200">{human(decision.execution_status)}</div></div>
    </div>
    <div className="mt-4 grid gap-3 lg:grid-cols-2">
      <div className="rounded-lg border border-emerald-500/20 bg-emerald-500/5 p-4"><div className="text-sm font-semibold text-emerald-300">Checks that passed</div>
        {decision.reasons?.length ? decision.reasons.map((reason) => <p key={reason} className="mt-2 text-sm capitalize leading-6 text-zinc-300">{human(reason)}</p>) : <p className="mt-2 text-sm text-zinc-500">No passing checks were reported.</p>}
      </div>
      <div className="rounded-lg border border-amber-500/20 bg-amber-500/5 p-4"><div className="text-sm font-semibold text-amber-300">Why a bet may be blocked</div>
        {decision.block_reasons?.length ? decision.block_reasons.map((reason) => <p key={reason} className="mt-2 text-sm capitalize leading-6 text-zinc-300">{human(reason)}</p>) : <p className="mt-2 text-sm text-zinc-500">No blocking reason was reported in this snapshot.</p>}
      </div>
    </div>
    <details className="mt-4 rounded-lg border border-zinc-800 bg-zinc-950/40 p-3"><summary className="cursor-pointer text-sm font-medium text-zinc-300">Round and session details</summary>
      <div className="mt-3 grid gap-2 text-xs text-zinc-400 sm:grid-cols-2 xl:grid-cols-4">
        <div>Analysis based on round: <span className="text-zinc-200">{decision.source_round_id || 'Unknown'}</span></div>
        <div>Decision for round: <span className="text-zinc-200">{decision.target_round_id || 'Unknown'}</span></div>
        <div>Risk profile: <span className="text-zinc-200">{decision.profile || 'Unknown'}</span></div>
        <div>Decision expires: <span className="text-zinc-200">{decision.expires_at || 'Unknown'}</span></div>
      </div>
    </details>
  </Card>;
}
