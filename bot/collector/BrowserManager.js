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

chromium.use(StealthPlugin());

const __dirname  = path.dirname(fileURLToPath(import.meta.url));
const ROOT       = path.resolve(__dirname, '..', '..');
const PROFILE_DIR = path.join(ROOT, 'data', 'bot', 'chrome-profile-new-email');
const DEFAULT_BETTING_PAGE_URL = 'https://winner.rw/en/sportsbook/upcoming';
const GOOGLE_SNAPSHOT_DIR = path.join(ROOT, 'data', 'bot', 'chrome-google-fallback');
const SUPERVISOR_LOCK = path.join(ROOT, 'data', 'bot', 'browser-supervisor.lock');
const RELIABILITY_PATH = path.join(ROOT, 'data', 'bot', 'browser-reliability.json');
const DIAGNOSTICS_PATH = path.join(ROOT, 'data', 'bot', 'browser-diagnostics.jsonl');
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

export class BrowserSupervisor {
  constructor(headless = false) {
    this.headless  = headless;
    this._context  = null;
    this._browser  = null;
    this._page     = null;
    this._historyPage = null;
    this._ownsBrowser = false;
    this._profileDir = PROFILE_DIR;
    this._profileName = null;
    this.restartCount = 0;
    this._lockOwned = false;
    this._browserStartedAt = null;
    this._lastBrowserEvent = { event: 'created', at: new Date().toISOString() };
    this._lastDisconnect = null;
    this._lastRecovery = null;
    this._historyPage = null;
    this._bettingPage = null;
    this._healthTimer = null;
    this._healthBusy = false;
    this._health = null;
    this._sessionStorage = null;
    this._pageProbeFailures = 0;
    this._circuitBreaker = { state: 'CLOSED', failures: [] };
    this._recoveryCount = 0;
    this._recoveryPromise = null;
    this._contextClosed = false;
    try {
      const saved = JSON.parse(fs.readFileSync(path.join(ROOT, 'data', 'bot', 'browser-session-state.json'), 'utf8'));
      this._sessionStorage = saved.sessionStorage || null;
      this._lastKnownUrls = saved.last_known_urls || {};
    } catch { this._lastKnownUrls = {}; }
    try {
      const previous = JSON.parse(fs.readFileSync(RELIABILITY_PATH, 'utf8'));
      this.restartCount = Number(previous.browser?.restart_count || 0);
      this._recoveryCount = Number(previous.recovery_count || 0);
      this._lastDisconnect = previous.last_disconnect || null;
      this._lastRecovery = previous.last_recovery || null;
      const cutoff = Date.now() - 10 * 60 * 1000;
      const failures = Array.isArray(previous.circuit_breaker?.failure_timestamps)
        ? previous.circuit_breaker.failure_timestamps.filter(ts => Number(ts) >= cutoff)
        : [];
      this._circuitBreaker = {
        state: previous.circuit_breaker?.state === 'OPEN' && failures.length >= 5 ? 'OPEN' : 'CLOSED',
        failures,
      };
    } catch {}
  }

  _event(event, fields = {}) {
    this._lastBrowserEvent = { event, at: new Date().toISOString(), ...fields };
  }

  _acquireSingletonLock() {
    if (this._lockOwned) return;
    fs.mkdirSync(path.dirname(SUPERVISOR_LOCK), { recursive: true });
    for (let attempt = 0; attempt < 2; attempt += 1) {
      try {
        const fd = fs.openSync(SUPERVISOR_LOCK, 'wx', 0o600);
        fs.writeFileSync(fd, JSON.stringify({ pid: process.pid, started_at: new Date().toISOString() }));
        fs.closeSync(fd);
        this._lockOwned = true;
        return;
      } catch (err) {
        if (err.code !== 'EEXIST') throw err;
        let ownerPid = null;
        try { ownerPid = Number(JSON.parse(fs.readFileSync(SUPERVISOR_LOCK, 'utf8')).pid); } catch {}
        if (pidIsAlive(ownerPid)) throw new Error('BROWSER_SUPERVISOR_ALREADY_ACTIVE pid=' + ownerPid);
        try { fs.unlinkSync(SUPERVISOR_LOCK); } catch {}
      }
    }
    throw new Error('Could not acquire the browser supervisor lock');
  }

