/**
 * BrowserManager.js — Owns the Chromium process lifecycle.
 * Only restarts the browser when it is truly dead.
 * All other recovery paths reuse the existing browser.
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
const PROFILE_DIR = path.join(ROOT, 'data', 'bot', 'chrome-profile');

const LAUNCH_OPTS = {
  channel: 'chrome',
  args: [
    '--disable-blink-features=AutomationControlled',
    '--no-sandbox',
    '--disable-dev-shm-usage',
    '--disable-setuid-sandbox',
    '--disable-web-security',
    '--disable-features=IsolateOrigins,site-per-process',
    '--disable-background-timer-throttling',
    '--disable-backgrounding-occluded-windows',
    '--disable-renderer-backgrounding',
  ],
};

const CTX_OPTS = {
  viewport:          { width: 1366, height: 768 },
  userAgent:         'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36',
  locale:            'en-US',
  timezoneId:        'Africa/Kigali',
  bypassCSP:         true,
  ignoreHTTPSErrors: false,
};

export class BrowserManager {
  constructor(headless = false) {
    this.headless  = headless;
    this._context  = null;
    this._browser  = null;
    this.restartCount = 0;
  }

  /** True if the browser process is alive and connected. */
  isAlive() {
    try {
      return !!(this._browser?.isConnected?.() ?? false) ||
             !!(this._context && !this._context.browser()?.isConnected?.() === false);
    } catch {
      return false;
    }
  }

  /** True if the context is still usable. */
  hasContext() {
    return !!this._context;
  }

  /**
   * Launch (or reuse) the browser.
   * Uses a persistent context so the Chrome profile persists across restarts.
   */
  async launch(signal) {
    if (this.isAlive() && this._context) {
      log.info('BrowserManager: reusing existing browser');
      return this._context;
    }

    await this._close();

    return withRetry(async () => {
      log.info(`BrowserManager: launching Chromium (headless=${this.headless}, restart #${this.restartCount})`);
      fs.mkdirSync(PROFILE_DIR, { recursive: true });

      this._context = await chromium.launchPersistentContext(PROFILE_DIR, {
        ...LAUNCH_OPTS,
        ...CTX_OPTS,
        headless: this.headless,
      });

      this._browser = this._context.browser();
      this.restartCount++;

      // Detect unexpected browser disconnect
      this._context.once('close', () => {
        log.warn('BrowserManager: context closed unexpectedly');
        this._context = null;
        this._browser = null;
      });

      log.info('BrowserManager: browser ready');
      return this._context;
    }, { maxAttempts: 5, label: 'browser-launch', signal });
  }

  /** Get or create a page. Reuses the first open page if available. */
  async getPage() {
    if (!this._context) throw new Error('Browser not launched');
    const pages = this._context.pages();
    if (pages.length > 0 && !pages[0].isClosed()) return pages[0];
    return this._context.newPage();
  }

  /** Close everything cleanly. */
  async _close() {
    if (this._context) {
      try { await this._context.close(); } catch {}
      this._context = null;
    }
    if (this._browser) {
      try { await this._browser.close(); } catch {}
      this._browser = null;
    }
  }

  /** Full browser restart — only called when browser is truly dead. */
  async restart(signal) {
    log.warn('BrowserManager: full browser restart');
    await this._close();
    return this.launch(signal);
  }
}
