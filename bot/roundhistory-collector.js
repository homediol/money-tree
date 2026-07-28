#!/usr/bin/env node

/**
 * roundhistory-collector.js  —  Aviator Round History Collector
 * ==================================================================
 *
 * A standalone Playwright bot for Winner.rw Aviator that captures round
 * multipliers in real-time using a MutationObserver inside the Spribe
 * iframe — no polling loops, instant detection, minimal CPU.
 *
 * The output file (./data/roundhistory.json) is a chronological array of
 * records ready for TensorFlow, Brain.js, or any ML training pipeline.
 *
 * Architecture:
 *   1. Launch Chromium (can reuse existing login via profile/storage)
 *   2. Navigate to winner.rw, login if needed, reach the Aviator page
 *   3. Find the Spribe iframe automatically (scans all child frames)
 *   4. Wait for ".payouts-block .payout" inside the iframe
 *   5. Inject a MutationObserver that fires only when the payout list
 *      actually changes
 *   6. On each mutation, extract all multipliers, deduplicate via
 *      content-signature, append new records, flush to JSON
 *   7. If the iframe detaches/reloads, reconnect transparently
 *
 * Requirements satisfied:
 *   ✓ Playwright + Node.js (ES modules)
 *   ✓ Auto-locate Spribe iframe
 *   ✓ Wait for iframe + ".payouts-block .payout"
 *   ✓ MutationObserver (no polling)
 *   ✓ Instant multiplier detection
 *   ✓ Numbers stripped of "x" suffix
 *   ✓ Deduplication
 *   ✓ 500-round rolling window
 *   ✓ ./data/roundhistory.json output
 *   ✓ File write only when a new round appears
 *   ✓ Iframe reload auto-reconnect
 *   ✓ Detailed console logging
 *   ✓ Error handling (never crashes)
 *   ✓ async/await modern JS
 *   ✓ ML-ready data format
 *
 * Usage:
 *   node roundhistory-collector.js
 *
 * Environment variables (override defaults):
 *   WINNER_PHONE, WINNER_PASSWORD
 *   BOT_HEADLESS         (true/false, default: false)
 *   BOT_CHROME_USER_DATA (path to Chrome profile)
 *   BOT_STORAGE_STATE    (path to storageState.json)
 *   BOT_USE_STORAGE_STATE (set "true" for JSON-based persistence)
 *   BOT_FRAME_TIMEOUT    (ms to wait for iframe, default 120000)
 *   BOT_PAYOUT_TIMEOUT   (ms to wait for payouts, default 120000)
 *   BOT_IDLE_TIMEOUT     (ms before mutation wait times out, default 90000)
 */

// ─────────────────────────────────────────────────────────────────────────────
// Imports
// ─────────────────────────────────────────────────────────────────────────────

import fs from 'fs';
import path from 'path';
import { chromium } from 'playwright-extra';
import StealthPlugin from 'puppeteer-extra-plugin-stealth';
import { fileURLToPath } from 'url';

// Apply stealth plugin to bypass Cloudflare bot detection
chromium.use(StealthPlugin());

// ─────────────────────────────────────────────────────────────────────────────
// Paths
// ─────────────────────────────────────────────────────────────────────────────

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(__dirname, '..');
const ROUND_HISTORY_PATH = path.join(ROOT, 'data', 'roundhistory.json');
const DATA_BOT_DIR = path.join(ROOT, 'data', 'bot');
const STATUS_PATH = path.join(DATA_BOT_DIR, 'status.json');
const CONFIG_PATH = path.join(DATA_BOT_DIR, 'config.json');

// ─────────────────────────────────────────────────────────────────────────────
// Configuration
// ─────────────────────────────────────────────────────────────────────────────

const HISTORY_LIMIT = 500;
const FRAME_TIMEOUT_MS = Number(process.env.BOT_FRAME_TIMEOUT || 120000);
const PAYOUT_TIMEOUT_MS = Number(process.env.BOT_PAYOUT_TIMEOUT || 120000);
const MUTATION_IDLE_MS = Number(process.env.BOT_IDLE_TIMEOUT || 90000);
const RECONNECT_DELAY_BASE = 1000;
const RECONNECT_DELAY_MAX = 10000;

// Keywords used to identify the Spribe Aviator iframe by URL or name
const FRAME_HINTS = [
  'spribe',
  'spribegaming',
  'aviator',
  'crash',
  'crash-games',
  'turbo-games',
  'game',
];

// Winner.rw URLs
const HOME_URL = 'https://winner.rw';
const LOGIN_URL = 'https://winner.rw/en/authentication/login';
const AVIATOR_URL = 'https://winner.rw/en/virtual/crash-games/aviator';
const BASE_URL = 'https://winner.rw';

// ─────────────────────────────────────────────────────────────────────────────
// Selectors
// ─────────────────────────────────────────────────────────────────────────────

// The element that contains each round's multiplier inside the Spribe iframe.
// Example: <div class="payout">2.03x</div>
export const PAYOUT_SELECTOR = '.payouts-block .payout';

// ─────────────────────────────────────────────────────────────────────────────
// Simple Logger
// ─────────────────────────────────────────────────────────────────────────────

