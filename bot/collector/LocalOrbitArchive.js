/**
 * Local, crash-safe Orbit reading checkpoint. PostgreSQL is a downstream
 * projection; this SQLite journal owns the Watch cursor and accepted rounds.
 */
import fs from 'node:fs';
import path from 'node:path';
import { DatabaseSync } from 'node:sqlite';
import { fileURLToPath } from 'node:url';
import { visiblePayoutItems } from './OrbitReadings.js';
import { OrbitApiError } from './OrbitDataWatchClient.js';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', '..');
const DEFAULT_PATH = path.join(ROOT, 'data', 'bot', 'orbit-live-journal.sqlite3');

const positiveLimit = (value, fallback) => {
  const parsed = Number(value);
  return Number.isSafeInteger(parsed) && parsed > 0 ? parsed : fallback;
};

export class LocalOrbitArchive {
  constructor({ filePath = DEFAULT_PATH, maxBytes, maxReadings } = {}) {
    this.filePath = filePath;
    this.maxBytes = positiveLimit(maxBytes ?? process.env.COLLECTOR_LOCAL_JOURNAL_MAX_BYTES, 256 * 1024 * 1024);
    this.maxReadings = positiveLimit(maxReadings ?? process.env.COLLECTOR_LOCAL_JOURNAL_MAX_READINGS, 10000);
    this.maxRounds = positiveLimit(process.env.COLLECTOR_LOCAL_JOURNAL_MAX_ROUNDS, 5000);
    this.db = null;
    this.lastError = null;
  }

  async initialize() {
    fs.mkdirSync(path.dirname(this.filePath), { recursive: true, mode: 0o700 });
    this.db = new DatabaseSync(this.filePath);
    this.db.exec('PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL; PRAGMA foreign_keys=ON; PRAGMA busy_timeout=5000;');
    this.db.exec(`
      CREATE TABLE IF NOT EXISTS orbit_state (
        singleton INTEGER PRIMARY KEY CHECK(singleton=1), watch_id TEXT NOT NULL,
        cursor TEXT NOT NULL, snapshot TEXT, updated_at TEXT NOT NULL
      );
      CREATE TABLE IF NOT EXISTS orbit_watch_checkpoints (
        watch_id TEXT PRIMARY KEY, cursor TEXT NOT NULL, snapshot TEXT,
        updated_at TEXT NOT NULL
      );
      CREATE TABLE IF NOT EXISTS orbit_readings (
        watch_id TEXT NOT NULL, reading_id TEXT NOT NULL, raw TEXT NOT NULL,
        parsed TEXT, validation_error TEXT, archived_at TEXT NOT NULL,
        PRIMARY KEY(watch_id, reading_id)
      );
      CREATE INDEX IF NOT EXISTS orbit_readings_order_idx ON orbit_readings(watch_id, reading_id);
      CREATE TABLE IF NOT EXISTS orbit_pending_rounds (
        sequence INTEGER PRIMARY KEY AUTOINCREMENT, round_id TEXT NOT NULL UNIQUE,
        watch_id TEXT NOT NULL, round TEXT NOT NULL,
        delivery_mode TEXT NOT NULL CHECK(delivery_mode IN ('LIVE','RECOVERED'))
      );
      CREATE TABLE IF NOT EXISTS orbit_rounds (
        round_id TEXT PRIMARY KEY, round_index INTEGER NOT NULL UNIQUE,
        round TEXT NOT NULL, accepted_at TEXT NOT NULL
      );
      CREATE INDEX IF NOT EXISTS orbit_rounds_index_idx ON orbit_rounds(round_index);
    `);
    this._assertCapacity();
    return this;
  }

  _assertOpen() {
    if (!this.db) throw new Error('LOCAL_ORBIT_JOURNAL_NOT_OPEN');
  }

