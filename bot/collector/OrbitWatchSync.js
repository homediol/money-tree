import { discoverAviatorWatch, parseOrbitReading, validateHistoryPage, visiblePayoutItems } from './OrbitReadings.js';
import { OrbitApiError } from './OrbitDataWatchClient.js';

const PAYOUT_SELECTOR = '.payouts-block .payout';
export const LIVE_OVERLAP_WINDOW_SIZE = 12;

function healthyWatch(watch) {
  return watch?.status === 'running' && watch?.collectorStatus === 'running' && !watch?.lastError;
}

function sourceIdentity(watch) {
  if (!watch || typeof watch !== 'object') return null;
  const numericId = value => value !== null && value !== undefined
    && Number.isInteger(Number(value)) ? Number(value) : null;
  return {
    engine: watch.engine || null,
    targetId: watch.targetId || null,
    tabId: numericId(watch.tabId),
    frameId: numericId(watch.frameId),
    frameUrl: typeof watch.frameUrl === 'string' ? watch.frameUrl : null,
    frameSelector: typeof watch.frameSelector === 'string' ? watch.frameSelector.trim() : '',
    selector: typeof watch.selector === 'string' ? watch.selector.trim() : null,
  };
}

function sortedQueryEntries(url, ignored = new Set()) {
  return [...url.searchParams.entries()]
    .filter(([key]) => !ignored.has(key.toLowerCase()))
    .sort(([leftKey, leftValue], [rightKey, rightValue]) =>
      leftKey.localeCompare(rightKey) || leftValue.localeCompare(rightValue));
}

function accountIdentityEntries(url) {
  const accountKeys = new Set(['user', 'user_id', 'userid', 'account', 'account_id', 'player_id']);
  return [...url.searchParams.entries()]
    .filter(([key, value]) => accountKeys.has(key.toLowerCase()) && value.trim())
    .sort(([leftKey, leftValue], [rightKey, rightValue]) =>
      leftKey.localeCompare(rightKey) || leftValue.localeCompare(rightValue));
}

function sameReturnUrl(candidate, saved) {
  const values = url => [...url.searchParams.entries()]
    .filter(([key]) => key.toLowerCase() === 'return_url')
    .map(([, value]) => value);
  const candidateValues = values(candidate);
  const savedValues = values(saved);
  if (candidateValues.length !== savedValues.length || candidateValues.length > 1) return false;
  if (!candidateValues.length) return true;

  try {
    const candidateReturn = new URL(candidateValues[0]);
    const savedReturn = new URL(savedValues[0]);
    const transientReturnParameters = new Set(['sid', 'token', 'ts']);
    return candidateReturn.origin === savedReturn.origin
      && candidateReturn.pathname === savedReturn.pathname
      && candidateReturn.hash === savedReturn.hash
      && JSON.stringify(sortedQueryEntries(candidateReturn, transientReturnParameters))
        === JSON.stringify(sortedQueryEntries(savedReturn, transientReturnParameters));
  } catch { return false; }
}

function matchesSavedFrame(candidateValue, savedValue) {
  try {
    const candidate = new URL(String(candidateValue || ''));
    const saved = new URL(String(savedValue || ''));
    const transientParameters = new Set(['token', 'ts']);
    const transientValues = url => [...url.searchParams.entries()]
      .filter(([key]) => transientParameters.has(key.toLowerCase()))
      .sort(([leftKey, leftValue], [rightKey, rightValue]) =>
        leftKey.localeCompare(rightKey) || leftValue.localeCompare(rightValue));
    const savedAccount = accountIdentityEntries(saved);
    const candidateAccount = accountIdentityEntries(candidate);
    const stableAccountIdentity = savedAccount.length > 0
      && JSON.stringify(candidateAccount) === JSON.stringify(savedAccount);
    const sameFrame = ['http:', 'https:'].includes(candidate.protocol)
      && candidate.origin === saved.origin && candidate.pathname === saved.pathname
      && candidate.hash === saved.hash;
    if (!sameFrame) return false;

    const stableParametersMatch = JSON.stringify(sortedQueryEntries(candidate, transientParameters))
      === JSON.stringify(sortedQueryEntries(saved, transientParameters));
    const transientValuesMatch = JSON.stringify(transientValues(candidate))
      === JSON.stringify(transientValues(saved));
    if (stableParametersMatch && (transientValuesMatch || stableAccountIdentity)) return true;

    // Aviator rotates its iframe token, dummyVar and return_url sid when the
    // same account reconnects. Rebind only when the account and every other
    // stable launch parameter still match exactly.
    if (!stableAccountIdentity) return false;
    const reconnectParameters = new Set(['token', 'ts', 'dummyvar', 'return_url']);
    return JSON.stringify(sortedQueryEntries(candidate, reconnectParameters))
        === JSON.stringify(sortedQueryEntries(saved, reconnectParameters))
      && sameReturnUrl(candidate, saved);
  } catch { return false; }
}

function sameRecoverableScope(candidate, savedIdentity) {
  const candidateIdentity = sourceIdentity(candidate);
  return Boolean(candidateIdentity && savedIdentity
    && candidateIdentity.engine === 'connected-chrome'
    && savedIdentity.engine === 'connected-chrome'
    && candidateIdentity.selector === PAYOUT_SELECTOR
    && savedIdentity.selector === PAYOUT_SELECTOR
    && candidateIdentity.frameSelector === String(savedIdentity.frameSelector || '').trim()
    && matchesSavedFrame(candidateIdentity.frameUrl, savedIdentity.frameUrl));
}