const log = {
  info:  (msg, ...args) => writeLog('INFO', msg, args),
  warn:  (msg, ...args) => writeLog('WARN', msg, args),
  error: (msg, ...args) => writeLog('ERROR', msg, args),
};

function writeLog(level, message, args) {
  const ts = new Date().toLocaleTimeString('en-US', { hour12: false });
  const extra = args.length
    ? ' ' + args.map(a => {
        try {
          return typeof a === 'object' ? JSON.stringify(a) : String(a);
        } catch {
          return String(a);
        }
      }).join(' ')
    : '';
  const line = `[${ts}] [COLLECTOR] [${level}] ${message}${extra}`;
  process.stdout.write(line + '\n');
}

// ─────────────────────────────────────────────────────────────────────────────
// File Helpers
// ─────────────────────────────────────────────────────────────────────────────

/** Ensure a directory exists (recursive mkdir). */
function ensureDir(filepath) {
  fs.mkdirSync(path.dirname(filepath), { recursive: true });
}

/** Read a JSON file, returning `defaultValue` if parsing fails or missing. */
function readJSON(filepath, defaultValue) {
  try {
    if (!fs.existsSync(filepath)) return defaultValue;
    return JSON.parse(fs.readFileSync(filepath, 'utf-8'));
  } catch (err) {
    log.warn(`Cannot read ${filepath}: ${err.message}`);
    return defaultValue;
  }
}

/**
 * Atomic JSON write: write to a .tmp file, then rename.
 * This prevents corruption if the process is killed mid-write.
 */
function writeJSON(filepath, payload) {
  ensureDir(filepath);
  const tmp = `${filepath}.tmp`;
  fs.writeFileSync(tmp, JSON.stringify(payload, null, 2), 'utf-8');
  fs.renameSync(tmp, filepath);
}

/** Return the current timestamp as ISO-8601. */
function nowISO() {
  return new Date().toISOString();
}

/** Async sleep. */
function sleep(ms, signal) {
  if (signal?.aborted) return Promise.resolve();
  return new Promise(resolve => {
    const timer = setTimeout(resolve, ms);
    if (signal) {
      signal.addEventListener('abort', () => {
        clearTimeout(timer);
        resolve();
      }, { once: true });
    }
  });
}

// ─────────────────────────────────────────────────────────────────────────────
// Multiplier Parsing
// ─────────────────────────────────────────────────────────────────────────────

/**
 * Convert raw text (e.g. "2.03x", "10.33X", "1.5×") to a clean number.
 * Returns null if the value is invalid (NaN, ≤ 0, etc.).
 */
export function normalizeMultiplier(raw) {
  if (typeof raw === 'number') {
    if (!Number.isFinite(raw) || raw <= 0) return null;
    return Math.round(raw * 100) / 100;
  }
  if (typeof raw !== 'string') return null;

  // Remove commas, trailing x/X/×, and any non-numeric chars except period
  const cleaned = raw
    .trim()
    .replace(/,/g, '')
    .replace(/[xX×]/g, '')
    .replace(/[^0-9.]/g, '');

  if (!cleaned) return null;

  const parsed = Number.parseFloat(cleaned);
  if (!Number.isFinite(parsed) || parsed <= 0) return null;

  return Math.round(parsed * 100) / 100;
}

/**
 * Build a content-signature string from an array of multiplier values.
 * Two snapshots with the same signature have identical visible history.
 */
function snapshotSignature(multipliers) {
  return multipliers.map(v => Number(v).toFixed(2)).join('|');
}

/** Human-friendly display of a multiplier value. */
function fmt(v) {
  return `${Number(v).toFixed(2)}x`;
}

// ─────────────────────────────────────────────────────────────────────────────
// Round History (read / write / merge)
// ─────────────────────────────────────────────────────────────────────────────

/**
 * Read the existing round history, normalise it, return as an array.
 * Handles files that may be an object with a "rounds" or "history" key.
 */
function readRoundHistory() {
  const raw = readJSON(ROUND_HISTORY_PATH, []);

  // Normalise: some older formats wrapped the array in { rounds: [...] }
  let rows = raw;
  if (!Array.isArray(rows)) {
    if (Array.isArray(rows.rounds)) rows = rows.rounds;
    else if (Array.isArray(rows.history)) rows = rows.history;
    else return [];
  }

  const cleaned = [];
  const seen = new Set();

  for (let i = 0; i < rows.length; i++) {
    const item = rows[i];
    const multiplier = normalizeMultiplier(
      typeof item === 'object' && item !== null
        ? item.multiplier ?? item.crashPoint ?? item.value
        : item
    );

    if (multiplier === null || multiplier < 1) continue;

    const roundIdx = (typeof item === 'object' && item !== null)
      ? Number(item.round_index ?? item.round_id ?? item.id ?? (i + 1))
      : (i + 1);

    const timestamp = (typeof item === 'object' && item !== null)
      ? item.timestamp ?? item.time ?? item.ts ?? null
      : null;

    // Deduplicate exact same round (same index + timestamp + multiplier)
    const key = `${roundIdx}|${timestamp || ''}|${multiplier.toFixed(2)}`;
    if (seen.has(key)) continue;
    seen.add(key);

    cleaned.push({ multiplier, timestamp, round_index: roundIdx });
  }

  return cleaned.slice(-HISTORY_LIMIT);
}

