/**
 * Login — authentication + session detection for winner.rw.
 *
 * Key design principles:
 *   1. SMART PAGE STATE DETECTION — uses multiple indicators (URL, dashboard
 *      elements, login form presence) to determine if the user is already
 *      logged in, instead of relying on a single URL check.
 *   2. CONDITIONAL LOGIN FLOW — only fills/submits credentials if the login
 *      form actually exists on the page. If already logged in, skip entirely.
 *   3. CLOUDFLARE BYPASS — winner.rw blocks direct navigation to sub-pages via
 *      `page.goto()` (returns "Access Denied"). Instead, we navigate to the
 *      homepage and click the target link, which works fine.
 *   4. SAFE ELEMENT CHECKS — always uses page.$() to first verify element
 *      existence before waiting or interacting. Never blindly calls
 *      waitForSelector without checking via page.$() first.
 *
 * Exports:
 *   - checkSession(page)     — detect if already logged in
 *   - login(page, phone, pw) — fill credentials and submit (only if form exists)
 *   - goToAviator(page)      — navigate to the Aviator crash game
 */

import { fileURLToPath } from 'url';
import path from 'path';
import { safeNavigate } from './browser.js';
import { createLogger } from './logger.js';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(__dirname, '..');

const log = createLogger('login');

// ═════════════════════════════════════════════════════════════════════
// Constants
// ═════════════════════════════════════════════════════════════════════

const BASE_URL    = 'https://winner.rw';
const HOME_URL    = `${BASE_URL}`;
const LOGIN_URL   = `${BASE_URL}/en/authentication/login`;

// Navigation link selectors — we click these from the homepage to bypass
// Cloudflare's "Access Denied" on direct page navigation.
const LOGIN_LINK_SELECTOR   = 'a#user-menu-login, a.login-btn, a[href*="/login"]';
const AVIATOR_LINK_SELECTOR = 'a[href*="/aviator"], a[href*="crash-games"]';

async function isAccessDeniedPage(page) {
  try {
    const bodyText = (await page.innerText('body', { timeout: 1000 })).toLowerCase();
    return bodyText.includes('accessdenied') || bodyText.includes('access denied');
  } catch {
    return false;
  }
}

// ── Indicators used by detectPageState ───────────────────────────────
//
// DASHBOARD_SELECTORS  – elements that ONLY appear when logged in
// LOGIN_FORM_SELECTORS – elements that ONLY appear on the login page
//
// We query BOTH sets and compare signals to decide login state.

const DASHBOARD_SELECTORS = [
  '.user-profile',
  '.account-dropdown',
  '.dashboard',
  '[class*="my-account"]',
  '[class*="logout"]',
  'a[href*="logout"]',
  'a[href*="my-account"]',
  // User avatar / balance indicators common on betting sites
  '[class*="balance"]',
  '[class*="wallet"]',
  '[class*="avatar"]',
];

const LOGIN_FORM_SELECTORS = [
  '#phoneInput',
  '#username',
  '#email',
  'input[name="phone"]',
  'input[name="username"]',
  'input[name="email"]',
  'input[type="tel"]',
  'input[placeholder*="phone" i]',
  'input[placeholder*="username" i]',
  // Password field is a strong indicator of a login form
  'input[type="password"]',
  '#password',
  'input[name="password"]',
];

// ═════════════════════════════════════════════════════════════════════
// Cloudflare bypass — click nav links from homepage
// ═════════════════════════════════════════════════════════════════════

/**
 * Navigate to the homepage, then click a navigation link to reach a
 * target page.  This bypasses Cloudflare which blocks direct goto()
 * to any sub-page (returns "Access Denied").
 *
 * @param {Page}   page
 * @param {string} linkSelector   CSS selector for the link to click
 * @param {string} label          Human-readable label for logging
 * @returns {Promise<boolean>}    Whether we successfully reached the target
 */