  _assertCapacity(additionalBytes = 0, additionalReadings = 0) {
    this._assertOpen();
    const dbBytes = fs.existsSync(this.filePath) ? fs.statSync(this.filePath).size : 0;
    const walPath = `${this.filePath}-wal`;
    const walBytes = fs.existsSync(walPath) ? fs.statSync(walPath).size : 0;
    if (dbBytes + walBytes + additionalBytes > this.maxBytes || additionalReadings > this.maxReadings) {
      const error = new Error('Local Orbit journal capacity reached; ingestion is blocked without dropping accepted events');
      error.code = 'LOCAL_JOURNAL_FULL';
      this.lastError = error.code;
      throw error;
    }
  }

  async close() {
    if (!this.db) return;
    this.db.exec('PRAGMA wal_checkpoint(FULL)');
    this.db.close();
    this.db = null;
  }

  async getState() {
    this._assertOpen();
    const row = this.db.prepare('SELECT watch_id,cursor,snapshot FROM orbit_state WHERE singleton=1').get();
    return row ? { watchId: row.watch_id, cursor: row.cursor,
      snapshot: row.snapshot ? JSON.parse(row.snapshot) : null } : null;
  }

  async seedState(state) {
    this._assertOpen();
    if (!state?.watchId || !/^\d{1,19}$/.test(String(state.cursor || '0'))) throw new OrbitApiError('INVALID_LOCAL_CHECKPOINT');
    this.db.prepare(`INSERT INTO orbit_state(singleton,watch_id,cursor,snapshot,updated_at)
      VALUES(1,?,?,?,?) ON CONFLICT(singleton) DO NOTHING`).run(
      String(state.watchId), String(state.cursor || '0'), state.snapshot ? JSON.stringify(state.snapshot) : null,
      new Date().toISOString());
  }

  async loadRounds() {
    this._assertOpen();
    return this.db.prepare('SELECT round FROM orbit_rounds ORDER BY round_index').all()
      .map(row => JSON.parse(row.round));
  }

  async seedRounds(rounds) {
    this._assertOpen();
    const bounded = [...rounds].sort((a, b) => Number(a.round_index) - Number(b.round_index))
      .slice(-this.maxRounds);
    const bytes = bounded.reduce((sum, round) => sum + Buffer.byteLength(JSON.stringify(round)), 0);
    this._assertCapacity(bytes, 0);
    const find = this.db.prepare('SELECT round_index,round FROM orbit_rounds WHERE round_id=?');
    const insert = this.db.prepare(`INSERT INTO orbit_rounds(round_id,round_index,round,accepted_at)
      VALUES(?,?,?,?) ON CONFLICT(round_id) DO NOTHING`);
    this.db.exec('BEGIN IMMEDIATE');
    try {
      for (const round of bounded) {
        const roundId = String(round.round_id);
        const roundIndex = Number(round.round_index);
        const multiplier = Number(round.multiplier);
        const existing = find.get(roundId);
        if (existing) {
          const accepted = JSON.parse(existing.round);
          if (Number(existing.round_index) !== roundIndex
              || Number(accepted.multiplier) !== multiplier) {
            throw new OrbitApiError('ROUND_IDENTITY_CONFLICT');
          }
          // PostgreSQL adds persistence metadata to its copy. The local
          // accepted observation remains authoritative and keeps its original
          // identity when reconciling that projection on restart.
          continue;
        }
        const result = insert.run(roundId, roundIndex, JSON.stringify(round), new Date().toISOString());
        if (result.changes !== 1) throw new OrbitApiError('ROUND_IDENTITY_CONFLICT');
      }
      this._pruneRounds();
      this.db.exec('COMMIT');
      this.lastError = null;
    } catch (error) { this.db.exec('ROLLBACK'); this.lastError = error.code || 'LOCAL_JOURNAL_WRITE_FAILED'; throw error; }
  }

  _pruneRounds() {
    this.db.prepare(`DELETE FROM orbit_rounds WHERE round_id IN (
      SELECT round_id FROM orbit_rounds ORDER BY round_index DESC LIMIT -1 OFFSET ?
    )`).run(this.maxRounds);
  }

