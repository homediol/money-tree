export const READINESS_EVENT = 'readiness:updated';

export function applyReadinessEvent(current, event) {
  return event?.type === READINESS_EVENT && event?.readiness
    ? event.readiness
    : current;
}

export function readinessRows(readiness) {
  const history = readiness?.history || {};
  const continuous = history.continuous_rounds ?? 0;
  const required = history.required_rounds ?? 100;
  const progress = history.progress_percentage ?? 0;
  return [
    ['Collector', readiness?.collector?.status || 'ERROR'],
    ['Total History', Number(history.total_rounds || 0).toLocaleString()],
    ['Continuous History', `${continuous}/${required}`],
    ['Warm-up Progress', `${progress}%`],
    ['History', history.status || 'WARMING_UP'],
    ['ML Model', readiness?.ml?.status || 'NOT_DEPLOYABLE'],
    ['Risk Engine', readiness?.risk_engine?.status || 'WAITING'],
    ['Decision', readiness?.decision?.status || 'NONE'],
    ['Betting Mode', readiness?.betting?.mode || 'OFF'],
    ['Emergency Stop', readiness?.emergency_stop ? 'TRUE' : 'FALSE'],
    ['Overall System', readiness?.overall?.status || 'NOT_READY'],
  ];
}

export function readinessProgress(readiness) {
  const value = Number(readiness?.history?.progress_percentage || 0);
  return Math.max(0, Math.min(100, Number.isFinite(value) ? value : 0));
}

export function trainingStatus(readiness) {
  const training = readiness?.automatic_training || {};
  return {
    status: training.status || 'IDLE',
    newRounds: training.new_rounds_since_training ?? 'UNKNOWN',
    minimumNewRounds: training.minimum_new_rounds || 250,
    nextRetrain: training.next_retrain_at || '—',
    cooldownSeconds: training.cooldown_remaining_s || 0,
    cooldownStatus: training.cooldown_status || 'UNKNOWN',
    trainingLock: training.training_lock || 'UNKNOWN',
    nextEvaluation: training.next_evaluation || 'WAITING',
    blockers: training.blockers || [],
    prospectiveStatus: training.prospective_evidence_status || 'UNKNOWN',
    latestCandidate: training.latest_candidate || 'NONE',
  };
}

export function persistenceStatus(readiness) {
  if (!readiness) return null;
  if (readiness.snapshot_source === 'CACHED_TIMEOUT') {
    return 'cached readiness snapshot';
  }
  if (readiness.restored_from_persistence) {
    return 'restored from persistence';
  }
  if (readiness.snapshot_source === 'PERSISTED_CACHE') {
    return 'restored cached snapshot';
  }
  return null;
}
