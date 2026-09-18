import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';

import { BrowserManager } from '../collector/BrowserManager.js';
import {
  appendRounds, inferNewMultipliers, normalizeMultiplier,
  readRoundHistory, writeRoundHistory,
} from '../collector/HistoryManager.js';

test('validates, orders, persists atomically and preserves normalized identity', () => {
  assert.equal(normalizeMultiplier(' 2.45x '), 2.45);
  for (const bad of [0, -1, NaN, Infinity, '', 'oops']) assert.equal(normalizeMultiplier(bad), null);
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'winner-history-'));
  const file = path.join(dir, 'roundhistory.json');
  fs.writeFileSync(file, JSON.stringify([
    { round_id: '2', round_index: 2, multiplier: 3, timestamp: '2024-01-02T00:00:00Z' },
    { round_id: '1', round_index: 1, multiplier: 1.2, timestamp: '2024-01-01T00:00:00Z' },
    { round_id: '1', round_index: 3, multiplier: 9 },
    { round_id: 'bad', round_index: 4, multiplier: 0 },
  ]));
  const rows = readRoundHistory(file);
  assert.deepEqual(rows.map(r => r.round_id), ['1', '2']);
  writeRoundHistory(rows, file);
  assert.equal(fs.existsSync(file + '.tmp'), false);
  assert.deepEqual(readRoundHistory(file), rows);
});

test('parses snapshots, detects only new rounds and appends chronologically', () => {
  assert.deepEqual(inferNewMultipliers([2, 1.1, 3], [5, 2, 1.1, 3]), [5]);
  assert.deepEqual(inferNewMultipliers([2, 1.1], [2, 1.1]), []);
  const { history, added } = appendRounds([], [3, 2]);
  assert.deepEqual(history.map(r => r.multiplier), [2, 3]);
  assert.deepEqual(added.map(r => r.round_id), ['1', '2']);
});

test('history page reuses one browser context and never launches another browser', async () => {
  let newPages = 0;
  const page = { isClosed: () => false, url: () => 'about:blank' };
  const manager = new BrowserManager(true);
  manager.isAlive = () => true;
  manager.launch = async () => { throw new Error('must not launch'); };
  manager._context = { pages: () => [page], newPage: async () => { newPages += 1; return page; } };
  manager._page = page;
  assert.equal(await manager.getHistoryPage(), page);
  assert.equal(await manager.getHistoryPage(), page);
  assert.equal(newPages, 1);
});
