import Card from './Card.jsx';
import { persistenceStatus, readinessProgress, readinessRows, trainingStatus } from '../services/readinessView.js';

const good = new Set(['HEALTHY', 'READY', 'DEPLOYABLE', 'FALSE']);
const waiting = new Set(['WARMING_UP', 'WAITING', 'OFF', 'SHADOW']);

function tone(value) {
  if (good.has(value)) return 'text-emerald-300';
  if (waiting.has(value)) return 'text-amber-300';
  return 'text-rose-300';
}

function metric(value) {
  return value == null ? '—' : typeof value === 'number' ? value.toFixed(6) : String(value);
}

export default function SystemReadinessPanel({ readiness, error }) {
  if (!readiness) return <Card title="Automatic System Readiness">
    <p className="text-sm text-amber-200">{error || 'Waiting for the first readiness update.'}</p>
  </Card>;
  const progress = readinessProgress(readiness);
  const ml = readiness?.ml || {};
  const baseline = ml.baseline_comparison || {};
  const folds = ml.validation_folds || {};
  const test = ml.test_performance || {};
  const training = trainingStatus(readiness);
  const reasons = readiness?.overall?.reasons || [];
  const persistence = persistenceStatus(readiness);

  return <Card title="Automatic System Readiness">
    {error && <div className="mb-3 text-xs text-amber-200">Readiness API delayed; showing the latest received snapshot. {error}</div>}
    {persistence && <div className="mb-3 rounded border border-emerald-500/30 bg-emerald-500/10 px-3 py-2 text-xs font-semibold text-emerald-200">{persistence}</div>}
    <div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-3">
      {readinessRows(readiness).map(([label, value]) => <div key={label} className="rounded bg-zinc-900 p-3 text-sm">
        <div className="text-xs uppercase tracking-wide text-zinc-500">{label}</div>
        <div className={`mt-1 font-semibold ${tone(value)}`}>{value}</div>
      </div>)}
    </div>

    <div className="mt-4">
      <div className="mb-1 flex justify-between text-xs text-zinc-400">
        <span>Continuous history warm-up</span><span>{progress}%</span>
      </div>
      <div className="h-3 overflow-hidden rounded-full bg-zinc-800" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow={progress}>
        <div className={`h-full transition-[width] duration-500 ${progress >= 100 ? 'bg-emerald-500' : 'bg-amber-500'}`} style={{ width: `${progress}%` }} />
      </div>
    </div>

    <div className="mt-4 grid gap-3 text-xs text-zinc-400 md:grid-cols-2 xl:grid-cols-4">
      <div><span className="text-zinc-500">ML Training</span><div className="text-zinc-200">{training.status}</div></div>
      <div><span className="text-zinc-500">New processed rounds</span><div className="text-zinc-200">{training.newRounds}/{training.minimumNewRounds}</div></div>
      <div><span className="text-zinc-500">Contiguous rounds</span><div className="text-zinc-200">{readiness.history?.continuous_rounds ?? 0}/{readiness.history?.required_rounds ?? 100}</div></div>
      <div><span className="text-zinc-500">Cooldown</span><div className="text-zinc-200">{training.cooldownStatus === 'READY' ? 'READY' : `${training.cooldownSeconds}s remaining`}</div></div>
      <div><span className="text-zinc-500">Training lock</span><div className="text-zinc-200">{training.trainingLock}</div></div>
      <div><span className="text-zinc-500">Next evaluation</span><div className="text-zinc-200">{training.nextEvaluation}</div></div>
      <div><span className="text-zinc-500">Latest candidate</span><div className="break-all text-zinc-200">{training.latestCandidate}</div></div>
      <div><span className="text-zinc-500">Model ID</span><div className="break-all text-zinc-200">{ml.model_id || 'NONE'}</div></div>
      <div><span className="text-zinc-500">Validation folds</span><div className="text-zinc-200">{folds.passed || 0}/{folds.total || 0} passed</div></div>
      <div><span className="text-zinc-500">Selection Brier advantage</span><div className="text-zinc-200">{metric(baseline.selection_brier_advantage)}</div></div>
      <div><span className="text-zinc-500">Test Brier / advantage</span><div className="text-zinc-200">{metric(test.brier_score)} / {metric(baseline.test_brier_advantage)}</div></div>
    </div>

    <div className="mt-3 text-xs text-zinc-400">New rounds are unseen before the next scheduled evaluation. The latest candidate result is a historical chronological evaluation.</div>
    {!!training.blockers.length && <div className="mt-3 rounded bg-zinc-900 p-3 text-xs text-zinc-300">
      <div className="mb-1 font-semibold">Evaluation blockers</div>
      {training.blockers.map(blocker => <div key={blocker}>{blocker}</div>)}
    </div>}

    {ml.rejection_reason && <div className="mt-3 rounded border border-rose-500/30 bg-rose-500/10 p-3 text-xs text-rose-200">
      Model rejected: {ml.rejection_reason}
    </div>}
    {!!reasons.length && <div className="mt-3 rounded border border-amber-500/30 bg-amber-500/10 p-3 text-xs text-amber-200">
      <div className="mb-1 font-semibold">Readiness blockers</div>
      {reasons.map(reason => <div key={reason}>{reason}</div>)}
    </div>}
  </Card>;
}
