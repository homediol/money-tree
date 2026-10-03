import assert from 'node:assert/strict';
import test from 'node:test';

import { accessScreenState, hasUsableApplicationState } from '../src/services/accessPolicy.js';

test('verified access keeps the application available through every transport state', () => {
  for (const state of ['INITIAL_CONNECT', 'CONNECTED', 'DEGRADED', 'RECONNECTING', 'DISCONNECTED']) {
    assert.equal(hasUsableApplicationState('granted'), true, `state ${state}`);
    assert.equal(accessScreenState('granted', state), state, `state ${state}`);
  }
});

test('initial connection and actual auth requirement remain separate', () => {
  assert.equal(hasUsableApplicationState('checking'), false);
  assert.equal(accessScreenState('checking', 'INITIAL_CONNECT'), 'INITIAL_CONNECT');
  assert.equal(accessScreenState('required', 'CONNECTED'), 'AUTH_REQUIRED');
  assert.equal(accessScreenState('error', 'CONNECTED'), 'ACCESS_CHECK_FAILED');
});
