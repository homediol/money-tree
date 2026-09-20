import { useState } from 'react';
import { BrainCircuit, PlayCircle } from 'lucide-react';
import Card from '../components/Card.jsx';
import Metric from '../components/Metric.jsx';
import StatusPill from '../components/StatusPill.jsx';
import { useLiveData } from '../hooks/useLiveData.js';
import { trainModels } from '../services/api.js';
import { pct } from '../utils/format.js';
import PageHeader from '../components/PageHeader.jsx';
import { ErrorState, LoadingState } from '../components/PageState.jsx';

export default function ModelPerformance() {
  const { models, mlStatus, mlMetrics, mlEstimate, refresh, loading, error } = useLiveData();
  const [training, setTraining] = useState(false);
  const [trainError, setTrainError] = useState('');
  async function train() {
    setTraining(true);
    setTrainError('');
    try { await trainModels(); await refresh(); }
    catch (err) { setTrainError(err?.response?.data?.detail || err.message || 'Training failed.'); }
    finally { setTraining(false); }
  }
  if (loading) return <LoadingState label="Loading ML model state…" />;
  if (error) return <ErrorState message={error} onRetry={refresh} />;
  const validation = models?.validation_metrics || mlMetrics?.validation_metrics || {};
  const test = models?.test_metrics || mlMetrics?.test_metrics || {};
  const baseline = models?.baselines?.test?.base_rate_probability || mlMetrics?.baselines?.test?.base_rate_probability || {};
  const calibration = models?.calibration || mlMetrics?.calibration || {};
  const inferenceUsable = mlEstimate?.usable === true;
  const displayedRate = inferenceUsable ? mlEstimate?.prediction?.probability_2x : mlEstimate?.estimate?.informational_frequency_2x;
  return <div className="space-y-5">
    {trainError && <p role="alert" className="text-rose-400">{trainError}</p>}
    <PageHeader eyebrow="Probability estimation" title="ML Model" icon={BrainCircuit}
      description="Chronologically evaluated model estimates. Outputs are not guarantees or betting decisions."
      action={<button onClick={train} disabled={training} className="inline-flex items-center gap-2 rounded-lg bg-emerald-400 px-4 py-2 text-sm font-bold text-zinc-950 disabled:opacity-60"><PlayCircle size={17} />{training ? 'Training…' : 'Train models'}</button>} />
      <Card title="Model Health"><div className="flex flex-wrap items-center gap-4"><StatusPill status={mlStatus?.status || 'NOT_TRAINED'} /><span className="text-sm text-zinc-400">{mlStatus?.last_error || mlStatus?.validation_message || models?.message || 'No active model.'}</span><span className={`rounded px-2 py-1 text-xs font-semibold ${mlStatus?.deployable ? 'bg-emerald-500/15 text-emerald-300' : 'bg-amber-500/15 text-amber-300'}`}>{mlStatus?.deployable && mlStatus?.status === 'READY' ? 'LIVE INFERENCE ENABLED' : `LIVE INFERENCE DISABLED — ${mlStatus?.status === 'READY' ? mlStatus?.quality_state : mlStatus?.status || 'NOT_TRAINED'}`}</span></div></Card>
    <section className="grid gap-4 md:grid-cols-4">
      <Card><Metric label="Model Version" value={mlStatus?.model_version || 'N/A'} /></Card>
      <Card><Metric label="Feature Version" value={mlStatus?.feature_version || 'N/A'} /></Card>
      <Card><Metric label="Training Samples" value={models?.splits?.train ?? 0} /></Card>
      <Card><Metric label="Test Samples" value={models?.splits?.test ?? 0} /></Card>
    </section>
    <Card title={mlEstimate?.status === 'INFORMATIONAL_FALLBACK' ? 'Historical Frequency — Informational Only' : 'Latest Prediction — Model Estimate'}>
      <div className="grid gap-4 md:grid-cols-4">
        <Metric label={inferenceUsable ? 'Next-round ≥2x estimate' : 'Observed ≥2x — informational'} value={pct(displayedRate)} />
        <Metric label="Source Round" value={mlEstimate?.estimate?.source_round_id || 'N/A'} />
        <Metric label="Rounds used" value={mlEstimate?.estimate?.data_points_used ?? 'N/A'} />
        <Metric label="Use for decisions" value={mlEstimate?.estimate?.usable ? 'VALIDATED' : 'NO'} />
      </div>
      {mlEstimate?.reason && <p className="mt-3 text-sm text-amber-300">{mlEstimate.reason}
        {typeof mlEstimate.history_age === 'number' && ` · history age ${Math.round(mlEstimate.history_age)}s / ${mlEstimate.staleness_threshold}s max`}
      </p>}
    </Card>
    <Card title="Model Quality">
      <div className="grid gap-4 md:grid-cols-4">
        <Metric label="Baseline Test Brier" value={baseline?.brier_score?.toFixed?.(4) || 'N/A'} />
        <Metric label="Validation Brier" value={validation?.brier_score?.toFixed?.(4) || 'N/A'} />
        <Metric label="Baseline Validation Brier" value={models?.baselines?.validation?.base_rate_probability?.brier_score?.toFixed?.(4) || 'N/A'} />
        <Metric label="Test Brier" value={test?.brier_score?.toFixed?.(4) || 'N/A'} />
        <Metric label="Calibration ECE" value={calibration?.expected_calibration_error?.toFixed?.(4) || 'N/A'} />
        <Metric label="Test ROC-AUC" value={test?.roc_auc?.toFixed?.(4) || 'N/A'} />
        <Metric label="Test PR-AUC" value={test?.pr_auc?.toFixed?.(4) || 'N/A'} />
        <Metric label="Test F1" value={pct(test?.f1)} />
        <Metric label="Baseline Outperformed" value={models?.overfitting_checks?.outperforms_validation_baseline ? 'YES' : 'NO'} />
      </div>
    </Card>
  </div>;
}
