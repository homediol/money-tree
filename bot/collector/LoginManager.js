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
const AVIATOR_URL = 'https://winner.rw/en/virtual/crash-games/aviator';

// Selectors that indicate the user is logged in.
// These are checked IN THE CURRENT PAGE without navigating away.
const SESSION_SIGNALS = [
  '.user-profile', '.account-dropdown', '.dashboard',
  '[class*="balance"]', '[class*="wallet"]', '[class*="avatar"]',
  'a[href*="logout"]', '[data-testid="user-balance"]',
  // Spribe iframe presence = game loaded = definitely logged in
  'iframe[src*="spribe"]', 'iframe[src*="aviator"]',
];

// Selectors that indicate the user is logged out / on login page
const LOGOUT_SIGNALS = [
  '#user-menu-login', 'a.login-btn', '#phoneInput',
  '[data-testid="login-button"]',
];

export class LoginManager {
  constructor(credentials) {
    this.phone    = credentials.phone;
    this.password = credentials.password;
    this.loginCount = 0;
  }

  /** Navigate safely, trying multiple waitUntil strategies. */
  async _navigate(page, url, signal) {
    for (const waitUntil of ['load', 'domcontentloaded', 'commit']) {
      if (signal?.aborted) return false;
      try {
        await page.goto(url, { waitUntil, timeout: 45000 });
        await page.waitForSelector('body', { timeout: 5000 }).catch(() => {});
        return true;
      } catch (err) {
        log.warn(`Navigation to ${url} failed (${waitUntil}): ${err.message}`);
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

      // On winner.rw but no clear signal either way — assume still logged in
      // to avoid false positives that trigger unnecessary re-logins
      if (url.includes('winner.rw')) return true;

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

    // Fill phone
    const phoneInput = page.locator('#phoneInput').first();
    await phoneInput.waitFor({ state: 'visible', timeout: 15000 });
    await phoneInput.click({ clickCount: 3 });
    await phoneInput.fill(this.phone);

    // Fill password
    const passInput = page.locator('#password').first();
    await passInput.waitFor({ state: 'visible', timeout: 10000 });
    await passInput.click({ clickCount: 3 });
    await passInput.fill(this.password);

    // Submit
    const submitBtn = page.locator('#buttonLoginSubmit, #buttonLoginSubmitLabel, button[type="submit"]').first();
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

      // Try clicking the Aviator link from the current page
      const aviatorLink = page.locator('a[href*="/aviator"], a[href*="crash-games"]').first();
      const linkVisible = await aviatorLink.isVisible({ timeout: 3000 }).catch(() => false);

      if (linkVisible) {
        await aviatorLink.click();
        await sleep(4000, signal);
      } else {
        const ok = await this._navigate(page, AVIATOR_URL, signal);
        if (!ok) throw new Error('Could not navigate to Aviator page');
        await sleep(3000, signal);
      }

      const url = page.url().toLowerCase();
      if (url.includes('aviator') || url.includes('crash')) {
        log.info(`LoginManager: Aviator page loaded — ${page.url()}`);
        return true;
      }
      throw new Error(`Unexpected URL after Aviator navigation: ${page.url()}`);
    }, { maxAttempts: 4, label: 'go-to-aviator', signal });
  }
}
