const DEFAULT_ROUND_EVENT_URL = 'http://127.0.0.1:8000/api/collector/round-observed';
const DEFAULT_BACKEND_EVENT_TIMEOUT_MS = 15_000;

function demoEnvironment() {
  if (process.env.WINNER_NONFINANCIAL_TEST_MODE !== 'true' || !process.env.AVIATOR_DEMO_URL) return null;
  try {
    const url = new URL(process.env.AVIATOR_DEMO_URL);
    if (url.protocol !== 'https:' || url.hostname.toLowerCase() !== 'aviator-demo.spribegaming.com'
        || url.searchParams.get('operator')?.toLowerCase() !== 'demo') return null;
    return { environment: 'DEMO', demo_origin: url.origin };
  } catch { return null; }
}

// Panel and betting-window evidence is useful only while it is fresh. It is
// also sent as a separate, live game-phase observation. A delayed durable
// round retry must not let stale UI evidence reject the whole round batch.
function freshGamePhase(round, now = Date.now()) {
  const phase = round?.game_phase;
  if (!phase || typeof phase !== 'object') return null;
  const timestamps = [phase.observed_at,
    ...(Array.isArray(phase.panel_observations)
      ? phase.panel_observations.map(panel => panel?.observed_at) : [])];
  if (timestamps.length < 3 || timestamps.some(value => {
    const observedAt = Date.parse(value || '');
    const ageMs = now - observedAt;
    return !Number.isFinite(observedAt) || ageMs < -30_000 || ageMs > 30_000;
  })) return null;
  return phase;
}

function directRound(round, { httpStartedAt = null } = {}) {
  const id = String(round.round_id);
  const gamePhase = freshGamePhase(round);
  const demo = demoEnvironment();
  return {
    event_id: `aviator-round:${id}`,
    observation_id: String(round.observation_id || id),
    round_id: id,
    multiplier: Number(round.multiplier),
    timestamp: round.timestamp ?? null,
    observed_at: String(round.observed_at || round.timestamp || ''),
    collector_observed_at: round.collector_observed_at || round.local_appended_at || null,
    platform_round_id: round.platform_round_id ?? null,
    platform_round_index: round.platform_round_index ?? null,
    platform_timestamp: round.platform_timestamp ?? null,
    round_index: Number(round.round_index ?? round.local_round_index),
    continuity_verified: round.continuity_verified === true,
    gap_before: round.gap_before === true,
    identity_confidence: round.identity_confidence ?? 'UNKNOWN',
    round_identity_type: round.round_identity_type ?? 'UNKNOWN',
    round_index_source: round.round_index_source ?? 'UNKNOWN',
    delivery_mode: demo ? 'DEMO' : round.delivery_mode || 'LIVE',
    ...(demo || {}),
    local_appended_at: round.local_appended_at ?? null,
    collector_http_started_at: httpStartedAt,
    commit_acknowledged_at: round.commit_acknowledged_at ?? null,
    observation_to_commit_ms: round.observation_to_commit_ms !== null
      && round.observation_to_commit_ms !== undefined
      && Number.isFinite(Number(round.observation_to_commit_ms))
      ? Number(round.observation_to_commit_ms) : null,
    ...(gamePhase ? { game_phase: gamePhase } : {}),
  };
}

/**
 * Stage evidence read from the owned Winner page before writing its round row.
 * Backend failure is surfaced to the caller; it must not stop collection.
 */
