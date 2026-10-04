/**
 * LoginManager.js — Session detection, login, and logout recovery.
 * Never opens a second browser. Works with the existing page.
 */

import path from 'path';
import { fileURLToPath } from 'url';
import { log } from './Logger.js';
import { withRetry, sleep } from './RetryManager.js';

const __dirname = path.dirname(fileURLToPath(import.meta.url));

const HOME_URL    = 'https://winner.rw';
const LOGIN_URL   = 'https://winner.rw/en/authentication/login';
const AVIATOR_URLS = [
  'https://winner.rw/en/virtual/crash-games/aviator',
  'https://winner.rw/en/games/aviator',
];

function isLoginRoute(url) {
  try {
    return /\/(?:authentication\/login|login)(?:\/|$)/i.test(new URL(url).pathname);
  } catch {
    return false;
  }
}

// Selectors that indicate the user is logged in.
// These are checked IN THE CURRENT PAGE without navigating away.
const SESSION_SIGNALS = [
  '.user-profile', '.account-dropdown', '.user-avatar',
  '.header__balance', '.header__wrap-balance',
  'a[href*="logout"]', '[data-testid="user-balance"]',
  // Current Winner navigation marks the authenticated account container with
  // these classes and exposes its balance through .usr-balance. Older logout
  // and header-balance selectors are absent on the live sportsbook shell.
  '.au-m-nav-u.login-area.usr-lgd-in', 'a.usr-balance',
  // Spribe iframe presence = game loaded = definitely logged in
  'iframe[src*="spribe"]', 'iframe[src*="aviator"]',
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
  } catch {
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
  async _navigate(page, url, signal) {
    // Winner keeps chat/analytics requests alive, so the browser's full
    // ``load`` event is not a reliable readiness boundary.
    for (const waitUntil of ['commit', 'domcontentloaded']) {
      if (signal?.aborted) return false;
      try {
        await page.goto(url, { waitUntil, timeout: 15000 });
        await page.waitForSelector('body', { timeout: 5000 }).catch(() => {});
        if (await isAccessDeniedPage(page)) {
          log.warn(`Navigation to ${url} reached Access Denied`);
          return false;
        }
        return true;
      } catch (err) {
        log.warn(`Navigation to ${url} failed (${waitUntil}): ${err.message}`);
        // Winner may keep analytics/chat requests open indefinitely. A load
        // timeout is harmless once the requested same-origin route has a body.
        try {
          const requested = new URL(url);
          const current = new URL(page.url());
          const routeMatches = requested.pathname === '/' ||
            current.pathname.replace(/\/$/, '') === requested.pathname.replace(/\/$/, '');
          const bodyReady = await page.locator('body').count().catch(() => 0);
          if (current.origin === requested.origin && routeMatches && bodyReady &&
              !await isAccessDeniedPage(page)) {
            log.info(`Navigation route rendered despite ${waitUntil} timeout: ${page.url()}`);
            return true;
          }
        } catch {}
      }
    }
    return false;
  }

  /**
   * Check if the current page shows a logged-in session.
   * IMPORTANT: Does NOT navigate away — checks the current page only.
   * The Aviator iframe being present is the strongest signal.
   */
  async isLoggedIn(page) {
    try {
      if (!page || page.isClosed()) return false;

      const url = page.url().toLowerCase();
      if (await isAccessDeniedPage(page)) return false;

      // On login/auth page = definitely not logged in
      // Winner's session expiry opens /user/logout/popup inside the shell.
      // That page can still expose the global logout link, so route evidence
      // must take precedence over broad "session is present" selectors.
      if (isLoginRoute(url) || url.includes('/logout')) return false;

      // If we're on the Aviator page and the iframe is present = logged in
      // This is the most reliable check and requires no extra navigation
      if (url.includes('aviator') || url.includes('crash')) {
        const hasIframe = await page.locator(
          'iframe[src*="spribe"], iframe[src*="aviator"], iframe[src*="crash"]'
        ).first().isVisible({ timeout: 1000 }).catch(() => false);
        if (hasIframe) return true;
        // Chromium may expose the authenticated Spribe game as an OOPIF
        // target even when Playwright's parent-page iframe locator is not
        // visible. Accept only the real game origin with both session
        // parameters present; never log or persist the token in diagnostics.
        const hasAuthenticatedGameFrame = page.frames().some(frame => {
          try {
            if (frame === page.mainFrame()) return false;
            const gameUrl = new URL(frame.url());
            return /(^|\.)spribegaming\.com$/i.test(gameUrl.hostname)
              && /aviator/i.test(gameUrl.pathname)
              && gameUrl.searchParams.has('token')
              && gameUrl.searchParams.has('user');
          } catch { return false; }
        });
        if (hasAuthenticatedGameFrame) return true;
      }

      // Check session signals in current DOM (no navigation)
      for (const sel of SESSION_SIGNALS) {
        try {
          if (await page.locator(sel).first().isVisible({ timeout: 800 })) return true;
        } catch {}
      }

      // Check logout signals — if visible, definitely not logged in
      for (const sel of LOGOUT_SIGNALS) {
        try {
          if (await page.locator(sel).first().isVisible({ timeout: 400 })) return false;
        } catch {}
      }

      return false;
    } catch {
      return false;
    }
  }

  /**
   * Ensure the user is logged in.
   * If already logged in, returns true immediately.
   * Otherwise navigates to login and submits credentials.
   */
  async ensureLoggedIn(page, signal) {
    return withRetry(async (attempt) => {
      if (signal?.aborted) throw new Error('Aborted');

      // A restored Aviator tab may already have a valid session. Keep that
      // page in place instead of bouncing through the sportsbook on recovery.
      if (await this.isLoggedIn(page)) {
        log.info('LoginManager: session valid on current page');
        return true;
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

      log.info(`LoginManager: not logged in — starting login (attempt ${attempt + 1})`);
      await this._doLogin(page, signal);

      if (await this.isLoggedIn(page)) {
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
      for (const candidate of AVIATOR_URLS) {
        if (!await this._navigate(page, candidate, signal)) continue;
        await sleep(2000, signal);
        if (await this.isLoggedIn(page)) {
          this.loginCount++;
          log.info(`LoginManager: session verified after opening Aviator route — ${page.url()}`);
          return true;
        }
        const currentUrl = page.url().toLowerCase();
        if (currentUrl.includes('aviator') || currentUrl.includes('crash-games')) {
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
        const count = await matches.count().catch(() => 0);
        for (let index = 0; index < count; index += 1) {
          const candidate = matches.nth(index);
          if (await candidate.isVisible().catch(() => false)) return candidate;
        }
      }
      return null;
    };
    const alreadyOnLoginRoute = isLoginRoute(page.url());
    let loginLink = alreadyOnLoginRoute ? null : await firstVisible(loginLinkSelectors);

    if (!alreadyOnLoginRoute) {
      if (loginLink) {
        await loginLink.click();
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
      const title = await page.title().catch(() => '');
      const inputs = await page.locator('input').count().catch(() => 0);
      return `url=${page.url()} title=${JSON.stringify(title)} inputs=${inputs}`;
    };

    const bodyText = await page.locator('body').innerText({ timeout: 3000 }).catch(() => '');
    if (/verify you are human|checking your browser|captcha|access denied/i.test(bodyText)) {
      throw new AuthRequiredError('Winner human verification is required; browser session left untouched');
    }

    // Winner hydrates its login form asynchronously after the route renders.
    // Search visible matches individually: a combined selector's .first()
    // may bind to a hidden duplicate and report the form missing.
    const findVisibleControl = async (selectors) => {
      for (const selector of selectors) {
        const matches = page.locator(selector);
        const count = await matches.count().catch(() => 0);
        for (let index = 0; index < count; index += 1) {
          const candidate = matches.nth(index);
          if (await candidate.isVisible().catch(() => false)) return candidate;
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
      for (const candidate of AVIATOR_URLS) {
        log.info(`LoginManager: login form missing; probing Aviator route ${candidate}`);
        if (!await this._navigate(page, candidate, signal)) continue;
        await sleep(2000, signal);
        if (await this.isLoggedIn(page)) {
          log.info(`LoginManager: authenticated session verified on Aviator route — ${page.url()}`);
          return true;
        }
        const route = page.url().toLowerCase();
        if (route.includes('aviator') || route.includes('crash-games')) {
          log.warn(`LoginManager: Aviator route opened without verified authentication — ${page.url()}`);
          // Some Winner shells expose login as a game-page modal instead of
          // the /authentication/login route. Open that form and continue with
          // the same configured credentials when it is actually present.
          loginLink = await firstVisible(loginLinkSelectors);
          if (loginLink) {
            await loginLink.click({ timeout: 5000 }).catch(() => {});
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
        const bodyAfterRoute = await page.locator('body').innerText({ timeout: 1000 }).catch(() => '');
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
    await submitBtn.click();

    // Wait for real authenticated page evidence, rather than treating a URL
    // transition alone as a successful login. Leave the login page open if
    // Winner rejects credentials or requires additional human verification.
    const authDeadline = Date.now() + 25000;
    while (Date.now() < authDeadline && !signal?.aborted) {
      if (await this.isLoggedIn(page)) return true;
      const text = await page.locator('body').innerText({ timeout: 1000 }).catch(() => '');
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
      } catch {}
    }
  }

  /** Navigate to the Aviator game page. */
  async goToAviator(page, signal) {
    return withRetry(async () => {
      if (signal?.aborted) throw new Error('Aborted');

      if (await isAccessDeniedPage(page)) {
        await this._navigate(page, HOME_URL, signal);
        await sleep(2000, signal);
      }

      // During frame recovery the current tab is often already on Aviator.
      // Reload that route directly if its frame vanished; routing through the
      // homepage creates a visible MetaBrandTitle sportsbook tab in Chrome.
      const currentUrl = page.url().toLowerCase();
      if (currentUrl.includes('winner.rw') &&
          (currentUrl.includes('aviator') || currentUrl.includes('crash-games'))) {
        const iframePresent = await page.locator(
          'iframe[src*="spribe"], iframe[src*="aviator"], iframe[src*="crash"]'
        ).first().isVisible({ timeout: 1000 }).catch(() => false);
        if (iframePresent || await this._navigate(page, AVIATOR_URLS[0], signal)) {
          log.info(`LoginManager: Aviator page loaded — ${page.url()}`);
          return true;
        }
      }

      // Winner renders the Aviator tile as either an anchor, button, or
      // accessible card depending on viewport and login state. A combined CSS
      // selector with .first() can select a hidden duplicate and miss the
      // visible control, so inspect each visible role candidate individually.
      await this._dismissModals(page);
      const getControls = () => [
        page.getByRole('link', { name: /aviator/i }),
        page.getByRole('button', { name: /aviator/i }),
        page.locator('a[href*="aviator" i], a[href*="crash-games" i]'),
        page.locator('[role="link"]:has-text("Aviator"), [role="button"]:has-text("Aviator")'),
        page.locator('button:has-text("Aviator")'),
        page.getByText('Aviator', { exact: true }),
      ];

      const clickAviatorControl = async () => {
        for (const locator of getControls()) {
          const count = await locator.count().catch(() => 0);
          for (let index = 0; index < count; index += 1) {
            const control = locator.nth(index);
            if (!await control.isVisible().catch(() => false)) continue;
            try {
              await control.scrollIntoViewIfNeeded({ timeout: 2000 });
              const routeChanged = page.waitForURL(
                url => /aviator|crash-games|\/crash/i.test(url),
                { timeout: 7000 },
              ).then(() => true).catch(() => false);
              await control.click({ timeout: 7000 });
              await routeChanged;
              await sleep(1500, signal);
              const current = page.url().toLowerCase();
              const gameFrameVisible = await page.locator(
                'iframe[src*="spribe"], iframe[src*="aviator"], iframe[src*="crash"]'
              ).first().isVisible().catch(() => false);
              if (current.includes('aviator') || current.includes('crash') || gameFrameVisible) {
                log.info(`LoginManager: Aviator control opened game page — ${page.url()}`);
                return true;
              }
              log.warn(`LoginManager: Aviator control was clickable but did not open the game route — ${page.url()}`);
            } catch (err) {
              log.warn(`LoginManager: visible Aviator control click failed: ${err.message}`);
            }
          }
        }
        return false;
      };

      let opened = await clickAviatorControl();
      if (!opened) {
        await this._navigate(page, HOME_URL, signal);
        await sleep(2000, signal);
        opened = await clickAviatorControl();
      }

      if (!opened) {
        // Winner changes its home-page tiles regularly. A missing, hidden, or
        // inert control should not strand collection; try known canonical routes.
        for (const candidate of AVIATOR_URLS) {
          log.warn(`LoginManager: Aviator control unavailable; trying ${candidate}`);
          if (await this._navigate(page, candidate, signal)) {
            await sleep(4000, signal);
            const candidateUrl = page.url().toLowerCase();
            if (candidateUrl.includes('aviator') || candidateUrl.includes('crash')) {
              opened = true;
              log.info(`LoginManager: Aviator page opened by canonical route — ${page.url()}`);
              break;
            }
          }
        }
        if (!opened) throw new Error('Could not click the Aviator control or open its canonical route');
      }

      const url = page.url().toLowerCase();
      if (await isAccessDeniedPage(page)) {
        throw new Error('Aviator navigation reached Access Denied');
      }
      if (url.includes('aviator') || url.includes('crash')) {
        log.info(`LoginManager: Aviator page loaded — ${page.url()}`);
        return true;
      }
      throw new Error(`Unexpected URL after Aviator navigation: ${page.url()}`);
    }, { maxAttempts: 4, label: 'go-to-aviator', signal });
  }
}
