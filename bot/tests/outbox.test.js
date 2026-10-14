import test from 'node:test';
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { DurableRoundOutbox } from '../collector/DurableRoundOutbox.js';
import { PostgresRoundStore } from '../collector/PostgresRoundStore.js';

function fixture(options = {}) {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'winner-outbox-'));
  const outbox = new DurableRoundOutbox({ filePath: path.join(directory, 'outbox.json'), ...options });
  return { directory, outbox };
}

function eventFixture(options = {}) {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'winner-event-outbox-'));
  const filePath = path.join(directory, 'events.jsonl');
  const outbox = new DurableRoundOutbox({ filePath, eventOutbox: true, ...options });
  return { directory, filePath, outbox };
}

function runNode(code) {
  return new Promise((resolve, reject) => {
    const child = spawn(process.execPath, ['--input-type=module', '-e', code], {
      stdio: ['ignore', 'ignore', 'pipe'],
    });
    let stderr = '';
    child.stderr.on('data', chunk => { stderr += chunk.toString(); });
    child.once('error', reject);
    child.once('close', code => code === 0 ? resolve() : reject(new Error(stderr || `child exited ${code}`)));
  });
}

test('outbox deduplicates repeated round IDs but retains legitimate equal multipliers', () => {
  const { directory, outbox } = fixture();
  try {
    const rows = [
      { round_id: 'real-1', round_index: 1, multiplier: 2.5, observed_at: '2026-10-06T10:00:00Z' },
      { round_id: 'real-2', round_index: 2, multiplier: 2.5, observed_at: '2026-10-06T10:01:00Z' },
    ];
    outbox.appendMany(rows);
    outbox.appendMany([rows[0]]);
    assert.equal(outbox.stats().backlog_size, 2);
    assert.deepEqual(outbox.readAll().map(row => row.round_id), ['real-1', 'real-2']);
  } finally { fs.rmSync(directory, { recursive: true, force: true }); }
});

test('outbox capacity refuses new rows without discarding existing durable records', () => {
  const { directory, outbox } = fixture({ maxRecords: 1, maxBytes: 4096 });
  try {
    const first = { round_id: 'kept', round_index: 1, multiplier: 1.25 };
    outbox.appendMany([first]);
    assert.throws(() => outbox.appendMany([
      { round_id: 'not-saved', round_index: 2, multiplier: 3 },
    ]), error => error.code === 'OUTBOX_CAPACITY');
    assert.deepEqual(outbox.readAll().map(row => row.round_id), ['kept']);
  } finally { fs.rmSync(directory, { recursive: true, force: true }); }
});

test('event journal compacts repeated state updates without dropping retained delivery stages', () => {
  const { directory, filePath, outbox } = eventFixture({ maxBytes: 16 * 1024 });
  try {
    outbox.appendMany([
      { round_id: 'pending-observed', round_index: 1, multiplier: 1.7, large_evidence: 'p'.repeat(900) },
      { round_id: 'committed-undelivered', round_index: 2, multiplier: 2.4, large_evidence: 'c'.repeat(900) },
    ]);
    outbox.markObservedHandoff(['committed-undelivered'], new Date().toISOString());
    outbox.markDatabaseCommittedMany([{
      round_id: 'committed-undelivered',
      round: { round_id: 'committed-undelivered', round_index: 2, multiplier: 2.4 },
      commitAcknowledgedAt: new Date().toISOString(),
    }]);
    const ids = new Map(outbox.readAll().map(record => [record.round_id, record.event_id]));

    for (let attempt = 1; attempt <= 80; attempt += 1) {
      outbox.markStageFailure(['committed-undelivered'], 'persisted',
        new Error(`simulated handoff timeout ${attempt}`), attempt,
        new Date(Date.now() + attempt * 1000).toISOString());
    }
    // The old markRecovered path appended a full update and failed at this
    // point. Startup recovery now folds that change into one atomic snapshot.
    outbox.markRecovered();
    const stats = outbox.stats();
    assert.ok(stats.backlog_bytes < stats.journal_capacity_bytes);
    assert.equal(outbox.readAll().length, 2);
    assert.equal(outbox.readAll()[0].delivery_mode, 'RECOVERED');
    assert.equal(outbox.readAll()[1].persistence_status, 'COMMITTED');
    assert.equal(outbox.readAll()[1].persisted_handoff_at, null);
    assert.equal(outbox.readAll()[1].event_id, ids.get('committed-undelivered'));

    const restarted = new DurableRoundOutbox({ filePath, eventOutbox: true, maxBytes: 16 * 1024 });
    restarted.appendMany([{ round_id: 'pending-observed', round_index: 1, multiplier: 1.7 }]);
    assert.equal(restarted.readAll().length, 2, 'compaction and restart keep event-ID deduplication');
    assert.equal(restarted.readAll()[0].event_id, ids.get('pending-observed'));
  } finally { fs.rmSync(directory, { recursive: true, force: true }); }
});

