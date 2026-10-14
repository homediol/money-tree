/** Ordered, restart-safe observed -> PostgreSQL -> persisted round delivery. */

function retryDelay(attempt) {
  return Math.min(30_000, 250 * (2 ** Math.min(Math.max(0, attempt - 1), 7)));
}

function waitFor(ms) {
  return new Promise(resolve => setTimeout(resolve, Math.max(10, ms)));
}

export class RoundDeliveryPipeline {
  constructor({ outbox, roundStore, publishObserved, publishPersisted,
    onCommitted = () => {}, onFailure = () => {}, maxBatch = 20 }) {
    this.outbox = outbox;
    this.roundStore = roundStore;
    this.publishObserved = publishObserved;
    this.publishPersisted = publishPersisted;
    this.onCommitted = onCommitted;
    this.onFailure = onFailure;
    this.maxBatch = Math.min(100, Math.max(1, Number(maxBatch) || 20));
    this.running = false;
    this.stopping = false;
    this.busy = false;
    this.loopPromise = null;
    this.wakeResolve = null;
    this.lastFailure = null;
    this.observationMonotonic = new Map();
    this.latencySamples = { observation_to_durable_append_ms: [], local_append_to_api_receipt_estimate_ms: [],
      observation_to_postgres_commit_ms: [] };
  }

  enqueue(rounds, { deliveryMode = 'LIVE' } = {}) {
    const started = performance.now();
    for (const round of rounds) {
      const id = String(round.round_id);
      if (!this.observationMonotonic.has(id)) this.observationMonotonic.set(id, started);
    }
    const records = rounds.map(round => ({ ...round, delivery_mode: deliveryMode }));
    let result;
    try { result = this.outbox.appendMany(records); }
    catch (error) {
      for (const round of rounds) this.observationMonotonic.delete(String(round.round_id));
      throw error;
    }
    const appendMs = Math.max(0, performance.now() - started);
    this._sample('observation_to_durable_append_ms', appendMs);
    result.append_latency_ms = Math.round(appendMs * 1000) / 1000;
    this.wakeResolve?.();
    return result;
  }

  _sample(stage, value) {
    const list = this.latencySamples[stage];
    if (!list || !Number.isFinite(Number(value))) return;
    list.push(Number(value));
    if (list.length > 1000) list.splice(0, list.length - 1000);
  }

  _latencySummary(values) {
    const sorted = [...values].sort((a, b) => a - b);
    const percentile = fraction => sorted.length
      ? Math.round(sorted[Math.min(sorted.length - 1, Math.max(0, Math.ceil(sorted.length * fraction) - 1))] * 100) / 100
      : null;
    return { samples: sorted.length, p50_ms: percentile(0.5), p95_ms: percentile(0.95),
      max_ms: sorted.length ? Math.round(sorted.at(-1) * 100) / 100 : null };
  }

  start({ recovered = true } = {}) {
    if (this.loopPromise) return this.loopPromise;
    this.stopping = false;
    this.running = true;
    // Recovery and journal maintenance run inside the retrying worker. A full
    // journal must never make start() throw before the worker can compact it.
    this.loopPromise = new Promise(resolve => setImmediate(resolve))
      .then(() => this._run({ recovered })).finally(() => {
      this.running = false;
      this.loopPromise = null;
    });
    return this.loopPromise;
  }

  async stop() {
    this.stopping = true;
    this.wakeResolve?.();
    await this.loopPromise?.catch(() => {});
  }

  async _wait(ms) {
    if (this.stopping) return;
    await new Promise(resolve => {
      let settled = false;
      const finish = () => {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        if (this.wakeResolve === finish) this.wakeResolve = null;
        resolve();
      };
      const timer = setTimeout(finish, Math.max(10, ms));
      this.wakeResolve = finish;
    });
  }

  _dueAt(record, stage) {
    const raw = record[`${stage}_next_attempt_at`];
    const parsed = raw ? Date.parse(raw) : 0;
    return Number.isFinite(parsed) ? parsed : 0;
  }

