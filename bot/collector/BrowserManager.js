/**
 * BrowserManager.js — owns the browser session for the collector.
 * It reuses an existing CDP browser when available and launches a managed
 * persistent Chromium profile when no attachable browser is running.
 */

import fs from 'fs';
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

function cdpEndpointCandidates() {
  const candidates = [
    process.env.BOT_CDP_ENDPOINT,
    devToolsEndpointFromProfile(PROFILE_DIR),
    process.env.BOT_CDP_ENDPOINT ? `http://127.0.0.1:${DEFAULT_CDP_PORT}` : null,
    process.env.BOT_CDP_ENDPOINT ? `http://localhost:${DEFAULT_CDP_PORT}` : null,
  ].filter(Boolean);
  return [...new Set(candidates)];
}

function buildLaunchOptions() {
  return {
    executablePath: chromium.executablePath?.() || undefined,
    args: [
      `--remote-debugging-port=${DEFAULT_CDP_PORT}`,
      '--disable-blink-features=AutomationControlled',
      '--disable-breakpad',
      '--disable-crash-reporter',
      '--disable-crashpad',
      '--disable-dev-shm-usage',
      '--disable-web-security',
      '--disable-features=IsolateOrigins,site-per-process',
      '--disable-background-timer-throttling',
      '--disable-backgrounding-occluded-windows',
      '--disable-renderer-backgrounding',
    ],
  };
}

const CTX_OPTS = {
  viewport:          { width: 1366, height: 768 },
  userAgent:         'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36',
  locale:            'en-US',
  timezoneId:        'Africa/Kigali',
  bypassCSP:         true,
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

export class BrowserManager {
  constructor(headless = false) {
    this.headless  = headless;
    this._context  = null;
    this._browser  = null;
    this._page     = null;
    this._ownsBrowser = false;
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
    }, { maxAttempts: reconnect ? 5 : 1, label: 'browser-launch', signal });
  }

  async _connectExisting() {
    for (const endpoint of cdpEndpointCandidates()) {
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
    fs.mkdirSync(PROFILE_DIR, { recursive: true });
    clearStaleProfileLocks(PROFILE_DIR);

    this._context = await chromium.launchPersistentContext(PROFILE_DIR, {
      ...buildLaunchOptions(),
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

  /** Reconnect or relaunch after browser connection loss. */
  async restart(signal) {
    log.warn('BrowserManager: restarting browser session');
    await this._close();
    this.restartCount += 1;
    return this.launch(signal, { reconnect: true });
  }
}
