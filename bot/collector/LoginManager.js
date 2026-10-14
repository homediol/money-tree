/**
 * LoginManager.js — Session detection, login, and logout recovery.
 * Never opens a second browser. Works with the existing page.
 */

import path from 'path';
import { fileURLToPath } from 'url';
import { log } from './Logger.js';
import { withRetry, sleep, classifyError } from './RetryManager.js';
import { inspectGameFrameSession } from './GameFrameAuth.js';
import { clickInCurrentTab } from './SameTabNavigation.js';

const __dirname = path.dirname(fileURLToPath(import.meta.url));

const HOME_URL    = 'https://winner.rw';
const LOGIN_URL   = 'https://winner.rw/en/authentication/login';
const DEFAULT_AVIATOR_URLS = [
  'https://winner.rw/en/virtual/crash-games/aviator',
  'https://winner.rw/en/games/aviator',
];
const acknowledgedAuthRoutes = new WeakMap();
const trackedAuthRoutePages = new WeakSet();

function aviatorUrls() {
  const configured = process.env.WINNER_AVIATOR_URL;
  if (!configured) return DEFAULT_AVIATOR_URLS;
  try {
    const url = new URL(configured);
    if (url.hostname !== 'winner.rw' || !url.pathname.startsWith('/en/')) {
      throw new Error('WINNER_AVIATOR_URL must be an /en/ route on winner.rw');
    }
    url.search = '';
    url.hash = '';
    const primary = url.toString();
    return [primary, ...DEFAULT_AVIATOR_URLS.filter(candidate => candidate !== primary)];
  } catch (error) {
    throw new Error(`Invalid configured Aviator route: ${error.message}`);
  }
}

function isLoginRoute(url) {
  try {
    return /\/(?:authentication\/login|login)(?:\/|$)/i.test(new URL(url).pathname);
  } catch (error) {
    log.debug(`Could not parse page route during login detection: ${error.message}`);
    return false;
  }
}

export function isExpiredAuthRoute(url) {
  try {
    const parsed = new URL(url);
    if (!/(^|\.)winner\.rw$/i.test(parsed.hostname)) return false;
    const pathname = parsed.pathname;
    return /\/(?:authentication\/login|login)(?:\/|$)/i.test(pathname)
      || /\/logout(?:\/|$)/i.test(pathname);
  } catch (error) {
    log.debug(`Could not parse route during expired-session detection: ${error.message}`);
    return false;
  }
}

function winnerRouteKey(rawUrl) {
  try {
    const parsed = new URL(rawUrl);
    if (!/(^|\.)winner\.rw$/i.test(parsed.hostname)) return null;
    return `${parsed.origin}${parsed.pathname}`.toLowerCase();
  } catch (error) {
    log.debug(`Could not parse Winner route: ${error.message}`);
    return null;
  }
}

export function isAviatorRoute(rawUrl) {
  try {
    const parsed = new URL(rawUrl);
    if (!/(^|\.)winner\.rw$/i.test(parsed.hostname) || isExpiredAuthRoute(parsed.href)) return false;
    return /\/(?:aviator|crash-games|crash)(?:\/|$)/i.test(parsed.pathname);
  } catch (error) {
    log.debug(`Could not parse Aviator route: ${error.message}`);
    return false;
  }
}

export function isAcknowledgedAuthRoute(page) {
  try {
    const url = page?.url?.() || '';
    return isExpiredAuthRoute(url) && acknowledgedAuthRoutes.get(page) === winnerRouteKey(url);
  } catch (error) {
    log.debug(`Could not check acknowledged login route: ${error.message}`);
    return false;
  }
}

function acknowledgeAuthRoute(page) {
  try {
    const url = page?.url?.() || '';
    if (!page || !isExpiredAuthRoute(url)) return;
    acknowledgedAuthRoutes.set(page, winnerRouteKey(url));
    if (trackedAuthRoutePages.has(page) || typeof page.on !== 'function') return;
    trackedAuthRoutePages.add(page);
    page.on('framenavigated', frame => {
      try {
        if (frame === page.mainFrame?.()) acknowledgedAuthRoutes.delete(page);
      } catch (error) {
        log.debug(`Acknowledged login-route navigation could not be checked: ${error.message}`);
      }
    });
  } catch (error) {
    log.debug(`Could not acknowledge related login route: ${error.message}`);
  }
}

function isUnstableLocatorError(error) {
  return /detached|not attached|not stable|element is not stable|timeout/i.test(String(error?.message || error));
}

function safeUrl(raw) {
  try { const url = new URL(raw); return `${url.origin}${url.pathname}`; }
  catch (error) { log.debug(`Could not sanitize browser URL for logs: ${error.message}`); return 'UNKNOWN_URL'; }
}

function networkNavigationError(error, url) {
  if (classifyError(error) !== 'NETWORK_RECOVER') return null;
  return Object.assign(new Error(`NETWORK_UNAVAILABLE: Winner navigation failed (${safeUrl(url)}): ${String(error?.message || error).replace(/https?:\/\/[^\s?]+\?[^\s]+/g, '[URL_REDACTED]')}`), {
    code: 'NETWORK_UNAVAILABLE',
    reasonCode: 'WINNER_NAVIGATION_NETWORK_FAILURE',
    evidence: { route: safeUrl(url), errorCode: error?.code || null },
    cause: error,
  });
}

