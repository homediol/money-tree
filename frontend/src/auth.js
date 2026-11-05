// Runtime owner credential storage for private APIs. Public committed-round
// reads need no token; remote private routes fail closed when API_KEY is absent.

const TOKEN_KEY = 'winner_predict_api_token';
export const API_UNAUTHORIZED_EVENT = 'winner:api-unauthorized';

export function setApiToken(token) {
  const value = token?.trim();
  if (value) localStorage.setItem(TOKEN_KEY, value);
  else localStorage.removeItem(TOKEN_KEY);
  sessionStorage.removeItem(TOKEN_KEY);
}

// Compatibility for the legacy login screen: its password field is treated
// as the API token. The username is intentionally not used for authorization.
export function login(_username, token) {
  if (!token?.trim()) return false;
  setApiToken(token);
  return true;
}

export function logout() {
  localStorage.removeItem(TOKEN_KEY);
  sessionStorage.removeItem(TOKEN_KEY);
}

export function getApiToken() {
  const saved = localStorage.getItem(TOKEN_KEY);
  if (saved) return saved;
  // Carry a token from an already-open tab into persistent browser storage.
  const previous = sessionStorage.getItem(TOKEN_KEY);
  if (previous) setApiToken(previous);
  return previous || '';
}

export function getSession() {
  return isAuthenticated() ? { authenticated: true } : null;
}

export function getWebSocketAuthProtocols(token = getApiToken()) {
  if (!token) return [];
  const bytes = new TextEncoder().encode(token);
  let binary = '';
  bytes.forEach((byte) => { binary += String.fromCharCode(byte); });
  const encoded = btoa(binary).replace(/=/g, '').replace(/\+/g, '-').replace(/\//g, '_');
  return [`auth.${encoded}`];
}

export function getWebSocketUrl() {
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  return import.meta.env?.VITE_WS_URL || `${protocol}//${window.location.host}/ws/live`;
}

export function isAuthenticated() {
  return Boolean(getApiToken());
}
