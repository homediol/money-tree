import { OrbitApiError } from './OrbitDataWatchClient.js';
import crypto from 'node:crypto';

const cursor = value => typeof value === 'string' && /^\d{1,19}$/.test(value)
  && BigInt(value) <= 9223372036854775807n;

/** Stable round identity from the source reading and selector match; timestamps are metadata only. */
export function orbitRoundIdentity(watchId, readingId, itemIndex) {
  if (typeof watchId !== 'string' || !cursor(String(readingId))
      || !Number.isInteger(itemIndex) || itemIndex < 0) throw new OrbitApiError('INVALID_ROUND_SOURCE_IDENTITY');
  return `orbit-${crypto.createHash('sha256').update(`${watchId}:${readingId}:${itemIndex}`).digest('hex').slice(0, 24)}`;
}

/** Reuse the saved payout watch, or discover one unambiguous payout watch before state exists. */
export function discoverAviatorWatch(response, preferredWatchId = null) {
  if (!response || !Array.isArray(response.watches)) throw new OrbitApiError('INVALID_WATCH_LIST');
  const matches = response.watches.filter(item => item?.selector?.trim() === '.payouts-block .payout');
  let watch;
  if (preferredWatchId) {
    watch = response.watches.find(item => item?.id === preferredWatchId);
    if (!watch) throw new OrbitApiError('SAVED_WATCH_NOT_FOUND');
    if (watch.selector?.trim() !== '.payouts-block .payout') throw new OrbitApiError('SAVED_WATCH_SELECTOR_MISMATCH');
  } else {
    if (matches.length > 1) throw new OrbitApiError('MULTIPLE_AVIATOR_WATCHES');
    watch = matches[0];
  }
  if (!watch) throw new OrbitApiError('AVIATOR_WATCH_UNAVAILABLE');
  if (typeof watch.id !== 'string'
      || !/^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(watch.id)) {
    throw new OrbitApiError('INVALID_WATCH_ID');
  }
  if (watch.status !== 'running') throw new OrbitApiError('WATCH_NOT_RUNNING');
  if (typeof watch.selector !== 'string' || !watch.selector.trim()) throw new OrbitApiError('INVALID_WATCH');
  if (watch.selector.trim() !== '.payouts-block .payout') throw new OrbitApiError('WATCH_SELECTOR_MISMATCH');
  return watch;
}

/** Validate a saved snapshot; retain the original reading separately for audit. */
export function parseOrbitReading(reading) {
  if (!reading || !cursor(reading.id) || reading.id === '0'
      || typeof reading.checkedAt !== 'string' || !Number.isFinite(Date.parse(reading.checkedAt))
      || !/(?:Z|[+-]\d{2}:\d{2})$/i.test(reading.checkedAt)
      || !Number.isInteger(reading.count) || reading.count < 0 || !Array.isArray(reading.items)
      || reading.items.length > reading.count) throw new OrbitApiError('INVALID_READING');
  const multipliers = [];
  const visibleItems = [];
  const selectorItems = [];
  for (let i = 0; i < reading.items.length; i++) {
    const item = reading.items[i];
    if (!item || item.index !== i || typeof item.visible !== 'boolean' || typeof item.value !== 'string') {
      throw new OrbitApiError('INVALID_READING_ITEM');
    }
    const match = /^\s*((?:\d{1,3}(?:,\d{3})+|\d{1,7}))(?:\.(\d{1,2}))?\s*[xX×]\s*$/.exec(item.value);
    if (!match) throw new OrbitApiError('INVALID_MULTIPLIER');
    const number = Number(`${match[1].replaceAll(',', '')}.${(match[2] || '').padEnd(2, '0')}`);
    if (!Number.isFinite(number) || number < 1 || number > 1000000) throw new OrbitApiError('INVALID_MULTIPLIER');
    const historyContainer = typeof item.historyContainer === 'string' && item.historyContainer.trim()
      ? item.historyContainer.trim() : typeof reading.historyContainer === 'string' && reading.historyContainer.trim()
        ? reading.historyContainer.trim() : null;
    const parsedItem = { index: item.index, rawValue: item.value, multiplier: number, visible: item.visible,
      ...(historyContainer ? { historyContainer: historyContainer.slice(0, 200) } : {}),
      ...Object.fromEntries(['mutationObservedAt', 'extractionStartedAt', 'extractedAt']
        .filter(key => typeof item[key] === 'string' && Number.isFinite(Date.parse(item[key])))
        .map(key => [key, item[key]])) };
    selectorItems.push(parsedItem);
    multipliers.push(number);
    if (item.visible) visibleItems.push(parsedItem);
  }
  return { id: reading.id, checkedAt: reading.checkedAt, multipliers, selectorItems,
    visibleItems, returnedCount: reading.items.length, matchCount: reading.count,
    projection: reading.projection || null, windowSize: reading.windowSize || null,
    truncated: reading.count > reading.items.length
      && !(reading.projection === 'LATEST_OVERLAP_WINDOW'
        && Number.isInteger(reading.windowSize) && reading.windowSize >= 11 && reading.windowSize <= 50
        && reading.items.length === Math.min(reading.count, reading.windowSize)) };
}

/**
 * Resolve visible payout nodes using their source-provided container identity.
 * Some Orbit pages render the same payout list in multiple visible responsive
 * containers. They are safe to treat as one logical list only when every
 * structurally identified candidate has the same ordered values and visibility
 * at every position. The result is a consensus projection, not a choice of the
 * first/last DOM container. Any disagreement remains ambiguous.
 */
