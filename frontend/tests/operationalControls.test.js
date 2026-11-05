import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';

const source = async (path) => readFile(new URL(path, import.meta.url), 'utf8');

test('betting UI preserves fixed stake, fixed cashout, and independent stop controls', async () => {
  const [rules, live, betting] = await Promise.all([
    source('../src/pages/RulesEngine.jsx'),
    source('../src/pages/LiveActivation.jsx'),
    source('../src/pages/Betting.jsx'),
  ]);
  assert.match(rules, /Fixed simulated stake \(RWF\)/);
  assert.match(rules, /htmlFor="rules-manager-win-limit"/);
  assert.match(rules, /checked=\{winEnabled\}/);
  assert.match(rules, /htmlFor="rules-manager-loss-limit"/);
  assert.match(rules, /checked=\{lossEnabled\}/);
  assert.match(rules, /loss_limit_enabled: lossEnabled/);
  assert.match(rules, /Settled WINs − LOSSes/);
  assert.match(rules, /Shadow profit \/ loss/);
  assert.match(rules, /displaySession\.net_profit_rwf/);
  assert.match(rules, /status\?\.latest_session/);
  assert.match(rules, /stop_reason_source/);
  assert.match(rules, /stop_reason_at/);
  assert.match(rules, /2\.00x model · pending excluded/);
  assert.match(rules, /Participate/);
  assert.match(rules, /Participation is controlled only by its separate checkbox/);
  assert.match(rules, /setSelectedIdsByMode/);
  assert.match(rules, /ids\.filter\(\(id\) => id !== rule\.rule_id\)/);
  assert.doesNotMatch(rules, /enabled \? \[\.\.\.new Set\(\[\.\.\.previous, rule\.rule_id\]\)\]/);
  assert.match(rules, /const participatingRuleIds = selectedIds\.filter/);
  assert.match(rules, /rule_ids: participatingRuleIds/);
  assert.match(live, /Cashout target: 2\.00x/);
  assert.match(live, /Emergency stop/);
  assert.match(betting, /run\('stop'\)/);
  assert.match(betting, /Emergency stop/);
  for (const text of [rules, live, betting]) {
    assert.doesNotMatch(text, /RISK_WAITING|RISK_REJECTED|RISK_APPROVED|Risk profile/i);
  }
});

test('Rules Engine session results expose target-bound settlement evidence and consume round notifications', async () => {
  const rules = await source('../src/pages/RulesEngine.jsx');
  for (const field of [
    'decision_id', 'source_round_id', 'target_round_id', 'matched_rule_ids',
    'stake_rwf', 'payout_multiplier', 'settlement_status', 'settlement_evidence',
    'last_received_round', 'last_settled_target', 'event_processing_latency_ms',
    'settlement_processing_latency_ms', 'target_settlement_durable_ms',
  ]) assert.ok(rules.includes(field), `missing SHADOW result field ${field}`);
  assert.match(rules, /rules-engine:evaluated/);
  assert.match(rules, /AWAITING_VERIFIED_TARGET/);
  assert.match(rules, /p50\/p95\/p99/);
});

test('saved SHADOW sessions include paused and settling rows, allow authorized deletion, and show details in an overlay', async () => {
  const rules = await source('../src/pages/RulesEngine.jsx');
  assert.match(rules, /session\?\.session_id && session\.status !== 'RUNNING'/);
  assert.match(rules, /DELETABLE_SESSION_STATUSES = new Set\(\[[^\]]*'SETTLING'/);
  assert.match(rules, /session\?\.auto_next_armed !== true/);
  assert.match(rules, /Settling sessions will stop observing immediately/);
  assert.match(rules, /role="dialog" aria-modal="true"/);
  assert.match(rules, /fixed inset-0 z-\[100\]/);
  assert.match(rules, /document\.body\.style\.overflow = 'hidden'/);
  assert.match(rules, /event\.key === 'Escape'/);
});
