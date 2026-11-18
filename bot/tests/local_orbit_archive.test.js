import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { LocalOrbitArchive } from '../collector/LocalOrbitArchive.js';
import { PostgresRoundStore } from '../collector/PostgresRoundStore.js';
import { DurableRoundOutbox } from '../collector/DurableRoundOutbox.js';
import { RoundDeliveryPipeline } from '../collector/RoundDeliveryPipeline.js';
import { parseOrbitReading } from '../collector/OrbitReadings.js';

function reading(id, multiplier) {
  return { id: String(id), checkedAt: new Date(1_700_000_000_000 + Number(id)).toISOString(),
    items: [{ text: `${multiplier.toFixed(2)}x`, index: 0 }] };
}

function round(id, index, multiplier, deliveryMode = 'LIVE') {
  return { round_id: id, round_index: index, multiplier, timestamp: new Date().toISOString(),
    continuity_verified: true, gap_before: false, delivery_mode: deliveryMode,
    source_metadata: { source: 'ORBIT_DATA_WATCH_API', reading_id: String(index + 10) } };
}

async function withArchive(run, options = {}) {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'orbit-local-journal-'));
  const filePath = path.join(directory, 'journal.sqlite3');
  try { await run(filePath, options); }
  finally { fs.rmSync(directory, { recursive: true, force: true }); }
}

test('local archive atomically checkpoints source cursor, identities and pending delivery', async () => {
  await withArchive(async filePath => {
    let archive = await new LocalOrbitArchive({ filePath }).initialize();
    const one = round('orbit-one', 1, 1.25);
    const two = round('orbit-two', 2, 1.25);
    await archive.commitPage({ watchId: 'watch-1', expectedCursor: '0', nextCursor: '12',
      entries: [{ reading: reading(12, 1.25), parsed: { visibleItems: [{ multiplier: 1.25, index: 0 }] } }],
      snapshot: { version: 2, visibleMultipliers: [1.25] }, pendingRounds: [one, two] });
    assert.deepEqual((await archive.getState()).cursor, '12');
    assert.deepEqual((await archive.loadRounds()).map(item => item.round_id), ['orbit-one', 'orbit-two']);
    assert.deepEqual((await archive.getPendingRounds()).map(item => item.round_id), ['orbit-one', 'orbit-two']);
    await archive.close();

    archive = await new LocalOrbitArchive({ filePath }).initialize();
    assert.equal((await archive.getState()).watchId, 'watch-1');
    assert.equal((await archive.getState()).cursor, '12');
    assert.deepEqual((await archive.loadRounds()).map(item => item.round_id), ['orbit-one', 'orbit-two']);
    await archive.ackPendingRounds(['orbit-one']);
    assert.deepEqual((await archive.getPendingRounds()).map(item => item.round_id), ['orbit-two']);
    assert.equal(archive.snapshot().healthy, true);
    await archive.close();
  });
});

test('duplicate source delivery is idempotent and equal multipliers remain distinct rounds', async () => {
  await withArchive(async filePath => {
    const archive = await new LocalOrbitArchive({ filePath }).initialize();
    const entries = [{ reading: reading(12, 1), parsed: { visibleItems: [{ multiplier: 1, index: 0 }] } }];
    const accepted = [round('orbit-a', 1, 1), round('orbit-b', 2, 1)];
    const page = { watchId: 'watch-1', expectedCursor: '0', nextCursor: '12', entries,
      snapshot: { version: 2, visibleMultipliers: [1, 1] }, pendingRounds: accepted };
    await archive.commitPage(page);
    assert.deepEqual((await archive.loadRounds()).map(item => item.round_id), ['orbit-a', 'orbit-b']);
    assert.deepEqual((await archive.getArchivedReadings('watch-1', '0', '12')).map(item => item.readingId), ['12']);
    await assert.rejects(() => archive.commitPage(page), error => error.code === 'ARCHIVE_CURSOR_CONFLICT');
    assert.equal((await archive.loadRounds()).length, 2);
    await archive.close();
  });
});

