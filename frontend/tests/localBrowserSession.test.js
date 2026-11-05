import assert from 'node:assert/strict';
import test from 'node:test';
import { bootstrapLocalBrowserSession } from '../src/services/localBrowserSession.js';

test('local dashboard bootstrap sends no API key and accepts only the HttpOnly session handshake', async () => {
  let request;
  const result = await bootstrapLocalBrowserSession('/api/auth/local-browser-session', async (url, options) => {
    request = { url, options };
    return { ok: true, status: 200, json: async () => ({ ok: true, session: 'LOCAL_BROWSER' }) };
  });

  assert.equal(result.session, 'LOCAL_BROWSER');
  assert.equal(request.url, '/api/auth/local-browser-session');
  assert.equal(request.options.method, 'POST');
  assert.equal(request.options.credentials, 'include');
  assert.equal(request.options.cache, 'no-store');
  assert.deepEqual(request.options.headers, { Accept: 'application/json' });
  assert.equal('Authorization' in request.options.headers, false);
});

test('local dashboard bootstrap rejects a remote or denied session response', async () => {
  await assert.rejects(bootstrapLocalBrowserSession('/api/auth/local-browser-session', async () => ({
    ok: false,
    status: 403,
    json: async () => ({ ok: false, error: 'local_browser_session_not_allowed' }),
  })), /local_browser_session_not_allowed/);
});
