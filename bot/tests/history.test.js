import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';

import { BrowserSupervisor } from '../collector/BrowserManager.js';
import { Collector } from '../collector/Collector.js';
import { FrameManager } from '../collector/FrameManager.js';
import { LoginManager } from '../collector/LoginManager.js';
import { selectMissingLegacyRounds } from '../collector/index.js';
import {
  appendRounds, inferNewMultipliers, normalizeMultiplier,
  readRoundHistory, stableRoundId, writeRoundHistory,
} from '../collector/HistoryManager.js';

test('validates, orders, persists atomically and preserves normalized identity', () => {
  assert.equal(normalizeMultiplier(' 2.45x '), 2.45);
  for (const bad of [0, -1, NaN, Infinity, '', 'oops']) assert.equal(normalizeMultiplier(bad), null);
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'winner-history-'));
  const file = path.join(dir, 'roundhistory.json');
  fs.writeFileSync(file, JSON.stringify([
    { round_id: '2', round_index: 2, multiplier: 3, timestamp: '2024-01-02T00:00:00Z' },
    { round_id: '1', round_index: 1, multiplier: 1.2, timestamp: '2024-01-01T00:00:00Z' },
    { round_id: '1', round_index: 3, multiplier: 9 },
    { round_id: 'bad', round_index: 4, multiplier: 0 },
  ]));
  const rows = readRoundHistory(file);
  assert.deepEqual(rows.map(r => r.round_id), ['1', '2']);
  writeRoundHistory(rows, file);
  assert.equal(fs.existsSync(file + '.tmp'), false);
  assert.deepEqual(readRoundHistory(file), rows);
});

test('parses snapshots, detects only new rounds and appends chronologically', () => {
  assert.deepEqual(inferNewMultipliers([2, 1.1, 3], [5, 2, 1.1, 3]), [5]);
  assert.deepEqual(inferNewMultipliers([2, 1.1], [2, 1.1]), []);
  const observedAt = '2026-09-26T12:00:00.000Z';
  const { history, added } = appendRounds([], [3, 2], observedAt);
  assert.deepEqual(history.map(r => r.multiplier), [2, 3]);
  assert.deepEqual(added.map(r => r.round_id), [
    stableRoundId(observedAt, 2, 1),
    stableRoundId(observedAt, 3, 2),
  ]);
});

test('PostgreSQL startup imports only rounds absent by both ID and index', () => {
  const persisted = [{ round_id: 'platform-1', round_index: 1 }];
  const file = [
    { round_id: 'platform-1', round_index: 1 },
    { round_id: 'old-hash-1', round_index: 1 },
    { round_id: 'platform-2', round_index: 2 },
  ];
  assert.deepEqual(selectMissingLegacyRounds(file, persisted), {
    missing: [file[2]], indexConflicts: 1,
  });
});

test('history page reuses one browser context and never launches another browser', async () => {
  let newPages = 0;
  const page = { isClosed: () => false, url: () => 'about:blank' };
  const manager = new BrowserSupervisor(true);
  manager.isAlive = () => true;
  manager.launch = async () => { throw new Error('must not launch'); };
  manager._context = { pages: () => [page], newPage: async () => { newPages += 1; return page; } };
  manager._page = page;
  assert.equal(await manager.getHistoryPage(), page);
  assert.equal(await manager.getHistoryPage(), page);
  assert.equal(newPages, 1);
});

test('managed history page removes restored MetaBrandTitle and blank tabs', async () => {
  const closed = [];
  const staleWinner = {
    isClosed: () => closed.includes('winner'),
    url: () => 'https://winner.rw/sportsbook/upcoming',
    close: async () => { closed.push('winner'); },
  };
  const blank = {
    isClosed: () => closed.includes('blank'),
    url: () => 'chrome://newtab/',
    close: async () => { closed.push('blank'); },
  };
  let focused = 0;
  const historyPage = {
    isClosed: () => false,
    url: () => 'https://winner.rw/en/virtual/crash-games/aviator',
    bringToFront: async () => { focused += 1; },
  };
  const manager = new BrowserSupervisor(true);
  manager.isAlive = () => true;
  manager._ownsBrowser = true;
  manager._page = staleWinner;
  let newPages = 0;
  manager._context = {
    pages: () => [staleWinner, blank, historyPage],
    newPage: async () => { newPages += 1; throw new Error('must reuse Aviator tab'); },
  };

  assert.equal(await manager.getHistoryPage(), historyPage);
  assert.deepEqual(closed.sort(), ['blank', 'winner']);
  assert.equal(focused, 1);
  assert.equal(newPages, 0);
  assert.equal(manager._page, null);
});

test('managed blank tab is reused, then later sportsbook tabs are removed after Aviator opens', async () => {
  let url = 'about:blank';
  let closed = false;
  let focused = 0;
  let extra = false;
  const page = {
    isClosed: () => false,
    url: () => url,
    bringToFront: async () => { focused += 1; },
  };
  const sportsbook = {
    isClosed: () => closed,
    url: () => 'https://winner.rw/sportsbook/upcoming',
    close: async () => { closed = true; },
  };
  const manager = new BrowserSupervisor(true);
  manager.isAlive = () => true;
  manager._ownsBrowser = true;
  manager._page = page;
  manager._context = {
    pages: () => extra && !closed ? [page, sportsbook] : [page],
    newPage: async () => { throw new Error('must reuse managed tab'); },
  };
  assert.equal(await manager.getHistoryPage(), page);
  assert.equal(closed, false);
  extra = true;
  url = 'https://winner.rw/en/virtual/crash-games/aviator';
  await manager.focusGamePage(page);
  assert.equal(closed, true);
  assert.equal(focused, 2);
});

