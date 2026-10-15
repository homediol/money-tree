import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import { EventEmitter } from 'node:events';
import {
  AutoCashoutInitializer,
  mutateCashoutPanel,
  readPanelSafety,
  readCashoutPanel,
} from '../collector/AutoCashoutInitializer.js';

function panel(overrides = {}) {
  return {
    ok: true, panelCount: 2, autoTabVisible: true, autoTabSelected: true,
    autoBetEnabled: false, autoCashoutEnabled: true, multiplier: '2.00',
    inputVisible: true, pendingBetState: 'CLEAR', betButtonText: 'Bet 200 BIF',
    missing: { autoTab: false, autoBet: false, autoCashout: false, multiplierInput: false },
    ...overrides,
  };
}

function harness(states, options = {}) {
  const actions = [];
  let reads = 0;
  const frame = {
    async evaluate(fn, arg) {
      if (fn === readCashoutPanel) {
        reads += 1;
        return { ...states[arg], missing: { ...states[arg].missing } };
      }
      assert.fail('read-only setup verification must not call a UI mutation function');
    },
  };
  const health = {
    value: null,
    setCashoutInitialization(value) { this.value = value; },
    snapshot() { return { cashout_initialization: this.value }; },
  };
  const frameManager = new EventEmitter();
  frameManager.waitForFrame = async () => {
      if (options.missingFrame) throw Object.assign(new Error('frame missing'), { code: 'GAME_FRAME_MISSING' });
      return frame;
  };
  const page = {
    events: new Map(),
    isClosed: () => false,
    on(name, callback) {
      const callbacks = this.events.get(name) || [];
      callbacks.push(callback);
      this.events.set(name, callbacks);
    },
    emit(name, arg) { for (const callback of this.events.get(name) || []) callback(arg); },
  };
  const initializer = new AutoCashoutInitializer({
    frameManager, health, authInspector: async () => ({ status: options.auth || 'AUTHENTICATED' }),
    retryAfterMs: 0, panelWaitMs: 50,
  });
  return { initializer, page, frameManager, frame, health, actions, reads, getReads: () => reads, states };
}

test('both manually configured panels are independently read and verified', async () => {
  const h = harness([panel(), panel()]);
  const status = await h.initializer.initialize(h.page);
  assert.equal(status.status, 'CASHOUT_VERIFIED');
  assert.equal(status.auto_bet, 'OFF');
  assert.equal(status.panel_1_cashout, 'CASHOUT_VERIFIED');
  assert.equal(status.panel_2_cashout, 'CASHOUT_VERIFIED');
  assert.deepEqual(h.states.map(item => [item.autoBetEnabled, item.autoCashoutEnabled, item.multiplier]), [
    [false, true, '2.00'], [false, true, '2.00'],
  ]);
  assert.deepEqual(h.actions, []);
  assert.equal(h.getReads(), 2);
});

test('page-evaluated cashout probes and mutations are self-contained after Playwright serialization', () => {
  class MockInput {
    constructor(value) { this._value = value; this.offsetWidth = 20; this.offsetHeight = 10; }
    get value() { return this._value; }
    set value(value) { this._value = value; }
    dispatchEvent() {}
    blur() {}
  }
  const makeElement = (text = '', classes = []) => {
    const element = {
      innerText: text, textContent: text, offsetWidth: 20, offsetHeight: 10,
      disabled: false, attributes: {},
      classList: {
        contains: name => classes.includes(name),
        add: name => { if (!classes.includes(name)) classes.push(name); },
        remove: name => { const index = classes.indexOf(name); if (index >= 0) classes.splice(index, 1); },
      },
      getAttribute: name => element.attributes[name] ?? null,
      click: () => {
        if (classes.includes('off')) classes.splice(classes.indexOf('off'), 1);
        else classes.push('off');
      },
    };
    return element;
  };
  const betButton = makeElement('Bet 200 BIF', ['btn-success', 'bet']);
  const betTab = makeElement('Bet', ['tab']);
  const autoTab = makeElement('Auto', ['tab', 'active']);
  const autoBet = makeElement('', ['input-switch', 'off']);
  const cashout = makeElement('', ['input-switch', 'off']);
  const multiplier = new MockInput('1.10');
  const panel = {
    querySelectorAll(selector) {
      if (selector === '.navigation-switcher .tab') return [betTab, autoTab];
      if (selector === 'button, .btn') return [betButton];
      return [];
    },
    querySelector(selector) {
      return ({
        '.auto-bet .input-switch': autoBet,
        '.cash-out-switcher .input-switch': cashout,
        '.cashout-spinner input[type="text"]': multiplier,
        '.btn-success.bet': betButton,
      })[selector] || null;
    },
  };
  const globals = {
    document: { querySelectorAll: selector => selector === 'app-bet-control' ? [panel, panel] : [] },
    getComputedStyle: () => ({ display: 'block', visibility: 'visible' }),
    HTMLInputElement: MockInput,
    Event: class { constructor(type, options) { this.type = type; this.options = options; } },
  };
  const serialized = fn => vm.runInNewContext(`(${fn.toString()})`, globals);

  const read = serialized(readCashoutPanel);
  const safety = serialized(readPanelSafety);
  assert.equal(read(0).pendingBetState, 'CLEAR');
  assert.equal(safety(0).pendingBetState, 'CLEAR');

  const mutate = serialized(mutateCashoutPanel);
  assert.equal(mutate({ slot: 0, action: 'enable_auto_cashout' }).mutation_performed, false);
  assert.equal(mutate({ slot: 0, action: 'set_multiplier', value: '2.00' }).mutation_performed, false);
  assert.equal(cashout.classList.contains('off'), true);
  assert.equal(multiplier.value, '1.10');
});

