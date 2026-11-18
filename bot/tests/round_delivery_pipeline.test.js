import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { DurableRoundOutbox } from '../collector/DurableRoundOutbox.js';
import { RoundDeliveryPipeline } from '../collector/RoundDeliveryPipeline.js';

async function fixture(t, limits = {}) {
  const directory = await fs.mkdtemp(path.join(os.tmpdir(), 'round-delivery-'));
  t.after(() => fs.rm(directory, { recursive: true, force: true }));
  return new DurableRoundOutbox({ filePath: path.join(directory, 'events.jsonl'), eventOutbox: true, ...limits });
}

async function until(predicate, message, timeoutMs = 3000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (predicate()) return;
    await new Promise(resolve => setTimeout(resolve, 10));
  }
  assert.fail(message);
}

function round(roundId, multiplier, roundIndex) {
  const observedAt = new Date().toISOString();
  return { round_id: roundId, observation_id: roundId, multiplier, round_index: roundIndex,
    local_round_index: roundIndex, observed_at: observedAt, timestamp: observedAt,
    platform_timestamp: null, platform_round_id: null, platform_round_index: null,
    round_identity_type: 'COLLECTOR_OBSERVATION_HASH', round_index_source: 'LOCAL_SEQUENCE',
    identity_confidence: 'HIGH', continuity_verified: true, gap_before: false };
}

function roundStore({ saveRounds, loadRoundsByIds }) {
  return { pool: {}, usingFileFallback: false, saveRounds, loadRoundsByIds };
}

test('acknowledged recovered rounds progress to storage despite a continuing backlog', () => {
  const pipeline = new RoundDeliveryPipeline({ outbox: {}, roundStore: {} });
  const recovered = { round_id: 'first', delivery_mode: 'RECOVERED', observed_handoff_at: '2026-01-01T00:00:00Z', persistence_status: 'PENDING' };
  const next = { round_id: 'second', delivery_mode: 'RECOVERED', observed_handoff_at: null, persistence_status: 'PENDING' };
  assert.equal(pipeline._nextWork([recovered, next]).stage, 'persistence');
  assert.equal(pipeline._nextWork([{ ...recovered, persistence_status: 'COMMITTED' }, next]).stage, 'persisted');
  assert.equal(pipeline._nextWork([recovered, { ...next, delivery_mode: 'LIVE', observed_at: new Date().toISOString() }]).stage, 'observed');
});

test('fresh live observations bypass recovered backlog while storage stays ordered', () => {
  const pipeline = new RoundDeliveryPipeline({ outbox: {}, roundStore: {} });
  const old = { ...round('old', 2, 1), delivery_mode: 'RECOVERED', persistence_status: 'PENDING' };
  const live = { ...round('live', 3, 2), delivery_mode: 'LIVE', persistence_status: 'PENDING' };
  const work = pipeline._nextWork([old, live]);
  assert.equal(work.stage, 'observed');
  assert.equal(work.records[0].round_id, 'live');
  const next = pipeline._nextWork([old, { ...live, observed_handoff_at: new Date().toISOString() }]);
  assert.equal(next.stage, 'observed');
  assert.equal(next.records[0].round_id, 'old');
});

test('persists recovery and live modes per round while preserving source order', async t => {
  const outbox = await fixture(t);
  const pipeline = new RoundDeliveryPipeline({ outbox, roundStore: {} });
  const rows = [round('older-orbit-round', 2.1, 41), round('current-orbit-round', 2.3, 42)];

  pipeline.enqueue(rows, { deliveryModes: ['RECOVERED', 'LIVE'] });

  assert.deepEqual(outbox.readAll().map(row => [row.round_id, row.delivery_mode]), [
    ['older-orbit-round', 'RECOVERED'],
    ['current-orbit-round', 'LIVE'],
  ]);
  assert.throws(() => pipeline.enqueue(rows, { deliveryModes: ['LIVE'] }), /deliveryModes/);
});

