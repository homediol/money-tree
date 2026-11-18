import assert from 'node:assert/strict';
import test from 'node:test';

import {
  isRuleSelectionApplied,
  ruleSelectionRequest,
  sessionSelectionDetail,
  sessionSelectionLabel,
  updateSessionListAfterAction,
  updateStatusAfterSessionAction,
} from '../src/services/rulesSessionControls.js';

test('behaviour strategy summaries show Behaviour Detection without empty rule IDs', () => {
  const session = { mode: 'ALL_RULES_TOGETHER', strategy_id: 'BEHAVIOUR_ROLLING_HIGH', rule_ids: [],
    automatic_rule_selection: false, adaptive_rule_selection: false };
  assert.equal(sessionSelectionLabel(session), 'Behaviour Detection');
  assert.match(sessionSelectionDetail(session), /Rolling HIGH eligibility/);
  assert.doesNotMatch(sessionSelectionDetail(session), /Selected rules: none/);
  assert.equal(isRuleSelectionApplied(session, { mode: 'ALL_RULES_TOGETHER',
    selectionMode: 'behaviour', selectedIds: [], availableRules: [] }), true);
});

test('automatic and manual session requests preserve the user selection mode', () => {
  const rules = [{ rule_id: 'A', enabled: true }, { rule_id: 'B', enabled: false }];
  assert.deepEqual(ruleSelectionRequest('automatic', ['A'], rules),
    { rule_ids: [], automatic_rule_selection: true });
  assert.deepEqual(ruleSelectionRequest('manual', ['A', 'B', 'A'], rules),
    { rule_ids: ['A'], automatic_rule_selection: false });
  assert.deepEqual(ruleSelectionRequest('adaptive', ['A', 'B'], rules),
    { rule_ids: [], automatic_rule_selection: false, adaptive_rule_selection: true });
});

test('apply state reflects selection mode and manual participants', () => {
  const rules = [{ rule_id: 'A', enabled: true }, { rule_id: 'B', enabled: true }];
  const manual = { mode: 'FIVE_RULE_TRACKER', rule_ids: ['A'],
    automatic_rule_selection: false, adaptive_rule_selection: false };
  assert.equal(isRuleSelectionApplied(manual, { mode: manual.mode,
    selectionMode: 'manual', selectedIds: ['A'], availableRules: rules }), true);
  assert.equal(isRuleSelectionApplied(manual, { mode: manual.mode,
    selectionMode: 'manual', selectedIds: ['B'], availableRules: rules }), false);
  assert.equal(isRuleSelectionApplied(manual, { mode: 'ALL_RULES_TOGETHER',
    selectionMode: 'manual', selectedIds: ['A'], availableRules: rules }), false);

  const adaptive = { ...manual, adaptive_rule_selection: true, rule_ids: ['A', 'B'] };
  assert.equal(isRuleSelectionApplied(adaptive, { mode: manual.mode,
    selectionMode: 'adaptive', selectedIds: [], availableRules: rules }), true);
});

const running = {
  session_id: 'session-current', status: 'RUNNING', mode: 'ALL_RULES_TOGETHER',
  started_at: '2026-11-12T18:00:00+00:00', observed_rounds: 20, window_size: 50,
  results: { WIN: 2, LOSS: 1, PENDING: 1 }, selection_count: 4, net_outcome: 1,
};
const unrelated = {
  session_id: 'session-other', status: 'PAUSED', mode: 'START_CHECK',
  started_at: '2026-11-11T18:00:00+00:00', stop_reason: 'MANUAL_PAUSE',
};

test('session control response updates only the session that received the action', () => {
  const paused = { ...running, status: 'PAUSED', stop_reason: 'MANUAL_PAUSE' };
  const rows = updateSessionListAfterAction([running, unrelated], paused);
  assert.equal(rows[0].session_id, running.session_id);
  assert.equal(rows[0].status, 'PAUSED');
  assert.deepEqual(rows[1], unrelated);

  const status = updateStatusAfterSessionAction({
    status: 'RUNNING', rules_configured: 5, active_session: running,
    latest_session: running, reporting_session_id: running.session_id,
    sessions: [running, unrelated],
  }, paused);
  assert.equal(status.active_session.session_id, running.session_id);
  assert.equal(status.latest_session.session_id, running.session_id);
  assert.equal(status.active_session.status, 'PAUSED');
  assert.equal(status.sessions.find((row) => row.session_id === unrelated.session_id).status, 'PAUSED');
});

test('terminal action clears controls but keeps the exact acted-on session visible as latest', () => {
  const stopped = { ...running, status: 'MANUALLY_STOPPED', stop_reason: 'MANUAL_STOP' };
  const status = updateStatusAfterSessionAction({
    status: 'RUNNING', rules_configured: 5, active_session: running,
    latest_session: running, reporting_session_id: running.session_id,
    sessions: [running, unrelated],
  }, stopped);
  assert.equal(status.active_session, null);
  assert.equal(status.latest_session.session_id, running.session_id);
  assert.equal(status.latest_session.status, 'MANUALLY_STOPPED');
  assert.equal(status.reporting_session_status, 'MANUALLY_STOPPED');
  assert.equal(status.status, 'READY');
});
