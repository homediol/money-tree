import test from 'node:test';
import assert from 'node:assert/strict';
import {
  BetAmountSynchronizer,
  mutateStakePanel,
  readStakePanel,
} from '../collector/BetAmountSynchronizer.js';
import { readPanelSafety } from '../collector/AutoCashoutInitializer.js';

function harness({ panel = 1, currency = 'BIF', pending = ['CLEAR', 'CLEAR'], auth = 'AUTHENTICATED' } = {}) {
  const stakes = ['100', '25'];
  const actions = [];
  const frame = {
    async evaluate(fn, arg) {
      if (fn === readPanelSafety) return { ok: true, panelCount: 2, pendingBetState: pending[arg] };
      if (fn === readStakePanel) return {
        ok: true, panelCount: 2, betTabSelected: true, inputVisible: true,
        inputType: 'text', value: stakes[arg], buttonText: `Bet ${stakes[arg]} BIF`,
        buttonVisible: true, buttonEnabled: true, currency: 'BIF', pendingBetState: pending[arg],
      };
      assert.fail('stake verification must not evaluate a mutation function');
    },
  };
  const published = [];
  const sync = new BetAmountSynchronizer({
    frameManager: { waitForFrame: async () => frame },
    health: { setBetAmountInitialization: (value) => published.push(value),
      snapshot: () => ({ bet_amount_initialization: published.at(-1) }) },
    configProvider: async () => ({ status: 'READY', execution_panel: panel,
      requested_stake: 25, requested_stake_currency: currency, platform_currency: currency,
      session_id: 'rules-session', authentication_state: 'AUTHENTICATED',
      page_role: 'SHARED_AVIATOR', browser_topology_verified: true }),
    authInspector: async () => ({ status: auth }),
  });
  const page = { isClosed: () => false };
  return { sync, page, frame, stakes, actions, published };
}

test('verifies the manually entered stake on the selected panel without mutation', async () => {
  const h = harness({ panel: 1 });
  const result = await h.sync.initialize(h.page);
  assert.equal(result.status, 'STAKE_VERIFIED');
  assert.deepEqual(h.stakes, ['100', '25']);
  assert.deepEqual(h.actions, []);
});

test('manual fixed-stake configuration remains untouched on later observer events', async () => {
  const h = harness({ panel: 1 });
  const first = await h.sync.initialize(h.page);
  assert.equal(first.status, 'STAKE_VERIFIED');
  const actionsAfterFirstSync = h.actions.length;
  h.sync.lastAttemptAt -= h.sync.retryAfterMs + 1;

  const later = await h.sync.initialize(h.page);

  assert.equal(later.status, 'STAKE_VERIFIED');
  assert.equal(h.actions.length, actionsAfterFirstSync);
  assert.deepEqual(h.stakes, ['100', '25']);
});

test('currency mismatch blocks before reading or changing either panel', async () => {
  const h = harness({ currency: 'RWF' });
  h.sync.configProvider = async () => ({ status: 'BLOCKED', reason: 'STAKE_CURRENCY_MISMATCH',
    execution_panel: 0, requested_stake: 100, requested_stake_currency: 'RWF',
    platform_currency: 'BIF', session_id: 'rules-session' });
  const result = await h.sync.initialize(h.page);
  assert.equal(result.status, 'BLOCKED');
  assert.equal(result.error_reason, 'STAKE_CURRENCY_MISMATCH');
  assert.deepEqual(h.actions, []);
});

test('stake setup resolves the shared game frame without waiting for payout history', async () => {
  const h = harness({ panel: 1 });
  h.sync.frameManager.pageManager = {
    getAviatorPage: async () => h.page,
    getAviatorFrame: async () => h.frame,
  };
  h.sync.frameManager.waitForFrame = async () => {
    throw Object.assign(new Error('payouts not ready'), { code: 'PAYOUTS_UNAVAILABLE' });
  };

  const result = await h.sync.initialize(h.page);

  assert.equal(result.status, 'STAKE_VERIFIED');
  assert.equal(h.stakes[1], '25');
});

