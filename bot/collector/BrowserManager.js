/**
 * BrowserManager.js — owns the browser session for the collector.
 * It reuses an existing CDP browser when available and launches a managed
 * persistent Chromium profile when no attachable browser is running.
 */

import fs from 'fs';
import os from 'os';
import path from 'path';
import { fileURLToPath } from 'url';
import { chromium } from 'playwright-extra';
import StealthPlugin from 'puppeteer-extra-plugin-stealth';
import { log } from './Logger.js';
import { withRetry } from './RetryManager.js';

chromium.use(StealthPlugin());

const __dirname  = path.dirname(fileURLToPath(import.meta.url));
const ROOT       = path.resolve(__dirname, '..', '..');
const PROFILE_DIR = path.join(ROOT, 'data', 'bot', 'chrome-profile-new-email');
const GOOGLE_SNAPSHOT_DIR = path.join(ROOT, 'data', 'bot', 'chrome-google-fallback');
const GOOGLE_USER_DATA_DIR = process.env.BOT_GOOGLE_USER_DATA_DIR || path.join(os.homedir(), '.config', 'google-chrome');
const DEFAULT_CDP_PORT = process.env.BOT_CDP_PORT || '9222';

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
    if (!port) return null;
    return `http://127.0.0.1:${port}`;
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

function cdpEndpointCandidates(profileDir = PROFILE_DIR) {
  const candidates = [
    process.env.BOT_CDP_ENDPOINT,
    devToolsEndpointFromProfile(profileDir),
    devToolsEndpointFromProfile(GOOGLE_USER_DATA_DIR),
    `http://127.0.0.1:${DEFAULT_CDP_PORT}`,
    `http://localhost:${DEFAULT_CDP_PORT}`,
  ].filter(Boolean);
  return [...new Set(candidates)];
}

function buildLaunchOptions() {
  const playwrightExecutable = chromium.executablePath?.();
  const systemExecutables = [
    process.env.BOT_BROWSER_EXECUTABLE,
    '/bin/google-chrome',
    '/usr/bin/google-chrome',
    '/usr/bin/chromium',
    '/usr/bin/chromium-browser',
  ].filter(Boolean);
  const executablePath = systemExecutables.find(candidate => fs.existsSync(candidate)) ||
    (playwrightExecutable && fs.existsSync(playwrightExecutable) ? playwrightExecutable : undefined);
  return {
    executablePath,
    args: [
      `--remote-debugging-port=${DEFAULT_CDP_PORT}`,
      '--disable-blink-features=AutomationControlled',
      '--disable-breakpad',
      '--disable-crash-reporter',
      '--disable-crashpad',
      '--disable-dev-shm-usage',
      '--disable-background-timer-throttling',
      '--disable-backgrounding-occluded-windows',
      '--disable-renderer-backgrounding',
    ],
  };
}

function lastUsedGoogleProfile() {
  if (process.env.BOT_GOOGLE_PROFILE) return process.env.BOT_GOOGLE_PROFILE;
  try {
    const state = JSON.parse(fs.readFileSync(path.join(GOOGLE_USER_DATA_DIR, 'Local State'), 'utf-8'));
    return state?.profile?.last_used || 'Default';
  } catch {
    return 'Default';
  }
}

const CTX_OPTS = {
  viewport:          { width: 1366, height: 768 },
  locale:            'en-US',
  timezoneId:        'Africa/Kigali',
  ignoreHTTPSErrors: false,
};

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
        log.info(`BrowserManager: removed stale Chrome profile lock: ${target}`);
      }
    } catch (err) {
      log.warn(`BrowserManager: could not remove stale Chrome profile lock ${target}: ${err.message}`);
    }
  }
}

function profileOwnerPid(userDataDir) {
  const lockPath = path.join(userDataDir, 'SingletonLock');
  if (!pathExistsNoFollow(lockPath)) return null;
  const pid = singletonLockPid(lockPath);
  return pid && pidIsAlive(pid) ? pid : null;
}

function copyIfPresent(source, destination) {
  if (!pathExistsNoFollow(source)) return;
  fs.mkdirSync(path.dirname(destination), { recursive: true });
  fs.cpSync(source, destination, { recursive: true, force: true });
}