test('already configured panels are a no-op, including both switches and the value', async () => {
  const configured = () => panel({ autoTabSelected: true, autoBetEnabled: false,
    autoCashoutEnabled: true, multiplier: '2.00', inputVisible: true });
  const h = harness([configured(), configured()]);
  const status = await h.initializer.initialize(h.page);
  assert.equal(status.status, 'CASHOUT_VERIFIED');
  assert.deepEqual(h.actions, []);
});

test('reports an incorrect panel without changing either panel', async () => {
  const ready = panel({ autoTabSelected: true, autoBetEnabled: false,
    autoCashoutEnabled: true, multiplier: '2.00', inputVisible: true });
  const h = harness([ready, panel({ autoTabSelected: true, autoCashoutEnabled: false,
    multiplier: '1.10', inputVisible: true })]);
  const status = await h.initializer.initialize(h.page);
  assert.equal(status.status, 'CASHOUT_MISMATCH');
  assert.equal(status.panels[0].status, 'CASHOUT_VERIFIED');
  assert.equal(status.panels[1].status, 'CASHOUT_MISMATCH');
  assert.deepEqual(h.actions, []);
});

test('an enabled Auto Bet control is reported without being changed', async () => {
  const h = harness([panel({ autoBetEnabled: true }), panel()]);
  const status = await h.initializer.initialize(h.page);
  assert.equal(status.status, 'CASHOUT_MISMATCH');
  assert.equal(h.states[0].autoBetEnabled, true);
  assert.deepEqual(h.actions, []);
});

test('missing iframe remains UNKNOWN and performs no panel mutation', async () => {
  const h = harness([panel(), panel()], { missingFrame: true });
  const status = await h.initializer.initialize(h.page);
  assert.equal(status.status, 'CASHOUT_UNAVAILABLE');
  assert.equal(status.error_reason.includes('AVIATOR_FRAME_UNAVAILABLE'), true);
  assert.deepEqual(h.actions, []);
});

test('cashout setup uses the shared game frame even when round payouts are unavailable', async () => {
  const h = harness([panel(), panel()]);
  h.frameManager.pageManager = {
    getAviatorPage: async () => h.page,
    getAviatorFrame: async () => h.frame,
  };
  h.frameManager.waitForFrame = async () => {
    throw Object.assign(new Error('payouts not ready'), { code: 'PAYOUTS_UNAVAILABLE' });
  };

  const status = await h.initializer.initialize(h.page);

  assert.equal(status.status, 'CASHOUT_VERIFIED');
  assert.deepEqual(h.states.map(item => item.multiplier), ['2.00', '2.00']);
  assert.deepEqual(h.actions, []);
});

test('missing switch is FAILED and does not change any control', async () => {
  const h = harness([panel({ missing: { ...panel().missing, autoCashout: true } }), panel()]);
  const status = await h.initializer.initialize(h.page);
  assert.equal(status.status, 'CASHOUT_UNAVAILABLE');
  assert.equal(status.panel_1_cashout, 'CASHOUT_UNAVAILABLE');
  assert.equal(status.panels[0].error_reason, 'MANUAL_AUTO_TAB_AND_CONTROLS_MUST_BE_VISIBLE');
  assert.deepEqual(h.actions, []);
});

