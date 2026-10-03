import test from 'node:test';
import assert from 'node:assert/strict';
import { PostgresRoundStore } from '../collector/PostgresRoundStore.js';

test('collector lease refuses a second collector and releases its connection', async () => {
  const store = new PostgresRoundStore();
  let released = 0;
  store.pool = {
    connect: async () => ({
      query: async () => ({ rows: [{ acquired: false }] }),
      release: () => { released += 1; },
    }),
  };

  await assert.rejects(store._acquireCollectorLease(), error => error.code === 'COLLECTOR_ALREADY_ACTIVE');
  assert.equal(released, 1);
  assert.equal(store.collectorLease, null);
});

test('collector lease is held on one session and unlocked at store shutdown', async () => {
  const store = new PostgresRoundStore();
  const queries = [];
  let released = 0;
  let poolEnded = false;
  store.pool = {
    connect: async () => ({
      query: async sql => {
        queries.push(sql);
        return { rows: [{ acquired: true }] };
      },
      release: () => { released += 1; },
    }),
    end: async () => { poolEnded = true; },
  };

  await store._acquireCollectorLease();
  assert.match(queries[0], /pg_try_advisory_lock/);
  await store.close();
  assert.match(queries[1], /pg_advisory_unlock/);
  assert.equal(released, 1);
  assert.equal(poolEnded, true);
});

test('round index collisions are reconciled inside the batch without aborting persistence', async () => {
  const store = new PostgresRoundStore();
  store.usingFileFallback = false;
  const rows = [{ round_id: 'old', round_index: 4, multiplier: 1.2, timestamp: null }];
  const client = {
    async query(sql, params = []) {
      if (sql === 'BEGIN' || sql === 'COMMIT' || sql === 'ROLLBACK' || sql.includes('pg_advisory_xact_lock')) return { rowCount: 0, rows: [] };
      if (sql.includes('COALESCE(MAX(round_index)')) return { rows: [{ max_round_index: Math.max(...rows.map(row => row.round_index)) }] };
      if (sql.includes('WHERE round_id =')) return { rowCount: rows.some(row => row.round_id === params[0]) ? 1 : 0, rows: [] };
      if (sql.includes('WHERE round_index =')) return { rowCount: rows.some(row => row.round_index === Number(params[0])) ? 1 : 0, rows: [] };
      if (sql.includes('INSERT INTO')) {
        rows.push({ round_id: params[0], round_index: Number(params[1]), multiplier: Number(params[2]), timestamp: params[3] });
        return { rowCount: 1, rows: [] };
      }
      throw new Error(`Unexpected client query: ${sql}`);
    },
    release() {},
  };
  store.pool = {
    connect: async () => client,
    async query(sql) {
      if (sql.includes('SELECT round_id, round_index')) return { rows: [...rows] };
      if (sql.includes('INSERT INTO collector_state')) return { rowCount: 1, rows: [] };
      throw new Error(`Unexpected pool query: ${sql}`);
    },
  };

  const result = await store.saveRounds([
    { round_id: 'first', round_index: 5, multiplier: 1.5 },
    { round_id: 'second', round_index: 5, multiplier: 2.1 },
  ]);

  assert.equal(result.saved, 2);
  assert.equal(result.rounds.length, 3);
  assert.deepEqual(result.rounds.map(row => row.round_index), [4, 5, 6]);
});
