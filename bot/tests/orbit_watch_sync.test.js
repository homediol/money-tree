import test from 'node:test';
import assert from 'node:assert/strict';
import { OrbitWatchSync, streamCursorCoversReading } from '../collector/OrbitWatchSync.js';

const WATCH_ID = 'cccccccc-cccc-4ccc-8ccc-cccccccccccc';
const SELECTOR = '.payouts-block .payout';
const watch = { id: WATCH_ID, status: 'running', collectorStatus: 'running', engine: 'connected-chrome', targetId: 'c', tabId: 1,
  frameId: 0, frameUrl: 'https://aviator-next.spribegaming.com/', selector: SELECTOR };
const reading = id => ({ id, checkedAt: '2026-01-01T00:00:00Z', count: 1,
  items: [{ index: 0, value: '1.16x', visible: true }] });
const payoutReading = id => ({ id, checkedAt: '2026-01-01T00:00:00Z', count: 12,
  items: Array.from({ length: 12 }, (_, index) => ({ index, value: `${index + 1}.10x`, visible: true })) });

test('stream reading is considered recovered only after the committed Orbit cursor reaches its event ID', () => {
  assert.equal(streamCursorCoversReading('10', '10'), true);
  assert.equal(streamCursorCoversReading('12', '10'), true);
  assert.equal(streamCursorCoversReading('9', '10'), false);
  assert.equal(streamCursorCoversReading('invalid', '10'), false);
});

test('live Orbit reading commits through the latest-only stream path without fetching history', async () => {
  const calls = [];
  const eventReading = { watchId: WATCH_ID, selector: SELECTOR, id: '2', checkedAt: '2026-01-01T00:00:02Z', count: 1,
    items: [{ index: 0, value: '1.50x', visible: true, historyContainer: 'div#history.payouts-block' }] };
  let committed;
  const store = {
    async getState() { return { watchId: WATCH_ID, cursor: '1', snapshot: {
      version: 2, visibleMultipliers: [1.25], sourceIdentity: { selector: SELECTOR, historyContainer: 'div#history.payouts-block' },
    } }; },
    async commitPage(page) { calls.push('commit'); committed = page; },
    async ackPendingRounds(ids) { calls.push('ack'); assert.deepEqual(ids, ['orbit-new-round']); },
  };
  const client = {
    async latest(id) { calls.push('latest'); assert.equal(id, WATCH_ID); return {
      watch: { id: WATCH_ID, selector: SELECTOR, status: 'running' }, latest: eventReading, cursor: '2',
    }; },
    async history() { calls.push('history'); throw new Error('live events must not replay history pages'); },
  };
  const sync = new OrbitWatchSync({ client, store,
    onPage: async ({ entries, state }) => {
      calls.push('decide');
      assert.equal(entries[0].reading.id, '2');
      assert.equal(state.cursor, '1');
      return { snapshot: { version: 2, visibleMultipliers: [1.5, 1.25] },
        candidates: [{ round_id: 'orbit-new-round', round_index: 2, multiplier: 1.5 }], deliveryModes: ['LIVE'] };
    },
    onPagePrepared: async () => calls.push('prepared'),
    onPageCommitted: async () => calls.push('committed'),
  });

  const result = await sync.syncStreamReading({ watchId: WATCH_ID, reading: eventReading, watchHealthy: true });

  assert.equal(result.cursor, '2');
  assert.deepEqual(calls, ['committed', 'latest', 'decide', 'commit', 'prepared', 'committed', 'ack']);
  assert.deepEqual(committed.entries.map(entry => entry.reading.id), ['2']);
  assert.deepEqual(committed.pendingRounds.map(round => round.round_id), ['orbit-new-round']);
});

test('live overlap ambiguity leaves the cursor untouched so the collector can replay full history', async () => {
  let commits = 0;
  const eventReading = { ...reading('2'), watchId: WATCH_ID, selector: SELECTOR,
    projection: 'LATEST_OVERLAP_WINDOW', windowSize: 12 };
  const store = {
    async getState() { return { watchId: WATCH_ID, cursor: '1', snapshot: {
      version: 2, visibleMultipliers: [1.25], sourceIdentity: { selector: SELECTOR },
    } }; },
    async commitPage() { commits += 1; },
  };
  const client = { async latest() { return {
    watch: { id: WATCH_ID, selector: SELECTOR, status: 'running' }, latest: eventReading, cursor: '2',
  }; } };
  const sync = new OrbitWatchSync({ client, store, onPage: async () => ({
    snapshot: { version: 2, visibleMultipliers: null, gapPending: true },
    candidates: [], deliveryModes: [], recoveryRequired: true,
  }) });

  await assert.rejects(sync.syncStreamReading({ watchId: WATCH_ID, reading: eventReading, watchHealthy: true }),
    error => error.code === 'STREAM_ROUND_RECOVERY_REQUIRED');
  assert.equal(commits, 0, 'the unverified live cursor is not advanced before history recovery');
});