function sameVisibleGameTab(candidate, savedIdentity) {
  const candidateIdentity = sourceIdentity(candidate);
  if (!candidateIdentity || !savedIdentity
      || candidateIdentity.engine !== 'connected-chrome'
      || savedIdentity.engine !== 'connected-chrome'
      || candidateIdentity.selector !== PAYOUT_SELECTOR
      || savedIdentity.selector !== PAYOUT_SELECTOR
      || candidateIdentity.tabId === null
      || candidateIdentity.tabId !== savedIdentity.tabId
      || candidateIdentity.frameSelector !== String(savedIdentity.frameSelector || '').trim()) return false;
  try {
    const candidateUrl = new URL(candidateIdentity.frameUrl || '');
    const savedUrl = new URL(savedIdentity.frameUrl || '');
    return ['http:', 'https:'].includes(candidateUrl.protocol)
      && candidateUrl.origin === savedUrl.origin
      && candidateUrl.pathname === savedUrl.pathname;
  } catch { return false; }
}

function executionScopeKey(watch) {
  const identity = sourceIdentity(watch);
  return JSON.stringify([identity?.engine, identity?.targetId, identity?.tabId, identity?.frameId]);
}

function freshness(watch) {
  for (const value of [watch?.lastCheckedAt, watch?.lastAttemptAt, watch?.updatedAt, watch?.createdAt]) {
    const parsed = Date.parse(value || '');
    if (Number.isFinite(parsed)) return parsed;
  }
  return 0;
}

function selectRecoveryWatch(watches, savedIdentity, savedWatchId) {
  const matches = watches.filter(item => item?.id !== savedWatchId
    && healthyWatch(item) && sameRecoverableScope(item, savedIdentity));
  if (!matches.length) return null;
  const scopes = new Set(matches.map(executionScopeKey));
  if (scopes.size > 1) throw new OrbitApiError('AMBIGUOUS_WATCH_RECOVERY');
  return [...matches].sort((left, right) => freshness(right) - freshness(left)
    || String(left.id).localeCompare(String(right.id)))[0];
}

function selectSameTabReanchorWatch(watches, savedIdentity, savedWatchId) {
  const matches = watches.filter(item => item?.id !== savedWatchId
    && healthyWatch(item) && sameVisibleGameTab(item, savedIdentity));
  if (matches.length > 1) throw new OrbitApiError('AMBIGUOUS_WATCH_RECOVERY');
  return matches[0] || null;
}

function withSourceIdentity(snapshot, watch) {
  const value = snapshot && typeof snapshot === 'object' ? snapshot : {};
  return { ...value, sourceIdentity: sourceIdentity(watch) };
}

export function streamCursorCoversReading(cursor, readingId) {
  const valid = value => typeof value === 'string' && /^\d{1,19}$/.test(value)
    && BigInt(value) <= 9223372036854775807n;
  return valid(cursor) && valid(readingId) && BigInt(cursor) >= BigInt(readingId);
}

export function streamSyncCoversReading(result, expectedWatchId, readingId) {
  return result?.watchId === expectedWatchId && streamCursorCoversReading(result.cursor, readingId);
}

/** Archive every Orbit reading and its validation outcome before advancing a cursor. */
export class OrbitWatchSync {
  constructor({ client, store, onPage = async () => null, onPagePrepared = async () => {},
    onPageCommitted = async () => {} }) {
    this.client = client;
    this.store = store;
    this.onPage = onPage;
    this.onPagePrepared = onPagePrepared;
    this.onPageCommitted = onPageCommitted;
    this.inFlight = false;
    this.repairedWatchIds = new Set();
  }

