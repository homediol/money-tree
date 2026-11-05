import test from 'node:test';
import assert from 'node:assert/strict';
import { HealthMonitor } from '../collector/HealthMonitor.js';

function makeHealthMonitor() {
  const health = new HealthMonitor();
  health._flush = () => {};
  return health;
}

test('session watchdog publishes verified state without inventing idle time', () => {
  const health = makeHealthMonitor();
  health.setSessionWatchdog({
    status: 'EXPIRING', monitor_tab_status: 'OPEN', reason_code: 'LOGOUT_COUNTDOWN_VISIBLE',
    last_successful_check_at: '2026-10-31T10:00:00.000Z', logout_countdown_seconds: 20,
    estimated_remaining_seconds: 'unknown', collector_connected: true,
  });
  const watchdog = health.snapshot().sessionWatchdog;
  assert.equal(watchdog.status, 'EXPIRING');
  assert.equal(watchdog.monitor_tab_status, 'OPEN');
  assert.equal(watchdog.logout_countdown_seconds, 20);
  assert.equal(watchdog.estimated_remaining_seconds, null);
  assert.equal(watchdog.last_successful_activity_at, null);
  assert.equal(watchdog.collector_connected, true);
});

test('collecting telemetry becomes COLLECTOR_STALE after the last round ages out', () => {
  const health = makeHealthMonitor();
  health._metrics.collectorRunning = true;
  health._metrics.browserConnected = true;
  health._metrics.pageConnected = true;
  health._metrics.frameConnected = true;
  health._metrics.loggedIn = true;
  health._metrics.network.status = 'ONLINE';
  health._metrics.network.checked_at = new Date().toISOString();
  health._metrics.state = 'COLLECTING';
  health._metrics.lastSuccessfulCollection = new Date(Date.now() - 240_000).toISOString();

  const snapshot = health.snapshot();
  assert.equal(snapshot.health, 'STALE');
  assert.equal(snapshot.state, 'COLLECTOR_STALE');
});

test('fresh verified round keeps collector health healthy', () => {
  const health = makeHealthMonitor();
  health._metrics.collectorRunning = true;
  health._metrics.browserConnected = true;
  health._metrics.pageConnected = true;
  health._metrics.frameConnected = true;
  health._metrics.loggedIn = true;
  health._metrics.network.status = 'ONLINE';
  health._metrics.network.checked_at = new Date().toISOString();
  health._metrics.state = 'COLLECTING';
  health._metrics.lastSuccessfulCollection = new Date().toISOString();

  const snapshot = health.snapshot();
  assert.equal(snapshot.health, 'HEALTHY');
  assert.equal(snapshot.state, 'COLLECTING');
});

test('small platform timestamp reversal does not stale a freshly persisted round', () => {
  const health = makeHealthMonitor();
  health._metrics.collectorRunning = true;
  health._metrics.browserConnected = true;
  health._metrics.pageConnected = true;
  health._metrics.frameConnected = true;
  health._metrics.loggedIn = true;
  health._metrics.network.status = 'ONLINE';
  health._metrics.network.checked_at = new Date().toISOString();
  health._metrics.state = 'COLLECTING';
  health.recordRound({ timestamp: new Date(Date.now() - 240_000).toISOString() });

  const snapshot = health.snapshot();
  assert.equal(snapshot.health, 'HEALTHY');
  assert.equal(snapshot.state, 'COLLECTING');
});

test('missing network probe evidence prevents a healthy collector report', () => {
  const health = makeHealthMonitor();
  health._metrics.collectorRunning = true;
  health._metrics.browserConnected = true;
  health._metrics.pageConnected = true;
  health._metrics.frameConnected = true;
  health._metrics.loggedIn = true;
  health._metrics.network.status = 'ONLINE';
  health._metrics.state = 'COLLECTING';

  const snapshot = health.snapshot();
  assert.equal(snapshot.health, 'STALE');
  assert.equal(snapshot.state, 'STALE');
});

test('cashout setup status is reported without interrupting round collection health', () => {
  const health = makeHealthMonitor();
  health._metrics.collectorRunning = true;
  health._metrics.browserConnected = true;
  health._metrics.pageConnected = true;
  health._metrics.frameConnected = true;
  health._metrics.loggedIn = true;
  health._metrics.network.status = 'ONLINE';
  health._metrics.network.checked_at = new Date().toISOString();
  health._metrics.state = 'COLLECTING';
  health._metrics.lastSuccessfulCollection = new Date().toISOString();

  health.setCashoutInitialization({ status: 'FAILED', target: '2.00', auto_bet: 'UNKNOWN',
    panel_1_cashout: 'READY', panel_2_cashout: 'FAILED', panels: [],
    updated_at: new Date().toISOString(), error_reason: 'AUTO_CASHOUT_TOGGLE_NOT_FOUND' });
  const snapshot = health.snapshot();
  assert.equal(snapshot.cashout_initialization.status, 'FAILED');
  assert.equal(snapshot.cashout_initialization.panel_2_cashout, 'FAILED');
  assert.equal(snapshot.state, 'COLLECTING');
  assert.equal(snapshot.health, 'HEALTHY');
});