  _releaseSingletonLock() {
    if (!this._lockOwned) return;
    try {
      const lock = JSON.parse(fs.readFileSync(SUPERVISOR_LOCK, 'utf8'));
      if (Number(lock.pid) === process.pid) fs.unlinkSync(SUPERVISOR_LOCK);
    } catch {}
    this._lockOwned = false;
  }

  setHealthSource(health) { this._health = health; }
  isPageResponsive() { return this._pageProbeFailures < 3; }

  async withRecoveryLock(operation) {
    if (this._recoveryPromise) return this._recoveryPromise;
    this._recoveryPromise = Promise.resolve().then(operation);
    try { return await this._recoveryPromise; }
    finally { this._recoveryPromise = null; }
  }

  _safeUrl(raw) {
    try { const url = new URL(raw); return url.origin + url.pathname; }
    catch { return ''; }
  }

  _trackPage(page, role) {
    if (!page || page.__winnerSupervisorTracked) return;
    try { page.__winnerSupervisorTracked = true; } catch {}
    page.on?.('close', () => {
      this._event('page_closed', { role, url: this._safeUrl(page.url?.()) });
      this._persistMetadata();
    });
    page.on?.('crash', error => {
      this._event('page_crashed', { role, error: String(error?.message || error || 'page crashed') });
      this._persistMetadata();
    });
    page.on?.('pageerror', error => {
      this._event('page_error', { role, error: String(error?.message || error || 'page error') });
    });
  }

  async _persistSession(page = this._historyPage || this._page) {
    if (!this._context) return;
    try {
      const storageState = await this._context.storageState();
      const sessionStorage = page && !page.isClosed()
        ? await page.evaluate(() => ({
          origin: location.origin,
          entries: Object.fromEntries(Array.from({ length: sessionStorage.length }, (_, i) => {
            const key = sessionStorage.key(i);
            return [key, sessionStorage.getItem(key)];
          })),
        })).catch(() => null)
        : this._sessionStorage;
      const payload = {
        saved_at: new Date().toISOString(), storageState, sessionStorage,
        last_known_urls: {
          history: this._safeUrl((this._historyPage || page)?.url?.()),
          betting: this._safeUrl(this._bettingPage?.url?.()),
        },
        collector_checkpoint: this._health?.snapshot?.().lastRoundId || null,
      };
      const target = path.join(ROOT, 'data', 'bot', 'browser-session-state.json');
      const temp = target + '.' + process.pid + '.tmp';
      fs.mkdirSync(path.dirname(target), { recursive: true });
      fs.writeFileSync(temp, JSON.stringify(payload), { encoding: 'utf8', mode: 0o600 });
      fs.chmodSync(temp, 0o600);
      fs.renameSync(temp, target);
      this._sessionStorage = sessionStorage;
      this._lastSessionPersistAt = Date.now();
    } catch (err) {
      this._event('session_persist_failed', { error: String(err?.message || err) });
    }
  }

  async _restoreSessionStorage(page) {
    if (!page || !this._sessionStorage?.origin || !this._sessionStorage?.entries) return;
    const state = this._sessionStorage;
    await page.addInitScript(({ origin, entries }) => {
      if (location.origin !== origin) return;
      for (const [key, value] of Object.entries(entries)) {
        try { sessionStorage.setItem(key, value); } catch {}
      }
    }, state).catch(() => {});
  }

  async persistSession(page) { await this._persistSession(page); }

  _readProcessMemoryMb(pid) {
    if (!pid) return null;
    try {
      const status = fs.readFileSync('/proc/' + pid + '/status', 'utf8');
      const rss = status.match(/^VmRSS:\s+(\d+)\s+kB$/m);
      return rss ? Math.round(Number(rss[1]) / 1024) : null;
    } catch { return null; }
  }

