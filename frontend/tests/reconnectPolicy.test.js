import test from 'node:test';
import assert from 'node:assert/strict';

import {
  connectionStateAfterHealth,
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

test('connection state distinguishes initial connect, recovery, and prolonged disconnect', () => {
  assert.equal(connectionStateAfterHealth({ online: false, attempt: 1 }), 'INITIAL_CONNECT');
  assert.equal(connectionStateAfterHealth({ online: true, degraded: false }), 'CONNECTED');
  assert.equal(connectionStateAfterHealth({ online: true, degraded: true }), 'DEGRADED');
  assert.equal(connectionStateAfterHealth({ online: false, lastSuccessfulAt: 123, attempt: 1 }), 'RECONNECTING');
  assert.equal(connectionStateAfterHealth({ online: false, lastSuccessfulAt: 123, attempt: 5 }), 'DISCONNECTED');
});