async function goViaHomepage(page, linkSelector, label) {
  const currentUrl = page.url();
  const accessDenied = await isAccessDeniedPage(page);
  // Reset through the homepage when the current document is blocked or external.
  if (!currentUrl || currentUrl === 'about:blank' || !currentUrl.startsWith(BASE_URL) || accessDenied) {
    log.info(`Navigating to homepage to click "${label}" link...`);
    const ok = await safeNavigate(page, HOME_URL, 30000);
    if (!ok) {
      log.error('Could not load homepage');
      return false;
    }
    // Wait for Angular SPA to bootstrap and render navigation
    try {
      await page.waitForSelector('#user-menu-login, a.login-btn, .au-m-nav-u, a[href*="/aviator"]', { timeout: 30000 });
    } catch {
      log.warn('Angular app render timeout — page may not have rendered fully');
    }
    await page.waitForTimeout(1000);
  } else {
    log.info(`Already on winner.rw — looking for "${label}" link on current page`);
  }

  // Find the navigation link
  const link = await page.$(linkSelector);
  if (!link) {
    log.warn(`"${label}" link (${linkSelector}) not found — page may have loaded differently`);
    return false;
  }

  const visible = await link.isVisible().catch(() => false);
  if (!visible) {
    log.warn(`"${label}" link found but not visible`);
    return false;
  }

  const href = await link.getAttribute('href');
  log.info(`Clicking "${label}" link: ${href}`);
  await link.click();
  await page.waitForTimeout(3500);
  return true;
}

// ═════════════════════════════════════════════════════════════════════
// Smart page state detection
// ═════════════════════════════════════════════════════════════════════

/**
 * Examine the current page and return a set of signals about the login state.
 *
 * Uses page.$() (not waitForSelector) for all queries — these are fast,
 * non-blocking existence checks that return null immediately if the element
 * doesn't exist.
 *
 * @param {import('playwright').Page} page
 * @returns {Promise<{
 *   isLoginPage: boolean,
 *   hasDashboard: boolean,
 *   hasLoginForm: boolean,
 *   hasLoginLink: boolean,
 *   confidence: 'high' | 'medium' | 'low',
 * }>}
 */
export async function detectPageState(page) {
  const currentUrl = page.url().toLowerCase();

  const state = {
    isLoginPage:   currentUrl.includes('/login') || currentUrl.includes('/auth') || currentUrl.includes('/signin'),
    hasDashboard:  false,
    hasLoginForm:  false,
    hasLoginLink:  false,
    confidence:    'low',
  };

  try {
    const loginLink = await page.$(LOGIN_LINK_SELECTOR);
    if (loginLink && await loginLink.isVisible().catch(() => false)) {
      state.hasLoginLink = true;
      log.info(`Login link signal found: "${LOGIN_LINK_SELECTOR}" is visible`);
    }
  } catch {
    // Ignore and continue with stronger signals.
  }

  // ── Check for dashboard elements (logged-in signals) ────────────
  for (const sel of DASHBOARD_SELECTORS) {
    try {
      const el = await page.$(sel);
      if (el) {
        const visible = await el.isVisible().catch(() => false);
        if (visible) {
          state.hasDashboard = true;
          log.info(`Dashboard signal found: "${sel}" is visible`);
          break;
        }
      }
    } catch {
      // Silently ignore detached/error elements and try next
    }
  }

  // ── Check for login form elements ───────────────────────────────
  for (const sel of LOGIN_FORM_SELECTORS) {
    try {
      const el = await page.$(sel);
      if (el) {
        const visible = await el.isVisible().catch(() => false);
        if (visible) {
          state.hasLoginForm = true;
          log.info(`Login form signal found: "${sel}" is visible`);
          break;
        }
      }
    } catch {
      // Silently ignore and try next
    }
  }

  // ── Determine confidence level ──────────────────────────────────
  if (state.hasDashboard && (state.hasLoginForm || state.hasLoginLink)) {
    state.confidence = 'low';
    log.warn('Conflicting signals: dashboard and login entry detected');
  } else if (state.hasDashboard) {
    state.confidence = state.isLoginPage ? 'medium' : 'high';
  } else if (state.hasLoginForm || state.hasLoginLink) {
    state.confidence = state.isLoginPage ? 'high' : 'medium';
  } else if (state.isLoginPage) {
    state.confidence = 'medium';
  } else {
    state.confidence = 'low';
    log.info('No clear login state signals found on current page');
  }

  log.info(`Page state: loginPage=${state.isLoginPage}, ` +
    `dashboard=${state.hasDashboard}, loginForm=${state.hasLoginForm}, ` +
    `loginLink=${state.hasLoginLink}, ` +
    `confidence=${state.confidence}`);

  return state;
}

