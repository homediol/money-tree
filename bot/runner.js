#!/usr/bin/env node

/**
 * Runner — Main entry point for the Aviator scraping bot.
 *
 * Orchestrates:
 *   1. Load credentials from config.json / environment variables
 *   2. Launch persistent browser (profile or storageState.json)
 *   3. Check session -> login if needed
 *   4. Navigate to Aviator game
 *   5. **Clean history** (deduplication + normalization on startup)
 *   6. Start iframe round monitoring with MutationObserver
 *   7. **Auto-restart watchdog** — if the monitor crashes, re-launch
 *
 * All major actions print structured STEP status messages in real-time.
 *
 * Usage:
 *   node runner.js
 *
 * Environment variables (override config.json):
 *   WINNER_PHONE, WINNER_PASSWORD
 *   BOT_HEADLESS          (true/false)
 *   BOT_CHROME_USER_DATA  (path to Chrome profile)
 *   BOT_STORAGE_STATE     (path to storageState.json)
 *   BOT_USE_STORAGE_STATE (set to "true" to use storageState.json mode)
 *   BOT_MAX_RESTARTS      (max auto-restarts before giving up, default 5)
 *   BOT_POLL_INTERVAL     (ms between polling checks, default 3000)
 */

import fs from 'fs';
import path from 'path';
import readline from 'readline';
import { fileURLToPath } from 'url';
import { launchBrowser, closeBrowser, getPage, saveStorageState } from './browser.js';
import { checkSession, login, goToAviator } from './login.js';
import { monitorRounds, extractAndSave, cleanHistory, setupMutationObserver } from './scraper.js';
import { createLogger } from './logger.js';
import { reportStep, printInfo, printError, printSuccess } from './status.js';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(__dirname, '..');
const CONFIG_PATH = path.join(ROOT, 'data', 'bot', 'config.json');
const STATUS_PATH = path.join(ROOT, 'data', 'bot', 'status.json');
const COMMAND_PATH = path.join(ROOT, 'data', 'bot', 'command.json');
const ROUND_HISTORY_PATH = path.join(ROOT, 'data', 'roundhistory.json');

const log = createLogger('runner');

// ═════════════════════════════════════════════════════════════════════
// Utilities
// ═════════════════════════════════════════════════════════════════════

function readJSON(filepath, defaultVal = null) {
  try {
    if (!fs.existsSync(filepath)) return defaultVal;
    return JSON.parse(fs.readFileSync(filepath, 'utf-8'));
  } catch {
    return defaultVal;
  }
}

function writeJSON(filepath, data) {
  const dir = path.dirname(filepath);
  fs.mkdirSync(dir, { recursive: true });
  const tmp = filepath + '.tmp';
  fs.writeFileSync(tmp, JSON.stringify(data, null, 2), 'utf-8');
  fs.renameSync(tmp, filepath);
}

function consumeCommand(commandName) {
  const command = readJSON(COMMAND_PATH, null);
  if (!command || command.command !== commandName) return false;
  try {
    fs.unlinkSync(COMMAND_PATH);
  } catch {
    // The command is best-effort; stale files are harmless.
  }
  return true;
}

function isAviatorUrl(page) {
  const url = page.url().toLowerCase();
  return url.includes('aviator') || url.includes('crash');
}

async function waitForManualAviator(page, timeoutMs = 45000) {
  const started = Date.now();
  while (!_shuttingDown && Date.now() - started < timeoutMs) {
    if (consumeCommand('start_monitor')) {
      const currentUrl = page.url();
      if (isAviatorUrl(page)) {
        printSuccess(`Manual Aviator confirmation accepted: ${currentUrl}`);
        writeStatus({
          ...readJSON(STATUS_PATH, {}),
          status: 'aviator_loaded',
          aviator_url: currentUrl,
          updated_at: nowISO(),
          step_details: 'Manual confirmation accepted',
        });
        return true;
      }

      printError(`Manual confirmation received, but current page is ${currentUrl}`);
      writeStatus({
        ...readJSON(STATUS_PATH, {}),
        status: 'navigating_aviator',
        error: `Manual confirmation received, but current page is ${currentUrl}`,
        current_url: currentUrl,
        updated_at: nowISO(),
        step_details: 'Open Aviator page, then press the button again',
      });
    }
    await new Promise(r => setTimeout(r, 500));
  }
  return false;
}

