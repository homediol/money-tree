import assert from 'node:assert/strict';
import test from 'node:test';

import {
  applyReadinessEvent,
  persistenceStatus,
  readinessProgress,
  readinessRows,
  trainingStatus,
  completionMessage,
} from '../src/services/readinessView.js';


const snapshot = {
  collector: { status: 'HEALTHY' },
  history: {
    total_rounds: 10137,
    continuous_rounds: 20,
    required_rounds: 100,
    progress_percentage: 20,
    status: 'WARMING_UP',
  },
  ml: { status: 'NOT_DEPLOYABLE' },
  risk_engine: { status: 'WAITING' },
  decision: { status: 'EXPIRED' },
  betting: { mode: 'OFF' },
  emergency_stop: false,
  overall: { status: 'WARMING_UP' },
};


test('readiness panel exposes every required live field', () => {
  assert.deepEqual(readinessRows(snapshot), [
    ['Collector', 'HEALTHY'],
    ['Total History', '10,137'],
    ['Continuous History', '20/100'],
    ['Warm-up Progress', '20%'],
    ['History', 'WARMING_UP'],
    ['ML Model', 'NOT_DEPLOYABLE'],
    ['Risk Engine', 'WAITING'],
    ['Decision', 'EXPIRED'],
    ['Betting Mode', 'OFF'],
    ['Emergency Stop', 'FALSE'],
    ['Overall System', 'WARMING_UP'],
  ]);
  assert.equal(readinessProgress(snapshot), 20);
});


test('WebSocket readiness events replace state without a manual refresh', () => {
  const next = structuredClone(snapshot);
  next.history.continuous_rounds = 100;
  next.history.progress_percentage = 100;
  next.history.status = 'READY';
  assert.equal(applyReadinessEvent(snapshot, {
    type: 'readiness:updated', readiness: next,
  }), next);
  assert.equal(applyReadinessEvent(snapshot, { type: 'history:updated' }), snapshot);
});


test('progress bar clamps invalid backend input defensively', () => {
  assert.equal(readinessProgress({ history: { progress_percentage: 120 } }), 100);
  assert.equal(readinessProgress({ history: { progress_percentage: -4 } }), 0);
  assert.equal(readinessProgress({ history: { progress_percentage: 'bad' } }), 0);
});


test('training status follows live readiness updates', () => {
  const next = applyReadinessEvent(snapshot, { type: 'readiness:updated', readiness: {
    ...snapshot,
    automatic_training: { status: 'EVALUATING', new_rounds_since_training: 275,
      minimum_new_rounds: 250, next_retrain_at: '275/250', cooldown_remaining_s: 30,
      cooldown_status: 'WAITING', training_lock: 'BUSY', next_evaluation: 'TRAINING',
      blockers: ['cooldown_remaining:30s'],
      prospective_evidence_status: 'UNSEEN_BEFORE_NEXT_EVALUATION',
      latest_candidate: 'ml-candidate-2' },
  } });
  assert.deepEqual(trainingStatus(next), {
    status: 'EVALUATING', newRounds: 275, minimumNewRounds: 250,
    nextRetrain: '275/250', cooldownSeconds: 30, cooldownStatus: 'WAITING',
    trainingLock: 'BUSY', nextEvaluation: 'TRAINING',
    blockers: ['cooldown_remaining:30s'],
    prospectiveStatus: 'UNSEEN_BEFORE_NEXT_EVALUATION', latestCandidate: 'ml-candidate-2',
  });
});

test('unknown legacy training cursor is not shown as zero new rounds', () => {
  assert.equal(trainingStatus(snapshot).newRounds, 'UNKNOWN');
});

test('readiness snapshot labels restored and cached persistence states', () => {
  assert.equal(persistenceStatus({ restored_from_persistence: true }), 'restored from persistence');
  assert.equal(persistenceStatus({ snapshot_source: 'PERSISTED_CACHE' }), 'restored cached snapshot');
  assert.equal(persistenceStatus({ snapshot_source: 'CACHED_TIMEOUT' }), 'cached readiness snapshot');
  assert.equal(persistenceStatus(snapshot), null);
});

test('waiting message separates warm-up completion from evaluation start', () => {
  const result = completionMessage({ ...snapshot, automatic_training: {
    enabled: true, status: 'IDLE', new_rounds_since_training: 150, minimum_new_rounds: 250,
  }, completion_estimate: { warmup_remaining_seconds: 1200, evaluation_start_remaining_seconds: 1500,
    remaining_warmup_rounds: 80, warmup_expected_at: '2026-09-28T22:00:00Z',
    evaluation_start_expected_at: '2026-09-28T22:05:00Z' } });
  assert.match(result.title, /waiting/);
  assert.match(result.lines.join(' '), /warm-up completion: about 20 min/);
  assert.match(result.lines.join(' '), /evaluation start: about 25 min/);
});

test('running training without duration has no invented completion time', () => {
  const result = completionMessage({ ...snapshot, automatic_training: { status: 'VERIFYING' },
    completion_estimate: { training_elapsed_seconds: 2 } });
  assert.match(result.title, /in progress/);
  assert.match(result.lines.join(' '), /Elapsed: 0 min 2 sec/);
  assert.match(result.lines.join(' '), /reliable completion time is not available/);
});

test('disconnected warm-up has no countdown', () => {
  const result = completionMessage({ ...snapshot, collector: { healthy: false } });
  assert.match(result.lines.join(' '), /paused while the collector reconnects/);
  assert.doesNotMatch(result.lines.join(' '), /Estimated warm-up completion/);
});
