import test from 'node:test';
import assert from 'node:assert/strict';
import { AviatorPageManager, BrowserSupervisor, getBrowserSupervisor,
  validateSpribeDemoUrl } from '../collector/BrowserManager.js';
import { FrameManager } from '../collector/FrameManager.js';
import { RecoveryManager } from '../collector/RecoveryManager.js';
import { Watchdog } from '../collector/Watchdog.js';
import { State, StateMachine } from '../collector/StateMachine.js';
import { backoffMs, classifyError, recoveryRetryDelayMs } from '../collector/RetryManager.js';
import { log } from '../collector/Logger.js';

// These fixtures exercise the isolated, non-financial browser test path.
process.env.WINNER_NONFINANCIAL_TEST_MODE = 'true';

// Recovery fixtures share the collector logger singleton. Keep unit-test
// events out of the live collector log used for operational evidence.
for (const method of ['debug', 'info', 'warn', 'error', 'recovery']) log[method] = () => {};

test('legacy and current callers resolve one process-wide supervisor', () => {
  assert.equal(getBrowserSupervisor(false), getBrowserSupervisor(false));
  assert.ok(getBrowserSupervisor(false) instanceof AviatorPageManager);
});

test('SPRIBE Demo URL validation requires the official Demo origin and operator mode', () => {
  const accepted = validateSpribeDemoUrl('https://aviator-demo.spribegaming.com/?currency=USD&operator=demo');
  assert.equal(accepted.origin, 'https://aviator-demo.spribegaming.com');
  assert.throws(() => validateSpribeDemoUrl('https://winner.rw/en/virtual/crash-games/aviator?operator=demo'),
    /AVIATOR_DEMO_URL_NOT_ALLOWLISTED/);
  assert.throws(() => validateSpribeDemoUrl('https://aviator-demo.spribegaming.com/?operator=real'),
    /AVIATOR_DEMO_URL_NOT_ALLOWLISTED/);
});

test('SPRIBE Demo main document can serve as the read-only game frame', () => {
  const manager = new FrameManager();
  const frame = { isDetached: () => false };
  const demoPage = { url: () => 'https://aviator-demo.spribegaming.com/?operator=demo', mainFrame: () => frame };
  const livePage = { url: () => 'https://winner.rw/en/virtual/crash-games/aviator', mainFrame: () => frame };
  assert.equal(manager._frameIsChild(demoPage, frame), true);
  assert.equal(manager._frameIsChild(livePage, frame), false);
});

test('SPRIBE Demo BrowserManager reuses its main document as the game frame', async () => {
  const previous = process.env.AVIATOR_DEMO_URL;
  process.env.AVIATOR_DEMO_URL = 'https://aviator-demo.spribegaming.com/?operator=demo';
  try {
  const browser = new BrowserSupervisor(true);
  browser._demoMode = true;
  const frame = { url: () => 'https://aviator-demo.spribegaming.com/?operator=demo', isDetached: () => false };
  const page = { url: () => 'https://aviator-demo.spribegaming.com/?operator=demo',
    mainFrame: () => frame, frames: () => [frame] };
  browser.getAviatorPage = async () => page;
  assert.equal(await browser.getAviatorFrame(), frame);
  } finally {
    if (previous === undefined) delete process.env.AVIATOR_DEMO_URL;
    else process.env.AVIATOR_DEMO_URL = previous;
  }
});

test('SPRIBE Demo attaches to its existing page without navigating, creating tabs, or persisting session data', async () => {
  const previous = process.env.AVIATOR_DEMO_URL;
  process.env.AVIATOR_DEMO_URL = 'https://aviator-demo.spribegaming.com/?currency=USD&operator=demo';
  try {
    const browser = new BrowserSupervisor(true);
    const pageUrl = 'https://aviator-demo.spribegaming.com/?operator=demo';
    let stateReads = 0;
    let lockAcquired = 0;
    const page = {
      isClosed: () => false,
      url: () => pageUrl,
      on() {},
    };
    const context = { pages: () => [page], storageState: async () => { stateReads += 1; return {}; } };
    browser._browser = { isConnected: () => true, contexts: () => [context] };
    browser._context = context;
    browser._contextClosed = false;
    browser._acquireSingletonLock = () => { lockAcquired += 1; };
    browser._releaseSingletonLock = () => {};
    browser._targetIdForPage = async () => 'demo-target';
    browser._persistMetadata = () => {};

    assert.equal(await browser.attachSpribeDemoPage(), page);
    assert.equal(lockAcquired, 1);
    assert.equal(browser._aviatorTargetId, 'demo-target');
    await browser._persistSession();
    assert.equal(stateReads, 0);
    assert.deepEqual(context.pages(), [page]);
  } finally {
    if (previous === undefined) delete process.env.AVIATOR_DEMO_URL;
    else process.env.AVIATOR_DEMO_URL = previous;
  }
});