function loadCredentials() {
  const config = readJSON(CONFIG_PATH, {});
  return {
    phone:     process.env.WINNER_PHONE     || config.phone     || '',
    password:  process.env.WINNER_PASSWORD  || config.password  || '',
    headless:  (process.env.BOT_HEADLESS || String(config.headless || 'false')).toLowerCase() === 'true',
  };
}

function writeStatus(payload) {
  const current = readJSON(STATUS_PATH, {});
  // Automatically track status transitions as steps
  if (payload.status && payload.status !== current.status) {
    const steps = current.steps || [];
    steps.push({
      step: steps.length + 1,
      action: (STEP_LABELS[payload.status] || payload.status),
      status: (payload.status === 'error' || payload.status === 'crashed') ? 'failed' :
              (payload.status === 'stopped' || payload.status === 'killed' || payload.status === 'login_success' || payload.status === 'browser_ready' || payload.status === 'aviator_loaded') ? 'success' : 'running',
      details: payload.error || (STEP_LABELS[payload.status] || payload.status),
      timestamp: nowISO(),
    });
    if (steps.length > 50) steps.splice(0, steps.length - 50);
    payload.steps = steps;
  }
  writeJSON(STATUS_PATH, payload);
}

const STEP_LABELS = {
  'starting': 'Starting bot',
  'launching_browser': 'Connecting to browser session',
  'browser_ready': 'Browser ready',
  'checking_session': 'Checking login state',
  'logging_in': 'Authenticating',
  'login_success': 'Login successful',
  'navigating_login': 'Opening login page',
  'filling_credentials': 'Entering credentials',
  'navigating_aviator': 'Navigating to Aviator game',
  'aviator_loaded': 'Aviator page loaded',
  'monitoring': 'Monitoring game rounds',
  'restarting': 'Restarting monitor',
  'stopped': 'Bot stopped',
  'error': 'Error occurred',
  'stopping': 'Stopping...',
  'killed': 'Force killed',
  'crashed': 'Monitor crashed',
  'monitor_error': 'Monitor issue',
  'opening_page': 'Opening browser page',
  'page_ready': 'Page tab ready',
  'preparing_monitor': 'Preparing monitoring loop',
  'prepared_monitor': 'Monitoring ready',
};

function nowISO() {
  return new Date().toISOString();
}

// ═════════════════════════════════════════════════════════════════════
// Terminal input
// ═════════════════════════════════════════════════════════════════════

function askQuestion(question) {
  const rl = readline.createInterface({
    input: process.stdin,
    output: process.stdout,
  });
  return new Promise(resolve => {
    rl.question(question, answer => {
      rl.close();
      resolve(answer);
    });
  });
}

// ═════════════════════════════════════════════════════════════════════
// Signal handling (graceful shutdown)
// ═════════════════════════════════════════════════════════════════════

let _shuttingDown = false;
const abortController = new AbortController();

async function shutdown(exitCode = 0) {
  if (_shuttingDown) return;
  _shuttingDown = true;
  abortController.abort();

  log.info('Shutting down bot...');
  writeStatus({
    ...readJSON(STATUS_PATH, {}),
    status: 'stopped',
    updated_at: nowISO(),
  });

  if (process.env.BOT_USE_STORAGE_STATE === 'true') {
    await saveStorageState();
  }

  await closeBrowser();
  log.info('Bot terminated.');
  process.exit(exitCode);
}

process.on('SIGTERM', () => shutdown(0));
process.on('SIGINT',  () => shutdown(0));
process.on('SIGHUP',  () => shutdown(0));

