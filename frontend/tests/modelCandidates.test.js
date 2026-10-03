import test from 'node:test';
import assert from 'node:assert/strict';
import { candidateCycleState, candidateRows, candidateReason } from '../src/services/modelCandidates.js';

test('candidate display preserves validation ranks regardless of test score', () => {
  const rows = candidateRows({ models: {
    unavailable: { status: 'UNAVAILABLE', rank: null },
    second: { rank: 2, test: { brier_score: 0 } },
    first: { rank: 1, test: { brier_score: 1 } },
  } });
  assert.deepEqual(rows.map(row => row.algorithm), ['first', 'second', 'unavailable']);
});

test('all rejection reasons and non-selected passing candidates are explained', () => {
  assert.equal(candidateReason({ rejection_reasons: ['test_failed', 'missing_dependency'] }), 'test failed; missing dependency');
  assert.match(candidateReason({ deployable: true, selected: false }), /not validation rank 1/);
  assert.deepEqual(candidateRows(null), []);
});

test('saved pre-cycle reports fill the operations dashboard with validation-only display ranks', () => {
  const saved = {
    model_version: 'legacy-selected', algorithm: 'random_forest',
    models: {
      logistic_regression: { selection_score: .18, selection: { brier_score: .17 }, selection_brier_advantage: .01, folds_beating_baseline: 3, walk_forward: [{}, {}, {}] },
      random_forest: { selection_score: .16, selection: { brier_score: .15 }, selection_brier_advantage: .02, folds_beating_baseline: 3, walk_forward: [{}, {}, {}] },
    },
    test_metrics: { brier_score: .22 },
  };
  const display = candidateCycleState(null, saved, ['cooldown_remaining:120s']);
  assert.equal(display.legacy, true);
  assert.equal(display.waiting, null);
  assert.deepEqual(display.rows.map(row => row.algorithm), ['random_forest', 'logistic_regression']);
  assert.equal(display.rows[0].test.brier_score, .22);
  assert.equal(display.rows[1].test, null);
  assert.match(candidateReason(display.rows[1], saved), /per-model final-test result not recorded/);
  assert.match(candidateReason(display.rows[1], saved), /legacy report/);
});

test('saved report ranks by composite score, not validation advantage', () => {
  const saved = { models: {
    extra_trees: { selection_score: .3768147, selection_brier_advantage: -.000892, selection: { brier_score: .250887 } },
    gradient_boosting: { selection_score: .3773984, selection_brier_advantage: -.000108, selection: { brier_score: .250103 } },
  } };
  const rows = candidateRows(null, saved);
  assert.deepEqual(rows.map(row => row.algorithm), ['extra_trees', 'gradient_boosting']);
  assert.ok(rows[0].selection_brier_advantage < rows[1].selection_brier_advantage);
  assert.ok(rows[0].selection_score < rows[1].selection_score);
});

test('a missing saved evaluation shows concrete cycle eligibility blockers', () => {
  const display = candidateCycleState(null, null, ['new_processed_rounds:80/250', 'cooldown_remaining:120s']);
  assert.equal(display.legacy, false);
  assert.equal(display.waiting, 'new_processed_rounds:80/250 · cooldown_remaining:120s');
});
