import { useEffect, useState } from 'react';
import Card from '../components/Card.jsx';
import { api, getResultExecutions } from '../services/api.js';

const value = (v) => v == null || v === '' ? 'UNKNOWN' : String(v);
const tone = (state) => state === 'HEALTHY' || state === 'CONNECTED' ? 'text-emerald-300' : state === 'PAUSED' || state === 'DEGRADED' ? 'text-amber-300' : 'text-rose-300';

export default function SystemDashboard() {
  const [health, setHealth] = useState(null);
  const [status, setStatus] = useState(null);
  const [risk, setRisk] = useState(null);
  const [history, setHistory] = useState(null);
  const [executions, setExecutions] = useState([]);
  const [error, setError] = useState(null);
  const [operations, setOperations] = useState(null);

  async function refresh() {
    try {
      const [h, b, r, his, xs, op] = await Promise.all([
        api.get('/api/health/detailed'), api.get('/api/betting/status'),
        api.get('/api/risk/status'), api.get('/api/history/status'), getResultExecutions(5), api.get('/api/operations/status'),
      ]);
      setHealth(h.data); setStatus(b.data.status); setRisk(r.data.status); setHistory(his.data); setExecutions(xs); setOperations(op.data);
      setError(null);
    } catch (e) { setError(e?.response?.data?.detail || e.message || 'Dashboard unavailable'); }
  }
  useEffect(() => { refresh(); const id = setInterval(refresh, 3000); return () => clearInterval(id); }, []);

  async function control(path) {
    try { await api.post(`/api/betting/${path}`); await refresh(); }
    catch (e) { setError(e?.response?.data?.message || e.message || 'Control rejected'); }
  }
  const components = health?.components || {};
  return <div className="space-y-5">
    <div><h1 className="text-3xl font-semibold">System Operations Dashboard</h1>
      <p className="mt-1 text-sm text-zinc-400">Backend-authoritative pipeline status. UNKNOWN values are never estimated.</p></div>
    {error && <div className="rounded border border-rose-500/40 bg-rose-950/30 p-3 text-sm text-rose-200">{error}</div>}
    <section className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
      {[["System Health", health?.state], ["Mode", status?.mode], ["History", history?.status], ["Risk", risk?.risk_status]].map(([k,v]) => <Card key={k}><div className="text-xs uppercase text-zinc-500">{k}</div><div className={`mt-1 text-xl font-semibold ${tone(v)}`}>{value(v)}</div></Card>)}
    </section>
    <Card title="Long-running operations"><div className="grid gap-2 text-sm sm:grid-cols-3"><div>Uptime: {value(operations?.snapshot?.uptime_s)}s</div><div>Open executions: {value(operations?.snapshot?.open_executions)}</div><div>Incidents: {value(operations?.incident ? 1 : 0)}</div></div>{operations?.incident && <div className="mt-3 text-rose-300">Operations paused betting: {operations.incident.reasons.join('; ')}</div>}</Card>
    <Card title="Controls"><div className="flex flex-wrap gap-2">
      <button className="rounded bg-sky-600 px-3 py-2 text-sm" onClick={() => control('observe')}>Observe</button>
      <button className="rounded bg-emerald-600 px-3 py-2 text-sm" onClick={() => window.location.assign('/betting')}>Start Automatic</button>
      <button className="rounded bg-amber-600 px-3 py-2 text-sm" onClick={() => control('pause')}>Pause</button>
      <button className="rounded bg-emerald-700 px-3 py-2 text-sm" onClick={() => control('resume')}>Resume</button>
      <button className="rounded bg-zinc-700 px-3 py-2 text-sm" onClick={() => control('stop')}>Stop</button>
      <button className="rounded bg-rose-700 px-3 py-2 text-sm" onClick={() => control('emergency-stop')}>Emergency Stop</button>
    </div></Card>
    <section className="grid gap-5 xl:grid-cols-2">
      <Card title="Pipeline health"><div className="grid gap-2 sm:grid-cols-2">{Object.entries(components).map(([name,c]) => <div key={name} className="rounded bg-zinc-900 p-2 text-sm"><span className="text-zinc-400">{name}</span><span className={`float-right ${tone(c.state)}`}>{c.state}</span></div>)}</div></Card>
      <Card title="Current execution"><div className="space-y-2 text-sm"><div>Bet: {value(status?.current_bet?.amount_bif)}</div><div>Round: {value(status?.current_round?.round_id)}</div><div>Balance: {value(status?.current_balance)} · Goal: {value(status?.goal_balance)}</div><div>Result: {value(status?.last_result?.status)}</div><div>P/L: {value(status?.running_pnl_bif)}</div></div></Card>
    </section>
    <Card title="Recent executions"><div className="space-y-2">{executions.length ? executions.map(x => <div key={x.execution_id} className="grid grid-cols-4 rounded bg-zinc-900 p-2 text-xs"><span>{x.target_round_id}</span><span>{x.status}</span><span>{x.result || 'UNKNOWN'}</span><span>{x.balance_status || 'UNKNOWN'}</span></div>) : <div className="text-sm text-zinc-500">No executions recorded.</div>}</div></Card>
  </div>;
}
