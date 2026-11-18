import assert from 'node:assert/strict';
import test from 'node:test';

import { getNotificationPresentation } from '../src/services/notificationPresentation.js';

test('system error notification shows its original diagnostic message', () => {
  const view = getNotificationPresentation({
    eventType: 'system.error',
    message: 'Rules Engine error: RuntimeError.',
  });
  assert.equal(view.category, 'SYSTEM HEALTH');
  assert.equal(view.summary, 'Rules Engine error: RuntimeError.');
});
