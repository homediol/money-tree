import Card from '../components/Card.jsx';
import Metric from '../components/Metric.jsx';
import { useLiveData } from '../hooks/useLiveData.js';

export default function SystemSettings() {
  const { stats, loading, error } = useLiveData();
  if (loading) return <div className="text-zinc-400">Loading settings...</div>;
  if (error) return <div className="text-rose-300">{error}</div>;
  return (
    <div className="space-y-5">
      <h1 className="text-3xl font-semibold">System Settings</h1>
      <section className="grid gap-4 md:grid-cols-3">
        <Card><Metric label="Target Multiplier" value={`${stats?.statistics?.target_multiplier || 2}x`} /></Card>
        <Card><Metric label="Data Source" value="roundhistory.json" /></Card>
        <Card><Metric label="Valid Records" value={stats?.data_quality?.valid_records ?? 0} /></Card>
      </section>
      <Card title="Configuration">
        <div className="grid gap-4 md:grid-cols-2">
          <label className="block">
            <span className="text-sm text-zinc-400">Target multiplier</span>
            <input readOnly value={stats?.statistics?.target_multiplier || 2} className="mt-2 w-full rounded border border-zinc-800 bg-zinc-950 px-3 py-2 text-zinc-300" />
          </label>
          <label className="block">
            <span className="text-sm text-zinc-400">Minimum sample size</span>
            <input readOnly value="30" className="mt-2 w-full rounded border border-zinc-800 bg-zinc-950 px-3 py-2 text-zinc-300" />
          </label>
          <label className="block">
            <span className="text-sm text-zinc-400">Signal threshold</span>
            <input readOnly value="60%" className="mt-2 w-full rounded border border-zinc-800 bg-zinc-950 px-3 py-2 text-zinc-300" />
          </label>
          <label className="block">
            <span className="text-sm text-zinc-400">Retraining</span>
            <input readOnly value="Manual, scheduled-ready, performance-trigger-ready" className="mt-2 w-full rounded border border-zinc-800 bg-zinc-950 px-3 py-2 text-zinc-300" />
          </label>
        </div>
        <p className="mt-4 text-sm text-zinc-500">Settings are currently read-only in the UI. Backend settings live in `backend/app/core/config.py`.</p>
      </Card>
      <Card title="Data Quality Report">
        <pre className="overflow-x-auto rounded bg-zinc-950 p-3 text-xs text-zinc-400">{JSON.stringify(stats?.data_quality, null, 2)}</pre>
      </Card>
    </div>
  );
}