test('saved watch snapshot and discovery run concurrently after pending recovery', async () => {
  const calls = [];
  let releaseWatches;
  const client = {
    listWatches() { calls.push('watches'); return new Promise(resolve => { releaseWatches = resolve; }); },
    async latest(_id, options) { calls.push('latest'); assert.equal(options.windowSize, 12);
      return { watch: { id: WATCH_ID, selector: SELECTOR }, latest: reading('1'), cursor: '1' }; },
    async history(_id, after) { calls.push('history'); return { watchId: WATCH_ID, selector: SELECTOR, readings: [], nextCursor: after, hasMore: false }; },
  };
  const store = { async getState() { return { watchId: WATCH_ID, cursor: '1' }; },
    async commitPage() { calls.push('commit'); } };
  const task = new OrbitWatchSync({ client, store, onPageCommitted: async () => { calls.push('pending'); } }).sync();
  await new Promise(resolve => setImmediate(resolve));
  assert.deepEqual(calls, ['pending', 'watches', 'latest']);
  releaseWatches({ watches: [watch] });
  await task;
  assert.deepEqual(calls, ['pending', 'watches', 'latest', 'history', 'commit', 'pending']);
});

test('archives all pages and resumes from the committed cursor after failure', async () => {
  let state = null;
  const archived = [];
  const store = {
    async getState() { return state; },
    async commitPage(page) {
      assert.equal(page.expectedCursor, state?.cursor || '0');
      archived.push(...page.entries);
      state = { watchId: page.watchId, cursor: page.nextCursor };
    },
  };
  let failed = false;
  const client = {
    async listWatches() { return { watches: [watch] }; },
    async latest() { return { watch: { id: WATCH_ID, selector: SELECTOR }, latest: reading('2'), cursor: '2' }; },
    async history(_id, after) {
      if (after === '0') return { watchId: WATCH_ID, selector: SELECTOR, readings: [reading('1')], nextCursor: '1', hasMore: true };
      if (!failed) { failed = true; throw new Error('temporary failure'); }
      return { watchId: WATCH_ID, selector: SELECTOR, readings: [reading('2')], nextCursor: '2', hasMore: false };
    },
  };
  await assert.rejects(new OrbitWatchSync({ client, store, watchId: WATCH_ID }).sync());
  assert.equal(state.cursor, '1');
  const resumed = await new OrbitWatchSync({ client, store, watchId: WATCH_ID }).sync();
  assert.equal(resumed.cursor, '2');
  assert.deepEqual(archived.map(item => item.reading.id), ['1', '2']);
});

test('archives malformed readings with a visible validation error', async () => {
  let saved;
  const client = { async listWatches() { return { watches: [watch] }; },
    async latest() { return { watch: { id: WATCH_ID, selector: SELECTOR },
      latest: { ...reading('1'), items: [{ index: 0, value: 'bad', visible: true }] }, cursor: '1' }; },
    async history() { return { watchId: WATCH_ID, selector: SELECTOR, nextCursor: '1', hasMore: false,
      readings: [{ ...reading('1'), items: [{ index: 0, value: 'bad', visible: true }] }] }; } };
  const store = { async getState() { return null; }, async commitPage(page) { saved = page; } };
  const result = await new OrbitWatchSync({ client, store, watchId: WATCH_ID }).sync();
  assert.equal(saved.entries[0].error, 'INVALID_MULTIPLIER');
  assert.equal(saved.entries[0].reading.items[0].value, 'bad');
  assert.equal(result.latestReadingError, 'INVALID_MULTIPLIER');
});