  _readProcessUptimeSeconds(pid) {
    if (!pid) return null;
    try {
      const stat = fs.readFileSync('/proc/' + pid + '/stat', 'utf8');
      const fields = stat.slice(stat.lastIndexOf(')') + 2).trim().split(/\s+/);
      const startedTicks = Number(fields[19]);
      return Number.isFinite(startedTicks) ? Math.max(0, Math.floor(os.uptime() - startedTicks / 100)) : null;
    } catch { return null; }
  }

  _readProcessCpuPercent(pid) {
    if (!pid) return null;
    try {
      const stat = fs.readFileSync('/proc/' + pid + '/stat', 'utf8');
      const fields = stat.slice(stat.lastIndexOf(')') + 2).trim().split(/\s+/);
      const ticks = Number(fields[11]) + Number(fields[12]);
      const total = fs.readFileSync('/proc/stat', 'utf8').split(/\r?\n/)[0].trim().split(/\s+/)
        .slice(1).reduce((sum, item) => sum + Number(item || 0), 0);
      const previous = this._cpuSample;
      this._cpuSample = { pid, ticks, total, at: Date.now() };
      if (!previous || previous.pid !== pid || total <= previous.total) return null;
      return Math.max(0, Math.round((ticks - previous.ticks) / (total - previous.total) * os.cpus().length * 100));
    } catch { return null; }
  }

  _backendStatus() {
    try {
      const file = path.join(ROOT, 'data', 'backend_lifecycle.jsonl');
      const lines = fs.readFileSync(file, 'utf8').trim().split(/\r?\n/).slice(-1000);
      const events = lines.map(line => { try { return JSON.parse(line); } catch { return null; } }).filter(Boolean);
      const starts = events.filter(event => event.event === 'backend_start');
      const latest = starts.at(-1);
      const latestExit = latest && events.findLast(event => event.pid === latest.pid
        && ['supervisor_exit', 'backend_shutdown'].includes(event.event));
      return {
        pid: latest?.pid || null, ppid: latest?.ppid || null,
        started_at: latest?.timestamp || null, restart_count: Math.max(0, starts.length - 1),
        unexpected_restart_count: events.filter(event => event.event === 'supervisor_exit' && event.unexpected).length,
        uptime_seconds: latest?.timestamp ? Math.max(0, Math.floor((Date.now() - Date.parse(latest.timestamp)) / 1000)) : null,
        memory_rss_mb: this._readProcessMemoryMb(latest?.pid),
        cpu_percent: this._readProcessCpuPercent(latest?.pid),
        running: Boolean(latest && !latestExit),
        last_exit: latestExit ? { signal: latestExit.signal || null, code: latestExit.exit_code ?? null } : null,
      };
    } catch { return { pid: null, restart_count: null, running: null }; }
  }

