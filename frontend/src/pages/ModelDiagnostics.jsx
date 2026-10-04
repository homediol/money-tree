import { BrainCircuit } from 'lucide-react';
import { useCallback, useEffect, useState } from 'react';
import Card from '../components/Card.jsx';
import ModelCandidates from '../components/ModelCandidates.jsx';
import ModelProgressMessage from '../components/ModelProgressMessage.jsx';
import Metric from '../components/Metric.jsx';
import StatusPill from '../components/StatusPill.jsx';
import { pct } from '../utils/format.js';
import PageHeader from '../components/PageHeader.jsx';
import { ErrorState, LoadingState } from '../components/PageState.jsx';
import { getExperimentalML, getMLEstimate, getMLMetrics, getMLStatus, getModelPerformance, getSystemReadiness } from '../services/api.js';

export default function ModelDiagnostics() {
  const [state, setState] = useState({ loading: true, error: null });
  const refresh = useCallback(async () => {
    try {
      const [models, mlStatus, mlMetrics, mlEstimate, experimental, readiness] = await Promise.all([
        getModelPerformance(), getMLStatus(), getMLMetrics(), getMLEstimate(), getExperimentalML(),
        getSystemReadiness().catch(() => null),
      ]);
      setState({ models, mlStatus, mlMetrics, mlEstimate, experimental, readiness, loading: false, error: null });
    } catch (failure) {
      setState((current) => ({ ...current, loading: false, error: failure.message || 'Model API unavailable' }));
    }
  }, []);
  useEffect(() => {
    refresh();
    const timer = setInterval(refresh, 15000);
    return () => clearInterval(timer);
  }, [refresh]);
  const { models, mlStatus, mlMetrics, mlEstimate, experimental, loading, error } = state;
  if (loading) return <LoadingState label="Loading ML model state…" />;
  if (error) return <ErrorState message={error} onRetry={refresh} />;
  const validation = models?.validation_metrics || mlMetrics?.validation_metrics || {};
  const test = models?.test_metrics || mlMetrics?.test_metrics || {};
  const baseline = models?.baselines?.test?.base_rate_probability || mlMetrics?.baselines?.test?.base_rate_probability || {};
  const calibration = models?.calibration || mlMetrics?.calibration || {};
  const inferenceUsable = mlEstimate?.usable === true;
  const displayedRate = inferenceUsable ? mlEstimate?.prediction?.probability_2x : mlEstimate?.estimate?.informational_frequency_2x;
  return <div className="space-y-5">
    <ModelProgressMessage readiness={state.readiness} />
    <PageHeader eyebrow="Probability estimation" title="ML Model" icon={BrainCircuit}
      description="Chronologically evaluated model estimates. Outputs are not guarantees or betting decisions." />
    <p className="text-sm text-zinc-400">The next evaluation runs automatically after enough new processed and contiguous rounds, cooldown, and a free training lock. Progress is on the System Operations Dashboard.</p>
    <Card title="Evaluation cycle — all candidates"><ModelCandidates cycle={models} activeModelVersion={mlStatus?.active_model_version} /></Card>
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
    <Card title="Experimental model observations — no betting use">
      <p className="mb-3 text-sm text-zinc-400">Candidate estimates are recorded before the next round and scored when the matching result arrives. They remain experimental until chronological validation passes.</p>
      <div className="grid gap-4 md:grid-cols-4">
        <Metric label="Candidate" value={experimental?.model_version || 'N/A'} />
        <Metric label="Latest estimate ≥2x" value={pct(experimental?.latest?.probability_2x)} />
        <Metric label="Source / result" value={experimental?.latest ? `${experimental.latest.source_round_id} / ${experimental.latest.status}` : 'WAITING'} />
        <Metric label="Scored (latest 50)" value={experimental?.scored_count ?? 0} />
        <Metric label="Candidate Brier" value={experimental?.mean_brier_score?.toFixed?.(4) || 'N/A'} />
        <Metric label="Frozen baseline Brier" value={experimental?.baseline_mean_brier_score?.toFixed?.(4) || 'N/A'} />
        <Metric label="Available for decisions" value="NO" />
      </div>
      {experimental?.status === 'UNAVAILABLE' && <p className="mt-3 text-sm text-amber-300">Observation API is unavailable.</p>}
      {experimental?.last_skip_reason &&
        <p className="mt-3 text-sm text-amber-300">New observations paused: {experimental.last_skip_reason.replaceAll('_', ' ')}.</p>}
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