// ═════════════════════════════════════════════════════════════════════
// Session detection
// ═════════════════════════════════════════════════════════════════════

/**
 * Determine whether the user is currently logged in.
 *
 * Strategy — because Cloudflare blocks direct goto() to sub-pages:
 *   1. Navigate to the homepage (always works).
 *   2. If dashboard elements are visible → already logged in.
 *   3. If not, click the LOGIN link → should land on the login page.
 *   4. If login form appears → not logged in.
 *   5. If we get redirected away from /login → session is active.
 *
 * @param {import('playwright').Page} page
 * @returns {Promise<{ loggedIn: boolean, state: object }>}
 */
export async function checkSession(page) {
  log.info('Checking existing session...');

  // ── Strategy: go to homepage first ──────────────────────────────
  log.info(`Navigating to homepage (${HOME_URL}) to check session...`);
  const homeOk = await safeNavigate(page, HOME_URL, 30000);
  if (!homeOk) {
    log.warn('Could not load homepage — will attempt login anyway');
    return { loggedIn: false, state: { confidence: 'low' } };
  }
  // Wait for Angular SPA to bootstrap (wait for navigation elements to appear)
  try {
    await page.waitForSelector('#user-menu-login, a.login-btn, a[href*="/aviator"], .au-m-nav-u', { timeout: 15000 });
  } catch {
    log.warn('Navigation elements did not appear within timeout — page may not have rendered fully');
  }
  await page.waitForTimeout(1000);

  let state = await detectPageState(page);

  // Already logged in: we see dashboard signals and no login form
  if (state.hasDashboard && !state.hasLoginForm && !state.hasLoginLink) {
    log.info('Session is valid — dashboard elements detected on homepage');
    return { loggedIn: true, state };
  }

  // Login form visible on homepage (unlikely but handle it)
  if (state.hasLoginForm || state.hasLoginLink) {
    log.info('Login entry detected on homepage — session is not active');
    return { loggedIn: false, state };
  }

  // Ambiguous — click the LOGIN link to reach the login page
  log.info('No dashboard detected — clicking LOGIN link to check session...');
  const clicked = await goViaHomepage(page, LOGIN_LINK_SELECTOR, 'LOGIN');

  if (clicked) {
    // Wait for Angular app to render on the login page
    try {
      await page.waitForSelector('#phoneInput, #password, #user-menu-login, a.login-btn', { timeout: 30000 });
    } catch {
      log.warn('Login page elements did not render within timeout');
    }
    await page.waitForTimeout(1000);
    state = await detectPageState(page);

    // If we navigated to /login but the URL changed away → logged in
    if (!state.isLoginPage && state.hasDashboard) {
      log.info('Login page redirected — session is valid');
      return { loggedIn: true, state };
    }

    // Login form on the login page → not logged in
    if (state.isLoginPage || state.hasLoginForm) {
      log.info('Confirmed on login page — session is not active');
      return { loggedIn: false, state };
    }
  }

  // Final fallback: check URL for /login presence
  const url = page.url().toLowerCase();
  if (url.includes('/login')) {
    log.info('URL contains /login — assuming not logged in');
    return { loggedIn: false, state };
  }

  // Not on login page, no dashboard, no login form — optimistic guess
  log.warn('Could not definitively determine login state — assuming not logged in');
  return { loggedIn: false, state };
}