test('round.observed reaches the API before a delayed PostgreSQL write; equal multipliers remain separate', async t => {
  const outbox = await fixture(t);
  const observed = [];
  let collectorTimingSent = [];
  const persisted = [];
  let databaseCommitted = false;
  let canonicalReads = 0;
  const historyProbes = [];
  let releaseCommit;
  const dbWait = new Promise(resolve => { releaseCommit = resolve; });
  const store = roundStore({
    async saveRounds(rows, source) {
      assert.equal(source, 'collector_async');
      await dbWait;
      databaseCommitted = true;
      return { storage_status: 'AVAILABLE', saved: rows.length, insertedRounds: rows,
        commitAcknowledgedAt: new Date().toISOString() };
    },
    async loadRoundsByIds(ids) {
      canonicalReads += 1;
      return ids.map(id => round(id, 1.25, Number(id.slice(1))));
    },
  });
  const pipeline = new RoundDeliveryPipeline({ outbox, roundStore: store,
    probeRoundHistory: async id => {
      historyProbes.push(id);
      return { available: true, status: 'CONFIRMED', checked_at: new Date().toISOString() };
    },
    publishObserved: async (events, { collectorTiming = [] } = {}) => {
      observed.push(...events);
      collectorTimingSent.push(...collectorTiming);
      assert.equal(databaseCommitted, false);
      return { accepted: events.length, api_published_at: new Date().toISOString(),
        collector_api_roundtrip_ms: 2.5, durable_inbox_append_ms: 1.25,
        priority_timings_ms: events.map(event => ({ round_id: event.round_id,
          api_request_to_rules_engine_receive_ms: 3, rules_engine_evaluation_ms: 4,
          rules_complete_to_other_consumer_enqueue_ms: 0.5 })) };
    },
    publishPersisted: async events => {
      persisted.push(...events);
      return { accepted: events.length, events: events.map((event, index) => ({
        observation_id: event.observation_id, committed_sequence: index + 1,
      })) };
    },
  });
  t.after(() => pipeline.stop());
  const mutationObservedAt = new Date(Date.now() - 100).toISOString();
  const extractionStartedAt = new Date(Date.now() - 20).toISOString();
  const extractedAt = new Date(Date.now() - 10).toISOString();
  const receivedAt = new Date().toISOString();
  const receiveMonotonic = performance.now();
  await new Promise(resolve => setTimeout(resolve, 5));
  const validationCompletedAt = new Date().toISOString();
  const validationMonotonic = performance.now();
  pipeline.enqueue([round('r1', 1.25, 1), round('r2', 1.25, 2)], { timingsByRoundId: { r2: {
    mutationObservedAt, extractionStartedAt, extractedAt,
    pageMutationToExtractionMs: 9.5, pageExtractionMs: 1.25,
    nodeReceivedAt: receivedAt, nodeReceivedMonotonicMs: receiveMonotonic,
    normalizedAt: receivedAt, validationCompletedAt,
    validationMonotonicMs: validationMonotonic,
  } } });
  void pipeline.start({ recovered: false });
  await until(() => observed.length === 2, 'observed stage was not published');
  await new Promise(resolve => setTimeout(resolve, 125));
  assert.equal(databaseCommitted, false);
  assert.equal(outbox.readAll().length, 2);
  releaseCommit();
  await until(() => persisted.length === 2 && outbox.readAll().length === 0,
    'committed stage was not handed off and removed');
  await until(() => pipeline.snapshot().last_round_timing?.round_history_api_visibility === 'CONFIRMED',
    'Round History API visibility was not measured');
  assert.equal(persisted[0].round_id, 'r1');
  assert.equal(persisted[1].round_id, 'r2');
  assert.equal(persisted[0].multiplier, persisted[1].multiplier);
  assert.equal(canonicalReads, 0, 'newly inserted rows use their canonical INSERT RETURNING result');
  const latency = pipeline.snapshot().latency_ms;
  assert.equal(latency.observation_to_durable_append_ms.samples, 1);
  assert.equal(latency.local_append_to_api_receipt_estimate_ms, undefined,
    'collector-to-backend one-way latency is not guessed from unrelated process spans');
  assert.equal(latency.page_mutation_to_multiplier_extraction_ms.samples, 1,
    'one DOM observation is attributed only to the newest recovered row');
  assert.equal(latency.page_extraction_ms.samples, 1);
  assert.ok(latency.node_receive_to_validation_ms.p99_ms >= 1);
  assert.equal(latency.validation_to_api_dispatch_ms.samples, 1);
  assert.equal(latency.api_request_to_rules_engine_receive_ms.samples, 2);
  assert.equal(latency.rules_engine_evaluation_ms.samples, 2);
  assert.equal(latency.rules_complete_to_other_consumer_enqueue_ms.samples, 2);
  assert.equal(latency.api_durable_inbox_append_ms.p99_ms, 1.25);
  assert.equal(latency.collector_http_roundtrip_to_api_ack_ms.samples, 2,
    'the observed-stage worker dispatches one priority round per API request');
  assert.equal(collectorTimingSent.length, 2);
  assert.equal(collectorTimingSent.find(item => item.round_id === 'r1').page_extraction_ms, null);
  assert.equal(collectorTimingSent[0].page_to_node_receive_ms, null);
  assert.ok(latency.durable_append_to_api_receipt_ms.p99_ms >= 0);
  assert.ok(latency.observation_to_postgres_commit_ms.p95_ms >= 125);
  assert.deepEqual(historyProbes, ['r2']);
  assert.equal(pipeline.snapshot().last_round_timing.round_history_api_visibility, 'CONFIRMED');
  assert.ok(pipeline.snapshot().last_round_timing.timestamps.database_commit_acknowledged_at);
  assert.ok(pipeline.snapshot().last_round_timing.timestamps.round_history_api_visible_at);
  assert.equal(latency.database_commit_to_history_api_visible_ms.samples, 1);
  t.diagnostic(`isolated delayed-store latency ${JSON.stringify(latency)}`);
  await pipeline.stop();
});