/**
 * Make an isolated, untracked snapshot of the Chrome authentication state.
 * This lets automation use an already logged-in profile without opening the
 * same live user-data directory twice (which Chrome deliberately forbids).
 */
function createGoogleProfileSnapshot(profileName) {
  const sourceProfile = path.join(GOOGLE_USER_DATA_DIR, profileName);
  if (!fs.existsSync(sourceProfile)) {
    throw new Error(`Google Chrome profile does not exist: ${sourceProfile}`);
  }

  fs.rmSync(GOOGLE_SNAPSHOT_DIR, { recursive: true, force: true });
  fs.mkdirSync(path.join(GOOGLE_SNAPSHOT_DIR, profileName), { recursive: true });
  copyIfPresent(
    path.join(GOOGLE_USER_DATA_DIR, 'Local State'),
    path.join(GOOGLE_SNAPSHOT_DIR, 'Local State'),
  );

  // Authentication and application state only; caches/history are omitted.
  for (const entry of [
    'Cookies', 'Network', 'Local Storage', 'Session Storage', 'IndexedDB',
    'WebStorage', 'Preferences', 'Secure Preferences', 'Login Data',
  ]) {
    copyIfPresent(
      path.join(sourceProfile, entry),
      path.join(GOOGLE_SNAPSHOT_DIR, profileName, entry),
    );
  }
  log.info(`BrowserManager: created isolated snapshot of Google profile ${profileName}`);
  return GOOGLE_SNAPSHOT_DIR;
}

export class BrowserManager {
  constructor(headless = false) {
    this.headless  = headless;
    this._context  = null;
    this._browser  = null;
    this._page     = null;
    this._ownsBrowser = false;
    this._profileDir = PROFILE_DIR;
    this._profileName = null;
    this.restartCount = 0;
  }

  /** True if the browser process is alive and connected. */
  isAlive() {
    try {
      if (this._browser) return this._browser.isConnected();
      return !!this._context?.browser?.()?.isConnected?.();
    } catch {
      return false;
    }
  }

  /** True if the context is still usable. */
  hasContext() {
    return !!this._context;
  }

  /** Connect to an existing browser or launch a managed browser. */
  async launch(signal, { reconnect = false } = {}) {
    if (this.isAlive() && this._context) {
      log.info('BrowserManager: reusing existing browser');
      return this._context;
    }

    await this._close();

    return withRetry(async () => {
      const connected = await this._connectExisting();
      if (connected) return connected;

      return this._launchManaged();
    }, { maxAttempts: reconnect ? 5 : 3, label: 'browser-launch', signal });
  }

  async _connectExisting() {
    for (const endpoint of cdpEndpointCandidates(this._profileDir)) {
      try {
        log.info(`BrowserManager: checking existing browser at ${endpoint}`);
        const browser = await chromium.connectOverCDP(endpoint, { timeout: 2500 });
        if (!browser.isConnected()) continue;

        const contexts = browser.contexts();
        const context = contexts.find(ctx => ctx.pages().some(isAviatorPage)) || contexts[0];
        if (!context) {
          continue;
        }

        this._browser = browser;
        this._context = context;
        this._page = context.pages().find(page => !page.isClosed() && isAviatorPage(page)) || null;
        this._ownsBrowser = false;

        browser.once('disconnected', () => {
          log.warn('BrowserManager: CDP browser connection lost');
          this._browser = null;
          this._context = null;
          this._page = null;
          this._ownsBrowser = false;
        });

        log.info(this._page
          ? `BrowserManager: connected to existing browser and reusing Aviator tab: ${this._page.url()}`
          : 'BrowserManager: connected to existing browser; Aviator tab not open');
        return this._context;
      } catch (err) {
        log.info(`BrowserManager: no CDP browser at ${endpoint}: ${err.message}`);
      }
    }
    return null;
  }