process.on('uncaughtException', (err) => {
  log.error(`Uncaught exception: ${err.message}`);
  if (err.stack) log.error(err.stack.slice(0, 500));
  writeStatus({
    ...readJSON(STATUS_PATH, {}),
    status: 'error',
    error: `Uncaught exception: ${err.message}`,
    updated_at: nowISO(),
  });
  shutdown(1);
});

process.on('unhandledRejection', (reason) => {
  log.error(`Unhandled rejection: ${reason}`);
});

// ═════════════════════════════════════════════════════════════════════
// Watchdog — auto-restart on crash
// ═════════════════════════════════════════════════════════════════════

let restartCount = 0;
const MAX_RESTARTS = parseInt(process.env.BOT_MAX_RESTARTS || '5', 10);

/**
 * Run the monitoring loop with crash recovery.
 * If the monitor exits unexpectedly, re-launch everything.
 */
async function runWithWatchdog(page) {
  while (!_shuttingDown) {
    try {
      log.info('Starting monitoring cycle');
      printInfo('Monitoring cycle started');

      const result = await monitorRounds(page, {
        signal: abortController.signal,
      });

      // Normal exit — stop signal or page crash
      if (result) {
        log.info(`Monitor returned: ${JSON.stringify(result)}`);
      }
      break;

    } catch (err) {
      restartCount++;
      log.error(`Monitor crashed (restart ${restartCount}/${MAX_RESTARTS}): ${err.message}`);

      writeStatus({
        ...readJSON(STATUS_PATH, {}),
        status: 'restarting',
        restart_count: restartCount,
        error: `Monitor crashed: ${err.message}`,
        updated_at: nowISO(),
      });

      if (restartCount >= MAX_RESTARTS) {
        log.error(`Max restarts (${MAX_RESTARTS}) reached — giving up`);
        printError(`Monitor crashed ${restartCount} times — max restarts reached`);
        break;
      }

      // Backoff delay before restart (increases with each restart)
      const backoff = Math.min(1000 * Math.pow(2, restartCount), 30000);
      printError(`Monitor crashed — restarting in ${backoff}ms (attempt ${restartCount}/${MAX_RESTARTS})`);
      log.info(`Backoff ${backoff}ms before restart`);
      await new Promise(r => setTimeout(r, backoff));

      // Re-navigate to Aviator and re-install observer
      try {
        await goToAviator(page);
        await setupMutationObserver(page);
        printSuccess('Reconnected to Aviator page');
      } catch (navErr) {
        log.error(`Re-navigation failed: ${navErr.message}`);
      }
    }
  }
}

// ═════════════════════════════════════════════════════════════════════
// Main
// ═════════════════════════════════════════════════════════════════════