test('reports the current Orbit reading when selector results are capped', async () => {
  const partial = { ...reading('1'), count: 120 };
  const client = {
    async listWatches() { return { watches: [watch] }; },
    async latest() { return { watch: { id: WATCH_ID, selector: SELECTOR }, latest: partial, cursor: '1' }; },
    async history() { return { watchId: WATCH_ID, selector: SELECTOR, readings: [partial], nextCursor: '1', hasMore: false }; },
  };
  const store = { async getState() { return null; }, async commitPage() {} };
  const result = await new OrbitWatchSync({ client, store }).sync();
  assert.equal(result.latestReadingTruncated, true);
  assert.equal(result.latestReadingMatchCount, 120);
  assert.equal(result.latestReadingReturnedCount, 1);
  assert.equal(result.latestReadingError, null);
});

test('repairs newly parseable archived readings without resetting the cursor', async () => {
  const beforeMultipliers = Array.from({ length: 11 }, (_, index) => index + 1);
  const before = { id: '1', checkedAt: '2026-01-01T00:00:00Z', count: 11,
    items: beforeMultipliers.map((value, index) => ({ index, value: `${value}.00x`, visible: true })) };
  const repaired = { id: '2', checkedAt: '2026-01-01T00:00:01Z', count: 12,
    items: [{ index: 0, value: '1.50x', visible: true },
      ...beforeMultipliers.slice(0, 10).map((value, index) => ({ index: index + 1, value: `${value}.00x`, visible: true })),
      { index: 11, value: '4,612.47x', visible: false }] };
  let repairCalls = 0;
  let committed;
  let updated;
  const store = {
    async getState() { return { watchId: WATCH_ID, cursor: '2', snapshot: { version: 2, visibleMultipliers: null, gapPending: true } }; },
    async getInvalidReadings() { return [{ readingId: '2', reading: repaired, validationError: 'INVALID_MULTIPLIER' }]; },
    async getLatestVisibleSnapshotBefore(_watchId, readingId) {
      assert.equal(readingId, '2');
      return { readingId: '1', multipliers: beforeMultipliers };
    },
    async getArchivedReadings(_watchId, after, through) {
      assert.equal(after, '1');
      assert.equal(through, '2');
      return [{ readingId: '2', reading: repaired, validationError: 'INVALID_MULTIPLIER' }];
    },
    async updateRepairedReading(_watchId, readingId, parsed) { updated = { readingId, parsed }; },
    async commitPage(page) { committed = page; },
  };
  const client = { async listWatches() { return { watches: [watch] }; },
    async latest() { return { watch: { id: WATCH_ID, selector: SELECTOR }, latest: repaired, cursor: '2' }; },
    async history(_id, after) { return { watchId: WATCH_ID, selector: SELECTOR, readings: [], nextCursor: after, hasMore: false }; } };
  const sync = new OrbitWatchSync({ client, store, onPage: async ({ entries, state }) => {
    if (entries.length) {
      repairCalls += 1;
      assert.equal(entries[0].parsed.visibleItems[0].multiplier, 1.5);
      return { version: 2, visibleMultipliers: entries[0].parsed.visibleItems.map(item => item.multiplier) };
    }
    return state.snapshot;
  } });
  const result = await sync.sync();
  assert.equal(repairCalls, 1);
  assert.equal(updated.readingId, '2');
  assert.equal(updated.parsed.selectorItems[11].multiplier, 4612.47);
  assert.equal(result.cursor, '2');
  assert.equal(result.repairedReadings, 1);
  assert.equal(committed.expectedCursor, '2');
  assert.deepEqual(committed.snapshot.visibleMultipliers, [1.5, ...beforeMultipliers.slice(0, 10)]);
});