  _persistMetadata() {
    const health = this._health?.snapshot?.() || {};
    const pages = (() => { try { return this._context?.pages?.() || []; } catch { return []; } })();
    const history = this._historyPage || this._page;
    const browserPid = profileOwnerPid(this._profileDir);
    const lastRound = health.lastRoundTime || null;
    const lastSuccessfulCollection = health.lastSuccessfulCollection || null;
    const state = this._circuitBreaker.state === 'OPEN' ? 'FAILED' : !this.isAlive() ? 'DISCONNECTED'
      : this._lastRecovery?.state === 'RECOVERING' ? 'RECOVERING'
      : !history || history.isClosed?.() || !this.isPageResponsive() ? 'DEGRADED'
        : health.state === 'RECOVERING' ? 'RECOVERING'
          : !health.frameConnected || (lastRound && Date.now() - Date.parse(lastRound) > 180000)
            ? 'DEGRADED' : 'HEALTHY';
    const status = {
      updated_at: new Date().toISOString(), state,
      browser: {
        connected: this.isAlive(), pid: browserPid,
        uptime_seconds: this._readProcessUptimeSeconds(browserPid)
          ?? (this._ownsBrowser && this._browserStartedAt ? Math.floor((Date.now() - this._browserStartedAt) / 1000) : null),
        memory_rss_mb: this._readProcessMemoryMb(browserPid), restart_count: this.restartCount,
        cpu_percent: this._readProcessCpuPercent(browserPid),
      },
      context: { alive: Boolean(this._context && !this._contextClosed && this.isAlive()), page_count: pages.length },
      history_page: { alive: Boolean(history && !history.isClosed?.()), url: this._safeUrl(history?.url?.()) },
      // Betting observes the collector-owned Aviator page through CDP. Keep an
      // explicit shared state instead of opening a second browser or tab.
      betting_page: this._bettingPage && !this._bettingPage.isClosed?.()
        ? { state: /^about:blank/i.test(this._bettingPage.url?.() || '') ? 'NOT_READY' : 'PAGE_OPEN', url: this._safeUrl(this._bettingPage.url?.()) }
        : { state: 'NOT_PROVISIONED', url: null },
      session: health.loggedIn === true ? 'AUTHENTICATED' : health.loggedIn === false ? 'AUTH_REQUIRED' : 'UNKNOWN',
      collector: {
        running: Boolean(health.collectorRunning), state: health.state || 'UNKNOWN',
        last_round_id: health.lastRoundId || null, last_round_timestamp: lastRound,
        last_successful_collection: lastSuccessfulCollection, heartbeat_at: health.heartbeatAt || null,
        iframe_attached: Boolean(health.frameConnected),
      },
      backend: this._backendStatus(), last_disconnect: this._lastDisconnect,
      last_browser_event: this._lastBrowserEvent, last_recovery: this._lastRecovery,
      recovery_count: this._recoveryCount || health.recoveryCount || 0,
      circuit_breaker: {
        state: this._circuitBreaker.state, failures_10m: this._circuitBreaker.failures.length,
        failure_timestamps: this._circuitBreaker.failures,
      },
      diagnostics: {
        page_responsive: this._pageProbeFailures < 3, page_count: pages.length,
        node_memory_rss_mb: Math.round(process.memoryUsage().rss / 1024 / 1024),
      },
    };
    try {
      fs.mkdirSync(path.dirname(RELIABILITY_PATH), { recursive: true });
      const temp = RELIABILITY_PATH + '.' + process.pid + '.tmp';
      fs.writeFileSync(temp, JSON.stringify(status, null, 2), { encoding: 'utf8', mode: 0o600 });
      fs.renameSync(temp, RELIABILITY_PATH);
    } catch {}
  }

  startHeartbeat({ intervalMs = 5000, health = this._health } = {}) {
    this._health = health;
    if (this._healthTimer) return;
    const tick = async () => {
      if (this._healthBusy) return;
      this._healthBusy = true;
      const page = this._historyPage || this._page;
      try {
        if (page && !page.isClosed?.()) {
          await Promise.race([
            page.evaluate(() => document.readyState),
            new Promise((_, reject) => setTimeout(() => reject(new Error('page responsiveness probe timed out')), 2500)),
          ]);
          this._pageProbeFailures = 0;
        } else this._pageProbeFailures += 1;
      } catch (err) {
        this._pageProbeFailures += 1;
        this._event('heartbeat_probe_failed', { error: String(err?.message || err) });
      } finally {
        if (Date.now() - (this._lastSessionPersistAt || 0) >= 60000) {
          await this._persistSession(page);
        }
        this._persistMetadata();
        this._healthBusy = false;
      }
    };
    this._healthTimer = setInterval(tick, intervalMs);
    this._healthTimer.unref?.();
    void tick();
  }

  stopHeartbeat() {
    if (this._healthTimer) clearInterval(this._healthTimer);
    this._healthTimer = null;
  }