  /** Process one authenticated SSE reading without rediscovering the Watch or replaying history. */
  async syncStreamReading({ watchId, reading, watchHealthy = false,
    receivedAt = null, receivedMonotonicMs = null } = {}) {
    if (this.inFlight) throw new OrbitApiError('SYNC_ALREADY_RUNNING');
    this.inFlight = true;
    try {
      let state = await this.store.getState();
      await this.flushPending({ watchId: state?.watchId || null,
        cursor: state?.cursor || '0', pages: 0, readings: 0 });
      if (!state || state.watchId !== watchId || !Array.isArray(state.snapshot?.visibleMultipliers)
          || !state.snapshot?.sourceIdentity || typeof state.snapshot.sourceIdentity !== 'object') {
        throw new OrbitApiError('STREAM_SYNC_CHECKPOINT_UNAVAILABLE', { retryable: true });
      }
      if (!reading || reading.watchId !== watchId || reading.selector?.trim() !== PAYOUT_SELECTOR
          || typeof reading.id !== 'string' || !/^\d{1,19}$/.test(reading.id)
          || BigInt(reading.id) > 9223372036854775807n) {
        throw new OrbitApiError('INVALID_STREAM_READING');
      }

      const expectedCursor = String(state.cursor || '0');
      const rawReading = { id: reading.id, checkedAt: reading.checkedAt,
        count: reading.count, items: reading.items,
        ...(reading.projection ? { projection: reading.projection } : {}),
        ...(Number.isInteger(reading.windowSize) ? { windowSize: reading.windowSize } : {}),
        ...(typeof reading.historyContainer === 'string' ? { historyContainer: reading.historyContainer } : {}) };
      if (BigInt(reading.id) <= BigInt(expectedCursor)) {
        return { watchId, cursor: expectedCursor, pages: 0, readings: 0,
          latestReadingAt: reading.checkedAt || null, watchHealthy };
      }

      // SSE contains the full validated-by-identity source reading. One latest
      // check confirms that this event is still the newest live target; the
      // archive transaction below still commits its raw and parsed evidence
      // before the rule pipeline receives it.
      const latest = await this.client.latest(watchId, { windowSize: LIVE_OVERLAP_WINDOW_SIZE });
      if (latest?.watch?.id !== watchId || latest.watch.selector?.trim() !== PAYOUT_SELECTOR
          || typeof latest.cursor !== 'string' || !/^\d{1,19}$/.test(latest.cursor)
          || BigInt(latest.cursor) > 9223372036854775807n
          || (latest.latest && latest.latest.id !== latest.cursor)
          || (!latest.latest && latest.cursor !== '0')) {
        throw new OrbitApiError('INVALID_LATEST_SNAPSHOT');
      }
      if (BigInt(latest.cursor) < BigInt(reading.id)) {
        throw new OrbitApiError('STREAM_READING_NOT_ARCHIVED', { retryable: true });
      }

      let parsed = null;
      let validationError = null;
      try { parsed = parseOrbitReading(rawReading); }
      catch (error) {
        if (!(error instanceof OrbitApiError)) throw error;
        validationError = error.code;
      }
      let latestReading = null;
      if (latest.latest) {
        try { latestReading = parseOrbitReading(latest.latest); }
        catch (error) {
          if (!(error instanceof OrbitApiError)) throw error;
        }
      }
      const entry = { reading: rawReading, parsed, error: validationError };
      const page = await this.onPage({
        watchId,
        entries: [entry],
        state: { ...state, cursor: expectedCursor },
        latestReading,
        latestCursor: latest.cursor,
        watchHealthy: Boolean(watchHealthy && latest.watch.status === 'running'),
        hasMore: BigInt(latest.cursor) > BigInt(reading.id),
        pageIndex: 0,
      });
      if (!page || !Array.isArray(page.candidates) || !Array.isArray(page.deliveryModes)
          || page.candidates.length !== page.deliveryModes.length) {
        throw new OrbitApiError('INVALID_STREAM_PAGE_RESULT');
      }
      if (page.recoveryRequired) {
        throw new OrbitApiError('STREAM_ROUND_RECOVERY_REQUIRED', { retryable: true });
      }
      const pendingRounds = page.candidates.map((round, index) => ({
        ...round, delivery_mode: page.deliveryModes[index],
      }));
      const timingsByRoundId = {};
      if (Number.isFinite(receivedMonotonicMs)) {
        for (const round of pendingRounds) timingsByRoundId[String(round.round_id)] = {
          streamReceivedAt: receivedAt,
          streamReceivedMonotonicMs: receivedMonotonicMs,
        };
      }
      // The local archive transaction is the acceptance point. It atomically
      // stores the source cursor, overlap snapshot and candidate rounds before
      // any network publisher can observe them. If the process exits after
      // this commit, flushPending() replays the delivery intent on restart.
      await this.store.commitPage({
        watchId,
        expectedCursor,
        nextCursor: reading.id,
        entries: [entry],
        snapshot: { ...page.snapshot, sourceIdentity: state.snapshot.sourceIdentity },
        pendingRounds,
      });
      if (pendingRounds.length) {
        await this.onPagePrepared({ watchId, cursor: expectedCursor, pages: 1,
          readings: 1, pendingRounds, timingsByRoundId });
        await this.onPageCommitted({ watchId, cursor: reading.id, pages: 1,
          readings: 1, pendingRounds, timingsByRoundId });
        await this.store.ackPendingRounds(pendingRounds.map(round => String(round.round_id)));
      }
      return { watchId, cursor: reading.id, pages: 1, readings: 1,
        latestReadingAt: latest.latest?.checkedAt || null,
        watchHealthy: Boolean(watchHealthy && latest.watch.status === 'running'),
        watchCollectorStatus: watchHealthy ? 'running' : 'unknown',
        latestReadingTruncated: Boolean(latestReading?.truncated),
        latestReadingMatchCount: latestReading?.matchCount ?? null,
        latestReadingReturnedCount: latestReading?.returnedCount ?? null,
        latestReadingWindowSize: latestReading?.windowSize ?? null,
        latestReadingError: validationError,
      };
    } finally {
      this.inFlight = false;
    }
  }

