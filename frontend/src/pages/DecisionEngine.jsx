import { GitBranch } from 'lucide-react';
import Card from '../components/Card.jsx';
import DecisionPanel from '../components/DecisionPanel.jsx';
import PageHeader from '../components/PageHeader.jsx';
import { ErrorState, LoadingState } from '../components/PageState.jsx';
import { useLiveData } from '../hooks/useLiveData.js';

export default function DecisionEngine() {
  const { loading, error, decision, decisionHistory, decisionStatus } = useLiveData();
  if (loading) return <LoadingState label="Loading decision audit…" />;
  if (error) return <ErrorState message={error} />;
  return <div className="space-y-5">
    <PageHeader eyebrow="Part 8" title="Decision Engine" icon={GitBranch} description="Qualification and risk-handoff records. This engine does not size or place bets." />
    <DecisionPanel decision={decision} />
    <Card title="Configuration"><pre className="overflow-auto text-xs text-zinc-400">{JSON.stringify(decisionStatus?.config || {}, null, 2)}</pre></Card>
    <Card title="Decision Audit History"><div className="space-y-2">{(decisionHistory || []).map((row) => <div key={row.decision_id} className="grid gap-2 rounded border border-zinc-800 p-3 text-sm md:grid-cols-5"><span>{row.created_at}</span><span>{row.status}</span><span>{row.source_round_id} → {row.target_round_id}</span><span>{row.risk_status}</span><span>{row.execution_status}</span></div>)}{!decisionHistory?.length && <p className="text-sm text-zinc-500">No decisions recorded.</p>}</div></Card>
  </div>;
}