test('an older UNKNOWN cashout snapshot cannot replace a newer READY verification', () => {
  const health = makeHealthMonitor();
  const readyAt = new Date(Date.now() + 1000).toISOString();
  const olderAt = new Date(Date.now() - 1000).toISOString();
  health.setCashoutInitialization({ status: 'READY', target: '2.00', auto_bet: 'OFF',
    panel_1_cashout: 'READY', panel_2_cashout: 'READY', panels: [],
    updated_at: readyAt });
  health.setCashoutInitialization({ status: 'UNKNOWN', target: '2.00', auto_bet: 'UNKNOWN',
    panel_1_cashout: 'UNKNOWN', panel_2_cashout: 'UNKNOWN', panels: [],
    updated_at: olderAt, error_reason: 'FRAME_NOT_READY' });
  assert.equal(health.snapshot().cashout_initialization.status, 'READY');
});

test('recovered commits without a monotonic start do not become zero-latency samples', () => {
  const health = makeHealthMonitor();
  health.recordRounds([{ round_id: 'recovered-round', observed_at: '2026-10-09T21:00:00Z' }],
    '2026-10-09T22:00:00Z', [{ round_id: 'recovered-round', observationToCommitMs: null }]);

  assert.deepEqual(health.snapshot().roundCommitSamples, []);
});

test('batched observed-round and storage health updates flush the status file once', () => {
  const health = new HealthMonitor();
  let flushes = 0;
  health._writeSnapshot = () => { flushes += 1; };

  health.batch(() => {
    health.recordObservedRound({ round_id: 'observed-1', round_index: 7,
      observed_at: '2026-10-18T07:00:00.000Z' });
    health.setStorage({ status: 'AVAILABLE', backlog_size: 1 });
  });

  assert.equal(flushes, 1);
  assert.equal(health.snapshot().lastObservedRoundId, 'observed-1');
  assert.equal(health.snapshot().storage.status, 'AVAILABLE');
});

test('round source and delivery states distinguish waiting, invalid, failed, and caught-up rounds', () => {
  const health = makeHealthMonitor();
  health._metrics.collectorRunning = true;
  health._metrics.browserConnected = true;
  health._metrics.pageConnected = true;
  health._metrics.frameConnected = true;
  health._metrics.authenticationState = 'AUTHENTICATED';
  health._metrics.loggedIn = true;
  health._metrics.network = { status: 'ONLINE', checked_at: new Date().toISOString() };
  health._metrics.state = 'COLLECTING';
  health._metrics.lastObserverInstalledAt = new Date().toISOString();
  health._metrics.storage = { status: 'AVAILABLE', backlog_size: 0,
    round_delivery: { backlog_size: 0 } };

  assert.equal(health.snapshot().roundSourceStatus, 'AVAILABLE');
  assert.equal(health.snapshot().roundCollectionStatus, 'NO_NEW_ROUND_YET');

  health.recordRoundDetection({ detected_at: new Date().toISOString(), status: 'INVALID',
    reason: 'NO_ROUND_OVERLAP_PROOF' }, { flush: false });
  assert.equal(health.snapshot().roundCollectionStatus, 'ROUND_DETECTED_INVALID');

  health._metrics.storage.round_delivery.last_failure = { stage: 'persistence' };
  assert.equal(health.snapshot().roundCollectionStatus, 'PERSISTENCE_FAILED');

  health._metrics.storage.round_delivery.last_failure = null;
  health._metrics.lastDetectedStatus = 'VALIDATED';
  health._metrics.lastObservedRoundId = 'round-10';
  health._metrics.lastRoundId = 'round-10';
  health._metrics.lastSuccessfulCollection = new Date().toISOString();
  assert.equal(health.snapshot().roundCollectionStatus, 'HEALTHY_CAUGHT_UP');

  health._metrics.frameConnected = false;
  assert.equal(health.snapshot().roundSourceStatus, 'SOURCE_UNAVAILABLE');
  assert.equal(health.snapshot().roundCollectionStatus, 'SOURCE_UNAVAILABLE');
});
