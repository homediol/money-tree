import test from 'node:test';
import assert from 'node:assert/strict';
import {
  BetAmountSynchronizer,
  mutateStakePanel,
  readStakePanel,
} from '../collector/BetAmountSynchronizer.js';
import { readCashoutPanel, readPanelSafety } from '../collector/AutoCashoutInitializer.js';

function harness({ panel = 1, currency = 'BIF', pending = ['CLEAR', 'CLEAR'], auth = 'AUTHENTICATED' } = {}) {
  const stakes = ['100', '200'];
  const tabs = [false, false];
  const actions = [];
  const cashouts = [0, 1].map(() => ({
    ok: true, panelCount: 2, autoTabSelected: true, autoBetEnabled: false,
    autoCashoutEnabled: true, multiplier: '2.00',
  }));
  const frame = {
    async evaluate(fn, arg) {
      if (fn === readPanelSafety) return { ok: true, panelCount: 2, pendingBetState: pending[arg] };
      if (fn === readStakePanel) return {
        ok: true, panelCount: 2, betTabSelected: tabs[arg], inputVisible: tabs[arg],
        inputType: 'text', value: stakes[arg], pendingBetState: pending[arg],
      };
      if (fn === readCashoutPanel) return { ...cashouts[arg] };
      assert.equal(fn, mutateStakePanel);
      actions.push({ ...arg });
      if (arg.action === 'select_bet_tab') tabs[arg.slot] = true;
      if (arg.action === 'select_auto_tab') tabs[arg.slot] = false;
      if (arg.action === 'set_stake') stakes[arg.slot] = String(arg.value);
      return { ok: true, changed: true, value: stakes[arg.slot] };
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

test('synchronizes only the selected panel and never submits a bet', async () => {
  const h = harness({ panel: 1 });
  const result = await h.sync.initialize(h.page);
  assert.equal(result.status, 'READY');
  assert.deepEqual(h.stakes, ['100', '25']);
  assert.deepEqual(h.actions.filter((item) => item.action === 'set_stake'), [
    { slot: 1, action: 'set_stake', value: '25' },
  ]);
  assert.ok(h.actions.every((item) => item.action !== 'place_bet' && item.action !== 'click_bet'));
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

  assert.equal(result.status, 'READY');
  assert.equal(h.stakes[1], '25');
});

test('unknown authentication and pending bets block amount synchronization', async () => {
  const unauthenticated = harness({ auth: 'UNKNOWN' });
  assert.equal((await unauthenticated.sync.initialize(unauthenticated.page)).status, 'BLOCKED');
  assert.deepEqual(unauthenticated.actions, []);

  const pending = harness({ pending: ['CLEAR', 'PENDING'] });
  const result = await pending.sync.initialize(pending.page);
  assert.equal(result.status, 'UNKNOWN');
  assert.equal(result.error_reason, 'ACTIVE_BET_PENDING_PANEL_2');
  assert.deepEqual(pending.actions, []);
});
