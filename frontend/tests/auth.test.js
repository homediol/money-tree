import assert from 'node:assert/strict';
import test from 'node:test';
import { getApiToken, logout, setApiToken } from '../src/auth.js';

function storage() {
  const values = new Map();
  return {
    getItem: (key) => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, String(value)),
    removeItem: (key) => values.delete(key),
  };
}

test('saved API token survives a new tab and logout removes it', () => {
  globalThis.localStorage = storage();
  globalThis.sessionStorage = storage();
  setApiToken('  example-token  ');
  assert.equal(getApiToken(), 'example-token');
  globalThis.sessionStorage = storage();
  assert.equal(getApiToken(), 'example-token');
  logout();
  assert.equal(getApiToken(), '');
});

test('an existing tab token migrates to persistent storage', () => {
  globalThis.localStorage = storage();
  globalThis.sessionStorage = storage();
  sessionStorage.setItem('winner_predict_api_token', 'old-session-token');
  assert.equal(getApiToken(), 'old-session-token');
  globalThis.sessionStorage = storage();
  assert.equal(getApiToken(), 'old-session-token');
  logout();
});
