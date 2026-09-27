import { useCallback, useEffect, useState } from 'react';
import Card from '../components/Card.jsx';
import SystemReadinessPanel from '../components/SystemReadinessPanel.jsx';
import { getWebSocketUrl } from '../auth.js';
import { api, getResultExecutions, getSystemReadiness } from '../services/api.js';
import { applyReadinessEvent, READINESS_EVENT } from '../services/readinessView.js';
import BettingModeControl from '../components/BettingModeControl.jsx';

const value = (v) => v == null || v === '' ? 'UNKNOWN' : String(v);
const tone = (state) => state === 'HEALTHY' || state === 'CONNECTED' || state === 'APPROVED' ? 'text-emerald-300' : state === 'PAUSED' || state === 'DEGRADED' || state === 'WAITING' || state === 'OFF' || state === 'IDLE' ? 'text-amber-300' : 'text-rose-300';

export default function SystemDashboard() {
  const [health, setHealth] = useState(null);
  const [status, setStatus] = useState(null);
  const [risk, setRisk] = useState(null);
  const [history, setHistory] = useState(null);
  const [executions, setExecutions] = useState([]);
  const [error, setError] = useState(null);
  const [operations, setOperations] = useState(null);
  const [readiness, setReadiness] = useState(null);
  const [readinessError, setReadinessError] = useState(null);
  const [stability, setStability] = useState(null);

  const refresh = useCallback(async () => {
    const coreRequest = Promise.all([
        api.get('/api/health/detailed'), api.get('/api/betting/status'),
        api.get('/api/risk/status'), api.get('/api/history/status'), getResultExecutions(5),
        api.get('/api/operations/status'),
      ]).then(([h, b, r, his, xs, op]) => {
        setHealth(h.data); setStatus(b.data.status); setRisk(r.data.status);
        setHistory(his.data); setExecutions(xs); setOperations(op.data);
        setError(null);
      }).catch((e) => {
        if (e?.code !== 'ERR_BACKEND_RECONNECTING') {
          setError(e?.response?.data?.detail || e.message || 'Dashboard unavailable');
        }
      });
    const readinessRequest = getSystemReadiness().then((snapshot) => {
      setReadiness(snapshot);
      setReadinessError(null);
    }).catch((e) => {
      setReadinessError(e?.response?.data?.status || e.message || 'Readiness status unavailable');
    });
    const stabilityRequest = api.get('/api/backend/stability').then(({ data }) => {
      setStability(data.stability);
    }).catch(() => {});
    await Promise.all([coreRequest, readinessRequest, stabilityRequest]);
  }, []);

  useEffect(() => {
    let ws = null;
    let reconnect = null;
    let closed = false;
    let attempts = 0;
    const connect = () => {
      if (closed) return;
      ws = new WebSocket(getWebSocketUrl());
      ws.onopen = () => { attempts = 0; };
      ws.onmessage = (message) => {
        try {
          const event = JSON.parse(message.data);
          if (event.type === READINESS_EVENT) {
            setReadiness(current => applyReadinessEvent(current, event));
            setReadinessError(null);
          }
          if (event.type === 'backend:stability' && event.stability) setStability(event.stability);
        } catch { /* malformed events are ignored; fallback refresh remains active */ }
      };
      ws.onerror = () => {};
      ws.onclose = () => {
        if (closed) return;
        reconnect = setTimeout(connect, Math.min(1000 * 2 ** attempts++, 15000));
      };
    };
    refresh();
    connect();
    const fallback = setInterval(refresh, 15000);
    return () => {
      closed = true;
      clearInterval(fallback);
      if (reconnect) clearTimeout(reconnect);
      if (ws) ws.close();
    };
  }, [refresh]);

  async function control(path) {
    try { await api.post(`/api/betting/${path}`); await refresh(); }
    catch (e) { setError(e?.response?.data?.message || e.message || 'Control rejected'); }
  }
  const components = health?.components || {};
  return <div className="space-y-5">
    <BettingModeControl />
    <div><h1 className="text-3xl font-semibold">System Operations Dashboard</h1>
      <p className="mt-1 text-sm text-zinc-400">Backend-authoritative pipeline status. UNKNOWN values are never estimated.</p></div>
    {error && <div className="rounded border border-rose-500/40 bg-rose-950/30 p-3 text-sm text-rose-200">{error}</div>}
    <SystemReadinessPanel readiness={readiness} error={readinessError} />
    <Card title="Backend Stability"><div className="grid gap-2 text-sm sm:grid-cols-2 xl:grid-cols-4">
      {[
        ['Backend Status', stability?.status], ['PID', stability?.pid],
        ['Parent PID', stability?.ppid], ['Uptime', stability?.uptime_seconds == null ? null : `${Math.floor(stability.uptime_seconds)}s`],
        ['Restart Count', stability?.restart_count], ['Unexpected Restarts', stability?.unexpected_restart_count],
        ['Last Restart', stability?.last_restart], ['Last Shutdown Reason', stability?.last_shutdown_reason],
        ['Collector Worker', stability?.collector?.status], ['Collector Browser', stability?.collector?.browser_connected && stability?.collector?.frame_connected ? 'CONNECTED' : 'WAITING'],
        ['ML Worker', stability?.ml_worker?.status], ['Database', stability?.database?.status],
        ['Execution Browser', stability?.browser?.status],
      ].map(([label, data]) => <div key={label} className="rounded bg-zinc-900 p-2"><div className="text-xs text-zinc-500">{label}</div><div className="break-all text-zinc-200">{value(data)}</div></div>)}
      {stability?.browser?.reason && <div className="text-xs text-amber-300 sm:col-span-2 xl:col-span-4">Execution browser: {stability.browser.reason}</div>}
    </div></Card>
    <section className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
      {[["System Health", health?.state], ["Mode", status?.mode || (status?.state === 'IDLE' ? 'OFF' : null)], ["History", history?.status], ["Risk", risk?.risk_status]].map(([k,v]) => <Card key={k}><div className="text-xs uppercase text-zinc-500">{k}</div><div className={`mt-1 text-xl font-semibold ${tone(v)}`}>{value(v)}</div>{k === 'Risk' && <div className="mt-1 text-xs text-zinc-500">{risk?.reason || 'No risk status received'}</div>}</Card>)}
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
      <Card title="Pipeline health"><div className="grid gap-2 sm:grid-cols-2">{Object.entries(components).map(([name,c]) => <div key={name} className="rounded bg-zinc-900 p-2 text-sm"><span className="text-zinc-400">{name === 'browser' ? 'execution browser' : name}</span><span className={`float-right ${tone(c.state)}`}>{c.state}</span>{c.last_error && <div className="mt-1 text-xs text-zinc-500">{c.last_error}</div>}</div>)}</div></Card>
      <Card title="Current execution"><div className="space-y-2 text-sm"><div>Bet: {value(status?.current_bet?.amount_bif)}</div><div>Round: {value(status?.current_round?.round_id)}</div><div>Balance: {value(status?.current_balance)} · Goal: {value(status?.goal_balance)}</div><div>Result: {value(status?.last_result?.status)}</div><div>P/L: {value(status?.running_pnl_bif)}</div></div></Card>
    </section>
    <Card title="Recent executions"><div className="space-y-2">{executions.length ? executions.map(x => <div key={x.execution_id} className="grid grid-cols-4 rounded bg-zinc-900 p-2 text-xs"><span>{x.target_round_id}</span><span>{x.status}</span><span>{x.result || 'UNKNOWN'}</span><span>{x.balance_status || 'UNKNOWN'}</span></div>) : <div className="text-sm text-zinc-500">No executions recorded.</div>}</div></Card>
  </div>;
}
