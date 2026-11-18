import test from 'node:test';
import assert from 'node:assert/strict';
import { discoverAviatorWatch, orbitRoundIdentity, parseOrbitReading, resolveVisiblePayoutItems,
  snapshotDelta, validateHistoryPage, visiblePayoutItems } from '../collector/OrbitReadings.js';
import { classifyOrbitDelivery, classifyOrbitDeliveries, roundRecord } from '../collector/OrbitRoundHistoryCollector.js';

const watch = id => ({ id, engine: 'connected-chrome', targetId: 'connection', tabId: 1, frameId: 0,
  frameUrl: 'https://aviator-next.spribegaming.com/', selector: '.payouts-block .payout',
  status: 'running', collectorStatus: 'running' });
const WATCH_A = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa';
const WATCH_B = 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb';

test('constructs a source round with its continuity boundary and timestamp', () => {
  const reading = { id: '21', checkedAt: '2026-01-01T00:00:00Z' };
  const item = { index: 0, multiplier: 1.71, rawValue: '1.71x' };
  const round = roundRecord({ watchId: WATCH_A, reading, item, index: 42, gapBefore: true });
  assert.equal(round.round_id, orbitRoundIdentity(WATCH_A, '21', 0));
  assert.equal(round.gap_before, true);
  assert.equal(round.round_index, 42);
  assert.equal(round.timestamp, reading.checkedAt);
});

test('classifies a fresh current Orbit reading as live after a normal idle interval', () => {
  const now = Date.parse('2026-01-01T00:00:10Z');
  const args = { caughtUp: true, watchHealthy: true, hasMore: false, pageIndex: 0, noBacklog: true,
    entries: [{ reading: { id: '21', checkedAt: '2026-01-01T00:00:09Z' } }], latestCursor: '21',
    candidates: [{ gap_before: false, source_metadata: { reading_id: '21' } }],
    previousSyncAt: now - 20000, now, freshnessMs: 15000 };
  assert.equal(classifyOrbitDelivery(args), 'LIVE');
  for (const change of [{ caughtUp: false }, { watchHealthy: false }, { hasMore: true },
    { pageIndex: 1 }, { latestCursor: '22' },
    { candidates: [{ gap_before: true, source_metadata: { reading_id: '21' } }] },
    { candidates: [{ gap_before: false, source_metadata: { reading_id: '20' } }] },
    { entries: [{ reading: { id: '21', checkedAt: '2025-12-31T23:00:00Z' } }] },
    { candidates: [{ gap_before: false, source_metadata: { reading_id: '20' } },
      { gap_before: false, source_metadata: { reading_id: '20' } }] }]) {
    assert.equal(classifyOrbitDelivery({ ...args, ...change }), 'RECOVERED');
  }
});

test('recovers earlier Orbit candidates but keeps only the newest current target decision live', () => {
  const now = Date.parse('2026-01-01T00:00:30Z');
  const args = { caughtUp: true, watchHealthy: true, hasMore: false, pageIndex: 0, noBacklog: true,
    entries: [
      { reading: { id: '20', checkedAt: '2026-01-01T00:00:09Z' } },
      { reading: { id: '21', checkedAt: '2026-01-01T00:00:29Z' } },
    ], latestCursor: '21', candidates: [
      { round_id: 'old', gap_before: false, source_metadata: { reading_id: '20' } },
      { round_id: 'current', gap_before: false, source_metadata: { reading_id: '21' } },
    ], now, freshnessMs: 15000 };
  assert.deepEqual(classifyOrbitDeliveries(args), ['RECOVERED', 'LIVE']);
  assert.equal(classifyOrbitDelivery(args), 'RECOVERED', 'a batch is not represented by one mode');
  assert.deepEqual(classifyOrbitDeliveries({ ...args,
    candidates: [args.candidates[0], { ...args.candidates[1], gap_before: true }] }),
  ['RECOVERED', 'RECOVERED']);
  assert.deepEqual(classifyOrbitDeliveries({ ...args, noBacklog: false }), ['RECOVERED', 'LIVE']);
});