test('reparses archived values without inferring rounds when no continuity baseline exists', async () => {
  const first = reading('1');
  const second = { ...reading('2'), items: [{ index: 0, value: '1.70x', visible: true }] };
  const repairBatches = [];
  let committed;
  const store = {
    async getState() { return { watchId: WATCH_ID, cursor: '2', snapshot: null }; },
    async getInvalidReadings() { return [
      { readingId: '1', reading: first, validationError: 'INVALID_MULTIPLIER' },
      { readingId: '2', reading: second, validationError: 'INVALID_MULTIPLIER' },
    ]; },
    async getLatestVisibleSnapshotBefore(_watchId, readingId) {
      assert.equal(readingId, '1');
      return null;
    },
    async getArchivedReadings(_watchId, after, through) {
      assert.deepEqual([after, through], ['1', '2']);
      return [{ readingId: '2', reading: second, validationError: 'INVALID_MULTIPLIER' }];
    },
    async commitRepairPage(page) {
      repairBatches.push({ ids: page.entries.map(entry => entry.reading.id), pending: page.pendingRounds });
    },
    async updateRepairedReading() {},
    async getPendingRounds() { return []; },
    async ackPendingRounds() {},
    async commitPage(page) { committed = page; },
  };
  const client = {
    async listWatches() { return { watches: [watch] }; },
    async latest() { return { watch: { id: WATCH_ID, selector: SELECTOR }, latest: second, cursor: '2' }; },
    async history(_id, after) { return { watchId: WATCH_ID, selector: SELECTOR,
      readings: [], nextCursor: after, hasMore: false }; },
  };
  let deliveredCandidates = 0;
  const sync = new OrbitWatchSync({ client, store,
    onPage: async ({ state: pageState, entries }) => {
      if (entries.length) deliveredCandidates += entries.length;
      return { snapshot: pageState.snapshot, candidates: [], deliveryModes: [] };
    },
  });
  const result = await sync.sync();
  assert.deepEqual(repairBatches, [
    { ids: ['1'], pending: [] },
    { ids: ['2'], pending: [] },
  ]);
  assert.equal(deliveredCandidates, 0);
  assert.equal(result.repairedReadings, 2);
  assert.equal(result.unreconstructedReadings, 2);
  assert.equal(result.repairBaselineUnavailable, true);
  assert.equal(committed.snapshot.repairBaselineUnavailable, true);
  assert.equal(committed.snapshot.gapPending, true);
});

test('accepts an empty history and persists the unchanged cursor', async () => {
  let committed;
  const client = { async listWatches() { return { watches: [watch] }; },
    async latest() { return { watch: { id: WATCH_ID, selector: SELECTOR }, latest: null, cursor: '0' }; },
    async history(_id, after) { return { watchId: WATCH_ID, selector: SELECTOR, readings: [], nextCursor: after, hasMore: false }; } };
  const store = { async getState() { return null; }, async commitPage(page) { committed = page; } };
  const result = await new OrbitWatchSync({ client, store, watchId: WATCH_ID }).sync();
  assert.equal(result.cursor, '0');
  assert.equal(result.pages, 1);
  assert.equal(committed.nextCursor, '0');
});

test('reports a watch source error independently of successful API synchronization', async () => {
  const client = { async listWatches() { return { watches: [{ ...watch, lastError: 'source unavailable' }] }; },
    async latest() { return { watch: { id: WATCH_ID, selector: SELECTOR }, latest: null, cursor: '0' }; },
    async history(_id, after) { return { watchId: WATCH_ID, selector: SELECTOR, readings: [], nextCursor: after, hasMore: false }; } };
  const store = { async getState() { return null; }, async commitPage() {} };
  const result = await new OrbitWatchSync({ client, store }).sync();
  assert.equal(result.watchSourceError, true);
  assert.equal(result.cursor, '0');
});

test('keeps the saved watch identity while its Chrome source is temporarily unhealthy', async () => {
  const otherId = 'dddddddd-dddd-4ddd-8ddd-dddddddddddd';
  const other = { ...watch, id: otherId, targetId: 'other-browser', tabId: 9, frameId: 101,
    frameUrl: 'https://other.example/game', collectorStatus: 'running' };
  const saved = { ...watch, collectorStatus: 'waiting_for_connection', lastError: 'Chrome disconnected' };
  const client = {
    async listWatches() { return { watches: [other, saved] }; },
    async latest(id) {
      assert.equal(id, WATCH_ID);
      return { watch: { id: WATCH_ID, selector: SELECTOR }, latest: null, cursor: '0' };
    },
    async history(id, after) {
      assert.equal(id, WATCH_ID);
      return { watchId: id, selector: SELECTOR, readings: [], nextCursor: after, hasMore: false };
    },
  };
  const store = { async getState() { return { watchId: WATCH_ID, cursor: '0', snapshot: null }; }, async commitPage() {} };
  const result = await new OrbitWatchSync({ client, store }).sync();
  assert.equal(result.watchId, WATCH_ID);
  assert.equal(result.watchHealthy, false);
  assert.equal(result.watchCollectorStatus, 'waiting_for_connection');
});