// Selectors that indicate the user is logged in.
// These are checked IN THE CURRENT PAGE without navigating away.
const SESSION_SIGNALS = [
  // Check Winner's current account shell first; the older selectors below
  // incur individual timeouts on the sportsbook's present DOM.
  '.au-m-nav-u.login-area.usr-lgd-in', 'a.usr-balance',
  '.user-profile', '.account-dropdown', '.user-avatar',
  '.header__balance', '.header__wrap-balance',
  'a[href*="logout"]', '[data-testid="user-balance"]',
  // Current Winner navigation marks the authenticated account container with
  // these classes and exposes its balance through .usr-balance. Older logout
  // and header-balance selectors are absent on the live sportsbook shell.
];

// Selectors that indicate the user is logged out / on login page
const LOGOUT_SIGNALS = [
  '#user-menu-login', 'a.login-btn', '#phoneInput',
  '[data-testid="login-button"]',
];

export class AuthRequiredError extends Error {
  constructor(message = 'Winner requires an authenticated human session') {
    super(message);
    this.name = 'AuthRequiredError';
    this.code = 'AUTH_REQUIRED';
  }
}

async function isAccessDeniedPage(page) {
  try {
    const bodyText = (await page.locator('body').innerText({ timeout: 1000 })).toLowerCase();
    return bodyText.includes('accessdenied') || bodyText.includes('access denied');
  } catch (error) {
    log.debug(`Access-denied page evidence was unavailable: ${error.message}`);
    return false;
  }
}

export class LoginManager {
  constructor(credentials) {
    this.phone    = credentials.phone;
    this.password = credentials.password;
    this.loginCount = 0;
  }

  /** Navigate safely, trying multiple waitUntil strategies. */
  async _navigate(page, url, signal, { timeoutMs = 15000 } = {}) {
    // Winner keeps chat/analytics requests alive, so the browser's full
    // ``load`` event is not a reliable readiness boundary.
    for (const waitUntil of ['commit', 'domcontentloaded']) {
      if (signal?.aborted) return false;
      try {
        await page.goto(url, { waitUntil, timeout: timeoutMs });
        await page.waitForSelector('body', { timeout: 5000 })
          .catch(error => log.debug(`Page body is still loading after route navigation: ${error.message}`));
        if (await isAccessDeniedPage(page)) {
          log.warn(`Navigation to ${safeUrl(url)} reached Access Denied`);
          return false;
        }
        return true;
      } catch (err) {
        const networkError = networkNavigationError(err, url);
        if (networkError) {
          log.warn(`LoginManager: network navigation failure reason=${networkError.reasonCode} route=${safeUrl(url)} evidence=${JSON.stringify(networkError.evidence)}`);
          throw networkError;
        }
        log.warn(`Navigation to ${safeUrl(url)} failed (${waitUntil}): ${String(err.message).replace(/https?:\/\/[^\s?]+\?[^\s]+/g, '[URL_REDACTED]')}`);
        // Winner may keep analytics/chat requests open indefinitely. A load
        // timeout is harmless once the requested same-origin route has a body.
        try {
          const requested = new URL(url);
          const current = new URL(page.url());
          const routeMatches = requested.pathname === '/' ||
            current.pathname.replace(/\/$/, '') === requested.pathname.replace(/\/$/, '');
          const bodyReady = await page.locator('body').count().catch(error => {
            log.debug(`Page body count failed after a navigation timeout: ${error.message}`);
            return 0;
          });
          if (current.origin === requested.origin && routeMatches && bodyReady &&
              !await isAccessDeniedPage(page)) {
            log.info(`Navigation route rendered despite ${waitUntil} timeout: ${safeUrl(page.url())}`);
            return true;
          }
        } catch (error) { log.debug(`Navigation evidence check was unavailable: ${error.message}`); }
      }
    }
    return false;
  }

  /**
   * Check if the current page shows a logged-in session.
   * IMPORTANT: Does NOT navigate away — checks the current page only.
   * Require an authenticated account shell or authenticated game-frame
   * session; the outer game iframe alone is not authentication proof.
   */
  async _legacyIsLoggedIn(page) {
    try {
      if (!page || page.isClosed()) return false;

      const url = page.url().toLowerCase();
      if (await isAccessDeniedPage(page)) return false;

      // On login/auth page = definitely not logged in
      // Winner's session expiry opens /user/logout/popup inside the shell.
      // That page can still expose the global logout link, so route evidence
      // must take precedence over broad "session is present" selectors.
      if (isExpiredAuthRoute(url)) return false;

      // Use the embedded game's own session state before falling back to
      // Winner's outer account shell.
      if (isAviatorRoute(url)) {
        const gameSession = await inspectGameFrameSession(page);
        if (gameSession.status === 'AUTHENTICATED') return true;
        if (gameSession.frameFound) return false;
      }

      // Check session signals in current DOM (no navigation)
      for (const sel of SESSION_SIGNALS) {
        try {
          if (await page.locator(sel).first().isVisible({ timeout: 800 })) return true;
        } catch (error) { log.debug(`Session selector ${sel} was unavailable: ${error.message}`); }
      }

      // Check logout signals — if visible, definitely not logged in
      for (const sel of LOGOUT_SIGNALS) {
        try {
          if (await page.locator(sel).first().isVisible({ timeout: 400 })) return false;
        } catch (error) { log.debug(`Login selector ${sel} was unavailable: ${error.message}`); }
      }

      return false;
    } catch (error) {
      log.debug(`Login session evidence check failed: ${error.message}`);
      return false;
    }
  }

