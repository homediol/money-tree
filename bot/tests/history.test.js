import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { randomUUID } from 'node:crypto';
import test from 'node:test';

import { BrowserSupervisor } from '../collector/BrowserManager.js';
import { Collector } from '../collector/Collector.js';
import { FrameManager } from '../collector/FrameManager.js';
import { SharedPageCoordinator } from '../collector/SharedPageCoordinator.js';
import { isAviatorRoute, LoginManager } from '../collector/LoginManager.js';
import { canContinueReadOnlyCollection, selectMissingLegacyRounds } from '../collector/index.js';
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
  const overlap = [1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 1.7, 1.8, 1.9, 2.0];
  assert.deepEqual(inferNewMultipliers(overlap, [5, ...overlap]), [5]);
  assert.equal(inferNewMultipliers([2, 1.1, 3], [5, 2, 1.1, 3]), null);
  assert.deepEqual(inferNewMultipliers([2, 1.1], [2, 1.1]), []);
  const observedAt = '2026-09-26T12:00:00.000Z';
  const { history, added } = appendRounds([], [3, 2], observedAt);
  assert.deepEqual(history.map(r => r.multiplier), [2, 3]);
  assert.deepEqual(added.map(r => r.round_id), [
    stableRoundId(observedAt, 2, 1),
    stableRoundId(observedAt, 3, 2),
  ]);
  assert.equal(added[0].round_identity_type, 'COLLECTOR_OBSERVATION_HASH');
  assert.equal(added[0].continuity_verified, true);
  assert.equal(added[0].continuity_proof, 'HISTORY_SNAPSHOT_OVERLAP');
  const nextEpoch = appendRounds([], [2], observedAt, { historyEpoch: 2 }).added[0];
  assert.equal(nextEpoch.history_epoch, 2);
  assert.notEqual(nextEpoch.round_id, added[0].round_id);
  assert.equal(stableRoundId(observedAt, 2, 1, 1), added[0].round_id,
    'epoch 1 retains pre-epoch stable identities');
});

