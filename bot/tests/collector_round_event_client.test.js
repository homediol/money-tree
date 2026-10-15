import test from 'node:test';
import assert from 'node:assert/strict';
import { drainDirectRoundEventOutbox, publishDirectRoundObservations } from '../collector/CollectorRoundEventClient.js';

const directRound = {
  round_id: 'aviator-direct-001', multiplier: 2.35,
  timestamp: '2026-10-06T17:00:00.000Z', observed_at: '2026-10-06T17:00:00.000Z',
  platform_round_id: 'winner-round-001', platform_round_index: 41,
  platform_timestamp: '2026-10-06T16:59:59.000Z', round_index: 907,
  continuity_verified: true, gap_before: false,
};

test('publishes real direct-page identity and timestamp before persistence handoff', async () => {
  let request;
  const result = await publishDirectRoundObservations([directRound], {
    url: 'http://backend.test/api/collector/round-observed', apiKey: 'test-api-key',
    fetchImpl: async (url, options) => {
      request = { url, options, body: JSON.parse(options.body) };
      return { ok: true, status: 200, text: async () => JSON.stringify({ ok: true, accepted: 1 }) };
    },
  });

  assert.equal(result.accepted, 1);
  assert.equal(request.url, 'http://backend.test/api/collector/round-observed');
  assert.equal(request.options.headers.authorization, 'Bearer test-api-key');
  assert.equal(request.body.source, 'WINNER_DIRECT_PAGE_HISTORY');
  assert.deepEqual({
    round_id: request.body.rounds[0].round_id,
    observation_id: request.body.rounds[0].observation_id,
    event_id: request.body.rounds[0].event_id,
    multiplier: request.body.rounds[0].multiplier,
    timestamp: request.body.rounds[0].timestamp,
    observed_at: request.body.rounds[0].observed_at,
    platform_round_id: request.body.rounds[0].platform_round_id,
    platform_round_index: request.body.rounds[0].platform_round_index,
    platform_timestamp: request.body.rounds[0].platform_timestamp,
    round_index: request.body.rounds[0].round_index,
    round_index_source: request.body.rounds[0].round_index_source,
    continuity_verified: request.body.rounds[0].continuity_verified,
    gap_before: request.body.rounds[0].gap_before,
    delivery_mode: request.body.rounds[0].delivery_mode,
  }, {
    round_id: directRound.round_id,
    observation_id: directRound.round_id,
    event_id: `aviator-round:${directRound.round_id}`,
    multiplier: directRound.multiplier,
    timestamp: directRound.timestamp,
    observed_at: directRound.observed_at,
    platform_round_id: directRound.platform_round_id,
    platform_round_index: directRound.platform_round_index,
    platform_timestamp: directRound.platform_timestamp,
    round_index: directRound.round_index,
    round_index_source: 'UNKNOWN',
    continuity_verified: true,
    gap_before: false,
    delivery_mode: 'LIVE',
  });
});

test('omits stale attached panel evidence so it cannot block durable round delivery', async () => {
  let request;
  const staleRound = {
    ...directRound,
    game_phase: {
      phase: 'UNKNOWN', observed_at: '2020-01-01T00:00:00.000Z',
      evidence: { source: 'AVIATOR_EXPLICIT_BETTING_PHASE_DOM', explicit_window_state: 'UNKNOWN' },
      panel_observations: [
        { panel_id: 'PANEL_1', observed_at: '2020-01-01T00:00:00.000Z' },
        { panel_id: 'PANEL_2', observed_at: '2020-01-01T00:00:00.000Z' },
      ],
    },
  };
  await publishDirectRoundObservations([staleRound], {
    fetchImpl: async (_url, options) => {
      request = JSON.parse(options.body);
      return { ok: true, status: 200, text: async () => '{"ok":true,"accepted":1}' };
    },
  });
  assert.equal('game_phase' in request.rounds[0], false);
});

test('retains fresh attached panel evidence for a timely round handoff', async () => {
  let request;
  const observedAt = new Date().toISOString();
  const freshRound = {
    ...directRound,
    game_phase: {
      phase: 'UNKNOWN', observed_at: observedAt,
      evidence: { source: 'AVIATOR_EXPLICIT_BETTING_PHASE_DOM', explicit_window_state: 'UNKNOWN' },
      panel_observations: [
        { panel_id: 'PANEL_1', observed_at: observedAt },
        { panel_id: 'PANEL_2', observed_at: observedAt },
      ],
    },
  };
  await publishDirectRoundObservations([freshRound], {
    fetchImpl: async (_url, options) => {
      request = JSON.parse(options.body);
      return { ok: true, status: 200, text: async () => '{"ok":true,"accepted":1}' };
    },
  });
  assert.deepEqual(request.rounds[0].game_phase, freshRound.game_phase);
});

test('reports backend rejection so collector can log the fallback path', async () => {
  await assert.rejects(
    publishDirectRoundObservations([directRound], {
      fetchImpl: async () => ({ ok: false, status: 503,
        text: async () => JSON.stringify({ ok: false, error: 'database_unavailable' }) }),
    }),
    error => error.category === 'BACKEND_REJECTED_EVENT' && error.httpStatus === 503
      && error.backendError === 'database_unavailable',
  );
});

test('aborts an unresponsive backend after the configured timeout', async () => {
  await assert.rejects(
    publishDirectRoundObservations([directRound], {
      timeoutMs: 100,
      fetchImpl: async (_url, { signal }) => new Promise((resolve, reject) => {
        signal.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')), { once: true });
      }),
    }),
    error => error.category === 'BACKEND_EVENT_TIMEOUT'
      && error.message.includes('timed out after 100ms'),
  );
});

test('observed-stage acknowledgement does not complete the durable round outbox', async () => {
  const outbox = {
    rows: [{ ...directRound }],
    readAll() { return this.rows; },
    removePersisted(ids) { this.rows = this.rows.filter(row => !ids.includes(String(row.round_id))); },
  };
  await assert.rejects(drainDirectRoundEventOutbox(outbox, {
    fetchImpl: async () => ({ ok: false, status: 503, text: async () => '{"ok":false}' }),
  }));
  assert.equal(outbox.rows.length, 1);
  const receipt = await drainDirectRoundEventOutbox(outbox, {
    fetchImpl: async () => ({ ok: true, status: 200, text: async () => '{"ok":true,"accepted":1}' }),
  });
  assert.equal(receipt.accepted, 1);
  assert.equal(outbox.rows.length, 1);
});