test('recovers to the healthy Watch for the same frame after Chrome connection IDs change', async () => {
  const replacementId = 'dddddddd-dddd-4ddd-8ddd-dddddddddddd';
  const frameUrl = 'https://aviator-next.spribegaming.com/?user=player-1&token=stable-token&ts=1';
  const saved = { ...watch, frameUrl, collectorStatus: 'waiting_for_connection', lastError: 'Chrome disconnected' };
  const replacement = { ...watch, id: replacementId, targetId: 'new-browser', tabId: 44, frameId: 105,
    frameUrl, lastCheckedAt: '2026-01-01T00:00:10Z' };
  let state = { watchId: WATCH_ID, cursor: '20', snapshot: { version: 2,
    repairBaselineUnavailable: true, unreconstructedReadings: 4 } };
  let transition;
  let committed;
  const store = {
    async getState() { return state; },
    async transitionWatch(change) {
      transition = change;
      state = { watchId: change.watchId, cursor: '0', snapshot: change.snapshot };
    },
    async commitPage(page) {
      committed = page;
      state = { watchId: page.watchId, cursor: page.nextCursor, snapshot: page.snapshot };
    },
  };
  const latest = payoutReading('5');
  const client = {
    async listWatches() { return { watches: [saved, replacement] }; },
    latest(id) {
      if (id === WATCH_ID) return new Promise(() => {});
      return Promise.resolve({ watch: { id: replacementId, selector: SELECTOR }, latest, cursor: '5' });
    },
    async history(id, after) {
      assert.deepEqual([id, after], [replacementId, '0']);
      return { watchId: id, selector: SELECTOR, readings: [latest], nextCursor: '5', hasMore: false };
    },
  };
  const result = await new OrbitWatchSync({ client, store,
    onPage: async ({ state: pageState }) => pageState.snapshot }).sync();
  assert.equal(result.watchId, replacementId);
  assert.equal(result.watchRecovered, true);
  assert.equal(result.previousWatchId, WATCH_ID);
  assert.equal(transition.previousState.cursor, '20');
  assert.equal(transition.snapshot.gapPending, true);
  assert.equal(transition.snapshot.bootstrapUntilCursor, '5');
  assert.equal(transition.snapshot.repairBaselineUnavailable, undefined);
  assert.equal(committed.snapshot.sourceIdentity.targetId, 'new-browser');
  assert.equal(committed.snapshot.sourceIdentity.frameId, 105);
});

test('recovers across rotated iframe token and changed tab/frame IDs when stable account identity matches', async () => {
  const replacementId = 'dddddddd-dddd-4ddd-8ddd-dddddddddddd';
  const saved = { ...watch,
    frameUrl: 'https://aviator-next.spribegaming.com/game?user=player-1&currency=RWF&token=old&ts=1',
    collectorStatus: 'waiting_for_connection', lastError: 'Chrome disconnected' };
  const replacement = { ...watch, id: replacementId, targetId: 'new-browser', tabId: 45, frameId: 106,
    frameUrl: 'https://aviator-next.spribegaming.com/game?currency=RWF&user=player-1&token=new&ts=2' };
  let state = { watchId: WATCH_ID, cursor: '3', snapshot: { version: 2 } };
  const latest = payoutReading('7');
  const store = {
    async getState() { return state; },
    async transitionWatch(change) { state = { watchId: change.watchId, cursor: '0', snapshot: change.snapshot }; },
    async commitPage(page) { state = { watchId: page.watchId, cursor: page.nextCursor, snapshot: page.snapshot }; },
  };
  const client = {
    async listWatches() { return { watches: [saved, replacement] }; },
    async latest(id) {
      if (id === WATCH_ID) throw new Error('old browser unavailable');
      return { watch: { id: replacementId, selector: SELECTOR }, latest, cursor: '7' };
    },
    async history(id) {
      return { watchId: id, selector: SELECTOR, readings: [latest], nextCursor: '7', hasMore: false };
    },
  };
  const result = await new OrbitWatchSync({ client, store,
    onPage: async ({ state: pageState }) => pageState.snapshot }).sync();
  assert.equal(result.watchId, replacementId);
  assert.equal(result.watchRecovered, true);
});