test('discovers one running payout watch and validates its identity and selector', () => {
  assert.equal(discoverAviatorWatch({ watches: [watch(WATCH_A)] }).id, WATCH_A);
  assert.throws(() => discoverAviatorWatch({ watches: [watch(WATCH_A), watch(WATCH_B)] }),
    { code: 'MULTIPLE_AVIATOR_WATCHES' });
  assert.throws(() => discoverAviatorWatch({ watches: [{ ...watch(WATCH_A), status: 'paused' }] }, WATCH_A),
    { code: 'WATCH_NOT_RUNNING' });
  assert.throws(() => discoverAviatorWatch({ watches: [watch(WATCH_B)] }, WATCH_A),
    { code: 'SAVED_WATCH_NOT_FOUND' });
  assert.throws(() => discoverAviatorWatch({ watches: [{ ...watch(WATCH_A), status: 'paused' }] }),
    { code: 'WATCH_NOT_RUNNING' });
  assert.throws(() => discoverAviatorWatch({ watches: [{ ...watch('bad-id') }] }),
    { code: 'INVALID_WATCH_ID' });
  assert.throws(() => discoverAviatorWatch({ watches: [{ ...watch(WATCH_A), selector: '.other' }] }),
    { code: 'AVIATOR_WATCH_UNAVAILABLE' });
  assert.throws(() => discoverAviatorWatch({ watches: [{ ...watch(WATCH_A), selector: '.other' }] }, WATCH_A),
    { code: 'SAVED_WATCH_SELECTOR_MISMATCH' });
  assert.throws(() => discoverAviatorWatch({ watches: [] }), { code: 'AVIATOR_WATCH_UNAVAILABLE' });
});

test('parses every selector match in DOM order, retains visibility metadata, and rejects malformed values', () => {
  const row = { id: '11', checkedAt: '2026-01-01T00:00:00Z', count: 4, items: [
    { index: 0, value: '1.16x', visible: true }, { index: 1, value: '264.70x', visible: true },
    { index: 2, value: ' 1.16x ', visible: false }, { index: 3, value: '264.70x', visible: false },
  ] };
  const parsed = parseOrbitReading(row);
  assert.deepEqual(parsed.multipliers, [1.16, 264.7, 1.16, 264.7]);
  assert.equal(parsed.visibleItems.length, 2);
  assert.equal(parsed.selectorItems.length, 4);
  const partial = parseOrbitReading({ ...row, count: 120 });
  assert.equal(partial.truncated, true);
  assert.equal(partial.matchCount, 120);
  assert.equal(partial.returnedCount, 4);
  assert.deepEqual(parseOrbitReading({ ...row, items: [...row.items.slice(0, 2),
    { index: 2, value: '5.35x', visible: false }, { index: 3, value: '3.45x', visible: true }] }).multipliers,
  [1.16, 264.7, 5.35, 3.45]);
  assert.equal(parseOrbitReading({ ...row, count: 1,
    items: [{ index: 0, value: '4,612.47x', visible: true }] }).multipliers[0], 4612.47);
  assert.throws(() => parseOrbitReading({ ...row, items: [{ index: 0, value: 'bad 1.16x', visible: true }] }),
    { code: 'INVALID_MULTIPLIER' });
  assert.throws(() => parseOrbitReading({ ...row, count: 1,
    items: [{ index: 0, value: '46,12.47x', visible: true }] }), { code: 'INVALID_MULTIPLIER' });
});

test('enforces ordered cursors and preserves an empty page cursor', () => {
  const page = { watchId: 'w', selector: '.payouts-block .payout', nextCursor: '10', readings: [], hasMore: false };
  assert.equal(validateHistoryPage(page, 'w', '10'), page);
  assert.throws(() => validateHistoryPage({ ...page, hasMore: true }, 'w', '10'), { code: 'INVALID_HISTORY_CURSOR' });
  assert.throws(() => validateHistoryPage({ ...page, readings: [{ id: '9' }] }, 'w', '10'), { code: 'INVALID_HISTORY_ORDER' });
});

