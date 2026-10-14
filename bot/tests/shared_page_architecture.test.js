import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { randomUUID } from 'node:crypto';
import { SharedPageCoordinator } from '../collector/SharedPageCoordinator.js';

function testLockPath() {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'winner-shared-ui-'));
  return { directory, lockDirectory: path.join(directory, randomUUID()) };
}

test('betting UI writes are serialized while history reads continue independently', async () => {
  const temp = testLockPath();
  const coordinator = new SharedPageCoordinator({ lockDirectory: temp.lockDirectory });
  const secondCoordinator = new SharedPageCoordinator({ lockDirectory: temp.lockDirectory });
  let releaseFirst;
  let firstStarted;
  const started = new Promise(resolve => { firstStarted = resolve; });
  const firstGate = new Promise(resolve => { releaseFirst = resolve; });
  const order = [];

  const panelOne = coordinator.withBettingUiLock(async () => {
    order.push('panel-one-start');
    firstStarted();
    await firstGate;
    order.push('panel-one-finish');
  });
  const panelTwo = secondCoordinator.withBettingUiLock(async () => {
    order.push('panel-two-start');
  });

  // Collector history reads do not acquire the betting UI mutex.
  await started;
  await Promise.resolve().then(() => order.push('history-read'));
  assert.deepEqual(order, ['panel-one-start', 'history-read']);
  assert.equal(coordinator.bettingUiBusy, true);

  releaseFirst();
  await Promise.all([panelOne, panelTwo]);
  assert.deepEqual(order, ['panel-one-start', 'history-read', 'panel-one-finish', 'panel-two-start']);
  assert.equal(coordinator.bettingUiBusy, false);
  fs.rmSync(temp.directory, { recursive: true, force: true });
});

test('an aborted waiter does not enter the betting UI mutation section', async () => {
  const temp = testLockPath();
  const coordinator = new SharedPageCoordinator({ lockDirectory: temp.lockDirectory });
  let releaseFirst;
  let firstStarted;
  const started = new Promise(resolve => { firstStarted = resolve; });
  const first = coordinator.withBettingUiLock(() => new Promise(resolve => {
    releaseFirst = resolve;
    firstStarted();
  }));
  const controller = new AbortController();
  let entered = false;
  const second = coordinator.withBettingUiLock(() => { entered = true; }, { signal: controller.signal });
  await started;
  controller.abort();
  releaseFirst();
  await first;
  await assert.rejects(second, /Aborted/);
  assert.equal(entered, false);
  fs.rmSync(temp.directory, { recursive: true, force: true });
});
