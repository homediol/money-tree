/**
 * PostgresRoundStore.js — PostgreSQL persistence for Aviator round history.
 */

import { log, formatError } from './Logger.js';
import { readRoundHistory, writeRoundHistory } from './HistoryManager.js';

const DEFAULT_TABLE = process.env.ROUND_HISTORY_TABLE || 'aviator_rounds';

function quoteIdent(name) {
  if (!/^[A-Za-z_][A-Za-z0-9_]*$/.test(name)) {
    throw new Error(`Invalid PostgreSQL identifier: ${name}`);
  }
  return `"${name}"`;
}

function normalizeTimestamp(value) {
  if (!value) return null;
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? null : date.toISOString();
}

function normalizeRound(record, index = 0) {
  const multiplier = Number(record?.multiplier ?? record?.crashPoint ?? record?.value);
  if (!Number.isFinite(multiplier) || multiplier < 1) return null;

  const roundIndexRaw = record?.round_index ?? record?.id ?? index + 1;
  const roundIndex = Number(roundIndexRaw);
  if (!Number.isFinite(roundIndex) || roundIndex <= 0) return null;

  return {
    round_id: record?.round_id != null ? String(record.round_id) : `aviator-${Math.trunc(roundIndex)}`,
    round_index: Math.trunc(roundIndex),
    multiplier: Math.round(multiplier * 100) / 100,
    timestamp: normalizeTimestamp(record?.timestamp ?? record?.time ?? record?.ts),
  };
}

export class PostgresRoundStore {
  constructor({ table = DEFAULT_TABLE, connectionString = null } = {}) {
    this.table = table;
    this.tableIdent = quoteIdent(table);
    this.connectionString = connectionString;
    this.pool = null;
    this.collectorLease = null;
    this.usingFileFallback = false;
    // Collector history is project data: silently changing to a JSON file on
    // a database outage creates two diverging sources of truth. Require a
    // real database unless explicitly running a local isolated test.
    this.requirePostgres = process.env.REQUIRE_POSTGRES !== 'false';
  }

  async init({ acquireCollectorLease = false } = {}) {
    try {
      const connectionString = this.connectionString || process.env.DATABASE_URL || process.env.POSTGRES_URL || process.env.WINNER_DATABASE_URL;
      if (!connectionString && !process.env.PGHOST) {
        throw new Error('PostgreSQL is required for Aviator history; configure DATABASE_URL or PGHOST');
      }
      const { Pool } = await import('pg');

      this.pool = new Pool({
        connectionString,
        host: process.env.PGHOST,
        port: process.env.PGPORT ? Number(process.env.PGPORT) : undefined,
        database: process.env.PGDATABASE,
        user: process.env.PGUSER,
        password: process.env.PGPASSWORD,
        max: 5,
        idleTimeoutMillis: 30_000,
        connectionTimeoutMillis: 5_000,
      });

      await this.pool.query(`
        CREATE TABLE IF NOT EXISTS ${this.tableIdent} (
          id BIGSERIAL PRIMARY KEY,
          round_id TEXT NOT NULL,
          round_index BIGINT NOT NULL UNIQUE,
          multiplier NUMERIC(12, 2) NOT NULL CHECK (multiplier >= 1),
          timestamp TIMESTAMPTZ,
          source TEXT NOT NULL DEFAULT 'collector',
          raw JSONB NOT NULL DEFAULT '{}'::jsonb,
          created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
          updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
      `);
      await this.pool.query(`ALTER TABLE ${this.tableIdent} ADD COLUMN IF NOT EXISTS round_id TEXT`);
      await this.pool.query(`UPDATE ${this.tableIdent} SET round_id = COALESCE(round_id, 'aviator-' || round_index::text) WHERE round_id IS NULL`);
      await this.pool.query(`ALTER TABLE ${this.tableIdent} ALTER COLUMN round_id SET NOT NULL`);
      await this.pool.query(`CREATE UNIQUE INDEX IF NOT EXISTS ${this.table}_round_id_idx ON ${this.tableIdent} (round_id)`);
      await this.pool.query(`CREATE INDEX IF NOT EXISTS ${this.table}_timestamp_idx ON ${this.tableIdent} (timestamp)`);

      // A transaction advisory lock only serializes individual insert batches.
      // Hold a session lock for the lifetime of the history collector so a
      // stale supervisor, backend-owned collector, or manual start cannot run
      // a second browser/collector against the same round stream.
      if (acquireCollectorLease) await this._acquireCollectorLease();
      await this.pool.query(`
        CREATE TABLE IF NOT EXISTS collector_state (
          singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
          collector_state TEXT NOT NULL,
          last_processed_round_id TEXT,
          last_round_timestamp TIMESTAMPTZ,
          contiguous_rounds INTEGER NOT NULL DEFAULT 0,
          total_history INTEGER NOT NULL DEFAULT 0,
          warmup_status TEXT NOT NULL DEFAULT 'WARMING_UP',
          collector_session_id TEXT,
          updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
      `);
      log.info(`PostgreSQL round store ready: table=${this.table}`);
    } catch (err) {
      await this.pool?.end?.().catch(() => {});
      this.pool = null;
      this.usingFileFallback = true;
      // A supervised collector cannot safely fall back to local JSON because
      // doing so would bypass both PostgreSQL deduplication and its singleton
      // collector lease.
      if (this.requirePostgres || acquireCollectorLease) throw err;
      log.warn(`PostgreSQL unavailable; using data/roundhistory.json fallback: ${formatError(err)}`);
    }
  }

