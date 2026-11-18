import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { OrbitDataWatchClient, OrbitApiError } from './OrbitDataWatchClient.js';
import { orbitRoundIdentity, resolveVisiblePayoutItems, snapshotDelta } from './OrbitReadings.js';
import { LIVE_OVERLAP_WINDOW_SIZE, OrbitWatchArchive, OrbitWatchSync, streamCursorCoversReading } from './OrbitWatchSync.js';
import { PostgresRoundStore } from './PostgresRoundStore.js';
import { LocalOrbitArchive } from './LocalOrbitArchive.js';
import { DurableRoundOutbox } from './DurableRoundOutbox.js';
import { RoundDeliveryPipeline } from './RoundDeliveryPipeline.js';
import { publishDirectRoundObservations, publishCommittedRoundEvents } from './CollectorRoundEventClient.js';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', '..');
const STATUS_PATH = path.join(ROOT, 'data', 'bot', 'status.json');
const HISTORY_OUTBOX_PATH = path.join(ROOT, 'data', 'bot', 'orbit-round-event-outbox.json');

/** LIVE describes only the newest fresh Orbit round; target checks remain backend-owned. */
export function classifyOrbitDeliveries({ caughtUp, watchHealthy, hasMore, pageIndex, noBacklog, entries, latestCursor,
  candidates, now, freshnessMs }) {
  const modes = candidates.map(() => 'RECOVERED');
  if (!candidates.length || !caughtUp || !watchHealthy || hasMore || pageIndex !== 0) return modes;

  const latestEntry = entries.at(-1);
  if (!latestEntry || String(latestEntry.reading?.id) !== String(latestCursor)) return modes;
  const checkedAt = Date.parse(latestEntry.reading.checkedAt);
  if (!Number.isFinite(checkedAt) || checkedAt > now + 2000 || now - checkedAt > freshnessMs) return modes;

  // Orbit readings may arrive 15+ seconds apart because the watch emits on
  // changed page snapshots, not on a fixed poll schedule. The latest reading's
  // checkedAt and cursor prove freshness; age of the previous API sync does not.
  // Earlier candidates in this page stay RECOVERED and are delivered first.
  const newestIndex = candidates.findLastIndex(candidate =>
    !candidate.gap_before
    && String(candidate.source_metadata?.reading_id) === String(latestCursor));
  if (newestIndex !== candidates.length - 1) return modes;
  modes[newestIndex] = 'LIVE';
  return modes;
}

/** Single-round compatibility helper for callers that need one delivery mode. */
export function classifyOrbitDelivery(args) {
  const modes = classifyOrbitDeliveries(args);
  return modes.length === 1 && modes[0] === 'LIVE' ? 'LIVE' : 'RECOVERED';
}

function required(name) {
  const value = process.env[name]?.trim();
  if (!value) throw new OrbitApiError(`${name}_REQUIRED`);
  return value;
}

function waitForRetry(ms, signal) {
  return new Promise(resolve => {
    let timer;
    const finish = () => {
      clearTimeout(timer);
      signal?.removeEventListener('abort', finish);
      resolve();
    };
    if (signal?.aborted) return finish();
    timer = setTimeout(finish, ms);
    signal?.addEventListener('abort', finish, { once: true });
  });
}

const collectorStorageRetryMs = () => Math.max(1000,
  Math.min(60000, Number(process.env.COLLECTOR_STORAGE_RETRY_MS) || 5000));

/** Keep the supervised collector alive while required PostgreSQL storage reconnects. */
export async function waitForPostgresRoundStore(roundStore, { signal, onWaiting = () => {}, wait = waitForRetry } = {}) {
  while (!signal?.aborted) {
    if (roundStore.pool && !roundStore.usingFileFallback) return true;
    onWaiting(roundStore.storageError || null);
    const retryAt = Number(roundStore.storageRetryAt) || 0;
    const delay = retryAt > Date.now() ? retryAt - Date.now() : collectorStorageRetryMs();
    await wait(delay, signal);
    if (signal?.aborted) return false;
    await roundStore.init({ acquireCollectorLease: true });
  }
  return false;
}

function isPostgresConnectionFailure(error) {
  const code = String(error?.code || '').toUpperCase();
  if (code.startsWith('08') || ['ECONNRESET', 'ECONNREFUSED', 'ETIMEDOUT', 'EPIPE', '57P01', '57P02', '57P03'].includes(code)) return true;
  return /connection (?:terminated|timeout|refused|closed|reset)|server closed the connection|socket hang up|no route to host/i
    .test(String(error?.message || error || ''));
}

function writeStatus(status) {
  fs.mkdirSync(path.dirname(STATUS_PATH), { recursive: true });
  const temporary = `${STATUS_PATH}.tmp`;
  fs.writeFileSync(temporary, JSON.stringify(status, null, 2), { encoding: 'utf8', mode: 0o600 });
  fs.renameSync(temporary, STATUS_PATH);
}

function nextIndex(rounds) {
  return rounds.reduce((max, round) => Math.max(max, Number(round.round_index) || 0), 0) + 1;
}