test('duplicate round identity is canonically read and validated after an idempotent insert conflict', async t => {
  const outbox = await fixture(t);
  const reads = [];
  const persisted = [];
  const pipeline = new RoundDeliveryPipeline({ outbox,
    roundStore: roundStore({
      async saveRounds() { return { storage_status: 'AVAILABLE', saved: 0, insertedRounds: [],
        commitAcknowledgedAt: new Date().toISOString() }; },
      async loadRoundsByIds(ids) { reads.push([...ids]); return ids.map(id => round(id, 1.8, 4)); },
    }),
    publishObserved: async events => ({ accepted: events.length }),
    publishPersisted: async events => { persisted.push(...events); return { accepted: events.length,
      events: events.map(event => ({ observation_id: event.observation_id, committed_sequence: 1 })) }; },
  });
  t.after(() => pipeline.stop());
  pipeline.enqueue([round('duplicate-round', 1.8, 4)]);
  void pipeline.start({ recovered: false });
  await until(() => outbox.readAll().length === 0, 'duplicate canonical row was not handed off');

  assert.deepEqual(reads, [['duplicate-round']]);
  assert.equal(persisted.length, 1);
});

test('recovered commit without a monotonic start keeps commit latency unavailable', async t => {
  const outbox = await fixture(t);
  outbox.appendMany([round('recovered-without-clock', 2.1, 1)]);
  outbox.markDatabaseCommittedMany([{
    round_id: 'recovered-without-clock',
    round: { round_id: 'recovered-without-clock', multiplier: 2.1 },
    commitAcknowledgedAt: new Date().toISOString(),
    observationToCommitMs: null,
  }]);

  assert.equal(outbox.readAll()[0].observation_to_commit_ms, null);
});

