// Runtime API credential storage. Authentication is enforced by FastAPI when
// API_KEY is configured; no usernames or passwords are shipped in the bundle.

const TOKEN_KEY = 'winner_predict_api_token';

export function setApiToken(token) {
  const value = token?.trim();
  if (value) sessionStorage.setItem(TOKEN_KEY, value);
  else sessionStorage.removeItem(TOKEN_KEY);
}

// Compatibility for the legacy login screen: its password field is treated
// as the API token. The username is intentionally not used for authorization.
export function login(_username, token) {
  if (!token?.trim()) return false;
  setApiToken(token);
  return true;
}

export function logout() {
  sessionStorage.removeItem(TOKEN_KEY);
}

export function getApiToken() {
  return sessionStorage.getItem(TOKEN_KEY) || '';
}

export function getSession() {
  return isAuthenticated() ? { authenticated: true } : null;
}

export function getWebSocketUrl() {
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  const base = import.meta.env.VITE_WS_URL || `${protocol}//${window.location.hostname}:8000/ws/live`;
  const token = getApiToken();
  return token ? `${base}${base.includes('?') ? '&' : '?'}token=${encodeURIComponent(token)}` : base;
}

export function isAuthenticated() {
  return Boolean(getApiToken());
}