export function roundRecord({ watchId, reading, item, index, gapBefore = false }) {
  return {
    round_id: orbitRoundIdentity(watchId, reading.id, item.index),
    multiplier: item.multiplier,
    timestamp: reading.checkedAt,
    observed_at: reading.checkedAt,
    local_round_index: index,
    round_index: index,
    platform_round_id: null,
    platform_round_index: null,
    platform_timestamp: null,
    round_identity_type: 'COLLECTOR_OBSERVATION_HASH',
    round_index_source: 'LOCAL_SEQUENCE',
    identity_confidence: 'OVERLAP_VERIFIED_ORDER_ONLY',
    continuity_verified: true,
    gap_before: gapBefore,
    continuity_proof: 'ORBIT_DATA_WATCH_READING_OVERLAP',
    source_metadata: {
      source: 'ORBIT_DATA_WATCH_API',
      watch_id: null,
      reading_id: String(reading.id),
      reading_checked_at: reading.checkedAt,
      selector_item_index: item.index,
      source_selector_item_index: item.sourceItemIndex ?? item.index,
      selector_value: item.rawValue,
      history_container: item.historyContainer || null,
      history_containers: item.historyContainers || null,
      mutation_observed_at: item.mutationObservedAt || null,
      extraction_started_at: item.extractionStartedAt || null,
      multiplier_extracted_at: item.extractedAt || null,
    },
  };
}

