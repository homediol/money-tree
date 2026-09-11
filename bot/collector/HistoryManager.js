/**
 * HistoryManager.js — All round data read/write/merge logic.
 * Atomic writes, deduplication, full-history persistence.
 */

import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';
import { log } from './Logger.js';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const ROOT               = path.resolve(__dirname, '..', '..');
const ROUND_HISTORY_PATH = path.join(ROOT, 'data', 'roundhistory.json');

// ── Multiplier helpers ────────────────────────────────────────────────────────

export function normalizeMultiplier(raw) {
  if (typeof raw === 'number') {
    return (Number.isFinite(raw) && raw > 0) ? Math.round(raw * 100) / 100 : null;
  }
  if (typeof raw !== 'string') return null;
  const cleaned = raw.trim().replace(/,/g, '').replace(/[xX×]/g, '').replace(/[^0-9.]/g, '');
  if (!cleaned) return null;
  const v = Number.parseFloat(cleaned);
  return (Number.isFinite(v) && v > 0) ? Math.round(v * 100) / 100 : null;
}

export function snapshotSignature(multipliers) {
  return multipliers.map(v => Number(v).toFixed(2)).join('|');
}

export function fmt(v) {
  return `${Number(v).toFixed(2)}x`;
}

// ── File I/O ──────────────────────────────────────────────────────────────────

function readJSON(filepath, def) {
  try {
    if (!fs.existsSync(filepath)) return def;
    return JSON.parse(fs.readFileSync(filepath, 'utf-8'));
  } catch (err) {
    log.warn(`Cannot read ${filepath}: ${err.message}`);
    return def;
  }
}

function writeJSON(filepath, payload) {
  fs.mkdirSync(path.dirname(filepath), { recursive: true });
  const tmp = `${filepath}.tmp`;
  fs.writeFileSync(tmp, JSON.stringify(payload, null, 2), 'utf-8');
  fs.renameSync(tmp, filepath);
}

// ── Public API ────────────────────────────────────────────────────────────────

export function readRoundHistory() {
  let rows = readJSON(ROUND_HISTORY_PATH, []);
  if (!Array.isArray(rows)) {
    rows = rows.rounds ?? rows.history ?? [];
  }

  const cleaned = [];
  const seen    = new Set();

  for (let i = 0; i < rows.length; i++) {
    const item = rows[i];
    const multiplier = normalizeMultiplier(
      typeof item === 'object' && item !== null
        ? (item.multiplier ?? item.crashPoint ?? item.value)
        : item
    );
    if (multiplier === null || multiplier < 1) continue;

    const roundIdx = typeof item === 'object' && item !== null
      ? Number(item.round_index ?? item.round_id ?? item.id ?? (i + 1))
      : (i + 1);

    const timestamp = typeof item === 'object' && item !== null
      ? (item.timestamp ?? item.time ?? item.ts ?? null)
      : null;

    const key = `${roundIdx}|${timestamp || ''}|${multiplier.toFixed(2)}`;
    if (seen.has(key)) continue;
    seen.add(key);
    cleaned.push({ multiplier, timestamp, round_index: roundIdx });
  }

  return sortHistoryChronological(cleaned);
}

export function writeRoundHistory(records) {
  writeJSON(ROUND_HISTORY_PATH, sortHistoryChronological(records));
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

export function nextRoundIndex(history) {
  return history.reduce((max, r) => {
    const idx = Number(r.round_index ?? 0);
    return Number.isFinite(idx) ? Math.max(max, idx) : max;
  }, 0) + 1;
}

/**
 * Infer which multipliers are new by comparing prev and curr snapshots.
 * Both arrays are newest-first (as returned by the DOM).
 */
export function inferNewMultipliers(prev, curr) {
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
  return [curr[0]];
}

/**
 * Append new multipliers (newest-first array) to history (oldest-first).
 * Returns { history, added }.
 */
export function appendRounds(history, newestFirst) {
  if (!newestFirst.length) return { history, added: [] };

  const added = [];
  let idx = nextRoundIndex(history);

  for (const mult of [...newestFirst].reverse()) {
    const normalized = normalizeMultiplier(mult);
    if (normalized === null || normalized < 1) continue;
    const record = { multiplier: normalized, timestamp: new Date().toISOString(), round_index: idx };
    history.push(record);
    added.push(record);
    idx++;
  }

  return { history: sortHistoryChronological(history), added };
}