test('startup overlap search covers every offset visible in a 50-round snapshot', () => {
  const persistedTail = Array.from({ length: 50 }, (_, index) => Number((index + 1.07).toFixed(2)));
  const newRounds = Array.from({ length: 35 }, (_, index) => Number((100 + index + 0.23).toFixed(2)));
  const currentSnapshot = [...newRounds, ...persistedTail.slice(0, 15)];

  assert.deepEqual(inferNewMultipliers(persistedTail, currentSnapshot), newRounds);

  const maxShiftNewRounds = Array.from({ length: 40 }, (_, index) => Number((200 + index + 0.31).toFixed(2)));
  assert.deepEqual(
    inferNewMultipliers(persistedTail, [...maxShiftNewRounds, ...persistedTail.slice(0, 10)]),
    maxShiftNewRounds,
  );

  const tooOldForSnapshot = Array.from({ length: 41 }, (_, index) => Number((300 + index + 0.47).toFixed(2)));
  assert.equal(inferNewMultipliers(persistedTail, [...tooOldForSnapshot, ...persistedTail.slice(0, 9)]), null);
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

test('read-only collection permits unknown auth but rejects explicit expiry or manual verification', () => {
  assert.equal(canContinueReadOnlyCollection('AUTHENTICATED'), true);
  assert.equal(canContinueReadOnlyCollection('UNKNOWN'), true);
  assert.equal(canContinueReadOnlyCollection('EXPIRED'), false);
  assert.equal(canContinueReadOnlyCollection('MANUAL_VERIFICATION'), false);
  assert.equal(canContinueReadOnlyCollection(undefined), false);
});

test('shared Aviator page reuses one browser context and never opens another page', async () => {
  let newPages = 0;
  const page = { isClosed: () => false, url: () => 'about:blank' };
  const manager = new BrowserSupervisor(true);
  manager.isAlive = () => true;
  manager.launch = async () => { throw new Error('must not launch'); };
  manager._context = { pages: () => [page], newPage: async () => { newPages += 1; return page; } };
  manager._page = page;
  assert.equal(await manager.getHistoryPage(), page);
  assert.equal(await manager.getBettingPage(), page);
  assert.equal(newPages, 0);
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
  assert.equal(focused, 0);
  assert.equal(newPages, 0);
  assert.equal(manager._page, historyPage);
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
  assert.equal(focused, 1);
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

test('page recovery reuses the healthy game tab and never creates a replacement', async () => {
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
    addInitScript: async () => {},
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
  manager._aviatorPage = oldPage;
  assert.equal(await manager.recoverHistoryPage(), oldPage);
  assert.equal(manager._context, context);
  assert.equal(closed, 0);
  assert.equal(newPages, 0);
  assert.deepEqual(order, []);
});

test('shared page recovery does not replace or close the active game tab', async () => {
  const pages = [];
  const oldHistory = {
    isClosed: () => false,
    url: () => 'https://winner.rw/en/virtual/crash-games/aviator',
    close: async () => { pages.splice(pages.indexOf(oldHistory), 1); },
  };
  pages.push(oldHistory);
  const replacement = {
    isClosed: () => false,
    url: () => 'about:blank',
    goto: async () => {},
  };
  const context = {
    pages: () => pages,
    newPage: async () => { pages.push(replacement); return replacement; },
  };
  const manager = new BrowserSupervisor(true);
  manager.isAlive = () => true;
  manager._context = context;
  manager._contextClosed = false;
  manager._historyPage = oldHistory;
  manager._page = oldHistory;
  manager._aviatorPage = oldHistory;
  manager._ownsBrowser = true;
  manager._persistSession = async () => {};
  manager._restoreSessionStorage = async () => {};
  manager._trackPage = () => {};
  manager._cleanupManagedStarterPages = async () => {};

  assert.equal(await manager.recoverHistoryPage(), oldHistory);
  assert.equal(manager._context, context);
  assert.equal(manager._bettingPage, oldHistory);
  assert.deepEqual(pages, [oldHistory]);
});

test('automatic navigation is blocked because page setup belongs to the operator', async () => {
  const body = { count: async () => 1, innerText: async () => 'Winner sportsbook' };
  const page = {
    goto: async () => { throw new Error('Timeout 45000ms exceeded'); },
    url: () => 'https://winner.rw/en/sportsbook/upcoming',
    locator: () => body,
    waitForSelector: async () => {},
  };
  const manager = new LoginManager({ phone: 'test', password: 'test' });
  await assert.rejects(manager._navigate(page, 'https://winner.rw/', null),
    error => error.code === 'MANUAL_OPERATOR_SETUP_REQUIRED');
});

test('logout popup route cannot be mistaken for an authenticated session', async () => {
  const page = {
    isClosed: () => false,
    url: () => 'https://winner.rw/en/user/logout/popup',
    locator: selector => ({
      first: () => ({ isVisible: async () => selector === 'a[href*="logout"]' }),
      innerText: async () => 'Sport Betting',
    }),
  };
  const manager = new LoginManager({ phone: 'test', password: 'test' });
  assert.equal(await manager.isLoggedIn(page), false);
});

test('Aviator UI launch controls are never clicked by the collector', async () => {
  const actions = [];
  let attached = false;
  const mainFrame = {};
  const gameFrame = {
    isDetached: () => false,
    name: () => 'aviator-game',
    url: () => 'https://aviator.spribegaming.com/game',
  };
  const empty = { count: async () => 0, nth: () => empty };
  const crash = {
    count: async () => 1,
    nth: () => crash,
    isVisible: async () => true,
    click: async () => actions.push('crash-games'),
  };
  const search = {
    first: () => search,
    waitFor: async () => {},
    fill: async value => actions.push(`search:${value}`),
  };
  const play = {
    count: async () => 1,
    nth: () => play,
    isVisible: async () => true,
    click: async () => { actions.push('play-game'); attached = true; },
  };
  const page = {
    url: () => attached ? 'https://winner.rw/en/virtual/crash-games/aviator' : 'https://winner.rw/en',
    getByRole: (role, options) => /crash games/i.test(options.name)
      ? role === 'button' ? crash : empty
      : /play game/i.test(options.name) ? role === 'button' ? play : empty : empty,
    getByText: () => empty,
    locator: selector => selector.includes('au-m-cs') ? search : empty,
    frames: () => attached ? [mainFrame, gameFrame] : [mainFrame],
    mainFrame: () => mainFrame,
  };
  const manager = new LoginManager({ phone: 'test', password: 'test' });
  assert.equal(await manager._openThroughCrashGamesSearch(page), false);
  assert.deepEqual(actions, []);
});

test('login return URL containing Aviator is not treated as a game route', async () => {
  assert.equal(isAviatorRoute('https://winner.rw/en/authentication/login?returnUrl=%2Fen%2Fvirtual%2Fcrash-games%2Faviator'), false);
  assert.equal(isAviatorRoute('https://winner.rw/en/virtual/crash-games/aviator?campaign=live'), true);
  assert.equal(isAviatorRoute('https://aviator.spribegaming.com/game'), false);

  let navigationAttempts = 0;
  const page = {
    isClosed: () => false,
    url: () => 'https://winner.rw/en/authentication/login?returnUrl=%2Faviator',
    locator: () => ({ innerText: async () => '' }),
    goto: async () => { navigationAttempts += 1; },
  };
  const manager = new LoginManager({ phone: '', password: '' });

  await assert.rejects(manager.goToAviator(page, AbortSignal.abort()), /Aborted/);
  assert.equal(navigationAttempts, 0, 'a login route must not trigger game controls or canonical route retries');
});

test('Aviator route and Play controls are not used to open the game automatically', async () => {
  let currentUrl = 'https://winner.rw/en';
  let clickAttempts = 0;
  let aviatorLocatorBuilds = 0;
  let attached = false;
  const mainFrame = {};
  const gameFrame = {
    isDetached: () => false,
    name: () => 'aviator-game',
    url: () => 'https://aviator.spribegaming.com/game',
  };
  const empty = { count: async () => 0, nth: () => empty };
  const page = {
    isClosed: () => false,
    url: () => currentUrl,
    mainFrame: () => mainFrame,
    frames: () => attached ? [mainFrame, gameFrame] : [mainFrame],
    locator: selector => selector === 'body' ? { innerText: async () => '' } : empty,
    getByRole: (_role, options) => {
      if (!/aviator/i.test(options.name)) return empty;
      aviatorLocatorBuilds += 1;
      return {
        count: async () => 1,
        nth: () => ({
          isVisible: async () => true,
          click: async () => {
            clickAttempts += 1;
            if (clickAttempts === 1) throw new Error('Timeout: element is not stable');
            currentUrl = 'https://winner.rw/en/virtual/crash-games/aviator';
            attached = true;
          },
        }),
      };
    },
    getByText: () => empty,
  };
  const manager = new LoginManager({ phone: '', password: '' });
  manager._openThroughCrashGamesSearch = async () => false;
  manager._dismissModals = async () => {};
  manager._navigate = async () => false;

  await assert.rejects(manager.goToAviator(page, AbortSignal.abort()), /Aborted/);
  assert.equal(clickAttempts, 0);
  assert.equal(aviatorLocatorBuilds, 0);
});

test('an attached Aviator iframe is reused without a homepage navigation', async () => {
  const mainFrame = {};
  const gameFrame = {
    isDetached: () => false,
    name: () => 'aviator-game',
    url: () => 'https://aviator.spribegaming.com/game',
  };
  const page = {
    url: () => 'https://winner.rw/en/virtual/crash-games/aviator',
    locator: selector => selector === 'body'
      ? { innerText: async () => 'Aviator' }
      : { count: async () => 0, first: () => ({ isVisible: async () => false }) },
    frames: () => [mainFrame, gameFrame],
    mainFrame: () => mainFrame,
  };
  const manager = new LoginManager({ phone: 'test', password: 'test' });
  assert.equal(await manager.goToAviator(page), true);
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
  const readyEvents = [];
  manager.on('ready', event => readyEvents.push(event));
  assert.equal(await manager.waitForFrame(page, { timeoutMs: 100 }), direct);
  assert.equal(readyEvents.length, 1);
  assert.equal(readyEvents[0].page, page);
  assert.equal(readyEvents[0].frame, direct);
  assert.equal(readyEvents[0].method, 'direct-cdp');
});

test('collector keeps reading the shared game iframe while betting UI lock is held', async () => {
  const gameFrame = {
    url: () => 'https://aviator-next.spribegaming.com/',
    isDetached: () => false,
    locator: () => ({ first: () => ({ waitFor: async () => {} }) }),
  };
  const mainFrame = { url: () => 'https://winner.rw/en/virtual/crash-games/aviator', isDetached: () => false };
  const page = { frames: () => [mainFrame, gameFrame], mainFrame: () => mainFrame };
  const pageManager = {
    getAviatorPage: async () => page,
    getAviatorFrame: async () => gameFrame,
  };
  const tempDir = fs.mkdtempSync(path.join(os.tmpdir(), 'winner-history-ui-lock-'));
  const coordinator = new SharedPageCoordinator({ lockDirectory: path.join(tempDir, randomUUID()) });
  let releaseUi;
  let uiStarted;
  const started = new Promise(resolve => { uiStarted = resolve; });
  const uiOperation = coordinator.withBettingUiLock(() => new Promise(resolve => {
    releaseUi = resolve;
    uiStarted();
  }));
  await started;
  const frameManager = new FrameManager({ pageManager });

  const observedFrame = await frameManager.waitForFrame(page, { timeoutMs: 50 });
  assert.equal(observedFrame, gameFrame);
  releaseUi();
  await uiOperation;
  fs.rmSync(tempDir, { recursive: true, force: true });
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

test('attached Aviator frame with delayed payout source reports readiness only after payouts appear', async () => {
  let checks = 0;
  const main = {};
  const nestedGameFrame = {
    isDetached: () => false,
    name: () => 'nested-game-surface',
    url: () => 'https://aviaport.spribegaming.com/session',
    locator: () => ({ first: () => ({ waitFor: async () => {
      checks += 1;
      if (checks < 2) throw new Error('history is still initializing');
    } }) }),
  };
  const page = {
    frames: () => [main, nestedGameFrame],
    mainFrame: () => main,
    waitForEvent: async () => {},
  };
  const manager = new FrameManager();
  manager._connectDirectFrame = async () => null;

  assert.equal(await manager.waitForFrame(page, { timeoutMs: 1000 }), nestedGameFrame);
  assert.deepEqual(manager.readiness(), {
    frameAttached: true, contentLoaded: true, payoutsReady: true, reason: 'PAYOUT_SELECTOR_PRESENT',
  });
  assert.equal(checks, 2);
});

test('attached frame timeout is distinct from a missing frame', async () => {
  const main = {};
  const gameFrame = {
    isDetached: () => false,
    name: () => 'aviator-game',
    url: () => 'https://aviaport.spribegaming.com/session',
    locator: () => ({ first: () => ({ waitFor: async () => { throw new Error('payouts not mounted'); } }) }),
  };
  const page = {
    frames: () => [main, gameFrame],
    mainFrame: () => main,
    waitForEvent: async () => {},
  };
  const manager = new FrameManager();
  manager._connectDirectFrame = async () => null;

  await assert.rejects(manager.waitForFrame(page, { timeoutMs: 10 }), error => error.code === 'PAYOUTS_UNAVAILABLE');
  assert.deepEqual(manager.readiness(), {
    frameAttached: true, contentLoaded: false, payoutsReady: false, reason: 'FRAME_ATTACHED_WAITING_FOR_PAYOUTS',
  });
});

test('detached frame is not reused as a valid game frame', async () => {
  const main = {};
  const detachedGameFrame = {
    isDetached: () => true,
    name: () => 'aviator-game',
    url: () => 'https://aviaport.spribegaming.com/session',
  };
  const page = {
    frames: () => [main, detachedGameFrame],
    mainFrame: () => main,
    waitForEvent: async () => {},
  };
  const manager = new FrameManager();
  manager._connectDirectFrame = async () => null;

  await assert.rejects(manager.waitForFrame(page, { timeoutMs: 10 }), error => error.code === 'GAME_FRAME_MISSING');
  assert.equal(manager.readiness().frameAttached, false);
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