  async getLatestVisibleSnapshot(watchId, cursor) {
    const row = this.db.prepare(`SELECT reading_id,parsed FROM orbit_readings
      WHERE watch_id=? AND CAST(reading_id AS INTEGER)<=CAST(? AS INTEGER) AND parsed IS NOT NULL
      ORDER BY CAST(reading_id AS INTEGER) DESC LIMIT 1`).get(String(watchId), String(cursor));
    const parsed = row?.parsed ? JSON.parse(row.parsed) : null;
    if (!parsed || !Array.isArray(parsed.visibleItems)) return null;
    return { readingId: row.reading_id,
      multipliers: visiblePayoutItems(parsed.visibleItems).map(item => item.multiplier) };
  }

  async getLatestVisibleSnapshotBefore(watchId, readingId) {
    const row = this.db.prepare(`SELECT reading_id,parsed FROM orbit_readings
      WHERE watch_id=? AND CAST(reading_id AS INTEGER)<CAST(? AS INTEGER) AND parsed IS NOT NULL
      ORDER BY CAST(reading_id AS INTEGER) DESC LIMIT 1`).get(String(watchId), String(readingId));
    const parsed = row?.parsed ? JSON.parse(row.parsed) : null;
    if (!parsed || !Array.isArray(parsed.visibleItems)) return null;
    return { readingId: row.reading_id,
      multipliers: visiblePayoutItems(parsed.visibleItems).map(item => item.multiplier) };
  }

  async getInvalidReadings(watchId, throughCursor) {
    return this.db.prepare(`SELECT reading_id,raw,validation_error FROM orbit_readings
      WHERE watch_id=? AND CAST(reading_id AS INTEGER)<=CAST(? AS INTEGER) AND validation_error IS NOT NULL
      ORDER BY CAST(reading_id AS INTEGER)`).all(String(watchId), String(throughCursor))
      .map(row => ({ readingId: row.reading_id, reading: JSON.parse(row.raw), validationError: row.validation_error }));
  }

  async getArchivedReadings(watchId, after, throughCursor, limit = 100) {
    return this.db.prepare(`SELECT reading_id,raw,validation_error FROM orbit_readings
      WHERE watch_id=? AND CAST(reading_id AS INTEGER)>CAST(? AS INTEGER)
        AND CAST(reading_id AS INTEGER)<=CAST(? AS INTEGER)
      ORDER BY CAST(reading_id AS INTEGER) LIMIT ?`).all(
      String(watchId), String(after), String(throughCursor), Math.max(1, Math.min(5000, Number(limit) || 100)))
      .map(row => ({ readingId: row.reading_id, reading: JSON.parse(row.raw), validationError: row.validation_error }));
  }

  async updateRepairedReading(watchId, readingId, parsed) {
    this.db.prepare(`UPDATE orbit_readings SET parsed=?,validation_error=NULL
      WHERE watch_id=? AND reading_id=? AND validation_error IS NOT NULL`).run(
      JSON.stringify(parsed), String(watchId), String(readingId));
  }

  async getPendingRounds(limit = 500) {
    return this.db.prepare(`SELECT round,delivery_mode FROM orbit_pending_rounds ORDER BY sequence LIMIT ?`)
      .all(Math.max(1, Math.min(5000, Number(limit) || 500)))
      .map(row => ({ ...JSON.parse(row.round), delivery_mode: row.delivery_mode }));
  }

  async ackPendingRounds(roundIds) {
    if (!roundIds.length) return;
    const statement = this.db.prepare('DELETE FROM orbit_pending_rounds WHERE round_id=?');
    this.db.exec('BEGIN IMMEDIATE');
    try { for (const id of roundIds) statement.run(String(id)); this.db.exec('COMMIT'); }
    catch (error) { this.db.exec('ROLLBACK'); throw error; }
  }