  async sync() {
    if (this.inFlight) throw new OrbitApiError('SYNC_ALREADY_RUNNING');
    this.inFlight = true;
    try {
      let state = await this.store.getState();
      await this.flushPending({ watchId: state?.watchId || null, cursor: state?.cursor || '0', pages: 0, readings: 0 });
      // The saved watch identity is already known, so its latest snapshot can
      // be fetched alongside discovery. Recovery and archive commits remain
      // ordered; a changed watch still gets its own validated snapshot.
      const watchesRequest = this.client.listWatches();
      const savedLatest = state?.watchId
        ? this.client.latest(state.watchId, { windowSize: LIVE_OVERLAP_WINDOW_SIZE })
          .then(value => ({ value }), error => ({ error }))
        : null;
      const watches = await watchesRequest;
      if (!Array.isArray(watches?.watches)) throw new OrbitApiError('INVALID_WATCH_LIST');
      const preferredWatch = state?.watchId
        ? watches.watches.find(item => item?.id === state.watchId) : null;
      const savedIdentity = state?.snapshot?.sourceIdentity || sourceIdentity(preferredWatch) || null;
      // Chrome reconnects can replace the browser, tab and frame IDs. Recover
      // only to a healthy Watch with the same persisted frame/account identity.
      // Distinct live execution scopes remain ambiguous and fail closed.
      const recoveredWatch = state?.watchId && (!preferredWatch || !healthyWatch(preferredWatch))
        ? selectRecoveryWatch(watches.watches, savedIdentity, state.watchId) : null;
      // If Orbit has exactly one healthy payout Watch in the same Chrome tab
      // and game frame, allow a new source segment even when account/session
      // query parameters changed. The transition below seeds a fresh visible
      // baseline and marks the boundary as a gap; it never claims continuity
      // or emits the old Watch's rounds as new.
      const reanchoredWatch = state?.watchId && !healthyWatch(preferredWatch) && !recoveredWatch
        ? selectSameTabReanchorWatch(watches.watches, savedIdentity, state.watchId) : null;
      const selected = recoveredWatch || reanchoredWatch
        || discoverAviatorWatch({ watches: watches.watches }, state?.watchId || null);
      const prefetched = selected.id === state?.watchId && savedLatest ? await savedLatest : null;
      if (selected.id === state?.watchId && prefetched?.error) throw prefetched.error;
      const snapshot = selected.id === state?.watchId && prefetched
        ? prefetched.value : await this.client.latest(selected.id, { windowSize: LIVE_OVERLAP_WINDOW_SIZE });
      if (snapshot?.watch?.id !== selected.id || snapshot?.watch?.selector !== selected.selector
          || typeof snapshot.cursor !== 'string'
          || !/^\d{1,19}$/.test(snapshot.cursor)
          || BigInt(snapshot.cursor) > 9223372036854775807n
          || (snapshot.latest == null && snapshot.cursor !== '0')
          || (snapshot.latest != null && snapshot.latest.id !== snapshot.cursor)) {
        throw new OrbitApiError('INVALID_LATEST_SNAPSHOT');
      }
      let latestReading = null;
      let latestReadingError = null;
      if (snapshot.latest?.id !== undefined && snapshot.latest.id !== '0') {
        try { latestReading = parseOrbitReading(snapshot.latest); }
        catch (error) {
          if (!(error instanceof OrbitApiError)) throw error;
          latestReadingError = error.code;
        }
      }
      let recoveredFromWatchId = null;
      let watchRecoveryMode = null;
      if (state && state.watchId !== selected.id) {
        if (!healthyWatch(selected) || !latestReading || latestReading.visibleItems.length < 10
            || typeof this.store.transitionWatch !== 'function') throw new OrbitApiError('SAVED_WATCH_CHANGED');
        recoveredFromWatchId = state.watchId;
        watchRecoveryMode = reanchoredWatch?.id === selected.id ? 'SAME_TAB_NEW_SOURCE_GAP' : 'IDENTITY_VERIFIED';
        const baseline = withSourceIdentity({ version: 2,
          visibleMultipliers: visiblePayoutItems(latestReading.visibleItems).map(item => item.multiplier),
          bootstrapUntilCursor: snapshot.cursor, gapPending: true }, selected);
        await this.store.transitionWatch({ previousState: state, watchId: selected.id, snapshot: baseline });
        state = { watchId: selected.id, cursor: '0', snapshot: baseline };
      }
      let after = state?.cursor || '0';
      if (BigInt(after) > BigInt(snapshot.cursor)) throw new OrbitApiError('LATEST_CURSOR_REGRESSION');
      let lastSnapshot = state?.snapshot || null;
      let repairedReadings = 0;
      let unreconstructedReadings = Number(state?.snapshot?.unreconstructedReadings) || 0;
      let repairBaselineUnavailable = state?.snapshot?.repairBaselineUnavailable === true;
      if (state && BigInt(state.cursor) > 0n && !this.repairedWatchIds.has(selected.id)
          && typeof this.store.getInvalidReadings === 'function'
          && typeof this.store.getLatestVisibleSnapshotBefore === 'function'
          && typeof this.store.getArchivedReadings === 'function'
          && typeof this.store.updateRepairedReading === 'function') {
        const invalid = await this.store.getInvalidReadings(selected.id, state.cursor);
        let earliestReparseable = null;
        for (const row of invalid) {
          try {
            earliestReparseable = { ...row, parsed: parseOrbitReading(row.reading) };
            break;
          } catch (error) {
            if (!(error instanceof OrbitApiError)) throw error;
          }
        }
        if (!earliestReparseable) {
          this.repairedWatchIds.add(selected.id);
        } else {
          const baseline = await this.store.getLatestVisibleSnapshotBefore(selected.id, earliestReparseable.readingId);
          if (!baseline) {
            // Reparse archived values even when an earlier continuity baseline
            // is missing, but do not infer rounds across that unprovable range.
            repairBaselineUnavailable = true;
            const seedEntry = { reading: earliestReparseable.reading, parsed: earliestReparseable.parsed,
              wasInvalid: true };
            if (typeof this.store.commitRepairPage === 'function') {
              await this.store.commitRepairPage({ watchId: selected.id, entries: [seedEntry], pendingRounds: [] });
            } else {
              await this.store.updateRepairedReading(selected.id, seedEntry.reading.id, seedEntry.parsed);
            }
            repairedReadings += 1;
            unreconstructedReadings += 1;
            let repairAfter = seedEntry.reading.id;
            while (BigInt(repairAfter) < BigInt(state.cursor)) {
              const archived = await this.store.getArchivedReadings(selected.id, repairAfter, state.cursor, 100);
              if (!archived.length) break;
              const repairedEntries = [];
              for (const row of archived) {
                if (!row.validationError) continue;
                try { repairedEntries.push({ reading: row.reading, parsed: parseOrbitReading(row.reading) }); }
                catch (error) { if (!(error instanceof OrbitApiError)) throw error; }
              }
              if (typeof this.store.commitRepairPage === 'function') {
                await this.store.commitRepairPage({ watchId: selected.id, entries: repairedEntries, pendingRounds: [] });
              } else {
                for (const entry of repairedEntries) {
                  await this.store.updateRepairedReading(selected.id, entry.reading.id, entry.parsed);
                }
              }
              repairedReadings += repairedEntries.length;
              unreconstructedReadings += repairedEntries.length;
              repairAfter = archived.at(-1).readingId;
            }
            const priorSnapshot = state.snapshot && typeof state.snapshot === 'object'
              ? state.snapshot : { version: 2, visibleMultipliers: null };
            lastSnapshot = { ...priorSnapshot, gapPending: true, repairBaselineUnavailable: true,
              unreconstructedReadings };
          } else {
            let repairAfter = baseline.readingId;
            let repairSnapshot = { version: 2, visibleMultipliers: baseline.multipliers,
              ...(repairBaselineUnavailable ? { repairBaselineUnavailable: true, unreconstructedReadings } : {}) };
            let repairPageIndex = 0;
            while (BigInt(repairAfter) < BigInt(state.cursor)) {
              const archived = await this.store.getArchivedReadings(selected.id, repairAfter, state.cursor, 100);
              if (!archived.length) break;
              const entries = archived.map(row => {
                try { return { reading: row.reading, parsed: parseOrbitReading(row.reading), error: null,
                  wasInvalid: Boolean(row.validationError) }; }
                catch (error) {
                  if (!(error instanceof OrbitApiError)) throw error;
                  return { reading: row.reading, parsed: null, error: error.code, wasInvalid: Boolean(row.validationError) };
                }
              });
              const nextCursor = entries.at(-1).reading.id;
              const pageResult = await this.onPage({ watchId: selected.id, entries,
                state: { ...state, cursor: repairAfter, snapshot: repairSnapshot }, expectedCursor: repairAfter,
                latestReading, latestCursor: snapshot.cursor, watchHealthy: false,
                hasMore: BigInt(nextCursor) < BigInt(state.cursor), pageIndex: repairPageIndex });
              const normalized = this.normalizePageResult(pageResult);
              repairSnapshot = withSourceIdentity(normalized.snapshot, selected);
              const repairedEntries = entries.filter(entry => entry.wasInvalid && entry.parsed);
              if (typeof this.store.commitRepairPage === 'function') {
                await this.store.commitRepairPage({ watchId: selected.id, entries: repairedEntries,
                  pendingRounds: this.toPendingRounds(normalized) });
              } else {
                for (const entry of repairedEntries) {
                  await this.store.updateRepairedReading(selected.id, entry.reading.id, entry.parsed);
                }
              }
              repairedReadings += repairedEntries.length;
              await this.flushPending({ watchId: selected.id, cursor: repairAfter,
                pages: 0, readings: 0, repairedReadings });
              repairAfter = nextCursor;
              repairPageIndex += 1;
            }
            lastSnapshot = repairSnapshot;
          }
          this.repairedWatchIds.add(selected.id);
        }
      }
      let pages = 0;
      let readings = 0;
      while (true) {
        const page = validateHistoryPage(await this.client.history(selected.id, after, 100), selected.id, after);
        const entries = page.readings.map(reading => {
          try { return { reading, parsed: parseOrbitReading(reading), error: null }; }
          catch (error) {
            if (!(error instanceof OrbitApiError)) throw error;
            return { reading, parsed: null, error: error.code };
          }
        });
        const pageResult = await this.onPage({ watchId: selected.id, entries,
          state: { ...(state || {}), cursor: after, snapshot: lastSnapshot }, expectedCursor: after,
          latestReading, latestCursor: snapshot.cursor, watchHealthy: healthyWatch(selected), hasMore: page.hasMore,
          pageIndex: pages });
        const normalized = this.normalizePageResult(pageResult);
        normalized.snapshot = withSourceIdentity(normalized.snapshot, selected);
        // store.commitPage must atomically persist the raw readings and cursor.
        await this.store.commitPage({ watchId: selected.id, expectedCursor: after,
          nextCursor: page.nextCursor, entries, snapshot: normalized.snapshot,
          pendingRounds: this.toPendingRounds(normalized) });
        lastSnapshot = normalized.snapshot;
        pages++;
        readings += entries.length;
        after = page.nextCursor;
        await this.flushPending({ watchId: selected.id, cursor: after, pages, readings });
        if (!page.hasMore && BigInt(after) < BigInt(snapshot.cursor)) {
          throw new OrbitApiError('LATEST_HISTORY_GAP');
        }
        if (!page.hasMore) return { watchId: selected.id, cursor: after, pages, readings, repairedReadings,
          unreconstructedReadings, repairBaselineUnavailable,
          latestReadingAt: snapshot.latest?.checkedAt || null, watchUpdatedAt: selected.updatedAt || null,
          lastSourceCheckAt: selected.lastCheckedAt || null,
          latestReadingError,
          latestReadingTruncated: Boolean(latestReading?.truncated),
          latestReadingMatchCount: latestReading?.matchCount ?? null,
          latestReadingReturnedCount: latestReading?.returnedCount ?? null,
          latestReadingWindowSize: latestReading?.windowSize ?? null,
          watchState: selected.status,
          watchCollectorStatus: selected.collectorStatus || selected.status,
          watchHealthy: healthyWatch(selected),
          watchRecovered: recoveredFromWatchId !== null,
          previousWatchId: recoveredFromWatchId,
          watchRecoveryMode,
          watchSourceError: typeof selected.lastError === 'string' && selected.lastError.trim().length > 0,
          watchLastError: selected.lastError || null };
      }
    } finally {
      this.inFlight = false;
    }
  }

