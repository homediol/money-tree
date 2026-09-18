import { ShieldCheck } from 'lucide-react';
import EvidencePanel from '../components/EvidencePanel.jsx';
import Card from '../components/Card.jsx';
import PageHeader from '../components/PageHeader.jsx';
import { ErrorState, LoadingState } from '../components/PageState.jsx';
import { useLiveData } from '../hooks/useLiveData.js';
import { pct } from '../utils/format.js';

export default function EvidenceExplorer() {
  const { loading, error, evidence, evidenceHistory } = useLiveData();
  if (loading) return <LoadingState label="Loading evidence snapshots…" />;
  if (error) return <ErrorState message={error} />;
  return <div className="space-y-5">
    <PageHeader eyebrow="Part 7" title="Confidence & Evidence" icon={ShieldCheck} description="Immutable explanations for historical model estimates. Probability and confidence are separate measurements." />
    <EvidencePanel evidence={evidence} />
    <Card title="Historical Evidence Explorer"><div className="space-y-2">{(evidenceHistory || []).map((row) => <div key={row.evidence_id} className="grid gap-2 rounded border border-zinc-800 p-3 text-sm md:grid-cols-5"><span>{row.calculated_at}</span><span>Round {row.source_round_id}</span><span>Estimate {pct(row.ml_probability)}</span><span>{row.confidence} ({row.confidence_score})</span><span>{row.evidence_strength}</span></div>)}{!evidenceHistory?.length && <p className="text-sm text-zinc-500">No stored evidence snapshots.</p>}</div></Card>
  </div>;
}