test('restart can recover the latest overlap baseline from equivalent mirrored history containers', async () => {
  await withArchive(async filePath => {
    const archive = await new LocalOrbitArchive({ filePath }).initialize();
    const values = Array.from({ length: 60 }, (_, index) => `${(1.1 + (index % 37) / 10).toFixed(2)}x`);
    const raw = { id: '15181', checkedAt: '2026-11-18T08:38:30.921Z', count: 120,
      items: [
        ...values.map((value, index) => ({ index, value, visible: true,
          historyContainer: 'div.payouts-block[1]' })),
        ...values.map((value, index) => ({ index: index + values.length, value, visible: true,
          historyContainer: 'div.payouts-block[2]' })),
      ] };
    const parsed = parseOrbitReading(raw);
    await archive.commitPage({ watchId: 'watch-1', expectedCursor: '0', nextCursor: raw.id,
      entries: [{ reading: raw, parsed }],
      snapshot: { version: 2, visibleMultipliers: null, gapPending: true }, pendingRounds: [] });

    const baseline = await archive.getLatestVisibleSnapshot('watch-1', raw.id);
    assert.equal(baseline.readingId, raw.id);
    assert.equal(baseline.multipliers.length, 60);
    assert.deepEqual(baseline.multipliers, parsed.visibleItems.slice(0, 60).map(item => item.multiplier));
    await archive.close();
  });
});

test('PostgreSQL restart seeding preserves existing local round identity despite persistence metadata', async () => {
  await withArchive(async filePath => {
    const archive = await new LocalOrbitArchive({ filePath }).initialize();
    const accepted = round('orbit-stable', 7, 2.5);
    await archive.commitPage({ watchId: 'watch-1', expectedCursor: '0', nextCursor: '12',
      entries: [{ reading: reading(12, 2.5), parsed: { visibleItems: [{ multiplier: 2.5, index: 0 }] } }],
      snapshot: { version: 2, visibleMultipliers: [2.5] }, pendingRounds: [accepted] });
    const postgresProjection = { ...accepted, db_id: 99, stored_at: new Date().toISOString(),
      source: 'collector_async' };
    await archive.seedRounds([postgresProjection]);
    assert.deepEqual(await archive.loadRounds(), [accepted]);
    await assert.rejects(() => archive.seedRounds([{ ...postgresProjection, multiplier: 3.5 }]),
      error => error.code === 'ROUND_IDENTITY_CONFLICT');
    await archive.close();
  });
});

test('local journal refuses capacity overflow without advancing the accepted cursor', async () => {
  await withArchive(async filePath => {
    const archive = await new LocalOrbitArchive({ filePath, maxReadings: 1 }).initialize();
    const entry = id => ({ reading: reading(id, 1.5), parsed: { visibleItems: [{ multiplier: 1.5, index: 0 }] } });
    await archive.commitPage({ watchId: 'watch-1', expectedCursor: '0', nextCursor: '12',
      entries: [entry(12)], snapshot: { version: 2, visibleMultipliers: [1.5] } });
    await assert.rejects(() => archive.commitPage({ watchId: 'watch-1', expectedCursor: '12', nextCursor: '14',
      entries: [entry(13), entry(14)], snapshot: { version: 2, visibleMultipliers: [1.5] } }),
    error => error.code === 'LOCAL_JOURNAL_FULL');
    assert.equal((await archive.getState()).cursor, '12');
    await archive.close();
  });
});