export async function publishDirectRoundObservations(rounds, {
  url = process.env.COLLECTOR_BACKEND_ROUND_OBSERVED_URL ||
    (demoEnvironment() ? 'http://127.0.0.1:8000/api/collector/demo-round-observed' : DEFAULT_ROUND_EVENT_URL),
  apiKey = process.env.API_KEY,
  timeoutMs = Number(process.env.COLLECTOR_BACKEND_EVENT_TIMEOUT_MS) || DEFAULT_BACKEND_EVENT_TIMEOUT_MS,
  fetchImpl = globalThis.fetch,
} = {}) {
  if (!Array.isArray(rounds) || rounds.length === 0) {
    return { ok: true, accepted: 0, status: 'NO_ROUNDS' };
  }
  if (typeof fetchImpl !== 'function') {
    throw Object.assign(new Error('global fetch is unavailable in this Node.js runtime'), {
      category: 'CLIENT_UNAVAILABLE',
    });
  }

  const controller = new AbortController();
  const effectiveTimeoutMs = Math.max(100, Number(timeoutMs) || 3000);
  const timeout = setTimeout(() => controller.abort(), effectiveTimeoutMs);
  timeout.unref?.();
  const headers = { 'content-type': 'application/json' };
  if (apiKey) headers.authorization = `Bearer ${apiKey}`;
  const httpStartedAt = new Date().toISOString();
  const requestStarted = performance.now();
  try {
    const response = await fetchImpl(url, {
      method: 'POST',
      headers,
      body: JSON.stringify({
        source: demoEnvironment() ? 'SPRIBE_DEMO_DIRECT_PAGE_HISTORY' : 'WINNER_DIRECT_PAGE_HISTORY',
        ...(demoEnvironment() || {}),
        rounds: rounds.map(round => directRound(round, { httpStartedAt })),
      }),
      signal: controller.signal,
    });
    const responseText = await response.text();
    let result = {};
    if (responseText) {
      try {
        result = JSON.parse(responseText);
      } catch (error) {
        throw Object.assign(new Error(`collector observation endpoint returned invalid JSON: ${error.message}`), {
          category: 'INVALID_BACKEND_RESPONSE',
          httpStatus: response.status,
        });
      }
    }
    if (!response.ok || result.ok !== true) {
      throw Object.assign(new Error(`collector observation endpoint rejected event with HTTP ${response.status}`), {
        category: 'BACKEND_REJECTED_EVENT',
        httpStatus: response.status,
        backendError: result.error || result.detail || null,
      });
    }
    if (Number(result.accepted) !== rounds.length) {
      throw Object.assign(new Error(`collector observation endpoint accepted ${result.accepted || 0}/${rounds.length} events`), {
        category: 'PARTIAL_EVENT_ACCEPTANCE',
      });
    }
    const roundtripMs = Math.max(0, performance.now() - requestStarted);
    const apiStageMs = Number(result.api_receipt_to_sse_publication_ms);
    result.collector_api_roundtrip_ms = Math.round(roundtripMs * 1000) / 1000;
    result.collector_to_api_one_way_estimate_ms = Number.isFinite(apiStageMs)
      ? Math.round(Math.max(0, (roundtripMs - apiStageMs) / 2) * 1000) / 1000 : null;
    result.collector_to_api_estimate_method = 'HTTP_RTT_MINUS_API_STAGE_DIVIDED_BY_TWO';
    return result;
  } catch (error) {
    if (error?.name === 'AbortError') {
      throw Object.assign(new Error(`collector observation endpoint timed out after ${effectiveTimeoutMs}ms`), {
        category: 'BACKEND_EVENT_TIMEOUT',
        timeoutMs: effectiveTimeoutMs,
        cause: error,
      });
    }
    if (error?.category) throw error;
    throw Object.assign(new Error(`collector observation endpoint unavailable: ${error.message}`), {
      category: 'BACKEND_EVENT_UNAVAILABLE',
      cause: error,
    });
  } finally {
    clearTimeout(timeout);
  }
}

/** Send a bounded process-local batch and remove it only after API acknowledgement. */
export async function drainDirectRoundEventOutbox(outbox, options = {}) {
  const batch = outbox.readAll().slice(0, Math.max(1, Number(options.batchSize) || 100));
  if (!batch.length) return { ok: true, accepted: 0, status: 'EMPTY' };
  const receipt = await publishDirectRoundObservations(batch, options);
  outbox.markObservedHandoff?.(batch.map(row => String(row.round_id)), receipt.api_published_at || new Date().toISOString());
  return receipt;
}

/** Submit committed-stage handoff only after the reused PostgreSQL pool acknowledged COMMIT. */
export async function publishCommittedRoundEvents(rounds, {
  url = process.env.COLLECTOR_BACKEND_ROUND_PERSISTED_URL ||
    (demoEnvironment() ? 'http://127.0.0.1:8000/api/collector/demo-round-persisted'
      : 'http://127.0.0.1:8000/api/collector/round-persisted'),
  apiKey = process.env.API_KEY,
  timeoutMs = Number(process.env.COLLECTOR_BACKEND_EVENT_TIMEOUT_MS) || DEFAULT_BACKEND_EVENT_TIMEOUT_MS,
  fetchImpl = globalThis.fetch,
} = {}) {
  if (!Array.isArray(rounds) || rounds.length === 0) return { ok: true, accepted: 0, events: [] };
  if (typeof fetchImpl !== 'function') throw Object.assign(new Error('global fetch is unavailable'), {
    category: 'CLIENT_UNAVAILABLE',
  });
  const controller = new AbortController();
  const effectiveTimeoutMs = Math.max(100, Number(timeoutMs) || 3000);
  const timeout = setTimeout(() => controller.abort(), effectiveTimeoutMs);
  timeout.unref?.();
  const headers = { 'content-type': 'application/json' };
  if (apiKey) headers.authorization = `Bearer ${apiKey}`;
  try {
    const response = await fetchImpl(url, {
      method: 'POST', headers,
      body: JSON.stringify({
        source: demoEnvironment() ? 'SPRIBE_DEMO_POSTGRESQL_COMMIT' : 'WINNER_CANONICAL_POSTGRESQL_COMMIT',
        ...(demoEnvironment() || {}),
        commit_acknowledged_at: rounds[0]?.commit_acknowledged_at ?? null,
        rounds: rounds.map(round => directRound(round)),
      }),
      signal: controller.signal,
    });
    const body = await response.text();
    let result = {};
    if (body) {
      try { result = JSON.parse(body); }
      catch (error) { throw Object.assign(new Error(`committed event endpoint returned invalid JSON: ${error.message}`), {
        category: 'INVALID_BACKEND_RESPONSE', httpStatus: response.status,
      }); }
    }
    if (!response.ok || result.ok !== true) {
      throw Object.assign(new Error(`committed event endpoint rejected event with HTTP ${response.status}`), {
        category: 'BACKEND_REJECTED_EVENT', httpStatus: response.status,
      });
    }
    if (Number(result.accepted) !== rounds.length) {
      throw Object.assign(new Error(`committed event endpoint accepted ${result.accepted || 0}/${rounds.length} events`), {
        category: 'PARTIAL_EVENT_ACCEPTANCE',
      });
    }
    return result;
  } catch (error) {
    if (error?.name === 'AbortError') throw Object.assign(
      new Error(`committed event endpoint timed out after ${effectiveTimeoutMs}ms`),
      { category: 'BACKEND_EVENT_TIMEOUT', timeoutMs: effectiveTimeoutMs, cause: error },
    );
    if (error?.category) throw error;
    throw Object.assign(new Error(`committed event endpoint unavailable: ${error.message}`), {
      category: 'BACKEND_EVENT_UNAVAILABLE', cause: error,
    });
  } finally {
    clearTimeout(timeout);
  }
}

