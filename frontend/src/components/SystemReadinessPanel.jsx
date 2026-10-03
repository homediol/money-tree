import { useEffect, useState } from 'react';
import Card from './Card.jsx';
import ModelCandidates from './ModelCandidates.jsx';
import ModelProgressMessage from './ModelProgressMessage.jsx';
import { getModelPerformance } from '../services/api.js';
import { persistenceStatus, readinessProgress, trainingStatus } from '../services/readinessView.js';

const positive = new Set(['HEALTHY', 'READY', 'DEPLOYABLE', 'APPROVED', 'CONNECTED', 'FALSE', 'ACTIVE']);
const waiting = new Set(['WARMING_UP', 'WAITING', 'OFF', 'SHADOW', 'IDLE', 'STARTING', 'NONE', 'NOT_EVALUATED']);

function plain(value) {
  if (value == null || value === '') return 'Unknown';
  const key = String(value).toUpperCase();
  const labels = {
    HEALTHY: 'Healthy', READY: 'Ready', DEPLOYABLE: 'Approved for use', APPROVED: 'Approved',
    CONNECTED: 'Connected', FALSE: 'Not active', TRUE: 'Active', WARMING_UP: 'Collecting history',
    WAITING: 'Waiting', OFF: 'Off', SHADOW: 'Shadow mode', IDLE: 'Idle', STARTING: 'Starting',
    NONE: 'Not available', NOT_EVALUATED: 'Not checked', NOT_DEPLOYABLE: 'Not approved for use',
    NOT_READY: 'Not ready', BLOCKED: 'Blocked', ERROR: 'Error', FAILED: 'Failed',
    TRAINING: 'Training', EVALUATING: 'Evaluating', VERIFYING: 'Verifying', TRAINING_FAILED: 'Evaluation failed',
    LIVE_REAL: 'Live mode', SHADOW_REALISTIC: 'Shadow mode', SIMULATION: 'Simulation',
  };
  return labels[key] || String(value).replaceAll('_', ' ').toLowerCase();
}

function stateColor(value) {
  const state = String(value || '').toUpperCase();
  return positive.has(state) ? 'text-emerald-300' : waiting.has(state) ? 'text-amber-300' : 'text-rose-300';
}

function ReadinessTile({ label, value, detail }) {
  const machineState = typeof value === 'string' && /^[A-Z][A-Z0-9_]+$/.test(value);
  return <div className="rounded-lg border border-zinc-800 bg-zinc-950/70 p-3">
    <div className="text-xs text-zinc-500">{label}</div>
    <div className={`mt-1 text-sm font-semibold capitalize ${machineState ? stateColor(value) : 'text-zinc-200'}`}>{machineState ? plain(value) : value ?? 'Unknown'}</div>
    {detail && <div className="mt-1 text-xs text-zinc-500">{detail}</div>}
  </div>;
}

