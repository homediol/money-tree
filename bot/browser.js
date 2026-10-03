/**
 * Legacy browser API. Lifecycle calls delegate to the collector's single
 * process-wide BrowserSupervisor; this module only keeps navigation helpers.
 *
 * Exports robust waiting primitives:
 *   - waitForSelector   (with fallback to waitForTimeout)
 *   - waitForNavigation (with fallback to waitForTimeout)
 *   - safeNavigate      (retry + automatic wait strategy selection)
 */

import path from 'path';
import { fileURLToPath } from 'url';
import { createLogger } from './logger.js';
import { getBrowserSupervisor } from './collector/BrowserManager.js';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(__dirname, '..');
const DEFAULT_STORAGE_STATE = path.join(ROOT, 'data', 'bot', 'storageState.json');

const log = createLogger('browser');

// ═════════════════════════════════════════════════════════════════════
// State
// ═════════════════════════════════════════════════════════════════════

let _browser = null;        // Browser instance
let _context = null;        // BrowserContext
let _page    = null;        // Active Page
let _ownsBrowser = false;   // False when attached to an existing CDP browser
let _lastLaunchOptions = {};

function resolveStorageStatePath() {
  return process.env.BOT_STORAGE_STATE || DEFAULT_STORAGE_STATE;
}

// ═════════════════════════════════════════════════════════════════════
// Browser lifecycle
// ═════════════════════════════════════════════════════════════════════

/**
 * Launch or connect to a Chromium session.
 *
 * @param {object}  options
 * @param {boolean} [options.headless=false]     Run headlessly
 * @param {string}  [options.userDataDir]         Chrome profile directory
 * @param {boolean} [options.useStorageState]     Use storageState.json instead of profile
 * @param {string}  [options.storageStatePath]    Custom storageState.json path
 * @returns {{ browser: Browser, context: BrowserContext, page: Page }}
 */
export async function launchBrowser(options = {}) {
  _lastLaunchOptions = { ...options };
  const supervisor = getBrowserSupervisor(options.headless === true);
  await supervisor.launch();
  const page = await supervisor.getPage();
  _browser = supervisor._browser;
  _context = supervisor._context;
  _page = page;
  _ownsBrowser = supervisor._ownsBrowser;
  return { browser: _browser, context: _context, page };
}

/**
 * Save current storage state (cookies + localStorage) to a JSON file.
 * Only meaningful when using non-persistent context (useStorageState=true).
 *
 * @param {string} [filePath]  Defaults to data/bot/storageState.json
 */
export async function saveStorageState(filePath) {
  const supervisor = getBrowserSupervisor();
  if (!supervisor._context) {
    log.warn('No active context — cannot save storage state');
    return;
  }
  const target = filePath || resolveStorageStatePath();
  try {
    await supervisor._context.storageState({ path: target });
    await supervisor.persistSession(_page);
    log.info(`Storage state saved to ${target}`);
  } catch (err) {
    log.error(`Failed to save storage state: ${err.message}`);
  }
}

/**
 * Get the current active page. Creates one if none exists.
 */
export async function getPage() {
  const supervisor = getBrowserSupervisor(_lastLaunchOptions.headless === true);
  _page = await supervisor.getPage();
  _context = supervisor._context;
  _browser = supervisor._browser;
  return _page;
}

// ═════════════════════════════════════════════════════════════════════
// Waiting strategies
// ═════════════════════════════════════════════════════════════════════

/**
 * Wait for a DOM selector to become visible.
 *
 * Safety-first approach:
 *   1. First check existence with page.$() (fast, non-blocking).
 *      If the element exists immediately, return true right away.
 *   2. If not found, wait up to `timeout` ms via official waitForSelector.
 *   3. If that times out as well, do one final page.$() fallback after
 *      a short delay for SPAs that render late.
 *
 * The page.$() first check prevents unnecessary waiting when the element
 * is already present but wouldn't trigger Playwright's observer (e.g.,
 * if the element existed before the waitForSelector call was made).
 *
 * @param {Page}   page
 * @param {string} selector
 * @param {number} [timeout=15000]
 * @returns {boolean}  Whether the selector was found
 */
export async function waitForSelector(page, selector, timeout = 15000) {
  // ── Phase 1: fast existence check (page.$, non-blocking) ──────
  try {
    const immediate = await page.$(selector);
    if (immediate) {
      const visible = await immediate.isVisible().catch(() => true);
      if (visible) {
        log.info(`Selector "${selector}" found immediately via page.$()`);
        return true;
      }
    }
  } catch {
    // ignore and proceed to waitForSelector
  }

  // ── Phase 2: official Playwright wait ──────────────────────────
  try {
    await page.waitForSelector(selector, { state: 'visible', timeout });
    return true;
  } catch (primaryErr) {
    log.warn(`waitForSelector timed out for "${selector}" after ${timeout}ms: ${primaryErr.message}`);
  }

  // ── Phase 3: final fallback — brief delay + direct query ───────
  try {
    log.info(`Fallback: waiting 2s then querying "${selector}" directly`);
    await page.waitForTimeout(2000);
    const el = await page.$(selector);
    if (el) {
      log.info(`Fallback: found "${selector}" via direct query`);
      return true;
    }
  } catch (fallbackErr) {
    log.warn(`Fallback also failed for "${selector}": ${fallbackErr.message}`);
  }

  log.error(`Selector "${selector}" not found after all strategies`);
  return false;
}

/**
 * Wait for the page to navigate to a new URL (or reload).
 *
 * Primary: waitForNavigation (triggers on 'load' event).
 * Fallback: waitForTimeout and check URL manually.
 *
 * @param {Page}       page
 * @param {object}     [options]
 * @param {string}     [options.url]        Wait for a specific URL (substring match)
 * @param {number}     [options.timeout=20000]
 * @param {function}   [options.trigger]    Async function that triggers the navigation
 * @returns {boolean}  Whether the navigation completed successfully
 */
