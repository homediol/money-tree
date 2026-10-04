import assert from 'node:assert/strict';
import test from 'node:test';

import { waitForNetwork } from '../collector/NetworkMonitor.js';

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
