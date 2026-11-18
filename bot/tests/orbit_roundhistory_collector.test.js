import test from 'node:test';
import assert from 'node:assert/strict';
import { waitForPostgresRoundStore } from '../collector/OrbitRoundHistoryCollector.js';

test('collector waits for PostgreSQL and retries initialization without exiting', async () => {
  let attempts = 1; // the initial startup connection attempt already failed
  const waits = [];
  const errors = [];
  const roundStore = {
    pool: null,
    usingFileFallback: false,
    storageRetryAt: 0,
    storageError: Object.assign(new Error('connection timeout'), { code: 'ETIMEDOUT' }),
    async init(options) {
      assert.deepEqual(options, { acquireCollectorLease: true });
      attempts += 1;
      if (attempts === 2) this.pool = { ready: true };
    },
  };

  const ready = await waitForPostgresRoundStore(roundStore, {
    onWaiting: error => errors.push(error?.code),
    wait: async milliseconds => waits.push(milliseconds),
  });

  assert.equal(ready, true);
  assert.equal(attempts, 2);
  assert.equal(errors.length, 1);
  assert.equal(waits.length, 1);
  assert.ok(waits[0] > 0);
});

test('collector storage retry stops cleanly when shutdown is requested', async () => {
  const controller = new AbortController();
  let initAttempts = 0;
  const roundStore = {
    pool: null,
    usingFileFallback: false,
    storageRetryAt: 0,
    async init() { initAttempts += 1; },
  };
  const ready = await waitForPostgresRoundStore(roundStore, {
    signal: controller.signal,
    wait: async () => controller.abort(),
  });
  assert.equal(ready, false);
  assert.equal(initAttempts, 0);
});
