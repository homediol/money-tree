/**
 * PostgresRoundStore.js — PostgreSQL persistence for Aviator round history.
 */

import { log, formatError } from './Logger.js';
import { readRoundHistory, writeRoundHistory } from './HistoryManager.js';
import { DurableRoundOutbox } from './DurableRoundOutbox.js';

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
    history_epoch: Number.isSafeInteger(Number(record?.history_epoch)) && Number(record.history_epoch) > 0
      ? Number(record.history_epoch) : 1,
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
    this.outbox = new DurableRoundOutbox();
    this.collectorSequenceRebaseOffset = null;
    this.collectorSequenceLastLocalIndex = null;
    this.historyEpoch = 1;
    this.storageError = null;
    this.storageRetryAt = 0;
    this.lastCommitAcknowledgedAt = null;
    this.lastCommittedRounds = [];
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
      await this.pool.query(`ALTER TABLE ${this.tableIdent} ADD COLUMN IF NOT EXISTS history_epoch BIGINT NOT NULL DEFAULT 1`);
      await this.pool.query(`CREATE TABLE IF NOT EXISTS history_epochs (
        singleton SMALLINT PRIMARY KEY CHECK(singleton=1), epoch BIGINT NOT NULL, updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW())`);
      await this.pool.query(`INSERT INTO history_epochs(singleton,epoch,updated_at) VALUES(1,1,NOW()) ON CONFLICT(singleton) DO NOTHING`);
      const epochResult = await this.pool.query('SELECT epoch FROM history_epochs WHERE singleton=1');
      this.historyEpoch = Math.max(1, Number(epochResult.rows?.[0]?.epoch || 1));
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
      try { await this.pool?.end?.(); }
      catch (closeError) { log.warn(`PostgreSQL pool close after failed initialization also failed: ${formatError(closeError)}`); }
      this.pool = null;
      this.storageError = err;
      this.storageRetryAt = Date.now() + Number(process.env.COLLECTOR_STORAGE_RETRY_MS || 5000);
      this.usingFileFallback = false;
      if (err?.code === 'COLLECTOR_ALREADY_ACTIVE') throw err;
      if (acquireCollectorLease) {
        log.error(`PostgreSQL unavailable; collector will use its bounded durable outbox and retry: ${formatError(err)}`);
        return;
      }
      if (this.requirePostgres) throw err;
      this.usingFileFallback = true;
      log.warn(`PostgreSQL unavailable; using data/roundhistory.json fallback for explicit non-production mode: ${formatError(err)}`);
    }
  }

  backendName() {
    return this.usingFileFallback ? 'data/roundhistory.json' : 'PostgreSQL';
  }

  async executionRecoveryStatus() {
    if (!this.pool || this.usingFileFallback) {
      return { safe: false, status: 'UNKNOWN', reason: 'authoritative PostgreSQL is unavailable' };
    }
    try {
      const result = await this.pool.query(`
        SELECT execution_id, status FROM bet_executions
        WHERE status NOT IN ('RECONCILED', 'CLOSED', 'FAILED', 'EXPIRED', 'REJECTED')
        ORDER BY created_at LIMIT 1
      `);
      const unresolved = result.rows?.[0];
      return unresolved
        ? { safe: false, status: 'RECONCILIATION_REQUIRED', executionId: String(unresolved.execution_id), executionStatus: String(unresolved.status) }
        : { safe: true, status: 'CLEAR' };
    } catch (error) {
      return { safe: false, status: 'UNKNOWN', reason: `execution state could not be verified: ${error.message}` };
    }
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
    await this.pool.end?.();
    this.pool = null;
  }

  async loadRounds() {
    if (this.usingFileFallback) {
      return readRoundHistory();
    }

    if (!this.pool) return [];
    const result = await this.pool.query(`
      SELECT round_id, platform_round_id, platform_round_index, round_index, local_round_index, history_epoch, multiplier::float8 AS multiplier,
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
      history_epoch: Number(row.history_epoch || 1),
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

  async loadRoundsByIds(roundIds = []) {
    const ids = [...new Set(roundIds.map(value => String(value)).filter(Boolean))];
    if (!ids.length || !this.pool || this.usingFileFallback) return [];
    const result = await this.pool.query(`
      SELECT round_id, platform_round_id, platform_round_index, round_index, local_round_index, history_epoch,
             multiplier::float8 AS multiplier, COALESCE(platform_timestamp, timestamp) AS timestamp,
             platform_timestamp, observed_at, COALESCE(stored_at, created_at) AS stored_at,
             round_identity_type, round_index_source, identity_confidence,
             continuity_verified, gap_before, continuity_proof
      FROM ${this.tableIdent}
      WHERE round_id = ANY($1::text[])
    `, [ids]);
    return result.rows.map(row => ({
      round_id: String(row.round_id),
      platform_round_id: row.platform_round_id == null ? null : String(row.platform_round_id),
      platform_round_index: row.platform_round_index == null ? null : Number(row.platform_round_index),
      local_round_index: Number(row.local_round_index ?? row.round_index),
      history_epoch: Number(row.history_epoch || 1),
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
      record.history_epoch = this.historyEpoch;
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

    if (!this.pool && Date.now() >= this.storageRetryAt) {
      await this.init({ acquireCollectorLease: true });
    }
    if (!this.pool) return this._queueOffline(normalized);

    try {
      const pending = this.outbox.readAll();
      if (pending.length) {
        const replay = await this._savePostgresRounds(pending, 'collector_outbox_replay');
        this.lastCommitAcknowledgedAt = replay.commitAcknowledgedAt || null;
        this.lastCommittedRounds = replay.insertedRounds || [];
        this.outbox.removePersisted(pending.map(row => row.round_id));
      }
      this.storageError = null;
      const result = await this._savePostgresRounds(normalized, source);
      result.storage_status = 'AVAILABLE';
      result.backlog_size = this.outbox.stats().backlog_size;
      return result;
    } catch (error) {
      if (error?.code === 'ROUND_IDENTITY_CONFLICT'
          || /unexpected (?:client|pool) query/i.test(error?.message || '')) throw error;
      this.storageError = error;
      this.storageRetryAt = Date.now() + Number(process.env.COLLECTOR_STORAGE_RETRY_MS || 5000);
      try { await this.close(); }
      catch (closeError) { log.warn(`PostgreSQL disconnect cleanup failed: ${formatError(closeError)}`); }
      return this._queueOffline(normalized, error);
    }
  }

  resetLocalSequenceForHistoryEpoch(epoch) {
    this.historyEpoch = Math.max(1, Number(epoch) || 1);
    this.collectorSequenceRebaseOffset = null;
    this.collectorSequenceLastLocalIndex = null;
  }

  async _queueOffline(normalized, cause = this.storageError) {
    try {
      const capacity = this.outbox.appendMany(normalized);
      log.warn(`Round observations durably queued while PostgreSQL is unavailable (${capacity.backlog_size}/${capacity.capacity_records}, ${capacity.backlog_bytes}/${capacity.capacity_bytes} bytes)`);
      return { saved: 0, queued: normalized.length, storage_status: 'UNAVAILABLE', ...capacity,
        last_error: cause ? formatError(cause) : this.storageError ? formatError(this.storageError) : null };
    } catch (error) {
      this.storageError = error;
      log.error(`Round outbox could not accept new observations: ${formatError(error)}`);
      throw error;
    }
  }

  async retryAndDrain() {
    if (!this.pool && Date.now() < this.storageRetryAt) return this.storageSnapshot();
    if (!this.pool) await this.init({ acquireCollectorLease: true });
    if (!this.pool) return this.storageSnapshot();
    const pending = this.outbox.readAll();
    if (pending.length) {
      try {
        const replay = await this._savePostgresRounds(pending, 'collector_outbox_replay');
        this.lastCommitAcknowledgedAt = replay.commitAcknowledgedAt || null;
        this.lastCommittedRounds = replay.insertedRounds || [];
        this.outbox.removePersisted(pending.map(row => row.round_id));
      } catch (error) {
        this.storageError = error;
        this.storageRetryAt = Date.now() + Number(process.env.COLLECTOR_STORAGE_RETRY_MS || 5000);
        try { await this.close(); }
        catch (closeError) { log.warn(`PostgreSQL disconnect cleanup failed during outbox replay: ${formatError(closeError)}`); }
        log.warn(`Outbox replay deferred; ${this.outbox.stats().backlog_size} round(s) remain durable: ${formatError(error)}`);
      }
    }
    return this.storageSnapshot();
  }

  storageSnapshot() {
    const queue = this.outbox.stats();
    return { ...queue, status: this.pool ? 'AVAILABLE' : 'UNAVAILABLE',
      last_commit_acknowledged_at: this.lastCommitAcknowledgedAt,
      last_committed_rounds: this.lastCommittedRounds,
      last_error: this.storageError ? formatError(this.storageError) : null,
      retry_at: this.storageRetryAt ? new Date(this.storageRetryAt).toISOString() : null };
  }

  async _savePostgresRounds(normalized, source = 'collector') {
    if (!normalized.length) return { saved: 0, totalPersistedRows: null };

    const client = await this.pool.connect();
    try {
      await client.query('BEGIN');
      // Multiple collector processes can observe the same next round index.
      // Serialize the short insert batch so each writer sees committed indices.
      await client.query('SELECT pg_advisory_xact_lock(hashtext($1))', [this.table]);
      const maxResult = await client.query(`
        SELECT COALESCE(MAX(round_index), 0) AS max_round_index,
               COUNT(*)::int AS total_history,
               (SELECT round_id FROM ${this.tableIdent} ORDER BY round_index DESC LIMIT 1) AS last_round_id
        FROM ${this.tableIdent}
      `);
      let maxRoundIndex = Number(maxResult.rows[0]?.max_round_index || 0);
      let totalPersistedRows = Number(maxResult.rows[0]?.total_history || 0);
      const stateResult = await client.query(`SELECT collector_state,last_processed_round_id,
          last_round_timestamp,contiguous_rounds,total_history,warmup_status,collector_session_id
        FROM collector_state WHERE singleton=1`);
      const storedState = stateResult.rows[0] || null;
      let collectorState = storedState ? {
        collector_state: storedState.collector_state,
        last_processed_round_id: storedState.last_processed_round_id,
        last_round_timestamp: storedState.last_round_timestamp,
        contiguous_rounds: Number(storedState.contiguous_rounds || 0),
        total_history: Number(storedState.total_history || 0),
        warmup_status: storedState.warmup_status,
        collector_session_id: this.sessionId || storedState.collector_session_id || null,
      } : null;
      let rebuildCollectorState = !collectorState
        || collectorState.total_history !== totalPersistedRows
        || String(collectorState.last_processed_round_id || '') !== String(maxResult.rows[0]?.last_round_id || '');
      if (rebuildCollectorState) {
        // This recovery path runs only when collector_state is absent or does
        // not match PostgreSQL's actual tail. Normal round writes update the
        // state incrementally below, without reloading the full history.
        const continuityResult = await client.query(`SELECT round_id,round_index,timestamp,
            continuity_verified,gap_before FROM ${this.tableIdent} ORDER BY round_index ASC`);
        collectorState = this.computeState(continuityResult.rows);
        collectorState.collector_session_id = this.sessionId || storedState?.collector_session_id || null;
      }
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

          const isLocalCollectorSequence = ['collector', 'collector_async', 'collector_catchup', 'collector_outbox_replay'].includes(source)
            && candidate.round_index_source === 'LOCAL_SEQUENCE'
            && !candidate.platform_round_id
            && candidate.platform_round_index == null;
          if (isLocalCollectorSequence) {
            // Do not let the sequence-rebase recovery path hide an identity
            // collision at the captured index. A rebase is only acceptable
            // when the observed local index is not already owned by another
            // persisted round; otherwise the incoming identity is ambiguous.
            const capturedIndexOwner = await client.query(
              `SELECT round_id FROM ${this.tableIdent} WHERE round_index = $1 LIMIT 1`,
              [candidate.round_index],
            );
            if (capturedIndexOwner.rowCount) {
              const error = new Error(`ROUND_IDENTITY_CONFLICT: round_index=${candidate.round_index} is already owned by ${capturedIndexOwner.rows[0].round_id}; incoming round_id=${candidate.round_id}`);
              error.code = 'ROUND_IDENTITY_CONFLICT';
              throw error;
            }

            const localIndex = Number(candidate.local_round_index || candidate.round_index);
            const expectedLocalIndex = this.collectorSequenceLastLocalIndex == null
              ? null : this.collectorSequenceLastLocalIndex + 1;
            const needsRebase = this.collectorSequenceRebaseOffset == null
              ? Number(candidate.round_index) !== maxRoundIndex + 1
              : localIndex !== expectedLocalIndex;
            if (needsRebase) {
              this.collectorSequenceRebaseOffset = maxRoundIndex + 1 - localIndex;
              candidate.gap_before = true;
              candidate.continuity_proof = 'LOCAL_SEQUENCE_REBASED_AT_POSTGRES_TAIL';
              log.warn(`Collector local round index ${localIndex} did not follow PostgreSQL tail ${maxRoundIndex}; appended at ${maxRoundIndex + 1} and opened a continuity boundary`);
            }
            if (this.collectorSequenceRebaseOffset != null) {
              candidate.round_index = localIndex + this.collectorSequenceRebaseOffset;
            }
            this.collectorSequenceLastLocalIndex = localIndex;
          }

          const byIndex = await client.query(
            `SELECT round_id FROM ${this.tableIdent} WHERE round_index = $1 LIMIT 1`,
            [candidate.round_index],
          );
          if (byIndex.rowCount) {
            if (source === 'collector_outbox_replay') {
              // Offline local indices may overlap the authoritative PostgreSQL
              // tail. Preserve the captured observation and its original
              // local index, but record a real gap instead of overwriting or
              // claiming continuity that cannot be proven.
              candidate.local_round_index = candidate.local_round_index || candidate.round_index;
              candidate.round_index = maxRoundIndex + 1;
              candidate.continuity_verified = false;
              candidate.gap_before = true;
              candidate.continuity_proof = 'OUTBOX_REPLAY_INDEX_RECONCILED_NO_PLATFORM_ID';
              candidate.identity_confidence = 'UNKNOWN';
              candidate.round_index_source = 'LOCAL_SEQUENCE';
              rebuildCollectorState = true;
              continue;
            }
            const error = new Error(`ROUND_IDENTITY_CONFLICT: round_index=${candidate.round_index} is already owned by ${byIndex.rows[0].round_id}; incoming round_id=${candidate.round_id}`);
            error.code = 'ROUND_IDENTITY_CONFLICT';
            throw error;
          }

          const result = await client.query(
            `INSERT INTO ${this.tableIdent} (round_id, platform_round_id, platform_round_index, round_index, local_round_index, history_epoch, multiplier, timestamp, platform_timestamp, observed_at, stored_at, round_identity_type, round_index_source, identity_confidence, continuity_verified, gap_before, continuity_proof, source, raw)
             VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, NOW(), $11, $12, $13, $14, $15, $16, $17, $18::jsonb)
             ON CONFLICT DO NOTHING
             RETURNING id, round_id, round_index, history_epoch, platform_round_id, platform_round_index,
                       multiplier, platform_timestamp, observed_at, stored_at, source,
                       continuity_verified, gap_before, identity_confidence`,
            [candidate.round_id, candidate.platform_round_id, candidate.platform_round_index,
              candidate.round_index, candidate.local_round_index, this.historyEpoch, candidate.multiplier,
              candidate.timestamp, candidate.platform_timestamp, candidate.observed_at,
              candidate.round_identity_type, candidate.round_index_source, candidate.identity_confidence,
              candidate.continuity_verified, candidate.gap_before, candidate.continuity_proof,
              source, JSON.stringify(candidate)],
          );
          if (result.rowCount) {
            saved += result.rowCount;
            totalPersistedRows += result.rowCount;
            const persisted = result.rows[0];
            if (persisted) {
              insertedRounds.push({
                db_id: Number(persisted.id),
                round_id: String(persisted.round_id),
                round_index: Number(persisted.round_index),
                history_epoch: Number(persisted.history_epoch || this.historyEpoch),
                local_round_index: Number(candidate.local_round_index || persisted.round_index),
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
            if (candidate.round_index <= maxRoundIndex) {
              rebuildCollectorState = true;
            } else {
              const previousIndex = maxRoundIndex;
              if (!candidate.continuity_verified) {
                collectorState.contiguous_rounds = 0;
              } else if (candidate.gap_before) {
                collectorState.contiguous_rounds = 1;
              } else if (candidate.round_index === previousIndex + 1
                  && Number(collectorState.contiguous_rounds || 0) > 0) {
                collectorState.contiguous_rounds = Number(collectorState.contiguous_rounds) + 1;
              } else {
                collectorState.contiguous_rounds = 1;
              }
              collectorState.last_processed_round_id = persisted?.round_id || candidate.round_id;
              collectorState.last_round_timestamp = persisted?.timestamp || candidate.timestamp || null;
              collectorState.total_history = totalPersistedRows;
              collectorState.collector_state = 'COLLECTING';
              collectorState.warmup_status = collectorState.contiguous_rounds >= 100 ? 'READY' : 'WARMING_UP';
              collectorState.collector_session_id = this.sessionId || collectorState.collector_session_id || null;
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

      if (saved > 0) {
        if (rebuildCollectorState) {
          const continuityResult = await client.query(`SELECT round_id,round_index,timestamp,
              continuity_verified,gap_before FROM ${this.tableIdent} ORDER BY round_index ASC`);
          collectorState = this.computeState(continuityResult.rows);
          collectorState.collector_session_id = this.sessionId || storedState?.collector_session_id || null;
        }
        collectorState.total_history = totalPersistedRows;
        collectorState.warmup_status = collectorState.contiguous_rounds >= 100 ? 'READY' : 'WARMING_UP';
        await client.query(`INSERT INTO collector_state(singleton,collector_state,last_processed_round_id,
            last_round_timestamp,contiguous_rounds,total_history,warmup_status,collector_session_id,updated_at)
          VALUES(1,$1,$2,$3,$4,$5,$6,$7,NOW())
          ON CONFLICT(singleton) DO UPDATE SET collector_state=EXCLUDED.collector_state,
            last_processed_round_id=EXCLUDED.last_processed_round_id,
            last_round_timestamp=EXCLUDED.last_round_timestamp,
            contiguous_rounds=EXCLUDED.contiguous_rounds,total_history=EXCLUDED.total_history,
            warmup_status=EXCLUDED.warmup_status,collector_session_id=EXCLUDED.collector_session_id,
            updated_at=NOW()`, [collectorState.collector_state || 'COLLECTING',
          collectorState.last_processed_round_id, collectorState.last_round_timestamp,
          Number(collectorState.contiguous_rounds || 0), totalPersistedRows,
          collectorState.warmup_status, this.sessionId || collectorState.collector_session_id || null]);
      }

      await client.query('COMMIT');
      const commitAcknowledgedAt = new Date().toISOString();
      this.lastCommitAcknowledgedAt = commitAcknowledgedAt;
      this.lastCommittedRounds = insertedRounds;
      // The caller already owns the complete in-memory history and appended
      // these rows to it before persistence. Returning that full history from
      // PostgreSQL here used to deserialize every historical round after each
      // insert, delaying the live observer by several seconds.
      return {
        saved,
        duplicates,
        insertedRounds,
        totalPersistedRows,
        commitAcknowledgedAt,
      };
    } catch (err) {
      try { await client.query('ROLLBACK'); }
      catch (rollbackError) { log.warn(`PostgreSQL rollback failed after round persistence error: ${formatError(rollbackError)}`); }
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