  /** Return true only when the current page explicitly proves a logged-out state. */
  async _legacyIsExplicitlyLoggedOut(page) {
    try {
      if (!page || page.isClosed()) return false;
      const url = page.url().toLowerCase();
      if (isLoginRoute(url) || url.includes('/logout')) return true;
      for (const selector of LOGOUT_SIGNALS) {
        try {
          if (await page.locator(selector).first().isVisible({ timeout: 400 })) return true;
        } catch (error) { log.debug(`Logout selector ${selector} was unavailable: ${error.message}`); }
      }
      return false;
    } catch (error) {
      log.debug(`Explicit logout evidence check failed: ${error.message}`);
      return false;
    }
  }

  async inspectSession(page, { relatedPages = [] } = {}) {
    try {
      if (!page || page.isClosed()) {
        return { status: 'UNKNOWN', reasonCode: 'PAGE_UNAVAILABLE', evidence: { pageAvailable: false } };
      }
      const url = page.url().toLowerCase();
      const route = safeUrl(url);
      if (isExpiredAuthRoute(url)) {
        const routeText = await page.locator('body').innerText({ timeout: 1000 }).catch(error => {
          log.debug(`Authentication-route verification text was unavailable: ${error.message}`);
          return '';
        });
        if (/verify you are human|checking your browser|captcha|two[- ]factor|security code/i.test(routeText)) {
          return { status: 'MANUAL_VERIFICATION', reasonCode: 'HUMAN_VERIFICATION_REQUIRED', evidence: { route } };
        }
        return { status: 'EXPIRED', reasonCode: isLoginRoute(url) ? 'AUTH_REDIRECT' : 'LOGOUT_ROUTE', evidence: { route } };
      }
      const relatedLoginPage = relatedPages.find(relatedPage => {
        try {
          return relatedPage && !relatedPage.isClosed?.()
            && isExpiredAuthRoute(relatedPage.url?.() || '')
            && !isAcknowledgedAuthRoute(relatedPage);
        }
        catch (error) { log.debug(`Related page login-route evidence was unavailable: ${error.message}`); return false; }
      });
      if (relatedLoginPage) {
        const relatedRoute = safeUrl(relatedLoginPage.url());
        const relatedText = await relatedLoginPage.locator('body').innerText({ timeout: 1000 }).catch(error => {
          log.debug(`Related authentication-route verification text was unavailable: ${error.message}`);
          return '';
        });
        if (/verify you are human|checking your browser|captcha|two[- ]factor|security code/i.test(relatedText)) {
          return { status: 'MANUAL_VERIFICATION', reasonCode: 'HUMAN_VERIFICATION_REQUIRED', evidence: { route, relatedRoute } };
        }
        return { status: 'EXPIRED', reasonCode: 'RELATED_LOGIN_ROUTE_VISIBLE', evidence: { route, relatedRoute } };
      }
      if (await isAccessDeniedPage(page)) {
        return { status: 'UNKNOWN', reasonCode: 'ACCESS_DENIED_PAGE', evidence: { route } };
      }
      const bodyText = await page.locator('body').innerText({ timeout: 1000 }).catch(error => {
        log.debug(`Session page text evidence was unavailable: ${error.message}`);
        return '';
      });
      if (/verify you are human|checking your browser|captcha|two[- ]factor|security code/i.test(bodyText)) {
        return { status: 'MANUAL_VERIFICATION', reasonCode: 'HUMAN_VERIFICATION_REQUIRED', evidence: { route } };
      }
      if (/session (?:has )?expired|session timed out|inactivity (?:timeout|logout)|you have been logged out|please log in again/i.test(bodyText)) {
        return { status: 'EXPIRED', reasonCode: 'SESSION_EXPIRED_MESSAGE', evidence: { route } };
      }
      const gameSession = await inspectGameFrameSession(page);
      if (gameSession.status === 'EXPIRED') {
        return { status: 'EXPIRED', reasonCode: gameSession.reasonCode, evidence: { route, gameFrame: 'login-required' } };
      }
      if (gameSession.status === 'AUTHENTICATED') {
        return { status: 'AUTHENTICATED', reasonCode: 'AUTHENTICATED_GAME_SESSION', evidence: { route, gameFrame: 'spribegaming-session' } };
      }
      // Once a Spribe iframe is attached, the Winner parent shell cannot
      // override its unknown auth state. Keep this UNKNOWN so read-only
      // collection can use its separate payout-readiness gate.
      if (gameSession.frameFound) {
        return { status: 'UNKNOWN', reasonCode: gameSession.reasonCode, evidence: { route, gameFrame: 'session-unverified' } };
      }
      for (const selector of SESSION_SIGNALS) {
        try {
          if (await page.locator(selector).first().isVisible({ timeout: 400 })) {
            return { status: 'AUTHENTICATED', reasonCode: 'AUTHENTICATED_ACCOUNT_SHELL', evidence: { route, selector } };
          }
        } catch (error) { log.debug(`Session selector ${selector} was unavailable: ${error.message}`); }
      }
      for (const selector of ['#phoneInput', 'input[type="password"]', ...LOGOUT_SIGNALS]) {
        try {
          if (await page.locator(selector).first().isVisible({ timeout: 300 })) {
            return { status: 'EXPIRED', reasonCode: 'LOGIN_FORM_VISIBLE', evidence: { route, selector } };
          }
        } catch (error) { log.debug(`Login selector ${selector} was unavailable: ${error.message}`); }
      }
      for (const relatedPage of relatedPages) {
        try {
          if (!relatedPage || relatedPage === page || relatedPage.isClosed?.()) continue;
          const relatedUrl = relatedPage.url?.() || '';
          if (isExpiredAuthRoute(relatedUrl)) continue;
          const relatedSession = await this.inspectSession(relatedPage);
          if (relatedSession.status === 'AUTHENTICATED') {
            return {
              status: 'AUTHENTICATED',
              reasonCode: 'SHARED_CONTEXT_AUTHENTICATED_SESSION',
              evidence: {
                route,
                relatedRoute: safeUrl(relatedUrl),
                relatedReasonCode: relatedSession.reasonCode,
              },
            };
          }
          if (relatedSession.status === 'MANUAL_VERIFICATION') {
            return {
              status: 'MANUAL_VERIFICATION',
              reasonCode: relatedSession.reasonCode,
              evidence: { route, relatedRoute: safeUrl(relatedUrl) },
            };
          }
        } catch (error) {
          log.debug(`Related page session evidence was unavailable: ${error.message}`);
        }
      }
      return { status: 'UNKNOWN', reasonCode: 'NO_SESSION_SIGNAL', evidence: { route } };
    } catch (error) {
      log.debug(`Login session evidence check failed: ${error.message}`);
      return { status: 'UNKNOWN', reasonCode: 'SESSION_PROBE_FAILED', evidence: { errorCode: error?.code || null } };
    }
  }