test('worker compacts an over-limit journal before restart recovery and completes delivery', async t => {
  const outbox = await fixture(t, { maxBytes: 4096 });
  const sourceRound = round('startup-compaction', 2.8, 1);
  outbox.appendMany([sourceRound]);
  outbox.markObservedHandoff([sourceRound.round_id], new Date().toISOString());
  outbox.markDatabaseCommittedMany([{
    round_id: sourceRound.round_id,
    round: { ...sourceRound, stored_at: new Date().toISOString() },
    commitAcknowledgedAt: new Date().toISOString(),
  }]);

  // Model a journal produced by the pre-fix writer: retained state remains
  // identical, while old update lines have grown the file beyond its limit.
  const retained = outbox.readAll()[0];
  const historyLines = Array.from({ length: 30 }, (_, attempt) => JSON.stringify({
    op: 'update', records: [{ ...retained, persisted_attempts: attempt,
      persisted_last_error: `legacy timeout ${attempt} ${'x'.repeat(220)}` }],
  })).join('\n') + '\n';
  await fs.appendFile(outbox.filePath, historyLines);
  const overLimitBytes = (await fs.stat(outbox.filePath)).size;
  assert.ok(overLimitBytes > outbox.stats().journal_capacity_bytes);

  const restartedOutbox = new DurableRoundOutbox({ filePath: outbox.filePath,
    eventOutbox: true, maxBytes: 4096 });
  const delivered = [];
  const pipeline = new RoundDeliveryPipeline({ outbox: restartedOutbox,
    roundStore: roundStore({
      async saveRounds() { assert.fail('committed record must not be written to PostgreSQL again'); },
      async loadRoundsByIds() { return []; },
    }),
    publishObserved: async () => assert.fail('already acknowledged observation must not be republished'),
    publishPersisted: async events => {
      delivered.push(...events);
      assert.ok(restartedOutbox.stats().backlog_bytes < overLimitBytes);
      assert.equal(events[0].delivery_mode, 'RECOVERED');
      return { accepted: events.length, events: [{ observation_id: sourceRound.round_id,
        committed_sequence: 9001 }] };
    },
  });
  t.after(() => pipeline.stop());
  void pipeline.start({ recovered: true });
  await until(() => delivered.length === 1 && restartedOutbox.readAll().length === 0,
    'worker did not compact and recover the retained committed event');
  assert.equal(delivered[0].event_id, 'aviator-round:startup-compaction:observed');
  assert.equal(delivered[0].committed_sequence, null,
    'the existing event identity survives compaction; sequence is attached by the receipt');
  await pipeline.stop();
});

test('collector outbox capacity fails explicitly and keeps earlier records intact', async t => {
  const outbox = await fixture(t, { maxRecords: 1 });
  const pipeline = new RoundDeliveryPipeline({ outbox, roundStore: roundStore({
    async saveRounds() { throw new Error('not reached'); }, async loadRoundsByIds() { return []; },
  }), publishObserved: async () => ({ accepted: 0 }), publishPersisted: async () => ({ accepted: 0 }) });
  t.after(() => pipeline.stop());
  pipeline.enqueue([round('capacity-first', 1.2, 1)]);
  assert.throws(() => pipeline.enqueue([round('capacity-second', 1.2, 2)]), error => error.code === 'OUTBOX_CAPACITY');
  assert.deepEqual(outbox.readAll().map(record => record.round_id), ['capacity-first']);
  await pipeline.stop();
});

