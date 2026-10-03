import assert from 'node:assert/strict';
import test from 'node:test';
import { build } from 'esbuild';
import { runInNewContext } from 'node:vm';
import { hasUsableApplicationState } from '../src/services/accessPolicy.js';

async function loadConnectionStore(fetchImpl) {
  const result = await build({
    entryPoints: ['src/services/backendConnection.js'],
    bundle: true,
    platform: 'node',
    format: 'cjs',
    external: ['react'],
    write: false,
    define: { 'import.meta.env.VITE_API_BASE_URL': '"http://backend.test"' },
  });
  const context = {
    AbortController,
    CustomEvent: class CustomEvent { constructor(type, options) { this.type = type; this.detail = options?.detail; } },
    Date,
    console: { info() {}, warn() {} },
    require: (specifier) => {
      if (specifier === 'react') return { useSyncExternalStore() {} };
      throw new Error(`Unexpected external dependency: ${specifier}`);
    },
    exports: {},
    fetch: fetchImpl,
    setTimeout,
    clearTimeout,
    window: { setTimeout, clearTimeout, addEventListener() {}, removeEventListener() {}, dispatchEvent() {} },
  };
  context.module = { exports: context.exports };
  context.globalThis = context;
  runInNewContext(result.outputFiles[0].text, context);
  return context.module.exports;
}

const healthyResponse = () => ({ json: async () => ({
  ok: true,
  service: 'winner-predict-backend',
  backend: { status: 'ok', instance_id: 'test-instance' },
}) });

test('verified app access survives health timeout and reconnect without clearing last success', async () => {
  let fail = false;
  const store = await loadConnectionStore(async () => {
    if (fail) throw new TypeError('controlled transport outage');
    return healthyResponse();
  });
  assert.equal(store.REQUEST_TIMEOUT_MS, 15000);

  await store.checkBackend();
  const connected = store.getBackendSnapshot();
  assert.equal(connected.state, 'CONNECTED');
  assert.ok(connected.lastSuccessfulAt);

  fail = true;
  await store.checkBackend();
  const disconnected = store.getBackendSnapshot();
  assert.equal(disconnected.state, 'RECONNECTING');
  assert.equal(disconnected.lastSuccessfulAt, connected.lastSuccessfulAt);
  assert.equal(hasUsableApplicationState('granted'), true);

  fail = false;
  await store.checkBackend();
  const restored = store.getBackendSnapshot();
  assert.equal(restored.state, 'CONNECTED');
  assert.ok(restored.lastSuccessfulAt >= connected.lastSuccessfulAt);
  assert.equal(hasUsableApplicationState('granted'), true);
});

test('prolonged initial failure is distinct from recovery after an established connection', async () => {
  const store = await loadConnectionStore(async () => { throw new TypeError('backend unavailable'); });
  for (let attempt = 1; attempt <= 5; attempt += 1) {
    await store.checkBackend();
    assert.equal(store.getBackendSnapshot().attempt, attempt);
    if (attempt === 1) assert.equal(store.getBackendSnapshot().state, 'INITIAL_CONNECT');
  }
  assert.equal(store.getBackendSnapshot().state, 'DISCONNECTED');
});
