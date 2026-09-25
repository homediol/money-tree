import { useSyncExternalStore } from 'react';
import { getHealthUrl } from './endpoints.js';
import { reconnectDelay } from './reconnectPolicy.js';

export const BACKEND_RESTORED_EVENT = 'winner:backend-restored';

const ONLINE_CHECK_MS = 10000;
const REQUEST_TIMEOUT_MS = 4000;

let snapshot = {
  status: 'checking',
  online: false,
  degraded: false,
  attempt: 0,
  nextRetryMs: 0,
  message: null,
  health: null,
};
let timer = null;
let scheduledAt = 0;
let inFlight = null;
let subscribers = 0;
const listeners = new Set();

function log(level, message, extra = '') {
  const method = console[level] || console.info;
  method(`[backend ${new Date().toISOString()}] ${message}${extra ? ` ${extra}` : ''}`);
}

function publish(next) {
  snapshot = { ...snapshot, ...next };
  for (const listener of listeners) listener();
}

function schedule(delay) {
  if (subscribers === 0) return;
  const due = Date.now() + delay;
  if (timer && scheduledAt <= due) return;
  if (timer) window.clearTimeout(timer);
  scheduledAt = due;
  timer = window.setTimeout(() => {
    timer = null;
    scheduledAt = 0;
    checkBackend();
  }, delay);
}

async function readHealth() {
  const controller = new AbortController();
  const timeout = window.setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
  try {
    const response = await fetch(getHealthUrl(), {
      method: 'GET',
      cache: 'no-store',
      credentials: 'same-origin',
      signal: controller.signal,
      headers: { Accept: 'application/json' },
    });
    const data = await response.json().catch(() => null);
    if (data?.service !== 'winner-predict-backend'
        || data?.backend?.status !== 'ok') {
      throw new Error('Port 8000 did not return the Winner Predict backend health response');
    }
    return data;
  } finally {
    window.clearTimeout(timeout);
  }
}

export function checkBackend() {
  if (inFlight) return inFlight;
  inFlight = (async () => {
    const wasUnavailable = snapshot.status === 'reconnecting';
    try {
      const health = await readHealth();
      const degraded = health.ok !== true;
      publish({
        status: degraded ? 'degraded' : 'online',
        online: true,
        degraded,
        attempt: 0,
        nextRetryMs: ONLINE_CHECK_MS,
        message: degraded ? 'Backend connected; database health is degraded' : null,
        health,
      });
      if (wasUnavailable) {
        log('info', 'connection restored', `instance=${health.backend?.instance_id || 'unknown'}`);
        window.dispatchEvent(new CustomEvent(BACKEND_RESTORED_EVENT, { detail: health }));
      }
      schedule(ONLINE_CHECK_MS);
      return health;
    } catch (error) {
      const attempt = snapshot.status === 'reconnecting' ? snapshot.attempt + 1 : 1;
      const delay = reconnectDelay(attempt);
      const message = error?.name === 'AbortError'
        ? 'Backend health check timed out'
        : (error?.message || 'Backend connection failed');
      if (snapshot.status !== 'reconnecting' || attempt === 1) {
        log('warn', 'connection unavailable; entering recovery loop', message);
      }
      publish({
        status: 'reconnecting',
        online: false,
        degraded: false,
        attempt,
        nextRetryMs: delay,
        message,
      });
      schedule(delay);
      return null;
    } finally {
      inFlight = null;
    }
  })();
  return inFlight;
}

export function reportBackendRequestFailure(error) {
  if (error?.code === 'ERR_BACKEND_RECONNECTING') return;
  const status = error?.response?.status;
  const connectionFailure = !error?.response
    || error?.code === 'ERR_NETWORK'
    || error?.code === 'ECONNABORTED'
    || [502, 503, 504].includes(status);
  if (connectionFailure) schedule(0);
}

export function backendRequestsAllowed() {
  return snapshot.status === 'online' || snapshot.status === 'degraded';
}

export function getBackendSnapshot() {
  return snapshot;
}

function subscribe(listener) {
  listeners.add(listener);
  subscribers += 1;
  if (subscribers === 1) {
    window.addEventListener('online', checkBackend);
    checkBackend();
  }
  return () => {
    listeners.delete(listener);
    subscribers = Math.max(0, subscribers - 1);
    if (subscribers === 0) {
      window.removeEventListener('online', checkBackend);
      if (timer) {
        window.clearTimeout(timer);
        timer = null;
        scheduledAt = 0;
      }
    }
  };
}

export function useBackendConnection() {
  return useSyncExternalStore(subscribe, getBackendSnapshot, getBackendSnapshot);
}
