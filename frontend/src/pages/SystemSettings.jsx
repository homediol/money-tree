import { useCallback, useEffect, useState } from 'react';
import { KeyRound, Settings2 } from 'lucide-react';
import Card from '../components/Card.jsx';
import PageHeader from '../components/PageHeader.jsx';
import { getApiToken, setApiToken } from '../auth.js';
import { api } from '../services/api.js';

export default function SystemSettings() {
  const [token, setToken] = useState(getApiToken());
  const [health, setHealth] = useState(null);
  const [system, setSystem] = useState(null);
  const [error, setError] = useState('');

  const refresh = useCallback(async () => {
    try {
      const [healthResult, systemResult] = await Promise.all([
        api.get('/health'), api.get('/api/system/status'),
      ]);
      setHealth(healthResult.data);
      setSystem(systemResult.data);
      setError('');
    } catch {
      setError('Backend settings are unavailable.');
    }
  }, []);

  useEffect(() => { refresh(); }, [refresh]);

  function saveToken(event) {
    event.preventDefault();
    setApiToken(token);
    window.location.reload();
  }

  return <div className="space-y-5">
    <PageHeader eyebrow="Console access" title="API access & system status" icon={Settings2}
      description="Manage this browser's API token and inspect backend and collector health." />
    <Card title="API Security">
      <form onSubmit={saveToken} className="flex flex-col gap-3 sm:flex-row sm:items-end">
        <label className="block flex-1">
          <span className="flex items-center gap-2 text-sm text-zinc-400"><KeyRound size={15} />API token</span>
          <input type="password" autoComplete="off" value={token} onChange={(event) => setToken(event.target.value)}
            placeholder="Optional when public access is enabled" className="mt-2 w-full rounded border border-zinc-800 bg-zinc-950 px-3 py-2 text-zinc-300" />
        </label>
        <button type="submit" className="rounded bg-emerald-500 px-4 py-2 font-medium text-zinc-950 hover:bg-emerald-400">Save for this browser</button>
      </form>
      <p className="mt-3 text-sm text-zinc-500">API tokens are optional when the backend is configured for public access.</p>
    </Card>
    {error && <div role="status" className="rounded border border-amber-500/30 bg-amber-500/10 p-3 text-amber-200">{error}</div>}
    <section className="grid gap-4 sm:grid-cols-2">
      <Card title="Backend status"><div className="text-lg font-semibold">{health?.status || 'UNKNOWN'}</div><div className="mt-2 text-sm text-zinc-500">Database: {health?.database?.status || 'UNKNOWN'}</div></Card>
      <Card title="Round history"><div className="text-lg font-semibold">{system?.dataset?.valid_records ?? '—'} rounds</div><div className="mt-2 text-sm text-zinc-500">Source: {system?.dataset?.source || 'UNKNOWN'}</div><div className="mt-1 text-sm text-zinc-500">Latest round index: {system?.dataset?.last_round_index ?? 'UNKNOWN'}</div></Card>
    </section>
    <Card title="Execution safety"><div className="text-sm text-zinc-300">Real-money execution is disabled pending LIVE qualification. Mandatory target, panel, stake, acceptance and settlement checks remain fail-closed in Betting Executor.</div></Card>
  </div>;
}
