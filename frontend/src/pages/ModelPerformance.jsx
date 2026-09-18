import { useState } from 'react';
import Card from '../components/Card.jsx';
import Metric from '../components/Metric.jsx';
import StatusPill from '../components/StatusPill.jsx';
import { useLiveData } from '../hooks/useLiveData.js';
import { trainModels } from '../services/api.js';
import { pct } from '../utils/format.js';
import { BrainCircuit, PlayCircle } from 'lucide-react';
import PageHeader from '../components/PageHeader.jsx';
import { EmptyState, ErrorState, LoadingState } from '../components/PageState.jsx';

export default function ModelPerformance() {
  const { models, refresh, loading, error } = useLiveData();
  const [training, setTraining] = useState(false);
  async function train() {
    setTraining(true);
    try {
      await trainModels();
      await refresh();
    } finally {
      setTraining(false);
    }
  }
  if (loading) return <LoadingState label="Loading model validation…" />;
  if (error) return <ErrorState message={error} onRetry={refresh} />;
  const modelRows = Object.entries(models?.models || {});
  const base = models?.baselines?.base_rate || {};
  return (
    <div className="space-y-5">
      <PageHeader eyebrow="Validation workspace" title="Models" icon={BrainCircuit}
        description="Review walk-forward validation and calibration metrics. Model outputs remain separate from risk approval and bet execution."
        action={<button onClick={train} disabled={training} className="inline-flex items-center gap-2 rounded-lg bg-emerald-400 px-4 py-2 text-sm font-bold text-zinc-950 hover:bg-emerald-300 disabled:opacity-60"><PlayCircle size={17} />{training ? 'Training…' : 'Train models'}</button>} />
      <Card title="Validation Status">
        <div className="flex flex-wrap items-center gap-4">
          <StatusPill status={models?.status || 'MODEL NOT TRAINED'} />
          <span className="text-sm text-zinc-400">{models?.message || 'Run training to create walk-forward validation metrics.'}</span>
        </div>
      </Card>
      <section className="grid gap-4 md:grid-cols-4">
        <Card><Metric label="Dataset Examples" value={models?.dataset_size ?? 0} /></Card>
        <Card><Metric label="Base Brier" value={base?.brier_score?.toFixed?.(3) || 'N/A'} /></Card>
        <Card><Metric label="Base Rate" value={pct(models?.baselines?.historical_base_rate)} /></Card>
        <Card><Metric label="Calibration" value={models?.calibration?.supported ? 'Supported' : 'Not supported'} /></Card>
      </section>
      <Card title="Walk-Forward Model Metrics">
        {modelRows.length === 0 ? <EmptyState title="No validated models" description="Run training to generate walk-forward metrics." /> :
        <div className="overflow-x-auto">
          <table className="w-full min-w-[720px] text-left text-sm">
            <thead className="text-xs uppercase text-zinc-500">
              <tr><th className="py-2">Model</th><th>Accuracy</th><th>Precision</th><th>Recall</th><th>ROC-AUC</th><th>Brier</th></tr>
            </thead>
            <tbody>
              {modelRows.map(([name, m]) => (
                <tr key={name} className="border-t border-zinc-800">
                  <td className="py-3">{name.replaceAll('_', ' ')}</td>
                  <td>{pct(m.accuracy)}</td>
                  <td>{pct(m.precision)}</td>
                  <td>{pct(m.recall)}</td>
                  <td>{m.roc_auc?.toFixed?.(3)}</td>
                  <td>{m.brier_score?.toFixed?.(3)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>}
      </Card>
    </div>
  );
}
