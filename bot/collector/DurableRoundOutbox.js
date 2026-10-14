/** Durable, bounded queue for observations waiting for PostgreSQL. */
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { randomUUID } from 'node:crypto';
import { fileURLToPath } from 'node:url';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', '..');
const DEFAULT_PATH = path.join(ROOT, 'data', 'bot', 'round-outbox.json');
const LOCK_WAIT_BUFFER = new Int32Array(new SharedArrayBuffer(4));

function processStartIdentity(pid) {
  try {
    const stat = fs.readFileSync(`/proc/${pid}/stat`, 'utf8');
    const close = stat.lastIndexOf(')');
    if (close < 0) return null;
    // The text after the command starts at field 3; starttime is field 22.
    return stat.slice(close + 2).trim().split(/\s+/)[19] || null;
  } catch { return null; }
}

function sameProcessIsAlive(owner) {
  // A shared filesystem can be mounted by another collector host. Its process
  // cannot be checked locally, so keep that lock rather than risk stealing it.
  if (!owner || owner.hostname !== os.hostname()) return true;
  if (!Number.isInteger(Number(owner.pid))) return false;
  try { process.kill(Number(owner.pid), 0); }
  catch (error) { return error?.code === 'EPERM'; }
  const currentStart = processStartIdentity(owner.pid);
  return !owner.process_start || !currentStart || String(owner.process_start) === currentStart;
}

function statSignature(filePath) {
  try {
    const stat = fs.statSync(filePath, { bigint: true });
    return [stat.dev, stat.ino, stat.size, stat.mtimeNs, stat.ctimeNs].join(':');
  } catch (error) {
    if (error?.code === 'ENOENT') return 'missing';
    throw error;
  }
}

function asLimit(value, fallback) {
  const parsed = Number(value);
  return Number.isSafeInteger(parsed) && parsed > 0 ? parsed : fallback;
}

export class DurableRoundOutbox {
  constructor({ filePath = DEFAULT_PATH, maxRecords, maxBytes, eventOutbox = false } = {}) {
    this.filePath = filePath;
    this.eventOutbox = eventOutbox === true;
    this.maxRecords = asLimit(maxRecords ?? process.env.COLLECTOR_OUTBOX_MAX_RECORDS, 50000);
    this.maxBytes = asLimit(maxBytes ?? process.env.COLLECTOR_OUTBOX_MAX_BYTES, 64 * 1024 * 1024);
    this._eventJournalLoaded = false;
    this._eventRecords = null;
    this._eventJournalBytes = 0;
    this._eventLegacyJson = false;
    this._eventJournalNeedsRewrite = false;
    this._eventJournalSignature = null;
    this._eventLockDepth = 0;
    this._lastCompaction = null;
  }

  readAll() {
    if (this.eventOutbox) return this._readEventJournal();
    let raw;
    try {
      raw = fs.readFileSync(this.filePath, 'utf8');
    } catch (error) {
      if (error?.code === 'ENOENT') return [];
      throw error;
    }
    if (!raw.trim()) return [];
    const parsed = JSON.parse(raw);
    if (!Array.isArray(parsed)) throw new Error('collector outbox is not a JSON array');
    return parsed;
  }

  _readEventJournal() {
    this._loadEventJournalFromDisk(false);
    return this._eventRecords;
  }