export default function SystemReadinessPanel({ readiness, error }) {
  const [savedModelReport, setSavedModelReport] = useState(null);
  useEffect(() => {
    let mounted = true;
    const refresh = () => getModelPerformance().then(report => {
      if (mounted) setSavedModelReport(report);
    }).catch(() => {});
    refresh();
    const timer = setInterval(refresh, 15000);
    return () => { mounted = false; clearInterval(timer); };
  }, []);

  if (!readiness) return <Card title="Model and safety readiness">
    <p className="text-sm text-amber-200">{error || 'Waiting for the first verified readiness update.'}</p>
  </Card>;

  const progress = readinessProgress(readiness);
  const history = readiness.history || {};
  const ml = readiness.ml || {};
  const baseline = ml.baseline_comparison || {};
  const folds = ml.validation_folds || {};
  const test = ml.test_performance || {};
  const training = trainingStatus(readiness);
  const reasons = readiness.overall?.reasons || [];
  const persistence = persistenceStatus(readiness);
  const emergency = readiness.emergency_stop;
  const rows = [
    ['Round collector', readiness.collector?.status, readiness.collector?.reason],
    ['Rounds in history', history.total_rounds == null ? 'Unknown' : Number(history.total_rounds).toLocaleString()],
    ['Consecutive rounds', history.continuous_rounds == null || history.required_rounds == null ? 'Unknown' : `${history.continuous_rounds} of ${history.required_rounds}`],
    ['History collection', history.status],
    ['Model', ml.status],
    ['Risk checks', readiness.risk_engine?.status],
    ['Current decision', readiness.decision?.status],
    ['Execution mode', readiness.betting?.mode],
    ['Emergency stop', emergency == null ? 'Unknown' : emergency ? 'TRUE' : 'FALSE'],
    ['Overall readiness', readiness.overall?.status],
  ];
  const remaining = typeof training.newRounds === 'number' ? Math.max(0, training.minimumNewRounds - training.newRounds) : null;

  return <Card title="Model and safety readiness">
    <div className="mb-4 rounded-lg border border-sky-500/20 bg-sky-500/5 p-4"><ModelProgressMessage readiness={readiness} /></div>
    {error && <div role="status" className="mb-3 rounded border border-amber-500/30 bg-amber-500/10 p-3 text-xs text-amber-200">Readiness could not refresh. Showing the last received snapshot; it may be stale. {error}</div>}
    {persistence && <div className="mb-3 rounded border border-zinc-700 bg-zinc-950/60 px-3 py-2 text-xs text-zinc-400">Snapshot source: {plain(persistence)}</div>}

    <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
      {rows.map(([label, current, detail]) => <ReadinessTile key={label} label={label} value={current} detail={detail} />)}
    </div>

    <div className="mt-4 rounded-lg border border-zinc-800 bg-zinc-950/50 p-4">
      <div className="mb-2 flex justify-between gap-3 text-sm"><span className="text-zinc-300">History needed before evaluation</span><span className="text-zinc-200">{progress}%</span></div>
      <div className="h-2.5 overflow-hidden rounded-full bg-zinc-800" role="progressbar" aria-label="History warm-up progress" aria-valuemin="0" aria-valuemax="100" aria-valuenow={progress}>
        <div className={`h-full transition-[width] duration-500 ${progress >= 100 ? 'bg-emerald-500' : 'bg-amber-500'}`} style={{ width: `${progress}%` }} />
      </div>
      <p className="mt-2 text-xs text-zinc-500">This shows continuous recorded rounds toward the history requirement. It does not indicate a likely result.</p>
    </div>

    <div className="mt-4 grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
      <ReadinessTile label="Model evaluation" value={plain(training.status)} />
      <ReadinessTile label="New rounds needed" value={typeof training.newRounds === 'number' ? `${training.newRounds} of ${training.minimumNewRounds}` : 'Unknown'} detail={remaining == null ? 'Waiting for a training count' : `${remaining} more rounds before the next evaluation` } />
      <ReadinessTile label="Evaluation cooldown" value={training.cooldownStatus === 'READY' ? 'Ready' : training.cooldownSeconds == null ? 'Unknown' : `${training.cooldownSeconds} seconds remaining`} />
      <ReadinessTile label="Next evaluation" value={plain(training.nextEvaluation)} />
    </div>

    {!!reasons.length && <div className="mt-4 rounded-lg border border-amber-500/30 bg-amber-500/10 p-4 text-sm text-amber-200"><div className="mb-1 font-semibold">Why the system is waiting</div>{reasons.map(reason => <div key={reason} className="mt-1">{plain(reason)}</div>)}</div>}

    <details className="mt-4 rounded-lg border border-zinc-800 bg-zinc-950/40 p-3">
      <summary className="cursor-pointer text-sm font-semibold text-zinc-300">Advanced model validation and diagnostics</summary>
      <p className="mt-2 text-xs leading-5 text-zinc-500">These technical metrics help operators review model validation. They do not authorize bets and missing values remain unknown.</p>
      <div className="mt-3 grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
        <ReadinessTile label="Training lock" value={plain(training.trainingLock)} />
        <ReadinessTile label="Next evaluation progress" value={training.nextRetrain || 'Unknown'} />
        <ReadinessTile label="Latest candidate" value={training.latestCandidate === 'NONE' ? 'No candidate recorded' : training.latestCandidate} />
        <ReadinessTile label="Model version" value={ml.model_id || 'No approved model ID'} />
        <ReadinessTile label="Validation folds passed" value={folds.total == null ? 'Unknown' : `${folds.passed ?? 0} of ${folds.total}`} />
        <ReadinessTile label="Selection Brier improvement" value={baseline.selection_brier_advantage == null ? 'Unknown' : Number(baseline.selection_brier_advantage).toFixed(6)} />
        <ReadinessTile label="Test Brier score" value={test.brier_score == null ? 'Unknown' : Number(test.brier_score).toFixed(6)} />
        <ReadinessTile label="Test Brier improvement" value={baseline.test_brier_advantage == null ? 'Unknown' : Number(baseline.test_brier_advantage).toFixed(6)} />
      </div>
      {ml.rejection_reason && <div className="mt-3 rounded border border-rose-500/30 bg-rose-500/10 p-3 text-xs text-rose-200">Latest model was not approved: {plain(ml.rejection_reason)}</div>}
      {!!training.blockers.length && <div className="mt-3 rounded-lg border border-amber-500/30 bg-amber-500/10 p-3 text-xs text-amber-200"><div className="mb-1 font-semibold">Evaluation waiting reasons</div>{training.blockers.map(blocker => <div key={blocker} className="mt-1">{plain(blocker)}</div>)}</div>}
      <div className="mt-4 border-t border-zinc-800 pt-4"><h3 className="mb-3 text-sm font-semibold text-zinc-300">Model candidate history</h3><ModelCandidates
        cycle={readiness.automatic_training?.candidate_cycle}
        savedReport={savedModelReport}
        activeModelVersion={readiness.ml?.deployable ? readiness.ml?.model_id : null}
        waitingReasons={readiness.automatic_training?.blockers || []}
      /></div>
    </details>
  </Card>;
}