test('stake setup uses the collector active OOPIF instead of the enclosing Aviaport frame', async () => {
  const h = harness({ panel: 0 });
  h.sync.configProvider = async () => ({ status: 'READY', execution_panel: 0,
    requested_stake: 100, requested_stake_currency: 'BIF', platform_currency: 'BIF',
    session_id: 'rules-session', authentication_state: 'AUTHENTICATED',
    page_role: 'SHARED_AVIATOR', browser_topology_verified: true });
  const outerFrame = { isDetached: () => false };
  h.sync.frameManager.pageManager = {
    getAviatorPage: async () => h.page,
    getAviatorFrame: async () => outerFrame,
  };
  h.sync.frameManager.getActiveFrame = () => h.frame;

  const result = await h.sync.initialize(h.page);

  assert.equal(result.status, 'STAKE_VERIFIED');
  assert.equal(h.stakes[0], '100');
  assert.equal(h.stakes[1], '25');
  assert.deepEqual(h.actions, []);
});

test('stake DOM functions are self-contained when serialized into the Aviator iframe', () => {
  const original = {
    document: globalThis.document,
    getComputedStyle: globalThis.getComputedStyle,
    HTMLInputElement: globalThis.HTMLInputElement,
    Event: globalThis.Event,
  };
  class FakeInput {
    constructor(value) {
      this._value = value;
      this.type = 'text';
      this.placeholder = 'Amount';
      this.disabled = false;
      this.offsetWidth = 20;
      this.offsetHeight = 10;
      this.events = [];
    }
    get value() { return this._value; }
    set value(value) { this._value = String(value); }
    getAttribute() { return null; }
    dispatchEvent(event) { this.events.push(event.type); }
    blur() {}
  }
  class FakeEvent { constructor(type) { this.type = type; } }
  Object.defineProperty(FakeInput.prototype, 'value', {
    configurable: true,
    get() { return this._value; },
    set(value) { this._value = String(value); },
  });
  const makePanel = amount => {
    const input = new FakeInput(amount);
    const betButton = {
      innerText: `Bet ${amount} BIF`, textContent: `Bet ${amount} BIF`,
      disabled: false, offsetWidth: 20, offsetHeight: 10,
      classList: { contains: () => false },
      getAttribute: () => null,
    };
    const betTab = { textContent: 'Bet', offsetWidth: 20, offsetHeight: 10,
      classList: { contains: () => true } };
    const betBlock = { querySelector: () => input };
    return {
      input,
      panel: {
        querySelector: selector => selector === '.bet-block' ? betBlock
          : selector.includes('input[type="text"]') ? input
            : selector === '.btn-success.bet' ? betButton : null,
        querySelectorAll: selector => selector === '.navigation-switcher .tab' ? [betTab]
          : selector === 'button, .btn' ? [betButton] : [],
      },
    };
  };
  const fixtures = [makePanel('200'), makePanel('200')];
  globalThis.document = { querySelectorAll: () => fixtures.map(item => item.panel) };
  globalThis.getComputedStyle = () => ({ display: 'block', visibility: 'visible' });
  globalThis.HTMLInputElement = FakeInput;
  globalThis.Event = FakeEvent;
  try {
    const serializedRead = Function(`return (${readStakePanel.toString()})(0)`)();
    assert.equal(serializedRead.ok, true);
    assert.equal(serializedRead.panelCount, 2);
    assert.equal(serializedRead.pendingBetState, 'CLEAR');

    const serializedMutation = Function(`return (${mutateStakePanel.toString()})(arguments[0])`)({
      slot: 0, action: 'set_stake', value: '100',
    });
    assert.deepEqual(serializedMutation, { ok: false, reason: 'MANUAL_OPERATOR_CONFIGURATION_REQUIRED', mutation_performed: false });
    assert.equal(fixtures[0].input.value, '200');
    assert.deepEqual(fixtures[0].input.events, []);
    assert.equal(fixtures[1].input.value, '200');
  } finally {
    for (const [key, value] of Object.entries(original)) {
      if (value === undefined) delete globalThis[key];
      else globalThis[key] = value;
    }
  }
});

test('unknown authentication and pending bets block amount synchronization', async () => {
  const unauthenticated = harness({ auth: 'UNKNOWN' });
  assert.equal((await unauthenticated.sync.initialize(unauthenticated.page)).status, 'BLOCKED');
  assert.deepEqual(unauthenticated.actions, []);

  const pending = harness({ pending: ['CLEAR', 'PENDING'] });
  const result = await pending.sync.initialize(pending.page);
  assert.equal(result.status, 'STAKE_UNAVAILABLE');
  assert.equal(result.error_reason, 'ACTIVE_BET_PENDING_PANEL_2');
  assert.deepEqual(pending.actions, []);
});
