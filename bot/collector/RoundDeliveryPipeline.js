/** Ordered delivery from a durable local ingress through observed and PG stages. */

function retryDelay(attempt) {
  return Math.min(30_000, 250 * (2 ** Math.min(Math.max(0, attempt - 1), 7)));
}

function waitFor(ms) {
  return new Promise(resolve => setTimeout(resolve, Math.max(10, ms)));
}

export class RoundDeliveryPipeline {
  constructor({ outbox, roundStore, publishObserved, publishPersisted,
    onCommitted = () => {}, onFailure = () => {}, probeRoundHistory = null,
    onTimingUpdate = () => {}, maxBatch = 20 }) {
    this.outbox = outbox;
    this.roundStore = roundStore;
    this.publishObserved = publishObserved;
    this.publishPersisted = publishPersisted;
    this.onCommitted = onCommitted;
    this.onFailure = onFailure;
    this.probeRoundHistory = probeRoundHistory;
    this.onTimingUpdate = onTimingUpdate;
    this.maxBatch = Math.min(100, Math.max(1, Number(maxBatch) || 20));
    this.running = false;
    this.stopping = false;
    this.busy = false;
    this.loopPromise = null;
    this.wakeResolve = null;
    this.lastFailure = null;
    this.lastBackendSuccessAt = null;
    this.observationMonotonic = new Map();
    this.lastRoundTiming = null;
    this.historyApiVisibilitySamples = [];
    this.latencySamples = {
      page_mutation_to_multiplier_extraction_ms: [], page_extraction_ms: [], node_receive_to_validation_ms: [],
      validation_to_durable_append_ms: [], observation_to_durable_append_ms: [],
      stream_event_to_durable_append_ms: [],
      stream_event_to_decision_ready_ms: [],
      validation_to_api_dispatch_ms: [], api_request_to_rules_engine_receive_ms: [],
      rules_engine_evaluation_ms: [], rules_complete_to_other_consumer_enqueue_ms: [],
      collector_ack_wait_for_rules_decision_ms: [],
      api_durable_inbox_append_ms: [], collector_http_roundtrip_to_api_ack_ms: [],
      durable_append_to_api_receipt_ms: [],
      database_write_ms: [], observation_to_postgres_commit_ms: [],
      durable_append_to_postgres_commit_ms: [], database_commit_to_history_api_visible_ms: [],
    };
  }

  enqueue(rounds, { deliveryMode = 'LIVE', deliveryModes = null, timing = null, timingsByRoundId = null } = {}) {
    if (deliveryModes !== null && (!Array.isArray(deliveryModes) || deliveryModes.length !== rounds.length
        || deliveryModes.some(mode => mode !== 'LIVE' && mode !== 'RECOVERED'))) {
      throw new TypeError('deliveryModes must provide one LIVE or RECOVERED value per round');
    }
    const started = performance.now();
    for (const round of rounds) {
      const id = String(round.round_id);
      const roundTiming = (timingsByRoundId && typeof timingsByRoundId === 'object'
        ? timingsByRoundId[id] : null) || (rounds.length === 1 ? timing : null);
      if (!this.observationMonotonic.has(id)) {
        this.observationMonotonic.set(id, { enqueuedMonotonic: started, timing: roundTiming || null });
      }
    }
    const records = rounds.map((round, index) => ({ ...round,
      delivery_mode: deliveryModes ? deliveryModes[index] : deliveryMode }));
    let result;
    try { result = this.outbox.appendMany(records); }
    catch (error) {
      for (const round of rounds) this.observationMonotonic.delete(String(round.round_id));
      throw error;
    }
    const appendCompletedMonotonic = performance.now();
    const appendMs = Math.max(0, appendCompletedMonotonic - started);
    this._sample('observation_to_durable_append_ms', appendMs);
    const durableAppendAt = new Date().toISOString();
    for (const round of rounds) {
      const id = String(round.round_id);
      const observation = this.observationMonotonic.get(id);
      if (observation) observation.durableAppendMonotonic = appendCompletedMonotonic;
      const sourceTiming = observation?.timing;
      if (Number.isFinite(sourceTiming?.streamReceivedMonotonicMs)) {
        this._sample('stream_event_to_durable_append_ms',
          appendCompletedMonotonic - sourceTiming.streamReceivedMonotonicMs);
      }
      if (!sourceTiming) {
        this.lastRoundTiming = {
          round_id: id,
          timestamps: { mutation_observed_at: null, extraction_started_at: null,
            multiplier_extracted_at: null, node_received_at: null, normalized_at: null,
            validated_at: null, durable_append_at: durableAppendAt,
            stream_event_received_at: null,
            database_write_started_at: null, database_commit_acknowledged_at: null,
            observed_api_receipt_at: null, persisted_event_acknowledged_at: null,
            round_history_api_visible_at: null },
          round_history_api_visibility: 'UNKNOWN_SOURCE_TIMING',
        };
        continue;
      }
      this._sample('page_mutation_to_multiplier_extraction_ms', sourceTiming.pageMutationToExtractionMs);
      this._sample('page_extraction_ms', sourceTiming.pageExtractionMs);
      if (Number.isFinite(sourceTiming.validationMonotonicMs)
          && Number.isFinite(sourceTiming.nodeReceivedMonotonicMs)) {
        this._sample('node_receive_to_validation_ms',
          sourceTiming.validationMonotonicMs - sourceTiming.nodeReceivedMonotonicMs);
      }
      if (Number.isFinite(sourceTiming.validationMonotonicMs)) {
        this._sample('validation_to_durable_append_ms', appendCompletedMonotonic - sourceTiming.validationMonotonicMs);
      }
      this.lastRoundTiming = {
        round_id: id,
        timestamps: {
          mutation_observed_at: sourceTiming.mutationObservedAt || null,
          extraction_started_at: sourceTiming.extractionStartedAt || null,
          multiplier_extracted_at: sourceTiming.extractedAt || null,
          node_received_at: sourceTiming.nodeReceivedAt || null,
          normalized_at: sourceTiming.normalizedAt || null,
          validated_at: sourceTiming.validationCompletedAt || null,
          durable_append_at: durableAppendAt,
          stream_event_received_at: sourceTiming.streamReceivedAt || null,
          database_write_started_at: null,
          database_commit_acknowledged_at: null,
          observed_api_receipt_at: null,
          persisted_event_acknowledged_at: null,
          round_history_api_visible_at: null,
        },
        round_history_api_visibility: 'NOT_MEASURED',
      };
    }
    result.append_latency_ms = Math.round(appendMs * 1000) / 1000;
    this.wakeResolve?.();
    return result;
  }