test('deduces rounds only with a unique sufficient overlap', () => {
  const previous = [2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12];
  assert.deepEqual(snapshotDelta(previous, [1.1, 1.2, ...previous.slice(0, 10)]),
    { status: 'VERIFIED', rounds: [1.2, 1.1] });
  assert.equal(snapshotDelta(previous, [7, 8, 9]).status, 'CONTINUITY_UNVERIFIED');
  assert.equal(snapshotDelta(previous, []).status, 'EMPTY');
  assert.deepEqual(snapshotDelta(Array(20).fill(1), Array(20).fill(1).map((_, index) => index < 3 ? 1 : index)),
    { status: 'CONTINUITY_UNVERIFIED', rounds: [] },
    'repeated values with several possible overlaps are not assigned invented round identities');
});

test('structurally selected payout lists retain every item and repeated legitimate outcomes', () => {
  const previous = Array.from({ length: 60 }, (_, index) => index + 1);
  previous[1] = previous[0];
  const current = [1.25, ...previous.slice(0, 59)];
  const items = values => values.map((multiplier, index) => ({ index, multiplier, historyContainer: 'div#history.payouts-block' }));
  const priorItems = visiblePayoutItems(items(previous));
  const nextItems = visiblePayoutItems(items(current));
  assert.equal(nextItems.length, 60);
  assert.equal(nextItems[0].index, 0);
  assert.equal(nextItems[1].multiplier, 1, 'two consecutive equal multipliers remain separate entries');
  assert.deepEqual(snapshotDelta(priorItems.map(item => item.multiplier), nextItems.map(item => item.multiplier)),
    { status: 'VERIFIED', rounds: [1.25] });
  const duplicateContainer = items([...current, ...current]).map(item => ({ ...item, historyContainer: 'div#history' }));
  assert.equal(visiblePayoutItems(duplicateContainer).length, 120,
    'multiplier equality by itself does not remove entries from one selected container');
  const legacyMirroredWithoutContainer = items([...current, ...current]).map(({ historyContainer, ...item }) => item);
  const updatingPanel = items([...current, ...current]).map(({ historyContainer, ...item }) => item);
  updatingPanel[73].multiplier = 777;
  assert.deepEqual(visiblePayoutItems(updatingPanel), []);
  assert.deepEqual(visiblePayoutItems(legacyMirroredWithoutContainer), [],
    'legacy value-only mirrored arrays stay ambiguous instead of collapsing into one guessed panel');
});

test('reading-level payout container identity is inherited by every parsed match', () => {
  const raw = { id: '20', checkedAt: '2026-01-01T00:00:00Z', count: 60,
    historyContainer: 'div#main-history.payouts-block',
    items: Array.from({ length: 60 }, (_, index) => ({ index, value: `${index + 1}.10x`, visible: true })) };
  const parsed = parseOrbitReading(raw);
  assert.equal(parsed.visibleItems.length, 60);
  assert.ok(parsed.visibleItems.every(item => item.historyContainer === raw.historyContainer));
  assert.equal(visiblePayoutItems(parsed.visibleItems).length, 60,
    'the single structurally selected list is retained even when outcomes repeat');
});