  recoveryDiagnostics(reason, extra = {}) {
    const health = this._health?.snapshot?.() || {};
    const pid = profileOwnerPid(this._profileDir);
    const page = this._historyPage || this._page;
    const backend = this._backendStatus();
    const payload = {
      timestamp: new Date().toISOString(), reason, browser_pid: pid, backend_pid: backend.pid,
      browser_uptime_seconds: this._readProcessUptimeSeconds(pid)
        ?? (this._ownsBrowser && this._browserStartedAt ? Math.floor((Date.now() - this._browserStartedAt) / 1000) : null),
      memory_rss_mb: this._readProcessMemoryMb(pid), cpu_percent: this._readProcessCpuPercent(pid),
      last_round_id: health.lastRoundId || null, last_round_received: health.lastRoundTime || null,
      last_browser_event: this._lastBrowserEvent, page_url: this._safeUrl(page?.url?.()),
      page_closed: !page || Boolean(page.isClosed?.()), context_closed: !this._context || this._contextClosed,
      browser_disconnected: !this.isAlive(), iframe_detached: !health.frameConnected,
      playwright_error: extra.playwrightError || null, backend_restart_count: backend.restart_count,
      backend_last_exit: backend.last_exit, os_signal: extra.signal || null, ...extra,
    };
    try {
      fs.mkdirSync(path.dirname(DIAGNOSTICS_PATH), { recursive: true });
      fs.appendFileSync(DIAGNOSTICS_PATH, JSON.stringify(payload) + '\n', { mode: 0o600 });
    } catch {}
    return payload;
  }

  beginRecovery(reason, extra = {}) {
    const diagnostic = this.recoveryDiagnostics(reason, extra);
    this._lastRecovery = { state: 'RECOVERING', at: diagnostic.timestamp, reason };
    this._recoveryCount += 1;
    this._event('recovery_started', { reason });
    this._persistMetadata();
    return diagnostic;
  }

  failRecovery(reason, error) {
    const cutoff = Date.now() - 10 * 60 * 1000;
    this._circuitBreaker.failures = this._circuitBreaker.failures.filter(ts => ts >= cutoff);
    this._circuitBreaker.failures.push(Date.now());
    if (this._circuitBreaker.failures.length >= 5) this._circuitBreaker.state = 'OPEN';
    this._lastRecovery = {
      state: this._circuitBreaker.state === 'OPEN' ? 'CIRCUIT_BREAKER_OPEN' : 'FAILED',
      at: new Date().toISOString(), reason, error: String(error?.message || error),
    };
    this._event('recovery_failed', { reason, error: String(error?.message || error) });
    this._persistMetadata();
  }

  authRequired(reason) {
    this._lastRecovery = { state: 'AUTH_REQUIRED', at: new Date().toISOString(), reason };
    this._event('auth_required', { reason });
    this._persistMetadata();
  }

  completeRecovery(round) {
    this._circuitBreaker.failures = [];
    this._circuitBreaker.state = 'CLOSED';
    this._lastRecovery = {
      state: 'HEALTHY', at: new Date().toISOString(),
      verified_round_id: round?.round_id || null,
      verified_round_timestamp: round?.timestamp || null,
    };
    this._event('recovery_verified', { round_id: round?.round_id || null });
    this._persistMetadata();
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
    if (!this._context || this._contextClosed || !this.isAlive()) return false;
    try { this._context.pages(); return true; } catch { return false; }
  }

  /** Connect to an existing browser or launch a managed browser. */
  async launch(signal, { reconnect = false } = {}) {
    if (this.isAlive() && this.hasContext()) {
      log.info('BrowserManager: reusing existing browser');
      return this._context;
    }
    if (signal?.aborted) throw new Error('Aborted');
    this._acquireSingletonLock();
    await this._close();
    const connected = await this._connectExisting();
    if (connected) return connected;
    const ownerPid = profileOwnerPid(this._profileDir);
    if (ownerPid) {
      throw new Error('BROWSER_PROCESS_STILL_ALIVE pid=' + ownerPid
        + '; refusing to start another Chromium while its profile lock is held');
    }
    if (this._cdpResponsiveEndpoint) {
      throw new Error('BROWSER_CDP_UNRESPONSIVE endpoint=' + this._cdpResponsiveEndpoint
        + '; browser process is present, so launch was withheld');
    }
    return this._launchManaged();
  }