export async function startOrbitRoundHistoryCollector(signal) {
  const databaseUrl = process.env.DATABASE_URL || process.env.POSTGRES_URL || process.env.WINNER_DATABASE_URL;
  const status = {
    collectorRunning: true, pid: process.pid, state: 'STARTING', dataSource: 'ORBIT_DATA_WATCH_API',
    browserConnected: false, pageConnected: false, frameConnected: false,
    loggedIn: null, authenticationState: 'NOT_REQUIRED', authRequired: false,
    lastSuccessfulCollection: null, totalRoundsSaved: 0,
    orbitApi: { status: 'NOT_CONFIGURED', transport: 'AUTHENTICATED_SSE', streamStatus: 'DISCONNECTED',
      watchId: null, cursor: '0', lastSuccessfulSync: null, lastStreamConnectedAt: null,
      lastStreamEventAt: null, lastHealthCheckAt: null, lastSourceCheckAt: null,
      latestReadingAt: null, watchUpdatedAt: null, sourceStatus: 'UNKNOWN',
      pages: 0, readings: 0, reprocessedReadings: 0, invalidReadings: 0, truncatedReadings: 0,
      latestReadingTruncated: false, latestReadingMatchCount: null, latestReadingReturnedCount: null,
      latestReadingWindowSize: null,
      latestReadingValidationError: null,
      continuityGaps: 0, unreconstructedReadings: 0, lastError: null,
      lastValidationError: null, watchRecoveries: 0, previousWatchId: null,
      lastWatchRecoveryAt: null, watchRecoveryMode: null, watchSourceMessage: null },
    roundHistoryHealth: { state: 'STALE', latestBrowserObservedMultiplier: null,
      latestAcceptedVerifiedMultiplier: null, latestPersistedRoundId: null,
      latestVerifiedRoundId: null, latestVerifiedDeliveryMode: null,
      latestPublishedRoundId: null, latestFrontendRoundId: null,
      lastObservationAt: null, lastVerifiedAt: null, lastPersistedAt: null,
      backlogCount: 0, coverage: 'UNKNOWN', lastIntegrityReason: null,
      selectorStatus: 'UNKNOWN', selectorDiagnostics: null },
  };
  const flushStatus = () => {
    const success = Date.parse(status.backend?.last_success_at || '');
    if (status.backend?.status === 'AVAILABLE' && (!Number.isFinite(success) || Date.now() - success > 60000)) {
      status.backend.status = 'UNKNOWN';
    }
    writeStatus({ ...status, heartbeatAt: new Date().toISOString() });
  };
  const heartbeatTimer = setInterval(flushStatus, 5000);
  heartbeatTimer.unref?.();
  let roundStore;
  let pipeline;
  let eventOutbox;
  let archive;
  let sync;
  let rounds = [];
  let roundIds = new Set();
  const preEnqueuedRoundIds = new Set();
  let activeSync = null;
  let retryAttempt = 0;
  let failed = false;
  let streamUnsupported = false;
  let streamRetryAt = 0;
  let caughtUp = false;
  let streamWatchHealthy = false;
  let currentStreamReading = null;
  let lastSourceRecoveryCheckAt = 0;
  let postgresRetryTimer = null;
  let postgresRetryRunning = false;
  let postgresRetryAttempt = 0;

  try {
    const baseUrl = required('ORBIT_API_BASE_URL');
    const apiKey = required('ORBIT_API_KEY');
    const client = new OrbitDataWatchClient({ baseUrl, apiKey,
      connectTimeoutMs: Number(process.env.ORBIT_CONNECT_TIMEOUT_MS) || 5000,
      requestTimeoutMs: Number(process.env.ORBIT_REQUEST_TIMEOUT_MS) || 15000 });
    status.orbitApi.status = 'CONNECTING';
    roundStore = new PostgresRoundStore({ connectionString: databaseUrl || null });
    // A bounded startup attempt can seed the old history baseline, but a
    // missing PostgreSQL service must not prevent local Orbit ingestion.
    await roundStore.init({ acquireCollectorLease: true });
    status.state = 'CONNECTING';
    status.orbitApi.status = 'CONNECTING';
    status.orbitApi.lastError = null;
    status.lastError = null;
    status.storage = roundStore.storageSnapshot();
    flushStatus();
    eventOutbox = new DurableRoundOutbox({ filePath: HISTORY_OUTBOX_PATH, eventOutbox: true });
    archive = new LocalOrbitArchive();
    await archive.initialize();
    if (!await archive.getState() && roundStore.pool) {
      try {
        const legacyArchive = new OrbitWatchArchive(roundStore.pool);
        await legacyArchive.initialize();
        const legacyState = await legacyArchive.getState();
        if (legacyState) await archive.seedState(legacyState);
      } catch (error) {
        if (isPostgresConnectionFailure(error)) await roundStore.markUnavailable(error);
        log.warn(`Legacy Watch checkpoint migration deferred: ${formatError(error)}`);
      }
    }
    let postgresRounds = [];
    try { postgresRounds = await roundStore.loadRounds(); }
    catch (error) {
      if (!isPostgresConnectionFailure(error)) throw error;
      await roundStore.markUnavailable(error);
      log.warn(`PostgreSQL history baseline unavailable; resuming from local journal: ${formatError(error)}`);
    }
    let localRounds = await archive.loadRounds();
    if (postgresRounds.length) {
      await archive.seedRounds(postgresRounds.slice(-5000));
      localRounds = await archive.loadRounds();
    }
    const pending = [...roundStore.outbox.readAll(), ...eventOutbox.readAll()];
    const recoveredRounds = new Map([...postgresRounds, ...localRounds]
      .map(row => [String(row.round_id), row]));
    for (const row of pending) if (!recoveredRounds.has(String(row.round_id))) recoveredRounds.set(String(row.round_id), row);
    rounds = [...recoveredRounds.values()];
    rounds.sort((a, b) => Number(a.round_index) - Number(b.round_index));
    roundIds = new Set(rounds.map(round => String(round.round_id)));
    const latestPersistedRound = postgresRounds.at(-1) || null;
    const latestAcceptedRound = rounds.at(-1) || null;
    const localCheckpoint = await archive.getState();
    const visible = localCheckpoint?.snapshot?.visibleMultipliers;
    if (Array.isArray(visible) && visible.length) {
      status.roundHistoryHealth.latestBrowserObservedMultiplier = Number(visible[0]);
    }
    if (latestAcceptedRound) {
      status.roundHistoryHealth.latestAcceptedVerifiedMultiplier = Number(latestAcceptedRound.multiplier);
      status.roundHistoryHealth.lastObservationAt = latestAcceptedRound.source_metadata?.reading_checked_at
        || latestAcceptedRound.observed_at || null;
      status.roundHistoryHealth.coverage = latestAcceptedRound.gap_before ? 'GAP_SUSPECTED' : 'CONTINUOUS';
      status.roundHistoryHealth.state = 'LAGGING';
    }
    if (latestPersistedRound) {
      status.roundHistoryHealth.latestPersistedRoundId = String(latestPersistedRound.round_id);
      status.roundHistoryHealth.lastPersistedAt = latestPersistedRound.stored_at || null;
    }
    pipeline = new RoundDeliveryPipeline({
      outbox: eventOutbox,
      roundStore,
      publishObserved: publishDirectRoundObservations,
      publishPersisted: publishCommittedRoundEvents,
      onCommitted: entries => {
        const byId = new Map(rounds.map(round => [String(round.round_id), round]));
        for (const entry of entries) if (entry.inserted) byId.set(String(entry.round_id), entry.round);
        if (entries.some(entry => entry.inserted)) status.lastSuccessfulCollection = new Date().toISOString();
        rounds = [...byId.values()].sort((a, b) => Number(a.round_index) - Number(b.round_index));
        const newest = entries.at(-1)?.round;
        if (newest) {
          status.roundHistoryHealth.latestPersistedRoundId = String(newest.round_id);
          status.roundHistoryHealth.latestAcceptedVerifiedMultiplier = Number(newest.multiplier);
          status.roundHistoryHealth.lastPersistedAt = new Date().toISOString();
          status.roundHistoryHealth.coverage = newest.gap_before ? 'GAP_SUSPECTED' : 'CONTINUOUS';
        }
        status.totalRoundsSaved = rounds.length;
        status.storage = { ...(status.storage || {}), local_journal: archive.snapshot(),
          postgres: roundStore.storageSnapshot() };
        flushStatus();
      },
      onFailure: failure => {
        status.orbitApi.lastError = failure.category || 'ROUND_DELIVERY_PENDING';
        status.storage = { ...(status.storage || {}), round_delivery: pipeline.snapshot(),
          local_journal: archive.snapshot(), postgres: roundStore.storageSnapshot() };
        if (failure.stage === 'observed' || failure.stage === 'persisted') {
          status.backend = { ...(status.backend || {}), status: 'UNAVAILABLE' };
        }
        flushStatus();
      },
      onTimingUpdate: delivery => {
        status.storage = { ...(status.storage || {}), round_delivery: delivery,
          local_journal: archive.snapshot(), postgres: roundStore.storageSnapshot() };
        status.roundHistoryHealth.backlogCount = Number(delivery.backlog_size) || 0;
        const latestTiming = delivery.last_round_timing;
        if (latestTiming?.timestamps?.persisted_event_acknowledged_at) {
          status.roundHistoryHealth.latestPublishedRoundId = String(latestTiming.round_id);
        }
        if (delivery.last_backend_success_at && !['observed', 'persisted'].includes(delivery.last_failure?.stage)) {
          status.backend = { status: 'AVAILABLE', last_success_at: delivery.last_backend_success_at };
        }
        flushStatus();
      },
    });
    status.storage = { round_delivery: pipeline.snapshot(), local_journal: archive.snapshot(),
      postgres: roundStore.storageSnapshot() };
    status.backend = { status: 'UNKNOWN', last_success_at: null };
    void pipeline.start({ recovered: true });

    // Retry PostgreSQL independently from the Orbit SSE consumer. Pending round
    // records remain in the existing fsynced outbox until PG confirms commit.
    const retryPostgres = async () => {
      postgresRetryTimer = null;
      if (postgresRetryRunning || signal?.aborted) return;
      postgresRetryRunning = true;
      try {
        if (!roundStore.pool && Date.now() >= (Number(roundStore.storageRetryAt) || 0)) {
          await roundStore.init({ acquireCollectorLease: true });
        }
        if (roundStore.pool) await roundStore.retryAndDrain();
        postgresRetryAttempt = roundStore.pool ? 0 : postgresRetryAttempt + 1;
      } catch (error) {
        if (isPostgresConnectionFailure(error)) await roundStore.markUnavailable(error);
        else log.warn(`PostgreSQL reconnect check deferred: ${formatError(error)}`);
        postgresRetryAttempt += 1;
      } finally {
        status.storage = { ...(status.storage || {}), local_journal: archive.snapshot(),
          postgres: roundStore.storageSnapshot(), round_delivery: pipeline.snapshot() };
        flushStatus();
        postgresRetryRunning = false;
        if (!signal?.aborted) {
          const delay = roundStore.pool ? 30000
            : Math.min(60000, collectorStorageRetryMs() * 2 ** Math.min(postgresRetryAttempt, 4));
          postgresRetryTimer = setTimeout(retryPostgres, delay);
          postgresRetryTimer.unref?.();
        }
      }
    };
    postgresRetryTimer = setTimeout(retryPostgres, collectorStorageRetryMs());
    postgresRetryTimer.unref?.();

    sync = new OrbitWatchSync({ client, store: archive,
      onPagePrepared: progress => {
        const pendingRounds = progress.pendingRounds || [];
        if (!pendingRounds.length) return;
        const timingsByRoundId = { ...(progress.timingsByRoundId || {}) };
        for (const round of pendingRounds) {
          const source = round.source_metadata || {};
          const timing = timingsByRoundId[String(round.round_id)] || {};
          const observedAt = Date.parse(source.mutation_observed_at || '');
          const extractionStartedAt = Date.parse(source.extraction_started_at || '');
          const extractedAt = Date.parse(source.multiplier_extracted_at || '');
          Object.assign(timing, {
            mutationObservedAt: source.mutation_observed_at || null,
            extractionStartedAt: source.extraction_started_at || null,
            extractedAt: source.multiplier_extracted_at || null,
            nodeReceivedAt: new Date().toISOString(),
            normalizedAt: new Date().toISOString(),
            validationCompletedAt: new Date().toISOString(),
            ...(Number.isFinite(observedAt) && Number.isFinite(extractedAt)
              ? { pageMutationToExtractionMs: Math.max(0, extractedAt - observedAt) } : {}),
            ...(Number.isFinite(extractionStartedAt) && Number.isFinite(extractedAt)
              ? { pageExtractionMs: Math.max(0, extractedAt - extractionStartedAt) } : {}),
          });
          timingsByRoundId[String(round.round_id)] = timing;
        }
        pipeline.enqueue(pendingRounds, {
          deliveryModes: pendingRounds.map(round => round.delivery_mode || 'RECOVERED'),
          timingsByRoundId,
        });
        for (const round of pendingRounds) preEnqueuedRoundIds.add(String(round.round_id));
      },
      onPageCommitted: progress => {
        const pendingRounds = progress.pendingRounds || [];
        if (pendingRounds.length) {
          const notPreEnqueued = pendingRounds.filter(round =>
            !preEnqueuedRoundIds.delete(String(round.round_id)));
          if (notPreEnqueued.length) pipeline.enqueue(notPreEnqueued, {
            deliveryModes: notPreEnqueued.map(round => round.delivery_mode || 'RECOVERED'),
            timingsByRoundId: progress.timingsByRoundId || null,
          });
          const byId = new Map(rounds.map(round => [String(round.round_id), round]));
          for (const round of pendingRounds) {
            byId.set(String(round.round_id), round);
            roundIds.add(String(round.round_id));
          }
          rounds = [...byId.values()].sort((a, b) => Number(a.round_index) - Number(b.round_index));
        }
        status.orbitApi.watchId = progress.watchId;
        status.orbitApi.cursor = progress.cursor;
        status.orbitApi.pages = progress.pages;
        status.orbitApi.readings = progress.readings;
        status.orbitApi.reprocessedReadings = progress.repairedReadings || status.orbitApi.reprocessedReadings;
        status.totalRoundsSaved = rounds.length;
        flushStatus();
      },
      onPage: async ({ watchId, entries, state, latestReading, latestCursor, watchHealthy, hasMore, pageIndex }) => {
        const savedSnapshot = state?.snapshot;
        const repairBaselineUnavailable = savedSnapshot?.repairBaselineUnavailable === true;
        const unreconstructedReadings = Number(savedSnapshot?.unreconstructedReadings) || 0;
        let currentSnapshot = savedSnapshot?.version === 2 && Array.isArray(savedSnapshot.visibleMultipliers)
          ? savedSnapshot.visibleMultipliers : null;
        let bootstrapUntilCursor = typeof savedSnapshot?.bootstrapUntilCursor === 'string'
          ? savedSnapshot.bootstrapUntilCursor : null;
        let gapPending = savedSnapshot?.gapPending === true;
        let recoveryRequired = false;
        if (currentSnapshot === null && BigInt(state?.cursor || '0') > 0n) {
          const archivedBaseline = await archive.getLatestVisibleSnapshot(watchId, state.cursor);
          if (archivedBaseline) {
            currentSnapshot = archivedBaseline.multipliers;
            bootstrapUntilCursor = null;
            // A null persisted snapshot means the last archived row could not
            // be used as a continuity boundary. Keep the next round marked as
            // recovered even when a valid older baseline can be restored.
            gapPending = true;
          }
        }
        if (currentSnapshot === null && rounds.length > 0 && latestReading) {
          const latestResolution = resolveVisiblePayoutItems(latestReading.visibleItems);
          currentSnapshot = latestResolution.items.map(item => item.multiplier);
          status.roundHistoryHealth.selectorStatus = latestResolution.status;
          status.roundHistoryHealth.selectorDiagnostics = latestResolution.diagnostics;
          bootstrapUntilCursor = latestCursor;
          gapPending = true;
          if (BigInt(state?.cursor || '0') > 0n) {
            status.orbitApi.continuityGaps += 1;
            status.orbitApi.lastValidationError = 'ORBIT_BASELINE_UNAVAILABLE';
          }
        }
        const maySeedHistory = currentSnapshot === null && rounds.length === 0
          && BigInt(state?.cursor || '0') === 0n;
        let index = nextIndex(rounds);
        const candidates = [];
        for (const entry of entries) {
          if (!entry.parsed) {
            status.orbitApi.invalidReadings += 1;
            status.orbitApi.lastValidationError = entry.error || 'INVALID_READING';
            recoveryRequired = true;
            if (!bootstrapUntilCursor || BigInt(entry.reading.id) > BigInt(bootstrapUntilCursor)) {
              currentSnapshot = null;
              gapPending = true;
            }
            continue;
          }
          const parsed = entry.parsed;
          if (parsed.truncated) {
            status.orbitApi.truncatedReadings += 1;
            status.orbitApi.lastValidationError = 'ORBIT_SELECTOR_RESULTS_TRUNCATED';
            recoveryRequired = true;
          } else if (status.orbitApi.lastValidationError === 'ORBIT_SELECTOR_RESULTS_TRUNCATED') {
            status.orbitApi.lastValidationError = null;
          }
          const selection = resolveVisiblePayoutItems(parsed.visibleItems);
          const visibleItems = selection.items;
          const nextSnapshot = visibleItems.map(item => item.multiplier);
          status.roundHistoryHealth.selectorStatus = selection.status;
          status.roundHistoryHealth.selectorDiagnostics = selection.diagnostics;
          status.roundHistoryHealth.lastObservationAt = parsed.checkedAt || entry.reading.checkedAt;
          status.roundHistoryHealth.latestBrowserObservedMultiplier = parsed.visibleItems[0]?.multiplier ?? null;
          if (!visibleItems.length) {
            currentSnapshot = null;
            gapPending = true;
            recoveryRequired = true;
            const ambiguousContainer = selection.status === 'AMBIGUOUS'
              || parsed.visibleItems.length >= 20;
            status.orbitApi.continuityGaps += 1;
            status.orbitApi.lastValidationError = ambiguousContainer
              ? 'AMBIGUOUS_HISTORY_CONTAINER' : 'EMPTY_VISIBLE_SNAPSHOT';
            status.roundHistoryHealth.state = ambiguousContainer ? 'GAP_SUSPECTED' : 'RECOVERING';
            status.roundHistoryHealth.coverage = ambiguousContainer ? 'AMBIGUOUS_SNAPSHOT' : 'UNKNOWN';
            status.roundHistoryHealth.lastIntegrityReason = status.orbitApi.lastValidationError;
            continue;
          }
          let selectedItems = [];
          if (bootstrapUntilCursor && BigInt(entry.reading.id) <= BigInt(bootstrapUntilCursor)) {
            // Preserve legacy PostgreSQL history while archiving old Orbit pages.
            // The latest real Orbit reading is the timestamp-independent source baseline.
            continue;
          } else if (bootstrapUntilCursor) {
            bootstrapUntilCursor = null;
          }
          if (currentSnapshot === null && maySeedHistory) {
            selectedItems = [...visibleItems].reverse();
          } else if (currentSnapshot !== null) {
            const delta = snapshotDelta(currentSnapshot, nextSnapshot);
            if (delta.status === 'VERIFIED') {
              selectedItems = [...visibleItems.slice(0, delta.rounds.length)].reverse();
              if (status.orbitApi.lastValidationError === 'AMBIGUOUS_HISTORY_CONTAINER') {
                status.orbitApi.lastValidationError = null;
                status.roundHistoryHealth.lastIntegrityReason = null;
                status.roundHistoryHealth.coverage = 'RECOVERED_FROM_HISTORY';
              }
            }
            else if (delta.status === 'CONTINUITY_UNVERIFIED') {
              gapPending = true;
              recoveryRequired = true;
              status.orbitApi.continuityGaps += 1;
              status.orbitApi.lastValidationError = 'CONTINUITY_UNVERIFIED';
            }
          }
          for (let itemIndex = 0; itemIndex < selectedItems.length; itemIndex += 1) {
            const record = roundRecord({ watchId, reading: entry.reading, item: selectedItems[itemIndex], index,
              gapBefore: gapPending });
            record.source_metadata.watch_id = watchId;
            if (currentStreamReading?.id === String(entry.reading.id)) {
              record.source_metadata.collector_received_at = currentStreamReading.receivedAt;
            }
            if (roundIds.has(record.round_id) || candidates.some(round => round.round_id === record.round_id)) continue;
            candidates.push(record);
            status.roundHistoryHealth.latestAcceptedVerifiedMultiplier = record.multiplier;
            status.roundHistoryHealth.latestVerifiedRoundId = String(record.round_id);
            status.roundHistoryHealth.lastVerifiedAt = new Date().toISOString();
            status.roundHistoryHealth.coverage = gapPending ? 'GAP_SUSPECTED' : 'CONTINUOUS';
            gapPending = false;
            index += 1;
          }
          currentSnapshot = nextSnapshot;
        }
        let deliveryModes = [];
        status.roundHistoryHealth.backlogCount = eventOutbox.readAll().length + roundStore.outbox.readAll().length;
        if (candidates.length) {
          const now = Date.now();
          deliveryModes = classifyOrbitDeliveries({ caughtUp, watchHealthy, hasMore, pageIndex,
            noBacklog: eventOutbox.readAll().length === 0 && roundStore.outbox.readAll().length === 0,
            entries, latestCursor,
            candidates, now, freshnessMs: 15000 });
          status.roundHistoryHealth.latestVerifiedDeliveryMode = deliveryModes.at(-1) || 'UNKNOWN';
        }
        status.roundHistoryHealth.state = deliveryModes.includes('LIVE') ? 'LIVE'
          : gapPending ? 'GAP_SUSPECTED' : candidates.length ? 'RECOVERING' : 'LAGGING';
        return { snapshot: { version: 2, visibleMultipliers: currentSnapshot,
          ...(bootstrapUntilCursor ? { bootstrapUntilCursor } : {}), ...(gapPending ? { gapPending: true } : {}),
          ...(repairBaselineUnavailable ? { repairBaselineUnavailable: true, unreconstructedReadings } : {}) },
          candidates, deliveryModes, recoveryRequired };
      },
    });
    const runSync = async () => {
      const task = sync.sync();
      activeSync = task;
      try { return await task; }
      finally {
        if (activeSync === task) activeSync = null;
        flushStatus();
      }
    };
    const runStreamReading = async (options) => {
      const task = sync.syncStreamReading(options);
      activeSync = task;
      try { return await task; }
      finally {
        if (activeSync === task) activeSync = null;
        flushStatus();
      }
    };
    status.state = 'COLLECTING';
    status.totalRoundsSaved = rounds.length;
    flushStatus();

    const recordSync = result => {
      caughtUp = result.watchHealthy;
      streamWatchHealthy = Boolean(result.watchHealthy);
      status.state = !result.watchHealthy ? 'WAITING_FOR_SOURCE'
        : status.roundHistoryHealth.state === 'GAP_SUSPECTED' ? 'RECOVERING' : 'COLLECTING';
      status.orbitApi.status = 'CONNECTED';
      status.orbitApi.sourceStatus = result.watchHealthy ? 'HEALTHY'
        : result.watchCollectorStatus?.toUpperCase() || (result.watchSourceError ? 'ERROR' : 'UNHEALTHY');
      status.orbitApi.watchId = result.watchId;
      status.orbitApi.cursor = result.cursor;
      status.orbitApi.pages = result.pages;
      status.orbitApi.readings += result.readings;
      status.orbitApi.reprocessedReadings += result.repairedReadings || 0;
      status.orbitApi.unreconstructedReadings = result.unreconstructedReadings || 0;
      status.orbitApi.latestReadingAt = result.latestReadingAt;
      status.orbitApi.watchUpdatedAt = result.watchUpdatedAt;
      if (result.lastSourceCheckAt) status.orbitApi.lastSourceCheckAt = result.lastSourceCheckAt;
      status.orbitApi.latestReadingTruncated = Boolean(result.latestReadingTruncated);
      status.orbitApi.latestReadingMatchCount = result.latestReadingMatchCount ?? null;
      status.orbitApi.latestReadingReturnedCount = result.latestReadingReturnedCount ?? null;
      status.orbitApi.latestReadingWindowSize = result.latestReadingWindowSize ?? null;
      status.orbitApi.latestReadingValidationError = result.latestReadingError
        || (result.latestReadingTruncated ? 'ORBIT_SELECTOR_RESULTS_TRUNCATED' : null);
      status.orbitApi.lastSuccessfulSync = new Date().toISOString();
      status.orbitApi.lastHealthCheckAt = status.orbitApi.lastSuccessfulSync;
      if (result.watchRecovered) {
        status.orbitApi.watchRecoveries += 1;
        status.orbitApi.previousWatchId = result.previousWatchId;
        status.orbitApi.watchRecoveryMode = result.watchRecoveryMode || 'IDENTITY_VERIFIED';
        status.orbitApi.lastWatchRecoveryAt = status.orbitApi.lastSuccessfulSync;
        if (status.orbitApi.lastValidationError === 'ORBIT_REPAIR_BASELINE_UNAVAILABLE') {
          status.orbitApi.lastValidationError = null;
        }
      }
      if (result.latestReadingError) status.orbitApi.lastValidationError = result.latestReadingError;
      if (result.repairBaselineUnavailable) {
        status.orbitApi.lastValidationError = 'ORBIT_REPAIR_BASELINE_UNAVAILABLE';
        status.orbitApi.continuityGaps = Math.max(1, status.orbitApi.continuityGaps);
      }
      status.orbitApi.lastError = result.watchSourceError ? 'WATCH_SOURCE_ERROR'
        : result.watchHealthy ? null : result.watchCollectorStatus?.toUpperCase() || 'WATCH_SOURCE_UNHEALTHY';
      status.orbitApi.watchSourceMessage = result.watchLastError || null;
    };

    const recordWatchStatus = watch => {
      const collectorStatus = String(watch.collectorStatus || watch.status || 'unknown').toLowerCase();
      const watchStatus = String(watch.status || (
        ['running', 'error', 'waiting_for_connection', 'waiting_for_browser', 'stale'].includes(collectorStatus)
          ? 'running' : collectorStatus
      )).toLowerCase();
      status.orbitApi.lastHealthCheckAt = new Date().toISOString();
      status.orbitApi.sourceStatus = watchStatus !== 'running' ? watchStatus.toUpperCase()
        : collectorStatus === 'running' && !watch.lastError ? 'HEALTHY' : collectorStatus.toUpperCase();
      status.orbitApi.lastError = watch.lastError ? 'WATCH_SOURCE_ERROR'
        : status.orbitApi.sourceStatus === 'HEALTHY' ? null : status.orbitApi.sourceStatus;
      status.orbitApi.watchSourceMessage = typeof watch.lastError === 'string' ? watch.lastError : null;
      if (watchStatus === 'running' && status.orbitApi.sourceStatus === 'HEALTHY') {
        status.state = status.roundHistoryHealth.state === 'GAP_SUSPECTED' ? 'RECOVERING' : 'COLLECTING';
      }
      else status.state = 'WAITING_FOR_SOURCE';
      streamWatchHealthy = watchStatus === 'running'
        && collectorStatus === 'running' && !watch.lastError;
      if (watch.lastCheckedAt) status.orbitApi.lastSourceCheckAt = watch.lastCheckedAt;
      status.orbitApi.watchIntervalMs = watch.intervalMs ?? null;
      flushStatus();
    };

    const recordFailure = (error, fallbackCode) => {
      const code = error.code || fallbackCode;
      const authFailure = error.status === 401 || error.status === 403;
      const storageUnavailable = code === 'POSTGRES_UNAVAILABLE'
        || code === 'LOCAL_JOURNAL_FULL' || String(code).startsWith('LOCAL_JOURNAL_')
        || ['SQLITE_FULL', 'EIO', 'ENOSPC'].includes(String(code));
      const watchUnavailable = ['SAVED_WATCH_NOT_FOUND', 'SAVED_WATCH_SELECTOR_MISMATCH',
        'WATCH_NOT_RUNNING', 'AVIATOR_WATCH_UNAVAILABLE', 'MULTIPLE_AVIATOR_WATCHES',
        'SAVED_WATCH_CHANGED', 'AMBIGUOUS_WATCH_RECOVERY'].includes(code);
      status.state = storageUnavailable ? 'STORAGE_UNAVAILABLE'
        : watchUnavailable ? 'WAITING_FOR_SOURCE' : authFailure ? 'AUTH_ERROR' : 'RECOVERING';
      status.orbitApi.status = authFailure ? 'AUTH_ERROR' : watchUnavailable ? 'CONNECTED' : 'UNAVAILABLE';
      status.orbitApi.sourceStatus = code === 'WATCH_NOT_RUNNING' ? 'STOPPED'
        : code === 'SAVED_WATCH_NOT_FOUND' ? 'MISSING'
          : ['MULTIPLE_AVIATOR_WATCHES', 'AMBIGUOUS_WATCH_RECOVERY'].includes(code) ? 'AMBIGUOUS'
            : code === 'SAVED_WATCH_CHANGED' ? 'UNAVAILABLE' : watchUnavailable ? 'UNAVAILABLE'
            : status.orbitApi.sourceStatus;
      status.orbitApi.lastHealthCheckAt = new Date().toISOString();
      status.orbitApi.streamStatus = 'RECONNECTING';
      status.orbitApi.lastError = code;
      if (storageUnavailable) status.storage = { ...(status.storage || {}),
        local_journal: archive.snapshot(), postgres: roundStore.storageSnapshot() };
      flushStatus();
    };

    while (!signal?.aborted) {
      if (streamUnsupported && Date.now() < streamRetryAt) {
        try {
          const fallbackResult = await runSync();
          recordSync(fallbackResult);
          status.orbitApi.transport = 'REST_FALLBACK_POLLING';
          status.orbitApi.streamStatus = 'UNSUPPORTED';
          status.orbitApi.lastError = 'EVENT_STREAM_UNSUPPORTED';
        } catch (error) {
          caughtUp = false;
          recordFailure(error, 'ORBIT_FALLBACK_SYNC_FAILED');
        }
        await waitForRetry(1000, signal);
        continue;
      }
      streamUnsupported = false;
      status.orbitApi.transport = 'AUTHENTICATED_SSE';
      let streamAttempted = false;
      let streamOpenedAt = null;
      try {
        const result = await runSync();
        recordSync(result);
        status.orbitApi.streamStatus = 'CONNECTING';
        flushStatus();
        streamAttempted = true;
        await client.stream(result.watchId, result.cursor, { signal, windowSize: LIVE_OVERLAP_WINDOW_SIZE,
          onOpen: () => {
            streamOpenedAt = Date.now();
            status.orbitApi.streamStatus = 'CONNECTED';
            status.orbitApi.lastStreamConnectedAt = new Date().toISOString();
            flushStatus();
          },
          onEvent: async message => {
          status.orbitApi.lastStreamEventAt = new Date().toISOString();
          if (message.type === 'status') {
            recordWatchStatus(message.data);
            const sourceUnavailable = message.data?.status !== 'running'
              || message.data?.collectorStatus !== 'running' || Boolean(message.data?.lastError);
            if (sourceUnavailable && Date.now() - lastSourceRecoveryCheckAt >= 15000) {
              lastSourceRecoveryCheckAt = Date.now();
              const recovered = await runSync();
              recordSync(recovered);
              if (recovered.watchId !== result.watchId) {
                throw new OrbitApiError('WATCH_SOURCE_CHANGED', { retryable: true });
              }
            } else if (streamWatchHealthy && !caughtUp) {
              const recovered = await runSync();
              recordSync(recovered);
              if (recovered.watchId !== result.watchId) {
                throw new OrbitApiError('WATCH_SOURCE_CHANGED', { retryable: true });
              }
            }
            return;
          }
          if (message.type !== 'reading') return;
          if (BigInt(message.id) <= BigInt(status.orbitApi.cursor || '0')) return;
          try {
            currentStreamReading = { id: message.id, receivedAt: message.receivedAt,
              receivedMonotonicMs: message.receivedMonotonicMs };
            let updated;
            try {
              updated = await runStreamReading({
                watchId: result.watchId,
                reading: message.data,
                watchHealthy: caughtUp && streamWatchHealthy,
                receivedAt: message.receivedAt,
                receivedMonotonicMs: message.receivedMonotonicMs,
              });
            } catch (error) {
              if (!['STREAM_SYNC_CHECKPOINT_UNAVAILABLE', 'ARCHIVE_CURSOR_CONFLICT', 'STREAM_ROUND_RECOVERY_REQUIRED']
                .includes(error?.code)) throw error;
              updated = await runSync();
            }
            if (!streamCursorCoversReading(updated.cursor, message.id)) {
              throw new OrbitApiError('STREAM_READING_NOT_ARCHIVED', { retryable: true });
            }
            recordSync(updated);
          } catch (error) {
            caughtUp = false;
            throw error;
          } finally {
            currentStreamReading = null;
          }
        } });
        if (!signal?.aborted) throw new OrbitApiError('STREAM_DISCONNECTED', { retryable: true });
      } catch (error) {
        if (signal?.aborted) break;
        caughtUp = false;
        if (error.code === 'WATCH_SOURCE_CHANGED') {
          retryAttempt = 0;
          status.orbitApi.status = 'CONNECTED';
          status.orbitApi.streamStatus = 'RECONNECTING';
          status.orbitApi.lastError = null;
          flushStatus();
          continue;
        }
        if (error.status === 404 && streamAttempted && streamOpenedAt === null) {
          streamUnsupported = true;
          streamRetryAt = Date.now() + 30000;
          status.state = 'COLLECTING';
          status.orbitApi.status = 'CONNECTED';
          status.orbitApi.transport = 'REST_FALLBACK_POLLING';
          status.orbitApi.streamStatus = 'UNSUPPORTED';
          status.orbitApi.lastError = 'EVENT_STREAM_UNSUPPORTED';
          flushStatus();
          continue;
        }
        if (streamOpenedAt !== null && Date.now() - streamOpenedAt >= 30000) retryAttempt = 0;
        retryAttempt += 1;
        recordFailure(error, 'ORBIT_STREAM_FAILED');
        if (!error.code && error instanceof Error) {
          const frame = error.stack?.split('\n').find(line => line.trim().startsWith('at '))?.trim();
          process.stderr.write(`[collector] Orbit stream failed (${error.name})${frame ? ` ${frame}` : ''}\n`);
        }
        flushStatus();
        await waitForRetry(Math.min(60000, 1000 * 2 ** Math.min(retryAttempt - 1, 6)), signal);
      }
    }
  } catch (error) {
    failed = true;
    status.collectorRunning = false;
    status.state = 'ERROR';
    status.orbitApi.status = error.status === 401 || error.status === 403 ? 'AUTH_ERROR'
      : error.code?.endsWith('_REQUIRED') ? 'NOT_CONFIGURED' : 'UNAVAILABLE';
    status.orbitApi.lastError = error.code || 'ORBIT_COLLECTOR_FAILED';
    flushStatus();
    throw error;
  } finally {
    clearInterval(heartbeatTimer);
    if (postgresRetryTimer) clearTimeout(postgresRetryTimer);
    await activeSync?.catch(() => {});
    await pipeline?.stop();
    status.collectorRunning = false;
    if (!failed) status.state = 'STOPPING';
    flushStatus();
    await archive?.close().catch(() => {});
    await roundStore?.close().catch(() => {});
  }
}