  async commitPage({ watchId, expectedCursor, nextCursor, entries, snapshot = null, pendingRounds = [] }) {
    const serialized = JSON.stringify({ watchId, expectedCursor, nextCursor, entries, snapshot, pendingRounds });
    this._assertCapacity(Buffer.byteLength(serialized), entries.length);
    const insertReading = this.db.prepare(`INSERT INTO orbit_readings
      (watch_id,reading_id,raw,parsed,validation_error,archived_at) VALUES(?,?,?,?,?,?)
      ON CONFLICT(watch_id,reading_id) DO UPDATE SET raw=orbit_readings.raw
      WHERE orbit_readings.raw=excluded.raw`);
    const upsertRound = this.db.prepare(`INSERT INTO orbit_rounds(round_id,round_index,round,accepted_at)
      VALUES(?,?,?,?) ON CONFLICT(round_id) DO UPDATE SET round=orbit_rounds.round
      WHERE orbit_rounds.round=excluded.round`);
    const insertPending = this.db.prepare(`INSERT INTO orbit_pending_rounds(round_id,watch_id,round,delivery_mode)
      VALUES(?,?,?,?) ON CONFLICT(round_id) DO UPDATE SET round=orbit_pending_rounds.round
      WHERE orbit_pending_rounds.watch_id=excluded.watch_id AND orbit_pending_rounds.round=excluded.round
        AND orbit_pending_rounds.delivery_mode=excluded.delivery_mode`);
    this.db.exec('BEGIN IMMEDIATE');
    try {
      const state = this.db.prepare('SELECT watch_id,cursor FROM orbit_state WHERE singleton=1').get();
      if ((state?.watch_id || watchId) !== watchId || (state?.cursor || '0') !== String(expectedCursor)) {
        throw new OrbitApiError('ARCHIVE_CURSOR_CONFLICT');
      }
      const now = new Date().toISOString();
      for (const entry of entries) {
        const result = insertReading.run(String(watchId), String(entry.reading.id), JSON.stringify(entry.reading),
          entry.parsed ? JSON.stringify(entry.parsed) : null, entry.error || null, now);
        if (result.changes !== 1) throw new OrbitApiError('READING_IDENTITY_CONFLICT');
      }
      for (const round of pendingRounds) {
        if (!round.round_id || !['LIVE', 'RECOVERED'].includes(round.delivery_mode || 'RECOVERED')) {
          throw new OrbitApiError('INVALID_PENDING_ROUND');
        }
        const raw = JSON.stringify(round);
        const result = upsertRound.run(String(round.round_id), Number(round.round_index), raw, now);
        if (result.changes !== 1) throw new OrbitApiError('ROUND_IDENTITY_CONFLICT');
        const queued = insertPending.run(String(round.round_id), String(watchId), raw, round.delivery_mode || 'RECOVERED');
        if (queued.changes !== 1) throw new OrbitApiError('PENDING_ROUND_IDENTITY_CONFLICT');
      }
      this._pruneRounds();
      this.db.prepare(`INSERT INTO orbit_state(singleton,watch_id,cursor,snapshot,updated_at)
        VALUES(1,?,?,?,?) ON CONFLICT(singleton) DO UPDATE SET watch_id=excluded.watch_id,
        cursor=excluded.cursor,snapshot=excluded.snapshot,updated_at=excluded.updated_at`).run(
        String(watchId), String(nextCursor), snapshot == null ? null : JSON.stringify(snapshot), now);
      const old = this.db.prepare(`SELECT reading_id FROM orbit_readings WHERE watch_id=?
        ORDER BY CAST(reading_id AS INTEGER) DESC LIMIT -1 OFFSET ?`).all(String(watchId), this.maxReadings);
      const remove = this.db.prepare('DELETE FROM orbit_readings WHERE watch_id=? AND reading_id=?');
      for (const row of old) remove.run(String(watchId), row.reading_id);
      this.db.exec('COMMIT');
      this.lastError = null;
    } catch (error) { this.db.exec('ROLLBACK'); this.lastError = error.code || 'LOCAL_JOURNAL_WRITE_FAILED'; throw error; }
  }