test('event journal repairs a torn final append from the durable prefix on restart', () => {
  const { directory, filePath, outbox } = eventFixture();
  try {
    outbox.appendMany([{ round_id: 'durable-prefix', round_index: 1, multiplier: 1.3 }]);
    fs.appendFileSync(filePath, '{"op":"update","records":[{"round_id":"partial');

    const restarted = new DurableRoundOutbox({ filePath, eventOutbox: true });
    assert.deepEqual(restarted.readAll().map(record => record.round_id), ['durable-prefix']);
    restarted.markRecovered();
    const repaired = new DurableRoundOutbox({ filePath, eventOutbox: true });
    assert.deepEqual(repaired.readAll().map(record => record.round_id), ['durable-prefix']);
    assert.ok(fs.readFileSync(filePath, 'utf8').endsWith('\n'));
  } finally { fs.rmSync(directory, { recursive: true, force: true }); }
});

test('event outbox backpressure keeps existing records when the active-data limit is reached', () => {
  const { directory, outbox } = eventFixture({ maxBytes: 2048 });
  try {
    outbox.appendMany([{ round_id: 'retained-at-capacity', round_index: 1, multiplier: 1.9 }]);
    assert.throws(() => outbox.appendMany([{
      round_id: 'too-large-to-queue', round_index: 2, multiplier: 2.3, evidence: 'x'.repeat(3000),
    }]), error => error.code === 'OUTBOX_CAPACITY');
    assert.deepEqual(outbox.readAll().map(record => record.round_id), ['retained-at-capacity']);
  } finally { fs.rmSync(directory, { recursive: true, force: true }); }
});

test('event outbox refuses removal until PostgreSQL commit and persisted-event acknowledgement', () => {
  const { directory, outbox } = eventFixture();
  try {
    outbox.appendMany([{ round_id: 'must-stay', round_index: 1, multiplier: 2.2 }]);
    outbox.removePersisted(['must-stay']);
    assert.equal(outbox.readAll().length, 1, 'pending observations are retained');

    outbox.markDatabaseCommittedMany([{
      round_id: 'must-stay', round: { round_id: 'must-stay', round_index: 1, multiplier: 2.2 },
      commitAcknowledgedAt: new Date().toISOString(),
    }]);
    outbox.removePersisted(['must-stay']);
    assert.equal(outbox.readAll().length, 1, 'committed rows remain until event handoff acknowledgement');

    outbox.markPersistedHandoff(['must-stay'], [{ observation_id: 'must-stay', committed_sequence: 8 }]);
    outbox.removePersisted(['must-stay']);
    assert.equal(outbox.readAll().length, 0);
  } finally { fs.rmSync(directory, { recursive: true, force: true }); }
});