  normalizePageResult(result) {
    if (result && typeof result === 'object' && Object.prototype.hasOwnProperty.call(result, 'snapshot')) {
      const candidates = Array.isArray(result.candidates) ? result.candidates : [];
      const deliveryModes = result.deliveryModes || candidates.map(() => 'RECOVERED');
      if (!Array.isArray(deliveryModes) || deliveryModes.length !== candidates.length
          || deliveryModes.some(mode => mode !== 'LIVE' && mode !== 'RECOVERED')) {
        throw new OrbitApiError('INVALID_PAGE_DELIVERY');
      }
      return { snapshot: result.snapshot, candidates, deliveryModes,
        recoveryRequired: result.recoveryRequired === true };
    }
    return { snapshot: result, candidates: [], deliveryModes: [] };
  }

  toPendingRounds({ candidates, deliveryModes }) {
    return candidates.map((round, index) => ({ ...round, delivery_mode: deliveryModes[index] }));
  }

  async flushPending(progress) {
    if (typeof this.store.getPendingRounds !== 'function') {
      await this.onPageCommitted(progress);
      return;
    }
    let deliveredAny = false;
    while (true) {
      const pendingRounds = await this.store.getPendingRounds(500);
      if (!pendingRounds.length) break;
      await this.onPageCommitted({ ...progress, pendingRounds });
      if (typeof this.store.ackPendingRounds !== 'function') throw new OrbitApiError('ARCHIVE_PENDING_ACK_UNAVAILABLE');
      await this.store.ackPendingRounds(pendingRounds.map(round => round.round_id));
      deliveredAny = true;
    }
    if (!deliveredAny) {
      await this.onPageCommitted({ ...progress, pendingRounds: [] });
    }
  }
}

