const DEFAULT_ROUND_EVENT_URL = 'http://127.0.0.1:8000/api/collector/round-observed';
const DEFAULT_ROUND_HISTORY_URL = 'http://127.0.0.1:8000/api/history?limit=100';

function directRound(round, { httpStartedAt = null } = {}) {
  const id = String(round.round_id);
  return {
    event_id: `aviator-round:${id}`,
    observation_id: String(round.observation_id || id),
    round_id: id,
    multiplier: Number(round.multiplier),
    timestamp: round.timestamp ?? null,
    observed_at: String(round.observed_at || round.timestamp || ''),
    platform_round_id: round.platform_round_id ?? null,
    platform_round_index: round.platform_round_index ?? null,
    platform_timestamp: round.platform_timestamp ?? null,
    round_index: Number(round.round_index ?? round.local_round_index),
    continuity_verified: round.continuity_verified === true,
    gap_before: round.gap_before === true,
    identity_confidence: round.identity_confidence ?? 'UNKNOWN',
    round_identity_type: round.round_identity_type ?? 'UNKNOWN',
    round_index_source: round.round_index_source ?? 'UNKNOWN',
    ...(round.source_metadata && typeof round.source_metadata === 'object'
      ? { source_metadata: round.source_metadata } : {}),
    ...(round.continuity_proof ? { continuity_proof: String(round.continuity_proof).slice(0, 160) } : {}),
    delivery_mode: round.delivery_mode || 'LIVE',
    local_appended_at: round.local_appended_at ?? null,
    collector_http_started_at: httpStartedAt,
    commit_acknowledged_at: round.commit_acknowledged_at ?? null,
    observation_to_commit_ms: round.observation_to_commit_ms !== null
      && round.observation_to_commit_ms !== undefined
      && Number.isFinite(Number(round.observation_to_commit_ms))
      ? Number(round.observation_to_commit_ms) : null,
    ...(round.game_phase ? { game_phase: round.game_phase } : {}),
  };
}

/**
 * Stage evidence read from the owned Winner page before writing its round row.
 * Backend failure is surfaced to the caller; it must not stop collection.
 */
