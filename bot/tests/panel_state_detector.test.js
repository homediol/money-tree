import test from 'node:test';
import assert from 'node:assert/strict';
import { classifyPanelConfigurationObservation, classifyPanelObservation,
  panelObservationTransition } from '../collector/PanelStateDetector.js';

const base = {
  panel_id: 'PANEL_1', panel_visible: true, button_visible: true, button_enabled: true,
  source: 'AVIATOR_BETTING_PANEL_MUTATION_OBSERVER',
  frame_identity: 'https://aviator-next.spribegaming.com/',
  observed_at: new Date().toISOString(),
};

const screenshotStates = [
  {main_label: 'Bet', button_text: 'Bet 200 BIF', displayed_stake_text: 'Bet 200 BIF',
    button_color: 'rgb(40, 167, 69)', visible_action_labels: ['Bet 200 BIF']},
  {main_label: 'Cancel', button_text: 'Cancel', secondary_text: 'Waiting for next round',
    button_color: 'rgb(220, 53, 69)', visible_action_labels: ['Cancel Waiting for next round'], pending_indicator: true},
  {main_label: 'Cash Out', button_text: 'Cash Out 121 BIF', cashout_text: 'Cash Out 121 BIF',
    button_color: 'rgb(255, 193, 7)', visible_action_labels: ['Cash Out 121 BIF'], active_indicator: true},
];

test('screenshot fixture detects green Bet, red queued Cancel, and amber Cash Out', () => {
  const [available, queued, active] = screenshotStates.map(row => classifyPanelObservation({ ...base, ...row }));
  assert.deepEqual([available.state, available.displayed_stake, available.currency], ['BET_AVAILABLE', 200, 'BIF']);
  assert.deepEqual([queued.state, queued.pending_bet_state], ['BET_QUEUED', 'PENDING']);
  assert.deepEqual([active.state, active.cashout_amount, active.currency], ['BET_ACTIVE', 121, 'BIF']);
  assert.equal(active.evidence.recognized_color, 'AMBER');
});

test('Panel 1 and Panel 2 remain independent', () => {
  const panelOne = classifyPanelObservation({ ...base, panel_id: 'PANEL_1', ...screenshotStates[2] });
  const panelTwo = classifyPanelObservation({ ...base, panel_id: 'PANEL_2', button_text: 'Bet 50 BIF',
    main_label: 'Bet', displayed_stake_text: 'Bet 50 BIF', button_color: 'rgb(40, 167, 69)',
    visible_action_labels: ['Bet 50 BIF'] });
  assert.deepEqual([panelOne.panel_id, panelOne.state], ['PANEL_1', 'BET_ACTIVE']);
  assert.deepEqual([panelTwo.panel_id, panelTwo.state], ['PANEL_2', 'BET_AVAILABLE']);
});

test('text changes classify with neutral color; contradictory recognized colors return UNKNOWN', () => {
  assert.equal(classifyPanelObservation({ ...base, ...screenshotStates[0], button_color: 'rgb(90, 90, 90)' }).state,
    'BET_AVAILABLE');
  assert.equal(classifyPanelObservation({ ...base, ...screenshotStates[1], button_color: 'rgb(40, 167, 69)' }).state,
    'UNKNOWN');
  assert.equal(classifyPanelObservation({ ...base, ...screenshotStates[0], button_color: 'rgb(255, 193, 7)' }).state,
    'UNKNOWN');
  assert.equal(classifyPanelObservation({ ...base, ...screenshotStates[2], button_color: 'rgb(255, 165, 0)' }).state,
    'UNKNOWN');
});

