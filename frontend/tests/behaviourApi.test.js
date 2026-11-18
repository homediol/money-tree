import test from 'node:test';
import assert from 'node:assert/strict';

import { behaviourApiPrefix, behaviourDelete, behaviourGet, behaviourPut } from '../src/services/behaviourApi.js';

test('Behaviour API uses its versioned namespace without Winner Predict credentials', async () => {
  const originalFetch = globalThis.fetch;
  let request;
  globalThis.fetch = async (url, options) => {
    request = { url, options };
    return { ok: true, json: async () => ({ state: 'READY' }) };
  };
  try {
    const result = await behaviourGet('/health');
    assert.equal(result.state, 'READY');
    assert.equal(behaviourApiPrefix, '/api/v1/behaviour');
    assert.equal(request.url, '/api/v1/behaviour/health');
    assert.equal(request.options.method, 'GET');
    assert.equal(request.options.credentials, 'omit');
    assert.equal(request.options.cache, 'no-store');
    assert.deepEqual(request.options.headers, { Accept: 'application/json' });
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test('Behaviour API reports backend error codes without hiding them', async () => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async () => ({
    ok: false, status: 503, json: async () => ({ detail: { code: 'BEHAVIOUR_DATABASE_UNCONFIGURED' } }),
  });
  try {
    await assert.rejects(behaviourGet('/overview'), /BEHAVIOUR_DATABASE_UNCONFIGURED/);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test('Behaviour API deletes only through its versioned audit endpoint', async () => {
  const originalFetch = globalThis.fetch;
  let request;
  globalThis.fetch = async (url, options) => {
    request = { url, options };
    return { ok: true, json: async () => ({ deleted: true }) };
  };
  try {
    const result = await behaviourDelete('/hybrid/high-dominance-2x/batch%3Aone');
    assert.deepEqual(result, { deleted: true });
    assert.equal(request.url, '/api/v1/behaviour/hybrid/high-dominance-2x/batch%3Aone');
    assert.equal(request.options.method, 'DELETE');
    assert.equal(request.options.credentials, 'omit');
    assert.equal(request.options.cache, 'no-store');
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test('Behaviour API sends a JSON body for selected and bulk audit deletion', async () => {
  const originalFetch = globalThis.fetch;
  let request;
  globalThis.fetch = async (url, options) => {
    request = { url, options };
    return { ok: true, json: async () => ({ deleted_count: 2 }) };
  };
  try {
    const result = await behaviourDelete('/hybrid/high-dominance-2x/bulk-delete', {
      phase: 'CONTINUATION', batch_ids: ['part2-a', 'part2-b'],
    });
    assert.deepEqual(result, { deleted_count: 2 });
    assert.equal(request.url, '/api/v1/behaviour/hybrid/high-dominance-2x/bulk-delete');
    assert.equal(request.options.method, 'DELETE');
    assert.equal(request.options.headers['Content-Type'], 'application/json');
    assert.deepEqual(JSON.parse(request.options.body), {
      phase: 'CONTINUATION', batch_ids: ['part2-a', 'part2-b'],
    });
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test('Behaviour API preserves active-series deletion errors', async () => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async () => ({
    ok: false, status: 409,
    json: async () => ({ detail: { code: 'HIGH_DOMINANCE_SERIES_ACTIVE', message: 'Batches in a live series cannot be deleted.' } }),
  });
  try {
    await assert.rejects(behaviourDelete('/hybrid/high-dominance-2x/batch-one'), /Batches in a live series cannot be deleted/);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test('Behaviour API persists HIGH series configuration through the isolated service', async () => {
  const originalFetch = globalThis.fetch;
  let request;
  globalThis.fetch = async (url, options) => {
    request = { url, options };
    return { ok: true, json: async () => ({ config: { pause_limit_verified_rounds: 20 } }) };
  };
  try {
    const result = await behaviourPut('/hybrid/high-series/config', { pause_limit_verified_rounds: 20 });
    assert.equal(result.config.pause_limit_verified_rounds, 20);
    assert.equal(request.options.method, 'PUT');
    assert.equal(request.options.credentials, 'omit');
    assert.equal(request.options.headers['Content-Type'], 'application/json');
    assert.deepEqual(JSON.parse(request.options.body), { pause_limit_verified_rounds: 20 });
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test('Behaviour strategy save reload keeps the persisted version and pending values', async () => {
  const originalFetch = globalThis.fetch;
  const initial = {
    strategy_mode: 'PART1_AND_PART2',
    configuration_version: 1,
    part1: { enabled: true, max_counted_rounds: 5, net_win_limit_enabled: true, net_win_target: 2 },
    part2: { enabled: true, max_counted_rounds_per_batch: 5, net_win_limit_enabled: true,
      net_win_target: 3, pause_limit_verified_rounds: 10 },
    cooldown_verified_rounds: 10, on_win_limit_reached: 'WAIT_FOR_NEXT_FRESH_TRIGGER', shadow_only: true,
  };
  let saved = structuredClone(initial);
  globalThis.fetch = async (url, options) => {
    if (options.method === 'PUT') {
      const submitted = JSON.parse(options.body);
      saved = { ...submitted, configuration_version: 2 };
      return { ok: true, json: async () => ({ config: saved, configuration_status: 'SAVED' }) };
    }
    return { ok: true, json: async () => ({ high_dominance_2x: { series_machine: { config: saved } } }) };
  };
  try {
    const edited = { ...initial, part1: { ...initial.part1, net_win_target: 1 } };
    const result = await behaviourPut('/hybrid/high-series/config', edited);
    assert.equal(result.configuration_status, 'SAVED');
    const reloaded = await behaviourGet('/hybrid');
    const config = reloaded.high_dominance_2x.series_machine.config;
    assert.equal(config.configuration_version, 2);
    assert.equal(config.part1.net_win_target, 1);
    assert.equal(config.part2.net_win_target, 3);
  } finally {
    globalThis.fetch = originalFetch;
  }
});