/** PostgreSQL archive, separate from the cursor that will govern round delivery. */
export class OrbitWatchArchive {
  constructor(pool) { this.pool = pool; }

  async initialize() {
    await this.pool.query(`CREATE TABLE IF NOT EXISTS orbit_watch_archive_state (
      singleton SMALLINT PRIMARY KEY CHECK (singleton=1), watch_id TEXT NOT NULL,
      cursor TEXT NOT NULL, snapshot JSONB, updated_at TIMESTAMPTZ NOT NULL DEFAULT now())`);
    await this.pool.query('ALTER TABLE orbit_watch_archive_state ADD COLUMN IF NOT EXISTS snapshot JSONB');
    await this.pool.query(`CREATE TABLE IF NOT EXISTS orbit_watch_archive_checkpoints (
      watch_id TEXT PRIMARY KEY, cursor TEXT NOT NULL, snapshot JSONB,
      updated_at TIMESTAMPTZ NOT NULL DEFAULT now())`);
    await this.pool.query(`CREATE TABLE IF NOT EXISTS orbit_watch_archive_readings (
      watch_id TEXT NOT NULL, reading_id TEXT NOT NULL, raw JSONB NOT NULL,
      parsed JSONB, validation_error TEXT, archived_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      PRIMARY KEY (watch_id, reading_id))`);
    await this.pool.query(`CREATE TABLE IF NOT EXISTS orbit_watch_archive_pending_rounds (
      sequence BIGSERIAL UNIQUE NOT NULL, round_id TEXT PRIMARY KEY, watch_id TEXT NOT NULL,
      round JSONB NOT NULL, delivery_mode TEXT NOT NULL CHECK (delivery_mode IN ('LIVE','RECOVERED')),
      created_at TIMESTAMPTZ NOT NULL DEFAULT now())`);
    await this.pool.query(`CREATE INDEX IF NOT EXISTS orbit_watch_archive_latest_valid_reading_idx
      ON orbit_watch_archive_readings (watch_id, (reading_id::numeric) DESC) WHERE parsed IS NOT NULL`);
    await this.pool.query(`CREATE INDEX IF NOT EXISTS orbit_watch_archive_reading_order_idx
      ON orbit_watch_archive_readings (watch_id, (reading_id::numeric))`);
    await this.pool.query(`CREATE INDEX IF NOT EXISTS orbit_watch_archive_invalid_reading_idx
      ON orbit_watch_archive_readings (watch_id, (reading_id::numeric)) WHERE validation_error IS NOT NULL`);
  }