  backendName() {
    return this.usingFileFallback ? 'data/roundhistory.json' : 'PostgreSQL';
  }

  async _acquireCollectorLease() {
    this.collectorLease = await this.pool.connect();
    const lease = await this.collectorLease.query(
      'SELECT pg_try_advisory_lock(hashtext($1)) AS acquired',
      [`winner-predict:collector:${this.table}`],
    );
    if (lease.rows[0]?.acquired) return;

    this.collectorLease.release();
    this.collectorLease = null;
    const error = new Error(`Another history collector already owns PostgreSQL lease for ${this.table}`);
    error.code = 'COLLECTOR_ALREADY_ACTIVE';
    throw error;
  }

  async close() {
    if (!this.pool) return;
    if (this.collectorLease) {
      try {
        await this.collectorLease.query(
          'SELECT pg_advisory_unlock(hashtext($1))',
          [`winner-predict:collector:${this.table}`],
        );
      } finally {
        this.collectorLease.release();
        this.collectorLease = null;
      }
    }
    await this.pool.end();
    this.pool = null;
  }

  async loadRounds() {
    if (this.usingFileFallback) {
      return readRoundHistory();
    }

    const result = await this.pool.query(`
      SELECT round_id, round_index, multiplier::float8 AS multiplier, timestamp
      FROM ${this.tableIdent}
      ORDER BY round_index ASC
    `);

    return result.rows.map((row) => ({
      round_id: String(row.round_id),
      round_index: Number(row.round_index),
      multiplier: Number(row.multiplier),
      timestamp: row.timestamp ? new Date(row.timestamp).toISOString() : null,
    }));
  }

  async importRounds(records, source = 'roundhistory_import') {
    return this.saveRounds(records, source);
  }

