/**
 * Aviator round history collector for Winner.rw.
 *
 * This module assumes the login/navigation flow has already opened the
 * Aviator page. It then:
 *   - Finds the Spribe/Aviator iframe automatically.
 *   - Waits for ".payouts-block .payout" inside that iframe.
 *   - Installs a MutationObserver inside the iframe.
 *   - Saves a new record only when the payout snapshot advances.
 *   - Keeps data/roundhistory.json as ML-ready chronological records.
 *
 * The newest payout in the DOM is assumed to be the first element.
 */

import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';
import { createLogger } from './logger.js';
import { reportStep, printInfo, printError, printSuccess } from './status.js';
import { PostgresRoundStore } from './collector/PostgresRoundStore.js';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(__dirname, '..');
const STATUS_PATH = path.join(ROOT, 'data', 'bot', 'status.json');
const ROUND_HISTORY_PATH = path.join(ROOT, 'data', 'roundhistory.json');

const log = createLogger('scraper');
let postgresStorePromise = null;

// ---------------------------------------------------------------------------
// Selectors and tuning knobs
// ---------------------------------------------------------------------------

export const PAYOUT_SELECTOR = '.payouts-block .payout';

const FRAME_WAIT_TIMEOUT_MS = Number(process.env.BOT_FRAME_WAIT_TIMEOUT || 120000);
const PAYOUT_WAIT_TIMEOUT_MS = Number(process.env.BOT_PAYOUT_WAIT_TIMEOUT || 120000);
const MUTATION_IDLE_TIMEOUT_MS = Number(process.env.BOT_MUTATION_IDLE_TIMEOUT || 90000);
const RECONNECT_BASE_DELAY_MS = 1000;
const RECONNECT_MAX_DELAY_MS = 10000;

const AVIATOR_FRAME_HINTS = [
  'spribe',
  'spribegaming',
  'aviator',
  'crash',
  'crash-games',
  'turbo-games',
  'game',
];

// ---------------------------------------------------------------------------
// File and status helpers
// ---------------------------------------------------------------------------

function nowISO() {
  return new Date().toISOString();
}

function ensureDirectory(filepath) {
  fs.mkdirSync(path.dirname(filepath), { recursive: true });
}

function readJSON(filepath, defaultValue) {
  try {
    if (!fs.existsSync(filepath)) return defaultValue;
    return JSON.parse(fs.readFileSync(filepath, 'utf-8'));
  } catch (err) {
    log.warn(`Could not read JSON at ${filepath}: ${err.message}`);
    return defaultValue;
  }
}

function writeJSON(filepath, payload) {
  ensureDirectory(filepath);
  const tmp = `${filepath}.tmp`;
  fs.writeFileSync(tmp, JSON.stringify(payload, null, 2), 'utf-8');
  fs.renameSync(tmp, filepath);
}

function readStatus(defaultValue = {}) {
  return readJSON(STATUS_PATH, defaultValue);
}

function writeStatus(patch) {
  try {
    writeJSON(STATUS_PATH, {
      ...readStatus({}),
      ...patch,
      updated_at: nowISO(),
    });
  } catch (err) {
    log.warn(`Could not write bot status: ${err.message}`);
  }
}

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

// ---------------------------------------------------------------------------
// Multiplier and history normalization
// ---------------------------------------------------------------------------

/**
 * Convert DOM text such as "2.03x" or "10.33X" to a numeric multiplier.
 */
