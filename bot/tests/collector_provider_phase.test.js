import test from 'node:test';
import assert from 'node:assert/strict';
import { _installObserver } from '../collector/Collector.js';

function frameFixture({ marker = null } = {}) {
  const queryCounts = { payouts: 0, providerMarkers: 0 };
  const observers = [];
  const intervals = [];
  const clearedIntervals = [];
  let providerMarker = marker;
  const button = {
    textContent: 'Bet 200 BIF', className: 'btn-success bet', disabled: false,
    offsetWidth: 100, offsetHeight: 30,
    getAttribute: () => null,
    classList: { contains: () => false },
  };
  const input = { offsetWidth: 80, offsetHeight: 20 };
  const payoutBlock = { nodeType: 1, parentElement: {}, contains: node => node === payoutBlock || node === payout };
  const payout = { nodeType: 1, textContent: '2.00x', closest: selector => selector === '.payouts-block' ? payoutBlock
      : selector === '.payouts-block .payout' ? payout : null,
    matches: selector => selector === '.payouts-block .payout', querySelector: () => null };
  const betBlock = {
    nodeType: 1,
    querySelector: selector => selector === '.btn-success.bet' ? button : input,
    querySelectorAll: selector => selector === '.tab' ? [{ textContent: 'Bet',
      classList: { contains: value => value === 'active' } }] : [],
    closest: selector => selector === '.bet-block' ? betBlock : null,
    contains: node => node === button || node === input,
  };
  const unrelated = { nodeType: 1, closest: () => null, matches: () => false,
    querySelector: () => null, contains: () => false };
  const document = {
    body: { nodeType: 1 },
    querySelector: selector => selector === '.bet-block' ? betBlock
      : selector === '.payouts-block' ? payoutBlock
        : selector === '.payouts-block .payout' ? payout : null,
    querySelectorAll: selector => {
      if (selector === '.payouts-block .payout') { queryCounts.payouts += 1; return [payout]; }
      if (selector.startsWith('[data-aviator-phase')) { queryCounts.providerMarkers += 1; return providerMarker ? [providerMarker] : []; }
      return [];
    },
  };
  class Observer {
    constructor(callback) { this.callback = callback; observers.push(this); }
    observe() {}
    disconnect() {}
  }
  const window = {
    __aviatorCollector: null,
    getComputedStyle: () => ({ display: 'block', visibility: 'visible' }),
  };
  const original = {
    document: globalThis.document,
    window: globalThis.window,
    MutationObserver: globalThis.MutationObserver,
    setInterval: globalThis.setInterval,
    clearInterval: globalThis.clearInterval,
  };
  globalThis.document = document;
  globalThis.window = window;
  globalThis.MutationObserver = Observer;
  globalThis.setInterval = (callback, interval) => {
    const timer = { callback, interval };
    intervals.push(timer);
    return timer;
  };
  globalThis.clearInterval = timer => clearedIntervals.push(timer);
  return {
    window, observers, queryCounts, intervals, clearedIntervals, unrelated, payoutBlock, payout, betBlock,
    setMarker(value) { providerMarker = value; }, restore() {
      for (const key of Object.keys(original)) {
        if (original[key] === undefined) delete globalThis[key];
        else globalThis[key] = original[key];
      }
    },
  };
}

function marker(values) {
  return { getAttribute: name => values[name] ?? null };
}

test('an enabled green Bet button alone remains UNKNOWN', async () => {
  const fixture = frameFixture();
  try {
    await _installObserver({ evaluate: (fn, selector) => fn(selector) }, '.payouts-block .payout');
    assert.equal(fixture.window.__aviatorCollector.latestPhaseEvent.gamePhase.phase, 'UNKNOWN');
  } finally {
    fixture.restore();
  }
});

test('unrelated page mutations do not rescan payouts or reread game phase', async () => {
  const fixture = frameFixture();
  try {
    await _installObserver({ evaluate: (fn, selector) => fn(selector) }, '.payouts-block .payout');
    const payoutReads = fixture.queryCounts.payouts;
    const phaseReads = fixture.queryCounts.providerMarkers;
    const mutation = { type: 'childList', target: fixture.unrelated, addedNodes: [], removedNodes: [] };
    fixture.observers[0].callback([mutation]);
    fixture.observers[1].callback([mutation]);
    assert.equal(fixture.queryCounts.payouts, payoutReads);
    assert.equal(fixture.queryCounts.providerMarkers, phaseReads);
  } finally {
    fixture.restore();
  }
});

test('payout and Bet panel mutations only refresh their own collector view', async () => {
  const fixture = frameFixture();
  try {
    await _installObserver({ evaluate: (fn, selector) => fn(selector) }, '.payouts-block .payout');
    const payoutReads = fixture.queryCounts.payouts;
    const phaseReads = fixture.queryCounts.providerMarkers;
    fixture.observers[0].callback([{ type: 'characterData', target: fixture.payoutBlock,
      addedNodes: [], removedNodes: [] }]);
    assert.ok(fixture.queryCounts.payouts > payoutReads);
    assert.equal(fixture.queryCounts.providerMarkers, phaseReads);

    const afterPayoutReads = fixture.queryCounts.payouts;
    fixture.observers[1].callback([{ type: 'attributes', target: fixture.betBlock,
      attributeName: 'class', addedNodes: [], removedNodes: [] }]);
    assert.ok(fixture.queryCounts.providerMarkers > phaseReads);
    assert.equal(fixture.queryCounts.payouts, afterPayoutReads);
  } finally {
    fixture.restore();
  }
});

