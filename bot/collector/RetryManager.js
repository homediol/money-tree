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
  return base + jitter;
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

  const browserFatal = [
    'browser has been closed',
    'browser disconnected',
    'chromium crashed',
    'target closed',
    'connection refused',
    'websocket',
    'econnrefused',
    'epipe',
  ];
  if (browserFatal.some(f => msg.includes(f))) return 'BROWSER_FATAL';

  const pageLevel = [
    'page closed',
    'page crashed',
    'navigation',
    'net::err',
    'timeout',
    'session expired',
    'logged out',
    'maintenance',
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

  return 'FRAME_RECOVER'; // default: try frame recovery first
}