export function normalizeMultiplier(raw) {
  if (typeof raw === 'number') {
    if (!Number.isFinite(raw) || raw <= 0) return null;
    return Math.round(raw * 100) / 100;
  }

  if (typeof raw !== 'string') return null;

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

function coerceHistoryArray(raw) {
  if (Array.isArray(raw)) return raw;
  if (raw && Array.isArray(raw.rounds)) return raw.rounds;
  if (raw && Array.isArray(raw.history)) return raw.history;
  return [];
}

function normalizeHistoryRecord(item, fallbackIndex) {
  const multiplier = normalizeMultiplier(
    typeof item === 'object' && item !== null
      ? item.multiplier ?? item.crashPoint ?? item.value
      : item
  );

  if (multiplier === null || multiplier < 1) return null;

  const roundIndexRaw = typeof item === 'object' && item !== null
    ? item.round_index ?? item.round_id ?? item.id
    : null;
  const roundIndex = Number.isFinite(Number(roundIndexRaw))
    ? Number(roundIndexRaw)
    : fallbackIndex + 1;

  const timestamp = typeof item === 'object' && item !== null
    ? item.timestamp ?? item.time ?? item.ts ?? null
    : null;

  return {
    multiplier,
    timestamp,
    round_index: roundIndex,
  };
}

function normalizeHistory(raw) {
  const rows = coerceHistoryArray(raw);
  const cleaned = [];
  const seenExactRecords = new Set();

  rows.forEach((item, index) => {
    const record = normalizeHistoryRecord(item, index);
    if (!record) return;

    // Remove duplicate file records without removing legitimate repeated
    // multipliers from different rounds.
    const key = `${record.round_index}|${record.timestamp || ''}|${record.multiplier.toFixed(2)}`;
    if (seenExactRecords.has(key)) return;
    seenExactRecords.add(key);
    cleaned.push(record);
  });

  return sortHistoryChronological(cleaned);
}

function readRoundHistory() {
  return normalizeHistory(readJSON(ROUND_HISTORY_PATH, []));
}

function sortHistoryChronological(records) {
  return [...records].sort((a, b) => {
    const aIndex = Number(a.round_index);
    const bIndex = Number(b.round_index);
    if (Number.isFinite(aIndex) && Number.isFinite(bIndex) && aIndex !== bIndex) {
      return aIndex - bIndex;
    }

    const aTime = Date.parse(a.timestamp || '');
    const bTime = Date.parse(b.timestamp || '');
    if (Number.isFinite(aTime) && Number.isFinite(bTime) && aTime !== bTime) {
      return aTime - bTime;
    }

    return 0;
  });
}

function writeRoundHistory(records) {
  writeJSON(ROUND_HISTORY_PATH, sortHistoryChronological(records));
}

async function getPostgresRoundStore() {
  if (!postgresStorePromise) {
    postgresStorePromise = (async () => {
      const store = new PostgresRoundStore();
      await store.init();

      const fileHistory = readRoundHistory();
      if (fileHistory.length > 0) {
        const { saved } = await store.importRounds(fileHistory);
        log.info(`Imported ${saved} round(s) from data/roundhistory.json into PostgreSQL`);
      }

      return store;
    })();
  }

  return postgresStorePromise;
}

async function loadPostgresRoundHistory() {
  const store = await getPostgresRoundStore();
  return store.loadRounds();
}

function nextRoundIndex(history) {
  const maxExisting = history.reduce((max, record, index) => {
    const value = Number(record.round_index ?? record.round_id ?? index + 1);
    return Number.isFinite(value) ? Math.max(max, value) : max;
  }, 0);
  return maxExisting + 1;
}

function formatMultiplier(value) {
  return `${Number(value).toFixed(2)}x`;
}

function snapshotSignature(multipliers) {
  return multipliers.map(value => Number(value).toFixed(2)).join('|');
}

function sameMultiplierAt(a, b) {
  return Number(a).toFixed(2) === Number(b).toFixed(2);
}

/**
 * The DOM history is newest-first. If we reconnect after one or more missed
 * rounds, infer the new prefix by aligning the previous snapshot inside the
 * current snapshot. If alignment fails, save only the current newest value.
 */
function inferNewMultipliers(previousSnapshot, currentSnapshot) {
  if (!previousSnapshot?.length || !currentSnapshot?.length) return [];

  if (snapshotSignature(previousSnapshot) === snapshotSignature(currentSnapshot)) {
    return [];
  }

  const maxShift = Math.min(currentSnapshot.length, 25);

  for (let shift = 1; shift <= maxShift; shift += 1) {
    const compareLength = Math.min(10, previousSnapshot.length, currentSnapshot.length - shift);
    if (compareLength <= 0) continue;

    let aligned = true;
    for (let i = 0; i < compareLength; i += 1) {
      if (!sameMultiplierAt(currentSnapshot[shift + i], previousSnapshot[i])) {
        aligned = false;
        break;
      }
    }

    if (aligned) return currentSnapshot.slice(0, shift);
  }

  return [currentSnapshot[0]];
}

function appendRounds(history, newestFirstMultipliers) {
  if (!newestFirstMultipliers.length) {
    return { history, addedRecords: [] };
  }

  const addedRecords = [];
  let roundIndex = nextRoundIndex(history);

  // The file is chronological for model training, so reverse the new prefix.
  for (const multiplier of [...newestFirstMultipliers].reverse()) {
    const normalized = normalizeMultiplier(multiplier);
    if (normalized === null || normalized < 1) continue;

    const record = {
      multiplier: normalized,
      timestamp: nowISO(),
      round_index: roundIndex,
    };

    history.push(record);
    addedRecords.push(record);
    roundIndex += 1;
  }

  return {
    history: sortHistoryChronological(history),
    addedRecords,
  };
}

// ---------------------------------------------------------------------------
// Frame discovery
// ---------------------------------------------------------------------------

function safeFrameUrl(frame) {
  try {
    return frame.url() || 'about:blank';
  } catch {
    return 'detached';
  }
}

function safeFrameName(frame) {
  try {
    return frame.name() || '';
  } catch {
    return '';
  }
}

function describeFrame(frame) {
  const name = safeFrameName(frame) || '(no name)';
  return `name="${name}" url="${safeFrameUrl(frame)}"`;
}

function frameLooksLikeAviator(frame) {
  const haystack = `${safeFrameName(frame)} ${safeFrameUrl(frame)}`.toLowerCase();
  return AVIATOR_FRAME_HINTS.some(hint => haystack.includes(hint));
}

function frameIsUsable(page, frame) {
  try {
    return frame !== page.mainFrame() && !frame.isDetached();
  } catch {
    return false;
  }
}

async function frameHasPayouts(frame, timeout = 350) {
  try {
    await frame.locator(PAYOUT_SELECTOR).first().waitFor({ state: 'attached', timeout });
    return true;
  } catch {
    return false;
  }
}

async function findAviatorFrame(page) {
  const frames = page.frames().filter(frame => frameIsUsable(page, frame));

  // Prefer frames whose URL/name looks like Spribe, but still inspect every
  // iframe because some providers mask or delay the final URL.
  const sorted = [
    ...frames.filter(frameLooksLikeAviator),
    ...frames.filter(frame => !frameLooksLikeAviator(frame)),
  ];

  for (const frame of sorted) {
    if (await frameHasPayouts(frame)) {
      return { frame, foundBy: 'payout selector' };
    }
  }

  const hintedFrame = sorted.find(frameLooksLikeAviator);
  if (hintedFrame) {
    return { frame: hintedFrame, foundBy: 'Spribe/Aviator URL hint' };
  }

  return null;
}

async function waitForFrameActivity(page, timeoutMs, signal) {
  if (signal?.aborted) return;

  const waits = [
    page.waitForEvent('frameattached', { timeout: timeoutMs }).catch(() => null),
    page.waitForEvent('framenavigated', { timeout: timeoutMs }).catch(() => null),
    page.waitForEvent('framedetached', { timeout: timeoutMs }).catch(() => null),
    sleep(timeoutMs, signal),
  ];

  await Promise.race(waits);
}

async function waitForAviatorFrame(page, options = {}) {
  const timeoutMs = options.timeoutMs || FRAME_WAIT_TIMEOUT_MS;
  const signal = options.signal || null;
  const deadline = Date.now() + timeoutMs;

  while (!signal?.aborted && Date.now() < deadline) {
    const found = await findAviatorFrame(page);
    if (found) {
      log.info(`Aviator frame found (${found.foundBy}): ${describeFrame(found.frame)}`);
      printSuccess(`Aviator frame found: ${safeFrameUrl(found.frame)}`);
      return found.frame;
    }

    writeStatus({
      status: 'waiting_frame',
      step_details: 'Waiting for Spribe Aviator iframe',
      current_url: page.url(),
    });

    await waitForFrameActivity(page, Math.min(5000, Math.max(250, deadline - Date.now())), signal);
  }

  throw new Error('Timed out waiting for Spribe Aviator iframe');
}

// ---------------------------------------------------------------------------
// Iframe DOM extraction and MutationObserver bridge
// ---------------------------------------------------------------------------

async function readMultipliersFromFrame(frame) {
  const values = await frame.evaluate((selector) => {
    function parseMultiplier(text) {
      if (!text) return null;
      const cleaned = String(text)
        .trim()
        .replace(/,/g, '')
        .replace(/[xX×]/g, '')
        .replace(/[^0-9.]/g, '');
      if (!cleaned) return null;
      const value = Number.parseFloat(cleaned);
      if (!Number.isFinite(value) || value <= 0) return null;
      return Math.round(value * 100) / 100;
    }

    return Array.from(document.querySelectorAll(selector))
      .map(element => parseMultiplier(element.textContent))
      .filter(value => Number.isFinite(value) && value > 0);
  }, PAYOUT_SELECTOR);

  return values.map(normalizeMultiplier).filter(value => value !== null);
}

async function waitForPayoutHistory(frame, options = {}) {
  const timeoutMs = options.timeoutMs || PAYOUT_WAIT_TIMEOUT_MS;

  log.info('Waiting for payout history');
  printInfo('Waiting for payout history');

  await frame.locator(PAYOUT_SELECTOR).first().waitFor({
    state: 'attached',
    timeout: timeoutMs,
  });

  const multipliers = await readMultipliersFromFrame(frame);
  if (!multipliers.length) {
    throw new Error('Payout history selector exists, but no numeric multipliers were found');
  }

  return multipliers;
}

async function installMutationObserverInFrame(frame) {
  const result = await frame.evaluate((selector) => {
    function parseMultiplier(text) {
      if (!text) return null;
      const cleaned = String(text)
        .trim()
        .replace(/,/g, '')
        .replace(/[xX×]/g, '')
        .replace(/[^0-9.]/g, '');
      if (!cleaned) return null;
      const value = Number.parseFloat(cleaned);
      if (!Number.isFinite(value) || value <= 0) return null;
      return Math.round(value * 100) / 100;
    }

    function readMultipliers() {
      return Array.from(document.querySelectorAll(selector))
        .map(element => parseMultiplier(element.textContent))
        .filter(value => Number.isFinite(value) && value > 0);
    }

    function signature(values) {
      return values.map(value => Number(value).toFixed(2)).join('|');
    }

    const firstPayout = document.querySelector(selector);
    const payoutsBlock = firstPayout?.closest('.payouts-block') || document.querySelector('.payouts-block');
    const observeTarget = payoutsBlock?.parentElement || payoutsBlock || document.body;

    if (!firstPayout || !payoutsBlock || !observeTarget) {
      return { ok: false, error: 'payout history target not found' };
    }

    if (window.__aviatorRoundCollector?.observer) {
      window.__aviatorRoundCollector.observer.disconnect();
    }

    const collector = {
      queue: [],
      waiter: null,
      lastSignature: signature(readMultipliers()),
      observer: null,
    };

    function publish(reason) {
      const multipliers = readMultipliers();
      if (!multipliers.length) return;

      const nextSignature = signature(multipliers);
      if (nextSignature === collector.lastSignature) return;
      collector.lastSignature = nextSignature;

      const event = {
        reason,
        timestamp: new Date().toISOString(),
        multipliers,
        count: multipliers.length,
        newest: multipliers[0],
        signature: nextSignature,
        url: window.location.href,
      };

      if (collector.waiter) {
        const waiter = collector.waiter;
        collector.waiter = null;
        waiter(event);
        return;
      }

      collector.queue.push(event);
      if (collector.queue.length > 25) {
        collector.queue = collector.queue.slice(-25);
      }
    }

    const observer = new MutationObserver((mutations) => {
      const relevant = mutations.some(mutation => {
        if (mutation.type === 'childList') return true;
        if (mutation.type === 'characterData') return true;
        if (mutation.type === 'attributes') {
          return ['class', 'style', 'hidden'].includes(mutation.attributeName);
        }
        return false;
      });

      if (relevant) publish('mutation');
    });

    observer.observe(observeTarget, {
      childList: true,
      subtree: true,
      characterData: true,
      attributes: true,
      attributeFilter: ['class', 'style', 'hidden'],
    });

    collector.observer = observer;
    window.__aviatorRoundCollector = collector;

    const multipliers = readMultipliers();

    return {
      ok: true,
      count: multipliers.length,
      newest: multipliers[0],
      multipliers,
      signature: signature(multipliers),
      url: window.location.href,
    };
  }, PAYOUT_SELECTOR);

  if (!result?.ok) {
    throw new Error(result?.error || 'Could not install MutationObserver in Aviator frame');
  }

  return {
    ...result,
    multipliers: result.multipliers.map(normalizeMultiplier).filter(value => value !== null),
  };
}

async function waitForNextPayoutMutation(frame, timeoutMs = MUTATION_IDLE_TIMEOUT_MS) {
  const event = await frame.evaluate(({ timeoutMs }) => {
    const collector = window.__aviatorRoundCollector;
    if (!collector) {
      return { error: 'collector_not_installed' };
    }

    if (collector.queue.length > 0) {
      return collector.queue.shift();
    }

    return new Promise(resolve => {
      let settled = false;

      const finish = (payload) => {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        if (collector.waiter === finish) {
          collector.waiter = null;
        }
        resolve(payload);
      };

      const timer = setTimeout(() => {
        finish({ timeout: true, timestamp: new Date().toISOString() });
      }, timeoutMs);

      collector.waiter = finish;
    });
  }, { timeoutMs });

  if (event?.multipliers) {
    event.multipliers = event.multipliers.map(normalizeMultiplier).filter(value => value !== null);
    event.newest = event.multipliers[0] ?? null;
    event.signature = snapshotSignature(event.multipliers);
  }

  return event;
}

async function connectToAviatorFrame(page, options = {}) {
  const frame = await waitForAviatorFrame(page, options);
  const initialMultipliers = await waitForPayoutHistory(frame, options);
  const observerState = await installMutationObserverInFrame(frame);

  return {
    frame,
    initialMultipliers,
    observerState,
    frameDescription: describeFrame(frame),
  };
}

// ---------------------------------------------------------------------------
// Public extraction helpers
// ---------------------------------------------------------------------------

/**
 * Extract the current visible payout history from the Spribe iframe.
 */
export async function extractPayouts(page) {
  try {
    const frame = await waitForAviatorFrame(page, { timeoutMs: FRAME_WAIT_TIMEOUT_MS });
    const multipliers = await waitForPayoutHistory(frame, { timeoutMs: PAYOUT_WAIT_TIMEOUT_MS });
    const timestamp = nowISO();

    return {
      rows: multipliers.map((multiplier, index) => ({
        multiplier,
        timestamp,
        position: index,
      })),
      multipliers,
      currentMultiplier: multipliers[0] ?? null,
      error: null,
      frame_url: safeFrameUrl(frame),
    };
  } catch (err) {
    log.error(`extractPayouts failed: ${err.message}`);
    return {
      rows: [],
      multipliers: [],
      currentMultiplier: null,
      error: err.message,
    };
  }
}

/**
 * Compatibility export used by the runner watchdog. It locates the iframe and
 * installs the in-frame MutationObserver.
 */
export async function setupMutationObserver(page) {
  try {
    const connection = await connectToAviatorFrame(page, { timeoutMs: FRAME_WAIT_TIMEOUT_MS });
    log.info(`MutationObserver installed in Aviator frame: ${connection.frameDescription}`);
    return true;
  } catch (err) {
    log.error(`Failed to set up MutationObserver: ${err.message}`);
    return false;
  }
}

/**
 * One-shot extraction that merges the currently visible frame history into the
 * JSON file. This is not used by the live monitor, but it is kept for existing
 * integrations that may call it manually.
 */
export async function extractAndSave(page) {
  try {
    const result = await extractPayouts(page);
    if (result.error || !result.multipliers.length) {
      const history = await loadPostgresRoundHistory().catch(() => readRoundHistory());
      return {
        added: 0,
        total: history.length,
        error: result.error || 'no_multipliers',
      };
    }

    const store = await getPostgresRoundStore();
    let history = await store.loadRounds();
    const recentSignature = snapshotSignature(history.slice(-result.multipliers.length).map(r => r.multiplier));
    const visibleChronological = [...result.multipliers].reverse();
    const visibleSignature = snapshotSignature(visibleChronological);

    if (recentSignature === visibleSignature) {
      return {
        added: 0,
        total: history.length,
        error: null,
        multipliers: result.multipliers,
      };
    }

    const { history: merged, addedRecords } = appendRounds(history, result.multipliers);
    history = merged;

    if (addedRecords.length > 0) {
      await store.saveRounds(addedRecords, 'legacy_extract');
    }

    return {
      added: addedRecords.length,
      total: history.length,
      error: null,
      multipliers: result.multipliers,
    };
  } catch (err) {
    log.error(`extractAndSave failed: ${err.message}`);
    const history = await loadPostgresRoundHistory().catch(() => readRoundHistory());
    return {
      added: 0,
      total: history.length,
      error: err.message,
    };
  }
}

// ---------------------------------------------------------------------------
// Live monitor
// ---------------------------------------------------------------------------

/**
 * Monitor new Aviator rounds with an in-frame MutationObserver.
 *
 * There is no rapid polling loop here. The process sleeps inside the browser
 * context until the payout DOM mutates, then Playwright receives exactly one
 * event payload containing all current multipliers as numbers.
 */
export async function monitorRounds(page, options = {}) {
  const signal = options.signal || null;

  let history = await loadPostgresRoundHistory();
  let previousSnapshot = null;
  let previousSignature = null;
  let lastSavedSignature = null;
  let roundsSeen = 0;
  let totalAdded = 0;
  let reconnectCount = 0;
  let reconnectDelayMs = RECONNECT_BASE_DELAY_MS;

  log.info('Starting Aviator MutationObserver monitor');
  printInfo('MutationObserver monitor started inside the Spribe iframe');

  while (!signal?.aborted) {
    let connection = null;

    try {
      connection = await connectToAviatorFrame(page, { signal });

      if (reconnectCount > 0) {
        log.info(`Frame reconnected: ${connection.frameDescription}`);
        printSuccess('Frame reconnected');
      }

      reconnectDelayMs = RECONNECT_BASE_DELAY_MS;
      reconnectCount += 1;

      const currentSnapshot = connection.observerState.multipliers.length
        ? connection.observerState.multipliers
        : connection.initialMultipliers;
      const currentSignature = snapshotSignature(currentSnapshot);

      // First connection creates the baseline. Later connections compare the
      // new iframe snapshot to the previous one to catch a round that appeared
      // during reload.
      if (!previousSnapshot) {
        previousSnapshot = currentSnapshot;
        previousSignature = currentSignature;
        writeStatus({
          status: 'monitoring',
          step_details: 'Aviator history baseline captured',
          history_length: history.length,
          last_multipliers: currentSnapshot.slice(0, 10),
          current_url: page.url(),
          frame_url: safeFrameUrl(connection.frame),
        });
      } else if (currentSignature !== previousSignature) {
        const inferred = inferNewMultipliers(previousSnapshot, currentSnapshot);
        const result = await saveNewRoundsFromSnapshot({
          history,
          newMultipliers: inferred,
          currentSnapshot,
          currentSignature,
          previousSignature,
          lastSavedSignature,
          page,
          frame: connection.frame,
        });

        history = result.history;
        lastSavedSignature = result.lastSavedSignature;
        roundsSeen += result.addedRecords.length;
        totalAdded += result.addedRecords.length;
        previousSnapshot = currentSnapshot;
        previousSignature = currentSignature;
      }

      while (!signal?.aborted) {
        const event = await waitForNextPayoutMutation(connection.frame);

        if (event?.timeout) {
          if (connection.frame.isDetached()) {
            throw new Error('Aviator frame detached while waiting for payout mutation');
          }
          continue;
        }

        if (event?.error) {
          throw new Error(event.error);
        }

        if (!event?.multipliers?.length) {
          continue;
        }

        const currentSnapshot = event.multipliers;
        const currentSignature = snapshotSignature(currentSnapshot);

        if (currentSignature === previousSignature) {
          continue;
        }

        const inferred = inferNewMultipliers(previousSnapshot, currentSnapshot);
        const result = await saveNewRoundsFromSnapshot({
          history,
          newMultipliers: inferred,
          currentSnapshot,
          currentSignature,
          previousSignature,
          lastSavedSignature,
          page,
          frame: connection.frame,
        });

        history = result.history;
        lastSavedSignature = result.lastSavedSignature;
        roundsSeen += result.addedRecords.length;
        totalAdded += result.addedRecords.length;
        previousSnapshot = currentSnapshot;
        previousSignature = currentSignature;
      }
    } catch (err) {
      if (signal?.aborted) break;

      const recoverable = isRecoverableFrameError(err);
      const message = recoverable
        ? `Frame changed or reloaded: ${err.message}`
        : `Monitor error: ${err.message}`;

      log.warn(message);
      printError(`${message}. Reconnecting...`);
      writeStatus({
        status: recoverable ? 'reconnecting_frame' : 'monitor_error',
        error: err.message,
        reconnect_count: reconnectCount,
        step_details: 'Reconnecting to Aviator frame',
        current_url: safePageUrl(page),
      });

      await sleep(reconnectDelayMs, signal);
      reconnectDelayMs = Math.min(reconnectDelayMs * 2, RECONNECT_MAX_DELAY_MS);
    }
  }

  reportStep({
    action: 'Monitoring game rounds',
    status: 'success',
    details: `Monitor stopped. New records saved: ${totalAdded}`,
    divider: false,
  });

  log.info(`Monitor stopped. Rounds seen: ${roundsSeen}, records added: ${totalAdded}`);
  return { roundsSeen, totalAdded };
}

async function saveNewRoundsFromSnapshot({
  history,
  newMultipliers,
  currentSnapshot,
  currentSignature,
  lastSavedSignature,
  page,
  frame,
}) {
  if (!newMultipliers.length) {
    return { history, addedRecords: [], lastSavedSignature };
  }

  if (currentSignature === lastSavedSignature) {
    return { history, addedRecords: [], lastSavedSignature };
  }

  const newest = newMultipliers[0];
  log.info(`New round detected: ${formatMultiplier(newest)}`);
  printSuccess(`New round detected: ${formatMultiplier(newest)}`);

  const appended = appendRounds(history, newMultipliers);
  const nextHistory = appended.history;

  if (appended.addedRecords.length > 0) {
    const store = await getPostgresRoundStore();
    await store.saveRounds(appended.addedRecords, 'legacy_monitor');
    log.info(`History updated: ${nextHistory.length} rounds saved`);
    printSuccess(`History updated: ${nextHistory.length} rounds saved`);
  }

  const lastRound = appended.addedRecords[appended.addedRecords.length - 1] || null;

  writeStatus({
    status: 'monitoring',
    rounds_seen: Number(readStatus({}).rounds_seen || 0) + appended.addedRecords.length,
    total_added: Number(readStatus({}).total_added || 0) + appended.addedRecords.length,
    history_length: nextHistory.length,
    last_round: lastRound,
    last_multipliers: currentSnapshot.slice(0, 10),
    current_url: safePageUrl(page),
    frame_url: safeFrameUrl(frame),
    step_details: lastRound ? `Latest multiplier ${formatMultiplier(lastRound.multiplier)}` : 'No new round saved',
  });

  return {
    history: nextHistory,
    addedRecords: appended.addedRecords,
    lastSavedSignature: appended.addedRecords.length > 0 ? currentSignature : lastSavedSignature,
  };
}

function safePageUrl(page) {
  try {
    return page.url();
  } catch {
    return '';
  }
}

function isRecoverableFrameError(err) {
  const message = String(err?.message || '').toLowerCase();
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
  ].some(fragment => message.includes(fragment));
}

// ---------------------------------------------------------------------------
// Startup cleanup
// ---------------------------------------------------------------------------

/**
 * Normalize the existing file without dropping older rounds.
 */
export function cleanHistory() {
  const raw = readJSON(ROUND_HISTORY_PATH, []);
  const before = coerceHistoryArray(raw).length;
  const afterRecords = normalizeHistory(raw);
  const after = afterRecords.length;

  const currentText = JSON.stringify(coerceHistoryArray(raw), null, 2);
  const nextText = JSON.stringify(afterRecords, null, 2);

  if (currentText !== nextText) {
    writeRoundHistory(afterRecords);
  }

  log.info(`History cleaned: ${before} -> ${after} records`);

  return {
    before,
    after,
    removed: Math.max(0, before - after),
  };
}