  async _launchManaged() {
    log.info(`BrowserManager: launching managed Chromium (headless=${this.headless}, restart #${this.restartCount})`);
    fs.mkdirSync(this._profileDir, { recursive: true });
    clearStaleProfileLocks(this._profileDir);

    const launchOptions = buildLaunchOptions();
    log.info(`BrowserManager: executable=${launchOptions.executablePath || 'Playwright default'}`);
    if (this._profileName) launchOptions.args.push(`--profile-directory=${this._profileName}`);
    this._context = await chromium.launchPersistentContext(this._profileDir, {
      ...launchOptions,
      ...CTX_OPTS,
      headless: this.headless,
    });

    this._browser = this._context.browser();
    this._page = this._context.pages().find(page => !page.isClosed() && isAviatorPage(page)) ||
      this._context.pages().find(page => !page.isClosed()) ||
      null;
    this._ownsBrowser = true;
    this.restartCount += 1;

    this._context.once('close', () => {
      log.warn('BrowserManager: managed context closed');
      this._context = null;
      this._browser = null;
      this._page = null;
      this._ownsBrowser = false;
    });

    log.info('BrowserManager: managed browser ready');
    return this._context;
  }

  /** Get or create a page. Reuses an existing Aviator tab before creating anything. */
  async getPage() {
    if (!this._context || !this.isAlive()) await this.launch();
    const pages = this._context.pages().filter(page => !page.isClosed());
    const aviatorPage = pages.find(isAviatorPage);
    if (aviatorPage) {
      this._page = aviatorPage;
      await aviatorPage.bringToFront().catch(() => {});
      return aviatorPage;
    }
    if (this._page && !this._page.isClosed()) return this._page;
    this._page = await this._context.newPage();
    return this._page;
  }

  /** Close everything cleanly. */
  async _close() {
    if (this._context) {
      if (this._ownsBrowser) {
        try { await this._context.close(); } catch {}
      }
      this._context = null;
    }
    if (this._browser) {
      if (this._ownsBrowser) {
        try { await this._browser.close(); } catch {}
      }
      this._browser = null;
    }
    this._page = null;
    this._ownsBrowser = false;
  }

  /** Release this manager's resources. Attached user browsers are left open. */
  async close() {
    await this._close();
  }

  /** Reconnect or relaunch after browser connection loss. */
  async restart(signal) {
    log.warn('BrowserManager: restarting browser session');
    await this._close();
    this.restartCount += 1;
    return this.launch(signal, { reconnect: true });
  }

  /** Switch to the user's last-used Google Chrome profile after bot-profile failure. */
  async restartWithGoogleProfile(signal) {
    await this._close();
    this._profileName = lastUsedGoogleProfile();
    const ownerPid = profileOwnerPid(GOOGLE_USER_DATA_DIR);
    if (ownerPid) {
      log.warn(`BrowserManager: Google Chrome profile is live in PID ${ownerPid}; using an isolated session snapshot`);
      this._profileDir = createGoogleProfileSnapshot(this._profileName);
    } else {
      this._profileDir = GOOGLE_USER_DATA_DIR;
    }
    log.warn(`BrowserManager: falling back to Google Chrome profile ${this._profileName}`);
    try {
      return await this.launch(signal, { reconnect: true });
    } catch (err) {
      throw new Error(
        `Google Chrome profile ${this._profileName} could not be opened. ` +
        `Close normal Chrome first, or start it with remote debugging on port ${DEFAULT_CDP_PORT}. ` +
        `Cause: ${err.message}`,
      );
    }
  }

  /**
   * Launch a completely fresh, non-persistent Chrome context. This behaves
   * like an incognito session: no profile locks, stale tabs, or cached site
   * state. LoginManager will visit winner.rw and authenticate normally.
   */
  async restartIncognito(signal) {
    await this._close();
    if (signal?.aborted) throw new Error('Aborted');

    const launchOptions = buildLaunchOptions();
    // A separate temporary browser does not need a public CDP port and must
    // not collide with an existing Chrome process already using port 9222.
    launchOptions.args = launchOptions.args.filter(arg => !arg.startsWith('--remote-debugging-port='));
    log.warn('BrowserManager: launching fresh incognito Chrome fallback');

    this._browser = await chromium.launch({
      ...launchOptions,
      headless: this.headless,
    });
    this._context = await this._browser.newContext(CTX_OPTS);
    this._page = await this._context.newPage();
    this._ownsBrowser = true;
    this.restartCount += 1;

    this._browser.once('disconnected', () => {
      log.warn('BrowserManager: incognito browser connection lost');
      this._browser = null;
      this._context = null;
      this._page = null;
      this._ownsBrowser = false;
    });
    return this._context;
  }
}
