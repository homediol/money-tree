import assert from 'node:assert/strict';
import test from 'node:test';

import { currentExecutionIntents, currentSelections } from '../src/services/rulesEngineView.js';

test('Rules Engine dashboard omits expired intents without mutating audit data', () => {
  const intents = [
    { intent_id: 'current', execution_status: 'BLOCKED_READ_ONLY' },
    { intent_id: 'expired', execution_status: 'EXPIRED' },
  ];
  assert.deepEqual(currentExecutionIntents(intents), [intents[0]]);
  assert.equal(intents[1].execution_status, 'EXPIRED');
});

test('Rules Engine selection table omits expired targets but preserves pending and settled outcomes', () => {
  const selections = [
    { selection_id: 'pending', status: 'PENDING' },
    { selection_id: 'expired', status: 'EXPIRED' },
    { selection_id: 'win', status: 'WIN' },
  ];
  assert.deepEqual(currentSelections(selections), [selections[0], selections[2]]);
  assert.equal(selections[1].status, 'EXPIRED');
});
