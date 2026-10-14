import test from 'node:test';
import assert from 'node:assert/strict';
import { publishCollectorGamePhase } from '../collector/CollectorRoundEventClient.js';

test('publishes read-only phase timing with the owned collector round identity', async () => {
  let request;
  const phase = {
    event_id: 'phase-event-1',
    phase: 'BETTING_OPEN',
    observed_at: '2026-10-09T10:00:00.000Z',
    evidence: {
      source: 'AVIATOR_BETTING_PANEL_READ_ONLY',
      selector: '.bet-block .btn-success.bet',
      button_visible: true, button_enabled: true,
      stake_input_visible: true, bet_tab_active: true,
      button_text: 'Place Bet', button_class: 'btn-success bet',
    },
  };
  const result = await publishCollectorGamePhase(phase, {
    round_id: 'canonical-round-90', round_index: 90,
  }, {
    url: 'http://backend.test/api/collector/game-phase',
    apiKey: 'collector-test-key',
    fetchImpl: async (url, options) => {
      request = { url, options, body: JSON.parse(options.body) };
      return { ok: true, status: 200, text: async () => JSON.stringify({ ok: true, status: 'RECORDED' }) };
    },
  });
  assert.equal(result.status, 'RECORDED');
  assert.equal(request.url, 'http://backend.test/api/collector/game-phase');
  assert.equal(request.options.headers.authorization, 'Bearer collector-test-key');
  assert.equal(request.body.source_round_id, 'canonical-round-90');
  assert.equal(request.body.source_round_index, 90);
  assert.equal(request.body.phase, 'BETTING_OPEN');
  assert.equal(request.body.event_id, 'phase-event-1');
  assert.equal(request.body.evidence.button_enabled, true);
});

test('does not publish phase evidence without a persisted source identity', async () => {
  let called = false;
  const result = await publishCollectorGamePhase({
    phase: 'BETTING_OPEN', observed_at: '2026-10-09T10:00:00.000Z',
  }, null, { fetchImpl: async () => { called = true; } });
  assert.equal(called, false);
  assert.equal(result.status, 'SOURCE_IDENTITY_UNAVAILABLE');
});