  async saveRounds(records, source = 'collector') {
    const normalized = records
      .map((record, index) => normalizeRound(record, index))
      .filter(Boolean);

    if (normalized.length === 0) return { saved: 0 };

    if (this.usingFileFallback) {
      const existing = readRoundHistory();
      const byIndex = new Map(existing.map((record) => [Number(record.round_index), record]));

      for (const record of normalized) {
        byIndex.set(record.round_index, {
          ...byIndex.get(record.round_index),
          ...record,
        });
      }

      writeRoundHistory([...byIndex.values()]);
      return { saved: normalized.length };
    }

    const client = await this.pool.connect();
    try {
      await client.query('BEGIN');
      // Multiple collector processes can observe the same next round index.
      // Serialize the short insert batch so each writer sees committed indices.
      await client.query('SELECT pg_advisory_xact_lock(hashtext($1))', [this.table]);
      const maxResult = await client.query(
        `SELECT COALESCE(MAX(round_index), 0) AS max_round_index FROM ${this.tableIdent}`,
      );
      let maxRoundIndex = Number(maxResult.rows[0]?.max_round_index || 0);
      let saved = 0;
      let duplicates = 0;

      for (const record of normalized) {
        const candidate = { ...record };
        let resolved = false;
        for (let attempt = 0; attempt < 20; attempt += 1) {
          const byId = await client.query(
            `SELECT round_id FROM ${this.tableIdent} WHERE round_id = $1 LIMIT 1`,
            [candidate.round_id],
          );
          if (byId.rowCount) {
            duplicates += 1;
            resolved = true;
            break;
          }

          const byIndex = await client.query(
            `SELECT round_id FROM ${this.tableIdent} WHERE round_index = $1 LIMIT 1`,
            [candidate.round_index],
          );
          if (byIndex.rowCount) {
            // A different observation already owns this index. Keep both rows
            // by assigning the incoming observation the next available index.
            candidate.round_index = ++maxRoundIndex;
            continue;
          }

          const result = await client.query(
            `INSERT INTO ${this.tableIdent} (round_id, round_index, multiplier, timestamp, source, raw)
             VALUES ($1, $2, $3, $4, $5, $6::jsonb)
             ON CONFLICT DO NOTHING`,
            [candidate.round_id, candidate.round_index, candidate.multiplier,
              candidate.timestamp, source, JSON.stringify(candidate)],
          );
          if (result.rowCount) {
            saved += result.rowCount;
            maxRoundIndex = Math.max(maxRoundIndex, candidate.round_index);
            resolved = true;
            break;
          }
          // A writer not using this advisory lock may have inserted between
          // the check and insert. Re-read both unique keys on the next pass.
        }
        if (!resolved) {
          throw new Error(`Could not persist round ${candidate.round_id} after resolving unique-index conflicts`);
        }
      }

      await client.query('COMMIT');
      const allRounds = await this.loadRounds();
      const state = this.computeState(allRounds);
      await this.saveState(state);
      // PostgreSQL is authoritative when configured. The JSON file is only a
      // legacy import/export fallback for environments without a DSN.
      return { saved, duplicates, rounds: allRounds };
    } catch (err) {
      await client.query('ROLLBACK').catch(() => {});
      throw err;
    } finally {
      client.release();
    }
  }

  computeState(rows) {
    const ordered = [...rows].sort((a, b) => Number(a.round_index) - Number(b.round_index));
    let contiguous = ordered.length ? 1 : 0;
    for (let i = ordered.length - 1; i > 0; i -= 1) {
      const previous = ordered[i - 1];
      const current = ordered[i];
      if (Number(previous.round_index) + 1 !== Number(current.round_index)) break;
      if (previous.timestamp && current.timestamp) {
        const delta = Date.parse(current.timestamp) - Date.parse(previous.timestamp);
        if (!Number.isFinite(delta) || delta < 0 || delta > 120000) break;
      }
      contiguous += 1;
    }
    const latest = ordered.at(-1) || {};
    return {
      collector_state: 'COLLECTING',
      last_processed_round_id: latest.round_id || null,
      last_round_timestamp: latest.timestamp || null,
      contiguous_rounds: contiguous,
      total_history: ordered.length,
      warmup_status: contiguous >= 100 ? 'READY' : 'WARMING_UP',
      collector_session_id: this.sessionId || null,
    };
  }

  async saveState(state) {
    if (this.usingFileFallback || !this.pool) return;
    await this.pool.query(`
      INSERT INTO collector_state(singleton, collector_state, last_processed_round_id,
        last_round_timestamp, contiguous_rounds, total_history, warmup_status,
        collector_session_id, updated_at)
      VALUES (1,$1,$2,$3,$4,$5,$6,$7,NOW())
      ON CONFLICT (singleton) DO UPDATE SET collector_state=EXCLUDED.collector_state,
        last_processed_round_id=EXCLUDED.last_processed_round_id,
        last_round_timestamp=EXCLUDED.last_round_timestamp,
        contiguous_rounds=EXCLUDED.contiguous_rounds,
        total_history=EXCLUDED.total_history,
        warmup_status=EXCLUDED.warmup_status,
        collector_session_id=EXCLUDED.collector_session_id,
        updated_at=NOW()
    `, [state.collector_state, state.last_processed_round_id, state.last_round_timestamp,
      state.contiguous_rounds, state.total_history, state.warmup_status,
      state.collector_session_id]);
  }

  async loadState() {
    if (this.usingFileFallback || !this.pool) return null;
    const result = await this.pool.query('SELECT * FROM collector_state WHERE singleton = 1');
    return result.rows[0] || null;
  }
}