async function main() {
  // ── Step 1 — Load configuration ─────────────────────────────────
  const { phone, password, headless } = loadCredentials();

  if (!phone || !password) {
    reportStep({
      action: 'Loading credentials',
      status: 'failed',
      details: 'Phone or password missing. Set via config.json or WINNER_PHONE/WINNER_PASSWORD env vars.',
    });
    writeStatus({ status: 'error', error: 'Missing credentials', updated_at: nowISO() });
    process.exit(1);
  }

  const maskedPhone = phone.slice(-4).padStart(phone.length, '*');
  log.info(`Starting bot for phone ${maskedPhone} (headless=${headless})`);

  writeStatus({
    status: 'starting',
    phone,
    headless,
    pid: process.pid,
    started_at: nowISO(),
    updated_at: nowISO(),
    rounds_seen: 0,
    last_round: null,
    error: null,
  });

  // ── Step 0 — Clean history (dedup + normalize on every startup) ─
  reportStep({
    action: 'Cleaning round history',
    status: 'running',
    details: 'Normalizing values, removing duplicates, ensuring ML-ready format',
  });
  writeStatus({ ...readJSON(STATUS_PATH, {}), status: 'starting', updated_at: nowISO(), step_details: 'Cleaning history...' });

  try {
    const stats = cleanHistory();
    if (stats.removed > 0) {
      printInfo(`Data quality: removed ${stats.removed} duplicate/invalid records (${stats.before} → ${stats.after})`);
    } else {
      printInfo(`Data quality: ${stats.after} records clean, no duplicates found`);
    }
    reportStep({
      action: 'Cleaning round history',
      status: 'success',
      details: `History normalized: ${stats.after} records (removed ${stats.removed} duplicates)`,
    });
    writeStatus({ ...readJSON(STATUS_PATH, {}), status: 'starting', updated_at: nowISO(), step_details: `History: ${stats.after} records` });
  } catch (err) {
    log.warn(`History cleaning failed (non-fatal): ${err.message}`);
    reportStep({
      action: 'Cleaning round history',
      status: 'success',
      details: 'Could not clean history (file may not exist yet)',
    });
    writeStatus({ ...readJSON(STATUS_PATH, {}), status: 'starting', updated_at: nowISO(), step_details: 'No history to clean' });
  }

  const useStorageState = process.env.BOT_USE_STORAGE_STATE === 'true';

  // ── Step 1 — Connect to existing browser ────────────────────────
  reportStep({
    action: 'Connecting to browser session',
    status: 'running',
    details: 'Attaching to an existing Chrome instance through CDP',
  });

  try {
    log.info('Connecting to existing browser...');
    writeStatus({ ...readJSON(STATUS_PATH, {}), status: 'launching_browser', updated_at: nowISO(), step_details: 'Connecting to existing browser' });

    const launchOpts = { headless };
    if (useStorageState) {
      launchOpts.useStorageState = true;
      launchOpts.storageStatePath = process.env.BOT_STORAGE_STATE || undefined;
    }

    await launchBrowser(launchOpts);

    reportStep({
      action: 'Connecting to browser session',
      status: 'success',
      details: 'Connected to existing browser session',
    });
    writeStatus({ ...readJSON(STATUS_PATH, {}), status: 'browser_ready', updated_at: nowISO(), step_details: 'Browser ready' });
  } catch (err) {
    log.error(`Failed to connect to browser: ${err.message}`);
    reportStep({
      action: 'Connecting to browser session',
      status: 'failed',
      details: `Browser connection failed: ${err.message}`,
    });
    writeStatus({ ...readJSON(STATUS_PATH, {}), status: 'error', error: 'Browser connection failed: ' + err.message, updated_at: nowISO(), step_details: err.message });
    process.exit(1);
  }

  // ── Step 2 — Get page ───────────────────────────────────────────
  reportStep({
    action: 'Opening browser page',
    status: 'running',
    details: 'Reusing Aviator tab or opening one tab in the existing browser',
  });
  writeStatus({ ...readJSON(STATUS_PATH, {}), status: 'opening_page', updated_at: nowISO(), step_details: 'Reusing or opening tab' });

  let page;
  try {
    page = await getPage();
    reportStep({
      action: 'Opening browser page',
      status: 'success',
      details: 'Page tab ready',
    });
    writeStatus({ ...readJSON(STATUS_PATH, {}), status: 'page_ready', updated_at: nowISO(), step_details: 'Page tab ready' });
  } catch (err) {
    log.error('Failed to get page: ' + err.message);
    reportStep({
      action: 'Opening browser page',
      status: 'failed',
      details: `Could not create page: ${err.message}`,
    });
    writeStatus({ ...readJSON(STATUS_PATH, {}), status: 'error', error: 'Get page failed: ' + err.message, updated_at: nowISO(), step_details: err.message });
    await shutdown(1);
    return;
  }

  // ── Step 3 — Check authentication state ─────────────────────────
  reportStep({
    action: 'Checking login state',
    status: 'running',
    details: 'Detecting session cookies and dashboard UI indicators on homepage',
  });

  try {
    writeStatus({ ...readJSON(STATUS_PATH, {}), status: 'checking_session', updated_at: nowISO(), step_details: 'Checking session...' });
    const onExistingAviatorTab = isAviatorUrl(page);
    const { loggedIn } = onExistingAviatorTab
      ? { loggedIn: true }
      : await checkSession(page);

    if (loggedIn) {
      printSuccess(onExistingAviatorTab
        ? 'Existing Aviator tab detected — preserving current session'
        : 'Dashboard elements detected — user is authenticated');
      reportStep({
        action: 'Checking login state',
        status: 'success',
        details: onExistingAviatorTab
          ? 'Reusing existing Aviator tab without navigating away'
          : 'User is already authenticated, skipping login page entirely',
      });
      writeStatus({ ...readJSON(STATUS_PATH, {}), status: 'login_success', updated_at: nowISO(), step_details: 'Already authenticated' });
    } else {
      // ── Step 3b — Login (only if no active session) ─────────────
      printInfo('No active session detected — proceeding to login');
      reportStep({
        action: 'Performing login',
        status: 'running',
        details: `Authenticating as ${maskedPhone} via homepage LOGIN link`,
        repeatStep: false,
      });

      writeStatus({ ...readJSON(STATUS_PATH, {}), status: 'logging_in', updated_at: nowISO(), step_details: `Authenticating ${maskedPhone}` });
      const loginOk = await login(page, phone, password);

      if (!loginOk) {
        log.error('Login failed — aborting');
        reportStep({
          action: 'Performing login',
          status: 'failed',
          details: 'Credentials rejected or login form not accessible',
        });
        writeStatus({ ...readJSON(STATUS_PATH, {}), status: 'error', error: 'Login failed', updated_at: nowISO(), step_details: 'Login failed' });
        await shutdown(1);
        return;
      }

      printSuccess('Authentication successful');
      reportStep({
        action: 'Performing login',
        status: 'success',
        details: 'Logged in successfully',
      });
    }

    writeStatus({ ...readJSON(STATUS_PATH, {}), status: 'login_success', updated_at: nowISO(), step_details: 'Logged in successfully' });
  } catch (err) {
    log.error('Authentication error: ' + err.message);
    reportStep({
      action: 'Authentication check',
      status: 'failed',
      details: `Error during session check or login: ${err.message}`,
    });
    writeStatus({ ...readJSON(STATUS_PATH, {}), status: 'error', error: 'Auth error: ' + err.message, updated_at: nowISO(), step_details: err.message });
    await shutdown(1);
    return;
  }

  // ── Step 4 — Navigate to Aviator game page ──────────────────────
  reportStep({
    action: 'Navigating to Aviator game',
    status: 'running',
    details: 'Clicking Aviator link from homepage navigation bar',
  });
  writeStatus({ ...readJSON(STATUS_PATH, {}), status: 'navigating_aviator', updated_at: nowISO(), step_details: 'Navigating...' });

  try {
    log.info('Navigating to Aviator game...');

    let aviatorOk = isAviatorUrl(page) || await goToAviator(page);
    if (!aviatorOk) {
      printInfo('Open the Aviator game manually in the connected Chrome window, then press the dashboard start button.');
      aviatorOk = await waitForManualAviator(page, 90000);
    }
    if (!aviatorOk) {
      log.error('Failed to reach Aviator page — aborting');
      reportStep({
        action: 'Navigating to Aviator game',
        status: 'failed',
        details: 'Could not navigate to /en/virtual/crash-games/aviator via any method',
      });
      writeStatus({ ...readJSON(STATUS_PATH, {}), status: 'error', error: 'Failed to reach Aviator page', updated_at: nowISO(), step_details: 'Failed to reach Aviator' });
      await shutdown(1);
      return;
    }

    const aviatorUrl = page.url();
    printSuccess(`Aviator page loaded: ${aviatorUrl}`);
    reportStep({
      action: 'Navigating to Aviator game',
      status: 'success',
      details: `Game page loaded at ${aviatorUrl}`,
    });
    writeStatus({ ...readJSON(STATUS_PATH, {}), status: 'aviator_loaded', aviator_url: aviatorUrl, updated_at: nowISO(), step_details: 'Aviator loaded' });
  } catch (err) {
    log.error('Navigation error: ' + err.message);
    reportStep({
      action: 'Navigating to Aviator game',
      status: 'failed',
      details: `Navigation error: ${err.message}`,
    });
    writeStatus({ ...readJSON(STATUS_PATH, {}), status: 'error', error: 'Navigation error: ' + err.message, updated_at: nowISO(), step_details: err.message });
    await shutdown(1);
    return;
  }

  // ── Step 5 — Pre-monitoring confirmation ────────────────────────
  reportStep({
    action: 'Preparing monitoring loop',
    status: 'running',
    details: 'Awaiting signal to start round data collection',
  });
  writeStatus({ ...readJSON(STATUS_PATH, {}), status: 'preparing_monitor', updated_at: nowISO(), step_details: 'Preparing...' });

  if (consumeCommand('start_monitor')) {
    log.info('Manual start signal received — starting monitor immediately');
    printSuccess('Manual start accepted — monitoring now');
  } else {
  // Determine if we have an interactive terminal.
  // When launched from the backend API (bot_runner.py), stdin is a pipe,
  // so isTTY is false — skip the prompt and auto-continue.
  const hasTTY = process.stdin.isTTY;

  if (hasTTY) {
    try {
      log.info('Waiting for user confirmation to start scraping...');
      await askQuestion('Press ENTER to continue scraping... ');
      log.info('User confirmed — starting scraping loop');
    } catch (err) {
      log.warn('Cannot read user input: ' + err.message);
      log.info('Proceeding with scraping after 3-second delay...');
      await new Promise(r => setTimeout(r, 3000));
    }
  } else {
    log.info('Non-interactive mode (no TTY) — auto-continuing after 3 seconds...');
    printInfo('Non-interactive mode — auto-continuing');
    for (let elapsed = 0; elapsed < 3000; elapsed += 500) {
      if (consumeCommand('start_monitor')) {
        printSuccess('Manual start accepted — monitoring now');
        break;
      }
      await new Promise(r => setTimeout(r, 500));
    }
  }
  }

  reportStep({
    action: 'Preparing monitoring loop',
    status: 'success',
    details: 'Monitoring ready to start',
  });
  writeStatus({ ...readJSON(STATUS_PATH, {}), status: 'prepared_monitor', updated_at: nowISO(), step_details: 'Ready to monitor' });

  // ── Step 6 — Start monitoring rounds (with watchdog) ────────────
  reportStep({
    action: 'Monitoring game rounds',
    status: 'running',
    details: 'Watching the Spribe Aviator iframe with MutationObserver for instant round updates',
  });

  try {
    log.info('Entering monitoring loop with watchdog...');
    writeStatus({ ...readJSON(STATUS_PATH, {}), status: 'monitoring', restart_count: 0, updated_at: nowISO(), step_details: 'Monitoring started' });

    // Run with auto-restart on crash
    await runWithWatchdog(page);

    log.info('Monitoring loop completed normally');

    if (restartCount > 0) {
      printInfo(`Monitor required ${restartCount} restart(s) during this session`);
    }

  } catch (err) {
    log.error('Monitoring error: ' + err.message);
    reportStep({
      action: 'Monitoring game rounds',
      status: 'failed',
      details: `Monitoring loop error: ${err.message}`,
    });
    writeStatus({
      ...readJSON(STATUS_PATH, {}),
      status: 'error',
      error: 'Monitoring error: ' + err.message,
      updated_at: nowISO(),
      step_details: err.message,
    });
  }

  // ── Done — Shutdown ────────────────────────────────────────────
  printSuccess('Bot session complete');
  reportStep({
    action: 'Bot shutdown',
    status: 'success',
    details: 'Browser closed, resources released',
  });
  writeStatus({ ...readJSON(STATUS_PATH, {}), status: 'stopped', updated_at: nowISO(), step_details: 'Bot shutdown complete' });

  await shutdown(0);
}

main();