  async isLoggedIn(page) {
    return (await this.inspectSession(page)).status === 'AUTHENTICATED';
  }

  async isExplicitlyLoggedOut(page) {
    return (await this.inspectSession(page)).status === 'EXPIRED';
  }

  async _requireNoAuthRedirect(page) {
    const url = page?.url?.() || '';
    if (!isExpiredAuthRoute(url)) return;
    const session = await this.inspectSession(page);
    const error = new AuthRequiredError('Winner redirected to login or logout while opening Aviator');
    error.reasonCode = session.reasonCode || 'AUTH_REDIRECT';
    error.evidence = session.evidence || { route: safeUrl(url) };
    throw error;
  }

  hasAutomaticCredentials() {
    return Boolean(this.phone && this.password);
  }

  /**
   * Ensure the user is logged in.
   * If already logged in, returns true immediately.
   * Otherwise navigates to login and submits credentials.
   */
  async ensureLoggedIn(page, signal, { relatedPages = [] } = {}) {
    return withRetry(async (attempt) => {
      if (signal?.aborted) throw new Error('Aborted');

      const relatedLoginRoute = relatedPages.some(relatedPage => {
        try {
          return relatedPage && !relatedPage.isClosed?.()
            && isExpiredAuthRoute(relatedPage.url?.() || '')
            && !isAcknowledgedAuthRoute(relatedPage);
        }
        catch (error) { log.debug(`Related page login-route evidence was unavailable during login: ${error.message}`); return false; }
      });

      // A restored Aviator tab may already have a valid session. Keep that
      // page in place instead of bouncing through the sportsbook on recovery.
      if (await this.isLoggedIn(page) && !relatedLoginRoute) {
        for (const relatedPage of relatedPages) acknowledgeAuthRoute(relatedPage);
        log.info('LoginManager: session valid on current page');
        return true;
      }

      // A login route in another page of the shared context is explicit
      // expiry evidence. Verify it through the existing History page; never
      // navigate the separate Betting page, which may protect an active bet.
      if (relatedLoginRoute && !isLoginRoute(page.url())) {
        await this._navigate(page, LOGIN_URL, signal);
        await sleep(1000, signal);
        if (await this.isLoggedIn(page)) {
          for (const relatedPage of relatedPages) acknowledgeAuthRoute(relatedPage);
          log.info('LoginManager: session verified after shared-context login-route check');
          return true;
        }
      }

      // Keep an already-rendered login page in place. Navigating to Home first
      // can restart Winner's SPA before its Angular login controls attach,
      // which used to make the collector incorrectly pause for manual auth.
      if (!isLoginRoute(page.url())) {
        await this._navigate(page, HOME_URL, signal);
        await sleep(1500, signal);

        if (await this.isLoggedIn(page)) {
          log.info('LoginManager: session valid');
          return true;
        }
      }

      if (!this.phone || !this.password) {
        if (!isLoginRoute(page.url())) await this._navigate(page, LOGIN_URL, signal);
        throw new AuthRequiredError('Saved session is not authenticated and automatic credentials are not configured; complete sign-in in the collector browser');
      }

      log.info(`LoginManager: not logged in — starting login (attempt ${attempt + 1})`);
      await this._doLogin(page, signal);

      if (await this.isLoggedIn(page)) {
        for (const relatedPage of relatedPages) acknowledgeAuthRoute(relatedPage);
        this.loginCount++;
        log.info(`LoginManager: login successful (total logins: ${this.loginCount})`);
        return true;
      }

      // Do not resubmit configured credentials repeatedly when Winner accepts
      // the login form but the session cannot be verified. Leave the existing
      // tab on the game route so an operator can complete any remaining
      // account step there; collection remains paused until session evidence
      // appears.
      let aviatorRouteOpened = false;
      for (const candidate of aviatorUrls()) {
        if (!await this._navigate(page, candidate, signal)) continue;
        await sleep(2000, signal);
        if (await this.isLoggedIn(page)) {
          for (const relatedPage of relatedPages) acknowledgeAuthRoute(relatedPage);
          this.loginCount++;
          log.info(`LoginManager: session verified after opening Aviator route — ${safeUrl(page.url())}`);
          return true;
        }
        const currentUrl = page.url().toLowerCase();
        if (isAviatorRoute(currentUrl)) {
          aviatorRouteOpened = true;
          break;
        }
      }
      const routeState = aviatorRouteOpened ? 'Aviator route is open' : 'Aviator route could not be opened';
      throw new AuthRequiredError(`Winner login did not produce a verifiable session; ${routeState}; manual login or verification is required`);
    }, { maxAttempts: 4, label: 'ensure-logged-in', signal });
  }

