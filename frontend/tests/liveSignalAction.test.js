import test from 'node:test';
import assert from 'node:assert/strict';
import {
  fetchLatestAction,
  LIVE_SIGNAL_ACTION_ENDPOINT,
  parseLatestActionPayload,
} from '../src/services/liveSignalAction.js';

test('action monitor reads only action and actual target index', () => {
  assert.deepEqual(parseLatestActionPayload({
    action: 'OK', index: '#20301', signal_id: 'internal-id',
  }), { action: 'OK', index: '#20301' });
  assert.deepEqual(parseLatestActionPayload({ action: 'WAIT', index: '#20302' }), {
    action: 'WAIT', index: '#20302',
  });
});

test('no canonical signal is shown as no signal, not as fabricated WAIT', () => {
  assert.equal(parseLatestActionPayload({ ok: true, signal: null }), null);
});

test('WAIT with a null or malformed index is reported as an API error', () => {
  assert.throws(() => parseLatestActionPayload({ action: 'WAIT', index: null }),
    /target round index is unavailable/);
  assert.throws(() => parseLatestActionPayload({ action: 'OK', index: '#0' }),
    /target round index is unavailable/);
});

test('action monitor calls the configured canonical endpoint without leaking metadata', async () => {
  let requestedUrl;
  const result = await fetchLatestAction({
    baseUrl: 'https://api.example.test',
    fetchImpl: async (url) => {
      requestedUrl = url;
      return { ok: true, async json() { return { action: 'OK', index: '#20301' }; } };
    },
  });

  assert.equal(requestedUrl, `https://api.example.test${LIVE_SIGNAL_ACTION_ENDPOINT}`);
  assert.deepEqual(result, { action: 'OK', index: '#20301' });
});