test('SPRIBE Demo attach refuses a second Aviator game page in the shared context', async () => {
  const previous = process.env.AVIATOR_DEMO_URL;
  process.env.AVIATOR_DEMO_URL = 'https://aviator-demo.spribegaming.com/?currency=USD&operator=demo';
  try {
    const browser = new BrowserSupervisor(true);
    const demoPage = { isClosed: () => false, url: () => 'https://aviator-demo.spribegaming.com/?operator=demo', on() {} };
    const winnerPage = { isClosed: () => false, url: () => 'https://winner.rw/en/virtual/crash-games/aviator', on() {} };
    const context = { pages: () => [demoPage, winnerPage] };
    browser._browser = { isConnected: () => true, contexts: () => [context] };
    browser._context = context;
    browser._contextClosed = false;
    browser._acquireSingletonLock = () => {};
    browser._releaseSingletonLock = () => {};

    await assert.rejects(browser.attachSpribeDemoPage(), /SPRIBE_AVIATOR_PAGE_COUNT_INVALID:2/);
    assert.deepEqual(context.pages(), [demoPage, winnerPage]);
  } finally {
    if (previous === undefined) delete process.env.AVIATOR_DEMO_URL;
    else process.env.AVIATOR_DEMO_URL = previous;
  }
});

test('history and betting accessors share one Aviator page and never provision another tab', async () => {
  const browser = new BrowserSupervisor(true);
  let newPages = 0;
  const page = {
    isClosed: () => false,
    url: () => 'https://winner.rw/en/virtual/crash-games/aviator',
    frames: () => [],
    on() {},
  };
  browser._browser = { isConnected: () => true };
  const context = { newPage: async () => { newPages += 1; throw new Error('must reuse the one game tab'); }, pages: () => [page] };
  browser._browser.contexts = () => [context];
  browser._context = context;
  browser._contextClosed = false;
  browser._restoreSessionStorage = async () => {};
  browser._targetIdForPage = async () => 'aviator-target';
  browser._trackPage = (_page, role) => assert.equal(role, 'shared_aviator');
  browser._cleanupManagedStarterPages = async () => {};
  browser._persistMetadata = () => {};

  const [aviator, history, betting] = await Promise.all([
    browser.getAviatorPage(), browser.getHistoryPage(), browser.getBettingPage(),
  ]);
  assert.equal(aviator, page);
  assert.equal(history, page);
  assert.equal(betting, page);
  assert.equal(browser._context, context);
  assert.deepEqual(context.pages(), [page]);
  assert.equal(newPages, 0);
  assert.equal(browser._aviatorTargetId, 'aviator-target');
});

test('duplicate session-ended Aviator tab is retired only after a healthy shared page is identified and transaction state is clear', async () => {
  const browser = new BrowserSupervisor(true);
  const closed = new Set();
  const makePage = (name, frameText) => ({
    name,
    isClosed: () => closed.has(name),
    url: () => 'https://winner.rw/en/virtual/crash-games/aviator',
    frames: () => [{ url: () => 'https://aviator-next.spribegaming.com/', evaluate: async () => ({
      text: frameText, panelCount: 2, panelStates: ['CLEAR', 'CLEAR'],
    }) }],
    on() {},
    close: async () => closed.add(name),
  });
  const healthyPage = makePage('healthy', 'Aviator game');
  const endedPage = makePage('ended', 'Session ended. The game was opened in another window.');
  const context = {
    pages: () => [healthyPage, endedPage].filter(page => !page.isClosed()),
    newPage: async () => { throw new Error('must not create another game tab'); },
  };
  browser._browser = { isConnected: () => true, contexts: () => [context] };
  browser._context = context;
  browser._contextClosed = false;
  browser._targetIdForPage = async page => `${page.name}-target`;
  browser._restoreSessionStorage = async () => {};
  browser._trackPage = (_page, role) => assert.equal(role, 'shared_aviator');
  browser._cleanupManagedStarterPages = async () => {};
  browser._persistMetadata = () => {};

  assert.equal(await browser.getAviatorPage(), healthyPage);
  assert.equal(browser._aviatorTargetId, 'healthy-target');
  assert.deepEqual(context.pages(), [healthyPage]);
  assert.equal(closed.has('ended'), true);
});