test('does not recover to the same hostname when stable account identity differs', async () => {
  const otherId = 'dddddddd-dddd-4ddd-8ddd-dddddddddddd';
  const saved = { ...watch, frameUrl: 'https://aviator-next.spribegaming.com/game?user=player-1&token=old',
    collectorStatus: 'waiting_for_connection', lastError: 'Chrome disconnected' };
  const other = { ...watch, id: otherId, targetId: 'new-browser', tabId: 45, frameId: 106,
    frameUrl: 'https://aviator-next.spribegaming.com/game?user=player-2&token=new' };
  const client = {
    async listWatches() { return { watches: [saved, other] }; },
    async latest(id) {
      assert.equal(id, WATCH_ID);
      return { watch: { id: WATCH_ID, selector: SELECTOR }, latest: null, cursor: '0' };
    },
    async history(id, after) {
      assert.equal(id, WATCH_ID);
      return { watchId: id, selector: SELECTOR, readings: [], nextCursor: after, hasMore: false };
    },
  };
  const store = { async getState() { return { watchId: WATCH_ID, cursor: '0', snapshot: null }; },
    async commitPage() {} };
  const result = await new OrbitWatchSync({ client, store }).sync();
  assert.equal(result.watchId, WATCH_ID);
  assert.equal(result.watchRecovered, false);
});

test('fails closed when more than one distinct healthy Chrome scope matches recovery identity', async () => {
  const saved = { ...watch, collectorStatus: 'waiting_for_connection', lastError: 'Chrome disconnected' };
  const first = { ...watch, id: 'dddddddd-dddd-4ddd-8ddd-dddddddddddd', targetId: 'browser-1', tabId: 2 };
  const second = { ...watch, id: 'eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee', targetId: 'browser-2', tabId: 3 };
  const client = { async listWatches() { return { watches: [saved, first, second] }; },
    async latest() { throw new Error('old browser unavailable'); } };
  const store = { async getState() { return { watchId: WATCH_ID, cursor: '2', snapshot: null }; } };
  await assert.rejects(new OrbitWatchSync({ client, store }).sync(), { code: 'AMBIGUOUS_WATCH_RECOVERY' });
});

test('recovers a missing saved Watch only when its persisted source identity proves the replacement scope', async () => {
  const replacementId = 'dddddddd-dddd-4ddd-8ddd-dddddddddddd';
  const replacement = { ...watch, id: replacementId, targetId: 'new-browser', tabId: 8, frameId: 12 };
  const latest = payoutReading('4');
  let state = { watchId: WATCH_ID, cursor: '3', snapshot: { version: 2, sourceIdentity: {
    engine: watch.engine, targetId: watch.targetId, tabId: watch.tabId, frameId: watch.frameId,
    frameUrl: watch.frameUrl, frameSelector: '', selector: SELECTOR,
  } } };
  const store = {
    async getState() { return state; },
    async transitionWatch(change) { state = { watchId: change.watchId, cursor: '0', snapshot: change.snapshot }; },
    async commitPage(page) { state = { watchId: page.watchId, cursor: page.nextCursor, snapshot: page.snapshot }; },
  };
  const client = {
    async listWatches() { return { watches: [replacement] }; },
    async latest(id) {
      if (id === WATCH_ID) throw new Error('saved watch deleted');
      return { watch: { id: replacementId, selector: SELECTOR }, latest, cursor: '4' };
    },
    async history(id) { return { watchId: id, selector: SELECTOR, readings: [latest], nextCursor: '4', hasMore: false }; },
  };
  const result = await new OrbitWatchSync({ client, store,
    onPage: async ({ state: pageState }) => pageState.snapshot }).sync();
  assert.equal(result.watchId, replacementId);
  assert.equal(result.watchRecovered, true);
});

