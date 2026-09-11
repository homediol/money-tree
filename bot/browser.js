/**
 * Browser Manager — launches or connects to a Chromium session.
 *
 * It first tries to attach to a manually started Chrome CDP session so open
 * tabs and login state can be reused. If none is available, it launches its
 * own persistent Playwright browser profile.
 *
 * Exports robust waiting primitives:
 *   - waitForSelector   (with fallback to waitForTimeout)
 *   - waitForNavigation (with fallback to waitForTimeout)
 *   - safeNavigate      (retry + automatic wait strategy selection)
 */

import { chromium } from 'playwright-extra';
import StealthPlugin from 'puppeteer-extra-plugin-stealth';

// Apply stealth plugin to bypass Cloudflare bot detection
chromium.use(StealthPlugin());
import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';
import { createLogger } from './logger.js';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(__dirname, '..');
const DEFAULT_BOT_PROFILE   = path.join(ROOT, 'data', 'bot', 'chrome-profile-new-email');
const DEFAULT_STORAGE_STATE = path.join(ROOT, 'data', 'bot', 'storageState.json');
const DEFAULT_CDP_PORT = process.env.BOT_CDP_PORT || '9222';

const log = createLogger('browser');

// ═════════════════════════════════════════════════════════════════════
// State
// ═════════════════════════════════════════════════════════════════════

let _browser = null;        // Browser instance
let _context = null;        // BrowserContext
let _page    = null;        // Active Page
let _ownsBrowser = false;   // False when attached to an existing CDP browser
let _lastLaunchOptions = {};

function isAviatorPage(page) {
  try {
    const url = page.url().toLowerCase();
    return url.includes('aviator') || url.includes('crash-games') || url.includes('/crash');
  } catch {
    return false;
  }
}

function devToolsEndpointFromProfile(profileDir) {
  try {
    const file = path.join(profileDir, 'DevToolsActivePort');
    if (!fs.existsSync(file)) return null;
    const [port] = fs.readFileSync(file, 'utf-8').trim().split(/\r?\n/);
    return port ? `http://127.0.0.1:${port}` : null;
  } catch {
    return null;
  }
}

function devToolsEndpointsFromFile(file) {
  try {
    if (!fs.existsSync(file)) return [];
    const [port, browserPath] = fs.readFileSync(file, 'utf-8').trim().split(/\r?\n/);
    if (!port) return [];
    return [
      `http://127.0.0.1:${port}`,
      `http://localhost:${port}`,
      browserPath ? `ws://127.0.0.1:${port}${browserPath}` : null,
      browserPath ? `ws://localhost:${port}${browserPath}` : null,
    ].filter(Boolean);
  } catch {
    return [];
  }
}

function findDevToolsFiles(dir, depth = 3) {
  if (depth < 0) return [];
  try {
    return fs.readdirSync(dir, { withFileTypes: true }).flatMap(entry => {
      const fullPath = path.join(dir, entry.name);
      if (entry.isFile() && entry.name === 'DevToolsActivePort') return [fullPath];
      if (entry.isDirectory()) return findDevToolsFiles(fullPath, depth - 1);
      return [];
    });
  } catch {
    return [];
  }
}

function cdpEndpointCandidates(userDataDir) {
  const candidates = [
    process.env.BOT_CDP_ENDPOINT,
    devToolsEndpointFromProfile(userDataDir),
    process.env.BOT_CDP_ENDPOINT ? `http://127.0.0.1:${DEFAULT_CDP_PORT}` : null,
    process.env.BOT_CDP_ENDPOINT ? `http://localhost:${DEFAULT_CDP_PORT}` : null,
  ].filter(Boolean);
  return [...new Set(candidates)];
}

async function connectToExistingBrowser(userDataDir) {
  for (const endpoint of cdpEndpointCandidates(userDataDir)) {
    try {
      log.info(`Checking existing browser at ${endpoint}`);
      const browser = await chromium.connectOverCDP(endpoint, { timeout: 2500 });
      if (!browser.isConnected()) continue;

      const contexts = browser.contexts();
      const context = contexts.find(ctx => ctx.pages().some(isAviatorPage)) || contexts[0];
      if (!context) {
        continue;
      }

      _browser = browser;
      _context = context;
      _page = context.pages().find(page => !page.isClosed() && isAviatorPage(page)) || null;
      _ownsBrowser = false;

      _browser.once('disconnected', () => {
        log.warn('Existing browser CDP connection lost; will reconnect on next browser request');
        _browser = null;
        _context = null;
        _page = null;
        _ownsBrowser = false;
      });

      log.info(_page
        ? `Connected to existing browser and reusing Aviator tab: ${_page.url()}`
        : 'Connected to existing browser; Aviator tab not open');
      return true;
    } catch (err) {
      log.info(`No CDP browser at ${endpoint}: ${err.message}`);
    }
  }
  return false;
}

