import { GitBranch } from 'lucide-react';
import Card from '../components/Card.jsx';
import DecisionPanel from '../components/DecisionPanel.jsx';
import PageHeader from '../components/PageHeader.jsx';
import { ErrorState, LoadingState } from '../components/PageState.jsx';
import { useLiveData } from '../hooks/useLiveData.js';

export default function DecisionEngine() {
  const { loading, error, decision, decisionHistory, decisionStatus } = useLiveData();
  const pipeline = decisionStatus?.pipeline;
  if (loading) return <LoadingState label="Loading decision audit…" />;
  if (error) return <ErrorState message={error} />;
  return <div className="space-y-5">
    <PageHeader eyebrow="Part 8" title="Decision Engine" icon={GitBranch} description="Qualification and risk-handoff records. This engine does not size or place bets." />
    <Card title="Decision pipeline">
      <div className="grid gap-3 text-sm md:grid-cols-4">
        <div><span className="text-zinc-500">State</span><div className={pipeline?.state === 'READY' ? 'font-semibold text-emerald-300' : 'font-semibold text-amber-300'}>{pipeline?.state || 'UNKNOWN'}</div></div>
        <div><span className="text-zinc-500">Model</span><div className="font-semibold">{pipeline?.model_status || 'UNKNOWN'}</div></div>
        <div><span className="text-zinc-500">Session mode</span><div className="font-semibold">{pipeline?.session_mode || 'OFF'}</div></div>
        <div><span className="text-zinc-500">Saved decisions</span><div className="font-semibold">{decisionStatus?.history_count ?? 0}</div></div>
      </div>
      <p className="mt-3 text-xs text-zinc-500"><code>decision.json</code> is the single current handoff record by design. Historical decisions are stored in the database audit.</p>
      {!!pipeline?.blockers?.length && <div className="mt-3 rounded border border-amber-500/30 bg-amber-500/10 p-3 text-sm text-amber-200">{pipeline.blockers.map((reason) => <div key={reason}>{reason}</div>)}</div>}
    </Card>
    <DecisionPanel decision={decision} />
    <Card title="Configuration"><pre className="overflow-auto text-xs text-zinc-400">{JSON.stringify(decisionStatus?.config || {}, null, 2)}</pre></Card>
    <Card title="Decision Audit History"><div className="space-y-2">{(decisionHistory || []).map((row) => <div key={row.decision_id} className="grid gap-2 rounded border border-zinc-800 p-3 text-sm md:grid-cols-5"><span>{row.created_at}</span><span>{row.status}</span><span>{row.source_round_id} → {row.target_round_id}</span><span>{row.risk_status}</span><span>{row.execution_status}</span></div>)}{!decisionHistory?.length && <p className="text-sm text-zinc-500">No decisions recorded.</p>}</div></Card>
  </div>;
}
