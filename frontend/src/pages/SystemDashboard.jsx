import { useCallback, useEffect, useRef, useState } from 'react';
import { Activity, AlertTriangle, CircleDollarSign, Database, ShieldCheck, Wifi } from 'lucide-react';
import Card from '../components/Card.jsx';
import SystemReadinessPanel from '../components/SystemReadinessPanel.jsx';
import { getWebSocketUrl } from '../auth.js';
import { api, getResultExecutions, getSystemReadiness } from '../services/api.js';
import { applyReadinessEvent, READINESS_EVENT } from '../services/readinessView.js';
import BettingModeControl from '../components/BettingModeControl.jsx';

const known = (v) => v != null && v !== '';
const value = (v) => known(v) ? String(v) : 'Unknown';
const statusLabels = {
  HEALTHY: 'Healthy', CONNECTED: 'Connected', APPROVED: 'Approved', READY: 'Ready',
  PAUSED: 'Paused', DEGRADED: 'Degraded', WAITING: 'Waiting', OFF: 'Off', IDLE: 'Idle',
  UNKNOWN: 'Unknown', STARTING: 'Starting', ERROR: 'Error', STOPPED: 'Stopped',
  BLOCKED: 'Blocked', NOT_READY: 'Not ready', SHADOW_REALISTIC: 'Shadow mode', LIVE_REAL: 'Live mode',
  SIMULATION: 'Simulation', REAL: 'Live', ACTIVE: 'Active', RUNNING: 'Running',
};
const readable = (v) => known(v) ? (statusLabels[String(v).toUpperCase()] || String(v).toLowerCase().replaceAll('_', ' ')) : 'Unknown';
const tone = (state) => ['HEALTHY', 'CONNECTED', 'APPROVED', 'READY', 'ACTIVE', 'RUNNING'].includes(state)
  ? 'text-emerald-300' : ['PAUSED', 'DEGRADED', 'WAITING', 'OFF', 'IDLE', 'STARTING'].includes(state)
    ? 'text-amber-300' : ['ERROR', 'BLOCKED', 'NOT_READY', 'STOPPED'].includes(state) ? 'text-rose-300' : 'text-zinc-300';
const money = (amount) => typeof amount === 'number' && Number.isFinite(amount)
  ? `${amount.toLocaleString()} BIF` : 'Unknown';

function SummaryCard({ icon: Icon, label, value: metric, detail, color = 'sky' }) {
  const colors = {
    sky: 'border-sky-500/20 from-sky-500/10 text-sky-300',
    emerald: 'border-emerald-500/20 from-emerald-500/10 text-emerald-300',
    amber: 'border-amber-500/20 from-amber-500/10 text-amber-300',
    violet: 'border-violet-500/20 from-violet-500/10 text-violet-300',
  };
  return <article className={`rounded-xl border bg-gradient-to-br ${colors[color]} to-zinc-900/70 p-4 shadow-lg shadow-black/10`}>
    <div className="flex items-center gap-2 text-xs font-semibold uppercase tracking-wide text-zinc-400"><Icon size={16} className={colors[color].split(' ')[2]} />{label}</div>
    <div className="mt-3 text-xl font-bold capitalize text-zinc-100">{metric}</div>
    {detail && <p className="mt-1 text-xs leading-5 text-zinc-400">{detail}</p>}
  </article>;
}

function InfoTile({ label, value: data, detail }) {
  return <div className="rounded-lg border border-zinc-800 bg-zinc-950/70 p-3">
    <div className="text-xs text-zinc-500">{label}</div>
    <div className="mt-1 break-words text-sm font-medium capitalize text-zinc-200">{value(data)}</div>
    {detail && <div className="mt-1 break-words text-xs text-zinc-500">{detail}</div>}
  </div>;
}