test('a changed payout text reuses the payout-node cache instead of querying the full list', async () => {
  const fixture = frameFixture();
  try {
    await _installObserver({ evaluate: (fn, selector) => fn(selector) }, '.payouts-block .payout');
    const payoutReads = fixture.queryCounts.payouts;
    fixture.observers[0].callback([{ type: 'characterData', target: fixture.payout,
      addedNodes: [], removedNodes: [] }]);
    assert.equal(fixture.queryCounts.payouts, payoutReads);
  } finally {
    fixture.restore();
  }
});

test('explicit provider round, target mapping, phase and deadline allow a phase observation', async () => {
  const values = {
    'data-aviator-phase': 'BETTING_OPEN',
    'data-provider-round-id': 'provider-round-11',
    'data-canonical-target-round-id': 'canonical-round-11',
    'data-betting-deadline': new Date(Date.now() + 20000).toISOString(),
    'data-target-identity-proof-method': 'platform_target_id_before_start',
    'data-target-not-started': 'true',
  };
  const fixture = frameFixture({ marker: marker(values) });
  try {
    await _installObserver({ evaluate: (fn, selector) => fn(selector) }, '.payouts-block .payout');
    const phase = fixture.window.__aviatorCollector.latestPhaseEvent.gamePhase;
    assert.equal(phase.phase, 'BETTING_OPEN');
    assert.equal(phase.provider_round_id, 'provider-round-11');
    assert.equal(phase.canonical_target_round_id, 'canonical-round-11');
    assert.equal(phase.target_identity_proof.target_not_started, true);
  } finally {
    fixture.restore();
  }
});

test('re-publishes an unchanged real provider phase on the freshness heartbeat', async () => {
  const values = {
    'data-aviator-phase': 'BETTING_OPEN',
    'data-provider-round-id': 'provider-round-11',
    'data-canonical-target-round-id': 'canonical-round-11',
    'data-betting-deadline': new Date(Date.now() + 20000).toISOString(),
    'data-target-identity-proof-method': 'platform_target_id_before_start',
    'data-target-not-started': 'true',
  };
  const fixture = frameFixture({ marker: marker(values) });
  try {
    await _installObserver({ evaluate: (fn, selector) => fn(selector) }, '.payouts-block .payout');
    assert.equal(fixture.intervals.length, 1);
    assert.equal(fixture.intervals[0].interval, 5000);
    fixture.intervals[0].callback();
    assert.equal(fixture.window.__aviatorCollector.queue.length, 0);
    const heartbeat = fixture.window.__aviatorCollector.latestPhaseEvent;
    assert.equal(heartbeat.phaseOnly, true);
    assert.equal(heartbeat.reason, 'provider-phase-heartbeat');
    assert.equal(heartbeat.gamePhase.phase, 'BETTING_OPEN');
    assert.equal(heartbeat.gamePhase.provider_round_id, 'provider-round-11');
    assert.equal(heartbeat.gamePhase.canonical_target_round_id, 'canonical-round-11');
  } finally {
    fixture.restore();
  }
});

test('phase heartbeats cannot displace completed-round payout observations', async () => {
  const fixture = frameFixture({ marker: marker({
    'data-aviator-phase': 'BETTING_OPEN',
    'data-provider-round-id': 'provider-round-11',
    'data-canonical-target-round-id': 'canonical-round-11',
    'data-betting-deadline': new Date(Date.now() + 20000).toISOString(),
    'data-target-identity-proof-method': 'platform_target_id_before_start',
    'data-target-not-started': 'true',
  }) });
  try {
    await _installObserver({ evaluate: (fn, selector) => fn(selector) }, '.payouts-block .payout');
    const collector = fixture.window.__aviatorCollector;
    for (let index = 1; index <= 31; index += 1) {
      fixture.payout.textContent = `${index + 1}.00x`;
      fixture.observers[0].callback([{ type: 'characterData', target: fixture.payout,
        addedNodes: [], removedNodes: [] }]);
    }
    const payoutsBeforeHeartbeat = [...collector.queue];
    fixture.intervals[0].callback();

    assert.equal(collector.queue.length, 30);
    assert.ok(collector.queue.every(event => event.phaseOnly !== true));
    assert.equal(collector.latestPhaseEvent.reason, 'provider-phase-heartbeat');
    assert.deepEqual(collector.queue, payoutsBeforeHeartbeat);
  } finally {
    fixture.restore();
  }
});

test('a provider target identity change is propagated even when phase and controls stay open', async () => {
  const deadline = new Date(Date.now() + 20000).toISOString();
  const fixture = frameFixture({ marker: marker({
    'data-aviator-phase': 'BETTING_OPEN',
    'data-provider-round-id': 'provider-round-11',
    'data-canonical-target-round-id': 'canonical-round-11',
    'data-betting-deadline': deadline,
    'data-target-identity-proof-method': 'platform_target_id_before_start',
    'data-target-not-started': 'true',
  }) });
  try {
    await _installObserver({ evaluate: (fn, selector) => fn(selector) }, '.payouts-block .payout');
    fixture.setMarker(marker({
      'data-aviator-phase': 'BETTING_OPEN',
      'data-provider-round-id': 'provider-round-12',
      'data-canonical-target-round-id': 'canonical-round-12',
      'data-betting-deadline': deadline,
      'data-target-identity-proof-method': 'platform_target_id_before_start',
      'data-target-not-started': 'true',
    }));
    fixture.observers[1].callback([{ type: 'attributes', target: fixture.betBlock,
      attributeName: 'class', addedNodes: [], removedNodes: [] }]);
    const changed = fixture.window.__aviatorCollector.latestPhaseEvent;
    assert.equal(changed.gamePhase.phase, 'BETTING_OPEN');
    assert.equal(changed.gamePhase.provider_round_id, 'provider-round-12');
    assert.equal(changed.gamePhase.canonical_target_round_id, 'canonical-round-12');
  } finally {
    fixture.restore();
  }
});
