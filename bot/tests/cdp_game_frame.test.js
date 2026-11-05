import test from 'node:test';
import assert from 'node:assert/strict';
import { CdpGameFrame } from '../collector/CdpGameFrame.js';

test('CDP binding queue drains round events before the coalesced latest phase', async () => {
  const payout = { multipliers: [2.1], sig: '2.10' };
  const phase = { phaseOnly: true, reason: 'provider-phase-heartbeat',
    gamePhase: { phase: 'BETTING_OPEN' } };
  const frame = Object.create(CdpGameFrame.prototype);
  frame.contextId = 9;
  frame.collectorBindingName = '__collector_binding';
  frame.eventDriven = true;
  frame.socket = { closed: false };
  frame.eventQueue = [];
  frame.maxQueuedEvents = 256;
  frame.eventQueueOverflow = false;
  frame.latestPhaseEvent = null;

  frame._onCollectorBinding({ name: frame.collectorBindingName, executionContextId: 9,
    payload: JSON.stringify(phase) });
  frame._onCollectorBinding({ name: frame.collectorBindingName, executionContextId: 9,
    payload: JSON.stringify(payout) });

  assert.deepEqual(await frame.waitForCollectorMutation(100), payout);
  assert.deepEqual(await frame.waitForCollectorMutation(100), phase);
});

test('CDP collector waits for pushed events without evaluating or polling the page', async () => {
  const frame = Object.create(CdpGameFrame.prototype);
  frame.contextId = 9;
  frame.collectorBindingName = '__collector_binding';
  frame.eventDriven = true;
  frame.socket = { closed: false };
  frame.eventQueue = [];
  frame.maxQueuedEvents = 256;
  frame.eventQueueOverflow = false;
  frame.latestPhaseEvent = null;
  frame.evaluate = async () => assert.fail('event wait must not poll Runtime.evaluate');

  const waiting = frame.waitForCollectorMutation(500);
  const event = { multipliers: [1.8], sig: '1.80' };
  frame._onCollectorBinding({ name: frame.collectorBindingName, executionContextId: 9,
    payload: JSON.stringify(event) });
  assert.deepEqual(await waiting, event);
});

test('CDP binding ignores events from another context or binding', async () => {
  const frame = Object.create(CdpGameFrame.prototype);
  frame.contextId = 9;
  frame.collectorBindingName = '__collector_binding';
  frame.eventDriven = true;
  frame.socket = { closed: false };
  frame.eventQueue = [];
  frame.maxQueuedEvents = 256;
  frame.eventQueueOverflow = false;
  frame.latestPhaseEvent = null;

  frame._onCollectorBinding({ name: '__other_binding', executionContextId: 9,
    payload: JSON.stringify({ multipliers: [1.8] }) });
  frame._onCollectorBinding({ name: frame.collectorBindingName, executionContextId: 10,
    payload: JSON.stringify({ multipliers: [1.8] }) });
  assert.equal(frame.eventQueue.length, 0);
});

test('CDP queue overflow is surfaced after buffered rounds drain for snapshot recovery', async () => {
  const frame = Object.create(CdpGameFrame.prototype);
  frame.contextId = 9;
  frame.collectorBindingName = '__collector_binding';
  frame.eventDriven = true;
  frame.maxQueuedEvents = 1;
  frame.socket = { closed: false };
  frame.eventQueue = [];
  frame.latestPhaseEvent = null;
  frame.eventQueueOverflow = false;

  const first = { multipliers: [1.3], sig: '1.30' };
  const overflowed = { multipliers: [1.7], sig: '1.70' };
  frame._onCollectorBinding({ name: frame.collectorBindingName, executionContextId: 9,
    payload: JSON.stringify(first) });
  frame._onCollectorBinding({ name: frame.collectorBindingName, executionContextId: 9,
    payload: JSON.stringify(overflowed) });

  assert.deepEqual(await frame.waitForCollectorMutation(100), first);
  assert.deepEqual(await frame.waitForCollectorMutation(100), {
    error: 'collector_event_queue_overflow',
  });
});

test('CDP compatibility polling still drains the in-frame queue when bindings are unavailable', async () => {
  const payout = { multipliers: [1.4], sig: '1.40' };
  const previousWindow = globalThis.window;
  globalThis.window = { __aviatorCollector: { queue: [payout], latestPhaseEvent: null } };
  try {
    const frame = Object.create(CdpGameFrame.prototype);
    frame.eventDriven = false;
    frame.evaluate = async pageFunction => pageFunction();
    assert.deepEqual(await frame.waitForCollectorMutation(100), payout);
  } finally {
    if (previousWindow === undefined) delete globalThis.window;
    else globalThis.window = previousWindow;
  }
});