  _sample(stage, value) {
    const list = this.latencySamples[stage];
    if (!list || value === null || value === undefined || value === ''
        || !Number.isFinite(Number(value)) || Number(value) < 0) return;
    list.push(Number(value));
    if (list.length > 1000) list.splice(0, list.length - 1000);
  }

  _latencySummary(values) {
    const sorted = [...values].sort((a, b) => a - b);
    const percentile = fraction => sorted.length
      ? Math.round(sorted[Math.min(sorted.length - 1, Math.max(0, Math.ceil(sorted.length * fraction) - 1))] * 100) / 100
      : null;
    return { samples: sorted.length,
      average_ms: sorted.length ? Math.round(sorted.reduce((sum, value) => sum + value, 0) / sorted.length * 100) / 100 : null,
      p50_ms: percentile(0.5), p95_ms: percentile(0.95), p99_ms: percentile(0.99),
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
    const live = unobserved.find(row => row.delivery_mode === 'LIVE'
      && this._dueAt(row, 'observed') <= now
      && Number.isFinite(Date.parse(row.observed_at || row.timestamp || ''))
      && now - Date.parse(row.observed_at || row.timestamp) <= 15000
      && Date.parse(row.observed_at || row.timestamp) <= now + 2000);
    if (live) {
      // The API performs the Rules Engine priority evaluation before it
      // acknowledges this stage. Keep this latency-sensitive request to one
      // round; a large batch multiplied per-round evaluation time until the
      // collector's HTTP deadline expired and replayed the same prefix.
      return { stage: 'observed', records: [live] };
    }

    // Keep PostgreSQL writes in outbox order, but allow them to continue while
    // an earlier round waits for its committed-event HTTP acknowledgement.
    const pendingPersistence = [];
    for (const row of records) {
      if (row.persistence_status === 'COMMITTED') continue;
      if (!row.observed_handoff_at) break;
      pendingPersistence.push(row);
    }
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

    // Recovered backlog must not indefinitely starve its own acknowledged
    // PostgreSQL writes or committed events when new readings keep arriving.
    if (unobserved.length && this._dueAt(unobserved[0], 'observed') <= now) {
      return { stage: 'observed', records: unobserved.slice(0, 1) };
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
      let completed = false;
      try {
        completed = await this._process(work.stage, work.records);
        if (completed && this.lastFailure?.stage === work.stage) this.lastFailure = null;
      } catch (error) {
        this.lastFailure = { stage: error?.pipelineStage || work.stage, at: new Date().toISOString(),
          category: error?.category || error?.code || 'ROUND_DELIVERY_FAILURE',
          message: String(error?.message || error).slice(0, 500) };
        try { this.onFailure(this.lastFailure); } catch {}
      } finally {
        this.busy = false;
      }
      if (completed) {
        // Publish the post-ack outbox state so the health snapshot cannot keep
        // reporting a drained delivery queue as pending after a successful retry.
        try { this.onTimingUpdate(this.snapshot()); } catch {}
      }
    }
  }

  async _process(stage, records) {
    if (stage === 'observed') {
      try {
        // A queued live round can become historical while waiting for the
        // backend. Never replay it as a live target after its freshness window.
        const staleIds = records.filter(row => row.delivery_mode === 'LIVE'
          && (!Number.isFinite(Date.parse(row.observed_at || row.timestamp || ''))
            || Date.now() - Date.parse(row.observed_at || row.timestamp) > 15000
            || Date.parse(row.observed_at || row.timestamp) > Date.now() + 2000))
          .map(row => row.round_id);
        if (staleIds.length) {
          this.outbox.markRecovered(staleIds);
          const stale = new Set(staleIds.map(String));
          records = records.map(row => stale.has(String(row.round_id))
            ? { ...row, delivery_mode: 'RECOVERED' } : row);
        }
        const dispatchMonotonic = performance.now();
        const collectorTiming = records.map(record => {
          const observation = this.observationMonotonic.get(String(record.round_id));
          const sourceTiming = observation?.timing || null;
          const validationToDispatchMs = sourceTiming && Number.isFinite(sourceTiming.validationMonotonicMs)
            ? Math.max(0, dispatchMonotonic - sourceTiming.validationMonotonicMs) : null;
          this._sample('validation_to_api_dispatch_ms', validationToDispatchMs);
          return {
            round_id: String(record.round_id),
            page_mutation_to_extraction_ms: sourceTiming?.pageMutationToExtractionMs ?? null,
            page_extraction_ms: sourceTiming?.pageExtractionMs ?? null,
            node_receive_to_validation_ms: sourceTiming
              && Number.isFinite(sourceTiming.validationMonotonicMs)
              && Number.isFinite(sourceTiming.nodeReceivedMonotonicMs)
              ? Math.max(0, sourceTiming.validationMonotonicMs - sourceTiming.nodeReceivedMonotonicMs) : null,
            validation_to_api_dispatch_ms: validationToDispatchMs,
            page_to_node_receive_ms: null,
            page_to_node_receive_unavailable_reason: 'SEPARATE_MONOTONIC_CLOCK_DOMAINS',
          };
        });
        const requestStartedMonotonic = performance.now();
        const receipt = await this.publishObserved(records, { collectorTiming });
        this.lastBackendSuccessAt = new Date().toISOString();
        const requestFinishedMonotonic = performance.now();
        this._sample('collector_http_roundtrip_to_api_ack_ms',
          requestFinishedMonotonic - requestStartedMonotonic);
        const apiReceiptAt = receipt.api_decision_ready_at
          || receipt.api_published_at || new Date().toISOString();
        const receiptMonotonic = performance.now();
        this.outbox.markObservedHandoff(records.map(row => row.round_id), apiReceiptAt, receipt);
        for (const record of records) {
          const observation = this.observationMonotonic.get(String(record.round_id));
          if (observation) {
            this._sample('durable_append_to_api_receipt_ms',
              receiptMonotonic - (observation.durableAppendMonotonic ?? observation.enqueuedMonotonic));
            const receivedAtMs = Date.parse(observation.timing?.streamReceivedAt || '');
            const decisionReadyAtMs = Date.parse(apiReceiptAt);
            if (Number.isFinite(receivedAtMs) && Number.isFinite(decisionReadyAtMs)
                && decisionReadyAtMs >= receivedAtMs) {
              this._sample('stream_event_to_decision_ready_ms', decisionReadyAtMs - receivedAtMs);
            }
            if (this.lastRoundTiming?.round_id === String(record.round_id)) {
              this.lastRoundTiming.timestamps.observed_api_receipt_at = apiReceiptAt;
            }
          }
        }
        for (const timing of receipt.priority_timings_ms || []) {
          this._sample('api_request_to_rules_engine_receive_ms', timing.api_request_to_rules_engine_receive_ms);
          this._sample('rules_engine_evaluation_ms', timing.rules_engine_evaluation_ms);
          this._sample('rules_complete_to_other_consumer_enqueue_ms', timing.rules_complete_to_other_consumer_enqueue_ms);
          this._sample('collector_ack_wait_for_rules_decision_ms',
            timing.collector_ack_wait_for_rules_decision_ms);
          if (this.lastRoundTiming?.round_id === String(timing.round_id)) {
            this.lastRoundTiming.timestamps.rules_engine_received_at = timing.rules_engine_received_at || null;
            this.lastRoundTiming.timestamps.rules_engine_evaluation_finished_at = timing.rules_engine_evaluation_finished_at || null;
            this.lastRoundTiming.timestamps.other_consumer_enqueue_at = timing.other_consumer_enqueue_at || null;
            this.lastRoundTiming.rules_engine_priority_delivery = timing.status || 'DELIVERED';
          }
        }
        this._sample('api_durable_inbox_append_ms', receipt.durable_inbox_append_ms);
      } catch (error) {
        this._recordFailure(records, 'observed', error);
        return false;
      }
      return true;
    }

    if (stage === 'persistence') {
      try {
        // A failed PostgreSQL write closes the pool and records a retry
        // deadline in PostgresRoundStore. Reinitialize it here once that
        // deadline has elapsed; checking `pool` alone would otherwise make
        // every persistence retry fail before the store can recover itself.
        if (!this.roundStore.pool && !this.roundStore.usingFileFallback
            && typeof this.roundStore.init === 'function'
            && Date.now() >= (Number(this.roundStore.storageRetryAt) || 0)) {
          await this.roundStore.init({ acquireCollectorLease: true });
        }
        if (!this.roundStore.pool || this.roundStore.usingFileFallback) {
          throw Object.assign(new Error('PostgreSQL pool is not ready for round persistence'), {
            category: 'POSTGRES_NOT_READY',
          });
        }
        const databaseWriteStarted = performance.now();
        const databaseWriteStartedAt = new Date().toISOString();
        const result = await this.roundStore.saveRounds(records, 'collector_async');
        const commitAcknowledgedMonotonic = performance.now();
        const databaseWriteMs = Math.max(0, commitAcknowledgedMonotonic - databaseWriteStarted);
        if (result.storage_status !== 'AVAILABLE' || !result.commitAcknowledgedAt) {
          throw Object.assign(new Error(result.last_error || 'PostgreSQL did not acknowledge round commit'), {
            category: 'POSTGRES_COMMIT_UNCONFIRMED',
          });
        }
        this._sample('database_write_ms', databaseWriteMs);
        const newIds = new Set((result.insertedRounds || []).map(round => String(round.round_id)));
        const insertedById = new Map((result.insertedRounds || [])
          .map(round => [String(round.round_id), round]));
        const duplicateIds = records.map(row => String(row.round_id)).filter(id => !newIds.has(id));
        // INSERT ... RETURNING is already the canonical committed row for new
        // records. Only rows that were duplicates need a follow-up read.
        const duplicateRows = duplicateIds.length
          ? await this.roundStore.loadRoundsByIds(duplicateIds) : [];
        const byId = new Map([...insertedById, ...duplicateRows.map(round => [String(round.round_id), round])]);
        const committedEntries = records.map(record => {
          const round = byId.get(String(record.round_id));
          if (!round || Number(round.multiplier) !== Number(record.multiplier)) {
            throw Object.assign(new Error(`canonical PostgreSQL row is missing or conflicts for ${record.round_id}`), {
              category: 'ROUND_IDENTITY_CONFLICT',
            });
          }
          const observation = this.observationMonotonic.get(String(record.round_id));
          if (observation) {
            this._sample('durable_append_to_postgres_commit_ms', commitAcknowledgedMonotonic
              - (observation.durableAppendMonotonic ?? observation.enqueuedMonotonic));
          }
          if (this.lastRoundTiming?.round_id === String(record.round_id)) {
            this.lastRoundTiming.timestamps.database_write_started_at = databaseWriteStartedAt;
            this.lastRoundTiming.timestamps.database_commit_acknowledged_at = result.commitAcknowledgedAt;
          }
          return { round_id: record.round_id, round,
            commitAcknowledgedAt: result.commitAcknowledgedAt,
            observationToCommitMs: observation
              ? Math.max(0, commitAcknowledgedMonotonic - observation.enqueuedMonotonic) : null,
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
        this._probeHistoryVisibility(records.at(-1)?.round_id, result.commitAcknowledgedAt);
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
        this.lastBackendSuccessAt = new Date().toISOString();
        if (this.lastRoundTiming && records.some(row => String(row.round_id) === this.lastRoundTiming.round_id)) {
          this.lastRoundTiming.timestamps.persisted_event_acknowledged_at = new Date().toISOString();
        }
        // The server has durably accepted this committed stage. One atomic
        // snapshot rewrite removes it; a separate journal append followed by
        // another fsynced rewrite only doubled local acknowledgement latency.
        if (typeof this.outbox.acknowledgePersistedAndRemove === 'function') {
          this.outbox.acknowledgePersistedAndRemove(
            records.map(row => row.round_id), receipt.events || []);
        } else {
          this.outbox.markPersistedHandoff(records.map(row => row.round_id), receipt.events || []);
          this.outbox.removePersisted(records.map(row => row.round_id));
        }
        for (const record of records) this.observationMonotonic.delete(String(record.round_id));
      } catch (error) {
        this._recordFailure(records, 'persisted', error);
        return false;
      }
      return true;
    }
    return false;
  }

  _probeHistoryVisibility(roundId, commitAcknowledgedAt) {
    if (typeof this.probeRoundHistory !== 'function' || !roundId) return;
    Promise.resolve().then(() => this.probeRoundHistory(String(roundId))).then(result => {
      const checkedAt = result?.checked_at || new Date().toISOString();
      const commitMs = Date.parse(commitAcknowledgedAt || '');
      const checkedMs = Date.parse(checkedAt);
      const latencyMs = checkedMs - commitMs;
      const available = result?.available === true;
      if (available && Number.isFinite(latencyMs) && latencyMs >= 0 && latencyMs <= 300_000) {
        this._sample('database_commit_to_history_api_visible_ms', latencyMs);
      }
      const probe = { round_id: String(roundId), commit_acknowledged_at: commitAcknowledgedAt || null,
        visibility: available ? 'CONFIRMED' : (result?.status || 'NOT_CONFIRMED'),
        checked_at: checkedAt,
        latency_ms: available && Number.isFinite(latencyMs) && latencyMs >= 0
          ? Math.round(latencyMs * 100) / 100 : null };
      this.historyApiVisibilitySamples.push(probe);
      if (this.historyApiVisibilitySamples.length > 50) this.historyApiVisibilitySamples.shift();
      if (!this.lastRoundTiming || this.lastRoundTiming.round_id === String(roundId)) {
        if (!this.lastRoundTiming) this.lastRoundTiming = { round_id: String(roundId), timestamps: {} };
        this.lastRoundTiming.timestamps.round_history_api_visible_at = available ? checkedAt : null;
        this.lastRoundTiming.round_history_api_visibility = probe.visibility;
        this.lastRoundTiming.round_history_api_probe_at = checkedAt;
        this.lastRoundTiming.database_commit_to_history_api_visible_ms = probe.latency_ms;
      }
      this.onTimingUpdate(this.snapshot());
    }).catch(error => {
      const checkedAt = new Date().toISOString();
      const visibility = error?.category || 'PROBE_FAILED';
      this.historyApiVisibilitySamples.push({ round_id: String(roundId),
        commit_acknowledged_at: commitAcknowledgedAt || null, visibility, checked_at: checkedAt,
        latency_ms: null, error: String(error?.message || error).slice(0, 200) });
      if (this.historyApiVisibilitySamples.length > 50) this.historyApiVisibilitySamples.shift();
      if (!this.lastRoundTiming || this.lastRoundTiming.round_id === String(roundId)) {
        if (!this.lastRoundTiming) this.lastRoundTiming = { round_id: String(roundId), timestamps: {} };
        this.lastRoundTiming.timestamps.round_history_api_visible_at = null;
        this.lastRoundTiming.round_history_api_visibility = visibility;
        this.lastRoundTiming.round_history_api_probe_error = String(error?.message || error).slice(0, 200);
      }
      this.onTimingUpdate(this.snapshot());
    });
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
      last_backend_success_at: this.lastBackendSuccessAt,
      last_round_timing: this.lastRoundTiming,
      round_history_api_visibility_samples: this.historyApiVisibilitySamples,
      measurement_availability: {
        bet_page_round_available_at: 'UNAVAILABLE_PROVIDER_EVENT_NOT_EXPOSED',
        page_to_node_receive_monotonic: 'UNAVAILABLE_SEPARATE_CLOCK_DOMAINS',
        other_consumer_client_ack: 'UNAVAILABLE_NO_CLIENT_ACK_PROTOCOL',
      },
      latency_ms: Object.fromEntries(Object.entries(this.latencySamples)
        .map(([stage, values]) => [stage, this._latencySummary(values)])),
    };
  }
}