  async _cdpEndpointResponds(endpoint) {
    try {
      const response = await fetch(new URL('/json/version', endpoint), { signal: AbortSignal.timeout(1000) });
      return response.ok;
    } catch { return false; }
  }

  async _connectExisting() {
    this._cdpResponsiveEndpoint = null;
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
        this._contextClosed = false;
        context.once('close', () => {
          this._contextClosed = true;
          this._event('context_closed', { source: 'attached_context' });
          this._persistMetadata();
        });
        this._page = context.pages().find(page => !page.isClosed() && isAviatorPage(page)) || null;
        this._historyPage = this._page;
        this._ownsBrowser = false;
        this._event('browser_attached', { endpoint: this._safeUrl(endpoint) });

        browser.once('disconnected', () => {
          this._lastDisconnect = { at: new Date().toISOString(), reason: 'Playwright CDP disconnected' };
          this._event('browser_disconnected', this._lastDisconnect);
          log.warn('BrowserSupervisor: CDP connection lost; checking process state before recovery');
          this._ownsBrowser = false;
        });

        log.info(this._page
          ? `BrowserManager: connected to existing browser and reusing Aviator tab: ${this._page.url()}`
          : 'BrowserManager: connected to existing browser; Aviator tab not open');
        return this._context;
      } catch (err) {
        if (await this._cdpEndpointResponds(endpoint)) this._cdpResponsiveEndpoint = endpoint;
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
    this._contextClosed = false;

    this._browser = this._context.browser();
    this._page = this._context.pages().find(page => !page.isClosed() && isAviatorPage(page)) ||
      this._context.pages().find(page => !page.isClosed()) ||
      null;
    this._ownsBrowser = true;
    this.restartCount += 1;
    this._browserStartedAt = Date.now();
    this._event('browser_launched', { restart_count: this.restartCount });

    this._context.once('close', () => {
      this._contextClosed = true;
      log.warn('BrowserManager: managed context closed');
      this._lastDisconnect = { at: new Date().toISOString(), reason: 'managed context closed' };
      this._event('context_closed', this._lastDisconnect);
      this._ownsBrowser = false;
    });

    this._browser?.once?.('disconnected', () => {
      this._lastDisconnect = { at: new Date().toISOString(), reason: 'managed browser disconnected' };
      this._event('browser_disconnected', this._lastDisconnect);
    });
    this._persistMetadata();
    log.info('BrowserManager: managed browser ready');
    return this._context;
  }

  /** Get or create a page. Reuses an existing Aviator tab before creating anything. */
  async getPage() {
    if (!this.hasContext()) await this.launch();
    const pages = this._context.pages().filter(page => !page.isClosed());
    const aviatorPage = pages.find(isAviatorPage);
    if (aviatorPage) {
      this._page = aviatorPage;
      this._trackPage(aviatorPage, 'shared_aviator');
      await aviatorPage.bringToFront().catch(() => {});
      return aviatorPage;
    }
    if (this._page && !this._page.isClosed()) {
      this._trackPage(this._page, 'shared_aviator');
      return this._page;
    }
    this._page = await this._context.newPage();
    await this._restoreSessionStorage(this._page);
    this._trackPage(this._page, 'shared_aviator');
    return this._page;
  }

