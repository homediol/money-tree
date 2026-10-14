/** Check the PC's route to Winner without opening or changing a browser tab. */
import dns from 'node:dns/promises';
import net from 'node:net';
import http from 'node:http';
import { log } from './Logger.js';
import { backoffMs, sleep } from './RetryManager.js';

const TIMEOUT_MS = 5000;
const WINNER_HOST = 'winner.rw';
const GAME_PROVIDER_HOSTS = (process.env.BOT_GAME_PROVIDER_HOSTS || 'aviator-next.spribegaming.com,aviaport.spribegaming.com')
  .split(',').map(host => host.trim()).filter(Boolean);

function deadline(promise, label) {
  let timer;
  return Promise.race([
    promise,
    new Promise((_, reject) => {
      timer = setTimeout(() => reject(new Error(`${label} timed out`)), TIMEOUT_MS);
    }),
  ]).finally(() => clearTimeout(timer));
}

function connect(address) {
  return new Promise((resolve, reject) => {
    const socket = net.createConnection({ host: address, port: 443 });
    socket.setTimeout(TIMEOUT_MS);
    socket.once('connect', () => { socket.destroy(); resolve(); });
    socket.once('error', error => { socket.destroy(); reject(error); });
    socket.once('timeout', () => { socket.destroy(); reject(new Error('TCP connection timed out')); });
  });
}

export async function probeNetwork({ lookup = dns.lookup, dial = connect,
  providerHosts = GAME_PROVIDER_HOSTS } = {}) {
  const checked_at = new Date().toISOString();
  const evidence = [];
  let address;
  try {
    ({ address } = await deadline(lookup(WINNER_HOST, { family: 4 }), 'Winner DNS'));
  } catch (error) {
    const publicIpReachable = await dial('1.1.1.1').then(() => true, () => false);
    return { status: publicIpReachable ? 'DNS_UNAVAILABLE' : 'OFFLINE', reason_code: publicIpReachable ? 'WINNER_DNS_FAILED' : 'NO_NETWORK_ROUTE',
      reason: `Winner DNS lookup failed: ${error.message}`, evidence: [{ host: WINNER_HOST, result: 'DNS_FAILED', code: error.code || null },
        { host: '1.1.1.1', result: publicIpReachable ? 'TCP_REACHABLE' : 'TCP_UNREACHABLE' }], checked_at };
  }
  try {
    await dial(address);
    evidence.push({ host: WINNER_HOST, address, result: 'TCP_REACHABLE' });
  } catch (error) {
    const publicIpReachable = await dial('1.1.1.1').then(() => true, () => false);
    return { status: publicIpReachable ? 'MAIN_SITE_UNAVAILABLE' : 'OFFLINE', reason_code: publicIpReachable ? 'WINNER_TCP_FAILED' : 'NO_NETWORK_ROUTE',
      reason: `Winner TCP connection failed: ${error.message}`, evidence: [{ host: WINNER_HOST, address, result: 'TCP_FAILED', code: error.code || null },
        { host: '1.1.1.1', result: publicIpReachable ? 'TCP_REACHABLE' : 'TCP_UNREACHABLE' }], checked_at };
  }
  const providerResults = await Promise.all(providerHosts.map(async host => {
    try {
      const resolved = await deadline(lookup(host, { family: 4 }), `${host} DNS`);
      await dial(resolved.address);
      return { host, address: resolved.address, result: 'TCP_REACHABLE' };
    } catch (error) {
      return { host, result: 'UNAVAILABLE', code: error.code || null, reason: error.message };
    }
  }));
  evidence.push(...providerResults);
  const reachableProvider = providerResults.find(result => result.result === 'TCP_REACHABLE');
  if (providerResults.length > 0 && !reachableProvider) {
    const unavailableProvider = providerResults[0];
    return { status: 'GAME_PROVIDER_UNAVAILABLE', reason_code: 'GAME_PROVIDER_PROBE_FAILED',
      reason: `No configured Aviator provider endpoint is reachable; ${unavailableProvider.host} probe failed: ${unavailableProvider.reason || unavailableProvider.code || 'unavailable'}`,
    evidence, checked_at };
  }
  return { status: 'ONLINE', reason_code: null, reason: null, evidence, checked_at };
}

export async function waitForNetwork(signal, onUpdate, { probe = probeNetwork, pause = sleep } = {}) {
  let attempt = 0;
  let previousStatus = null;
  while (!signal?.aborted) {
    const result = await probe();
    const retryDelay = result.status === 'ONLINE' ? null : backoffMs(attempt);
    const retryStatus = {
      ...result,
      attempt: attempt + 1,
      next_retry_at: retryDelay === null ? null : new Date(Date.now() + retryDelay).toISOString(),
    };
    onUpdate(retryStatus);
    if (result.status === 'ONLINE') {
      if (previousStatus && previousStatus !== 'ONLINE') log.info('Network path to Winner restored');
      return true;
    }
    if (result.status !== previousStatus || attempt % 5 === 0) {
      log.warn(`Network ${result.status}: ${result.reason}; retry ${attempt + 1} scheduled at ${retryStatus.next_retry_at}`);
    }
    previousStatus = result.status;
    await pause(retryDelay, signal);
    attempt += 1;
  }
  return false;
}

export function probeBackendHealth({ url = process.env.COLLECTOR_BACKEND_HEALTH_URL || 'http://127.0.0.1:8000/health', timeoutMs = 3000 } = {}) {
  return new Promise(resolve => {
    let target;
    try { target = new URL(url); }
    catch (error) {
      resolve({ status: 'UNKNOWN', reason: `Invalid backend health URL: ${error.message}`, checked_at: new Date().toISOString() });
      return;
    }
    const request = http.get(target, { timeout: timeoutMs }, response => {
      response.resume();
      const ok = response.statusCode >= 200 && response.statusCode < 300;
      resolve({ status: ok ? 'AVAILABLE' : 'UNAVAILABLE', http_status: response.statusCode,
        reason: ok ? null : `Backend health returned HTTP ${response.statusCode}`, checked_at: new Date().toISOString() });
    });
    request.on('timeout', () => {
      request.destroy();
      resolve({ status: 'UNAVAILABLE', reason: `Backend health timed out after ${timeoutMs}ms`, checked_at: new Date().toISOString() });
    });
    request.on('error', error => resolve({ status: 'UNAVAILABLE', reason: error.message, checked_at: new Date().toISOString() }));
  });
}
