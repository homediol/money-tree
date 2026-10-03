import { useState } from 'react';
import Card from '../components/Card.jsx';
import Metric from '../components/Metric.jsx';
import { useLiveData } from '../hooks/useLiveData.js';
import { getApiToken, setApiToken } from '../auth.js';
import { KeyRound, Settings2 } from 'lucide-react';
import PageHeader from '../components/PageHeader.jsx';

export default function SystemSettings() {
  const { stats, system, loading, error } = useLiveData();
  const [token, setToken] = useState(getApiToken());

  function saveToken(event) {
    event.preventDefault();
    setApiToken(token);
    window.location.reload();
  }

  return (
    <div className="space-y-5">
      <PageHeader eyebrow="System configuration" title="Settings" icon={Settings2}
        description="Manage this browser’s API access and review read-only backend configuration and data quality." />
      <Card title="API Security">
        <form onSubmit={saveToken} className="flex flex-col gap-3 sm:flex-row sm:items-end">
          <label className="block flex-1">
            <span className="flex items-center gap-2 text-sm text-zinc-400"><KeyRound size={15} />API token</span>
            <input type="password" autoComplete="off" value={token} onChange={(event) => setToken(event.target.value)} placeholder="Required when backend API_KEY is configured" className="mt-2 w-full rounded border border-zinc-800 bg-zinc-950 px-3 py-2 text-zinc-300" />
          </label>
          <button type="submit" className="rounded bg-emerald-500 px-4 py-2 font-medium text-zinc-950 hover:bg-emerald-400">Save for this browser</button>
        </form>
        <p className="mt-3 text-sm text-zinc-500">Saved in this browser until you clear its site data or the backend token changes.</p>
      </Card>
      {loading && <div className="text-zinc-400">Loading settings...</div>}
      {error && <div className="rounded border border-rose-900 bg-rose-950/40 p-3 text-rose-300">{error}. If API security is enabled, save the matching token above.</div>}
      {!loading && !error && <>
      <section className="grid gap-4 md:grid-cols-3">
        <Card><Metric label="Target Multiplier" value={`${stats?.statistics?.target_multiplier || 2}x`} /></Card>
        <Card><Metric label="Data Source" value={system?.dataset?.source || 'Unavailable'} /></Card>
        <Card><Metric label="Valid Records" value={stats?.data_quality?.valid_records ?? 0} /></Card>
      </section>
      <Card title="Configuration">
        <div className="grid gap-4 md:grid-cols-2">
          <label className="block">
            <span className="text-sm text-zinc-400">Target multiplier</span>
            <input readOnly value={system?.configuration?.target_multiplier ?? 'Unavailable'} className="mt-2 w-full rounded border border-zinc-800 bg-zinc-950 px-3 py-2 text-zinc-300" />
          </label>
          <label className="block">
            <span className="text-sm text-zinc-400">Minimum sample size</span>
            <input readOnly value={system?.configuration?.minimum_sample_size ?? 'Unavailable'} className="mt-2 w-full rounded border border-zinc-800 bg-zinc-950 px-3 py-2 text-zinc-300" />
          </label>
          <label className="block">
            <span className="text-sm text-zinc-400">Signal threshold</span>
            <input readOnly value={system?.configuration?.signal_threshold != null ? `${Math.round(system.configuration.signal_threshold * 100)}%` : 'Unavailable'} className="mt-2 w-full rounded border border-zinc-800 bg-zinc-950 px-3 py-2 text-zinc-300" />
          </label>
          <label className="block">
            <span className="text-sm text-zinc-400">API security</span>
            <input readOnly value={system?.configuration?.api_security_enabled ? 'Enabled' : 'Disabled'} className="mt-2 w-full rounded border border-zinc-800 bg-zinc-950 px-3 py-2 text-zinc-300" />
          </label>
        </div>
        <p className="mt-4 text-sm text-zinc-500">Settings are currently read-only in the UI. Backend settings live in `backend/app/core/config.py`.</p>
      </Card>
      <Card title="Data Quality Report">
        <pre className="overflow-x-auto rounded bg-zinc-950 p-3 text-xs text-zinc-400">{JSON.stringify(stats?.data_quality, null, 2)}</pre>
      </Card>
      </>}
    </div>
  );
}