test('reanchors a missing saved Watch from persisted same-tab identity while preserving a new-source gap', async () => {
  const replacementId = 'eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee';
  const replacement = { ...watch, id: replacementId, targetId: 'reconnected-browser', frameId: 12,
    frameUrl: 'https://aviator-next.spribegaming.com/?user=player-2&token=new' };
  const latest = payoutReading('4');
  const savedIdentity = { engine: watch.engine, targetId: watch.targetId, tabId: watch.tabId,
    frameId: watch.frameId, frameUrl: 'https://aviator-next.spribegaming.com/?user=player-1&token=old',
    frameSelector: '', selector: SELECTOR };
  let state = { watchId: WATCH_ID, cursor: '3', snapshot: { version: 2, sourceIdentity: savedIdentity } };
  let transition;
  const store = {
    async getState() { return state; },
    async transitionWatch(change) {
      transition = change;
      state = { watchId: change.watchId, cursor: '0', snapshot: change.snapshot };
    },
    async commitPage(page) { state = { watchId: page.watchId, cursor: page.nextCursor, snapshot: page.snapshot }; },
  };
  const client = {
    async listWatches() { return { watches: [replacement] }; },
    async latest(id) {
      if (id === WATCH_ID) throw new Error('saved watch removed');
      return { watch: { id: replacementId, selector: SELECTOR }, latest, cursor: '4' };
    },
    async history(id, after) {
      assert.deepEqual([id, after], [replacementId, '0']);
      return { watchId: id, selector: SELECTOR, readings: [latest], nextCursor: '4', hasMore: false };
    },
  };
  const result = await new OrbitWatchSync({ client, store,
    onPage: async ({ state: pageState }) => pageState.snapshot }).sync();
  assert.equal(result.watchId, replacementId);
  assert.equal(result.watchRecovered, true);
  assert.equal(result.watchRecoveryMode, 'SAME_TAB_NEW_SOURCE_GAP');
  assert.equal(transition.previousState.cursor, '3');
  assert.equal(transition.snapshot.gapPending, true);
});

test('reanchors a unique healthy Watch on the same game tab as a new gap-bounded source segment', async () => {
  const replacementId = 'dddddddd-dddd-4ddd-8ddd-dddddddddddd';
  const saved = { ...watch, frameUrl: 'https://aviator-next.spribegaming.com/?user=player-1&token=old',
    collectorStatus: 'stopped', lastError: 'saved page closed' };
  const replacement = { ...watch, id: replacementId, targetId: 'reconnected-browser', frameId: 12,
    frameUrl: 'https://aviator-next.spribegaming.com/?user=player-2&token=new' };
  const latest = payoutReading('4');
  let state = { watchId: WATCH_ID, cursor: '20', snapshot: { version: 2,
    visibleMultipliers: [1.1, 1.2], sourceIdentity: {
      engine: saved.engine, targetId: saved.targetId, tabId: saved.tabId, frameId: saved.frameId,
      frameUrl: saved.frameUrl, frameSelector: '', selector: SELECTOR,
    } } };
  let transition;
  const store = {
    async getState() { return state; },
    async transitionWatch(change) {
      transition = change;
      state = { watchId: change.watchId, cursor: '0', snapshot: change.snapshot };
    },
    async commitPage(page) { state = { watchId: page.watchId, cursor: page.nextCursor, snapshot: page.snapshot }; },
  };
  const client = {
    async listWatches() { return { watches: [saved, replacement] }; },
    async latest(id) {
      return { watch: { id, selector: SELECTOR }, latest: id === replacementId ? latest : reading('20'),
        cursor: id === replacementId ? '4' : '20' };
    },
    async history(id, after) {
      assert.deepEqual([id, after], [replacementId, '0']);
      return { watchId: id, selector: SELECTOR, readings: [latest], nextCursor: '4', hasMore: false };
    },
  };
  const result = await new OrbitWatchSync({ client, store,
    onPage: async ({ state: pageState }) => pageState.snapshot }).sync();
  assert.equal(result.watchId, replacementId);
  assert.equal(result.watchRecovered, true);
  assert.equal(result.watchRecoveryMode, 'SAME_TAB_NEW_SOURCE_GAP');
  assert.equal(result.previousWatchId, WATCH_ID);
  assert.equal(transition.previousState.cursor, '20');
  assert.equal(transition.snapshot.gapPending, true);
  assert.equal(transition.snapshot.bootstrapUntilCursor, '4');
  assert.equal(transition.snapshot.sourceIdentity.targetId, 'reconnected-browser');
});

test('refuses to guess between multiple running payout watches when no watch is saved yet', async () => {
  const other = { ...watch, id: 'dddddddd-dddd-4ddd-8ddd-dddddddddddd' };
  const client = { async listWatches() { return { watches: [watch, other] }; } };
  const store = { async getState() { return null; } };
  await assert.rejects(new OrbitWatchSync({ client, store }).sync(), { code: 'MULTIPLE_AVIATOR_WATCHES' });
});