test('identical visible responsive history containers resolve to one consensus sequence', () => {
  const values = Array.from({ length: 60 }, (_, index) => `${(1 + (index * 37) % 997 / 100).toFixed(2)}x`);
  values[9] = values[8]; // A genuine repeated multiplier remains a separate sequence item.
  const items = [
    ...values.map((value, index) => ({ index, value, multiplier: Number.parseFloat(value),
      rawValue: value, visible: true, historyContainer: 'div.payouts-block[1]' })),
    ...values.map((value, index) => ({ index: index + values.length, value, multiplier: Number.parseFloat(value),
      rawValue: value, visible: true, historyContainer: 'div.payouts-block[2]' })),
  ];

  const result = resolveVisiblePayoutItems(items);

  assert.equal(result.status, 'CONSENSUS_MIRRORS');
  assert.equal(result.items.length, 60);
  assert.deepEqual(result.items.map(item => item.index), Array.from({ length: 60 }, (_, index) => index));
  assert.equal(result.items[8].multiplier, result.items[9].multiplier);
  assert.deepEqual(result.diagnostics.accepted_containers.map(item => item.container),
    ['div.payouts-block[1]', 'div.payouts-block[2]']);
  assert.equal(result.diagnostics.policy, 'ALL_CANDIDATES_MUST_MATCH_BY_POSITION');
});

test('history container consensus fails closed for stale, hidden, or incomplete candidates', () => {
  const values = Array.from({ length: 60 }, (_, index) => index + 1);
  const mirrored = () => [
    ...values.map((multiplier, index) => ({ index, multiplier, rawValue: `${multiplier}.00x`,
      visible: true, historyContainer: 'div.payouts-block[1]' })),
    ...values.map((multiplier, index) => ({ index: index + values.length, multiplier,
      rawValue: `${multiplier}.00x`, visible: true, historyContainer: 'div.payouts-block[2]' })),
  ];
  const stale = mirrored();
  stale[91].multiplier = 999;
  stale[91].rawValue = '999.00x';
  const hidden = mirrored();
  hidden[91].visible = false;
  // Fewer than the minimum overlap must remain ambiguous even when both
  // responsive containers happen to contain the same short partial window.
  const short = mirrored().slice(0, 9).concat(mirrored().slice(60, 69));

  for (const sample of [stale, hidden, short]) {
    const result = resolveVisiblePayoutItems(sample);
    assert.equal(result.status, 'AMBIGUOUS');
    assert.deepEqual(result.items, []);
    assert.ok(result.diagnostics.reason);
  }
});

test('intentional latest overlap windows are not reported as truncated Orbit readings', () => {
  const reading = { id: '21', checkedAt: '2026-01-01T00:00:01Z', count: 120,
    projection: 'LATEST_OVERLAP_WINDOW', windowSize: 12,
    items: Array.from({ length: 12 }, (_, index) => ({ index, value: `${index + 1}.10x`, visible: true,
      historyContainer: 'div#main-history.payouts-block' })) };
  const parsed = parseOrbitReading(reading);
  assert.equal(parsed.matchCount, 120);
  assert.equal(parsed.returnedCount, 12);
  assert.equal(parsed.truncated, false);
  assert.equal(parsed.visibleItems.length, 12);
});

test('uses visible Orbit order for deltas and source identity independent of timestamps', () => {
  const before = parseOrbitReading({ id: '20', checkedAt: '2025-01-01T00:00:00Z', count: 12, items: [
    ...[1.16, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11].map((v, index) => ({ index, value: `${v}x`, visible: true })),
    { index: 11, value: '1.16x', visible: false },
  ] });
  const after = parseOrbitReading({ id: '21', checkedAt: '2030-12-31T23:59:59Z', count: 13, items: [
    { index: 0, value: '1.71x', visible: true },
    ...before.visibleItems.map((item, i) => ({ index: i + 1, value: `${item.multiplier}x`, visible: true })),
    { index: 12, value: '1.71x', visible: false },
  ] });
  assert.deepEqual(snapshotDelta(before.visibleItems.map(item => item.multiplier),
    after.visibleItems.map(item => item.multiplier)), { status: 'VERIFIED', rounds: [1.71] });
  assert.equal(orbitRoundIdentity(WATCH_A, '21', 0), orbitRoundIdentity(WATCH_A, '21', 0));
  assert.notEqual(orbitRoundIdentity(WATCH_A, '21', 0), orbitRoundIdentity(WATCH_A, '22', 0));
  assert.notEqual(orbitRoundIdentity(WATCH_A, '21', 0), orbitRoundIdentity(WATCH_B, '21', 0));
});