test('text is detected without a color change and a color-only conflict fails closed', () => {
  const neutral = { ...base, button_color: 'rgb(90, 90, 90)' };
  assert.equal(classifyPanelObservation({ ...neutral, ...screenshotStates[0] }).state, 'BET_AVAILABLE');
  assert.equal(classifyPanelObservation({ ...neutral, ...screenshotStates[1] }).state, 'BET_QUEUED');
  assert.equal(classifyPanelObservation({ ...neutral, ...screenshotStates[2] }).state, 'BET_ACTIVE');
  const textUnchanged = { ...base, ...screenshotStates[0], button_color: 'rgb(40, 167, 69)' };
  const colorChangedOnly = { ...textUnchanged, button_color: 'rgb(255, 193, 7)' };
  assert.equal(classifyPanelObservation(textUnchanged).state, 'BET_AVAILABLE');
  assert.equal(classifyPanelObservation(colorChangedOnly).state, 'UNKNOWN');
  const transparent = classifyPanelObservation({ ...base, ...screenshotStates[0],
    button_color: 'rgba(0, 0, 0, 0)' });
  assert.equal(transparent.state, 'BET_AVAILABLE');
  assert.equal(transparent.evidence.recognized_color, null);
});

test('queued label can be in a sibling within its panel; pending is not a receipt', () => {
  const queued = classifyPanelObservation({ ...base, main_label: 'Cancel', button_text: 'Cancel',
    secondary_text: 'Waiting for next round', visible_action_labels: ['Cancel'],
    button_color: 'rgb(220, 53, 69)', displayed_stake_text: '200', currency_hint: 'BIF' });
  assert.equal(queued.state, 'BET_QUEUED');
  assert.equal(queued.pending_bet_state, 'PENDING');
  assert.deepEqual([queued.displayed_stake, queued.currency], [200, 'BIF']);
  assert.equal(queued.evidence.reason, 'CANCEL_AND_NEXT_ROUND_WAIT_VISIBLE');
});

test('missing controls, stale observations, and unknown frame identity fail closed', () => {
  assert.equal(classifyPanelObservation({ ...base, panel_visible: false }).state, 'UNKNOWN');
  assert.equal(classifyPanelObservation({ ...base, button_visible: false }).state, 'UNKNOWN');
  assert.equal(classifyPanelObservation({ ...base, frame_identity: null }).state, 'UNKNOWN');
  assert.equal(classifyPanelObservation({ ...base, observed_at: new Date(Date.now() - 20000).toISOString() }).state,
    'UNKNOWN');
});

test('disabled Bet is UNKNOWN without explicit reset evidence; uppercase action is not a currency', () => {
  const disabled = classifyPanelObservation({ ...base, ...screenshotStates[0], button_enabled: false });
  assert.equal(disabled.state, 'UNKNOWN');
  const reset = classifyPanelObservation({ ...base, panel_visible: true, button_visible: false,
    button_enabled: false, data_state: 'reset' });
  assert.equal(reset.state, 'BET_RESET');
  const uppercase = classifyPanelObservation({ ...base, main_label: 'BET', button_text: 'BET 200 BIF',
    displayed_stake_text: 'BET 200 BIF' });
  assert.equal(uppercase.currency, 'BIF');
});

test('queued to active to available transitions are recorded once and stale events are ignored', () => {
  const at = offset => new Date(Date.now() + offset).toISOString();
  const available = classifyPanelObservation({ ...base, ...screenshotStates[0], observed_at: at(0) });
  const queued = classifyPanelObservation({ ...base, ...screenshotStates[1], observed_at: at(10) });
  const active = classifyPanelObservation({ ...base, ...screenshotStates[2], observed_at: at(20) });
  assert.deepEqual(panelObservationTransition(available, queued), {
    changed: true, duplicate: false, stale: false, frame_reloaded: false,
    from_state: 'BET_AVAILABLE', to_state: 'BET_QUEUED', current_state: 'BET_QUEUED',
    observed_at: queued.observed_at,
  });
  assert.equal(panelObservationTransition(queued, active).to_state, 'BET_ACTIVE');
  const reset = panelObservationTransition(active, available);
  assert.equal(reset.to_state, 'BET_RESET');
  assert.equal(reset.current_state, 'BET_AVAILABLE');
  assert.equal(panelObservationTransition(available, { ...available, observed_at: at(1) }).duplicate, true);
  assert.equal(panelObservationTransition(active, { ...available, observed_at: at(-10) }).stale, true);
});

test('frame reload resets the previous panel state attribution', () => {
  const before = classifyPanelObservation({ ...base, ...screenshotStates[0] });
  const after = classifyPanelObservation({ ...base, ...screenshotStates[0],
    frame_identity: 'https://aviator-next.spribegaming.com/reloaded',
    observed_at: new Date(Date.now() + 5).toISOString() });
  const transition = panelObservationTransition(before, after);
  assert.equal(transition.frame_reloaded, true);
  assert.equal(transition.from_state, 'UNKNOWN');
});

