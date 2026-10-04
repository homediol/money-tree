import test from 'node:test';
import assert from 'node:assert/strict';
import { BrowserSupervisor, getBrowserSupervisor } from '../collector/BrowserManager.js';
import { RecoveryManager } from '../collector/RecoveryManager.js';
import { State, StateMachine } from '../collector/StateMachine.js';
import { classifyError } from '../collector/RetryManager.js';

test('legacy and current callers resolve one process-wide supervisor', () => {
  assert.equal(getBrowserSupervisor(false), getBrowserSupervisor(false));
});

test('betting page is a separate read-only tab in the existing context', async () => {
  const browser = new BrowserSupervisor(true);
  let navigated = null;
  let closed = false;
  const page = {
    isClosed: () => closed,
    url: () => navigated || 'about:blank',
    goto: async (url, options) => { navigated = url; assert.equal(options.waitUntil, 'domcontentloaded'); },
    locator: () => ({ waitFor: async () => {} }),
    close: async () => { closed = true; },
    on() {},
  };
  const historyPage = { isClosed: () => false, url: () => 'https://winner.rw/en/virtual/crash-games/aviator' };
  browser._browser = { isConnected: () => true };
  browser._context = { newPage: async () => page, pages: () => [historyPage, page] };
  browser._contextClosed = false;
  browser._ownsBrowser = true;
  browser._historyPage = historyPage;
  browser._restoreSessionStorage = async () => {};
  browser._trackPage = (_page, role) => assert.equal(role, 'betting');
  browser._persistMetadata = () => {};

  assert.equal(await browser.getBettingPage(), page);
  assert.equal(navigated, 'https://winner.rw/en/sportsbook/upcoming');
  await browser._cleanupManagedStarterPages(historyPage);
  assert.equal(closed, false);
});

test('recovery lock runs one operation for concurrent callers', async () => {
  const browser = new BrowserSupervisor(true);
  let starts = 0;
  const operation = async () => {
    starts += 1;
    await new Promise(resolve => setTimeout(resolve, 10));
    return 'done';
  };
  const first = browser.withRecoveryLock(operation);
  const second = browser.withRecoveryLock(operation);
  assert.deepEqual(await Promise.all([first, second]), ['done', 'done']);
  assert.equal(starts, 1);
});

test('repeated recovery failures open the circuit breaker', () => {
  const browser = new BrowserSupervisor(true);
  browser._persistMetadata = () => {};
  browser._event = () => {};
  for (let index = 0; index < 5; index += 1) browser.failRecovery('test', new Error('simulated failure'));
  assert.equal(browser._circuitBreaker.state, 'OPEN');
  assert.equal(browser._circuitBreaker.failures.length, 5);
});

test('database uniqueness errors are persistence faults, not browser recovery faults', () => {
  assert.equal(classifyError(new Error('duplicate key value violates unique constraint aviator_rounds_round_index_key')), 'PERSISTENCE_ERROR');
});

function recoveryFixture({ isLoggedIn = true, pageClosed = false, browserAlive = true } = {}) {
  const sm = new StateMachine();
  sm.transition(State.LOGIN, 'test');
  sm.transition(State.HOME, 'test');
  sm.transition(State.GAME_LOADING, 'test');
  sm.transition(State.WAITING_IFRAME, 'test');
  sm.transition(State.COLLECTING, 'test');
  let restarts = 0;
  let alive = browserAlive;
  let closed = pageClosed;
  const page = { isClosed: () => closed, url: () => 'https://winner.rw/en/virtual/crash-games/aviator', reload: async () => assert.fail('healthy Aviator page must not be reloaded') };
  const browser = {
    _circuitBreaker: { state: 'CLOSED' },
    withRecoveryLock: operation => operation(),
    beginRecovery() {},
    _persistMetadata() {},
    isAlive: () => alive,
    restart: async () => { restarts += 1; alive = true; },
    recoverHistoryPage: async () => { closed = false; return page; },
    getHistoryPage: async () => { closed = false; return page; },
  };
  const health = { setLoggedIn() {}, recordRecovery() {}, recordBrowserRestart() {}, setBrowserConnected() {}, setPageConnected() {}, setFrameConnected() {} };
  const login = { isLoggedIn: async () => isLoggedIn };
  const recovery = new RecoveryManager({ stateMachine: sm, browserManager: browser, loginManager: login, health });
  return { recovery, page, get restarts() { return restarts; } };
}

test('iframe recovery leaves a live page and browser running until a real round is verified', async () => {
  const fixture = recoveryFixture();
  const page = await fixture.recovery.recover(new Error('Frame detached'), fixture.page);
  assert.equal(page, fixture.page);
  assert.equal(fixture.restarts, 0);
  assert.equal(fixture.recovery._pendingVerification, true);
});

test('auth-required recovery does not restart Chromium', async () => {
  const fixture = recoveryFixture({ isLoggedIn: false });
  await assert.rejects(fixture.recovery.recover(Object.assign(new Error('expired'), { code: 'AUTH_REQUIRED' }), fixture.page), /AUTH_REQUIRED/);
  assert.equal(fixture.restarts, 0);
});

test('closed page recovery replaces only the page in its existing context', async () => {
  const fixture = recoveryFixture({ pageClosed: true });
  assert.equal(await fixture.recovery.recover(new Error('page closed'), fixture.page), fixture.page);
  assert.equal(fixture.restarts, 0);
});

test('browser restart is reached only after disconnection is confirmed', async () => {
  const fixture = recoveryFixture({ browserAlive: false });
  assert.equal(await fixture.recovery.recover(new Error('browser disconnected'), fixture.page), fixture.page);
  assert.equal(fixture.restarts, 1);
});
