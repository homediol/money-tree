/**
 * RetryManager.js — Exponential backoff with jitter and configurable limits.
 *
 * Backoff sequence (seconds): 1, 2, 5, 10, 20, 30 (then capped at max).
 */

import { log, formatError } from './Logger.js';

const BACKOFF_STEPS = [1000, 2000, 5000, 10000, 20000, 30000];

export function backoffMs(attempt) {
  const base = BACKOFF_STEPS[Math.min(attempt, BACKOFF_STEPS.length - 1)];
  // ±20% jitter to avoid thundering herd
  const jitter = Math.floor(base * 0.2 * Math.random());
  return Math.min(BACKOFF_STEPS[BACKOFF_STEPS.length - 1], base + jitter);
}

/**
 * Retry a failed frame scan quickly only while recovery is waiting for a real
 * persisted round. Browser, page, and ordinary collection failures retain the
 * normal bounded exponential backoff.
 */
export function recoveryRetryDelayMs(err, attempt, { recoveryPending = false, frameDelayMs = process.env.BOT_FRAME_RECOVERY_RETRY_MS } = {}) {
  const message = formatError(err).toLowerCase();
  const isFrameWaitFailure = /cdp websocket (?:closed|is not open)|frame (?:was )?detached|frame detached while waiting for payouts|timed out waiting for aviator iframe with payouts/.test(message);
  if (recoveryPending && isFrameWaitFailure) {
    const configured = Number(frameDelayMs);
    return Number.isFinite(configured) && configured > 0 ? configured : 1000;
  }
  return backoffMs(attempt);
}

/**
 * sleep that respects an AbortSignal.
 */
export function sleep(ms, signal) {
  if (signal?.aborted) return Promise.resolve();
  return new Promise(resolve => {
    const t = setTimeout(resolve, ms);
    signal?.addEventListener('abort', () => { clearTimeout(t); resolve(); }, { once: true });
  });
}

/**
 * Retry `fn` up to `maxAttempts` times with exponential backoff.
 * `fn` receives the current attempt number (0-based).
 * Throws the last error if all attempts fail.
 */
export async function withRetry(fn, { maxAttempts = 6, label = 'op', signal } = {}) {
  let lastErr;
  for (let attempt = 0; attempt < maxAttempts; attempt++) {
    if (signal?.aborted) throw new Error('Aborted');
    try {
      return await fn(attempt);
    } catch (err) {
      lastErr = err;
      if (signal?.aborted) throw err;
      if (['AUTH_REQUIRED', 'NETWORK_UNAVAILABLE', 'SITE_UNAVAILABLE',
        'GAME_PROVIDER_UNAVAILABLE', 'PERSISTENCE_ERROR'].includes(err?.code)
        || err?.name === 'AuthRequiredError') throw err;
      const delay = backoffMs(attempt);
      log.warn(`${label} failed (attempt ${attempt + 1}/${maxAttempts}): ${formatError(err)} — retrying in ${delay}ms`);
      await sleep(delay, signal);
    }
  }
  throw lastErr;
}

/**
 * Classify an error as browser-fatal, page-recoverable, or frame-recoverable.
 */
export function classifyError(err) {
  const msg = formatError(err).toLowerCase();
  const code = String(err?.code || '').toUpperCase();

  if (['OUTBOX_CAPACITY', 'PERSISTENCE_ERROR', 'STORAGE_UNAVAILABLE'].includes(code)
      || /duplicate key value violates unique constraint|postgres|database|sqlstate|round_index_key/.test(msg)) {
    return 'PERSISTENCE_ERROR';
  }

  if (code === 'AUTH_REQUIRED' || err?.name === 'AuthRequiredError' || /AUTH_REQUIRED/.test(msg)) {
    return 'AUTH_REQUIRED';
  }
  if (['NETWORK_UNAVAILABLE', 'DNS_UNAVAILABLE', 'OFFLINE', 'EAI_AGAIN', 'ENOTFOUND', 'EAI_FAIL',
    'ERR_NAME_NOT_RESOLVED', 'ERR_INTERNET_DISCONNECTED'].includes(code)
      || /EAI_AGAIN|ENOTFOUND|EAI_FAIL|ERR_NAME_NOT_RESOLVED|ERR_INTERNET_DISCONNECTED/i.test(msg)) return 'NETWORK_RECOVER';
  if (['SITE_UNAVAILABLE', 'GAME_PROVIDER_UNAVAILABLE'].includes(code)) return code;
  if (code === 'PAGE_CRASHED' || /page crashed|renderer process gone/.test(msg)) return 'PAGE_CRASH';
  if (code === 'BROWSER_DISCONNECTED') return 'BROWSER_FATAL';

  // A websocket to the nested game-frame CDP target can close while the
  // browser and parent page remain connected. Recover the frame first; only
  // browser-level disconnect evidence should trigger browser-fatal handling.
  if (/cdp websocket (?:closed|is not open)/.test(msg)) return 'FRAME_RECOVER';

  const browserFatal = [
    'browser has been closed',
    'browser disconnected',
    'chromium crashed',
    'target closed',
    'browser process closed',
    'chromium crashed',
    'browser disconnected',
    'browser has been closed',
    'websocket is not open',
  ];
  if (browserFatal.some(f => msg.includes(f))) return 'BROWSER_FATAL';

  const pageLevel = [
    'page closed',
    'page crashed',
    'navigation',
    'net::err',
    'timeout',
    'page navigated away',
  ];
  if (pageLevel.some(f => msg.includes(f))) return 'PAGE_RECOVER';

  const frameLevel = [
    'execution context was destroyed',
    'frame was detached',
    'frame detached',
    'context closed',
    'has been closed',
    'collector_not_installed',
    'stale',
  ];
  if (frameLevel.some(f => msg.includes(f))) return 'FRAME_RECOVER';

  if (['GAME_FRAME_MISSING', 'PAYOUTS_UNAVAILABLE', 'FRAME_DETACHED'].includes(code)
      || /execution context was destroyed|frame (?:was )?detached|collector_not_installed|timed out waiting for aviator iframe/.test(msg)) {
    return 'FRAME_RECOVER';
  }

  return 'UNKNOWN';
}
