import { useCallback, useEffect, useRef, useState } from 'react';
import { RefreshCw, Signal } from 'lucide-react';
import { fetchLatestAction } from '../services/liveSignalAction.js';
const POLL_INTERVAL_MS = 5000;

export default function LiveSignalActionMonitor({ diagnostics }) {
  const [result, setResult] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const requestInFlight = useRef(false);
  const lifecycle = useRef(0);

  const refresh = useCallback(async () => {
    if (requestInFlight.current) return;
    requestInFlight.current = true;
    const currentLifecycle = lifecycle.current;
    setLoading(true);
    setError(null);

    try {
      const nextResult = await fetchLatestAction();
      if (currentLifecycle !== lifecycle.current) return;
      setResult(nextResult);
      setError(null);
    } catch (requestError) {
      if (currentLifecycle !== lifecycle.current) return;
      setResult(null);
      setError(requestError instanceof Error ? requestError.message : 'Request failed');
    } finally {
      requestInFlight.current = false;
      if (currentLifecycle === lifecycle.current) setLoading(false);
    }
  }, []);

  useEffect(() => {
    lifecycle.current += 1;
    void refresh();
    const timer = window.setInterval(() => { void refresh(); }, POLL_INTERVAL_MS);
    return () => {
      lifecycle.current += 1;
      window.clearInterval(timer);
    };
  }, [refresh]);

  const status = result?.action || (loading ? 'CHECKING' : error ? 'UNAVAILABLE' : 'NO SIGNAL');
  const statusClass = status === 'OK'
    ? 'border-emerald-500/30 bg-emerald-500/10 text-emerald-200'
    : status === 'WAIT'
        ? 'border-amber-500/30 bg-amber-500/10 text-amber-200'
        : status === 'UNAVAILABLE'
          ? 'border-rose-500/30 bg-rose-500/10 text-rose-200'
          : 'border-zinc-700 bg-zinc-900 text-zinc-300';

  return <section aria-labelledby="live-signal-action-heading" className="rounded-xl border border-zinc-800 bg-zinc-900/50 p-4">
    <div className="flex flex-wrap items-start justify-between gap-3">
      <div>
        <div className="flex items-center gap-2">
          <Signal size={17} className="text-emerald-300" />
          <h3 id="live-signal-action-heading" className="font-medium">Live BET signal action</h3>
        </div>
        <p className="mt-1 text-sm text-zinc-500">Public action endpoint · refreshes every 5 seconds</p>
      </div>
      <button
        type="button"
        onClick={() => { void refresh(); }}
        disabled={loading}
        aria-label="Refresh live action"
        className="inline-flex items-center gap-2 rounded-lg border border-zinc-700 px-3 py-2 text-sm hover:bg-zinc-800 disabled:cursor-wait disabled:opacity-50"
      >
        <RefreshCw size={15} className={loading ? 'animate-spin' : ''} />Refresh
      </button>
    </div>

    <div className="mt-4 grid gap-4 sm:grid-cols-2" aria-live="polite">
      <div>
        <div className="text-xs uppercase tracking-wide text-zinc-500">Action</div>
        <div className="mt-2">
          <span className={`inline-flex rounded-full border px-3 py-1.5 text-sm font-semibold tracking-wide ${statusClass}`}>
            {status}
          </span>
        </div>
      </div>
      <div>
        <div className="text-xs uppercase tracking-wide text-zinc-500">Target round index</div>
        <div className="mt-2 font-mono text-lg text-zinc-100">
          {result?.index || (loading ? 'Checking…' : error ? 'Unavailable' : '—')}
        </div>
      </div>
    </div>

    {error && <p role="status" className="mt-3 text-sm text-rose-200">The live action could not be loaded ({error}). It will retry automatically.</p>}
    {result?.action === 'WAIT' && diagnostics?.action === 'WAIT' && diagnostics.wait_reason
      && <p className="mt-3 text-sm text-amber-200">WAIT reason: {diagnostics.wait_reason}</p>}
    <p className="mt-3 text-xs text-zinc-500">OK reflects a published BET signal; it does not guarantee the round outcome.</p>
  </section>;
}