  /** Return the managed game tab, or a dedicated tab in an attached context. */
  async getHistoryPage() {
    if (!this.hasContext()) await this.launch();
    if (this._historyPage && !this._historyPage.isClosed()) return this._historyPage;
    // The managed profile already has a tab. Cloning an Aviator URL into a
    // second tab can make Winner redirect one of them to the sportsbook shell.
    // Attached browsers belong to the user, so give the collector its own tab.
    const managedPage = this._ownsBrowser && (
      this._context.pages().find(page => !page.isClosed() && isAviatorPage(page)) ||
      (this._page && !this._page.isClosed() ? this._page : null) ||
      this._context.pages().find(page => !page.isClosed())
    );
    this._historyPage = managedPage || await this._context.newPage();
    this._trackPage(this._historyPage, 'history');
    if (this._ownsBrowser && /^about:blank/i.test(this._historyPage.url()) && this._lastKnownUrls?.history) {
      try {
        const target = new URL(this._lastKnownUrls.history);
        if (target.hostname === 'winner.rw' && /aviator|crash-games/i.test(target.pathname)) {
          await this._restoreSessionStorage(this._historyPage);
          await this._historyPage.goto(target.href, { waitUntil: 'domcontentloaded', timeout: 20000 });
        }
      } catch (err) {
        this._event('last_known_url_restore_failed', { role: 'history', error: String(err?.message || err) });
      }
    }
    await this._cleanupManagedStarterPages(this._historyPage);
    if (typeof this._historyPage.bringToFront === 'function') {
      await this._historyPage.bringToFront().catch(() => {});
    }
    log.info('BrowserManager: collector page ready in existing context');
    return this._historyPage;
  }

  /** Lazily provision the execution page in the same persistent context. */
  async getBettingPage() {
    if (!this.hasContext()) await this.launch();
    if (this._bettingPage && !this._bettingPage.isClosed()) return this._bettingPage;
    this._bettingPage = await this._context.newPage();
    await this._restoreSessionStorage(this._bettingPage);
    this._trackPage(this._bettingPage, 'betting');
    try {
      const target = new URL(process.env.WINNER_BETTING_PAGE_URL || DEFAULT_BETTING_PAGE_URL);
      if (target.hostname !== 'winner.rw' || !target.pathname.startsWith('/en/')) {
        throw new Error('configured betting page must be an /en/ route on winner.rw');
      }
      const current = new URL(this._bettingPage.url());
      if (current.origin !== target.origin || current.pathname !== target.pathname) {
        await this._bettingPage.goto(target.href, { waitUntil: 'domcontentloaded', timeout: 15000 });
        await this._bettingPage.locator('body').waitFor({ state: 'attached', timeout: 5000 }).catch(() => {});
      }
    } catch (err) {
      log.warn(`BrowserManager: betting page navigation is not ready: ${err.message}`);
    }
    this._persistMetadata();
    return this._bettingPage;
  }

  /** Keep the managed Aviator tab visible after login or recovery. */
  async focusGamePage(page) {
    if (!page || page.isClosed() || !isAviatorPage(page)) return;
    await this._cleanupManagedStarterPages(page);
    if (typeof page.bringToFront === 'function') {
      await page.bringToFront().catch(() => {});
    }
  }

  /**
   * A persistent collector profile can restore old Winner and chrome://newtab
   * pages. Winner's sportsbook shell exposes the untranslated document title
   * "MetaBrandTitle", leaving that broken-looking tab beside the real Aviator
   * page. These are safe to close only when this manager owns the dedicated
   * bot browser; attached user browsers are never modified.
   */
  async _cleanupManagedStarterPages(keepPage) {
    if (!this._ownsBrowser || !this._context) return;
    for (const page of this._context.pages()) {
      if (page === keepPage || page.isClosed()) continue;
      if (page === this._bettingPage) continue;
      const url = page.url().toLowerCase();
      const staleWinnerShell = url.includes('winner.rw') && !isAviatorPage(page);
      const disposableBlank = url === 'about:blank' || url.startsWith('chrome://newtab');
      if (!staleWinnerShell && !disposableBlank) continue;
      try {
        await page.close();
        log.info(`BrowserManager: closed stale managed tab: ${url}`);
      } catch (err) {
        log.warn(`BrowserManager: could not close stale managed tab ${url}: ${err.message}`);
      }
    }
    if (this._page && this._page !== keepPage && this._page.isClosed()) {
      this._page = null;
    }
  }