test('attached user browser tabs are never cleaned up', async () => {
  let closed = 0;
  const userPage = {
    isClosed: () => false,
    url: () => 'https://winner.rw/sportsbook/upcoming',
    close: async () => { closed += 1; },
  };
  const manager = new BrowserSupervisor(true);
  manager._ownsBrowser = false;
  manager._context = { pages: () => [userPage] };
  await manager._cleanupManagedStarterPages(null);
  assert.equal(closed, 0);
});

test('history page recovery stays in the existing browser context', async () => {
  let newPages = 0;
  let closed = 0;
  const order = [];
  const oldPage = {
    isClosed: () => false,
    url: () => 'https://winner.rw/en/virtual/crash-games/aviator',
    close: async () => { order.push('close'); closed += 1; },
  };
  const freshPage = {
    isClosed: () => false,
    url: () => 'about:blank',
    goto: async () => {},
  };
  const context = {
    pages: () => [oldPage],
    newPage: async () => { order.push('new'); newPages += 1; return freshPage; },
  };
  const manager = new BrowserSupervisor(true);
  manager.isAlive = () => true;
  manager.launch = async () => { throw new Error('must not launch'); };
  manager._context = context;
  manager._historyPage = oldPage;
  manager._page = oldPage;
  assert.equal(await manager.recoverHistoryPage(), freshPage);
  assert.equal(manager._context, context);
  assert.equal(closed, 1);
  assert.equal(newPages, 1);
  assert.deepEqual(order, ['new', 'close']);
});

test('navigation accepts a rendered Winner route after load timeout', async () => {
  const body = { count: async () => 1, innerText: async () => 'Winner sportsbook' };
  const page = {
    goto: async () => { throw new Error('Timeout 45000ms exceeded'); },
    url: () => 'https://winner.rw/en/sportsbook/upcoming',
    locator: () => body,
    waitForSelector: async () => {},
  };
  const manager = new LoginManager({ phone: 'test', password: 'test' });
  assert.equal(await manager._navigate(page, 'https://winner.rw/', null), true);
});

test('frame recovery reloads Aviator without visiting the sportsbook homepage', async () => {
  const visits = [];
  const page = {
    url: () => 'https://winner.rw/en/virtual/crash-games/aviator',
    locator: selector => selector === 'body'
      ? { innerText: async () => 'Aviator' }
      : { first: () => ({ isVisible: async () => false }) },
  };
  const manager = new LoginManager({ phone: 'test', password: 'test' });
  manager._navigate = async (_page, url) => { visits.push(url); return true; };
  assert.equal(await manager.goToAviator(page), true);
  assert.deepEqual(visits, ['https://winner.rw/en/virtual/crash-games/aviator']);
});

test('frame manager uses direct CDP when Playwright omits the game OOPIF', async () => {
  const direct = {
    isDetached: () => false,
    name: () => 'aviator-next-cdp',
    url: () => 'https://aviaport.spribegaming.com/aviator',
    locator: () => ({ first: () => ({ waitFor: async () => {} }) }),
  };
  const page = {
    frames: () => [],
    mainFrame: () => null,
    waitForEvent: async () => {},
  };
  const manager = new FrameManager();
  manager._connectDirectFrame = async () => direct;
  assert.equal(await manager.waitForFrame(page, { timeoutMs: 100 }), direct);
});

test('frame manager does not return an outer Aviator iframe without payouts', async () => {
  let checks = 0;
  const outer = {
    isDetached: () => false,
    name: () => 'aviator',
    url: () => 'https://aviaport.spribegaming.com/aviator',
    locator: () => ({ first: () => ({ waitFor: async () => { throw new Error('no payouts'); } }) }),
  };
  const direct = {
    isDetached: () => false,
    name: () => 'aviator-next-cdp',
    url: () => 'https://aviator-next.spribegaming.com/',
    locator: () => ({ first: () => ({ waitFor: async () => {
      if (++checks < 2) throw new Error('game still loading');
    } }) }),
  };
  const main = {};
  const page = {
    frames: () => [main, outer],
    mainFrame: () => main,
    waitForEvent: async () => {},
  };
  const manager = new FrameManager();
  manager._connectDirectFrame = async () => direct;
  assert.equal(await manager.waitForFrame(page, { timeoutMs: 1000 }), direct);
  assert.equal(checks, 2);
});

test('collector uses Node-side mutation polling for a direct CDP frame', async () => {
  let requestedIdle = null;
  const frame = {
    waitForCollectorMutation: async idleMs => {
      requestedIdle = idleMs;
      return { multipliers: [2.5, 1.1], sig: '2.50|1.10', ts: '2026-09-27T10:00:00Z' };
    },
  };
  const result = await new Collector().waitForMutation(frame, 1234);
  assert.equal(requestedIdle, 1234);
  assert.deepEqual(result.multipliers, [2.5, 1.1]);
  assert.equal(result.timestamp, '2026-09-27T10:00:00Z');
});

test('collector installs an observer from a ready frame', async () => {
  const frame = { evaluate: async () => ({ ok: true, multipliers: [1.2, 2.5] }) };
  const result = await new Collector().install(frame);
  assert.deepEqual(result.multipliers, [1.2, 2.5]);
  assert.equal(result.signature, '1.20|2.50');
});