test('backend outage retains durable observations and marks their later delivery RECOVERED', async t => {
  const outbox = await fixture(t);
  let apiOnline = false;
  let databaseCalls = 0;
  let observedMode = null;
  let persistedMode = null;
  const pipeline = new RoundDeliveryPipeline({ outbox,
    roundStore: roundStore({
      async saveRounds(rows) { databaseCalls += 1; return { storage_status: 'AVAILABLE', saved: rows.length,
        insertedRounds: rows, commitAcknowledgedAt: new Date().toISOString() }; },
      async loadRoundsByIds(ids) { return ids.map(id => round(id, 2.2, 1)); },
    }),
    publishObserved: async events => {
      if (!apiOnline) throw Object.assign(new Error('API offline'), { category: 'BACKEND_EVENT_UNAVAILABLE' });
      observedMode = events[0].delivery_mode;
      return { accepted: events.length, api_published_at: new Date().toISOString() };
    },
    publishPersisted: async events => {
      persistedMode = events[0].delivery_mode;
      return { accepted: events.length, events: [{ observation_id: events[0].observation_id, committed_sequence: 1 }] };
    },
  });
  t.after(() => pipeline.stop());
  pipeline.enqueue([round('offline-round', 2.2, 1)]);
  void pipeline.start({ recovered: false });
  await until(() => pipeline.snapshot().last_failure?.stage === 'observed', 'API outage was not recorded');
  assert.equal(databaseCalls, 0);
  assert.equal(outbox.readAll().length, 1);
  assert.equal(outbox.readAll()[0].persistence_status, 'PENDING');
  apiOnline = true;
  await until(() => outbox.readAll().length === 0, 'retained event did not recover after API returned');
  assert.equal(observedMode, 'RECOVERED');
  assert.equal(persistedMode, 'RECOVERED');
  assert.equal(databaseCalls, 1);
  await pipeline.stop();
});

test('PostgreSQL outage is recorded after immediate observed publication and later drains safely', async t => {
  const outbox = await fixture(t);
  let databaseAvailable = false;
  const observed = [];
  let persisted = 0;
  const pipeline = new RoundDeliveryPipeline({ outbox,
    roundStore: roundStore({
      async saveRounds(rows) {
        if (!databaseAvailable) throw Object.assign(new Error('isolated PostgreSQL unavailable'), {
          category: 'POSTGRES_UNAVAILABLE',
        });
        return { storage_status: 'AVAILABLE', saved: rows.length, insertedRounds: rows,
          commitAcknowledgedAt: new Date().toISOString() };
      },
      async loadRoundsByIds(ids) { return ids.map(id => round(id, 2.4, 1)); },
    }),
    publishObserved: async events => { observed.push(...events); return { accepted: events.length }; },
    publishPersisted: async events => { persisted += events.length;
      return { accepted: events.length, events: [{ observation_id: events[0].observation_id, committed_sequence: 12 }] };
    },
  });
  t.after(() => pipeline.stop());
  pipeline.enqueue([round('pg-outage', 2.4, 1)]);
  void pipeline.start({ recovered: false });
  await until(() => pipeline.snapshot().last_failure?.stage === 'persistence',
    'PostgreSQL outage was not recorded');
  assert.equal(observed.length, 1);
  assert.equal(outbox.readAll()[0].persistence_status, 'PENDING');
  assert.equal(persisted, 0);
  databaseAvailable = true;
  await until(() => outbox.readAll().length === 0, 'PostgreSQL recovery did not drain the durable observation');
  assert.equal(persisted, 1);
  await pipeline.stop();
});