export function resolveVisiblePayoutItems(items) {
  if (!Array.isArray(items) || items.length === 0) {
    return { items: [], status: "EMPTY", diagnostics: {
      reason: "NO_VISIBLE_ITEMS", candidate_count: 0, accepted_containers: [], rejected_containers: [],
    } };
  }

  const groups = new Map();
  for (const item of items) {
    const container = typeof item?.historyContainer === "string" ? item.historyContainer.trim() : "";
    const key = container || "<UNSCOPED>";
    const group = groups.get(key) || [];
    group.push(item);
    groups.set(key, group);
  }

  const names = [...groups.keys()];
  const diagnosticsFor = (reason, accepted = [], rejected = names) => ({
    reason,
    candidate_count: names.length,
    accepted_containers: accepted.map(name => ({
      container: name, item_count: groups.get(name).length,
      first_index: groups.get(name)[0]?.index ?? null,
      last_index: groups.get(name).at(-1)?.index ?? null,
    })),
    rejected_containers: rejected.map(name => ({
      container: name, item_count: groups.get(name).length,
      first_index: groups.get(name)[0]?.index ?? null,
      last_index: groups.get(name).at(-1)?.index ?? null,
    })),
  });

  if (names.length === 1) {
    if (names[0] === "<UNSCOPED>") {
      // A long flat selector result with no parent identity may contain more
      // than one responsive/history list. Only small projections (the Orbit
      // latest-overlap endpoint) are accepted without container structure.
      if (items.length > 19) return { items: [], status: "AMBIGUOUS",
        diagnostics: diagnosticsFor("HISTORY_CONTAINER_IDENTITY_MISSING") };
    }
    return { items: [...items], status: "SELECTED_SINGLE",
      diagnostics: diagnosticsFor("SINGLE_STRUCTURAL_CONTAINER", names, []) };
  }

  const candidateGroups = names.map(name => groups.get(name));
  const allCompleteAndVisible = candidateGroups.every(group => group.length >= 10
    && group.every(item => item?.visible === true)
    && group.every((item, index) => index === 0 || Number(item.index) === Number(group[index - 1].index) + 1));
  const expected = candidateGroups[0];
  const equivalent = allCompleteAndVisible && candidateGroups.slice(1).every(group =>
    group.length === expected.length && group.every((item, index) =>
      Number(item.multiplier) === Number(expected[index].multiplier)
      && item.rawValue === expected[index].rawValue
      && item.visible === expected[index].visible));

  if (!equivalent) {
    const mismatchPositions = candidateGroups.slice(1).map((group, offset) => {
      const firstDifference = group.findIndex((item, index) => !expected[index]
        || Number(item.multiplier) !== Number(expected[index].multiplier)
        || item.rawValue !== expected[index].rawValue || item.visible !== expected[index].visible);
      return { container: names[offset + 1], first_difference_index: firstDifference < 0
        ? (group.length === expected.length ? null : Math.min(group.length, expected.length))
        : firstDifference };
    });
    return { items: [], status: "AMBIGUOUS", diagnostics: {
      ...diagnosticsFor(allCompleteAndVisible ? "CONTAINER_CONTENTS_DISAGREE"
        : "CONTAINER_STRUCTURE_OR_VISIBILITY_INCOMPLETE"), mismatch_positions: mismatchPositions,
    } };
  }

  // Build the common ordered projection across every equivalent container.
  // Normalized item indices refer to positions in that shared list, so mirrored
  // DOM copies cannot create a second UID or shift the canonical item identity.
  const consensus = expected.map((item, index) => ({
    ...item, index, sourceItemIndex: item.index,
    historyContainer: names.join(" | "),
    historyContainers: [...names],
  }));
  return { items: consensus, status: "CONSENSUS_MIRRORS", diagnostics: {
    ...diagnosticsFor("IDENTICAL_VISIBLE_ORDERED_CONTAINERS", names, []),
    logical_item_count: consensus.length,
    policy: "ALL_CANDIDATES_MUST_MATCH_BY_POSITION",
  } };
}

/** Compatibility projection for existing reconciliation code and tests. */
export function visiblePayoutItems(items) {
  return resolveVisiblePayoutItems(items).items;
}

export function validateHistoryPage(page, watchId, after) {
  if (!page || page.watchId !== watchId || page.selector !== '.payouts-block .payout'
      || !Array.isArray(page.readings)
      || !cursor(after) || !cursor(page.nextCursor) || typeof page.hasMore !== 'boolean') {
    throw new OrbitApiError('INVALID_HISTORY_PAGE');
  }
  let previous = BigInt(after);
  for (const reading of page.readings) {
    if (!cursor(reading?.id) || BigInt(reading.id) <= previous) throw new OrbitApiError('INVALID_HISTORY_ORDER');
    previous = BigInt(reading.id);
  }
  if (BigInt(page.nextCursor) !== previous || (page.hasMore && !page.readings.length)) {
    throw new OrbitApiError('INVALID_HISTORY_CURSOR');
  }
  return page;
}

/** Infer changes only if the preserved suffix gives one unambiguous overlap. */
export function snapshotDelta(previous, current, minimumOverlap = 10) {
  if (!Array.isArray(previous) || !Array.isArray(current) || !previous.length || !current.length) {
    return { status: 'EMPTY', rounds: [] };
  }
  if (previous.length === current.length && previous.every((value, i) => value === current[i])) {
    return { status: 'UNCHANGED', rounds: [] };
  }
  const shifts = [];
  for (let shift = 1; shift <= current.length - minimumOverlap; shift++) {
    const overlap = Math.min(previous.length, current.length - shift);
    if (overlap >= minimumOverlap && previous.slice(0, overlap).every((v, i) => v === current[shift + i])) shifts.push(shift);
  }
  if (shifts.length !== 1) return { status: 'CONTINUITY_UNVERIFIED', rounds: [] };
  return { status: 'VERIFIED', rounds: current.slice(0, shifts[0]).reverse() };
}
