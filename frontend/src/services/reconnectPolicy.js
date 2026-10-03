export const MAX_RECONNECT_BACKOFF_MS = 30000;

export function reconnectDelay(attempt) {
  const safeAttempt = Math.max(1, Number(attempt) || 1);
  return Math.min(1000 * (2 ** (safeAttempt - 1)), MAX_RECONNECT_BACKOFF_MS);
}

export function connectionStateAfterHealth({ online, degraded = false, lastSuccessfulAt = null, attempt = 0 }) {
  if (online) return degraded ? 'DEGRADED' : 'CONNECTED';
  if (Number(attempt) >= 5) return 'DISCONNECTED';
  return lastSuccessfulAt == null ? 'INITIAL_CONNECT' : 'RECONNECTING';
}
