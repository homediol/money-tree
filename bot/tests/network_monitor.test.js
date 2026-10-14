import assert from 'node:assert/strict';
import test from 'node:test';

import { probeNetwork, waitForNetwork } from '../collector/NetworkMonitor.js';
import { classifyError } from '../collector/RetryManager.js';

test('network wait resumes collection only after Winner connectivity returns', async () => {
  const updates = [];
  const statuses = [
    { status: 'OFFLINE', reason: 'test outage' },
    { status: 'ONLINE', reason: null },
  ];
  const result = await waitForNetwork(null, state => updates.push(state), {
    probe: async () => statuses.shift(),
    pause: async () => {},
  });

  assert.equal(result, true);
  assert.deepEqual(updates.map(state => state.status), ['OFFLINE', 'ONLINE']);
});

test('EAI_AGAIN is reported as DNS outage and resumes after connectivity returns', async () => {
  const updates = [];
  let winnerLookups = 0;
  const lookup = async host => {
    if (host === 'winner.rw' && winnerLookups++ === 0) {
      throw Object.assign(new Error('getaddrinfo EAI_AGAIN winner.rw'), { code: 'EAI_AGAIN' });
    }
    return { address: host === 'winner.rw' ? '198.51.100.10' : '198.51.100.20' };
  };
  const result = await waitForNetwork(null, status => updates.push(status), {
    probe: () => probeNetwork({ lookup, dial: async address => {
      assert.ok(['1.1.1.1', '198.51.100.10', '198.51.100.20'].includes(address));
    }, providerHosts: ['provider.test'] }),
    pause: async () => {},
  });

  assert.equal(result, true);
  assert.deepEqual(updates.map(status => status.status), ['DNS_UNAVAILABLE', 'ONLINE']);
  assert.equal(updates[0].reason_code, 'WINNER_DNS_FAILED');
  assert.equal(updates[0].attempt, 1);
  assert.ok(updates[0].next_retry_at);
  assert.equal(updates[1].next_retry_at, null);
  assert.equal(classifyError(Object.assign(new Error('getaddrinfo EAI_AGAIN winner.rw'), { code: 'EAI_AGAIN' })), 'NETWORK_RECOVER');
});

test('main-site connectivity and game-provider outage are separate signals', async () => {
  const status = await probeNetwork({
    lookup: async host => {
      if (host === 'provider.test') throw Object.assign(new Error('EAI_AGAIN provider.test'), { code: 'EAI_AGAIN' });
      return { address: '198.51.100.10' };
    },
    dial: async address => { assert.equal(address, '198.51.100.10'); },
    providerHosts: ['provider.test'],
  });

  assert.equal(status.status, 'GAME_PROVIDER_UNAVAILABLE');
  assert.equal(status.evidence[0].result, 'TCP_REACHABLE');
  assert.equal(status.reason_code, 'GAME_PROVIDER_PROBE_FAILED');
});

test('alternate Aviator provider host remains online when one provider endpoint fails', async () => {
  const status = await probeNetwork({
    lookup: async host => {
      if (host === 'aviator-next.spribegaming.com') {
        throw Object.assign(new Error('EAI_AGAIN aviator-next.spribegaming.com'), { code: 'EAI_AGAIN' });
      }
      return { address: host === 'winner.rw' ? '198.51.100.10' : '198.51.100.20' };
    },
    dial: async address => {
      assert.ok(['198.51.100.10', '198.51.100.20'].includes(address));
    },
    providerHosts: ['aviator-next.spribegaming.com', 'aviaport.spribegaming.com'],
  });

  assert.equal(status.status, 'ONLINE');
  assert.deepEqual(status.evidence.slice(1).map(item => item.result), ['UNAVAILABLE', 'TCP_REACHABLE']);
});