  async _doLogin(page, signal) {
    // Try clicking the login link from homepage first
    const loginLinkSelectors = [
      '#user-menu-login',
      'a.login-btn',
      'a[href*="/login"]',
      'button:has-text("Log in")',
      'button:has-text("Login")',
      'a:has-text("Log in")',
      'a:has-text("Login")',
      '[role="button"]:has-text("Log in")',
      '[role="button"]:has-text("Login")',
    ];
    const firstVisible = async (selectors) => {
      for (const selector of selectors) {
        const matches = page.locator(selector);
        const count = await matches.count().catch(error => { log.debug(`Login selector count failed: ${error.message}`); return 0; });
        for (let index = 0; index < count; index += 1) {
          const candidate = matches.nth(index);
          if (await candidate.isVisible().catch(error => { log.debug(`Login control visibility check failed: ${error.message}`); return false; })) return candidate;
        }
      }
      return null;
    };
    const alreadyOnLoginRoute = isLoginRoute(page.url());
    let loginLink = alreadyOnLoginRoute ? null : await firstVisible(loginLinkSelectors);

    if (!alreadyOnLoginRoute) {
      if (loginLink) {
        await clickInCurrentTab(page, loginLink);
      } else {
        await this._navigate(page, LOGIN_URL, signal);
      }
      await sleep(1000, signal);
      // Winner redirects a valid session away from its login route. Confirm
      // that evidence before treating the missing login form as expired auth.
      if (await this.isLoggedIn(page)) {
        log.info('LoginManager: authenticated game session verified after login-route redirect');
        return true;
      }
    }

    // Dismiss any modal/popup that might be blocking the form
    await this._dismissModals(page);

    const pageInfo = async () => {
      const title = await page.title().catch(error => { log.debug(`Login-page title is unavailable: ${error.message}`); return ''; });
      const inputs = await page.locator('input').count().catch(error => { log.debug(`Login-page input count is unavailable: ${error.message}`); return 0; });
      return `url=${safeUrl(page.url())} title=${JSON.stringify(title)} inputs=${inputs}`;
    };

    const bodyText = await page.locator('body').innerText({ timeout: 3000 }).catch(error => {
      log.debug(`Login-page body text is unavailable: ${error.message}`);
      return '';
    });
    if (/verify you are human|checking your browser|captcha|access denied/i.test(bodyText)) {
      throw new AuthRequiredError('Winner human verification is required; browser session left untouched');
    }

    // Winner hydrates its login form asynchronously after the route renders.
    // Search visible matches individually: a combined selector's .first()
    // may bind to a hidden duplicate and report the form missing.
    const findVisibleControl = async (selectors) => {
      for (const selector of selectors) {
        const matches = page.locator(selector);
        const count = await matches.count().catch(error => { log.debug(`Login control count failed: ${error.message}`); return 0; });
        for (let index = 0; index < count; index += 1) {
          const candidate = matches.nth(index);
          if (await candidate.isVisible().catch(error => { log.debug(`Login control visibility check failed: ${error.message}`); return false; })) return candidate;
        }
      }
      return null;
    };
    const waitForVisible = async (selectors, timeoutMs) => {
      const deadline = Date.now() + timeoutMs;
      while (Date.now() < deadline && !signal?.aborted) {
        const candidate = await findVisibleControl(selectors);
        if (candidate) return candidate;
        await sleep(250, signal);
      }
      return null;
    };

    const phoneSelectors = [
      '#phoneInput',
      'input[name="phoneInput"]',
      'input[name="phone"]',
      'input[name="username"]',
      'input[name="login"]',
      'input[type="tel"]',
      'input[autocomplete="username"]',
      'input[placeholder*="phone" i]',
      'input[placeholder*="mobile" i]',
    ];
    const passwordSelectors = [
      '#password',
      'input[name="password"]',
      'input[type="password"]',
      'input[autocomplete="current-password"]',
    ];
    let phoneInput = await waitForVisible(phoneSelectors, 25000);
    let passInput = await waitForVisible(passwordSelectors, phoneInput ? 10000 : 0);

    if (!phoneInput || !passInput) {
      // A valid session can redirect the login route to Aviator before the
      // Spribe OOPIF is exposed to Playwright. Allow that evidence to attach
      // before asking for manual authentication.
      const authDeadline = Date.now() + 10000;
      while (Date.now() < authDeadline && !signal?.aborted) {
        if (await this.isLoggedIn(page)) {
          log.info('LoginManager: authenticated session verified after game-frame attachment');
          return true;
        }
        await sleep(500, signal);
      }
      if (signal?.aborted) throw new Error('Aborted');

      // If Winner already routed to its login page, leave that page open for
      // the operator. Do not navigate away while a delayed login form renders.
      if (isLoginRoute(page.url())) {
        throw new AuthRequiredError(`Winner login controls did not become visible; the existing login page was left open (${await pageInfo()})`);
      }

      // Winner can redirect its login route to the sportsbook shell even
      // when no login form was opened. Try the canonical Aviator route from
      // that shell so the single managed tab reaches the game or the platform
      // can present its actual authentication/verification screen. Collection
      // remains blocked unless isLoggedIn() confirms a real session and the
      // frame manager later finds live payout elements.
      for (const candidate of aviatorUrls()) {
        log.info(`LoginManager: login form missing; probing Aviator route ${candidate}`);
        if (!await this._navigate(page, candidate, signal)) continue;
        await sleep(2000, signal);
        if (await this.isLoggedIn(page)) {
          log.info(`LoginManager: authenticated session verified on Aviator route — ${safeUrl(page.url())}`);
          return true;
        }
        const route = page.url().toLowerCase();
        if (isAviatorRoute(route)) {
          log.warn(`LoginManager: Aviator route opened without verified authentication — ${safeUrl(page.url())}`);
          // Some Winner shells expose login as a game-page modal instead of
          // the /authentication/login route. Open that form and continue with
          // the same configured credentials when it is actually present.
          loginLink = await firstVisible(loginLinkSelectors);
          if (loginLink) {
            await clickInCurrentTab(page, loginLink, { timeout: 5000 }).catch(error => log.debug(`Login form link could not be clicked: ${error.message}`));
            await sleep(1500, signal);
            phoneInput = await waitForVisible(phoneSelectors, 15000);
            passInput = await waitForVisible(passwordSelectors, 10000);
            if (phoneInput && passInput) {
              log.info('LoginManager: login form opened from Aviator page');
              break;
            }
          }
          break;
        }
      }
      if (signal?.aborted) throw new Error('Aborted');
      if (!phoneInput || !passInput) {
        const bodyAfterRoute = await page.locator('body').innerText({ timeout: 1000 }).catch(error => {
          log.debug(`Login body check after navigation failed: ${error.message}`);
          return '';
        });
        if (/verify you are human|checking your browser|captcha|access denied/i.test(bodyAfterRoute)) {
          throw new AuthRequiredError('Winner human verification is required; browser session left untouched');
        }
        throw new AuthRequiredError(`Winner login form is unavailable after redirect; manual login or verification is required (${await pageInfo()})`);
      }
    }

    if (!this.phone || !this.password) {
      throw new AuthRequiredError('Winner login controls are ready, but configured credentials are missing; manual login is required');
    }
    await phoneInput.click({ clickCount: 3 });
    await phoneInput.fill(this.phone);

    await passInput.click({ clickCount: 3 });
    await passInput.fill(this.password);

    // Submit
    const submitSelectors = [
      '#buttonLoginSubmit',
      'button[type="submit"]',
      'input[type="submit"]',
      'button:has-text("Log in")',
      'button:has-text("Login")',
      'button:has-text("Sign in")',
    ];
    const submitBtn = await waitForVisible(submitSelectors, 10000);
    if (!submitBtn) throw new AuthRequiredError('Winner login submit control is unavailable; manual login is required');
    await clickInCurrentTab(page, submitBtn);

    // Wait for real authenticated page evidence, rather than treating a URL
    // transition alone as a successful login. Leave the login page open if
    // Winner rejects credentials or requires additional human verification.
    const authDeadline = Date.now() + 25000;
    while (Date.now() < authDeadline && !signal?.aborted) {
      if (await this.isLoggedIn(page)) return true;
      const text = await page.locator('body').innerText({ timeout: 1000 }).catch(error => {
        log.debug(`Login verification body check failed: ${error.message}`);
        return '';
      });
      if (/verify you are human|checking your browser|captcha|access denied/i.test(text)) {
        throw new AuthRequiredError('Winner human verification is required; browser session left untouched');
      }
      await sleep(500, signal);
    }
    if (signal?.aborted) throw new Error('Aborted');
    throw new AuthRequiredError('Winner login was submitted but no authenticated session was verified; manual login or verification is required');
  }