test('multiplier readback mismatch is reported for manual correction', async () => {
  const h = harness([panel({ multiplier: '2.10' }), panel()]);
  const status = await h.initializer.initialize(h.page);
  assert.equal(status.status, 'CASHOUT_MISMATCH');
  assert.equal(status.panel_1_cashout, 'CASHOUT_MISMATCH');
  assert.equal(status.panels[0].error_reason, 'MANUAL_CASHOUT_SETTINGS_DO_NOT_MATCH_2_00X');
});

test('unknown authentication does not mutate either panel', async () => {
  const h = harness([panel(), panel()], { auth: 'UNKNOWN' });
  const status = await h.initializer.initialize(h.page);
  assert.equal(status.status, 'CASHOUT_UNAVAILABLE');
  assert.deepEqual(h.actions, []);
});

test('pending or ambiguous bet state blocks all settings changes', async () => {
  const h = harness([panel(), panel({ pendingBetState: 'PENDING' })]);
  const status = await h.initializer.initialize(h.page);
  assert.equal(status.status, 'CASHOUT_UNAVAILABLE');
  assert.deepEqual(h.actions, []);
  assert.equal(status.panels[0].error_reason, 'ACTIVE_BET_PENDING_PANEL_2');

  const ambiguous = harness([panel(), panel({ pendingBetState: 'UNKNOWN' })]);
  const ambiguousStatus = await ambiguous.initializer.initialize(ambiguous.page);
  assert.equal(ambiguousStatus.status, 'CASHOUT_UNAVAILABLE');
  assert.deepEqual(ambiguous.actions, []);
});

test('frame navigation rechecks configuration after a browser/page reconnect', async () => {
  const h = harness([panel({ autoTabSelected: true, autoCashoutEnabled: true,
    multiplier: '2.00', inputVisible: true }), panel({ autoTabSelected: true,
    autoCashoutEnabled: true, multiplier: '2.00', inputVisible: true })]);
  await h.initializer.initialize(h.page);
  const firstReadCount = h.getReads();
  h.page.emit('framenavigated', { url: () => 'https://aviaport.spribegaming.com/aviator' });
  await new Promise(resolve => setTimeout(resolve, 180));
  assert.ok(h.getReads() > firstReadCount);
  assert.equal(h.health.value.status, 'CASHOUT_VERIFIED', JSON.stringify(h.health.value));
  assert.deepEqual(h.actions, []);
});

test('cashout initialization retries when the shared FrameManager announces READY', async () => {
  const h = harness([panel(), panel()], { missingFrame: true });
  const initial = await h.initializer.initialize(h.page);
  assert.equal(initial.status, 'CASHOUT_UNAVAILABLE');
  assert.deepEqual(h.actions, []);

  h.initializer.frameManager.waitForFrame = async () => h.frame;
  h.initializer.frameManager.emit('ready', { page: h.page, frame: h.frame, method: 'direct-cdp' });
  const deadline = Date.now() + 1500;
  while (h.health.value.status !== 'CASHOUT_VERIFIED' && Date.now() < deadline) {
    await new Promise(resolve => setTimeout(resolve, 20));
  }

  assert.equal(h.health.value.status, 'CASHOUT_VERIFIED', JSON.stringify(h.health.value));
  assert.deepEqual(h.states.map(state => state.multiplier), ['2.00', '2.00']);
});

test('FrameManager READY refreshes a stale cashout readback on the same shared frame', async () => {
  const h = harness([panel({ autoTabSelected: true, autoCashoutEnabled: true,
    multiplier: '2.00', inputVisible: true }), panel({ autoTabSelected: true,
    autoCashoutEnabled: true, multiplier: '2.00', inputVisible: true })]);
  await h.initializer.initialize(h.page);
  const oldReads = h.getReads();
  const staleAt = new Date(Date.now() - 301000).toISOString();
  h.health.value.updated_at = staleAt;

  h.initializer.frameManager.emit('ready', { page: h.page, frame: h.frame, method: 'direct-cdp' });
  const deadline = Date.now() + 1500;
  while (h.health.value.updated_at === staleAt && Date.now() < deadline) {
    await new Promise(resolve => setTimeout(resolve, 20));
  }

  assert.ok(h.getReads() > oldReads);
  assert.notEqual(h.health.value.updated_at, staleAt);
  assert.equal(h.health.value.status, 'CASHOUT_VERIFIED');
  assert.deepEqual(h.actions, []);
});

test('shared collector and betting page can initialize the same game controls', async () => {
  const h = harness([panel(), panel()]);
  const status = await h.initializer.initialize(h.page, { historyPage: h.page });
  assert.equal(status.status, 'CASHOUT_VERIFIED');
  assert.deepEqual(h.actions, []);
});