export async function publishDirectRoundObservations(rounds, {
  url = process.env.COLLECTOR_BACKEND_ROUND_OBSERVED_URL || DEFAULT_ROUND_EVENT_URL,
  apiKey = process.env.API_KEY,
  collectorTiming = null,
  timeoutMs = Number(process.env.COLLECTOR_BACKEND_EVENT_TIMEOUT_MS) || 15000,
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
      body: JSON.stringify({ source: 'WINNER_DIRECT_PAGE_HISTORY',
        rounds: rounds.map(round => directRound(round, { httpStartedAt })),
        ...(Array.isArray(collectorTiming) ? { collector_timing: collectorTiming } : {}) }),
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
    result.collector_api_roundtrip_ms = Math.round(roundtripMs * 1000) / 1000;
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

/** Send a bounded batch from the collector's local event outbox and remove it only after acknowledgement. */
export async function drainDirectRoundEventOutbox(outbox, options = {}) {
  const batch = outbox.readAll().slice(0, Math.max(1, Number(options.batchSize) || 100));
  if (!batch.length) return { ok: true, accepted: 0, status: 'EMPTY' };
  const receipt = await publishDirectRoundObservations(batch, options);
  outbox.markObservedHandoff?.(batch.map(row => String(row.round_id)), receipt.api_published_at || new Date().toISOString());
  return receipt;
}

/** Submit committed-stage handoff only after the reused PostgreSQL pool acknowledged COMMIT. */
export async function publishCommittedRoundEvents(rounds, {
  url = process.env.COLLECTOR_BACKEND_ROUND_PERSISTED_URL || 'http://127.0.0.1:8000/api/collector/round-persisted',
  apiKey = process.env.API_KEY,
  timeoutMs = Number(process.env.COLLECTOR_BACKEND_EVENT_TIMEOUT_MS) || 15000,
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
        source: 'WINNER_CANONICAL_POSTGRESQL_COMMIT',
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

/**
 * Read-only diagnostic confirming that the latest committed round can be read
 * from the existing Round History API. This is called off the collector's
 * detection and PostgreSQL write path and never changes round state.
 */
export async function probeRoundHistoryVisibility(roundId, {
  url = process.env.COLLECTOR_BACKEND_ROUND_HISTORY_URL || DEFAULT_ROUND_HISTORY_URL,
  apiKey = process.env.API_KEY,
  timeoutMs = Number(process.env.COLLECTOR_ROUND_HISTORY_PROBE_TIMEOUT_MS) || 5000,
  fetchImpl = globalThis.fetch,
} = {}) {
  if (!roundId) return { available: false, status: 'ROUND_ID_UNKNOWN', checked_at: null };
  if (typeof fetchImpl !== 'function') throw Object.assign(new Error('global fetch is unavailable'), {
    category: 'CLIENT_UNAVAILABLE',
  });
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), Math.max(100, Number(timeoutMs) || 5000));
  timeout.unref?.();
  try {
    const headers = { accept: 'application/json' };
    if (apiKey) headers.authorization = `Bearer ${apiKey}`;
    const response = await fetchImpl(url, { method: 'GET', signal: controller.signal,
      headers });
    const body = await response.text();
    if (!response.ok) throw Object.assign(new Error(`Round History API returned HTTP ${response.status}`), {
      category: 'ROUND_HISTORY_API_REJECTED', httpStatus: response.status,
    });
    let payload;
    try { payload = JSON.parse(body); }
    catch (error) { throw Object.assign(new Error(`Round History API returned invalid JSON: ${error.message}`), {
      category: 'ROUND_HISTORY_API_INVALID_RESPONSE',
    }); }
    const rows = Array.isArray(payload?.rounds) ? payload.rounds : [];
    const available = rows.some(row => String(row?.round_id || '') === String(roundId));
    return { available, status: available ? 'CONFIRMED' : 'ROUND_NOT_IN_RESPONSE',
      checked_at: new Date().toISOString(), round_id: String(roundId) };
  } catch (error) {
    if (error?.category) throw error;
    if (error?.name === 'AbortError') throw Object.assign(new Error('Round History API visibility probe timed out'), {
      category: 'ROUND_HISTORY_API_TIMEOUT', timeoutMs,
    });
    throw Object.assign(new Error(`Round History API visibility probe failed: ${error.message}`), {
      category: 'ROUND_HISTORY_API_UNAVAILABLE',
    });
  } finally {
    clearTimeout(timeout);
  }
}

/** Publish one read-only game-phase transition from the collector-owned Aviator frame. */
export async function publishCollectorGamePhase(phase, sourceIdentity, {
  url = process.env.COLLECTOR_BACKEND_GAME_PHASE_URL || 'http://127.0.0.1:8000/api/collector/game-phase',
  apiKey = process.env.API_KEY,
  timeoutMs = Number(process.env.COLLECTOR_BACKEND_EVENT_TIMEOUT_MS) || 15000,
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
        source: 'OWNED_AVIATOR_GAME_FRAME_READ_ONLY',
        event_id: String(phase.event_id || [phase.phase, phase.observed_at, sourceIdentity.round_id].join(':')),
        source_round_id: String(sourceIdentity.round_id),
        source_round_index: Number(sourceIdentity.round_index),
        phase: String(phase.phase || 'UNKNOWN'),
        observed_at: String(phase.observed_at || ''),
        provider_round_id: phase.provider_round_id || null,
        canonical_target_round_id: phase.canonical_target_round_id || null,
        target_round_index: Number(sourceIdentity.round_index) + 1,
        betting_deadline: phase.betting_deadline || null,
        target_identity_verified: phase.target_identity_verified === true,
        target_started: typeof phase.target_started === 'boolean' ? phase.target_started : null,
        target_identity_proof: phase.target_identity_proof || null,
        evidence: phase.evidence || {},
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