  _loadEventJournalFromDisk(force = true) {
    const signature = statSignature(this.filePath);
    if (!force && this._eventJournalLoaded && signature === this._eventJournalSignature) {
      return this._eventRecords;
    }
    let raw;
    try { raw = fs.readFileSync(this.filePath, 'utf8'); }
    catch (error) {
      if (error?.code !== 'ENOENT') throw error;
      raw = '';
    }
    const trimmed = raw.trim();
    const records = new Map();
    this._eventLegacyJson = false;
    this._eventJournalNeedsRewrite = false;
    if (trimmed.startsWith('[')) {
      const legacy = JSON.parse(trimmed);
      if (!Array.isArray(legacy)) throw new Error('collector event outbox is not a JSON array');
      for (const record of legacy) records.set(String(record.round_id), record);
      this._eventLegacyJson = true;
    } else {
      const lastNewline = raw.lastIndexOf('\n');
      const completeEnd = lastNewline + 1;
      let cursor = 0;
      while (cursor < completeEnd) {
        const newline = raw.indexOf('\n', cursor);
        const end = newline < 0 ? completeEnd : newline;
        const line = raw.slice(cursor, end);
        cursor = end + 1;
        if (!line.trim()) continue;
        const entry = JSON.parse(line);
        if (entry.op === 'append' && entry.record?.round_id != null) {
          records.set(String(entry.record.round_id), entry.record);
        } else if (entry.op === 'append_batch' && Array.isArray(entry.records)) {
          for (const record of entry.records) records.set(String(record.round_id), record);
        } else if (entry.op === 'update' && Array.isArray(entry.records)) {
          for (const record of entry.records) records.set(String(record.round_id), record);
        } else if (entry.op === 'remove' && Array.isArray(entry.round_ids)) {
          for (const id of entry.round_ids) records.delete(String(id));
        }
      }
      const tail = raw.slice(completeEnd);
      if (tail.trim()) {
        try {
          const entry = JSON.parse(tail);
          if (entry.op === 'append' && entry.record?.round_id != null) {
            records.set(String(entry.record.round_id), entry.record);
          } else if (entry.op === 'append_batch' && Array.isArray(entry.records)) {
            for (const record of entry.records) records.set(String(record.round_id), record);
          } else if (entry.op === 'update' && Array.isArray(entry.records)) {
            for (const record of entry.records) records.set(String(record.round_id), record);
          } else if (entry.op === 'remove' && Array.isArray(entry.round_ids)) {
            for (const id of entry.round_ids) records.delete(String(id));
          }
        } catch {
          // Appends are newline terminated and fsynced. An incomplete final
          // line did not complete its durable append; preserve the valid
          // prefix and atomically repair the tail on the next write.
        }
        this._eventJournalNeedsRewrite = true;
      } else if (raw && !raw.endsWith('\n')) {
        this._eventJournalNeedsRewrite = true;
      }
    }
    this._eventRecords = [...records.values()];
    this._eventJournalBytes = Buffer.byteLength(raw);
    this._eventJournalLoaded = true;
    this._eventJournalSignature = signature;
    return this._eventRecords;
  }

  _withEventJournalLock(work, { refresh = true } = {}) {
    if (!this.eventOutbox) return work();
    if (this._eventLockDepth) {
      this._eventLockDepth += 1;
      try { return work(); } finally { this._eventLockDepth -= 1; }
    }
    const release = this._acquireEventJournalLock();
    this._eventLockDepth = 1;
    try {
      this._loadEventJournalFromDisk(refresh);
      return work();
    } finally {
      this._eventLockDepth = 0;
      release();
    }
  }

  _acquireEventJournalLock() {
    const lockPath = `${this.filePath}.lock`;
    fs.mkdirSync(path.dirname(this.filePath), { recursive: true });
    const timeoutMs = asLimit(process.env.COLLECTOR_OUTBOX_LOCK_TIMEOUT_MS, 30_000);
    const startedAt = Date.now();
    const token = randomUUID();
    while (true) {
      try {
        fs.mkdirSync(lockPath, { mode: 0o700 });
        const ownerPath = path.join(lockPath, 'owner.json');
        const fd = fs.openSync(ownerPath, 'wx', 0o600);
        try {
          fs.writeFileSync(fd, JSON.stringify({
            pid: process.pid, hostname: os.hostname(), process_start: processStartIdentity(process.pid),
            token, created_at: new Date().toISOString(),
          }));
          fs.fsyncSync(fd);
        } finally { fs.closeSync(fd); }
        this._fsyncDirectoryBestEffort(lockPath);
        this._fsyncDirectoryBestEffort(path.dirname(this.filePath));
        return () => {
          try {
            const owner = JSON.parse(fs.readFileSync(ownerPath, 'utf8'));
            if (owner.token === token) {
              fs.unlinkSync(ownerPath);
              fs.rmdirSync(lockPath);
            }
          } catch (error) {
            if (!['ENOENT', 'ENOTDIR'].includes(error?.code)) throw error;
          }
        };
      } catch (error) {
        if (error?.code !== 'EEXIST') {
          try { fs.rmdirSync(lockPath); } catch {}
          throw error;
        }
        this._removeStaleEventJournalLock(lockPath);
        if (Date.now() - startedAt >= timeoutMs) {
          const timeout = new Error(`timed out waiting for collector event outbox lock after ${timeoutMs}ms`);
          timeout.code = 'OUTBOX_LOCK_TIMEOUT';
          throw timeout;
        }
        Atomics.wait(LOCK_WAIT_BUFFER, 0, 0, 10);
      }
    }
  }