export default function SystemDashboard() {
  const [health, setHealth] = useState(null);
  const [status, setStatus] = useState(null);
  const [browserObservation, setBrowserObservation] = useState(null);
  const [risk, setRisk] = useState(null);
  const [history, setHistory] = useState(null);
  const [executions, setExecutions] = useState([]);
  const [error, setError] = useState(null);
  const [operations, setOperations] = useState(null);
  const [readiness, setReadiness] = useState(null);
  const [readinessError, setReadinessError] = useState(null);
  const [stability, setStability] = useState(null);
  const [lastUpdated, setLastUpdated] = useState(null);
  const refreshInFlight = useRef(false);

  const refresh = useCallback(async () => {
    if (refreshInFlight.current) return;
    refreshInFlight.current = true;
    const coreRequest = Promise.all([
      api.get('/api/health/detailed'), api.get('/api/betting/status'),
      api.get('/api/risk/status'), api.get('/api/history/status'), getResultExecutions(5),
      api.get('/api/operations/status'),
    ]).then(([h, b, r, his, xs, op]) => {
      setHealth(h.data); setStatus(b.data.status); setRisk(r.data.status);
      setHistory(his.data); setExecutions(xs); setOperations(op.data);
      setLastUpdated(Date.now()); setError(null);
    }).catch((e) => {
      if (e?.code !== 'ERR_BACKEND_RECONNECTING') {
        setError(e?.response?.data?.detail || e.message || 'Dashboard data is temporarily unavailable');
      }
    });
    const readinessRequest = getSystemReadiness().then((snapshot) => {
      setReadiness(snapshot); setReadinessError(null);
    }).catch((e) => setReadinessError(e?.response?.data?.status || e.message || 'Readiness information is unavailable'));
    const stabilityRequest = api.get('/api/backend/stability').then(({ data }) => setStability(data.stability)).catch(() => {});
    const browserRequest = api.get('/api/betting/browser', { params: { include_snapshot: true } })
      .then(({ data }) => setBrowserObservation({ ...data, observedAt: Date.now() }))
      .catch(() => setBrowserObservation({ ok: false, observedAt: Date.now() }));
    try { await Promise.all([coreRequest, readinessRequest, stabilityRequest, browserRequest]); }
    finally { refreshInFlight.current = false; }
  }, []);

  useEffect(() => {
    let ws = null;
    let reconnect = null;
    let closed = false;
    let attempts = 0;
    let hasConnected = false;
    const connect = () => {
      if (closed) return;
      ws = new WebSocket(getWebSocketUrl());
      ws.onopen = () => {
        const isReconnect = hasConnected; hasConnected = true; attempts = 0;
        if (isReconnect) refresh();
      };
      ws.onmessage = (message) => {
        try {
          const event = JSON.parse(message.data);
          if (event.type === READINESS_EVENT) { setReadiness(current => applyReadinessEvent(current, event)); setReadinessError(null); }
          if (event.type === 'backend:stability' && event.stability) setStability(event.stability);
        } catch { /* Ignore malformed event; REST refresh remains available. */ }
      };
      ws.onerror = () => {};
      ws.onclose = () => {
        if (!closed) reconnect = setTimeout(connect, Math.min(1000 * 2 ** attempts++, 15000));
      };
    };
    refresh(); connect();
    const fallback = setInterval(refresh, 15000);
    return () => { closed = true; clearInterval(fallback); if (reconnect) clearTimeout(reconnect); if (ws) ws.close(); };
  }, [refresh]);

  async function control(path) {
    try { await api.post(`/api/betting/${path}`); await refresh(); }
    catch (e) { setError(e?.response?.data?.message || e.message || 'Control was not accepted'); }
  }

  const components = health?.components || {};
  const mode = status?.mode || (status?.state === 'IDLE' ? 'OFF' : null);
  const savedRounds = history?.count ?? history?.valid_records ?? history?.valid_rounds ?? history?.round_count;
  const browserSnapshot = browserObservation?.snapshot;
  const browserBalanceVerified = browserObservation?.browser?.connected === true
    && browserSnapshot?.ok === true
    && typeof browserSnapshot.balance === 'number'
    && Number.isFinite(browserSnapshot.balance)
    && browserSnapshot.balance >= 0;
  const sessionBalanceVerified = typeof status?.current_balance === 'number'
    && Number.isFinite(status.current_balance);
  const balanceVerified = browserBalanceVerified || sessionBalanceVerified;
  const historyState = history?.status;
  const runningPnl = status?.running_pnl_bif;
  const profit = status?.profit;
  const target = status?.goal_balance;
  const current = browserBalanceVerified ? browserSnapshot.balance : status?.current_balance;
  const balanceObservedAt = browserBalanceVerified
    ? browserObservation.observedAt
    : status?.last_balance_observed_at;
  const balanceSource = browserBalanceVerified ? 'connected browser' : 'betting session';
  const browserReliability = stability?.browser_reliability;
  const browserSnapshotFresh = Boolean(browserReliability && !browserReliability.snapshot_stale);
  const lastRoundAt = browserReliability?.collector?.last_round_timestamp;
  const lastRecovery = browserReliability?.last_recovery;
  const start = status?.starting_balance;
  const goalProgress = typeof current === 'number' && typeof start === 'number' && typeof target === 'number' && target > start
    ? Math.min(100, Math.max(0, ((current - start) / (target - start)) * 100)) : null;

  return <div className="space-y-5">
    <header className="rounded-2xl border border-zinc-800 bg-gradient-to-br from-zinc-900 via-zinc-900 to-sky-950/30 p-5 shadow-xl sm:p-6">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div><div className="text-xs font-semibold uppercase tracking-[0.18em] text-sky-300">Operations overview</div>
          <h1 className="mt-1 text-2xl font-bold tracking-tight text-white sm:text-3xl">Dashboard</h1>
          <p className="mt-2 max-w-3xl text-sm leading-6 text-zinc-400">A clear view of collected rounds, session funds and system safety. Unknown means the backend has not verified that value.</p>
        </div>
        <div className="rounded-lg border border-zinc-700 bg-zinc-950/60 px-3 py-2 text-xs text-zinc-300">
          <div className="flex items-center gap-2"><Wifi size={14} className={error ? 'text-amber-300' : 'text-emerald-300'} />{error ? 'Connection needs attention' : 'Dashboard status'}</div>
          <div className="mt-1 text-zinc-500">Last updated: {lastUpdated ? new Date(lastUpdated).toLocaleTimeString() : 'Waiting for backend data'}</div>
        </div>
      </div>
    </header>

    {error && <div role="status" className="flex gap-2 rounded-lg border border-amber-500/30 bg-amber-500/10 p-3 text-sm text-amber-200"><AlertTriangle size={17} className="mt-0.5 shrink-0" />Some dashboard information could not be refreshed. Values below are the last received values and may be stale. {error}</div>}

    <BettingModeControl showProgress={false} />

    <section aria-label="Dashboard summary" className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
      <SummaryCard icon={Activity} label="System health" value={readable(health?.state)} detail={health?.reason || 'Overall backend health'} color={health?.state === 'HEALTHY' ? 'emerald' : 'amber'} />
      <SummaryCard icon={ShieldCheck} label="Betting mode" value={readable(mode)} detail={status?.state ? `Session: ${readable(status.state)}` : status ? 'No active session reported' : 'Session status not received'} color={['LIVE_REAL', 'REAL'].includes(mode) ? 'amber' : 'sky'} />
      <SummaryCard icon={Database} label="Saved valid rounds" value={known(savedRounds) ? Number(savedRounds).toLocaleString() : 'Unknown'} detail={history?.last_update || history?.last_timestamp ? `Most recent round: ${new Date(history.last_update || history.last_timestamp).toLocaleString()}` : 'No recent round timestamp received'} color="violet" />
      <SummaryCard icon={CircleDollarSign} label="Verified balance" value={balanceVerified ? money(current) : 'Unknown'} detail={balanceVerified ? `Observed from ${balanceSource}${balanceObservedAt ? ` at ${new Date(balanceObservedAt).toLocaleTimeString()}` : ''}` : 'Waiting for a verified platform balance'} color={balanceVerified ? 'emerald' : 'amber'} />
    </section>

    <section className="grid gap-4 xl:grid-cols-2">
      <Card title="Session progress">
        <div className="grid gap-3 sm:grid-cols-2">
          <InfoTile label="Starting balance" value={known(start) ? money(start) : null} />
          <InfoTile label="Current balance" value={balanceVerified ? money(current) : null} detail={balanceObservedAt ? `Verified ${new Date(balanceObservedAt).toLocaleTimeString()} from ${balanceSource}` : 'Verification time unknown'} />
          <InfoTile label="Goal balance" value={known(target) ? money(target) : null} />
          <InfoTile label="Profit / loss" value={known(profit ?? runningPnl) ? money(profit ?? runningPnl) : null} detail={known(status?.total_bets) ? `${status.total_bets} bets · ${status.wins ?? 'Unknown'} wins · ${status.losses ?? 'Unknown'} losses` : 'Session totals unavailable'} />
        </div>
        <div className="mt-4">
          <div className="mb-2 flex justify-between text-xs text-zinc-400"><span>Progress from starting balance to goal</span><span>{goalProgress == null ? 'Unavailable' : `${goalProgress.toFixed(0)}%`}</span></div>
          <div className="h-2.5 overflow-hidden rounded-full bg-zinc-800" role={goalProgress == null ? undefined : 'progressbar'} aria-label="Session goal progress" aria-valuemin="0" aria-valuemax="100" aria-valuenow={goalProgress ?? undefined}>
            {goalProgress != null && <div className="h-full rounded-full bg-gradient-to-r from-sky-500 to-emerald-400 transition-[width]" style={{ width: `${goalProgress}%` }} />}
          </div>
          <p className="mt-2 text-xs text-zinc-500">{goalProgress == null ? 'Progress needs verified starting balance, current balance and a higher goal.' : `${money(Math.max(0, target - current))} remaining to goal`}</p>
        </div>
      </Card>

      <Card title="Decision and risk">
        <div className="grid gap-3 sm:grid-cols-2">
          <InfoTile label="Risk status" value={readable(risk?.risk_status)} detail={risk?.reason || 'No risk explanation received'} />
          <InfoTile label="Current decision" value={risk?.last_evaluation?.approved == null ? 'Unknown' : risk.last_evaluation.approved ? 'Approved' : 'Not approved'} detail={risk?.last_evaluation?.reason || risk?.reason || 'No decision explanation received'} />
          <InfoTile label="Risk limit" value={known(risk?.maximum_bet) ? money(risk.maximum_bet) : null} detail="Maximum approved single bet, when provided" />
          <InfoTile label="Emergency stop" value={risk?.emergency_stop == null ? null : risk.emergency_stop ? 'Active' : 'Not active'} detail={risk?.emergency_stop ? 'Betting must remain stopped until safely reset' : 'Safety control status'} />
        </div>
      </Card>
    </section>

    <SystemReadinessPanel readiness={readiness} error={readinessError} />

    <Card title="Browser Reliability">
      <div className="mb-3 flex flex-wrap items-center gap-2 text-sm">
        <span className={`rounded-full border px-2.5 py-1 font-semibold ${browserReliability?.state === 'HEALTHY' ? 'border-emerald-500/30 bg-emerald-500/10 text-emerald-300' : 'border-amber-500/30 bg-amber-500/10 text-amber-200'}`}>
          Browser {readable(browserReliability?.state || 'UNKNOWN')}
        </span>
        {browserReliability?.state === 'RECOVERING' && <span className="text-amber-200">Browser RECOVERING</span>}
        <span className="text-xs text-zinc-500">{browserReliability?.snapshot_stale ? `Supervisor heartbeat stale · ${browserReliability.reason || ''}` : browserReliability?.updated_at ? `Updated ${new Date(browserReliability.updated_at).toLocaleTimeString()}` : 'Supervisor heartbeat unavailable'}</span>
      </div>
      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
        <InfoTile label="Browser process" value={!browserSnapshotFresh ? 'UNKNOWN' : browserReliability?.browser?.connected ? 'HEALTHY' : browserReliability?.state === 'DISCONNECTED' ? 'DISCONNECTED' : 'UNKNOWN'} detail={browserSnapshotFresh && browserReliability?.browser?.pid ? `PID ${browserReliability.browser.pid}` : 'Process ID unavailable or snapshot stale'} />
        <InfoTile label="Persistent context" value={!browserSnapshotFresh ? 'UNKNOWN' : browserReliability?.context?.alive ? 'HEALTHY' : 'UNKNOWN'} detail={browserSnapshotFresh && browserReliability?.context?.page_count != null ? `${browserReliability.context.page_count} open page(s)` : 'Context snapshot unavailable or stale'} />
        <InfoTile label="History page" value={!browserSnapshotFresh ? 'UNKNOWN' : browserReliability?.history_page?.alive ? 'HEALTHY' : 'UNKNOWN'} detail={browserSnapshotFresh && browserReliability?.collector?.iframe_attached ? 'Aviator iframe attached' : 'Aviator iframe not verified'} />
        <InfoTile label="Betting page" value={browserSnapshotFresh ? browserReliability?.betting_page?.state || 'UNKNOWN' : 'UNKNOWN'} detail="Reports the page assigned by the shared browser supervisor" />
        <InfoTile label="Session" value={browserSnapshotFresh ? browserReliability?.session || 'UNKNOWN' : 'UNKNOWN'} />
        <InfoTile label="Browser uptime" value={!browserSnapshotFresh || browserReliability?.browser?.uptime_seconds == null ? null : `${Math.floor(browserReliability.browser.uptime_seconds)} seconds`} detail={!browserSnapshotFresh || browserReliability?.browser?.memory_rss_mb == null ? 'Memory unavailable' : `${browserReliability.browser.memory_rss_mb} MB RAM`} />
        <InfoTile label="Backend uptime" value={stability?.uptime_seconds == null ? null : `${Math.floor(stability.uptime_seconds)} seconds`} detail={stability?.pid ? `PID ${stability.pid}` : null} />
        <InfoTile label="Last round received" value={browserReliability?.collector?.last_round_id || null} detail={lastRoundAt ? new Date(lastRoundAt).toLocaleString() : 'No verified round timestamp'} />
        <InfoTile label="Last disconnect" value={browserReliability?.last_disconnect?.at ? new Date(browserReliability.last_disconnect.at).toLocaleString() : null} detail={browserReliability?.last_disconnect?.reason || null} />
        <InfoTile label="Recovery count" value={browserReliability?.recovery_count} detail={lastRecovery?.reason || null} />
        <InfoTile label="Last recovery" value={lastRecovery?.at ? new Date(lastRecovery.at).toLocaleString() : null} detail={lastRecovery?.state || null} />
        <InfoTile label="Circuit breaker" value={browserReliability?.circuit_breaker?.state || 'UNKNOWN'} detail={browserReliability?.circuit_breaker?.failures_10m == null ? null : `${browserReliability.circuit_breaker.failures_10m} failures in 10 minutes`} />
      </div>
      {browserReliability?.last_recovery?.state === 'CIRCUIT_BREAKER_OPEN' && <p className="mt-3 rounded-lg border border-rose-500/30 bg-rose-500/10 p-3 text-sm text-rose-200">Automatic browser recovery is paused. Investigate the recorded diagnostics before resuming.</p>}
    </Card>

    <section className="grid gap-4 xl:grid-cols-2">
      <Card title="Round collection">
        <div className="grid gap-3 sm:grid-cols-2">
          <InfoTile label="Collection status" value={readable(historyState)} detail={history?.message || history?.reason || 'Status from stored round history'} />
          <InfoTile label="Valid rounds stored" value={known(savedRounds) ? Number(savedRounds).toLocaleString() : null} />
          <InfoTile label="Last collected round" value={history?.latest?.round_id || history?.latest_round_id || history?.last_round_id || null} />
          <InfoTile label="Last collection time" value={history?.last_update || history?.last_timestamp ? new Date(history.last_update || history.last_timestamp).toLocaleString() : null} detail={history?.last_error || null} />
        </div>
      </Card>
      <Card title="Safety and operations">
        <div className="grid gap-3 sm:grid-cols-2">
          <InfoTile label="Open executions" value={operations?.snapshot?.open_executions} detail="Bets awaiting a final result" />
          <InfoTile label="Recorded incidents" value={operations?.incident ? 1 : 0} detail={operations?.incident ? 'An operations incident is active' : 'No active incident reported'} />
          <InfoTile label="Session state" value={readable(status?.state)} detail={status?.stop_reason ? `Stopped because: ${readable(status.stop_reason)}` : null} />
          <InfoTile label="Active round" value={status?.current_round?.round_id || status?.current_round || null} />
          <InfoTile label="Current bet" value={status?.current_bet?.amount_bif == null ? null : money(status.current_bet.amount_bif)} detail={status?.current_bet?.cashout_target ? `Cashout target: ${status.current_bet.cashout_target}x` : 'No open bet amount received'} />
          <InfoTile label="Last bet result" value={readable(status?.last_result?.status)} detail={status?.last_result?.message || null} />
        </div>
        {operations?.incident?.reasons?.length > 0 && <div className="mt-3 rounded-lg border border-rose-500/30 bg-rose-500/10 p-3 text-sm text-rose-200">Betting is paused: {operations.incident.reasons.map(readable).join(', ')}</div>}
      </Card>
    </section>

    <Card title="Session controls">
      <p className="mb-3 text-sm text-zinc-400">Use these controls to observe or stop the current session. Live execution still requires its separate authorization and safety checks.</p>
      <div className="flex flex-wrap gap-2">
        <button className="rounded-lg border border-sky-500/30 bg-sky-500/15 px-3 py-2 text-sm text-sky-200 hover:bg-sky-500/25" onClick={() => window.location.assign('/betting')}>Configure a session</button>
        <button className="rounded-lg border border-sky-500/30 bg-sky-500/15 px-3 py-2 text-sm text-sky-200 hover:bg-sky-500/25" onClick={() => control('observe')}>Refresh observation</button>
        <button className="rounded-lg border border-amber-500/30 bg-amber-500/15 px-3 py-2 text-sm text-amber-200 hover:bg-amber-500/25" onClick={() => control('pause')}>Pause</button>
        <button className="rounded-lg border border-emerald-500/30 bg-emerald-500/15 px-3 py-2 text-sm text-emerald-200 hover:bg-emerald-500/25" onClick={() => control('resume')}>Resume session</button>
        <button className="rounded-lg border border-zinc-600 bg-zinc-800 px-3 py-2 text-sm text-zinc-200 hover:bg-zinc-700" onClick={() => control('stop')}>Stop session</button>
        <button className="rounded-lg border border-rose-500/30 bg-rose-500/15 px-3 py-2 text-sm text-rose-200 hover:bg-rose-500/25" onClick={() => control('emergency-stop')}>Emergency stop</button>
      </div>
    </Card>

    <details className="group rounded-xl border border-zinc-800 bg-zinc-900/40 p-4">
      <summary className="cursor-pointer list-none text-sm font-semibold text-zinc-300">Technical diagnostics <span className="ml-1 text-xs font-normal text-zinc-500">(for troubleshooting)</span></summary>
      <div className="mt-4 space-y-4">
        <div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-4">
          {[
            ['Backend process', stability?.status], ['Process ID', stability?.pid],
            ['Parent process ID', stability?.ppid], ['Uptime', stability?.uptime_seconds == null ? null : `${Math.floor(stability.uptime_seconds)} seconds`],
            ['Restart count', stability?.restart_count], ['Unexpected restarts', stability?.unexpected_restart_count],
            ['Last restart', stability?.last_restart], ['Last shutdown reason', stability?.last_shutdown_reason],
            ['Collector worker', stability?.collector?.status], ['Collector browser', stability?.collector?.browser_connected && stability?.collector?.frame_connected ? 'CONNECTED' : stability?.collector ? 'WAITING' : null],
            ['ML worker', stability?.ml_worker?.status], ['Database', stability?.database?.status], ['Execution browser', stability?.browser?.status],
          ].map(([label, data]) => <InfoTile key={label} label={label} value={known(data) ? readable(data) : null} />)}
        </div>
        {stability?.browser?.reason && <p className="text-xs text-amber-300">Execution browser detail: {stability.browser.reason}</p>}
        <div className="grid gap-3 xl:grid-cols-2">
          <Card title="Pipeline components"><div className="grid gap-2 sm:grid-cols-2">{Object.entries(components).length ? Object.entries(components).map(([name, c]) => <div key={name} className="rounded-lg border border-zinc-800 bg-zinc-950 p-3 text-sm"><div className="flex justify-between gap-2"><span className="capitalize text-zinc-300">{name === 'browser' ? 'Execution browser' : name.replaceAll('_', ' ')}</span><span className={tone(c.state)}>{readable(c.state)}</span></div>{c.last_error && <div className="mt-2 text-xs text-zinc-500">{c.last_error}</div>}</div>) : <p className="text-sm text-zinc-500">No component details received.</p>}</div></Card>
          <Card title="Recent bets"><div className="space-y-2">{executions.length ? executions.map((x) => <div key={x.execution_id} className="grid grid-cols-2 gap-2 rounded-lg border border-zinc-800 bg-zinc-950 p-3 text-xs sm:grid-cols-4"><span><b className="block text-zinc-500">Round</b>{value(x.target_round_id)}</span><span><b className="block text-zinc-500">Status</b>{readable(x.status)}</span><span><b className="block text-zinc-500">Result</b>{readable(x.result)}</span><span><b className="block text-zinc-500">Balance check</b>{readable(x.balance_status)}</span></div>) : <p className="text-sm text-zinc-500">No saved bet executions were returned.</p>}</div></Card>
        </div>
      </div>
    </details>
  </div>;
}