  /** Dismiss cookie banners, modals, popups. */
  async _dismissModals(page) {
    const dismissSelectors = [
      'button[aria-label="Close"]',
      '.modal-close', '.close-btn', '[data-dismiss="modal"]',
      'button:has-text("Accept")', 'button:has-text("OK")',
      'button:has-text("Close")', 'button:has-text("Got it")',
    ];
    for (const sel of dismissSelectors) {
      try {
        const el = page.locator(sel).first();
        if (await el.isVisible({ timeout: 500 })) {
          await el.click({ timeout: 1000 });
          await sleep(300);
        }
      } catch (error) { log.debug(`Modal dismissal candidate ${sel} was unavailable: ${error.message}`); }
    }
  }

  async _hasAviatorFrame(page) {
    try {
      if (page.frames?.().some(frame => {
        if (frame === page.mainFrame?.() || frame.isDetached?.()) return false;
        const hint = `${frame.name?.() || ''} ${frame.url?.() || ''}`.toLowerCase();
        return /spribe|aviator|crash|game/.test(hint);
      })) return true;
    } catch (error) { log.debug(`Aviator frame scan failed: ${error.message}`); }
    try {
      return (await page.locator(
        'iframe[src*="spribe"], iframe[src*="aviator"], iframe[src*="crash"]'
      ).count()) > 0;
    } catch (error) {
      log.debug(`Game iframe selector scan failed: ${error.message}`);
      return false;
    }
  }

  async _waitForAviatorFrame(page, timeoutMs = 10000, signal) {
    const deadline = Date.now() + timeoutMs;
    while (!signal?.aborted && Date.now() < deadline) {
      if (await this._hasAviatorFrame(page)) return true;
      await sleep(400, signal);
    }
    return false;
  }