  async getState() {
    const result = await this.pool.query('SELECT watch_id, cursor, snapshot FROM orbit_watch_archive_state WHERE singleton=1');
    return result.rows[0] ? { watchId: result.rows[0].watch_id, cursor: result.rows[0].cursor,
      snapshot: result.rows[0].snapshot || null } : null;
  }

  async getLatestVisibleSnapshot(watchId, cursor) {
    const result = await this.pool.query(`SELECT reading_id, parsed
      FROM orbit_watch_archive_readings
      WHERE watch_id=$1 AND reading_id::numeric <= $2::numeric AND parsed IS NOT NULL
      ORDER BY reading_id::numeric DESC LIMIT 1`, [watchId, cursor]);
    const parsed = result.rows[0]?.parsed;
    if (!parsed || !Array.isArray(parsed.visibleItems)) return null;
    return { readingId: result.rows[0].reading_id,
      multipliers: visiblePayoutItems(parsed.visibleItems).map(item => item.multiplier) };
  }

  async getLatestVisibleSnapshotBefore(watchId, readingId) {
    const result = await this.pool.query(`SELECT reading_id, parsed
      FROM orbit_watch_archive_readings
      WHERE watch_id=$1 AND reading_id::numeric < $2::numeric AND parsed IS NOT NULL
      ORDER BY reading_id::numeric DESC LIMIT 1`, [watchId, readingId]);
    const parsed = result.rows[0]?.parsed;
    if (!parsed || !Array.isArray(parsed.visibleItems)) return null;
    return { readingId: result.rows[0].reading_id,
      multipliers: visiblePayoutItems(parsed.visibleItems).map(item => item.multiplier) };
  }

  async getInvalidReadings(watchId, throughCursor) {
    const result = await this.pool.query(`SELECT reading_id,raw,validation_error
      FROM orbit_watch_archive_readings
      WHERE watch_id=$1 AND reading_id::numeric <= $2::numeric AND validation_error IS NOT NULL
      ORDER BY reading_id::numeric ASC`, [watchId, throughCursor]);
    return result.rows.map(row => ({ readingId: row.reading_id, reading: row.raw, validationError: row.validation_error }));
  }

  async getArchivedReadings(watchId, after, throughCursor, limit = 100) {
    const result = await this.pool.query(`SELECT reading_id,raw,validation_error
      FROM orbit_watch_archive_readings
      WHERE watch_id=$1 AND reading_id::numeric > $2::numeric AND reading_id::numeric <= $3::numeric
      ORDER BY reading_id::numeric ASC LIMIT $4`, [watchId, after, throughCursor, limit]);
    return result.rows.map(row => ({ readingId: row.reading_id, reading: row.raw, validationError: row.validation_error }));
  }

  async updateRepairedReading(watchId, readingId, parsed) {
    await this.pool.query(`UPDATE orbit_watch_archive_readings
      SET parsed=$3::jsonb,validation_error=NULL
      WHERE watch_id=$1 AND reading_id=$2 AND validation_error IS NOT NULL`,
    [watchId, readingId, JSON.stringify(parsed)]);
  }

  async getPendingRounds(limit = 500) {
    const result = await this.pool.query(`SELECT round_id,round,delivery_mode FROM orbit_watch_archive_pending_rounds
      ORDER BY sequence ASC LIMIT $1`, [Math.max(1, Math.min(5000, Number(limit) || 500))]);
    return result.rows.map(row => ({ ...row.round, round_id: row.round_id, delivery_mode: row.delivery_mode }));
  }

  async ackPendingRounds(roundIds) {
    if (!roundIds.length) return;
    await this.pool.query('DELETE FROM orbit_watch_archive_pending_rounds WHERE round_id = ANY($1::text[])', [roundIds.map(String)]);
  }

  async insertPendingRounds(conn, watchId, pendingRounds = []) {
    for (const round of pendingRounds) {
      const mode = round.delivery_mode || 'RECOVERED';
      if (!['LIVE', 'RECOVERED'].includes(mode) || !round.round_id) throw new OrbitApiError('INVALID_PENDING_ROUND');
      const raw = JSON.stringify(round);
      const inserted = await conn.query(`INSERT INTO orbit_watch_archive_pending_rounds(watch_id,round_id,round,delivery_mode)
        VALUES($1,$2,$3::jsonb,$4) ON CONFLICT(round_id) DO UPDATE SET round=orbit_watch_archive_pending_rounds.round
        WHERE orbit_watch_archive_pending_rounds.watch_id=EXCLUDED.watch_id
          AND orbit_watch_archive_pending_rounds.round=EXCLUDED.round
          AND orbit_watch_archive_pending_rounds.delivery_mode=EXCLUDED.delivery_mode RETURNING round_id`,
      [watchId, String(round.round_id), raw, mode]);
      if (!inserted.rowCount) throw new OrbitApiError('PENDING_ROUND_IDENTITY_CONFLICT');
    }
  }

