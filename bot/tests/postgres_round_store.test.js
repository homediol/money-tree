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

test('saveRounds returns committed database identity and total row count for capture logging', async () => {
  const store = new PostgresRoundStore();
  const persisted = [{
    round_id: 'live-round-1', platform_round_id: null, platform_round_index: null,
    round_index: 11, local_round_index: 11, multiplier: 2.42,
    timestamp: '2026-10-04T10:00:00.000Z', platform_timestamp: null,
    observed_at: '2026-10-04T10:00:00.000Z', stored_at: '2026-10-04T10:00:00.050Z',
    round_identity_type: 'COLLECTOR_OBSERVATION_HASH', round_index_source: 'LOCAL_SEQUENCE',
    identity_confidence: 'OVERLAP_VERIFIED_ORDER_ONLY', continuity_verified: true,
    gap_before: false, continuity_proof: 'HISTORY_SNAPSHOT_OVERLAP',
  }];
  const client = {
    async query(sql, params = []) {
      if (sql === 'BEGIN' || sql === 'COMMIT' || sql === 'ROLLBACK' || sql.includes('pg_advisory_xact_lock')) return { rowCount: 0, rows: [] };
      if (sql.includes('COALESCE(MAX(round_index)')) return { rows: [{ max_round_index: 10 }] };
      if (sql.includes('WHERE round_id =') || sql.includes('WHERE round_index =')) return { rowCount: 0, rows: [] };
      if (sql.includes('INSERT INTO')) return {
        rowCount: 1,
        rows: [{
          id: 501, round_id: params[0], round_index: params[3],
          platform_round_id: params[1], platform_round_index: params[2],
          multiplier: params[5], platform_timestamp: params[7],
          observed_at: params[8], stored_at: new Date('2026-10-04T10:00:00.050Z'),
          source: params[15], continuity_verified: params[12], gap_before: params[13],
          identity_confidence: params[11],
        }],
      };
      throw new Error(`Unexpected client query: ${sql}`);
    },
    release() {},
  };
  store.pool = {
    connect: async () => client,
    async query(sql) {
      if (sql.includes('SELECT round_id, platform_round_id')) return { rows: persisted };
      if (sql.includes('INSERT INTO collector_state')) return { rowCount: 1, rows: [] };
      throw new Error(`Unexpected pool query: ${sql}`);
    },
  };

  const result = await store.saveRounds([persisted[0]], 'collector');
  assert.equal(result.saved, 1);
  assert.equal(result.totalPersistedRows, 1);
  assert.deepEqual(result.insertedRounds[0], {
    db_id: 501,
    round_id: 'live-round-1',
    round_index: 11,
    local_round_index: 11,
    platform_round_id: null,
    platform_round_index: null,
    multiplier: 2.42,
    platform_timestamp: null,
    observed_at: '2026-10-04T10:00:00.000Z',
    stored_at: '2026-10-04T10:00:00.050Z',
    source: 'collector',
    continuity_verified: true,
    gap_before: false,
    identity_confidence: 'OVERLAP_VERIFIED_ORDER_ONLY',
  });
});

test('round index collision fails closed instead of inventing a new local index', async () => {
  const store = new PostgresRoundStore();
  store.usingFileFallback = false;
  const rows = [{ round_id: 'old', round_index: 4, multiplier: 1.2, timestamp: null }];
  const client = {
    async query(sql, params = []) {
      if (sql === 'BEGIN' || sql === 'COMMIT' || sql === 'ROLLBACK' || sql.includes('pg_advisory_xact_lock')) return { rowCount: 0, rows: [] };
      if (sql.includes('COALESCE(MAX(round_index)')) return { rows: [{ max_round_index: Math.max(...rows.map(row => row.round_index)) }] };
      if (sql.includes('WHERE round_id =')) return { rowCount: rows.some(row => row.round_id === params[0]) ? 1 : 0, rows: [] };
      if (sql.includes('WHERE round_index =')) {
        const owner = rows.find(row => row.round_index === Number(params[0]));
        return { rowCount: owner ? 1 : 0, rows: owner ? [owner] : [] };
      }
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

  await assert.rejects(
    store.saveRounds([{ round_id: 'different-round', round_index: 4, multiplier: 2.1 }]),
    error => error.code === 'ROUND_IDENTITY_CONFLICT',
  );
  assert.deepEqual(rows, [{ round_id: 'old', round_index: 4, multiplier: 1.2, timestamp: null }]);
});