/** Write the history array to disk, enforcing the 500-record cap. */
function writeRoundHistory(records) {
  writeJSON(ROUND_HISTORY_PATH, records.slice(-HISTORY_LIMIT));
}

/**
 * Compute the next available round index.
 */
function nextRoundIndex(history) {
  const maxIdx = history.reduce((max, r) => {
    const idx = Number(r.round_index ?? 0);
    return Number.isFinite(idx) ? Math.max(max, idx) : max;
  }, 0);
  return maxIdx + 1;
}

/**
 * Compare the previous visible snapshot with the current one and infer
 * which multipliers are new (prepended to the list, newest first).
 */
function inferNewMultipliers(prev, curr) {
  if (!prev?.length || !curr?.length) return [];
  if (snapshotSignature(prev) === snapshotSignature(curr)) return [];

  const maxShift = Math.min(curr.length, 25);

  for (let shift = 1; shift <= maxShift; shift++) {
    const len = Math.min(10, prev.length, curr.length - shift);
    if (len <= 0) continue;

    let aligned = true;
    for (let i = 0; i < len; i++) {
      if (Number(prev[i]).toFixed(2) !== Number(curr[shift + i]).toFixed(2)) {
        aligned = false;
        break;
      }
    }
    if (aligned) return curr.slice(0, shift);
  }

  // Fallback: just take the newest value
  return [curr[0]];
}

/**
 * Append new multipliers (newest-first) to the history array.
 * The file is stored chronologically (oldest-first), so we reverse the input.
 */
function appendRounds(history, newestFirst) {
  if (!newestFirst.length) return { history, added: [] };

  const added = [];
  let idx = nextRoundIndex(history);

  for (const mult of [...newestFirst].reverse()) {
    const normalized = normalizeMultiplier(mult);
    if (normalized === null || normalized < 1) continue;

    const record = {
      multiplier: normalized,
      timestamp: nowISO(),
      round_index: idx,
    };
    history.push(record);
    added.push(record);
    idx++;
  }

  return { history: history.slice(-HISTORY_LIMIT), added };
}

// ─────────────────────────────────────────────────────────────────────────────
// Browser Launch
// ─────────────────────────────────────────────────────────────────────────────

let _browser = null;
let _context = null;
let _page = null;

/** Load credentials from config.json or environment variables. */
function loadCredentials() {
  const config = readJSON(CONFIG_PATH, {});
  return {
    phone:    process.env.WINNER_PHONE    || config.phone    || '',
    password: process.env.WINNER_PASSWORD || config.password || '',
    headless: (process.env.BOT_HEADLESS || String(config.headless || 'false')).toLowerCase() === 'true',
  };
}

