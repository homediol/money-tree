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

  const roundIndexRaw = record?.round_index ?? record?.round_id ?? record?.id ?? index + 1;
  const roundIndex = Number(roundIndexRaw);
  if (!Number.isFinite(roundIndex) || roundIndex <= 0) return null;

  return {
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
    this.usingFileFallback = false;
  }

  async init() {
    try {
      const { Pool } = await import('pg');

      this.pool = new Pool({
        connectionString: this.connectionString || process.env.DATABASE_URL || process.env.POSTGRES_URL || process.env.WINNER_DATABASE_URL,
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
          round_index BIGINT NOT NULL UNIQUE,
          multiplier NUMERIC(12, 2) NOT NULL CHECK (multiplier >= 1),
          timestamp TIMESTAMPTZ,
          source TEXT NOT NULL DEFAULT 'collector',
          raw JSONB NOT NULL DEFAULT '{}'::jsonb,
          created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
          updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
      `);
      await this.pool.query(`CREATE INDEX IF NOT EXISTS ${this.table}_timestamp_idx ON ${this.tableIdent} (timestamp)`);
      log.info(`PostgreSQL round store ready: table=${this.table}`);
    } catch (err) {
      await this.pool?.end?.().catch(() => {});
      this.pool = null;
      this.usingFileFallback = true;
      log.warn(`PostgreSQL unavailable; using data/roundhistory.json fallback: ${formatError(err)}`);
    }
  }

  backendName() {
    return this.usingFileFallback ? 'data/roundhistory.json' : 'PostgreSQL';
  }

  async close() {
    if (!this.pool) return;
    await this.pool.end();
    this.pool = null;
  }

  async loadRounds() {
    if (this.usingFileFallback) {
      return readRoundHistory();
    }

    const result = await this.pool.query(`
      SELECT round_index, multiplier::float8 AS multiplier, timestamp
      FROM ${this.tableIdent}
      ORDER BY round_index ASC
    `);

    return result.rows.map((row) => ({
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
      let saved = 0;

      for (const record of normalized) {
        const result = await client.query(
          `
            INSERT INTO ${this.tableIdent} (round_index, multiplier, timestamp, source, raw)
            VALUES ($1, $2, $3, $4, $5::jsonb)
            ON CONFLICT (round_index) DO UPDATE SET
              multiplier = EXCLUDED.multiplier,
              timestamp = COALESCE(EXCLUDED.timestamp, ${this.tableIdent}.timestamp),
              source = EXCLUDED.source,
              raw = EXCLUDED.raw,
              updated_at = NOW()
          `,
          [
            record.round_index,
            record.multiplier,
            record.timestamp,
            source,
            JSON.stringify(record),
          ],
        );
        saved += result.rowCount || 0;
      }

      await client.query('COMMIT');
      return { saved };
    } catch (err) {
      await client.query('ROLLBACK').catch(() => {});
      throw err;
    } finally {
      client.release();
    }
  }
}