  async commitRepairPage({ watchId, entries, pendingRounds = [] }) {
    const now = new Date().toISOString();
    this.db.exec('BEGIN IMMEDIATE');
    try {
      for (const entry of entries) {
        const result = this.db.prepare(`UPDATE orbit_readings SET parsed=?,validation_error=NULL
          WHERE watch_id=? AND reading_id=? AND validation_error IS NOT NULL`).run(
          JSON.stringify(entry.parsed), String(watchId), String(entry.reading.id));
        if (result.changes !== 1) throw new OrbitApiError('ARCHIVED_REPAIR_CONFLICT');
      }
      for (const round of pendingRounds) {
        const raw = JSON.stringify(round);
        this.db.prepare(`INSERT INTO orbit_rounds(round_id,round_index,round,accepted_at) VALUES(?,?,?,?)
          ON CONFLICT(round_id) DO NOTHING`).run(String(round.round_id), Number(round.round_index), raw, now);
        this.db.prepare(`INSERT INTO orbit_pending_rounds(round_id,watch_id,round,delivery_mode)
          VALUES(?,?,?,?) ON CONFLICT(round_id) DO NOTHING`).run(
          String(round.round_id), String(watchId), raw, round.delivery_mode || 'RECOVERED');
      }
      this.db.exec('COMMIT');
      this.lastError = null;
    } catch (error) { this.db.exec('ROLLBACK'); this.lastError = error.code || 'LOCAL_JOURNAL_WRITE_FAILED'; throw error; }
  }

  async transitionWatch({ previousState, watchId, snapshot }) {
    this.db.exec('BEGIN IMMEDIATE');
    try {
      const current = this.db.prepare('SELECT watch_id,cursor,snapshot FROM orbit_state WHERE singleton=1').get();
      if (!current || current.watch_id !== previousState.watchId || current.cursor !== previousState.cursor) {
        throw new OrbitApiError('ARCHIVE_CURSOR_CONFLICT');
      }
      this.db.prepare(`INSERT INTO orbit_watch_checkpoints(watch_id,cursor,snapshot,updated_at)
        VALUES(?,?,?,?) ON CONFLICT(watch_id) DO UPDATE SET cursor=excluded.cursor,
        snapshot=excluded.snapshot,updated_at=excluded.updated_at`).run(
        current.watch_id, current.cursor, current.snapshot, new Date().toISOString());
      this.db.prepare('DELETE FROM orbit_watch_checkpoints WHERE watch_id=?').run(String(watchId));
      this.db.prepare(`INSERT INTO orbit_state(singleton,watch_id,cursor,snapshot,updated_at) VALUES(1,?,'0',?,?)
        ON CONFLICT(singleton) DO UPDATE SET watch_id=excluded.watch_id,cursor='0',
        snapshot=excluded.snapshot,updated_at=excluded.updated_at`).run(
        String(watchId), JSON.stringify(snapshot), new Date().toISOString());
      this.db.exec('COMMIT');
      this.lastError = null;
    } catch (error) { this.db.exec('ROLLBACK'); this.lastError = error.code || 'LOCAL_JOURNAL_WRITE_FAILED'; throw error; }
  }

  snapshot() {
    this._assertOpen();
    const counts = this.db.prepare(`SELECT
      (SELECT COUNT(*) FROM orbit_readings) AS readings,
      (SELECT COUNT(*) FROM orbit_rounds) AS rounds,
      (SELECT COUNT(*) FROM orbit_pending_rounds) AS pending`).get();
    const dbBytes = fs.existsSync(this.filePath) ? fs.statSync(this.filePath).size : 0;
    const walPath = `${this.filePath}-wal`;
    const bytes = dbBytes + (fs.existsSync(walPath) ? fs.statSync(walPath).size : 0);
    const state = this.db.prepare('SELECT watch_id,cursor,updated_at FROM orbit_state WHERE singleton=1').get();
    return { healthy: !this.lastError, state: this.lastError ? 'DEGRADED' : 'HEALTHY',
      last_error: this.lastError, path: this.filePath, bytes, capacity_bytes: this.maxBytes,
      reading_count: Number(counts.readings), accepted_round_count: Number(counts.rounds),
      pending_delivery_count: Number(counts.pending), checkpoint: state || null };
  }
}
