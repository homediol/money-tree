/** Check the PC's route to Winner without opening or changing a browser tab. */
import dns from 'node:dns/promises';
import net from 'node:net';
import { log } from './Logger.js';
import { backoffMs, sleep } from './RetryManager.js';

const TIMEOUT_MS = 5000;

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

export async function probeNetwork({ lookup = dns.lookup, dial = connect } = {}) {
  const checked_at = new Date().toISOString();
  let address;
  try {
    ({ address } = await deadline(lookup('winner.rw', { family: 4 }), 'Winner DNS'));
  } catch (error) {
    const publicIpReachable = await dial('1.1.1.1').then(() => true, () => false);
    return { status: publicIpReachable ? 'DNS_UNAVAILABLE' : 'OFFLINE',
      reason: `Winner DNS lookup failed: ${error.message}`, checked_at };
  }
  try {
    await dial(address);
    return { status: 'ONLINE', reason: null, checked_at };
  } catch (error) {
    const publicIpReachable = await dial('1.1.1.1').then(() => true, () => false);
    return { status: publicIpReachable ? 'WINNER_UNREACHABLE' : 'OFFLINE',
      reason: `Winner TCP connection failed: ${error.message}`, checked_at };
  }
}

export async function waitForNetwork(signal, onUpdate, { probe = probeNetwork, pause = sleep } = {}) {
  let attempt = 0;
  let previousStatus = null;
  while (!signal?.aborted) {
    const result = await probe();
    onUpdate(result);
    if (result.status === 'ONLINE') {
      if (previousStatus && previousStatus !== 'ONLINE') log.info('Network path to Winner restored');
      return true;
    }
    if (result.status !== previousStatus || attempt % 5 === 0) {
      log.warn(`Network ${result.status}: ${result.reason}; retrying while collector stays running`);
    }
    previousStatus = result.status;
    await pause(backoffMs(attempt++), signal);
  }
  return false;
}