function buildLaunchOptions(headless, executablePath) {
  const installedExecutable = executablePath || chromium.executablePath?.();
  return {
    executablePath: installedExecutable || undefined,
    headless,
    args: [
      `--remote-debugging-port=${DEFAULT_CDP_PORT}`,
      '--disable-blink-features=AutomationControlled',
      '--disable-dev-shm-usage',
      '--disable-web-security',
      '--disable-features=IsolateOrigins,site-per-process',
      '--disable-background-timer-throttling',
      '--disable-backgrounding-occluded-windows',
      '--disable-renderer-backgrounding',
    ],
  };
}

function buildContextOptions() {
  return {
    viewport: { width: 1366, height: 768 },
    userAgent:
      'Mozilla/5.0 (Windows NT 10.0; Win64; x64) ' +
      'AppleWebKit/537.36 (KHTML, like Gecko) ' +
      'Chrome/125.0.0.0 Safari/537.36',
    locale: 'en-US',
    timezoneId: 'Africa/Kigali',
    bypassCSP: true,
    ignoreHTTPSErrors: false,
  };
}

function pidIsAlive(pid) {
  if (!pid || Number.isNaN(pid)) return false;
  try {
    process.kill(pid, 0);
    return true;
  } catch (err) {
    return err.code !== 'ESRCH';
  }
}

function pathExistsNoFollow(target) {
  try {
    fs.lstatSync(target);
    return true;
  } catch {
    return false;
  }
}

function singletonLockPid(lockPath) {
  try {
    const linkTarget = fs.readlinkSync(lockPath);
    const match = linkTarget.match(/-(\d+)$/);
    return match ? Number(match[1]) : null;
  } catch (linkErr) {
    try {
      const content = fs.readFileSync(lockPath, 'utf-8').trim();
      const match = content.match(/(?:^|[^\d])(\d+)(?:[^\d]|$)/);
      return match ? Number(match[1]) : null;
    } catch {
      return null;
    }
  }
}

function clearStaleProfileLocks(userDataDir) {
  const lockPath = path.join(userDataDir, 'SingletonLock');
  if (!pathExistsNoFollow(lockPath)) return;

  const pid = singletonLockPid(lockPath);
  if (!pid) {
    throw new Error(`Chrome profile lock exists but PID could not be read: ${lockPath}`);
  }
  if (pidIsAlive(pid)) {
    throw new Error(`Chrome profile is already in use by PID ${pid}`);
  }

  for (const name of ['SingletonLock', 'SingletonSocket', 'SingletonCookie']) {
    const target = path.join(userDataDir, name);
    try {
      if (pathExistsNoFollow(target)) {
        fs.rmSync(target, { force: true, recursive: true });
        log.info(`Removed stale Chrome profile lock: ${target}`);
      }
    } catch (err) {
      log.warn(`Could not remove stale Chrome profile lock ${target}: ${err.message}`);
    }
  }
}

async function launchManagedBrowser(options, userDataDir) {
  const headless = options.headless === true;
  const useStorageState = options.useStorageState === true;
  const launchOpts = buildLaunchOptions(headless, options.executablePath);
  const contextOpts = buildContextOptions();

  fs.mkdirSync(userDataDir, { recursive: true });
  clearStaleProfileLocks(userDataDir);
  log.info(`Launching managed Chromium (headless=${headless}, persistence=${useStorageState ? 'storageState' : 'profile'})`);

  if (useStorageState) {
    const storageStatePath = options.storageStatePath || resolveStorageStatePath();
    if (fs.existsSync(storageStatePath)) {
      log.info(`Loading storage state from ${storageStatePath}`);
      contextOpts.storageState = storageStatePath;
    } else {
      log.info('No storage state file found; starting with a fresh context');
    }

    _browser = await chromium.launch(launchOpts);
    _context = await _browser.newContext(contextOpts);
  } else {
    _context = await chromium.launchPersistentContext(userDataDir, {
      ...launchOpts,
      ...contextOpts,
    });
    _browser = _context.browser();
  }

  const pages = _context.pages().filter(page => !page.isClosed());
  _page = pages.find(isAviatorPage) || pages[0] || await _context.newPage();
  _ownsBrowser = true;

  _browser?.once?.('disconnected', () => {
    log.warn('Managed browser disconnected');
    _browser = null;
    _context = null;
    _page = null;
    _ownsBrowser = false;
  });

  log.info('Managed Chromium launched successfully');
  return { browser: _browser, context: _context, page: _page };
}

