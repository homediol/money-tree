// Local test fixtures only. This module has no browser-driver or API access.
export const TEST_MULTIPLIERS = [1, 1.2, 1.5, 2, 2.5, 3];
export const TEST_CRASH = 3;
const money = value => Math.round(value * 100) / 100;

export function createPanelSimulation() {
  return {
    balance: 10000, round: 1, phase: 'ready', tick: 0, error: null,
    events: [], nextEventId: 1,
    panels: [0, 1].map(slot => ({
      slot, amount: '100', cashout: slot === 0 ? '2.00' : '1.50',
      autoCashout: true, status: 'idle', bet: null, payout: 0,
    })),
  };
}

function record(state, panel, message) {
  state.events = [{
    id: state.nextEventId++, round: state.round,
    panel: panel == null ? null : panel.slot + 1, message,
  }, ...state.events].slice(0, 30);
}

function settle(state, panel, multiplier, automatic) {
  panel.payout = money(panel.bet.amount * multiplier);
  panel.status = 'cashed_out';
  state.balance = money(state.balance + panel.payout);
  record(state, panel, `${automatic ? 'Auto cash-out' : 'Manual cash-out'} at ${multiplier.toFixed(2)}×: ${panel.payout} test credits returned.`);
}

export function panelSimulationReducer(previous, action) {
  if (action.type === 'RESET') return createPanelSimulation();
  const state = {
    ...previous, error: null,
    panels: previous.panels.map(panel => ({ ...panel })),
  };
  const fail = error => ({ ...previous, error });
  const panel = state.panels.find(item => item.slot === action.slot);

  if (['CONFIGURE', 'PLACE', 'REPLACE', 'CANCEL', 'CASH_OUT'].includes(action.type) && !panel) {
    return fail('Select panel 1 or panel 2.');
  }

  switch (action.type) {
    case 'CONFIGURE':
      if (state.phase !== 'ready') return fail('Edit the inputs before starting the test round.');
      if (!['amount', 'cashout', 'autoCashout'].includes(action.field)) return fail('Unknown input.');
      panel[action.field] = action.field === 'autoCashout' ? Boolean(action.value) : String(action.value);
      return state;

    case 'PLACE':
    case 'REPLACE': {
      if (state.phase !== 'ready') return fail('Test bets can only be queued before the round starts.');
      if (action.type === 'PLACE' && panel.status !== 'idle') return fail('This panel already has a queued test bet.');
      if (action.type === 'REPLACE' && panel.status !== 'queued') return fail('Queue a test bet before replacing it.');
      const amount = Number(panel.amount);
      const target = Number(panel.cashout);
      if (!Number.isSafeInteger(amount) || amount < 1) return fail('Enter a positive whole number of test credits.');
      if (panel.autoCashout && (!Number.isFinite(target) || target < 1.01 || target > 100)) {
        return fail('Auto cash-out must be between 1.01× and 100×.');
      }
      const available = money(state.balance + (panel.bet?.amount || 0));
      if (amount > available) return fail('Insufficient test credits across the two panels.');
      state.balance = money(available - amount);
      panel.bet = { amount, target: panel.autoCashout ? money(target) : null };
      panel.status = 'queued';
      panel.payout = 0;
      record(state, panel, `${action.type === 'REPLACE' ? 'Replaced' : 'Queued'} test bet: ${amount} credits; ${panel.bet.target == null ? 'manual cash-out' : `auto cash-out ${panel.bet.target.toFixed(2)}×`}.`);
      return state;
    }

    case 'CANCEL':
      if (state.phase !== 'ready' || panel.status !== 'queued') return fail('Only a queued test bet can be cancelled.');
      state.balance = money(state.balance + panel.bet.amount);
      panel.bet = null;
      panel.status = 'idle';
      record(state, panel, 'Cancelled test bet; reserved credits returned.');
      return state;

    case 'START':
      if (state.phase !== 'ready' || !state.panels.some(item => item.status === 'queued')) {
        return fail('Queue at least one test bet before starting.');
      }
      state.phase = 'running';
      state.panels.forEach(item => { if (item.status === 'queued') item.status = 'active'; });
      record(state, null, 'Scripted test round started at 1.00×.');
      return state;

    case 'ADVANCE': {
      if (state.phase !== 'running') return fail('Start the test round first.');
      state.tick += 1;
      const multiplier = TEST_MULTIPLIERS[state.tick];
      for (const item of state.panels) {
        if (item.status !== 'active') continue;
        // Only targets strictly below the scripted crash settle successfully.
        if (item.bet.target != null && item.bet.target <= multiplier && item.bet.target < TEST_CRASH) {
          settle(state, item, item.bet.target, true);
        } else if (multiplier >= TEST_CRASH) {
          item.status = 'lost';
          record(state, item, `Test bet lost: ${item.bet.amount} credits.`);
        }
      }
      if (multiplier >= TEST_CRASH) {
        state.phase = 'crashed';
        record(state, null, 'Scripted test round ended at 3.00×.');
      }
      return state;
    }

    case 'CASH_OUT':
      if (state.phase !== 'running' || panel.status !== 'active') return fail('This panel has no active test bet to cash out.');
      settle(state, panel, TEST_MULTIPLIERS[state.tick], false);
      return state;

    case 'NEXT_ROUND':
      if (state.phase !== 'crashed') return fail('Finish the current test round first.');
      state.phase = 'ready';
      state.tick = 0;
      state.round += 1;
      state.panels.forEach(item => { item.status = 'idle'; item.bet = null; item.payout = 0; });
      return state;

    default:
      return fail('Unknown test action.');
  }
}