test('duplicate page cleanup is blocked when transaction state is pending or unknown', async () => {
  const browser = new BrowserSupervisor(true);
  const closed = new Set();
  const makePage = (name, frameText, pending) => ({
    name, isClosed: () => closed.has(name),
    url: () => 'https://winner.rw/en/virtual/crash-games/aviator',
    frames: () => [{ url: () => 'https://aviator-next.spribegaming.com/',
      evaluate: async () => ({ text: frameText, panelCount: 2, panelStates: [pending, 'CLEAR'] }) }],
    on() {}, close: async () => closed.add(name),
  });
  const healthy = makePage('healthy', 'Aviator', 'CLEAR');
  const duplicate = makePage('duplicate', 'Session ended. The game was opened in another window.', 'PENDING');
  const context = { pages: () => [healthy, duplicate].filter(page => !page.isClosed()) };
  browser._browser = { isConnected: () => true, contexts: () => [context] };
  browser._context = context;
  await assert.rejects(browser.getAviatorPage(), /DUPLICATE_AVIATOR_TRANSACTION_PENDING/);
  assert.equal(closed.has('duplicate'), false);
});

test('session-ended game is detected and recovery does not create a replacement session', async () => {
  const browser = new BrowserSupervisor(true);
  let newPages = 0;
  const frame = { url: () => 'https://aviator-next.spribegaming.com/',
    evaluate: async () => ({ text: 'Session ended. The game was opened in another window.', panelCount: 0, panelStates: [] }) };
  const page = {
    isClosed: () => false,
    url: () => 'https://winner.rw/en/virtual/crash-games/aviator',
    frames: () => [frame],
    on() {},
  };
  const context = { pages: () => [page], newPage: async () => { newPages += 1; throw new Error('must not create a second game session'); } };
  browser._browser = { isConnected: () => true, contexts: () => [context] };
  browser._context = context;
  browser._contextClosed = false;
  browser._aviatorPage = page;
  assert.equal((await browser.getGameSessionStatus()).status, 'SESSION_ENDED');
  await assert.rejects(browser.recoverHistoryPage(), /AVIATOR_SESSION_ENDED/);
  assert.equal(newPages, 0);
});

test('concurrent startup resolves one initial page and same shared game iframe', async () => {
  const browser = new BrowserSupervisor(true);
  let newPages = 0;
  const frame = { url: () => 'https://aviator-next.spribegaming.com/', evaluate: async () => ({
    text: 'Aviator', panelCount: 2, panelStates: ['CLEAR', 'CLEAR'],
  }) };
  const page = { isClosed: () => false, url: () => 'https://winner.rw/en/virtual/crash-games/aviator',
    frames: () => [frame], on() {} };
  const pages = [];
  const context = { pages: () => pages.filter(item => !item.isClosed()),
    newPage: async () => { newPages += 1; pages.push(page); return page; } };
  browser._browser = { isConnected: () => true, contexts: () => [context] };
  browser._context = context;
  browser._contextClosed = false;
  browser._ownsBrowser = true;
  browser._restoreSessionStorage = async () => {};
  browser._targetIdForPage = async () => 'shared-target';
  browser._trackPage = () => {};
  browser._cleanupManagedStarterPages = async () => {};
  browser._persistMetadata = () => {};
  const results = await Promise.all(Array.from({ length: 8 }, () => browser.getAviatorPage()));
  const sharedFrame = await browser.getAviatorFrame();
  assert.ok(results.every(item => item === page));
  assert.equal(sharedFrame, frame);
  assert.equal(newPages, 1);
  assert.equal(pages.length, 1);
});