export async function waitForNavigation(page, options = {}) {
  const timeout    = options.timeout || 20000;
  const targetUrl  = options.url     || null;
  const trigger    = options.trigger || null;

  // Primary strategy — waitForNavigation
  try {
    if (trigger) {
      // Use Promise.all to trigger + wait concurrently
      await Promise.all([
        page.waitForNavigation({ waitUntil: 'load', timeout }),
        trigger(),
      ]);
    } else {
      await page.waitForNavigation({ waitUntil: 'load', timeout });
    }

    if (targetUrl) {
      const current = page.url().toLowerCase();
      if (current.includes(targetUrl.toLowerCase())) {
        log.info(`Navigation confirmed to URL containing "${targetUrl}": ${current}`);
        return true;
      }
      log.warn(`Navigation completed but URL "${current}" does not match "${targetUrl}"`);
      return false;
    }

    log.info('Navigation completed successfully');
    return true;
  } catch (primaryErr) {
    log.warn(`waitForNavigation primary failed: ${primaryErr.message}`);
  }

  // Fallback strategy — wait for a few seconds and check URL manually
  try {
    log.info('Fallback: waiting 4s then checking URL');
    await page.waitForTimeout(4000);

    if (targetUrl) {
      const current = page.url().toLowerCase();
      if (current.includes(targetUrl.toLowerCase())) {
        log.info(`Fallback navigation confirmed: ${current}`);
        return true;
      }
      log.warn(`Fallback: URL "${current}" still does not contain "${targetUrl}"`);
      return false;
    }

    // No target URL specified — assume navigation happened if page is interactive
    return true;
  } catch (fallbackErr) {
    log.error(`Navigation fallback also failed: ${fallbackErr.message}`);
    return false;
  }
}

/**
 * Navigate to a URL with retry and multiple waiting strategies.
 *
 * Strategies (in order):
 *   1. goto with 'load' event
 *   2. goto with 'domcontentloaded' event (if load times out)
 *   3. goto with 'commit' event (if page still hangs)
 *
 * @param {Page}   page
 * @param {string} url
 * @param {number} [timeout=60000]
 * @returns {boolean}  success
 */
export async function safeNavigate(page, url, timeout = 60000) {
  const waitStrategies = ['load', 'domcontentloaded', 'commit'];

  for (let attempt = 1; attempt <= 3; attempt++) {
    const waitUntil = waitStrategies[attempt - 1];
    try {
      log.info(`Navigating to: ${url} (attempt ${attempt}, wait=${waitUntil})`);
      await page.goto(url, { waitUntil, timeout });

      const currentUrl = page.url();
      log.info(`Page loaded: ${currentUrl}`);

      // Quick sanity check — page should have some content
      const bodyOk = await waitForSelector(page, 'body', 5000);
      if (!bodyOk) {
        log.warn('Page loaded but body selector not found — may be blank');
        if (attempt < 3) continue;
        return false;
      }

      // Check for browser error pages
      const pageText = (await page.innerText('body').catch(() => '')).toLowerCase();
      if (pageText.includes('accessdenied') || pageText.includes('access denied')) {
        log.warn(`Navigation reached an Access Denied page for ${url}`);
        return false;
      }

      if (pageText.includes('err_') || pageText.includes('this site can') ||
          pageText.includes('404') || pageText.includes('not found')) {
        log.warn(`Page may have an error: "${pageText.slice(0, 120)}"`);
        if (attempt < 3) {
          log.info('Waiting 5s before retry...');
          await page.waitForTimeout(5000);
          continue;
        }
        return false;
      }

      return true;
    } catch (err) {
      const isTimeout = err.message && (
        err.message.includes('timeout') || err.message.includes('Timeout') ||
        err.message.includes('TimeoutError') || err.message.includes('net::ERR_')
      );

      if (isTimeout) {
        log.warn(`Navigation attempt ${attempt} timed out for ${url}`);
      } else if (err.message && err.message.includes('net::ERR_INTERNET_DISCONNECTED')) {
        log.error('Network failure: internet disconnected');
        return false;
      } else if (err.message && err.message.includes('net::ERR_CONNECTION_REFUSED')) {
        log.error(`Network failure: connection refused to ${url}`);
        return false;
      } else if (err.message && err.message.includes('net::ERR_NAME_NOT_RESOLVED')) {
        log.error(`Network failure: DNS resolution failed for ${url}`);
        return false;
      } else if (err.message && err.message.includes('net::ERR_SSL')) {
        log.error(`Network failure: SSL error for ${url}`);
        return false;
      } else {
        log.error(`Navigation failed to ${url} (attempt ${attempt}/3): ${err.message}`);
      }

      if (attempt < 3) {
        const backoff = attempt * 3000;
        log.info(`Waiting ${backoff}ms before retry...`);
        await page.waitForTimeout(backoff);
      }
    }
  }

  log.error(`All navigation attempts failed for ${url}`);
  return false;
}

// ═════════════════════════════════════════════════════════════════════
// Cleanup
// ═════════════════════════════════════════════════════════════════════

/**
 * Close the browser session gracefully.
 *
 * Attempts to save storage state before closing if using storageState mode.
 */
export async function closeBrowser(options = {}) {
  const { saveState = !!process.env.BOT_STORAGE_STATE } = options;
  const supervisor = getBrowserSupervisor(_lastLaunchOptions.headless === true);
  if (saveState && _context) await saveStorageState();
  await supervisor.close();
  _browser = null;
  _context = null;
  _page = null;
  _ownsBrowser = false;
  log.info('Browser resources released');
}