/** Launch Chromium with stealth mode and profile persistence. */
async function launchBrowser({ headless = false, useStorageState = false, storageStatePath } = {}) {
  if (_browser && _page && !_page.isClosed()) {
    log.info('Reusing existing browser session');
    return { browser: _browser, context: _context, page: _page };
  }

  const userDataDir = process.env.BOT_CHROME_USER_DATA || path.join(DATA_BOT_DIR, 'chrome-profile');
  fs.mkdirSync(userDataDir, { recursive: true });

  log.info(`Launching Chromium (headless=${headless})`);

  const launchOpts = {
    channel: 'chrome',
    headless,
    args: [
      '--disable-blink-features=AutomationControlled',
      '--no-sandbox',
      '--disable-dev-shm-usage',
      '--disable-setuid-sandbox',
      '--disable-web-security',
      '--disable-features=IsolateOrigins,site-per-process',
    ],
  };

  const ctxOpts = {
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

  if (useStorageState || process.env.BOT_USE_STORAGE_STATE === 'true') {
    // Non-persistent context — session saved to a JSON file
    const ssPath = storageStatePath || process.env.BOT_STORAGE_STATE || path.join(DATA_BOT_DIR, 'storageState.json');
    if (fs.existsSync(ssPath)) {
      ctxOpts.storageState = ssPath;
      log.info(`Loaded storage state from ${ssPath}`);
    }
    _browser = await chromium.launch(launchOpts);
    _context = await _browser.newContext(ctxOpts);
  } else {
    // Persistent context — session saved in the Chrome profile directory
    _context = await chromium.launchPersistentContext(userDataDir, { ...launchOpts, ...ctxOpts });
    _browser = _context.browser();
  }

  const pages = _context.pages();
  _page = pages.length > 0 ? pages[0] : await _context.newPage();

  log.info('Browser launched successfully');
  return { browser: _browser, context: _context, page: _page };
}

/** Close the browser gracefully. */
async function closeBrowser() {
  if (_page) _page = null;
  if (_context) { try { await _context.close(); } catch {} _context = null; }
  if (_browser) { try { await _browser.close(); } catch {} _browser = null; }
  log.info('Browser closed');
}

/** Get or create the current page. */
async function getPage() {
  if (_page && !_page.isClosed()) return _page;
  if (_context) {
    _page = await _context.newPage();
    return _page;
  }
  throw new Error('Browser not launched. Call launchBrowser() first.');
}

// ─────────────────────────────────────────────────────────────────────────────
// Safer Navigation (bypass Cloudflare)
// ─────────────────────────────────────────────────────────────────────────────

/**
 * Navigate to a URL with retry logic and multiple wait strategies.
 * Cloudflare blocks direct goto() on some sub-pages, so we try
 * 'load' → 'domcontentloaded' → 'commit' in sequence.
 */
async function safeNavigate(page, url, timeout = 60000) {
  for (let attempt = 1; attempt <= 3; attempt++) {
    const waitUntil = ['load', 'domcontentloaded', 'commit'][attempt - 1];
    try {
      log.info(`Navigating to ${url} (attempt ${attempt}, wait=${waitUntil})`);
      await page.goto(url, { waitUntil, timeout });

      // Quick sanity check — page should have a <body>
      await page.waitForSelector('body', { timeout: 5000 }).catch(() => {});
      return true;
    } catch (err) {
      const isTimeout = err.message?.toLowerCase().includes('timeout');
      if (isTimeout) {
        log.warn(`Navigation attempt ${attempt} timed out for ${url}`);
      } else {
        log.error(`Navigation error: ${err.message}`);
      }
      if (attempt < 3) await sleep(attempt * 3000);
    }
  }
  log.error(`All navigation attempts failed for ${url}`);
  return false;
}

// ─────────────────────────────────────────────────────────────────────────────
// Login
// ─────────────────────────────────────────────────────────────────────────────

async function checkSession(page) {
  log.info('Checking existing session...');

  // Go to homepage first (always works)
  const ok = await safeNavigate(page, HOME_URL, 30000);
  if (!ok) return false;

  // Wait for SPA to render navigation elements
  await page.waitForSelector(
    '#user-menu-login, a.login-btn, a[href*="/aviator"]',
    { timeout: 15000 }
  ).catch(() => {});
  await page.waitForTimeout(1000);

  // Look for dashboard indicators (balance, avatar, etc.)
  const dashboardSignals = [
    '.user-profile', '.account-dropdown', '.dashboard',
    '[class*="balance"]', '[class*="wallet"]', '[class*="avatar"]',
    'a[href*="logout"]',
  ];

  for (const sel of dashboardSignals) {
    const el = await page.$(sel).catch(() => null);
    if (el) {
      const visible = await el.isVisible().catch(() => false);
      if (visible) {
        log.info('Session valid — dashboard element found');
        return true;
      }
    }
  }

  log.info('No session detected — will login');
  return false;
}

async function login(page, phone, password) {
  log.info('Starting login flow...');

  // Navigate to homepage and click the LOGIN link (bypasses Cloudflare)
  if (!page.url().toLowerCase().includes('/login')) {
    await safeNavigate(page, HOME_URL, 30000).catch(() => {});
    await page.waitForTimeout(1500);

    // Try clicking the login link
    const loginLink = await page.$('#user-menu-login, a.login-btn, a[href*="/login"]');
    if (loginLink) {
      await loginLink.click();
      await page.waitForTimeout(2000);
    } else {
      // Fallback: try direct navigation
      await safeNavigate(page, LOGIN_URL, 30000).catch(() => {});
    }
  }

  await page.waitForTimeout(2000);

  // Find and fill the phone field
  const phoneInput = await page.$('#phoneInput');
  if (!phoneInput) {
    log.error('Phone input not found on login page');
    return false;
  }
  await phoneInput.click({ clickCount: 3 });
  await page.fill('#phoneInput', phone);
  log.info('Phone filled');

  // Find and fill the password field
  const passInput = await page.$('#password');
  if (!passInput) {
    log.error('Password input not found');
    return false;
  }
  await passInput.click({ clickCount: 3 });
  await page.fill('#password', password);
  log.info('Password filled');

  // Click the submit button
  const submitBtn = await page.$('#buttonLoginSubmit, #buttonLoginSubmitLabel, button[type="submit"]');
  if (!submitBtn) {
    log.error('Login button not found');
    return false;
  }
  await submitBtn.click();
  log.info('Login submitted');

  // Wait for post-login navigation
  await page.waitForNavigation({ waitUntil: 'load', timeout: 20000 }).catch(() => {});
  await page.waitForTimeout(3000);

  if (page.url().toLowerCase().includes('/login')) {
    log.error('Still on login page — credentials may be wrong');
    return false;
  }

  log.info(`Login successful: ${page.url()}`);
  return true;
}

async function goToAviator(page) {
  log.info('Navigating to Aviator game...');

  // Try clicking the Aviator link from homepage
  await safeNavigate(page, HOME_URL, 30000).catch(() => {});
  await page.waitForTimeout(2000);

  const aviatorLink = await page.$('a[href*="/aviator"], a[href*="crash-games"]');
  if (aviatorLink) {
    await aviatorLink.click();
    await page.waitForTimeout(5000);
  } else {
    // Direct navigation (may be blocked by Cloudflare)
    const ok = await safeNavigate(page, AVIATOR_URL, 60000);
    if (!ok) {
      log.error('Could not navigate to Aviator page');
      return false;
    }
  }

  const url = page.url().toLowerCase();
  if (url.includes('aviator') || url.includes('crash')) {
    log.info(`Aviator page loaded: ${page.url()}`);
    return true;
  }
  log.error(`Unexpected URL: ${page.url()}`);
  return false;
}

// ─────────────────────────────────────────────────────────────────────────────
// Aviator Iframe Discovery
// ─────────────────────────────────────────────────────────────────────────────

/** Describe a frame for logging. */
function describeFrame(frame) {
  let name = '', url = '';
  try { name = frame.name(); } catch { name = 'detached'; }
  try { url = frame.url(); } catch { url = 'detached'; }
  return `name="${name}" url="${url}"`;
}

/** Check if a frame URL/name looks like the Spribe Aviator iframe. */
function frameLooksLikeAviator(frame) {
  try {
    const haystack = `${frame.name()} ${frame.url()}`.toLowerCase();
    return FRAME_HINTS.some(hint => haystack.includes(hint));
  } catch {
    return false;
  }
}

/** Check if a frame is a usable child frame (not the main frame, not detached). */
function frameIsUsable(page, frame) {
  try {
    return frame !== page.mainFrame() && !frame.isDetached();
  } catch {
    return false;
  }
}

/** Quick check if a frame contains the payout history elements. */
async function frameHasPayouts(frame) {
  try {
    await frame.locator(PAYOUT_SELECTOR).first().waitFor({ state: 'attached', timeout: 500 });
    return true;
  } catch {
    return false;
  }
}

/**
 * Find the Spribe Aviator iframe among all child frames of the page.
 * Prioritises frames that look like Aviator (by URL/name) but falls back
 * to scanning every iframe for the payout selector.
 */
async function findAviatorFrame(page) {
  const frames = page.frames().filter(f => frameIsUsable(page, f));

  // Sort: Aviator-looking frames first
  const sorted = [
    ...frames.filter(frameLooksLikeAviator),
    ...frames.filter(f => !frameLooksLikeAviator(f)),
  ];

  // Try each frame for the payout selector
  for (const frame of sorted) {
    if (await frameHasPayouts(frame)) {
      return { frame, method: 'payout-selector' };
    }
  }

  // Fallback: return the first Aviator-hinted frame even if no payouts yet
  const hinted = sorted.find(frameLooksLikeAviator);
  if (hinted) return { frame: hinted, method: 'url-hint' };

  return null;
}

/**
 * Wait for the Aviator iframe to become available.
 *
 * The Spribe game loads in stages:
 *   1. Outer iframe appears (spribegaming.com) — found by URL/name hint
 *   2. Game bundle loads and renders the React UI inside the iframe
 *   3. ".payouts-block .payout" elements appear
 *
 * If we return at stage 1, the caller won't find payout elements yet.
 * This function continues to loop for a grace period after the first
 * hint, trying to find a frame that actually has the payout selector.
 *
 * @returns {Frame} A Playwright frame object (may or may not have payouts yet)
 */
async function waitForAviatorFrame(page, { timeoutMs = FRAME_TIMEOUT_MS, signal } = {}) {
  const deadline = Date.now() + timeoutMs;
  const HINT_GRACE_MS = 15000; // how long to wait after first URL hint for payouts
  let hintedFrame = null;
  let hintDeadline = 0;

  while (!signal?.aborted && Date.now() < deadline) {
    const found = await findAviatorFrame(page);

    if (found && found.method === 'payout-selector') {
      // Found a frame that already has payout elements — best case, return immediately
      log.info(`Aviator frame found (payout-selector): ${describeFrame(found.frame)}`);
      console.log('  ✔ Aviator frame found');
      return found.frame;
    }

    if (found && found.method === 'url-hint') {
      if (!hintedFrame) {
        // First time seeing the Aviator frame — note it and keep waiting for payouts
        hintedFrame = found.frame;
        hintDeadline = Date.now() + HINT_GRACE_MS;
        log.info(`Aviator frame hinted: ${describeFrame(hintedFrame)} — waiting for game to initialize`);
      }

      // If the grace period has expired, return the hinted frame
      if (Date.now() >= hintDeadline) {
        log.info(`Aviator frame found (url-hint, after ${HINT_GRACE_MS}ms): ${describeFrame(hintedFrame)}`);
        console.log('  ✔ Aviator frame found');
        return hintedFrame;
      }
    }

    // Wait for any frame activity or a brief timeout
    const waitMs = hintedFrame
      ? Math.min(1000, hintDeadline - Date.now())
      : 2000;
    if (waitMs <= 0) continue;

    await Promise.race([
      page.waitForEvent('frameattached', { timeout: waitMs }).catch(() => {}),
      page.waitForEvent('framenavigated', { timeout: waitMs }).catch(() => {}),
      sleep(waitMs, signal),
    ]);
  }

  // Final fallback: if we ever had a hinted frame, return it
  if (hintedFrame) {
    log.info(`Aviator frame found (url-hint, deadline fallback): ${describeFrame(hintedFrame)}`);
    console.log('  ✔ Aviator frame found');
    return hintedFrame;
  }

  throw new Error('Timed out waiting for Aviator iframe');
}

// ─────────────────────────────────────────────────────────────────────────────
// Iframe Mutation Injection
// ─────────────────────────────────────────────────────────────────────────────

/**
 * Inside the iframe, inject a MutationObserver on the payouts container.
 *
 * The observer watches for:
 *   - childList changes (new payout elements added)
 *   - characterData changes (existing payout text updates)
 *   - attribute changes on class/style/hidden
 *
 * When a relevant mutation fires, we read all multipliers, compute a
 * content-signature, and push an event to a queue that Playwright can
 * retrieve on its next check — or immediately if a waiter is active.
 */
async function installMutationObserver(frame) {
  const result = await frame.evaluate((selector) => {
    // ── In-frame helpers ───────────────────────────────────────────
    function parse(text) {
      if (!text) return null;
      const cleaned = String(text)
        .trim()
        .replace(/,/g, '')
        .replace(/[xX×]/g, '')
        .replace(/[^0-9.]/g, '');
      if (!cleaned) return null;
      const v = Number.parseFloat(cleaned);
      return (Number.isFinite(v) && v > 0) ? Math.round(v * 100) / 100 : null;
    }

    function readAll() {
      return Array.from(document.querySelectorAll(selector))
        .map(el => parse(el.textContent))
        .filter(v => v !== null);
    }

    function sig(values) {
      return values.map(v => v.toFixed(2)).join('|');
    }

    // ── Locate the container to observe ───────────────────────────
    const first = document.querySelector(selector);
    const block = first?.closest('.payouts-block') || document.querySelector('.payouts-block');
    const target = block?.parentElement || block || document.body;

    if (!first || !block) {
      return { ok: false, error: 'payout history target not found' };
    }

    // Clean up any previous observer on the same window
    if (window.__aviatorCollector?.observer) {
      window.__aviatorCollector.observer.disconnect();
    }

    // ── Collector state ───────────────────────────────────────────
    const collector = {
      queue: [],         // buffered mutation events
      waiter: null,      // promise resolver (single waiting consumer)
      lastSig: sig(readAll()),
      observer: null,
    };

    // ── Publish a mutation event ──────────────────────────────────
    function publish(reason) {
      const multipliers = readAll();
      if (!multipliers.length) return;

      const nextSig = sig(multipliers);
      if (nextSig === collector.lastSig) return; // no actual change
      collector.lastSig = nextSig;

      const event = {
        reason,
        timestamp: new Date().toISOString(),
        multipliers,
        count: multipliers.length,
        newest: multipliers[0],
        signature: nextSig,
      };

      // If someone is already waiting for the next event, resolve immediately
      if (collector.waiter) {
        const w = collector.waiter;
        collector.waiter = null;
        w(event);
        return;
      }

      // Otherwise push to queue (capped at 25)
      collector.queue.push(event);
      if (collector.queue.length > 25) collector.queue = collector.queue.slice(-25);
    }

    // ── Create the observer ───────────────────────────────────────
    const observer = new MutationObserver((mutations) => {
      const relevant = mutations.some(m => {
        if (m.type === 'childList') return true;
        if (m.type === 'characterData') return true;
        if (m.type === 'attributes') {
          return ['class', 'style', 'hidden'].includes(m.attributeName);
        }
        return false;
      });
      if (relevant) publish('mutation');
    });

    observer.observe(target, {
      childList: true,
      subtree: true,
      characterData: true,
      attributes: true,
      attributeFilter: ['class', 'style', 'hidden'],
    });

    collector.observer = observer;
    window.__aviatorCollector = collector;

    const multipliers = readAll();

    return {
      ok: true,
      count: multipliers.length,
      newest: multipliers[0],
      multipliers,
      signature: sig(multipliers),
    };
  }, PAYOUT_SELECTOR);

  if (!result?.ok) {
    throw new Error(result?.error || 'Could not install MutationObserver');
  }

  log.info(`MutationObserver installed in Aviator frame (${result.count} rounds visible)`);
  console.log(`  ✔ MutationObserver ready — ${result.count} rounds seen`);

  return {
    multipliers: result.multipliers
      .map(normalizeMultiplier)
      .filter(v => v !== null),
    count: result.count,
    newest: result.newest !== undefined
      ? normalizeMultiplier(result.newest)
      : null,
  };
}

/**
 * Wait for the next payout mutation event from the in-frame observer.
 * Returns an event object with `multipliers`, `signature`, etc.
 * Returns { timeout: true } if nothing changes within the idle window.
 */
async function waitForMutation(frame, idleMs = MUTATION_IDLE_MS) {
  const event = await frame.evaluate(({ idleMs }) => {
    const c = window.__aviatorCollector;
    if (!c) return { error: 'collector_not_installed' };

    if (c.queue.length > 0) return c.queue.shift();

    return new Promise(resolve => {
      let settled = false;
      const done = (payload) => {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        if (c.waiter === done) c.waiter = null;
        resolve(payload);
      };
      const timer = setTimeout(() => done({ timeout: true, timestamp: new Date().toISOString() }), idleMs);
      c.waiter = done;
    });
  }, { idleMs });

  if (event?.multipliers) {
    event.multipliers = event.multipliers
      .map(normalizeMultiplier)
      .filter(v => v !== null);
    event.newest = event.multipliers?.[0] ?? null;
    event.signature = snapshotSignature(event.multipliers);
  }

  return event;
}

// ─────────────────────────────────────────────────────────────────────────────
// Frame Reconnection Detection
// ─────────────────────────────────────────────────────────────────────────────

/** Determine if an error is recoverable (iframe reload/detach). */
function isRecoverableError(err) {
  const msg = String(err?.message || '').toLowerCase();
  return [
    'execution context was destroyed',
    'frame was detached',
    'frame detached',
    'target closed',
    'context closed',
    'page closed',
    'has been closed',
    'collector_not_installed',
    'navigation',
  ].some(f => msg.includes(f));
}

// ─────────────────────────────────────────────────────────────────────────────
// One-Shot Extraction
// ─────────────────────────────────────────────────────────────────────────────

/**
 * Extract all current payouts from the Aviator iframe and return as
 * numeric multipliers (newest first). Does not modify the JSON file.
 */
export async function extractPayouts(page) {
  try {
    const frame = await waitForAviatorFrame(page);
    console.log('  ✔ Aviator frame found');

    await frame.locator(PAYOUT_SELECTOR).first().waitFor({
      state: 'attached',
      timeout: PAYOUT_TIMEOUT_MS,
    });
    console.log('  ✔ Waiting for payout history');

    const multipliers = await frame.evaluate((selector) => {
      function parse(text) {
        if (!text) return null;
        const cleaned = String(text).trim().replace(/,/g, '').replace(/[xX×]/g, '').replace(/[^0-9.]/g, '');
        if (!cleaned) return null;
        const v = Number.parseFloat(cleaned);
        return (Number.isFinite(v) && v > 0) ? Math.round(v * 100) / 100 : null;
      }
      return Array.from(document.querySelectorAll(selector))
        .map(el => parse(el.textContent))
        .filter(v => v !== null);
    }, PAYOUT_SELECTOR);

    return {
      multipliers: multipliers.map(normalizeMultiplier).filter(v => v !== null),
      count: multipliers.length,
      newest: multipliers.length > 0 ? normalizeMultiplier(multipliers[0]) : null,
      error: null,
    };
  } catch (err) {
    log.error(`extractPayouts failed: ${err.message}`);
    return { multipliers: [], count: 0, newest: null, error: err.message };
  }
}

// ─────────────────────────────────────────────────────────────────────────────
// Live Monitor
// ─────────────────────────────────────────────────────────────────────────────

/**
 * The main monitoring loop.
 *
 * 1. Find the Aviator iframe
 * 2. Wait for payout elements
 * 3. Install MutationObserver
 * 4. Loop: wait for mutation events → extract new multipliers → save to JSON
 * 5. If the iframe reloads/detaches, reconnect and resume
 */
export async function monitorRounds(page, options = {}) {
  const signal = options.signal || null;

  let history = readRoundHistory();
  let prevSnapshot = null;
  let prevSig = null;
  let lastSavedSig = null;
  let totalAdded = 0;
  let delay = RECONNECT_DELAY_BASE;

  console.log('\n=== Aviator Round History Monitor ===');
  log.info('Starting MutationObserver monitor');
  console.log('  ✔ MutationObserver monitor started inside the Spribe iframe');

  while (!signal?.aborted) {
    let frame = null;

    try {
      // ── Connect / Reconnect to the Aviator iframe ────────────
      frame = await waitForAviatorFrame(page, { signal });

      console.log('  ✔ Aviator frame found');
      log.info('Waiting for payout history');
      console.log('  ✔ Waiting for payout history');

      await frame.locator(PAYOUT_SELECTOR).first().waitFor({
        state: 'attached',
        timeout: PAYOUT_TIMEOUT_MS,
      });

      const observerState = await installMutationObserver(frame);

      // Build initial snapshot
      const currSnapshot = observerState.multipliers;
      const currSig = snapshotSignature(currSnapshot);

      if (!prevSnapshot) {
        // First connection — establish baseline
        prevSnapshot = currSnapshot;
        prevSig = currSig;
        log.info(`Baseline captured: ${currSnapshot.length} rounds`);
        console.log(`  ✔ Baseline: ${currSnapshot.length} rounds loaded`);
      } else if (currSig !== prevSig) {
        // Reconnected after reload — catch up on missed rounds
        const inferred = inferNewMultipliers(prevSnapshot, currSnapshot);
        if (inferred.length > 0) {
          const { history: newHistory, added } = appendRounds(history, inferred);
          if (added.length > 0) {
            history = newHistory;
            writeRoundHistory(history);
            totalAdded += added.length;
            log.info(`Frame reconnected — caught up ${added.length} missed round(s)`);
            console.log(`  ✔ Frame reconnected — caught up ${added.length} missed round(s)`);
          }
        }
        prevSnapshot = currSnapshot;
        prevSig = currSig;
      }

      log.info('Frame reconnected');
      console.log('  ✔ Frame reconnected');
      delay = RECONNECT_DELAY_BASE;

      // ── Mutation wait loop ──────────────────────────────────
      while (!signal?.aborted) {
        const event = await waitForMutation(frame, MUTATION_IDLE_MS);

        if (signal?.aborted) break;

        if (event?.timeout) {
          // No change within the idle window — check if frame is still alive
          if (frame.isDetached()) {
            throw new Error('Aviator frame detached while waiting');
          }
          continue;
        }

        if (event?.error) {
          throw new Error(event.error);
        }

        if (!event?.multipliers?.length) continue;

        const currSnapshot = event.multipliers;
        const currSig = snapshotSignature(currSnapshot);

        if (currSig === prevSig) continue;

        const inferred = inferNewMultipliers(prevSnapshot, currSnapshot);

        if (inferred.length === 0) {
          prevSnapshot = currSnapshot;
          prevSig = currSig;
          continue;
        }

        // ── Save new rounds ────────────────────────────────────
        const newest = inferred[0];

        if (currSig !== lastSavedSig) {
          log.info(`New round detected: ${fmt(newest)}`);
          console.log(`  ✔ New round detected: ${fmt(newest)}`);

          const { history: newHistory, added } = appendRounds(history, inferred);

          if (added.length > 0) {
            history = newHistory;
            writeRoundHistory(history);
            lastSavedSig = currSig;
            totalAdded += added.length;
            log.info(`History updated: ${history.length} rounds saved`);
            console.log(`  ✔ History updated: ${history.length} rounds saved`);
          }
        }

        prevSnapshot = currSnapshot;
        prevSig = currSig;
      }
    } catch (err) {
      if (signal?.aborted) break;

      if (isRecoverableError(err)) {
        log.warn(`Frame disconnected: ${err.message}. Reconnecting...`);
        console.log(`  ⚠ Frame disconnected — reconnecting in ${delay}ms...`);
      } else {
        log.error(`Monitor error: ${err.message}. Reconnecting...`);
        console.log(`  ✖ Error: ${err.message} — reconnecting...`);
      }

      await sleep(delay, signal);
      delay = Math.min(delay * 2, RECONNECT_DELAY_MAX);
    }
  }

  log.info(`Monitor stopped. Total new records saved: ${totalAdded}`);
  console.log(`\n=== Monitor stopped: ${totalAdded} new rounds saved ===`);
  return { totalAdded };
}

// ─────────────────────────────────────────────────────────────────────────────
// Main Entry Point
// ─────────────────────────────────────────────────────────────────────────────

async function main() {
  // ── Load credentials ──────────────────────────────────────────────
  const { phone, password, headless } = loadCredentials();

  if (!phone || !password) {
    console.error('ERROR: Phone or password missing. Set in data/bot/config.json or via WINNER_PHONE/WINNER_PASSWORD env vars.');
    process.exit(1);
  }

  const masked = phone.slice(-4).padStart(phone.length, '*');
  log.info(`Starting Aviator collector for ${masked} (headless=${headless})`);

  try {
    // ── Launch browser ──────────────────────────────────────────
    console.log('\n--- Step 1: Launching browser ---');
    await launchBrowser({ headless });

    const page = await getPage();

    // ── Check session / Login ───────────────────────────────────
    console.log('\n--- Step 2: Authentication ---');
    const loggedIn = await checkSession(page);

    if (!loggedIn) {
      console.log('  → Session not found, logging in...');
      const loginOk = await login(page, phone, password);
      if (!loginOk) {
        console.error('ERROR: Login failed. Check credentials in data/bot/config.json');
        await closeBrowser();
        process.exit(1);
      }
    } else {
      console.log('  ✔ Already authenticated');
    }

    // ── Navigate to Aviator ─────────────────────────────────────
    console.log('\n--- Step 3: Navigate to Aviator ---');
    const onAviator = await goToAviator(page);
    if (!onAviator) {
      console.error('ERROR: Could not reach Aviator page. Please navigate manually.');
      console.log('  → The bot will wait for you to open the Aviator page...');
      // Wait up to 3 minutes for manual navigation
      const timeout = Date.now() + 180000;
      let manualOk = false;
      while (Date.now() < timeout) {
        const url = page.url().toLowerCase();
        if (url.includes('aviator') || url.includes('crash')) {
          manualOk = true;
          break;
        }
        await sleep(1000);
      }
      if (!manualOk) {
        console.error('ERROR: Timed out waiting for manual Aviator navigation');
        await closeBrowser();
        process.exit(1);
      }
    }
    console.log(`  ✔ Aviator page loaded: ${page.url()}`);

    // ── Start monitoring ────────────────────────────────────────
    console.log('\n--- Step 4: Monitoring ---');
    console.log('  Press Ctrl+C to stop\n');

    await monitorRounds(page);

  } catch (err) {
    log.error(`Fatal error: ${err.message}`);
    console.error(`\nFATAL: ${err.message}`);
  } finally {
    await closeBrowser();
  }
}

// Run only when executed directly (not imported)
const isDirectRun = process.argv[1] && (
  process.argv[1] === fileURLToPath(import.meta.url) ||
  process.argv[1].endsWith('roundhistory-collector.js')
);

if (isDirectRun) {
  // Handle graceful shutdown
  const abortController = new AbortController();

  process.on('SIGINT', () => {
    console.log('\n  Shutting down gracefully...');
    abortController.abort();
    // Give the monitor a moment to clean up, then force exit
    setTimeout(() => process.exit(0), 5000);
  });

  process.on('SIGTERM', () => {
    abortController.abort();
    setTimeout(() => process.exit(0), 5000);
  });

  process.on('uncaughtException', (err) => {
    log.error(`Uncaught exception: ${err.message}`);
    console.error(`\nUNCAUGHT ERROR: ${err.message}`);
    closeBrowser().catch(() => {}).finally(() => process.exit(1));
  });

  process.on('unhandledRejection', (reason) => {
    log.error(`Unhandled rejection: ${reason}`);
  });

  main();
}


