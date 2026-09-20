import assert from 'node:assert/strict';
import test from 'node:test';
import { createPanelSimulation, panelSimulationReducer as reduce } from '../src/lib/panelSimulation.js';

const config = (state, slot, field, value) => reduce(state, { type: 'CONFIGURE', slot, field, value });
const act = (state, type, slot) => reduce(state, { type, slot });

for (const slot of [0, 1]) {
  test(`panel ${slot + 1}: place, replace and cancel reserve/refund exactly once`, () => {
    let state = act(createPanelSimulation(), 'PLACE', slot);
    assert.equal(state.balance, 9900);
    const duplicate = act(state, 'PLACE', slot);
    assert.match(duplicate.error, /already/);
    assert.equal(duplicate.balance, 9900);
    state = config(state, slot, 'amount', '250');
    state = config(state, slot, 'cashout', '1.75');
    assert.equal(state.panels[slot].bet.amount, 100);
    state = act(state, 'REPLACE', slot);
    assert.equal(state.balance, 9750);
    assert.deepEqual(state.panels[slot].bet, { amount: 250, target: 1.75 });
    state = act(state, 'CANCEL', slot);
    assert.equal(state.balance, 10000);
    assert.equal(act(state, 'CANCEL', slot).balance, 10000);
  });

  test(`panel ${slot + 1}: manual cash-out cannot settle twice`, () => {
    let state = config(createPanelSimulation(), slot, 'autoCashout', false);
    state = act(act(act(state, 'PLACE', slot), 'START'), 'ADVANCE');
    state = act(state, 'CASH_OUT', slot);
    assert.equal(state.balance, 10020);
    assert.equal(state.panels[slot].payout, 120);
    assert.equal(act(state, 'CASH_OUT', slot).balance, 10020);
    assert.equal(act(state, 'REPLACE', slot).balance, 10020);
  });

  test(`panel ${slot + 1}: auto cash-out uses target even when a tick crosses it`, () => {
    let state = config(createPanelSimulation(), slot, 'cashout', '1.35');
    state = act(act(act(act(state, 'PLACE', slot), 'START'), 'ADVANCE'), 'ADVANCE');
    assert.equal(state.panels[slot].payout, 135);
    assert.equal(state.balance, 10035);
    assert.equal(act(state, 'ADVANCE').balance, 10035);
  });
}

test('both panels share the balance and settle independently', () => {
  let state = act(act(createPanelSimulation(), 'PLACE', 0), 'PLACE', 1);
  assert.equal(state.balance, 9800);
  state = act(act(state, 'START'), 'ADVANCE');
  state = act(state, 'CASH_OUT', 0);
  state = act(state, 'ADVANCE');
  assert.deepEqual(state.panels.map(panel => panel.payout), [120, 150]);
  assert.equal(state.balance, 10070);
});

test('invalid stakes, cash-outs and overspending do not reserve credits', () => {
  for (const amount of ['', '-1', '0', '2.5', 'Infinity', 'NaN', '10001']) {
    const state = act(config(createPanelSimulation(), 0, 'amount', amount), 'PLACE', 0);
    assert.ok(state.error, amount);
    assert.equal(state.balance, 10000);
  }
  for (const target of ['', '1', 'NaN', 'Infinity', '101']) {
    const state = act(config(createPanelSimulation(), 0, 'cashout', target), 'PLACE', 0);
    assert.ok(state.error, target);
    assert.equal(state.balance, 10000);
  }
  let state = act(config(createPanelSimulation(), 0, 'amount', '9950'), 'PLACE', 0);
  state = act(state, 'PLACE', 1);
  assert.ok(state.error);
  assert.equal(state.balance, 50);
  assert.equal(state.panels[1].status, 'idle');
});

test('failed replacement retains original stake and target', () => {
  let state = act(act(createPanelSimulation(), 'PLACE', 0), 'PLACE', 1);
  state = act(config(state, 0, 'amount', '10000'), 'REPLACE', 0);
  assert.ok(state.error);
  assert.equal(state.balance, 9800);
  assert.deepEqual(state.panels[0].bet, { amount: 100, target: 2 });
});

test('crash boundary loses unsettled bets and next round never repeats them', () => {
  let state = config(createPanelSimulation(), 0, 'cashout', '3.00');
  state = config(state, 1, 'autoCashout', false);
  state = act(act(act(state, 'PLACE', 0), 'PLACE', 1), 'START');
  for (let i = 0; i < 5; i++) state = act(state, 'ADVANCE');
  assert.equal(state.phase, 'crashed');
  assert.deepEqual(state.panels.map(panel => panel.status), ['lost', 'lost']);
  assert.equal(act(state, 'CASH_OUT', 0).balance, 9800);
  state = act(state, 'NEXT_ROUND');
  assert.equal(state.round, 2);
  assert.equal(state.balance, 9800);
  assert.ok(state.panels.every(panel => panel.status === 'idle' && !panel.bet));
  assert.ok(act(state, 'START').error);
  assert.deepEqual(act(state, 'RESET'), createPanelSimulation());
});