  async _waitForAviatorOutcome(page, previousUrl, timeoutMs = 12000, signal) {
    const previousRoute = winnerRouteKey(previousUrl);
    const deadline = Date.now() + timeoutMs;
    let routeChanged = false;
    while (!signal?.aborted && Date.now() < deadline) {
      const currentUrl = page.url();
      if (isExpiredAuthRoute(currentUrl)) return { status: 'AUTH_REQUIRED', routeChanged };
      const currentRoute = winnerRouteKey(currentUrl);
      if (currentRoute && currentRoute !== previousRoute && isAviatorRoute(currentUrl)) routeChanged = true;
      if (await this._hasAviatorFrame(page)) return { status: 'READY', routeChanged };
      await sleep(400, signal);
    }
    const currentUrl = page.url();
    if (isExpiredAuthRoute(currentUrl)) return { status: 'AUTH_REQUIRED', routeChanged };
    return { status: 'TIMEOUT', routeChanged };
  }

  /** Open the game through the authenticated virtual-games UI when the route
   * shell is present but the Spribe game itself has not launched. */
  async _openThroughCrashGamesSearch(page, signal) {
    await this._requireNoAuthRedirect(page);
    const crashControls = () => [
      page.getByRole?.('button', { name: /crash games/i }),
      page.getByRole?.('link', { name: /crash games/i }),
      page.getByText?.(/crash games/i),
    ].filter(Boolean);
    const firstVisible = async locators => {
      for (const locator of locators) {
        const count = await locator.count?.().catch(error => { log.debug(`Game navigation control count failed: ${error.message}`); return 0; }) || 0;
        for (let index = 0; index < count; index += 1) {
          const candidate = locator.nth(index);
          if (await candidate.isVisible?.().catch(error => { log.debug(`Game navigation control visibility failed: ${error.message}`); return false; })) return candidate;
        }
      }
      return null;
    };

    let crashControl = await firstVisible(crashControls());
    if (!crashControl) {
      if (!await this._navigate(page, HOME_URL, signal)) return false;
      await sleep(700, signal);
      await this._requireNoAuthRedirect(page);
      crashControl = await firstVisible(crashControls());
    }
    if (!crashControl) {
      log.warn('LoginManager: visible Crash Games control was not found');
      return false;
    }
    try {
      await clickInCurrentTab(page, crashControl, { timeout: 5000 });
    } catch (err) {
      log.warn(`LoginManager: Crash Games control click failed: ${err.message}`);
      return false;
    }
    await this._requireNoAuthRedirect(page);

    // This is the current Winner virtual-games search field supplied by the
    // live UI. Keep it first so the collector follows the same visible flow as
    // an operator instead of guessing a different route.
    const requestedSearchSelector = 'body > app-root > main > div > div.au-l-main > ng-component > div.grid-100.idb-gam-virtual > div > div.gam-wrapper-div > div.au-m-cs > div > input';
    let search = page.locator(requestedSearchSelector).first();
    try {
      await search.waitFor({ state: 'visible', timeout: 8000 });
    } catch (error) {
      log.debug(`Configured Aviator search selector was unavailable: ${error.message}`);
      search = page.locator('input[placeholder*="search" i], input[aria-label*="search" i]').first();
      try { await search.waitFor({ state: 'visible', timeout: 3000 }); }
      catch (error) {
        log.debug(`Fallback Aviator search selector was unavailable: ${error.message}`);
        log.warn('LoginManager: Crash Games opened but the virtual-games search field was not visible');
        return false;
      }
    }
    try {
      await search.fill('Aviator', { timeout: 3000 });
      await sleep(500, signal);
    } catch (err) {
      log.warn(`LoginManager: Aviator search could not be entered: ${err.message}`);
      return false;
    }

    const playControls = () => [
      page.getByRole?.('button', { name: /play game/i }),
      page.getByRole?.('link', { name: /play game/i }),
      page.getByText?.(/play game/i),
    ].filter(Boolean);
    const play = await firstVisible(playControls());
    if (!play) {
      log.warn('LoginManager: Aviator search returned no visible Play Game control');
      return false;
    }
    const previousUrl = page.url();
    try {
      await clickInCurrentTab(page, play, { timeout: 7000 });
    } catch (err) {
      await this._requireNoAuthRedirect(page);
      log.warn(`LoginManager: Play Game control click failed: ${err.message}`);
      return false;
    }
    const outcome = await this._waitForAviatorOutcome(page, previousUrl, 12000, signal);
    if (outcome.status === 'AUTH_REQUIRED') await this._requireNoAuthRedirect(page);
    if (outcome.status !== 'READY') {
      log.warn(outcome.routeChanged
        ? `LoginManager: Crash Games route changed but the Aviator frame did not attach — ${safeUrl(page.url())}`
        : `LoginManager: Play Game did not attach an Aviator frame — ${safeUrl(page.url())}`);
      return false;
    }
    log.info(`LoginManager: Aviator opened through Crash Games search — ${safeUrl(page.url())}`);
    return true;
  }