test('committed local outbox survives a failed persisted handoff and retries idempotently', async t => {
  const outbox = await fixture(t);
  let dbRowExists = false;
  let observedCalls = 0;
  let persistedCalls = 0;
  const store = roundStore({
    async saveRounds(rows) {
      dbRowExists = true;
      return { storage_status: 'AVAILABLE', saved: 1, insertedRounds: rows,
        commitAcknowledgedAt: new Date().toISOString() };
    },
    async loadRoundsByIds(ids) {
      assert.equal(dbRowExists, true);
      return ids.map(id => round(id, 3.1, 1));
    },
  });
  const first = new RoundDeliveryPipeline({ outbox, roundStore: store,
    publishObserved: async events => { observedCalls += 1; return { accepted: events.length }; },
    publishPersisted: async () => { persistedCalls += 1; throw new Error('API crashed after PostgreSQL commit'); },
  });
  t.after(() => first.stop());
  first.enqueue([round('commit-before-publish', 3.1, 1)]);
  void first.start({ recovered: false });
  await until(() => first.snapshot().last_failure?.stage === 'persisted', 'persisted handoff failure was not retried');
  assert.equal(outbox.readAll()[0].persistence_status, 'COMMITTED');
  await first.stop();
  const bytesBeforeCompaction = outbox.stats().backlog_bytes;
  outbox.compactEventJournal();
  assert.ok(outbox.stats().backlog_bytes <= bytesBeforeCompaction);
  assert.equal(outbox.readAll()[0].persistence_status, 'COMMITTED',
    'failed handoff remains durable after compaction');

  const restartedOutbox = new DurableRoundOutbox({ filePath: outbox.filePath, eventOutbox: true });
  assert.equal(restartedOutbox.readAll()[0].persistence_status, 'COMMITTED');
  const recoveredEvents = [];
  const restarted = new RoundDeliveryPipeline({ outbox: restartedOutbox, roundStore: store,
    publishObserved: async events => { observedCalls += 1; return { accepted: events.length }; },
    publishPersisted: async events => {
      persistedCalls += 1; recoveredEvents.push(...events);
      return { accepted: events.length, events: [{ observation_id: events[0].observation_id, committed_sequence: 77 }] };
    },
  });
  t.after(() => restarted.stop());
  void restarted.start({ recovered: true });
  await until(() => restartedOutbox.readAll().length === 0, 'restart did not hand off committed event');
  assert.equal(observedCalls, 1);
  assert.equal(persistedCalls, 2);
  assert.equal(recoveredEvents[0].delivery_mode, 'RECOVERED');
  assert.equal(recoveredEvents[0].commit_acknowledged_at != null, true);
  await restarted.stop();
});

test('a persisted-stage retry cannot block observed publication of later rounds', async t => {
  const outbox = await fixture(t);
  const observedIds = [];
  let persistedCalls = 0;
  let releaseRetry;
  const retryGate = new Promise(resolve => { releaseRetry = resolve; });
  const pipeline = new RoundDeliveryPipeline({ outbox,
    roundStore: roundStore({
      async saveRounds(rows) { return { storage_status: 'AVAILABLE', saved: rows.length,
        insertedRounds: rows, commitAcknowledgedAt: new Date().toISOString() }; },
      async loadRoundsByIds(ids) { return ids.map(id => round(id,
        id === 'next-observed' ? 1.6 : 1.4, id === 'next-observed' ? 2 : 1)); },
    }),
    publishObserved: async events => { observedIds.push(...events.map(event => event.round_id));
      return { accepted: events.length, api_published_at: new Date().toISOString() }; },
    publishPersisted: async events => {
      persistedCalls += 1;
      if (persistedCalls === 1) throw new Error('isolated committed-stage timeout');
      await retryGate;
      return { accepted: events.length, events: events.map(event => ({
        observation_id: event.observation_id, committed_sequence: 50 + persistedCalls,
      })) };
    },
  });
  t.after(async () => { releaseRetry(); await pipeline.stop(); });
  pipeline.enqueue([round('first-committed', 1.4, 1)]);
  void pipeline.start({ recovered: false });
  await until(() => pipeline.snapshot().last_failure?.stage === 'persisted',
    'initial committed-stage failure was not recorded');
  assert.equal(outbox.readAll()[0].persistence_status, 'COMMITTED');

  pipeline.enqueue([round('next-observed', 1.6, 2)]);
  await until(() => observedIds.includes('next-observed'),
    'new observed event waited behind a committed-stage retry');
  assert.equal(persistedCalls, 1, 'the second persisted request should still be waiting on retry backoff');
  assert.deepEqual(observedIds, ['first-committed', 'next-observed']);

  releaseRetry();
  await until(() => outbox.readAll().length === 0, 'outbox did not finish after the committed-stage retry recovered');
});
