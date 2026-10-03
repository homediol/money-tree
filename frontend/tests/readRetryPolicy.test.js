import test from 'node:test';
import assert from 'node:assert/strict';
import { MAX_SAFE_READ_RETRIES, safeReadRetryDelay, shouldRetryReadFailure } from '../src/services/readRetryPolicy.js';

test('retries a safe GET after timeout or temporary server failure', () => {
  assert.equal(shouldRetryReadFailure({ method: 'get' }, { code: 'ECONNABORTED' }), true);
  assert.equal(shouldRetryReadFailure({ method: 'GET' }, { response: { status: 503 } }), true);
});

test('does not replay writes, auth failures, missing routes, or a retry twice', () => {
  assert.equal(shouldRetryReadFailure(undefined, { code: 'ETIMEDOUT' }), false);
  assert.equal(shouldRetryReadFailure(undefined, new Error('request failed')), false);
  assert.equal(shouldRetryReadFailure({ method: 'post' }, { code: 'ECONNABORTED' }), false);
  assert.equal(shouldRetryReadFailure({ method: 'get' }, { response: { status: 401 } }), false);
  assert.equal(shouldRetryReadFailure({ method: 'get' }, { response: { status: 404 } }), false);
  assert.equal(shouldRetryReadFailure({ method: 'get', _safeReadRetryCount: MAX_SAFE_READ_RETRIES }, { code: 'ETIMEDOUT' }), false);
  assert.equal(shouldRetryReadFailure({ method: 'get' }, { code: 'ERR_CANCELED' }), false);
});

test('retry delay backs off and is capped', () => {
  assert.equal(safeReadRetryDelay(1), 1000);
  assert.equal(safeReadRetryDelay(2), 2000);
  assert.equal(safeReadRetryDelay(20), 5000);
});