// ═════════════════════════════════════════════════════════════════════
// Conditional login
// ══════════════════════════════════════════════════��══════════════════

/**
 * Log into winner.rw, but ONLY if the login form actually exists on the page.
 *
 * Flow:
 *   1. Navigate to homepage and click LOGIN link (bypasses Cloudflare)
 *   2. Use page.$() to check for phone input — if missing, skip login
 *   3. Use page.$() to check for password input — if missing, skip login
 *   4. Fill credentials using page.fill()
 *   5. Use page.$() to check for submit button — if missing, skip
 *   6. Click submit and wait for navigation
 *
 * @param {import('playwright').Page} page
 * @param {string} phone
 * @param {string} password
 * @returns {Promise<boolean>} Whether login was successful
 */
export async function login(page, phone, password) {
  const currentUrl = page.url().toLowerCase();

  // ── Step 1: ensure we're on the login page ──────────────────────
  // Winner.rw blocks direct navigation to /login, so we go via the
  // homepage and click the LOGIN link if we're not already there.
  if (!currentUrl.includes('/login')) {
    log.info('Not on login page — navigating via homepage LOGIN link...');
    const clicked = await goViaHomepage(page, LOGIN_LINK_SELECTOR, 'LOGIN');
    if (!clicked) {
      // Fallback: try direct navigation (may fail with Cloudflare)
      log.warn('Could not click LOGIN link — trying direct navigation...');
      const ok = await safeNavigate(page, LOGIN_URL, 45000);
      if (!ok) {
        log.error('Could not reach login page via any method');
        return false;
      }
      await page.waitForTimeout(2000);
      // Wait for Angular SPA to render the login form
      try {
        await page.waitForSelector('#phoneInput, #password, input[type="password"]', { timeout: 30000 });
      } catch {
        log.warn('Login form elements did not render within timeout');
      }
      await page.waitForTimeout(1000);
    }
  } else {
    log.info('Already on login page');
  }

  // ── Step 2: safe-check for phone input ──────────────────────────
  const phoneSelector = '#phoneInput';
  let phoneEl = null;
  try {
    phoneEl = await page.$(phoneSelector);
  } catch { /* ignore */ }

  if (!phoneEl) {
    log.warn(`Phone input (${phoneSelector}) not found on page — checking if already logged in...`);
    const state = await detectPageState(page);
    if (state.hasDashboard) {
      log.info('Already logged in — no login needed');
      return true;
    }
    log.error(`Phone input (${phoneSelector}) not found and not logged in`);
    return false;
  }

  const phoneVisible = await phoneEl.isVisible().catch(() => false);
  if (!phoneVisible) {
    log.error(`Phone input (${phoneSelector}) exists but is not visible`);
    return false;
  }
  log.info('Phone input found and visible');

  // ── Step 3: safe-check for password input ───────────────────────
  const passwordSelector = '#password';
  let passEl = null;
  try {
    passEl = await page.$(passwordSelector);
  } catch { /* ignore */ }

  if (!passEl) {
    log.error(`Password input (${passwordSelector}) not found`);
    return false;
  }

  const passVisible = await passEl.isVisible().catch(() => false);
  if (!passVisible) {
    log.error(`Password input (${passwordSelector}) exists but is not visible`);
    return false;
  }
  log.info('Password input found and visible');

  // ── Step 4: safe-check for submit button ────────────────────────
  const submitSelector = '#buttonLoginSubmit, #buttonLoginSubmitLabel, button[type="submit"]';
  let submitEl = null;
  try {
    submitEl = await page.$(submitSelector);
  } catch { /* ignore */ }

  if (!submitEl) {
    log.error(`Login button (${submitSelector}) not found`);
    return false;
  }

  const submitVisible = await submitEl.isVisible().catch(() => false);
  if (!submitVisible) {
    log.error(`Login button (${submitSelector}) exists but is not visible`);
    return false;
  }
  log.info('Login button found and visible');

  // ── Step 5: fill credentials ────────────────────────────────────
  try {
    await phoneEl.click({ clickCount: 3 });
    await page.fill(phoneSelector, phone);
    log.info('Phone field filled');
  } catch (err) {
    log.error(`Failed to fill phone field: ${err.message}`);
    return false;
  }

  try {
    await passEl.click({ clickCount: 3 });
    await page.fill(passwordSelector, password);
    log.info('Password field filled');
  } catch (err) {
    log.error(`Failed to fill password field: ${err.message}`);
    return false;
  }

  // ── Step 6: submit ──────────────────────────────────────────────
  try {
    await submitEl.click();
    log.info('Login button clicked');
  } catch (err) {
    log.error(`Failed to click login button: ${err.message}`);
    return false;
  }

  // ── Step 7: wait for post-login navigation ──────────────────────
  log.info('Waiting for post-login navigation...');

  try {
    await page.waitForNavigation({ waitUntil: 'load', timeout: 20000 }).catch(() => {});
  } catch { /* ignore */ }
  await page.waitForTimeout(3000);

  // ── Step 8: verify ──────────────────────────────────────────────
  const postUrl = page.url().toLowerCase();
  if (postUrl.includes('/login')) {
    log.error('Still on login page after submit — credentials may be wrong');

    try {
      const errEl = await page.$('.alert-danger, .error, [class*="error"], [class*="alert"], .invalid-feedback');
      if (errEl) {
        const errText = (await errEl.innerText()).slice(0, 200);
        log.error(`Login error message: "${errText}"`);
      }
    } catch { /* ignore */ }

    return false;
  }

  log.info(`Login successful. Post-login URL: ${page.url()}`);
  return true;
}

