import assert from 'node:assert/strict';
import test from 'node:test';
import { getApiToken, getWebSocketAuthProtocols, getWebSocketUrl, logout, setApiToken } from '../src/auth.js';

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

test('WebSocket authentication does not place the API key in its URL', () => {
  globalThis.localStorage = storage();
  globalThis.sessionStorage = storage();
  globalThis.window = { location: { protocol: 'https:', host: 'winner.example' } };
  const token = 'api-key-with-private-value';
  setApiToken(token);
  const url = getWebSocketUrl();
  assert.equal(url, 'wss://winner.example/ws/live');
  assert.equal(url.includes(token), false);
  const protocol = getWebSocketAuthProtocols()[0];
  assert.match(protocol, /^auth\./);
  const encoded = protocol.slice('auth.'.length).replace(/-/g, '+').replace(/_/g, '/');
  const decoded = new TextDecoder().decode(Uint8Array.from(atob(encoded), (character) => character.charCodeAt(0)));
  assert.equal(decoded, token);
  logout();
});
