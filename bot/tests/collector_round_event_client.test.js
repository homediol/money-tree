import test from 'node:test';
import assert from 'node:assert/strict';
import { drainDirectRoundEventOutbox, probeRoundHistoryVisibility,
  publishDirectRoundObservations } from '../collector/CollectorRoundEventClient.js';

const directRound = {
  round_id: 'aviator-direct-001', multiplier: 2.35,
  timestamp: '2026-10-06T17:00:00.000Z', observed_at: '2026-10-06T17:00:00.000Z',
  platform_round_id: 'winner-round-001', platform_round_index: 41,
  platform_timestamp: '2026-10-06T16:59:59.000Z', round_index: 907,
  continuity_verified: true, gap_before: false,
};

test('publishes real direct-page identity and timestamp before persistence handoff', async () => {
  let request;
  const collectorTiming = [{ round_id: directRound.round_id,
    page_mutation_to_extraction_ms: 1.25, page_extraction_ms: 0.75,
    node_receive_to_validation_ms: 0.5, validation_to_api_dispatch_ms: 2.0,
    page_to_node_receive_ms: null,
    page_to_node_receive_unavailable_reason: 'SEPARATE_MONOTONIC_CLOCK_DOMAINS' }];
  const result = await publishDirectRoundObservations([directRound], {
    url: 'http://backend.test/api/collector/round-observed', apiKey: 'test-api-key',
    collectorTiming,
    fetchImpl: async (url, options) => {
      request = { url, options, body: JSON.parse(options.body) };
      return { ok: true, status: 200, text: async () => JSON.stringify({ ok: true, accepted: 1 }) };
    },
  });

  assert.equal(result.accepted, 1);
  assert.equal(request.url, 'http://backend.test/api/collector/round-observed');
  assert.equal(request.options.headers.authorization, 'Bearer test-api-key');
  assert.equal(request.body.source, 'WINNER_DIRECT_PAGE_HISTORY');
  assert.deepEqual(request.body.collector_timing, collectorTiming);
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

test('Round History visibility probe confirms the committed identity through the existing API', async () => {
  let requestedUrl;
  const result = await probeRoundHistoryVisibility('aviator-direct-001', {
    url: 'http://backend.test/api/history?limit=100',
    fetchImpl: async (url, options) => {
      requestedUrl = url;
      assert.equal(options.method, 'GET');
      return { ok: true, status: 200,
        text: async () => JSON.stringify({ rounds: [{ round_id: 'aviator-direct-001' }] }) };
    },
  });

  assert.equal(requestedUrl, 'http://backend.test/api/history?limit=100');
  assert.equal(result.available, true);
  assert.equal(result.status, 'CONFIRMED');
  assert.ok(result.checked_at);
});

test('Round History visibility probe preserves UNKNOWN when the round is absent', async () => {
  const result = await probeRoundHistoryVisibility('missing-round', {
    fetchImpl: async () => ({ ok: true, status: 200, text: async () => '{"rounds":[]}' }),
  });
  assert.equal(result.available, false);
  assert.equal(result.status, 'ROUND_NOT_IN_RESPONSE');
});