  _nextWork(records, now = Date.now()) {
    // A slow committed-stage handoff must never hold later observations behind
    // it. Publish the oldest unacknowledged observed prefix before doing more
    // database or committed-event work.
    const unobserved = records.filter(row => !row.observed_handoff_at);
    if (unobserved.length && this._dueAt(unobserved[0], 'observed') <= now) {
      return { stage: 'observed', records: unobserved.slice(0, this.maxBatch) };
    }

    // Keep PostgreSQL writes in outbox order, but allow them to continue while
    // an earlier round waits for its committed-event HTTP acknowledgement.
    const pendingPersistence = records.filter(row => row.persistence_status !== 'COMMITTED'
      && row.observed_handoff_at);
    if (pendingPersistence.length && this._dueAt(pendingPersistence[0], 'persistence') <= now) {
      return { stage: 'persistence', records: pendingPersistence.slice(0, this.maxBatch) };
    }

    // Committed events also remain ordered. Their retry delay does not block
    // observed delivery or the PostgreSQL worker above.
    const pendingHandoff = records.filter(row => row.persistence_status === 'COMMITTED'
      && !row.persisted_handoff_at);
    if (pendingHandoff.length && this._dueAt(pendingHandoff[0], 'persisted') <= now) {
      return { stage: 'persisted', records: pendingHandoff.slice(0, this.maxBatch) };
    }

    const waits = [];
    if (unobserved.length) waits.push(this._dueAt(unobserved[0], 'observed'));
    if (pendingPersistence.length) waits.push(this._dueAt(pendingPersistence[0], 'persistence'));
    if (pendingHandoff.length) waits.push(this._dueAt(pendingHandoff[0], 'persisted'));
    const nextAt = waits.filter(value => value > now).sort((a, b) => a - b)[0];
    return { stage: null, waitMs: nextAt == null ? 1000 : Math.max(10, nextAt - now) };
  }

  async _run({ recovered = false } = {}) {
    let recoveryPending = recovered;
    let maintenanceAttempts = 0;
    while (!this.stopping) {
      try {
        if (recoveryPending) {
          this.outbox.markRecovered();
          recoveryPending = false;
        }
        this.outbox.compactEventJournalIfNeeded?.();
        maintenanceAttempts = 0;
      } catch (error) {
        maintenanceAttempts += 1;
        this.lastFailure = { stage: 'outbox_maintenance', at: new Date().toISOString(),
          category: error?.code || 'OUTBOX_MAINTENANCE_FAILURE',
          attempts: maintenanceAttempts,
          message: String(error?.message || error).slice(0, 500) };
        try { this.onFailure(this.lastFailure); } catch {}
        await this._wait(retryDelay(maintenanceAttempts));
        continue;
      }
      let records;
      try { records = this.outbox.readAll(); }
      catch (error) {
        this.lastFailure = { stage: 'outbox_read', at: new Date().toISOString(), message: String(error?.message || error) };
        try { this.onFailure(this.lastFailure); } catch {}
        await this._wait(1000);
        continue;
      }
      if (!records.length) {
        await this._wait(1000);
        continue;
      }
      const work = this._nextWork(records);
      if (!work.stage) {
        await this._wait(work.waitMs);
        continue;
      }
      this.busy = true;
      try {
        const complete = await this._process(work.stage, work.records);
        if (complete && this.lastFailure?.stage === work.stage) this.lastFailure = null;
      } catch (error) {
        this.lastFailure = { stage: error?.pipelineStage || work.stage, at: new Date().toISOString(),
          category: error?.category || error?.code || 'ROUND_DELIVERY_FAILURE',
          message: String(error?.message || error).slice(0, 500) };
        try { this.onFailure(this.lastFailure); } catch {}
      } finally {
        this.busy = false;
      }
    }
  }

