export const READINESS_EVENT = 'readiness:updated';

export function durationLabel(seconds) {
  if (seconds == null || !Number.isFinite(Number(seconds))) return null;
  const minutes = Math.max(1, Math.ceil(Number(seconds) / 60));
  return minutes < 60 ? `about ${minutes} min` : `about ${Math.floor(minutes / 60)} hr ${minutes % 60} min`;
}

export function completionMessage(readiness) {
  if (!readiness) return { title: 'Checking model progress', lines: ['Waiting for the current readiness status.'] };
  const history = readiness.history || {};
  const training = readiness.automatic_training || {};
  const estimate = readiness.completion_estimate || {};
  const running = ['TRAINING', 'EVALUATING', 'VERIFYING'].includes(training.status) || training.training_lock === 'BUSY';
  const clock = value => value ? new Date(value).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) : null;
  const lines = [];
  if (running) {
    if (estimate.training_elapsed_seconds != null) {
      const elapsed = Math.max(0, Math.floor(estimate.training_elapsed_seconds));
      lines.push(`Elapsed: ${Math.floor(elapsed / 60)} min ${elapsed % 60} sec.`);
    }
    const duration = durationLabel(estimate.training_remaining_seconds);
    lines.push(duration ? `Estimated evaluation completion: ${duration} (${clock(estimate.training_expected_at)}), based on the previous evaluation.`
      : 'Evaluation is running. A reliable completion time is not available yet.');
    lines.push('The result will appear automatically when validation finishes; this estimate may change.');
    return { title: `Model ${String(training.status || 'TRAINING').toLowerCase()} in progress`, lines };
  }
  if (!history.history_ready) {
    const remaining = estimate.remaining_warmup_rounds ?? Math.max(0, (history.required_rounds ?? 100) - (history.continuous_rounds ?? 0));
    lines.push(`Collecting continuous history: ${history.continuous_rounds ?? 0}/${history.required_rounds ?? 100} rounds; ${remaining} remaining.`);
    const duration = durationLabel(estimate.warmup_remaining_seconds);
    lines.push(duration ? `Estimated warm-up completion: ${duration} (${clock(estimate.warmup_expected_at)}).`
      : readiness.collector?.healthy === false ? 'Warm-up is paused while the collector reconnects. No completion estimate is available.'
        : 'Measuring recent round speed before estimating warm-up completion.');
  } else lines.push('Continuous history warm-up is complete.');
  if (training.enabled === false) lines.push('Automatic model evaluation is disabled.');
  else {
    const duration = durationLabel(estimate.evaluation_start_remaining_seconds);
    if (duration) lines.push(estimate.evaluation_start_remaining_seconds === 0 ? 'The next automatic evaluation is ready to start.'
      : `Estimated next evaluation start: ${duration} (${clock(estimate.evaluation_start_expected_at)}).`);
    else lines.push('The next evaluation starts after enough new rounds, cooldown, and a free training slot.');
    lines.push(`New rounds: ${training.new_rounds_since_training ?? 'unknown'}/${training.minimum_new_rounds ?? 250}.`);
  }
  if (training.status === 'TRAINING_FAILED') lines.push(`Evaluation failed: ${training.reason || 'see the model status for details'}.`);
  if (!readiness.ml?.deployable) lines.push(readiness.ml?.model_id
    ? 'The latest model did not pass validation. A completion time for an approved model cannot be predicted.'
    : 'No approved model is available yet. Approval depends on the validation result.');
  lines.push('Time estimates follow the recent round speed and update automatically.');
  return { title: training.last_result === 'FAILED' || training.status === 'TRAINING_FAILED' ? 'Model evaluation failed' : 'Model waiting for the next evaluation', lines };
}

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