test('collector health publishes one browser/context and one shared Aviator target', async () => {
  const { HealthMonitor } = await import('../collector/HealthMonitor.js');
  const health = new HealthMonitor();
  health.setBrowserPages({ browser_count: 1, context_count: 1, page_count: 1,
    aviator_page_count: 1, aviator_target_id: 'aviator-target', aviator_page_state: 'OPEN',
    game_frame_count: 1, game_frame_state: 'READY', page_role: 'SHARED_AVIATOR' });
  const pages = health.snapshot().browser_pages;
  assert.equal(pages.browser_count, 1);
  assert.equal(pages.context_count, 1);
  assert.equal(pages.page_count, 1);
  assert.equal(pages.aviator_page_count, 1);
  assert.equal(pages.history_target_id, pages.betting_target_id);
  assert.equal(pages.aviator_target_id, 'aviator-target');
  assert.equal(pages.betting_page_state, 'OPEN');
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

function recoveryFixture({ isLoggedIn = true, pageClosed = false, browserAlive = true,
  pageUrl = 'https://winner.rw/en/virtual/crash-games/aviator' } = {}) {
  const sm = new StateMachine();
  sm.transition(State.LOGIN, 'test');
  sm.transition(State.HOME, 'test');
  sm.transition(State.GAME_LOADING, 'test');
  sm.transition(State.WAITING_IFRAME, 'test');
  sm.transition(State.COLLECTING, 'test');
  let restarts = 0;
  let alive = browserAlive;
  let closed = pageClosed;
  const page = { isClosed: () => closed, url: () => pageUrl, reload: async () => assert.fail('page recovery should navigate through LoginManager') };
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
  let aviatorNavigations = 0;
  const login = { isLoggedIn: async () => isLoggedIn, goToAviator: async () => { aviatorNavigations += 1; } };
  const recovery = new RecoveryManager({ stateMachine: sm, browserManager: browser, loginManager: login, health });
  return { recovery, page, browser, health, login,
    get restarts() { return restarts; }, get aviatorNavigations() { return aviatorNavigations; } };
}

test('iframe recovery leaves a live page and browser running until a real round is verified', async () => {
  const fixture = recoveryFixture();
  assert.equal(fixture.recovery.frameWaitTimeoutMs(120000, 30000), 120000);
  const page = await fixture.recovery.recover(new Error('Frame detached'), fixture.page);
  assert.equal(page, fixture.page);
  assert.equal(fixture.restarts, 0);
  assert.equal(fixture.recovery._pendingVerification, true);
  assert.equal(fixture.recovery.frameWaitTimeoutMs(120000, 30000), 30000);
  await fixture.recovery.verifyRound({ round_id: 'verified-round' });
  assert.equal(fixture.recovery.frameWaitTimeoutMs(120000, 30000), 120000);
});

test('short retry is limited to frame scans while recovery awaits a persisted round', () => {
  assert.equal(backoffMs(100) <= 30000, true);
  assert.equal(recoveryRetryDelayMs(new Error('Timed out waiting for Aviator iframe with payouts'), 4, {
    recoveryPending: true,
    frameDelayMs: 750,
  }), 750);
  assert.equal(recoveryRetryDelayMs(new Error('Frame was detached'), 4, {
    recoveryPending: false,
    frameDelayMs: 750,
  }) >= 20000, true);
  const ordinaryDelay = recoveryRetryDelayMs(new Error('browser disconnected'), 2, {
    recoveryPending: true,
    frameDelayMs: 750,
  });
  assert.equal(ordinaryDelay >= 5000 && ordinaryDelay <= 6000, true);
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

test('authenticated wrong page navigates to Aviator in the same browser context', async () => {
  const fixture = recoveryFixture({ pageUrl: 'https://winner.rw/en/sportsbook/upcoming' });
  await fixture.recovery.recover(new Error('page navigated away'), fixture.page);
  assert.equal(fixture.aviatorNavigations, 1);
  assert.equal(fixture.restarts, 0);
});

test('outer-page heartbeat timeout does not trigger recovery while collecting', async () => {
  const sm = new StateMachine();
  sm.transition(State.LOGIN, 'test');
  sm.transition(State.HOME, 'test');
  sm.transition(State.GAME_LOADING, 'test');
  sm.transition(State.WAITING_IFRAME, 'test');
  sm.transition(State.COLLECTING, 'test');
  let alerts = 0;
  const health = {
    snapshot: () => ({ network: { status: 'ONLINE' }, frameConnected: true }),
    setBrowserConnected() {},
    setPageConnected() {},
  };
  const watchdog = new Watchdog({
    stateMachine: sm,
    browserManager: { isAlive: () => true, isPageResponsive: () => false },
    loginManager: { isLoggedIn: async () => true },
    health,
    onUnhealthy: async () => { alerts += 1; },
  });
  watchdog.setPage({ isClosed: () => false, url: () => 'https://winner.rw/en/virtual/crash-games/aviator' });
  watchdog._lastLoginCheck = Date.now();

  await watchdog._check();

  assert.equal(alerts, 0, 'outer-page probe failure must not reload a live Aviator frame');
});

test('login route navigation overrides stale auth while recovery is active', async () => {
  const sm = new StateMachine();
  sm.transition(State.RECOVERING, 'test-recovery');
  const listeners = {};
  let currentUrl = 'https://winner.rw/en/virtual/crash-games/aviator';
  const mainFrame = { url: () => currentUrl };
  const page = {
    url: () => currentUrl,
    isClosed: () => false,
    mainFrame: () => mainFrame,
    on: (event, callback) => { listeners[event] = callback; },
  };
  const healthChanges = [];
  const health = {
    setLoggedIn: value => healthChanges.push(['loggedIn', value]),
    setAuthRequired: value => healthChanges.push(['authRequired', value]),
    setRecovery: (stage, evidence) => healthChanges.push(['recovery', stage, evidence.reasonCode]),
    setBrowserConnected() {},
    setPageConnected() {},
  };
  const alerts = [];
  const watchdog = new Watchdog({
    stateMachine: sm,
    browserManager: { isAlive: () => true },
    loginManager: { isLoggedIn: async () => true },
    health,
    onUnhealthy: async reason => alerts.push(reason),
  });
  watchdog.setPage(page);

  currentUrl = 'https://winner.rw/en/authentication/login?returnUrl=%2Faviator';
  listeners.framenavigated(mainFrame);
  await new Promise(resolve => setImmediate(resolve));

  assert.ok(healthChanges.some(([key, value]) => key === 'loggedIn' && value === false));
  assert.ok(healthChanges.some(([key, value]) => key === 'recovery' && value === 'AUTH_REQUIRED'));
  assert.deepEqual(alerts, ['AUTH_REQUIRED: login route visible on Aviator page']);
});

test('shared Aviator login route marks authentication required without opening another page', async () => {
  const sm = new StateMachine();
  sm.transition(State.RECOVERING, 'test-recovery');
  const aviatorPage = { url: () => 'https://winner.rw/en/authentication/login', isClosed: () => false };
  const healthChanges = [];
  const alerts = [];
  const watchdog = new Watchdog({
    stateMachine: sm,
    browserManager: { isAlive: () => true },
    loginManager: { isLoggedIn: async () => true },
    health: {
      setLoggedIn: value => healthChanges.push(['loggedIn', value]),
      setAuthRequired: value => healthChanges.push(['authRequired', value]),
      setRecovery: (stage, evidence) => healthChanges.push(['recovery', stage, evidence]),
      setBrowserConnected() {},
      setPageConnected() {},
    },
    onUnhealthy: async reason => alerts.push(reason),
  });
  watchdog.setPage(aviatorPage);
  await new Promise(resolve => setImmediate(resolve));

  assert.ok(healthChanges.some(([key, value]) => key === 'loggedIn' && value === false));
  assert.ok(healthChanges.some(([key, value]) => key === 'recovery' && value === 'AUTH_REQUIRED'));
  assert.deepEqual(alerts, ['AUTH_REQUIRED: login route visible on Aviator page']);
  assert.equal(aviatorPage.url(), 'https://winner.rw/en/authentication/login');
});

test('legacy page-unresponsive recovery preserves the active Aviator tab', async () => {
  const fixture = recoveryFixture();
  const page = await fixture.recovery.recover(new Error('page unresponsive'), fixture.page);
  assert.equal(page, fixture.page);
  assert.equal(fixture.restarts, 0);
});

test('browser restart is reached only after disconnection is confirmed', async () => {
  const fixture = recoveryFixture({ browserAlive: false });
  assert.equal(await fixture.recovery.recover(new Error('browser disconnected'), fixture.page), fixture.page);
  assert.equal(fixture.restarts, 1);
});

test('verified expiry runs LoginManager and leaves the existing Aviator route in place', async () => {
  const fixture = recoveryFixture();
  let loginAttempts = 0;
  let routeAttempts = 0;
  fixture.login.inspectSession = async () => ({ status: 'EXPIRED', reasonCode: 'AUTH_REDIRECT' });
  fixture.login.ensureLoggedIn = async () => { loginAttempts += 1; return true; };
  fixture.login.openAviatorRoute = async () => { routeAttempts += 1; return true; };

  await fixture.recovery.recover(Object.assign(new Error('frame absent'), { code: 'GAME_FRAME_MISSING' }), fixture.page);

  assert.equal(loginAttempts, 1);
  assert.equal(routeAttempts, 0, 'same-page recovery must not reopen the game route');
});

test('authenticated recovery uses only the shared Aviator page', async () => {
  const fixture = recoveryFixture();
  let loginAttempts = 0;
  let routeAttempts = 0;
  fixture.login.inspectSession = async _page => {
    return { status: 'EXPIRED', reasonCode: 'RELATED_LOGIN_ROUTE_VISIBLE' };
  };
  fixture.login.ensureLoggedIn = async (_page, _signal) => {
    loginAttempts += 1;
    return true;
  };
  fixture.login.openAviatorRoute = async () => { routeAttempts += 1; return true; };

  await fixture.recovery.recover(Object.assign(new Error('frame absent'), { code: 'GAME_FRAME_MISSING' }), fixture.page);

  assert.equal(loginAttempts, 1);
  assert.equal(routeAttempts, 0);
});

test('missing frame with UNKNOWN session does not trigger login or navigation', async () => {
  const fixture = recoveryFixture();
  fixture.login.inspectSession = async () => ({ status: 'UNKNOWN', reasonCode: 'NO_SESSION_SIGNAL' });
  fixture.login.ensureLoggedIn = async () => assert.fail('unknown session must not trigger login');
  fixture.login.openAviatorRoute = async () => assert.fail('unknown session must not trigger navigation');

  await assert.rejects(
    fixture.recovery.recover(Object.assign(new Error('frame absent'), { code: 'GAME_FRAME_MISSING' }), fixture.page),
    error => error.code === 'SESSION_STATUS_UNKNOWN',
  );
});

test('game-provider outage does not repeat login or navigate the authenticated page', async () => {
  const fixture = recoveryFixture();
  fixture.health.snapshot = () => ({ network: { status: 'GAME_PROVIDER_UNAVAILABLE' } });
  fixture.login.inspectSession = async () => ({ status: 'AUTHENTICATED' });
  fixture.login.ensureLoggedIn = async () => assert.fail('valid login must not be repeated for a provider outage');
  fixture.login.openAviatorRoute = async () => assert.fail('provider outage must not trigger route loops');

  await assert.rejects(
    fixture.recovery.recover(Object.assign(new Error('frame absent'), { code: 'GAME_FRAME_MISSING' }), fixture.page),
    error => error.code === 'GAME_PROVIDER_UNAVAILABLE',
  );
  assert.equal(fixture.restarts, 0);
});

test('browser restart waits for unresolved execution reconciliation', async () => {
  const fixture = recoveryFixture({ browserAlive: false });
  fixture.recovery.executionRecoveryCheck = async () => ({ safe: false, status: 'RECONCILIATION_REQUIRED' });

  await assert.rejects(
    fixture.recovery.recover(new Error('browser disconnected'), fixture.page),
    error => error.code === 'EXECUTION_RECONCILIATION_REQUIRED',
  );
  assert.equal(fixture.restarts, 0);
});

test('storage failures never invoke browser or page recovery', async () => {
  const fixture = recoveryFixture({ browserAlive: false });
  await assert.rejects(fixture.recovery.recover(new Error('PostgreSQL database unavailable'), fixture.page));
  assert.equal(fixture.restarts, 0);
  assert.equal(fixture.aviatorNavigations, 0);
});

test('shutdown preservation leaves the shared Aviator page open', async () => {
  const browser = new BrowserSupervisor(true);
  let closedPages = 0;
  browser._browser = { isConnected: () => true };
  browser._context = { pages: () => [] };
  browser._historyPage = { isClosed: () => false, close: async () => { closedPages += 1; } };
  browser._aviatorPage = browser._historyPage;
  browser._bettingPage = browser._aviatorPage;
  browser._ownsBrowser = true;
  browser._persistSession = async () => {};
  browser._persistMetadata = () => {};
  browser._releaseSingletonLock = () => {};
  browser._event = () => {};

  await browser.preserveOpenContext();

  assert.equal(closedPages, 0);
  assert.equal(browser._ownsBrowser, false);
});