  async commitRepairPage({ watchId, entries, pendingRounds = [] }) {
    const conn = await this.pool.connect();
    try {
      await conn.query('BEGIN');
      await conn.query("SELECT pg_advisory_xact_lock(hashtext('winner-predict:orbit-archive'))");
      for (const entry of entries) {
        const updated = await conn.query(`UPDATE orbit_watch_archive_readings
          SET parsed=$3::jsonb,validation_error=NULL
          WHERE watch_id=$1 AND reading_id=$2 AND validation_error IS NOT NULL RETURNING reading_id`,
        [watchId, entry.reading.id, JSON.stringify(entry.parsed)]);
        if (!updated.rowCount) throw new OrbitApiError('ARCHIVED_REPAIR_CONFLICT');
      }
      await this.insertPendingRounds(conn, watchId, pendingRounds);
      await conn.query('COMMIT');
    } catch (error) {
      await conn.query('ROLLBACK').catch(() => {});
      throw error;
    } finally { conn.release(); }
  }

  async transitionWatch({ previousState, watchId, snapshot }) {
    const conn = await this.pool.connect();
    try {
      await conn.query('BEGIN');
      await conn.query("SELECT pg_advisory_xact_lock(hashtext('winner-predict:orbit-archive'))");
      const current = await conn.query('SELECT watch_id,cursor FROM orbit_watch_archive_state WHERE singleton=1 FOR UPDATE');
      if (current.rows[0]?.watch_id !== previousState.watchId || current.rows[0]?.cursor !== previousState.cursor)
        throw new OrbitApiError('ARCHIVE_CURSOR_CONFLICT');
      await conn.query(`INSERT INTO orbit_watch_archive_checkpoints(watch_id,cursor,snapshot)
        VALUES ($1,$2,$3::jsonb) ON CONFLICT (watch_id) DO UPDATE
        SET cursor=EXCLUDED.cursor,snapshot=EXCLUDED.snapshot,updated_at=now()`,
      [previousState.watchId, previousState.cursor, JSON.stringify(previousState.snapshot)]);
      // A Watch may become healthy again after another Chrome reconnect. Its
      // checkpoint is historical, not a permanent lock on that source ID.
      await conn.query('DELETE FROM orbit_watch_archive_checkpoints WHERE watch_id=$1', [watchId]);
      await conn.query('UPDATE orbit_watch_archive_state SET watch_id=$1,cursor=$2,snapshot=$3::jsonb,updated_at=now() WHERE singleton=1',
        [watchId, '0', JSON.stringify(snapshot)]);
      await conn.query('COMMIT');
    } catch (error) {
      await conn.query('ROLLBACK').catch(() => {});
      throw error;
    } finally { conn.release(); }
  }

  async commitPage({ watchId, expectedCursor, nextCursor, entries, snapshot = null, pendingRounds = [] }) {
    const conn = await this.pool.connect();
    try {
      await conn.query('BEGIN');
      await conn.query("SELECT pg_advisory_xact_lock(hashtext('winner-predict:orbit-archive'))");
      const state = await conn.query('SELECT watch_id,cursor FROM orbit_watch_archive_state WHERE singleton=1 FOR UPDATE');
      if ((state.rows[0]?.watch_id || watchId) !== watchId
          || (state.rows[0]?.cursor || '0') !== expectedCursor) throw new OrbitApiError('ARCHIVE_CURSOR_CONFLICT');
      for (const entry of entries) {
        const raw = JSON.stringify(entry.reading);
        const existing = await conn.query(`INSERT INTO orbit_watch_archive_readings
          (watch_id,reading_id,raw,parsed,validation_error) VALUES ($1,$2,$3::jsonb,$4::jsonb,$5)
          ON CONFLICT (watch_id,reading_id) DO UPDATE SET raw=orbit_watch_archive_readings.raw
          WHERE orbit_watch_archive_readings.raw=EXCLUDED.raw RETURNING reading_id`,
        [watchId, entry.reading.id, raw, entry.parsed ? JSON.stringify(entry.parsed) : null, entry.error]);
        if (!existing.rowCount) throw new OrbitApiError('READING_IDENTITY_CONFLICT');
      }
      await conn.query(`INSERT INTO orbit_watch_archive_state(singleton,watch_id,cursor,snapshot)
        VALUES (1,$1,$2,$3::jsonb) ON CONFLICT(singleton) DO UPDATE
        SET cursor=EXCLUDED.cursor,snapshot=EXCLUDED.snapshot,updated_at=now()`,
      [watchId, nextCursor, snapshot == null ? null : JSON.stringify(snapshot)]);
      await this.insertPendingRounds(conn, watchId, pendingRounds);
      await conn.query('COMMIT');
    } catch (error) {
      await conn.query('ROLLBACK').catch(() => {});
      throw error;
    } finally { conn.release(); }
  }
}