test('collector delivery stays live with PostgreSQL offline and retains the round for recovery', async () => {
  await withArchive(async filePath => {
    const directory = path.dirname(filePath);
    const archive = await new LocalOrbitArchive({ filePath }).initialize();
    const postgres = new PostgresRoundStore({ connectionString: 'postgresql://offline:offline@127.0.0.1:1/offline' });
    await postgres.init({ acquireCollectorLease: true });
    assert.equal(postgres.pool, null);
    const accepted = round('orbit-offline', 1, 2.25);
    await archive.commitPage({ watchId: 'watch-1', expectedCursor: '0', nextCursor: '12',
      entries: [{ reading: reading(12, 2.25), parsed: { visibleItems: [{ multiplier: 2.25, index: 0 }] } }],
      snapshot: { version: 2, visibleMultipliers: [2.25] },
      pendingRounds: [{ ...accepted, delivery_mode: 'LIVE' }] });

    const outbox = new DurableRoundOutbox({ filePath: path.join(directory, 'events.jsonl'), eventOutbox: true });
    const receivedByLiveConsumer = [];
    const pipeline = new RoundDeliveryPipeline({ outbox, roundStore: postgres,
      publishObserved: async rows => { receivedByLiveConsumer.push(...rows); return { api_published_at: new Date().toISOString() }; },
      publishPersisted: async () => ({ acknowledged: true }),
    });
    void pipeline.start();
    pipeline.enqueue([{ ...accepted, delivery_mode: 'LIVE' }], { deliveryMode: 'LIVE' });
    const deadline = Date.now() + 2000;
    while (!receivedByLiveConsumer.length && Date.now() < deadline) await new Promise(resolve => setTimeout(resolve, 10));
    assert.deepEqual(receivedByLiveConsumer.map(row => row.round_id), ['orbit-offline']);
    assert.equal((await archive.getState()).cursor, '12');
    assert.equal(outbox.readAll().some(row => row.round_id === 'orbit-offline'), true,
      'unpersisted delivery intent remains durable while PostgreSQL is offline');
    await pipeline.stop();
    await archive.close();
    await postgres.close();
  });
});

test('durable observed round is automatically replayed to PostgreSQL without changing its UID', async () => {
  await withArchive(async filePath => {
    let postgresAvailable = false;
    const savedIds = [];
    const persistedIds = [];
    const postgres = {
      pool: null, usingFileFallback: false, storageRetryAt: Date.now() + 50,
      async init() { if (postgresAvailable) this.pool = {}; },
      async saveRounds(rows) {
        if (!this.pool) throw new Error('postgres_offline');
        savedIds.push(...rows.map(row => row.round_id));
        return { storage_status: 'AVAILABLE', commitAcknowledgedAt: new Date().toISOString(),
          insertedRounds: rows.map(row => ({ ...row, stored_at: new Date().toISOString() })) };
      },
      async loadRoundsByIds() { return []; },
      async close() {},
    };
    const outbox = new DurableRoundOutbox({ filePath: path.join(path.dirname(filePath), 'sync.jsonl'), eventOutbox: true });
    const accepted = { ...round('orbit-replay-stable', 41, 1.75), delivery_mode: 'LIVE' };
    let observed = 0;
    const pipeline = new RoundDeliveryPipeline({ outbox, roundStore: postgres,
      publishObserved: async () => { observed += 1; return { api_published_at: new Date().toISOString() }; },
      publishPersisted: async rows => { persistedIds.push(...rows.map(row => row.round_id));
        return { accepted: rows.length, events: [{ observation_id: rows[0].observation_id, committed_sequence: 1 }] }; },
    });
    void pipeline.start();
    pipeline.enqueue([accepted], { deliveryMode: 'LIVE' });
    const waitUntil = async predicate => {
      const deadline = Date.now() + 2500;
      while (!predicate() && Date.now() < deadline) await new Promise(resolve => setTimeout(resolve, 10));
      assert.equal(predicate(), true, 'condition should complete before the deadline');
    };
    try {
      await waitUntil(() => observed === 1 && outbox.readAll().length === 1);
      postgresAvailable = true;
      postgres.storageRetryAt = 0;
      await waitUntil(() => outbox.readAll().length === 0);
      assert.deepEqual(savedIds, ['orbit-replay-stable']);
      assert.deepEqual(persistedIds, ['orbit-replay-stable']);
    } finally { await pipeline.stop(); }
  });
});