  _removeStaleEventJournalLock(lockPath) {
    let ownerPath;
    let owner;
    let lockStat;
    try {
      lockStat = fs.statSync(lockPath);
      ownerPath = path.join(lockPath, 'owner.json');
      owner = JSON.parse(fs.readFileSync(ownerPath, 'utf8'));
    } catch (error) {
      if (error?.code === 'ENOENT' || error instanceof SyntaxError) {
        // A process killed between mkdir and writing owner.json leaves an
        // empty lock. Give the creator time to finish before reclaiming it.
        if (lockStat && Date.now() - lockStat.mtimeMs > 1000) {
          try { fs.rmdirSync(lockPath); } catch {}
        }
      }
      return;
    }
    if (sameProcessIsAlive(owner)) return;
    try {
      const current = JSON.parse(fs.readFileSync(ownerPath, 'utf8'));
      if (current.token !== owner.token) return;
      fs.unlinkSync(ownerPath);
      // rmdir is safe if another contender has already acquired this path:
      // a successful new owner creates owner.json before it can proceed.
      fs.rmdirSync(lockPath);
    } catch {}
  }

  _fsyncDirectory(directory) {
    const fd = fs.openSync(directory, 'r');
    try { fs.fsyncSync(fd); } finally { fs.closeSync(fd); }
  }

  _fsyncDirectoryBestEffort(directory) {
    try { this._fsyncDirectory(directory); }
    catch (error) {
      if (!['EINVAL', 'ENOTSUP', 'EISDIR'].includes(error?.code)) throw error;
    }
  }

  _encodeEventSnapshot(records) {
    const compacted = records.map(record => JSON.stringify({ op: 'append', record })).join('\n');
    return compacted ? `${compacted}\n` : '';
  }

  _writeEventSnapshot(records) {
    const contents = this._encodeEventSnapshot(records);
    const bytes = Buffer.byteLength(contents);
    const journalLimit = this.maxBytes * 2;
    if (bytes > journalLimit) {
      const error = new Error(`collector event outbox compacted snapshot exceeds the ${journalLimit} byte journal safety limit; all retained records were preserved`);
      error.code = 'OUTBOX_JOURNAL_CAPACITY';
      error.capacity = this._eventStatsUnlocked();
      throw error;
    }
    this._atomicWrite(contents);
    this._eventRecords = records;
    this._eventJournalBytes = bytes;
    this._eventLegacyJson = false;
    this._eventJournalNeedsRewrite = false;
    this._eventJournalLoaded = true;
    this._eventJournalSignature = statSignature(this.filePath);
    this._lastCompaction = { at: new Date().toISOString(), bytes_before: null, bytes_after: bytes };
    return contents;
  }

  _eventStatsUnlocked() {
    const records = this._eventRecords || [];
    return {
      backlog_size: records.length,
      backlog_bytes: this._eventJournalBytes,
      capacity_records: this.maxRecords,
      capacity_bytes: this.maxBytes,
      capacity_remaining_records: Math.max(0, this.maxRecords - records.length),
      journal_capacity_bytes: this.maxBytes * 2,
      journal_compaction: this._lastCompaction,
    };
  }

  _appendEventJournal(entry, apply) {
    if (!this._eventLockDepth) throw new Error('event journal write requires its cross-process lock');
    const line = `${JSON.stringify(entry)}\n`;
    const encoded = Buffer.from(line, 'utf8');
    const journalLimit = this.maxBytes * 2;
    const compactThreshold = Math.floor(journalLimit * 0.72);
    if (this._eventLegacyJson || this._eventJournalNeedsRewrite
        || this._eventJournalBytes + encoded.length > compactThreshold) {
      const before = this._eventJournalBytes;
      this._writeEventSnapshot(this._eventRecords);
      if (this._lastCompaction) this._lastCompaction.bytes_before = before;
    }
    if (this._eventJournalBytes + encoded.length > journalLimit) {
      const error = new Error(`collector event outbox journal reached its ${this.maxBytes * 2} byte safety limit; retained records need worker compaction`);
      error.code = 'OUTBOX_JOURNAL_CAPACITY';
      error.capacity = this._eventStatsUnlocked();
      throw error;
    }
    const existed = fs.existsSync(this.filePath);
    const fd = fs.openSync(this.filePath, 'a', 0o600);
    try {
      let offset = 0;
      while (offset < encoded.length) offset += fs.writeSync(fd, encoded, offset, encoded.length - offset);
      fs.fsyncSync(fd);
    }
    finally { fs.closeSync(fd); }
    if (!existed) {
      this._fsyncDirectory(path.dirname(this.filePath));
    }
    this._eventJournalBytes += encoded.length;
    this._eventRecords = apply(this._eventRecords);
    this._eventJournalSignature = statSignature(this.filePath);
  }

