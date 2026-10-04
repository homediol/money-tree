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

  const roundIndexRaw = record?.round_index ?? record?.id;
  const roundIndex = Number(roundIndexRaw);
  if (!Number.isFinite(roundIndex) || roundIndex <= 0) return null;

  const platformRoundId = record?.platform_round_id == null ? null : String(record.platform_round_id);
  const platformRoundIndex = Number(record?.platform_round_index);
  return {
    round_id: record?.round_id != null ? String(record.round_id) : `aviator-${Math.trunc(roundIndex)}`,
    platform_round_id: platformRoundId,
    platform_round_index: Number.isSafeInteger(platformRoundIndex) && platformRoundIndex > 0
      ? Math.trunc(platformRoundIndex) : null,
    local_round_index: Math.trunc(roundIndex),
    round_index: Math.trunc(roundIndex),
    multiplier: Math.round(multiplier * 100) / 100,
    timestamp: normalizeTimestamp(record?.timestamp ?? record?.time ?? record?.ts),
    platform_timestamp: normalizeTimestamp(record?.platform_timestamp),
    observed_at: normalizeTimestamp(record?.observed_at ?? record?.timestamp ?? record?.observedAt),
    round_identity_type: platformRoundId ? 'PLATFORM' : 'COLLECTOR_OBSERVATION_HASH',
    round_index_source: Number.isSafeInteger(platformRoundIndex) && platformRoundIndex > 0 ? 'PLATFORM' : 'LOCAL_SEQUENCE',
    identity_confidence: platformRoundId ? 'PLATFORM_ID' :
      Number.isSafeInteger(platformRoundIndex) && platformRoundIndex > 0 ? 'PLATFORM_ORDER_ONLY' :
        record?.continuity_verified ? 'OVERLAP_VERIFIED_ORDER_ONLY' : 'UNKNOWN',
    continuity_verified: Boolean(record?.continuity_verified),
    gap_before: Boolean(record?.gap_before),
    continuity_proof: record?.continuity_proof || 'UNVERIFIED',
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
      await this.pool.query(`ALTER TABLE ${this.tableIdent} ADD COLUMN IF NOT EXISTS platform_timestamp TIMESTAMPTZ`);
      await this.pool.query(`ALTER TABLE ${this.tableIdent} ADD COLUMN IF NOT EXISTS platform_round_id TEXT`);
      await this.pool.query(`ALTER TABLE ${this.tableIdent} ADD COLUMN IF NOT EXISTS platform_round_index BIGINT`);
      await this.pool.query(`ALTER TABLE ${this.tableIdent} ADD COLUMN IF NOT EXISTS observed_at TIMESTAMPTZ`);
      await this.pool.query(`ALTER TABLE ${this.tableIdent} ADD COLUMN IF NOT EXISTS stored_at TIMESTAMPTZ`);
      await this.pool.query(`ALTER TABLE ${this.tableIdent} ADD COLUMN IF NOT EXISTS local_round_index BIGINT`);
      await this.pool.query(`ALTER TABLE ${this.tableIdent} ADD COLUMN IF NOT EXISTS round_identity_type TEXT`);
      await this.pool.query(`ALTER TABLE ${this.tableIdent} ADD COLUMN IF NOT EXISTS round_index_source TEXT`);
      await this.pool.query(`ALTER TABLE ${this.tableIdent} ADD COLUMN IF NOT EXISTS identity_confidence TEXT`);
      await this.pool.query(`ALTER TABLE ${this.tableIdent} ADD COLUMN IF NOT EXISTS continuity_verified BOOLEAN NOT NULL DEFAULT FALSE`);
      await this.pool.query(`ALTER TABLE ${this.tableIdent} ADD COLUMN IF NOT EXISTS gap_before BOOLEAN NOT NULL DEFAULT FALSE`);
      await this.pool.query(`ALTER TABLE ${this.tableIdent} ADD COLUMN IF NOT EXISTS continuity_proof TEXT`);
      await this.pool.query(`UPDATE ${this.tableIdent} SET stored_at = COALESCE(stored_at, created_at)`);
      await this.pool.query(`UPDATE ${this.tableIdent} SET local_round_index = COALESCE(local_round_index, round_index)`);
      await this.pool.query(`UPDATE ${this.tableIdent} SET observed_at = COALESCE(observed_at, timestamp) WHERE round_id ~ '^aviator-[0-9a-f]{24}$'`);
      await this.pool.query(`UPDATE ${this.tableIdent} SET round_identity_type = COALESCE(round_identity_type, 'COLLECTOR_OBSERVATION_HASH'), round_index_source = COALESCE(round_index_source, 'LOCAL_SEQUENCE'), identity_confidence = COALESCE(identity_confidence, CASE WHEN continuity_verified THEN 'OVERLAP_VERIFIED_ORDER_ONLY' ELSE 'UNKNOWN' END), continuity_proof = COALESCE(continuity_proof, 'LEGACY_UNVERIFIED')`);
      await this.pool.query(`UPDATE ${this.tableIdent} SET platform_timestamp = NULL WHERE round_id ~ '^aviator-[0-9a-f]{24}$'`);
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
      SELECT round_id, platform_round_id, platform_round_index, round_index, local_round_index, multiplier::float8 AS multiplier,
             COALESCE(platform_timestamp, timestamp) AS timestamp,
             platform_timestamp, observed_at, COALESCE(stored_at, created_at) AS stored_at,
             round_identity_type, round_index_source, identity_confidence, continuity_verified, gap_before, continuity_proof
      FROM ${this.tableIdent}
      ORDER BY round_index ASC
    `);

    return result.rows.map((row) => ({
      round_id: String(row.round_id),
      platform_round_id: row.platform_round_id == null ? null : String(row.platform_round_id),
      platform_round_index: row.platform_round_index == null ? null : Number(row.platform_round_index),
      local_round_index: Number(row.local_round_index ?? row.round_index),
      round_index: Number(row.round_index),
      multiplier: Number(row.multiplier),
      timestamp: row.timestamp ? new Date(row.timestamp).toISOString() : null,
      platform_timestamp: row.platform_timestamp ? new Date(row.platform_timestamp).toISOString() : null,
      observed_at: row.observed_at ? new Date(row.observed_at).toISOString() : null,
      stored_at: row.stored_at ? new Date(row.stored_at).toISOString() : null,
      round_identity_type: row.round_identity_type,
      round_index_source: row.round_index_source,
      identity_confidence: row.identity_confidence || 'UNKNOWN',
      continuity_verified: Boolean(row.continuity_verified),
      gap_before: Boolean(row.gap_before),
      continuity_proof: row.continuity_proof,
    }));
  }

  async importRounds(records, source = 'roundhistory_import') {
    return this.saveRounds(records, source);
  }

  async saveRounds(records, source = 'collector') {
    const normalized = records
      .map((record, index) => normalizeRound(record, index))
      .filter(Boolean);
    for (const record of normalized) {
      if (!record.observed_at && source.startsWith('collector')) record.observed_at = new Date().toISOString();
    }

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
      const insertedRounds = [];

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
            const error = new Error(`ROUND_IDENTITY_CONFLICT: round_index=${candidate.round_index} is already owned by ${byIndex.rows[0].round_id}; incoming round_id=${candidate.round_id}`);
            error.code = 'ROUND_IDENTITY_CONFLICT';
            throw error;
          }

          const result = await client.query(
            `INSERT INTO ${this.tableIdent} (round_id, platform_round_id, platform_round_index, round_index, local_round_index, multiplier, timestamp, platform_timestamp, observed_at, stored_at, round_identity_type, round_index_source, identity_confidence, continuity_verified, gap_before, continuity_proof, source, raw)
             VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, NOW(), $10, $11, $12, $13, $14, $15, $16, $17::jsonb)
             ON CONFLICT DO NOTHING
             RETURNING id, round_id, round_index, platform_round_id, platform_round_index,
                       multiplier, platform_timestamp, observed_at, stored_at, source,
                       continuity_verified, gap_before, identity_confidence`,
            [candidate.round_id, candidate.platform_round_id, candidate.platform_round_index,
              candidate.round_index, candidate.local_round_index, candidate.multiplier,
              candidate.timestamp, candidate.platform_timestamp, candidate.observed_at,
              candidate.round_identity_type, candidate.round_index_source, candidate.identity_confidence,
              candidate.continuity_verified, candidate.gap_before, candidate.continuity_proof,
              source, JSON.stringify(candidate)],
          );
          if (result.rowCount) {
            saved += result.rowCount;
            const persisted = result.rows[0];
            if (persisted) {
              insertedRounds.push({
                db_id: Number(persisted.id),
                round_id: String(persisted.round_id),
                round_index: Number(persisted.round_index),
                local_round_index: Number(persisted.round_index),
                platform_round_id: persisted.platform_round_id == null ? null : String(persisted.platform_round_id),
                platform_round_index: persisted.platform_round_index == null ? null : Number(persisted.platform_round_index),
                multiplier: Number(persisted.multiplier),
                platform_timestamp: persisted.platform_timestamp ? new Date(persisted.platform_timestamp).toISOString() : null,
                observed_at: persisted.observed_at ? new Date(persisted.observed_at).toISOString() : null,
                stored_at: persisted.stored_at ? new Date(persisted.stored_at).toISOString() : null,
                source: persisted.source,
                continuity_verified: Boolean(persisted.continuity_verified),
                gap_before: Boolean(persisted.gap_before),
                identity_confidence: persisted.identity_confidence || 'UNKNOWN',
              });
            }
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
      return {
        saved,
        duplicates,
        rounds: allRounds,
        insertedRounds,
        totalPersistedRows: allRounds.length,
      };
    } catch (err) {
      await client.query('ROLLBACK').catch(() => {});
      throw err;
    } finally {
      client.release();
    }
  }

  computeState(rows) {
    const ordered = [...rows].sort((a, b) => Number(a.round_index) - Number(b.round_index));
    let contiguous = ordered.length && ordered.at(-1).continuity_verified ? 1 : 0;
    for (let i = ordered.length - 1; i > 0; i -= 1) {
      const previous = ordered[i - 1];
      const current = ordered[i];
      if (Number(previous.round_index) + 1 !== Number(current.round_index)) break;
      if (!current.continuity_verified || !previous.continuity_verified || current.gap_before) break;
      if (String(previous.round_id) === String(current.round_id)) break;
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