// ═════════════════════════════════════════════════════════════════════
// Profile resolution
// ═════════════════════════════════════════════════════════════════════

/**
 * Resolve the Chrome user-data directory.
 *
 * Priority:
 *   1. BOT_CHROME_USER_DATA env var
 *   2. User's existing Chrome profile (~/.config/google-chrome)
 *   3. A dedicated bot profile inside data/bot/chrome-profile-new-email
 */
function resolveUserDataDir() {
  const envDir = process.env.BOT_CHROME_USER_DATA;
  if (envDir) {
    log.info(`Using Chrome profile from BOT_CHROME_USER_DATA: ${envDir}`);
    return envDir;
  }

  // Use a dedicated bot profile to avoid conflicts when system Chrome is running
  log.info(`Using bot profile at ${DEFAULT_BOT_PROFILE}`);
  fs.mkdirSync(DEFAULT_BOT_PROFILE, { recursive: true });
  return DEFAULT_BOT_PROFILE;
}

/**
 * Resolve the storage state file path.
 */
function resolveStorageStatePath() {
  const envPath = process.env.BOT_STORAGE_STATE;
  if (envPath) return envPath;
  fs.mkdirSync(path.dirname(DEFAULT_STORAGE_STATE), { recursive: true });
  return DEFAULT_STORAGE_STATE;
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
  if (_browser?.isConnected?.() && _context && _page && !_page.isClosed()) {
    log.info('Reusing existing browser session');
    return { browser: _browser, context: _context, page: _page };
  }

  const userDataDir    = options.userDataDir || resolveUserDataDir();

  if (await connectToExistingBrowser(userDataDir)) {
    return { browser: _browser, context: _context, page: await getPage() };
  }

  return launchManagedBrowser(options, userDataDir);
}

/**
 * Save current storage state (cookies + localStorage) to a JSON file.
 * Only meaningful when using non-persistent context (useStorageState=true).
 *
 * @param {string} [filePath]  Defaults to data/bot/storageState.json
 */
export async function saveStorageState(filePath) {
  if (!_context) {
    log.warn('No active context — cannot save storage state');
    return;
  }
  const target = filePath || resolveStorageStatePath();
  const dir = path.dirname(target);
  fs.mkdirSync(dir, { recursive: true });

  try {
    await _context.storageState({ path: target });
    log.info(`Storage state saved to ${target}`);
  } catch (err) {
    log.error(`Failed to save storage state: ${err.message}`);
  }
}

/**
 * Get the current active page. Creates one if none exists.
 */
export async function getPage() {
  if (!_context || (_browser && !_browser.isConnected?.())) {
    await launchBrowser(_lastLaunchOptions);
  }
  if (_context) {
    const pages = _context.pages().filter(page => !page.isClosed());
    const aviatorPage = pages.find(isAviatorPage);
    if (aviatorPage) {
      _page = aviatorPage;
      await aviatorPage.bringToFront().catch(() => {});
      return _page;
    }
    if (_page && !_page.isClosed()) return _page;
    _page = await _context.newPage();
    return _page;
  }
  throw new Error('Browser not launched. Call launchBrowser() first.');
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

  if (saveState) {
    await saveStorageState();
  }

  if (_page) {
    try {
      _page = null;
    } catch { /* ignore */ }
  }

  if (_context) {
    try {
      if (_ownsBrowser) {
        await _context.close();
        log.info('Browser context closed');
      } else {
        log.info('Detached from existing browser context');
      }
    } catch (err) {
      log.warn(`Error closing context: ${err.message}`);
    }
    _context = null;
  }

  if (_browser) {
    try {
      if (_ownsBrowser) {
        await _browser.close();
        log.info('Browser process terminated');
      } else {
        log.info('Left existing browser process running');
      }
    } catch (err) {
      log.warn(`Error closing browser: ${err.message}`);
    }
    _browser = null;
  }
  _ownsBrowser = false;

  log.info('Browser resources released');
}