  stats() {
    if (this.eventOutbox) {
      this._readEventJournal();
      return this._eventStatsUnlocked();
    }
    let sizeBytes = 0;
    try { sizeBytes = fs.statSync(this.filePath).size; }
    catch (error) { if (error?.code !== 'ENOENT') throw error; }
    return {
      backlog_size: this.readAll().length,
      backlog_bytes: sizeBytes,
      capacity_records: this.maxRecords,
      capacity_bytes: this.maxBytes,
      capacity_remaining_records: Math.max(0, this.maxRecords - this.readAll().length),
    };
  }

  appendMany(records) {
    if (!records.length) return this.stats();
    if (this.eventOutbox) return this._appendEventRecords(records);
    const current = this.readAll();
    const ids = new Set(current.map(row => String(row.round_id)));
    const additions = [];
    for (const record of records) {
      const id = String(record?.round_id ?? '');
      if (!id) throw new Error('outbox record is missing round_id');
      if (ids.has(id)) continue;
      ids.add(id);
      if (this.eventOutbox) {
        const enqueuedAt = new Date().toISOString();
        additions.push({
          ...record,
          schema_version: '1.0',
          event_id: `aviator-round:${id}:observed`,
          legacy_event_id: `aviator-round:${id}`,
          observation_id: String(record.observation_id || id),
          event_type: 'round.observed',
          stage: 'OBSERVED',
          durability: 'PENDING',
          delivery_mode: record.delivery_mode || 'LIVE',
          persistence_status: 'PENDING',
          observed_handoff_at: null,
          commit_acknowledged_at: null,
          committed_sequence: null,
          persistence_attempts: 0,
          persistence_next_attempt_at: null,
          persistence_last_error: null,
          persisted_handoff_at: null,
          outbox_enqueued_at: enqueuedAt,
          local_appended_at: record.local_appended_at || enqueuedAt,
        });
      } else {
        additions.push({ ...record });
      }
    }
    if (!additions.length) return this.stats();
    const next = [...current, ...additions];
    const encoded = `${JSON.stringify(next)}\n`;
    if (next.length > this.maxRecords || Buffer.byteLength(encoded) > this.maxBytes) {
      const error = new Error(`collector outbox capacity reached (${next.length}/${this.maxRecords} records, ${Buffer.byteLength(encoded)}/${this.maxBytes} bytes); no observations were discarded`);
      error.code = 'OUTBOX_CAPACITY';
      error.capacity = this.stats();
      throw error;
    }
    this._atomicWrite(encoded);
    return this.stats();
  }

  _appendEventRecords(records) {
    return this._withEventJournalLock(() => {
    const current = this._eventRecords;
    const ids = new Set(current.map(row => String(row.round_id)));
    const additions = [];
    for (const record of records) {
      const id = String(record?.round_id ?? '');
      if (!id) throw new Error('outbox record is missing round_id');
      if (ids.has(id)) continue;
      ids.add(id);
      const enqueuedAt = new Date().toISOString();
      additions.push({ ...record, schema_version: '1.0',
        event_id: `aviator-round:${id}:observed`, legacy_event_id: `aviator-round:${id}`,
        observation_id: String(record.observation_id || id), event_type: 'round.observed',
        stage: 'OBSERVED', durability: 'PENDING', delivery_mode: record.delivery_mode || 'LIVE',
        persistence_status: 'PENDING', observed_handoff_at: null, commit_acknowledged_at: null,
        committed_sequence: null, persistence_attempts: 0, persistence_next_attempt_at: null,
        persistence_last_error: null, persisted_handoff_at: null,
        outbox_enqueued_at: enqueuedAt, local_appended_at: record.local_appended_at || enqueuedAt });
    }
    if (!additions.length) return this._eventStatsUnlocked();
    const estimatedActiveBytes = Buffer.byteLength(JSON.stringify([...current, ...additions]));
    if (current.length + additions.length > this.maxRecords || estimatedActiveBytes > this.maxBytes) {
      const error = new Error(`collector event outbox capacity reached (${current.length + additions.length}/${this.maxRecords} records, ${estimatedActiveBytes}/${this.maxBytes} active bytes); no observations were discarded`);
      error.code = 'OUTBOX_CAPACITY';
      error.capacity = this._eventStatsUnlocked();
      throw error;
    }
    this._appendEventJournal({ op: 'append_batch', records: additions }, list => [...list, ...additions]);
    return this._eventStatsUnlocked();
    });
  }

