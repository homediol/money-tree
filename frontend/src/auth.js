// Runtime API credential storage. Authentication is enforced by FastAPI when
// API_KEY is configured; no usernames or passwords are shipped in the bundle.

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

export function getWebSocketUrl() {
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  const base = import.meta.env.VITE_WS_URL || `${protocol}//${window.location.host}/ws/live`;
  const token = getApiToken();
  return token ? `${base}${base.includes('?') ? '&' : '?'}token=${encodeURIComponent(token)}` : base;
}

export function isAuthenticated() {
  return Boolean(getApiToken());
}