// ═════════════════════════════════════════════════════════════════════
// Aviator navigation
// ═════════════════════════════════════════════════════════════════════

/**
 * Navigate to the Aviator crash game page.
 *
 * Uses the homepage-first approach to bypass Cloudflare blocking
 * direct navigation to the game URL.
 *
 * @param {import('playwright').Page} page
 * @returns {Promise<boolean>}
 */
export async function goToAviator(page) {
  log.info('Navigating to Aviator game...');

  // Try clicking the Aviator nav link from the current page first
  const clicked = await goViaHomepage(page, AVIATOR_LINK_SELECTOR, 'Aviator');

  if (!clicked) {
    log.warn('Could not click Aviator link — resetting through homepage and retrying link click...');
    const homeOk = await safeNavigate(page, HOME_URL, 30000);
    if (!homeOk) {
      log.error('Could not load homepage');
      return false;
    }
    await page.waitForTimeout(2000);
    const retry = await goViaHomepage(page, AVIATOR_LINK_SELECTOR, 'Aviator');
    if (!retry) {
      log.error('Failed to reach Aviator page through site navigation');
      return false;
    }
  }

  // Extra wait for the game canvas/widget to initialise
  log.info('Waiting for game canvas to initialize...');
  await page.waitForTimeout(5000);

  // Verify we're on a plausible game page using safe element check
  const currentUrl = page.url().toLowerCase();
  if (await isAccessDeniedPage(page)) {
    log.error('Aviator navigation reached an Access Denied page');
    return false;
  }

  if (currentUrl.includes('aviator') || currentUrl.includes('crash')) {
    log.info(`Aviator page loaded: ${currentUrl}`);
    return true;
  }

  if (currentUrl.includes('/login') || currentUrl.includes('/authentication')) {
    log.error(`Redirected to login instead of Aviator: ${currentUrl}`);
    return false;
  }

  log.error(`Unexpected URL after navigation: ${currentUrl}`);
  return false;
}




