import test from 'node:test';
import assert from 'node:assert/strict';
import { LoginManager } from '../collector/LoginManager.js';

function pageFixture(initialUrl = 'about:blank', visibleSelectors = new Set(), bodyText = '') {
  let currentUrl = initialUrl;
  const page = {
    isClosed: () => false,
    url: () => currentUrl,
    goto: async url => { currentUrl = url; },
    waitForSelector: async () => {},
    frames: () => [],
    locator(selector) {
      return {
        first() { return this; },
        count: async () => selector === 'body' ? 1 : 0,
        isVisible: async () => visibleSelectors.has(selector),
        innerText: async () => bodyText,
      };
    },
  };
  return { page, currentUrl: () => currentUrl };
}

test('expired login route overrides stale account-shell evidence', async () => {
  const fixture = pageFixture('https://winner.rw/en/authentication/login', new Set(['.au-m-nav-u.login-area.usr-lgd-in']));
  const login = new LoginManager({ phone: '', password: '' });
  assert.equal(await login.isLoggedIn(fixture.page), false);
});

test('related login route overrides a stale authenticated shell in the shared context', async () => {
  const history = pageFixture('https://winner.rw/page404');
  const betting = pageFixture('https://winner.rw/en/authentication/login');
  const authenticatedHistory = pageFixture('https://winner.rw/en/virtual/crash-games/aviator',
    new Set(['.au-m-nav-u.login-area.usr-lgd-in']));
  const login = new LoginManager({ phone: '', password: '' });

  const unresolved = await login.inspectSession(history.page, { relatedPages: [betting.page] });
  const authenticated = await login.inspectSession(authenticatedHistory.page, { relatedPages: [betting.page] });

  assert.equal(unresolved.status, 'EXPIRED');
  assert.equal(unresolved.reasonCode, 'RELATED_LOGIN_ROUTE_VISIBLE');
  assert.equal(authenticated.status, 'EXPIRED');
  assert.equal(authenticated.evidence.relatedRoute, 'https://winner.rw/en/authentication/login');
});

test('authentication verification does not navigate either shared page to login', async () => {
  const history = pageFixture('https://winner.rw/en/virtual/crash-games/aviator',
    new Set(['.au-m-nav-u.login-area.usr-lgd-in']));
  const betting = pageFixture('https://winner.rw/en/authentication/login');
  const login = new LoginManager({ phone: '', password: '' });

  assert.equal(await login.ensureLoggedIn(history.page, undefined, { relatedPages: [betting.page] }), true);

  assert.match(history.currentUrl(), /aviator/);
  assert.match(betting.currentUrl(), /authentication\/login/);
});

test('verified session acknowledges an unchanged related login tab until it navigates again', async () => {
  const history = pageFixture('https://winner.rw/en/virtual/crash-games/aviator',
    new Set(['.au-m-nav-u.login-area.usr-lgd-in']));
  const betting = pageFixture('https://winner.rw/en/authentication/login');
  const navigateHistory = history.page.goto.bind(history.page);
  history.page.goto = async (url, options) => navigateHistory(
    /authentication\/login/.test(url) ? 'https://winner.rw/en/virtual/crash-games/aviator' : url,
    options,
  );
  const bettingMainFrame = {};
  const listeners = {};
  betting.page.mainFrame = () => bettingMainFrame;
  betting.page.on = (event, callback) => { listeners[event] = callback; };
  const login = new LoginManager({ phone: '', password: '' });

  assert.equal(await login.ensureLoggedIn(history.page, undefined, { relatedPages: [betting.page] }), true);
  assert.equal((await login.inspectSession(history.page, { relatedPages: [betting.page] })).status, 'AUTHENTICATED');

  listeners.framenavigated(bettingMainFrame);
  assert.equal((await login.inspectSession(history.page, { relatedPages: [betting.page] })).reasonCode, 'RELATED_LOGIN_ROUTE_VISIBLE');
});

test('game iframe alone does not authenticate a session', async () => {
  const fixture = pageFixture('https://winner.rw/en/virtual/crash-games/aviator',
    new Set(['iframe[src*="spribe"], iframe[src*="aviator"], iframe[src*="crash"]']));
  const login = new LoginManager({ phone: '', password: '' });
  assert.equal(await login.isLoggedIn(fixture.page), false);
});

test('missing game frame with an authenticated account shell is not treated as logout', async () => {
  const fixture = pageFixture('https://winner.rw/en/virtual/crash-games/aviator', new Set(['.au-m-nav-u.login-area.usr-lgd-in']));
  const login = new LoginManager({ phone: '', password: '' });
  assert.deepEqual((await login.inspectSession(fixture.page)).status, 'AUTHENTICATED');
  assert.equal(await login.isExplicitlyLoggedOut(fixture.page), false);
});

test('authenticated Betting page verifies the shared History session when its shell is absent', async () => {
  const history = pageFixture('https://winner.rw/en/virtual/crash-games/aviator');
  const betting = pageFixture('https://winner.rw/en/sportsbook', new Set(['.au-m-nav-u.login-area.usr-lgd-in']));
  const login = new LoginManager({ phone: '', password: '' });

  const session = await login.inspectSession(history.page, { relatedPages: [betting.page] });

  assert.equal(session.status, 'AUTHENTICATED');
  assert.equal(session.reasonCode, 'SHARED_CONTEXT_AUTHENTICATED_SESSION');
  assert.equal(session.evidence.relatedRoute, 'https://winner.rw/en/sportsbook');
});

test('inactivity expiry and CAPTCHA are distinct verified session states', async () => {
  const expired = pageFixture('https://winner.rw/en/virtual/crash-games/aviator', new Set(), 'Your session has expired due to inactivity. Please log in again.');
  const captcha = pageFixture('https://winner.rw/en/authentication/login', new Set(), 'Verify you are human before continuing.');
  const login = new LoginManager({ phone: '', password: '' });

  assert.equal((await login.inspectSession(expired.page)).reasonCode, 'SESSION_EXPIRED_MESSAGE');
  assert.equal((await login.inspectSession(captcha.page)).status, 'MANUAL_VERIFICATION');
  assert.equal((await login.inspectSession(captcha.page)).reasonCode, 'HUMAN_VERIFICATION_REQUIRED');
});

test('manual verification message is not classified as a fresh logout signal', async () => {
  const fixture = pageFixture('https://winner.rw/en/authentication/challenge', new Set(), 'Verify you are human before continuing.');
  const login = new LoginManager({ phone: '', password: '' });
  const state = await login.inspectSession(fixture.page);
  assert.equal(state.status, 'MANUAL_VERIFICATION');
  assert.equal(state.reasonCode, 'HUMAN_VERIFICATION_REQUIRED');
});

test('expired session leaves the existing page untouched for manual sign-in', async () => {
  const fixture = pageFixture();
  const login = new LoginManager({ phone: '', password: '' });
  await assert.rejects(login.ensureLoggedIn(fixture.page), error => error.code === 'AUTH_REQUIRED');
  assert.equal(fixture.currentUrl(), 'about:blank');
  assert.equal(login.hasAutomaticCredentials(), false);
});