  /** Recreate only the collector page while preserving this browser/context. */
  async recoverHistoryPage(signal) {
    if (signal?.aborted) throw new Error('Aborted');
    if (!this.hasContext()) {
      await this.restart(signal);
    }
    const previous = this._historyPage;
    const sourceUrl = previous && !previous.isClosed() ? previous.url() : '';
    if (previous && !previous.isClosed()) await this._persistSession(previous);
    // Keep one tab alive while replacing the page. Closing Chrome's last tab
    // first can tear down the entire managed context before newPage() runs.
    const replacement = await this._context.newPage();
    await this._restoreSessionStorage(replacement);
    this._trackPage(replacement, 'history');
    if (previous && !previous.isClosed()) {
      await previous.close().catch(() => {});
    }
    this._historyPage = replacement;
    if (this._page === previous) this._page = replacement;
    if (sourceUrl && sourceUrl !== 'about:blank') {
      await this._historyPage.goto(sourceUrl, { waitUntil: 'domcontentloaded' }).catch(() => {});
    }
    await this._cleanupManagedStarterPages(this._historyPage);
    log.warn('BrowserManager: recreated history page in the existing browser context');
    return this._historyPage;
  }

  /** Close everything cleanly. */
  async _close() {
    this.stopHeartbeat();
    if (this._bettingPage && this._bettingPage !== this._historyPage && !this._bettingPage.isClosed()) {
      try { await this._bettingPage.close(); } catch {}
    }
    if (!this._ownsBrowser && this._historyPage && this._historyPage !== this._page &&
        !this._historyPage.isClosed()) {
      try { await this._historyPage.close(); } catch {}
    }
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
    this._historyPage = null;
    this._bettingPage = null;
    this._ownsBrowser = false;
    this._contextClosed = false;
  }

  /** Release this manager's resources. Attached user browsers are left open. */
  async close() {
    await this._persistSession();
    await this._close();
    this._releaseSingletonLock();
  }

  /** Reconnect or relaunch after browser connection loss. */
  async restart(signal) {
    if (this.isAlive() && this.hasContext()) return this._context;
    const ownerPid = profileOwnerPid(this._profileDir);
    if (ownerPid) {
      const reconnected = await this._connectExisting();
      if (reconnected) return reconnected;
      throw new Error('BROWSER_PROCESS_STILL_ALIVE pid=' + ownerPid
        + '; recovery is paused to prevent a second Chromium process');
    }
    log.warn('BrowserSupervisor: confirmed browser process absence; reconnecting or starting one managed process');
    await this._close();
    return this.launch(signal, { reconnect: true });
  }

  /** Switch to the user's last-used Google Chrome profile after bot-profile failure. */
  async restartWithGoogleProfile(signal) {
    void signal;
    throw new Error('Independent Google-profile fallback is disabled by BrowserSupervisor');
  }

  /** Compatibility alias retained for older callers; never opens a second browser. */
  async restartIncognito(signal) {
    await this.recoverHistoryPage(signal);
    return this._context;
  }
}

let supervisorInstance = null;
export function getBrowserSupervisor(headless = false) {
  if (!supervisorInstance) supervisorInstance = new BrowserSupervisor(headless);
  else if (supervisorInstance.headless !== headless && !supervisorInstance.isAlive()) {
    supervisorInstance.headless = headless;
  }
  return supervisorInstance;
}

// Kept as a source-compatible name for existing imports. There is still only
// one production owner: callers receive the process-wide BrowserSupervisor.
export class BrowserManager extends BrowserSupervisor {
  constructor(headless = false) {
    super(headless);
    if (supervisorInstance && supervisorInstance.isAlive()) return supervisorInstance;
    supervisorInstance = this;
  }
}