/** Publish one read-only game-phase transition from the collector-owned Aviator frame. */
export async function publishCollectorGamePhase(phase, sourceIdentity, {
  url = process.env.COLLECTOR_BACKEND_GAME_PHASE_URL || 'http://127.0.0.1:8000/api/collector/game-phase',
  apiKey = process.env.API_KEY,
  timeoutMs = Number(process.env.COLLECTOR_BACKEND_EVENT_TIMEOUT_MS) || DEFAULT_BACKEND_EVENT_TIMEOUT_MS,
  fetchImpl = globalThis.fetch,
} = {}) {
  if (!phase || !sourceIdentity?.round_id || !Number.isSafeInteger(Number(sourceIdentity.round_index))) {
    return { ok: false, status: 'SOURCE_IDENTITY_UNAVAILABLE' };
  }
  if (typeof fetchImpl !== 'function') throw Object.assign(new Error('global fetch is unavailable'), {
    category: 'CLIENT_UNAVAILABLE',
  });
  const controller = new AbortController();
  const effectiveTimeoutMs = Math.max(100, Number(timeoutMs) || 3000);
  const timeout = setTimeout(() => controller.abort(), effectiveTimeoutMs);
  timeout.unref?.();
  const headers = { 'content-type': 'application/json' };
  if (apiKey) headers.authorization = 'Bearer ' + apiKey;
  try {
    const response = await fetchImpl(url, {
      method: 'POST', headers,
      body: JSON.stringify({
        source: demoEnvironment() ? 'SPRIBE_DEMO_GAME_FRAME_READ_ONLY' : 'OWNED_AVIATOR_GAME_FRAME_READ_ONLY',
        ...(demoEnvironment() || {}),
        event_id: String(phase.event_id || [phase.phase, phase.observed_at, sourceIdentity.round_id].join(':')),
        source_round_id: String(sourceIdentity.round_id),
        source_round_index: Number(sourceIdentity.round_index),
        phase: String(phase.phase || 'UNKNOWN'),
        round_phase: String(phase.round_phase || 'UNKNOWN'),
        observed_at: String(phase.observed_at || ''),
        evidence: phase.evidence || {},
        panel_observations: Array.isArray(phase.panel_observations)
          ? phase.panel_observations : [],
      }),
      signal: controller.signal,
    });
    const body = await response.text();
    let result = {};
    if (body) {
      try { result = JSON.parse(body); }
      catch (error) { throw Object.assign(new Error('game phase endpoint returned invalid JSON: ' + error.message), {
        category: 'INVALID_BACKEND_RESPONSE', httpStatus: response.status,
      }); }
    }
    if (!response.ok || result.ok !== true) {
      throw Object.assign(new Error('game phase endpoint rejected event with HTTP ' + response.status), {
        category: 'BACKEND_REJECTED_EVENT', httpStatus: response.status,
        backendError: result.error || result.detail || null,
      });
    }
    return result;
  } catch (error) {
    if (error?.name === 'AbortError') throw Object.assign(
      new Error('game phase endpoint timed out after ' + effectiveTimeoutMs + 'ms'),
      { category: 'BACKEND_EVENT_TIMEOUT', timeoutMs: effectiveTimeoutMs, cause: error },
    );
    if (error?.category) throw error;
    throw Object.assign(new Error('game phase endpoint unavailable: ' + error.message), {
      category: 'BACKEND_EVENT_UNAVAILABLE', cause: error,
    });
  } finally {
    clearTimeout(timeout);
  }
}