test('does not replace a missing saved watch or reset its committed cursor', async () => {
  const newId = 'dddddddd-dddd-4ddd-8ddd-dddddddddddd';
  const store = { async getState() { return { watchId: WATCH_ID, cursor: '20', snapshot: { version: 2 } }; },
    async transitionWatch() { assert.fail('a missing saved watch must not be silently replaced'); },
    async commitPage() { assert.fail('a missing saved watch must not advance the cursor'); } };
  const client = { async listWatches() { return { watches: [{ ...watch, id: newId }] }; },
    async latest() { assert.fail('latest must not be fetched for a different watch'); },
    async history() { assert.fail('history must not be read from a different watch'); } };
  await assert.rejects(new OrbitWatchSync({ client, store }).sync(), { code: 'SAVED_WATCH_NOT_FOUND' });
});

test('does not report success when history cannot reach the latest snapshot cursor', async () => {
  let committedCursor = null;
  const client = { async listWatches() { return { watches: [watch] }; },
    async latest() { return { watch: { id: WATCH_ID, selector: SELECTOR }, latest: reading('1'), cursor: '1' }; },
    async history(_id, after) { return { watchId: WATCH_ID, selector: SELECTOR, readings: [], nextCursor: after, hasMore: false }; } };
  const store = { async getState() { return null; }, async commitPage(page) { committedCursor = page.nextCursor; } };
  await assert.rejects(new OrbitWatchSync({ client, store, watchId: WATCH_ID }).sync(),
    { code: 'LATEST_HISTORY_GAP' });
  assert.equal(committedCursor, '0');
});

test('commits round evidence before placing it in the durable delivery queue', async () => {
  let state = null;
  const pending = [];
  const events = [];
  const candidate = { round_id: 'orbit-round-1', round_index: 1, multiplier: 1.16 };
  const store = {
    async getState() { return state; },
    async getPendingRounds() { return [...pending]; },
    async ackPendingRounds(ids) {
      for (const id of ids) pending.splice(pending.findIndex(round => round.round_id === id), 1);
    },
    async commitPage(page) {
      events.push('archive-commit');
      assert.equal(page.entries[0].reading.id, '1');
      pending.push(...page.pendingRounds);
      state = { watchId: page.watchId, cursor: page.nextCursor, snapshot: page.snapshot };
    },
  };
  const client = {
    async listWatches() { return { watches: [watch] }; },
    async latest() { return { watch: { id: WATCH_ID, selector: SELECTOR }, latest: reading('1'), cursor: '1' }; },
    async history(_id, after) { return { watchId: WATCH_ID, selector: SELECTOR,
      readings: [reading('1')], nextCursor: '1', hasMore: false }; },
  };
  const sync = new OrbitWatchSync({ client, store,
    onPage: async () => ({ snapshot: { version: 2, visibleMultipliers: [1.16] },
      candidates: [candidate], deliveryModes: ['LIVE'] }),
    onPageCommitted: async progress => {
      if (!progress.pendingRounds?.length) return;
      events.push('delivery-outbox-append');
      assert.deepEqual(progress.pendingRounds.map(round => round.round_id), ['orbit-round-1']);
    },
  });
  await sync.sync();
  assert.deepEqual(events, ['archive-commit', 'delivery-outbox-append']);
  assert.deepEqual(pending, []);
});

test('replays committed pending rounds before querying Orbit after a collector restart', async () => {
  const events = [];
  const pending = [{ round_id: 'orbit-round-1', round_index: 1, multiplier: 1.16, delivery_mode: 'RECOVERED' }];
  const client = {
    async listWatches() { events.push('orbit-query'); return { watches: [watch] }; },
    async latest() { return { watch: { id: WATCH_ID, selector: SELECTOR }, latest: null, cursor: '0' }; },
    async history(_id, after) { return { watchId: WATCH_ID, selector: SELECTOR,
      readings: [], nextCursor: after, hasMore: false }; },
  };
  const store = {
    async getState() { return { watchId: WATCH_ID, cursor: '0', snapshot: null }; },
    async getPendingRounds() { return [...pending]; },
    async ackPendingRounds(ids) { pending.splice(0, ids.length); },
    async commitPage() {},
  };
  const sync = new OrbitWatchSync({ client, store, onPageCommitted: async progress => {
    if (!progress.pendingRounds?.length) return;
    events.push('delivery-outbox-append');
  } });
  await sync.sync();
  assert.equal(events[0], 'delivery-outbox-append');
  assert.equal(events[1], 'orbit-query');
  assert.deepEqual(pending, []);
});
