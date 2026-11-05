import { getApiBaseUrl } from './endpoints.js';

export const LIVE_SIGNAL_ACTION_ENDPOINT = '/api/v1/signals/latest/action';
const ROUND_INDEX_PATTERN = /^#[1-9]\d*$/;

export function parseLatestActionPayload(payload) {
  if (payload && typeof payload === 'object'
      && payload.ok === true && payload.signal === null) return null;

  if (!payload || typeof payload !== 'object'
      || (payload.action !== 'OK' && payload.action !== 'WAIT')) {
    throw new Error('Invalid action response: expected OK or WAIT.');
  }
  if (typeof payload.index !== 'string' || !ROUND_INDEX_PATTERN.test(payload.index)) {
    throw new Error('Invalid action response: target round index is unavailable or malformed.');
  }

  // The monitor consumes only the public two-field projection.
  return { action: payload.action, index: payload.index };
}

export async function fetchLatestAction({
  fetchImpl = fetch,
  baseUrl = getApiBaseUrl(),
  timeoutMs = 15000,
} = {}) {
  const response = await fetchImpl(`${baseUrl}${LIVE_SIGNAL_ACTION_ENDPOINT}`, {
    method: 'GET',
    cache: 'no-store',
    credentials: 'omit',
    headers: { Accept: 'application/json' },
    signal: AbortSignal.timeout(timeoutMs),
  });
  const payload = await response.json().catch(() => null);
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  return parseLatestActionPayload(payload);
}