  async _process(stage, records) {
    if (stage === 'observed') {
      try {
        const receipt = await this.publishObserved(records);
        this.outbox.markObservedHandoff(records.map(row => row.round_id),
          receipt.api_published_at || new Date().toISOString(), receipt);
        if (Number.isFinite(Number(receipt.collector_to_api_one_way_estimate_ms))) {
          this._sample('local_append_to_api_receipt_estimate_ms', Number(receipt.collector_to_api_one_way_estimate_ms));
        }
      } catch (error) {
        this._recordFailure(records, 'observed', error);
        return false;
      }
      return true;
    }

    if (stage === 'persistence') {
      try {
        if (!this.roundStore.pool || this.roundStore.usingFileFallback) {
          throw Object.assign(new Error('PostgreSQL pool is not ready for round persistence'), {
            category: 'POSTGRES_NOT_READY',
          });
        }
        const result = await this.roundStore.saveRounds(records, 'collector_async');
        const commitAcknowledgedMonotonic = performance.now();
        if (result.storage_status !== 'AVAILABLE' || !result.commitAcknowledgedAt) {
          throw Object.assign(new Error(result.last_error || 'PostgreSQL did not acknowledge round commit'), {
            category: 'POSTGRES_COMMIT_UNCONFIRMED',
          });
        }
        const canonicalRows = await this.roundStore.loadRoundsByIds(
          records.map(row => String(row.round_id)));
        const byId = new Map(canonicalRows.map(round => [String(round.round_id), round]));
        const newIds = new Set((result.insertedRounds || []).map(round => String(round.round_id)));
        const committedEntries = records.map(record => {
          const round = byId.get(String(record.round_id));
          if (!round || Number(round.multiplier) !== Number(record.multiplier)) {
            throw Object.assign(new Error(`canonical PostgreSQL row is missing or conflicts for ${record.round_id}`), {
              category: 'ROUND_IDENTITY_CONFLICT',
            });
          }
          return { round_id: record.round_id, round,
            commitAcknowledgedAt: result.commitAcknowledgedAt,
            observationToCommitMs: this.observationMonotonic.has(String(record.round_id))
              ? Math.max(0, commitAcknowledgedMonotonic - this.observationMonotonic.get(String(record.round_id))) : null,
            inserted: newIds.has(String(record.round_id)) };
        });
        for (const entry of committedEntries) {
          if (entry.observationToCommitMs !== null && entry.observationToCommitMs !== undefined
              && Number.isFinite(Number(entry.observationToCommitMs))) {
            this._sample('observation_to_postgres_commit_ms', Number(entry.observationToCommitMs));
          }
        }
        this.outbox.markDatabaseCommittedMany(committedEntries);
        await this.onCommitted(committedEntries, result);
      } catch (error) {
        this._recordFailure(records, 'persistence', error);
        return false;
      }
      return true;
    }

    if (stage === 'persisted') {
      try {
        const events = records.map(record => ({
          ...record,
          ...(record.committed_round || {}),
          observation_id: String(record.observation_id || record.round_id),
          round_id: String(record.round_id),
          delivery_mode: record.delivery_mode || 'RECOVERED',
          commit_acknowledged_at: record.commit_acknowledged_at || null,
        }));
        const receipt = await this.publishPersisted(events);
        this.outbox.markPersistedHandoff(records.map(row => row.round_id), receipt.events || []);
        this.outbox.removePersisted(records.map(row => row.round_id));
        for (const record of records) this.observationMonotonic.delete(String(record.round_id));
      } catch (error) {
        this._recordFailure(records, 'persisted', error);
        return false;
      }
      return true;
    }
    return false;
  }

  _recordFailure(records, stage, error) {
    const attempts = Math.max(...records.map(record => Number(record[`${stage}_attempts`]) || 0)) + 1;
    const nextAttemptAt = new Date(Date.now() + retryDelay(attempts)).toISOString();
    if (stage === 'persistence') {
      this.outbox.markPersistenceFailure(records.map(record => record.round_id), error, attempts, nextAttemptAt);
    } else {
      this.outbox.markStageFailure(records.map(record => record.round_id), stage, error, attempts, nextAttemptAt);
      if (stage === 'observed') this.outbox.markRecovered(records.map(record => record.round_id));
    }
    const failure = { stage, attempts, next_attempt_at: nextAttemptAt,
      category: error?.category || error?.code || 'ROUND_DELIVERY_FAILURE',
      message: String(error?.message || error).slice(0, 500),
      backlog_size: this.outbox.stats().backlog_size };
    this.lastFailure = failure;
    this.onFailure(failure);
  }

  snapshot() {
    const stats = this.outbox.stats();
    const oldest = this.outbox.readAll()[0] || null;
    const observedMs = oldest ? Date.parse(oldest.observed_at || oldest.timestamp || '') : NaN;
    return {
      ...stats,
      oldest_pending_observation_id: oldest?.observation_id || null,
      oldest_pending_observation_age_seconds: Number.isFinite(observedMs)
        ? Math.max(0, Math.round((Date.now() - observedMs) / 1000 * 1000) / 1000) : null,
      persistence_lag_ms: oldest?.persistence_status === 'COMMITTED' ? 0
        : Number.isFinite(observedMs) ? Math.max(0, Date.now() - observedMs) : null,
      worker_running: this.running,
      worker_busy: this.busy,
      last_failure: this.lastFailure,
      latency_ms: Object.fromEntries(Object.entries(this.latencySamples)
        .map(([stage, values]) => [stage, this._latencySummary(values)])),
    };
  }
}