  /** Navigate to the Aviator game page. */
  async goToAviator(page, signal) {
    return withRetry(async () => {
      if (signal?.aborted) throw new Error('Aborted');
      await this._requireNoAuthRedirect(page);

      if (await isAccessDeniedPage(page)) {
        await this._navigate(page, HOME_URL, signal);
        await sleep(2000, signal);
        await this._requireNoAuthRedirect(page);
      }

      // A route shell without the game frame is not a successful game launch.
      // Try the visible Crash Games → search → Play Game flow in the same tab.
      const currentUrl = page.url();
      if (isAviatorRoute(currentUrl)) {
        if (await this._hasAviatorFrame(page)) {
          log.info(`LoginManager: Aviator page loaded — ${safeUrl(page.url())}`);
          return true;
        }
      }

      if (await this._openThroughCrashGamesSearch(page, signal)) return true;
      await this._requireNoAuthRedirect(page);

      // Winner renders the Aviator tile as either an anchor, button, or
      // accessible card depending on viewport and login state. A combined CSS
      // selector with .first() can select a hidden duplicate and miss the
      // visible control, so inspect each visible role candidate individually.
      await this._dismissModals(page);
      const getControls = () => [
        page.getByRole('link', { name: /aviator/i }),
        page.getByRole('button', { name: /aviator/i }),
        page.locator('a[href*="aviator" i]'),
        page.locator('[role="link"]:has-text("Aviator"), [role="button"]:has-text("Aviator")'),
        page.locator('button:has-text("Aviator")'),
        page.getByText('Aviator', { exact: true }),
      ];

      const clickAviatorControl = async () => {
        const maximumCandidateAttempts = 4;
        let candidateAttempts = 0;
        let staleRescans = 0;
        while (candidateAttempts < maximumCandidateAttempts) {
          let rescan = false;
          for (const locator of getControls()) {
            const count = await locator.count().catch(error => { log.debug(`Aviator control count failed: ${error.message}`); return 0; });
            for (let index = 0; index < count && candidateAttempts < maximumCandidateAttempts; index += 1) {
              const control = locator.nth(index);
              if (!await control.isVisible().catch(error => { log.debug(`Aviator control visibility failed: ${error.message}`); return false; })) continue;
              candidateAttempts += 1;
              const previousUrl = page.url();
              try {
                await clickInCurrentTab(page, control, { timeout: 5000 });
              } catch (err) {
                await this._requireNoAuthRedirect(page);
                if (staleRescans === 0 && isUnstableLocatorError(err)) {
                  staleRescans += 1;
                  rescan = true;
                  break;
                }
                log.warn(`LoginManager: visible Aviator control click failed: ${err.message}`);
                continue;
              }
              const outcome = await this._waitForAviatorOutcome(page, previousUrl, 12000, signal);
              if (outcome.status === 'AUTH_REQUIRED') await this._requireNoAuthRedirect(page);
              if (outcome.status === 'READY') {
                log.info(`LoginManager: Aviator control opened game page — ${safeUrl(page.url())}`);
                return true;
              }
              if (outcome.routeChanged) {
                log.warn(`LoginManager: Aviator route changed but the game frame did not attach — ${safeUrl(page.url())}`);
              } else {
                log.warn(`LoginManager: Aviator control click did not reach a game route or attach its frame — ${safeUrl(page.url())}`);
              }
            }
            if (rescan || candidateAttempts >= maximumCandidateAttempts) break;
          }
          if (!rescan) break;
          await sleep(250, signal);
        }
        return false;
      };

      let opened = await clickAviatorControl();
      if (!opened) {
        await this._navigate(page, HOME_URL, signal);
        await sleep(2000, signal);
        await this._requireNoAuthRedirect(page);
        opened = await clickAviatorControl();
      }

      if (!opened) {
        // Winner changes its home-page tiles regularly. A missing, hidden, or
        // inert control should not strand collection; try known canonical routes.
        for (const candidate of aviatorUrls()) {
          log.warn(`LoginManager: Aviator control unavailable; trying ${candidate}`);
          const previousUrl = page.url();
          if (await this._navigate(page, candidate, signal)) {
            await this._requireNoAuthRedirect(page);
            const outcome = await this._waitForAviatorOutcome(page, previousUrl, 12000, signal);
            if (outcome.status === 'AUTH_REQUIRED') await this._requireNoAuthRedirect(page);
            if (isAviatorRoute(page.url()) && outcome.status === 'READY') {
              opened = true;
              log.info(`LoginManager: Aviator page opened by canonical route — ${safeUrl(page.url())}`);
              break;
            }
          }
        }
        if (!opened) throw Object.assign(new Error('Aviator game frame did not attach after Crash Games search and canonical route attempts'), {
          code: 'GAME_FRAME_MISSING', reasonCode: 'GAME_FRAME_NOT_ATTACHED',
          evidence: { route: safeUrl(page.url()), source: 'history_page_frame_scan' },
        });
      }

      await this._requireNoAuthRedirect(page);
      const url = page.url();
      if (await isAccessDeniedPage(page)) {
        throw new Error('Aviator navigation reached Access Denied');
      }
      if (isAviatorRoute(url)) {
        if (await this._waitForAviatorFrame(page, 5000, signal)) {
          log.info(`LoginManager: Aviator game frame loaded — ${safeUrl(page.url())}`);
          return true;
        }
        throw new Error(`Aviator route loaded without its game frame: ${safeUrl(page.url())}`);
      }
      throw new Error(`Unexpected URL after Aviator navigation: ${safeUrl(page.url())}`);
    }, { maxAttempts: 1, label: 'go-to-aviator', signal });
  }

  /** Navigate the existing history tab straight to Aviator during recovery. */
  async openAviatorRoute(page, signal, { timeoutMs = 7000 } = {}) {
    for (const candidate of aviatorUrls()) {
      const previousUrl = page.url();
      if (!await this._navigate(page, candidate, signal, { timeoutMs })) continue;
      await this._requireNoAuthRedirect(page);
      if (isAviatorRoute(page.url())) {
        const outcome = await this._waitForAviatorOutcome(page, previousUrl, 5000, signal);
        if (outcome.status === 'AUTH_REQUIRED') await this._requireNoAuthRedirect(page);
        if (outcome.status === 'READY') {
          log.info(`LoginManager: opened Aviator route directly during recovery — ${safeUrl(page.url())}`);
          return true;
        }
        if (await this._openThroughCrashGamesSearch(page, signal)) return true;
      }
    }
    return false;
  }
}