test('event outbox reclaims a dead writer lock after a crash', () => {
  const { directory, filePath, outbox } = eventFixture();
  try {
    const lockPath = `${filePath}.lock`;
    fs.mkdirSync(lockPath);
    fs.writeFileSync(path.join(lockPath, 'owner.json'), JSON.stringify({
      pid: 2_147_000_000, hostname: os.hostname(), process_start: 'dead-process', token: 'stale',
    }));
    outbox.appendMany([{ round_id: 'after-crashed-writer', round_index: 1, multiplier: 1.6 }]);
    assert.deepEqual(outbox.readAll().map(record => record.round_id), ['after-crashed-writer']);
    assert.equal(fs.existsSync(lockPath), false);
  } finally { fs.rmSync(directory, { recursive: true, force: true }); }
});

test('event journal serializes concurrent processes and preserves duplicate prevention through compaction', async () => {
  const { directory, filePath, outbox } = eventFixture();
  try {
    const moduleUrl = pathToFileURL(path.resolve(path.dirname(fileURLToPath(import.meta.url)),
      '../collector/DurableRoundOutbox.js')).href;
    const makeChild = label => `
      import { DurableRoundOutbox } from ${JSON.stringify(moduleUrl)};
      const outbox = new DurableRoundOutbox({ filePath: ${JSON.stringify(filePath)}, eventOutbox: true });
      outbox.appendMany([
        { round_id: 'shared', round_index: 1, multiplier: 2.1 },
        { round_id: ${JSON.stringify(`${label}-one`)}, round_index: 2, multiplier: 1.4 },
        { round_id: ${JSON.stringify(`${label}-two`)}, round_index: 3, multiplier: 1.4 },
      ]);
      outbox.compactEventJournal();
    `;
    await Promise.all([runNode(makeChild('first')), runNode(makeChild('second'))]);
    const records = outbox.readAll();
    assert.deepEqual(new Set(records.map(record => record.round_id)),
      new Set(['shared', 'first-one', 'first-two', 'second-one', 'second-two']));
    assert.equal(records.filter(record => record.round_id === 'shared').length, 1);
    assert.equal(records.find(record => record.round_id === 'shared').event_id, 'aviator-round:shared:observed');
  } finally { fs.rmSync(directory, { recursive: true, force: true }); }
});

test('PostgreSQL recovery replays queued rows before new writes and removes only committed IDs', async () => {
  const { directory, outbox } = fixture();
  try {
    outbox.appendMany([{ round_id: 'queued', round_index: 1, multiplier: 1.8 }]);
    const store = new PostgresRoundStore();
    store.outbox = outbox;
    store.pool = {};
    const writes = [];
    store._savePostgresRounds = async (rows, source) => {
      writes.push({ ids: rows.map(row => row.round_id), source });
      return { saved: rows.length };
    };
    const result = await store.saveRounds([
      { round_id: 'new', round_index: 2, multiplier: 2.1 },
    ]);
    assert.deepEqual(writes.map(write => write.ids), [['queued'], ['new']]);
    assert.equal(writes[0].source, 'collector_outbox_replay');
    assert.equal(outbox.stats().backlog_size, 0);
    assert.equal(result.storage_status, 'AVAILABLE');
  } finally { fs.rmSync(directory, { recursive: true, force: true }); }
});

test('storage outage durably queues an observation and reports backlog', async () => {
  const { directory, outbox } = fixture();
  try {
    const store = new PostgresRoundStore();
    store.outbox = outbox;
    store.storageRetryAt = Number.MAX_SAFE_INTEGER;
    store.storageError = new Error('simulated PostgreSQL outage');
    const result = await store.saveRounds([
      { round_id: 'outage-round', round_index: 11, multiplier: 1.2,
        observed_at: '2026-10-06T10:00:00Z' },
    ]);
    assert.equal(result.saved, 0);
    assert.equal(result.queued, 1);
    assert.equal(result.storage_status, 'UNAVAILABLE');
    assert.equal(outbox.readAll()[0].round_id, 'outage-round');
  } finally { fs.rmSync(directory, { recursive: true, force: true }); }
});
