import test from 'node:test';
import assert from 'node:assert/strict';
import { HealthMonitor } from '../collector/HealthMonitor.js';

function makeHealthMonitor() {
  const health = new HealthMonitor();
  health._flush = () => {};
  return health;
}

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
