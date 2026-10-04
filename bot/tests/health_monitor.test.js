import test from 'node:test';
import assert from 'node:assert/strict';
import { HealthMonitor } from '../collector/HealthMonitor.js';

test('collecting telemetry becomes COLLECTOR_STALE after the last round ages out', () => {
  const health = new HealthMonitor();
  health._metrics.collectorRunning = true;
  health._metrics.browserConnected = true;
  health._metrics.pageConnected = true;
  health._metrics.frameConnected = true;
  health._metrics.loggedIn = true;
  health._metrics.network.status = 'ONLINE';
  health._metrics.state = 'COLLECTING';
  health._metrics.lastSuccessfulCollection = new Date(Date.now() - 240_000).toISOString();

  const snapshot = health.snapshot();
  assert.equal(snapshot.health, 'STALE');
  assert.equal(snapshot.state, 'COLLECTOR_STALE');
});

test('fresh verified round keeps collector health healthy', () => {
  const health = new HealthMonitor();
  health._metrics.collectorRunning = true;
  health._metrics.browserConnected = true;
  health._metrics.pageConnected = true;
  health._metrics.frameConnected = true;
  health._metrics.loggedIn = true;
  health._metrics.network.status = 'ONLINE';
  health._metrics.state = 'COLLECTING';
  health._metrics.lastSuccessfulCollection = new Date().toISOString();

  const snapshot = health.snapshot();
  assert.equal(snapshot.health, 'HEALTHY');
  assert.equal(snapshot.state, 'COLLECTING');
});

test('small platform timestamp reversal does not stale a freshly persisted round', () => {
  const health = new HealthMonitor();
  health._metrics.collectorRunning = true;
  health._metrics.browserConnected = true;
  health._metrics.pageConnected = true;
  health._metrics.frameConnected = true;
  health._metrics.loggedIn = true;
  health._metrics.network.status = 'ONLINE';
  health._metrics.state = 'COLLECTING';
  health.recordRound({ timestamp: new Date(Date.now() - 240_000).toISOString() });

  const snapshot = health.snapshot();
  assert.equal(snapshot.health, 'HEALTHY');
  assert.equal(snapshot.state, 'COLLECTING');
});
