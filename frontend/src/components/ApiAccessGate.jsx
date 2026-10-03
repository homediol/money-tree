import { useEffect, useState } from 'react';
import { KeyRound } from 'lucide-react';
import { API_UNAUTHORIZED_EVENT, getApiToken, logout, setApiToken } from '../auth.js';
import { useBackendConnection } from '../services/backendConnection.js';
import { getApiBaseUrl } from '../services/endpoints.js';
import { accessScreenState, hasUsableApplicationState } from '../services/accessPolicy.js';

async function checkToken(token) {
  const controller = new AbortController();
  const timeout = window.setTimeout(() => controller.abort(), 8000);
  try {
    const response = await fetch(`${getApiBaseUrl()}/api/system/status`, {
      method: 'GET',
      cache: 'no-store',
      signal: controller.signal,
      headers: token ? { Authorization: `Bearer ${token}` } : {},
    });
    if (response.status === 401) return false;
    if (!response.ok) throw new Error(`API access check failed (${response.status})`);
    const payload = await response.json().catch(() => null);
    if (payload?.ok !== true) throw new Error('API access check returned an unexpected response');
    return true;
  } finally {
    window.clearTimeout(timeout);
  }
}

export default function ApiAccessGate({ children }) {
  const connection = useBackendConnection();
  const [access, setAccess] = useState('checking');
  const [token, setToken] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [retryAttempt, setRetryAttempt] = useState(0);
  const online = connection.status === 'online' || connection.status === 'degraded';
  const authRequired = connection.health?.auth_required;
  const publicReadOnly = connection.health?.public_read_only === true;
  const screenState = accessScreenState(access, connection.state);

  useEffect(() => {
    let cancelled = false;
    let retryTimer = null;
    if (!online || access === 'granted') return () => { cancelled = true; };
    if (authRequired === false || publicReadOnly) {
      setAccess('granted');
      return () => { cancelled = true; };
    }
    const saved = getApiToken();
    if (authRequired === true && !saved) {
      setAccess('required');
      return () => { cancelled = true; };
    }
    setAccess('checking');
    checkToken(saved).then((valid) => {
      if (cancelled) return;
      if (!valid) logout();
      setAccess(valid ? 'granted' : 'required');
    }).catch((reason) => {
      if (!cancelled) {
        setError(reason.message || 'Could not check API access');
        setAccess('error');
        const delay = Math.min(1000 * (2 ** Math.max(0, retryAttempt)), 15000);
        retryTimer = window.setTimeout(() => setRetryAttempt((value) => value + 1), delay);
      }
    });
    return () => { cancelled = true; if (retryTimer) window.clearTimeout(retryTimer); };
  }, [online, authRequired, publicReadOnly, access, retryAttempt]);

  useEffect(() => {
    const unauthorized = () => {
      logout();
      if (publicReadOnly) return;
      setToken('');
      setError('The API token is no longer valid. Enter the current token.');
      setAccess('required');
    };
    window.addEventListener(API_UNAUTHORIZED_EVENT, unauthorized);
    return () => window.removeEventListener(API_UNAUTHORIZED_EVENT, unauthorized);
  }, [publicReadOnly]);

  async function submit(event) {
    event.preventDefault();
    const candidate = token.trim();
    if (!candidate) return;
    setBusy(true);
    setError('');
    try {
      if (!await checkToken(candidate)) {
        setError('Invalid API token. Check the backend API_KEY and try again.');
        return;
      }
      setApiToken(candidate);
      setToken('');
      setAccess('granted');
    } catch (reason) {
      setError(reason.message || 'Could not check API access');
    } finally {
      setBusy(false);
    }
  }

  // A verified session remains mounted during transient backend failures.
  // API requests still fail closed in the API client and real 401s revoke access below.
  if (hasUsableApplicationState(access)) return children;

  return (
    <main className="flex min-h-screen items-center justify-center bg-zinc-950 px-4 text-zinc-100">
      <section className="w-full max-w-md rounded-xl border border-zinc-800 bg-zinc-900 p-6 shadow-xl">
        <div className="mb-4 flex items-center gap-3 text-emerald-300"><KeyRound size={22} /><h1 className="text-xl font-semibold">Winner Predict access</h1></div>
        {screenState === 'INITIAL_CONNECT' || screenState === 'RECONNECTING' || screenState === 'DISCONNECTED' || access === 'checking' ? (
          <p className="text-sm text-zinc-400">{online ? 'Checking API access…' : connection.lastSuccessfulAt ? 'Backend reconnecting…' : connection.state === 'DISCONNECTED' ? 'Backend unavailable — retrying…' : 'Connecting to the backend…'}
            {connection.lastSuccessfulAt ? ` Last successful connection: ${new Date(connection.lastSuccessfulAt).toLocaleTimeString()}.` : ''}</p>
        ) : (
          <>
            <p className="mb-4 text-sm text-zinc-400">{screenState === 'ACCESS_CHECK_FAILED' ? 'API access could not be checked yet. Your saved token was kept; retry when the backend responds.' : 'Enter the API token configured for the backend to open this dashboard.'}</p>
            {access === 'error' && <button type="button" className="mb-3 rounded bg-zinc-700 px-3 py-2 text-sm" onClick={() => setRetryAttempt((value) => value + 1)}>Retry access check</button>}
            <form onSubmit={submit} className="space-y-3">
              <label className="block text-sm text-zinc-300" htmlFor="api-access-token">API token</label>
              <input id="api-access-token" type="password" autoComplete="off" value={token}
                onChange={(event) => setToken(event.target.value)}
                className="w-full rounded border border-zinc-700 bg-zinc-950 px-3 py-2 text-zinc-100" />
              {error && <p role="alert" className="text-sm text-rose-300">{error}</p>}
              <button type="submit" disabled={busy || !token.trim()}
                className="w-full rounded bg-emerald-500 px-4 py-2 font-semibold text-zinc-950 disabled:opacity-50">
                {busy ? 'Checking…' : 'Open dashboard'}
              </button>
            </form>
            <p className="mt-4 text-xs text-zinc-500">This browser remembers a valid token until you clear its site data or the backend token changes.</p>
          </>
        )}
      </section>
    </main>
  );
}
