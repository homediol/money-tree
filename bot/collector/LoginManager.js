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

// Selectors that indicate the user is logged in.
// These are checked IN THE CURRENT PAGE without navigating away.
const SESSION_SIGNALS = [
  '.user-profile', '.account-dropdown', '.user-avatar',
  '.header__balance', '.header__wrap-balance',
  'a[href*="logout"]', '[data-testid="user-balance"]',
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
      if (url.includes('/login') || url.includes('/authentication')) return false;

      // If we're on the Aviator page and the iframe is present = logged in
      // This is the most reliable check and requires no extra navigation
      if (url.includes('aviator') || url.includes('crash')) {
        const hasIframe = await page.locator(
          'iframe[src*="spribe"], iframe[src*="aviator"], iframe[src*="crash"]'
        ).first().isVisible({ timeout: 1000 }).catch(() => false);
        if (hasIframe) return true;
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

      // Navigate home first (avoids Cloudflare blocks on direct login URL)
      await this._navigate(page, HOME_URL, signal);
      await sleep(1500, signal);

      if (await this.isLoggedIn(page)) {
        log.info('LoginManager: session valid');
        return true;
      }

      log.info(`LoginManager: not logged in — starting login (attempt ${attempt + 1})`);
      await this._doLogin(page, signal);

      if (await this.isLoggedIn(page)) {
        this.loginCount++;
        log.info(`LoginManager: login successful (total logins: ${this.loginCount})`);
        return true;
      }
      throw new Error('Login completed but session not detected');
    }, { maxAttempts: 4, label: 'ensure-logged-in', signal });
  }

  async _doLogin(page, signal) {
    // Try clicking the login link from homepage first
    const loginLink = page.locator('#user-menu-login, a.login-btn, a[href*="/login"]').first();
    const linkVisible = await loginLink.isVisible({ timeout: 3000 }).catch(() => false);

    if (linkVisible) {
      await loginLink.click();
      await sleep(2000, signal);
    } else {
      await this._navigate(page, LOGIN_URL, signal);
      await sleep(2000, signal);
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

    // Prefer stable semantic attributes. Winner has changed these element IDs
    // more than once, so IDs are only one option rather than a requirement.
    const loginForm = page.locator('form:has(input[type="password"])').first();

    // Fill phone
    let phoneInput = page.locator([
      '#phoneInput',
      'input[name="phone"]',
      'input[name="username"]',
      'input[name="login"]',
      'input[type="tel"]',
      'input[autocomplete="username"]',
      'input[placeholder*="phone" i]',
      'input[placeholder*="mobile" i]',
    ].join(', ')).first();
    if (!await phoneInput.isVisible({ timeout: 5000 }).catch(() => false)) {
      phoneInput = loginForm.locator('input:not([type="password"]):not([type="hidden"]):not([type="submit"])').first();
    }
    if (!await phoneInput.isVisible({ timeout: 10000 }).catch(() => false)) {
      if (/\/login|\/authentication/i.test(page.url())) {
        throw new AuthRequiredError('Winner login form is unavailable; manual verification may be required');
      }
      throw new Error(`Winner phone/login input not found (${await pageInfo()})`);
    }
    await phoneInput.click({ clickCount: 3 });
    await phoneInput.fill(this.phone);

    // Fill password
    const passInput = page.locator([
      '#password',
      'input[name="password"]',
      'input[type="password"]',
      'input[autocomplete="current-password"]',
    ].join(', ')).first();
    if (!await passInput.isVisible({ timeout: 10000 }).catch(() => false)) {
      throw new Error(`Winner password input not found (${await pageInfo()})`);
    }
    await passInput.click({ clickCount: 3 });
    await passInput.fill(this.password);

    // Submit
    const submitBtn = page.locator([
      '#buttonLoginSubmit',
      '#buttonLoginSubmitLabel',
      'button[type="submit"]',
      'input[type="submit"]',
      'button:has-text("Log in")',
      'button:has-text("Login")',
      'button:has-text("Sign in")',
    ].join(', ')).first();
    await submitBtn.waitFor({ state: 'visible', timeout: 10000 });
    await submitBtn.click();

    // Wait for navigation away from login page
    await Promise.race([
      page.waitForURL(url => !url.includes('/login') && !url.includes('/authentication'), { timeout: 20000 }),
      sleep(20000, signal),
    ]).catch(() => {});

    await sleep(2000, signal);
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

      // Try clicking the Aviator link from the current page or homepage.
      const aviatorLink = page.locator([
        'a[href*="/aviator"]',
        'a[href*="crash-games"]',
        'a:has-text("Aviator")',
        '[role="link"]:has-text("Aviator")',
        'button:has-text("Aviator")',
      ].join(', ')).first();
      let linkVisible = await aviatorLink.isVisible({ timeout: 3000 }).catch(() => false);

      if (!linkVisible) {
        await this._navigate(page, HOME_URL, signal);
        await sleep(2000, signal);
        linkVisible = await aviatorLink.isVisible({ timeout: 5000 }).catch(() => false);
      }

      if (linkVisible) {
        await aviatorLink.click({ timeout: 10000 });
        await sleep(4000, signal);
      } else {
        // Winner changes its home-page tiles regularly. A missing link should
        // not strand the collector forever: try known canonical routes.
        let opened = false;
        for (const candidate of AVIATOR_URLS) {
          log.warn(`LoginManager: Aviator link not visible; trying ${candidate}`);
          if (await this._navigate(page, candidate, signal)) {
            await sleep(4000, signal);
            const candidateUrl = page.url().toLowerCase();
            if (candidateUrl.includes('aviator') || candidateUrl.includes('crash')) {
              opened = true;
              break;
            }
          }
        }
        if (!opened) throw new Error('Could not open Aviator from link or known direct routes');
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
