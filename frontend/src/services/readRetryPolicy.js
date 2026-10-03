export const MAX_SAFE_READ_RETRIES = 2;

const RETRYABLE_HTTP_STATUS = new Set([408, 425, 429, 500, 502, 503, 504]);
const RETRYABLE_ERROR_CODES = new Set(['ECONNABORTED', 'ETIMEDOUT', 'ERR_NETWORK']);

/** Retry only idempotent reads; never replay POST/PUT/PATCH/DELETE requests. */
export function shouldRetryReadFailure(config, error) {
  // Axios can reject before an HTTP request exists (for example while a
  // request interceptor is checking connection state). There is nothing safe
  // to retry without a request config.
  if (!config || typeof config !== 'object') return false;
  if (String(config?.method || 'get').toLowerCase() !== 'get') return false;
  if ((Number(config?._safeReadRetryCount) || 0) >= MAX_SAFE_READ_RETRIES) return false;
  if (error?.isBackendUnavailable || error?.code === 'ERR_BACKEND_RECONNECTING'
      || error?.code === 'ERR_CANCELED' || error?.name === 'AbortError') return false;
  if (error?.response) return RETRYABLE_HTTP_STATUS.has(error.response.status);
  return RETRYABLE_ERROR_CODES.has(error?.code) || !error?.code;
}

export function safeReadRetryDelay(attempt) {
  const safeAttempt = Math.max(1, Number(attempt) || 1);
  return Math.min(1000 * (2 ** (safeAttempt - 1)), 5000);
}