  removePersisted(roundIds) {
    const remove = new Set(roundIds.map(String));
    if (!remove.size) return this.stats();
    if (this.eventOutbox) {
      return this._withEventJournalLock(() => {
        const remaining = this._eventRecords.filter(row => !remove.has(String(row.round_id))
          || row.persistence_status !== 'COMMITTED' || !row.persisted_handoff_at);
        if (remaining.length === this._eventRecords.length) return this._eventStatsUnlocked();
        this._writeEventSnapshot(remaining);
        return this._eventStatsUnlocked();
      });
    }
    const remaining = this.readAll().filter(row => !remove.has(String(row.round_id)));
    this._atomicWrite(`${JSON.stringify(remaining)}\n`);
    return this.stats();
  }

  updateRecords(roundIds, updater) {
    const wanted = new Set(roundIds.map(String));
    if (!wanted.size) return this.stats();
    if (this.eventOutbox) return this._withEventJournalLock(() => {
      let changed = false;
      const next = this._eventRecords.map(record => {
        if (!wanted.has(String(record.round_id))) return record;
        const updated = updater({ ...record });
        if (updated && updated !== record) changed = true;
        return updated || record;
      });
      if (changed) {
      const updated = next.filter(record => wanted.has(String(record.round_id)));
      this._appendEventJournal({ op: 'update', records: updated }, rows => {
        const byId = new Map(updated.map(record => [String(record.round_id), record]));
        return rows.map(record => byId.get(String(record.round_id)) || record);
      });
      }
      return this._eventStatsUnlocked();
    });
    let changed = false;
    const next = this.readAll().map(record => {
      if (!wanted.has(String(record.round_id))) return record;
      const updated = updater({ ...record });
      changed = true;
      return updated || record;
    });
    if (changed) this._atomicWrite(`${JSON.stringify(next)}\n`);
    return this.stats();
  }

  compactEventJournal() {
    if (!this.eventOutbox) return this.stats();
    return this._withEventJournalLock(() => {
      const before = this._eventJournalBytes;
      this._writeEventSnapshot(this._eventRecords);
      if (this._lastCompaction) this._lastCompaction.bytes_before = before;
      return this._eventStatsUnlocked();
    });
  }

  compactEventJournalIfNeeded() {
    if (!this.eventOutbox) return this.stats();
    this._loadEventJournalFromDisk(false);
    const fastThreshold = Math.floor(this.maxBytes * 2 * 0.72);
    if (!this._eventLegacyJson && !this._eventJournalNeedsRewrite
        && this._eventJournalBytes < fastThreshold) return this._eventStatsUnlocked();
    return this._withEventJournalLock(() => {
      const journalLimit = this.maxBytes * 2;
      const threshold = Math.floor(journalLimit * 0.72);
      if (!this._eventLegacyJson && !this._eventJournalNeedsRewrite
          && this._eventJournalBytes < threshold) return this._eventStatsUnlocked();
      const before = this._eventJournalBytes;
      const compactedBytes = Buffer.byteLength(this._encodeEventSnapshot(this._eventRecords));
      const savings = before - compactedBytes;
      const minSavings = Math.max(4096, Math.floor(journalLimit * 0.03));
      if (this._eventLegacyJson || this._eventJournalNeedsRewrite
          || this._eventJournalBytes + minSavings >= journalLimit || savings >= minSavings) {
        this._writeEventSnapshot(this._eventRecords);
        if (this._lastCompaction) this._lastCompaction.bytes_before = before;
      }
      return this._eventStatsUnlocked();
    }, { refresh: false });
  }

  markObservedHandoff(roundIds, publishedAt = new Date().toISOString(), receipt = {}) {
    return this.updateRecords(roundIds, record => ({
      ...record, observed_handoff_at: publishedAt,
      collector_to_api_one_way_estimate_ms: receipt.collector_to_api_one_way_estimate_ms ?? null,
      collector_api_roundtrip_ms: receipt.collector_api_roundtrip_ms ?? null,
      observed_attempts: 0, observed_next_attempt_at: null, observed_last_error: null,
    }));
  }