test('fresh SPRIBE Auto panel readback verifies independent stake, Auto Bet, and Auto Cash Out values', () => {
  const observedAt = new Date().toISOString();
  const raw = {
    panel_id: 'PANEL_1', panel_visible: true, auto_tab_selected: true,
    frame_identity: 'https://aviator-demo.spribegaming.com/#demo-frame', observed_at: observedAt,
    source: 'AVIATOR_BETTING_PANEL_CONFIGURATION_OBSERVER',
    stake: {selector: '.bet-block input[type="text"]', value: '100.00', visible: true, enabled: true},
    auto_bet: {selector: '.auto-bet app-ui-switcher .input-switch',
      class: 'input-switch off', visible: true, input_enabled: null},
    auto_cashout: {selector: '.cash-out-switcher app-ui-switcher .input-switch',
      class: 'input-switch', visible: true, input_enabled: true, input_visible: true, input_value: '2.00'},
    currency_text: 'Bet 100.00 USD Auto bet Auto Cash Out',
  };
  const result = classifyPanelConfigurationObservation(raw);
  assert.deepEqual({status: result.status, auto_bet: result.auto_bet,
    auto_cashout: result.auto_cashout, stake: result.stake, currency: result.currency,
    cashout_target: result.cashout_target, requirements_satisfied: result.requirements_satisfied},
  {status: 'VERIFIED', auto_bet: 'OFF', auto_cashout: 'ON', stake: 100,
    currency: 'USD', cashout_target: 2, requirements_satisfied: true});
  assert.equal(result.expires_at, new Date(Date.parse(observedAt) + 15000).toISOString());
});

test('panel configuration readback fails closed when tab, switch, or freshness evidence is missing', () => {
  const observedAt = new Date().toISOString();
  const raw = {
    panel_id: 'PANEL_2', panel_visible: true, auto_tab_selected: true,
    frame_identity: 'https://aviator-demo.spribegaming.com/#demo-frame', observed_at: observedAt,
    stake: {value: '100.00', visible: true, enabled: true},
    auto_bet: {class: 'input-switch off', visible: true},
    auto_cashout: {class: 'input-switch', visible: true, input_enabled: true,
      input_visible: true, input_value: '2.00'},
    currency_text: 'Bet 100.00 USD',
  };
  const stale = classifyPanelConfigurationObservation(raw,
    {now: Date.parse(observedAt) + 16000});
  assert.equal(stale.status, 'UNKNOWN');
  assert.equal(stale.evidence.reason, 'STALE_OR_FUTURE_OBSERVATION');
  assert.equal(stale.requirements_satisfied, false);
  const tabHidden = classifyPanelConfigurationObservation({...raw, auto_tab_selected: false});
  assert.equal(tabHidden.status, 'UNKNOWN');
  assert.equal(tabHidden.auto_cashout, 'UNKNOWN');
  assert.equal(tabHidden.evidence.reason, 'AUTO_TAB_NOT_SELECTED');
  const unknownSwitch = classifyPanelConfigurationObservation({...raw,
    auto_cashout: {...raw.auto_cashout, class: 'input-switch disabled', input_enabled: false}});
  assert.equal(unknownSwitch.status, 'UNKNOWN');
  assert.equal(unknownSwitch.requirements_satisfied, false);
});

test('UNKNOWN to available is a transition and repeated DOM event is deduplicated', () => {
  const unknown = classifyPanelObservation({ ...base, button_visible: false });
  const available = classifyPanelObservation({ ...base, ...screenshotStates[0],
    observed_at: new Date(Date.now() + 10).toISOString() });
  const recovered = panelObservationTransition(unknown, available);
  assert.equal(recovered.changed, true);
  assert.equal(recovered.from_state, 'UNKNOWN');
  assert.equal(recovered.to_state, 'BET_AVAILABLE');
  assert.equal(panelObservationTransition(available, { ...available,
    observed_at: new Date(Date.now() + 20).toISOString() }).duplicate, true);
});
