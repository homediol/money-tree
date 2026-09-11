import { useState } from 'react';
import Card from '../components/Card.jsx';
import Metric from '../components/Metric.jsx';
import StatusPill from '../components/StatusPill.jsx';
import { useLiveData } from '../hooks/useLiveData.js';
import { trainModels } from '../services/api.js';
import { pct } from '../utils/format.js';

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
  if (loading) return <div className="text-zinc-400">Loading model performance...</div>;
  if (error) return <div className="text-rose-300">{error}</div>;
  const modelRows = Object.entries(models?.models || {});
  const base = models?.baselines?.base_rate || {};
  return (
    <div className="space-y-5">
      <div className="flex flex-col justify-between gap-3 md:flex-row md:items-center">
        <h1 className="text-3xl font-semibold">Model Performance</h1>
        <button onClick={train} disabled={training} className="rounded bg-emerald-500 px-4 py-2 text-sm font-semibold text-zinc-950 disabled:opacity-60">
          {training ? 'Training...' : 'Train Models'}
        </button>
      </div>
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
              {modelRows.length === 0 && <tr><td className="py-4 text-zinc-500" colSpan="6">No validated model metrics yet.</td></tr>}
            </tbody>
          </table>
        </div>
      </Card>
    </div>
  );
}