  markPersistenceFailure(roundIds, error, attempts, nextAttemptAt) {
    return this.updateRecords(roundIds, record => ({
      ...record, persistence_attempts: attempts,
      persistence_next_attempt_at: nextAttemptAt,
      persistence_last_error: String(error?.message || error || 'Unknown error').slice(0, 500),
    }));
  }

  markStageFailure(roundIds, stage, error, attempts, nextAttemptAt) {
    const prefix = String(stage || 'delivery');
    return this.updateRecords(roundIds, record => ({
      ...record,
      [`${prefix}_attempts`]: attempts,
      [`${prefix}_next_attempt_at`]: nextAttemptAt,
      [`${prefix}_last_error`]: String(error?.message || error || 'Unknown error').slice(0, 500),
    }));
  }

  markDatabaseCommitted(roundId, canonicalRound, commitAcknowledgedAt) {
    return this.updateRecords([roundId], record => ({
      ...record, persistence_status: 'COMMITTED',
      committed_round: canonicalRound,
      stored_at: canonicalRound?.stored_at || null,
      commit_acknowledged_at: commitAcknowledgedAt || null,
      persistence_next_attempt_at: null,
      persistence_last_error: null,
    }));
  }

  markDatabaseCommittedMany(entries) {
    const byId = new Map(entries.map(entry => [String(entry.round_id), entry]));
    return this.updateRecords([...byId.keys()], record => {
      const entry = byId.get(String(record.round_id));
      return {
        ...record, persistence_status: 'COMMITTED',
        committed_round: entry.round,
        stored_at: entry.round?.stored_at || null,
        commit_acknowledged_at: entry.commitAcknowledgedAt || null,
        observation_to_commit_ms: entry.observationToCommitMs !== null
          && entry.observationToCommitMs !== undefined
          && Number.isFinite(Number(entry.observationToCommitMs))
          ? Number(entry.observationToCommitMs) : null,
        persistence_next_attempt_at: null,
        persistence_last_error: null,
      };
    });
  }

  markRecovered(roundIds = null) {
    if (this.eventOutbox) {
      return this._withEventJournalLock(() => {
        const wanted = roundIds ? new Set(roundIds.map(String)) : null;
        let changed = false;
        const next = this._eventRecords.map(record => {
          if (wanted && !wanted.has(String(record.round_id))) return record;
          if (record.delivery_mode === 'RECOVERED') return record;
          changed = true;
          return { ...record, delivery_mode: 'RECOVERED' };
        });
        // Startup recovery doubles as atomic journal compaction. It rewrites
        // every retained state exactly once and cannot append the large
        // recovery update that used to hit the journal ceiling.
        if (changed || !roundIds || this._eventLegacyJson || this._eventJournalNeedsRewrite) {
          const before = this._eventJournalBytes;
          this._writeEventSnapshot(next);
          if (this._lastCompaction) this._lastCompaction.bytes_before = before;
        }
        return this._eventStatsUnlocked();
      });
    }
    const ids = roundIds || this.readAll().map(record => String(record.round_id));
    return this.updateRecords(ids.map(String), record => ({
      ...record, delivery_mode: 'RECOVERED',
    }));
  }

  markPersistedHandoff(roundIds, committedEvents) {
    const byId = new Map((committedEvents || []).map(event => [String(event.observation_id || event.round_id), event]));
    return this.updateRecords(roundIds, record => {
      const event = byId.get(String(record.observation_id || record.round_id));
      return {
        ...record,
        persisted_handoff_at: new Date().toISOString(),
        committed_sequence: event?.committed_sequence ?? record.committed_sequence ?? null,
      };
    });
  }

  _atomicWrite(contents) {
    fs.mkdirSync(path.dirname(this.filePath), { recursive: true });
    const tmpPath = `${this.filePath}.${process.pid}.${randomUUID()}.tmp`;
    const fd = fs.openSync(tmpPath, 'wx', 0o600);
    try {
      fs.writeFileSync(fd, contents, 'utf8');
      fs.fsyncSync(fd);
    } catch (error) {
      fs.closeSync(fd);
      try { fs.unlinkSync(tmpPath); } catch {}
      throw error;
    }
    fs.closeSync(fd);
    fs.renameSync(tmpPath, this.filePath);
    try {
      this._fsyncDirectory(path.dirname(this.filePath));
    } catch (error) {
      if (!['EINVAL', 'ENOTSUP', 'EISDIR'].includes(error?.code)) throw error;
    }
  }
}
