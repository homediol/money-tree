import test from 'node:test';
import assert from 'node:assert/strict';

import {
  MAX_RECONNECT_BACKOFF_MS,
  reconnectDelay,
} from '../src/services/reconnectPolicy.js';

test('backend reconnect uses capped exponential backoff', () => {
  assert.deepEqual(
    [1, 2, 3, 4, 5, 6].map(reconnectDelay),
    [1000, 2000, 4000, 8000, 16000, 30000],
  );
  assert.equal(reconnectDelay(100), MAX_RECONNECT_BACKOFF_MS);
});

test('invalid attempts cannot produce a rapid zero-delay loop', () => {
  assert.equal(reconnectDelay(0), 1000);
  assert.equal(reconnectDelay(-10), 1000);
  assert.equal(reconnectDelay(undefined), 1000);
});

