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
    ok: true, panelCount: 2, autoTabVisible: true, autoTabSelected: false,
    autoBetEnabled: false, autoCashoutEnabled: false, multiplier: '1.10',
    inputVisible: false, pendingBetState: 'CLEAR', betButtonText: 'Bet 200 BIF',
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
      assert.equal(fn, mutateCashoutPanel);
      actions.push({ slot: arg.slot, action: arg.action });
      const current = states[arg.slot];
      if (current.pendingBetState !== 'CLEAR') {
        return { ok: false, reason: current.pendingBetState === 'PENDING'
          ? 'ACTIVE_BET_PENDING' : 'BETTING_STATE_UNKNOWN' };
      }
      if (arg.action === 'select_auto_tab') current.autoTabSelected = true;
      if (arg.action === 'disable_auto_bet') current.autoBetEnabled = false;
      if (arg.action === 'enable_auto_cashout') current.autoCashoutEnabled = true;
      if (arg.action === 'set_multiplier') current.multiplier = current.rejectMultiplier ? '2.10' : '2.00';
      return { ok: true, changed: true };
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

test('both disabled panels are independently initialized with Auto Bet kept off', async () => {
  const h = harness([panel(), panel()]);
  const status = await h.initializer.initialize(h.page);
  assert.equal(status.status, 'READY');
  assert.equal(status.auto_bet, 'OFF');
  assert.equal(status.panel_1_cashout, 'READY');
  assert.equal(status.panel_2_cashout, 'READY');
  assert.deepEqual(h.states.map(item => [item.autoBetEnabled, item.autoCashoutEnabled, item.multiplier]), [
    [false, true, '2.00'], [false, true, '2.00'],
  ]);
  assert.equal(h.actions.filter(item => item.action === 'set_multiplier').length, 2);
  assert.ok(h.getReads() >= 8, 'the panel is re-read after changing the Auto tab and controls');
  assert.ok(h.actions.every(item => !/bet|submit/i.test(item.action)));
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
  assert.equal(mutate({ slot: 0, action: 'enable_auto_cashout' }).ok, true);
  assert.equal(mutate({ slot: 0, action: 'set_multiplier', value: '2.00' }).ok, true);
  assert.equal(cashout.classList.contains('off'), false);
  assert.equal(multiplier.value, '2.00');
});

test('already configured panels are a no-op, including both switches and the value', async () => {
  const configured = () => panel({ autoTabSelected: true, autoBetEnabled: false,
    autoCashoutEnabled: true, multiplier: '2.00', inputVisible: true });
  const h = harness([configured(), configured()]);
  const status = await h.initializer.initialize(h.page);
  assert.equal(status.status, 'READY');
  assert.deepEqual(h.actions, []);
});

test('repairs only the incorrect panel when panel one is already ready', async () => {
  const ready = panel({ autoTabSelected: true, autoBetEnabled: false,
    autoCashoutEnabled: true, multiplier: '2.00', inputVisible: true });
  const h = harness([ready, panel()]);
  const status = await h.initializer.initialize(h.page);
  assert.equal(status.status, 'READY');
  assert.ok(h.actions.length > 0);
  assert.ok(h.actions.every(item => item.slot === 1));
});

test('an enabled Auto Bet control is turned off before cashout is verified', async () => {
  const h = harness([panel({ autoBetEnabled: true }), panel()]);
  const status = await h.initializer.initialize(h.page);
  assert.equal(status.status, 'READY');
  assert.equal(h.states[0].autoBetEnabled, false);
  assert.equal(h.actions[0].action, 'select_auto_tab');
  assert.ok(h.actions.some(item => item.slot === 0 && item.action === 'disable_auto_bet'));
});

test('missing iframe remains UNKNOWN and performs no panel mutation', async () => {
  const h = harness([panel(), panel()], { missingFrame: true });
  const status = await h.initializer.initialize(h.page);
  assert.equal(status.status, 'UNKNOWN');
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

  assert.equal(status.status, 'READY');
  assert.deepEqual(h.states.map(item => item.multiplier), ['2.00', '2.00']);
  assert.equal(h.actions.filter(item => item.action === 'enable_auto_cashout').length, 2);
});

test('missing switch is FAILED and does not change any control', async () => {
  const h = harness([panel({ missing: { ...panel().missing, autoCashout: true } }), panel()]);
  const status = await h.initializer.initialize(h.page);
  assert.equal(status.status, 'FAILED');
  assert.equal(status.panel_1_cashout, 'FAILED');
  assert.equal(status.panels[0].error_reason, 'AUTOCASHOUT_NOT_FOUND');
  assert.ok(h.actions.length > 0);
  assert.ok(h.actions.every(item => item.slot === 1));
});

test('multiplier readback mismatch is FAILED', async () => {
  const h = harness([panel({ rejectMultiplier: true }), panel()]);
  const status = await h.initializer.initialize(h.page);
  assert.equal(status.status, 'FAILED');
  assert.equal(status.panel_1_cashout, 'FAILED');
  assert.equal(status.panels[0].error_reason, 'CASHOUT_READBACK_MISMATCH');
});

test('unknown authentication does not mutate either panel', async () => {
  const h = harness([panel(), panel()], { auth: 'UNKNOWN' });
  const status = await h.initializer.initialize(h.page);
  assert.equal(status.status, 'UNKNOWN');
  assert.deepEqual(h.actions, []);
});

test('pending or ambiguous bet state blocks all settings changes', async () => {
  const h = harness([panel(), panel({ pendingBetState: 'PENDING' })]);
  const status = await h.initializer.initialize(h.page);
  assert.equal(status.status, 'UNKNOWN');
  assert.deepEqual(h.actions, []);
  assert.equal(status.panels[0].error_reason, 'ACTIVE_BET_PENDING_PANEL_2');

  const ambiguous = harness([panel(), panel({ pendingBetState: 'UNKNOWN' })]);
  const ambiguousStatus = await ambiguous.initializer.initialize(ambiguous.page);
  assert.equal(ambiguousStatus.status, 'UNKNOWN');
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
  assert.equal(h.health.value.status, 'READY', JSON.stringify(h.health.value));
  assert.deepEqual(h.actions, []);
});

test('cashout initialization retries when the shared FrameManager announces READY', async () => {
  const h = harness([panel(), panel()], { missingFrame: true });
  const initial = await h.initializer.initialize(h.page);
  assert.equal(initial.status, 'UNKNOWN');
  assert.deepEqual(h.actions, []);

  h.initializer.frameManager.waitForFrame = async () => h.frame;
  h.initializer.frameManager.emit('ready', { page: h.page, frame: h.frame, method: 'direct-cdp' });
  const deadline = Date.now() + 1500;
  while (h.health.value.status !== 'READY' && Date.now() < deadline) {
    await new Promise(resolve => setTimeout(resolve, 20));
  }

  assert.equal(h.health.value.status, 'READY', JSON.stringify(h.health.value));
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
  assert.equal(h.health.value.status, 'READY');
  assert.deepEqual(h.actions, []);
});

test('shared collector and betting page can initialize the same game controls', async () => {
  const h = harness([panel(), panel()]);
  const status = await h.initializer.initialize(h.page, { historyPage: h.page });
  assert.equal(status.status, 'READY');
  assert.equal(h.actions.filter(action => action.action === 'enable_auto_cashout').length, 2);
  assert.equal(h.actions.some(action => action.action === 'place_bet'), false);
});
