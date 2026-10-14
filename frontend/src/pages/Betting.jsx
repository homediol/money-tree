import { useCallback, useEffect, useRef, useState } from 'react';
import {
  AlertTriangle, Bot, CheckCircle2, Circle, OctagonX, Radio, Square, Zap,
} from 'lucide-react';
import Card from '../components/Card.jsx';
import { getWebSocketUrl } from '../auth.js';
import {
  checkBettingBrowser, emergencyStopBetting, getBettingLedger,
  getBettingStatus, getRiskStatus, resetRiskEmergency,
  stopBettingSession,
} from '../services/api.js';

// Distinct visual language per state — CONNECTED (real) is emerald,
// SIMULATION is amber, browser problems are red/orange.
const STATE_CONFIG = {
  IDLE:               { label: 'Idle',                 cls: 'border-zinc-500/40 bg-zinc-500/10 text-zinc-300', dot: 'bg-zinc-400' },
  STARTING:           { label: 'Starting',             cls: 'border-sky-500/40 bg-sky-500/10 text-sky-200',     dot: 'bg-sky-400' },
  CONNECTED:          { label: 'Connected (live)',     cls: 'border-emerald-500/40 bg-emerald-500/10 text-emerald-200', dot: 'bg-emerald-400' },
  SIMULATION:         { label: 'Simulation',           cls: 'border-amber-500/40 bg-amber-500/10 text-amber-200', dot: 'bg-amber-400' },
  NOT_CONNECTED:      { label: 'Not connected',        cls: 'border-rose-500/40 bg-rose-500/10 text-rose-200',   dot: 'bg-rose-400' },
  WAITING_FOR_BROWSER:{ label: 'Waiting for browser',  cls: 'border-orange-500/40 bg-orange-500/10 text-orange-200', dot: 'bg-orange-400' },
  STOPPING:           { label: 'Stopping',             cls: 'border-zinc-500/40 bg-zinc-500/10 text-zinc-300',   dot: 'bg-zinc-400' },
  STOPPED:            { label: 'Stopped',              cls: 'border-zinc-500/40 bg-zinc-500/10 text-zinc-300',   dot: 'bg-zinc-400' },
  ERROR:              { label: 'Error',                cls: 'border-rose-500/40 bg-rose-500/10 text-rose-200',    dot: 'bg-rose-400' },
};

const ACTIVE_STATES = new Set(['STARTING', 'CONNECTED', 'SIMULATION', 'NOT_CONNECTED', 'WAITING_FOR_BROWSER']);

const STATUS_CHIP = {
  placed:   'border-sky-500/40 bg-sky-500/10 text-sky-200',
  resolved: 'border-emerald-500/40 bg-emerald-500/10 text-emerald-200',
  deferred: 'border-amber-500/40 bg-amber-500/10 text-amber-200',
  rejected: 'border-rose-500/40 bg-rose-500/10 text-rose-200',
  received: 'border-zinc-500/40 bg-zinc-500/10 text-zinc-300',
};

function fmtTime(ts) {
  if (!ts) return '—';
  return new Date(ts * 1000).toLocaleTimeString();
}

function printable(value) {
  if (typeof value === 'string') return value;
  if (value == null) return '';
  try { return JSON.stringify(value); } catch { return String(value); }
}

function observedFlag(value) {
  return value === true ? 'Yes' : value === false ? 'No' : 'Unknown';
}

function errMsg(error) {
  const data = error?.response?.data;
  const detail = data?.detail;
  if (Array.isArray(detail)) {
    return detail.map((item) => {
      if (typeof item === 'string') return item;
      const location = Array.isArray(item?.loc)
        ? item.loc.filter((part) => part !== 'body').join('.') : '';
      const message = printable(item?.msg || item?.message || item?.type);
      return [location, message].filter(Boolean).join(': ');
    }).filter(Boolean).join(' · ') || 'The server rejected the request.';
  }
  if (detail && typeof detail === 'object') {
    const reasons = Array.isArray(detail.reasons) ? detail.reasons.map(printable).join(' · ') : '';
    const message = [detail.message, detail.reason, detail.error, detail.browser?.error]
      .find((value) => typeof value === 'string' && value);
    return reasons || message || printable(detail);
  }
  return printable(data?.message || data?.error || detail || error?.message) || 'Request failed';
}

function StateBadge({ state, simulated }) {
  const cfg = STATE_CONFIG[state] || STATE_CONFIG.IDLE;
  const pulse = ACTIVE_STATES.has(state);
  return (
    <span className={`inline-flex items-center gap-2 rounded-full border px-3 py-1.5 text-sm font-semibold ${cfg.cls}`}>
      <span className="relative inline-flex">
        <span className={`h-2 w-2 rounded-full ${cfg.dot} ${pulse ? 'animate-pulse' : ''}`} />
      </span>
      {cfg.label}
      {simulated && <span className="rounded bg-amber-400/20 px-1.5 py-0.5 text-[10px] font-bold uppercase tracking-wide">demo</span>}
    </span>
  );
}

export default function Betting() {
  const [status, setStatus] = useState(null);      // backend /api/betting/status payload
  const [ledger, setLedger] = useState([]);
  const [browser, setBrowser] = useState(null);
  const [risk, setRisk] = useState(null);
  const [browserLoading, setBrowserLoading] = useState(false);
  const [actionBusy, setActionBusy] = useState(false);
  const [initialLoading, setInitialLoading] = useState(true);
  const [lastUpdated, setLastUpdated] = useState(null);
  const [notice, setNotice] = useState(null);
  const [error, setError] = useState(null);
  const [browserError, setBrowserError] = useState(null);
  const [refreshError, setRefreshError] = useState(null);
  const refreshInFlight = useRef(false);
  const refreshQueued = useRef(false);
  const browserInFlight = useRef(false);

  const wsRef = useRef(null);

  const st = status?.status || status || {};
  const state = st.state || 'IDLE';
  const active = ACTIVE_STATES.has(state);
  const refresh = useCallback(async () => {
    if (refreshInFlight.current) {
      refreshQueued.current = true;
      return;
    }
    refreshInFlight.current = true;
    const requests = await Promise.allSettled([
      getBettingStatus(), getBettingLedger(50), getRiskStatus(),
    ]);
    const [statusResult, ledgerResult, riskResult] = requests;
    const failed = [];
    if (statusResult.status === 'fulfilled') setStatus(statusResult.value);
    else failed.push(`session status: ${errMsg(statusResult.reason)}`);
    if (ledgerResult.status === 'fulfilled') setLedger(Array.isArray(ledgerResult.value?.entries) ? ledgerResult.value.entries : []);
    else failed.push(`ledger: ${errMsg(ledgerResult.reason)}`);
    if (riskResult.status === 'fulfilled') setRisk(riskResult.value?.status || null);
    else failed.push(`risk status: ${errMsg(riskResult.reason)}`);
    setRefreshError(failed.length ? failed.join(' · ') : null);
    if (requests.some((result) => result.status === 'fulfilled')) setLastUpdated(new Date());
    setInitialLoading(false);
    refreshInFlight.current = false;
    if (refreshQueued.current) {
      refreshQueued.current = false;
      window.setTimeout(() => { void refresh(); }, 0);
    }
  }, []);

  useEffect(() => {
    let closed = false;
    let retryTimer = null;
    let attempts = 0;
    function connect() {
      if (closed) return;
    const ws = new WebSocket(getWebSocketUrl());
      wsRef.current = ws;
      ws.onopen = () => { attempts = 0; };
      ws.onmessage = (event) => {
        if (closed) return;
        try {
          const msg = JSON.parse(event.data);
          if (!msg?.type || (!msg.type.startsWith('betting:') && !msg.type.startsWith('browser:') && !msg.type.startsWith('risk:'))) return;
          if (msg.type === 'betting:state') {
            setStatus((prev) => ({ ...prev, status: { ...(prev?.status || {}), ...msg } }));
            refresh();
          } else if (msg.type === 'betting:ledger') {
            refresh();
          } else refresh();
        } catch (err) { /* ignore malformed frames */ }
      };
      ws.onerror = () => {};
      ws.onclose = () => {
        if (closed) return;
        const delay = Math.min(1000 * 2 ** attempts, 15000);
        attempts += 1;
        retryTimer = window.setTimeout(connect, delay);
      };
    }

    refresh();
    const pollBrowser = async () => {
      if (browserInFlight.current) return;
      browserInFlight.current = true;
      try {
        const liveBrowser = await checkBettingBrowser(true);
        if (!closed) {
          setBrowser(liveBrowser);
          setBrowserError(liveBrowser?.ok === false
            ? liveBrowser?.browser?.error || liveBrowser?.message || 'Browser readiness check failed.'
            : null);
        }
      } catch (e) {
        if (!closed) setBrowserError(errMsg(e));
      } finally {
        browserInFlight.current = false;
      }
    };
    pollBrowser();
    const browserTimer = window.setInterval(pollBrowser, 5000);
    connect();
    return () => {
      closed = true;
      if (retryTimer) window.clearTimeout(retryTimer);
      if (browserTimer) window.clearInterval(browserTimer);
      if (wsRef.current) wsRef.current.close();
    };
  }, [refresh]);

  async function run(action) {
    setActionBusy(true);
    setNotice(null);
    setError(null);
    try {
      if (action === 'stop') {
        await stopBettingSession();
        setNotice('Stop requested.');
        await refresh();
      } else if (action === 'emergency') {
        if (!window.confirm('Emergency stop? (Forces the session loop to halt at the next safe point.)')) return;
        await emergencyStopBetting();
        setNotice('Emergency stop requested.');
        await refresh();
      } else if (action === 'browser') {
        setBrowserLoading(true);
        const res = await checkBettingBrowser(true);
        setBrowser(res);
        setBrowserError(res?.ok === false
          ? res?.browser?.error || res?.message || 'Browser readiness check failed.' : null);
        setNotice(null);
      }
    } catch (e) {
      setError(errMsg(e));
    } finally {
      setActionBusy(false);
      setBrowserLoading(false);
    }
  }

  const s = st;
  const browserReady = browser?.browser;
  const currencyLabel = browser?.platform_balance?.currency_verified === true
    ? browser.platform_balance.currency
    : browser?.snapshot?.balance_currency || s.currency || s.balance_currency || risk?.currency || 'currency unverified';
  const verifiedLiveBalance = browser?.platform_balance?.verified === true
    && browser?.platform_balance?.fresh === true
    && browserReady?.game_found === true
    ? browser.platform_balance.balance_text : null;
  const snap = browser?.snapshot;

  async function resetEmergency() {
    setActionBusy(true);
    setError(null);
    try {
      await resetRiskEmergency();
      setNotice('Emergency-stop latch reset. Automatic betting remains off until explicitly started.');
      await refresh();
    } catch (e) { setError(errMsg(e)); }
    finally { setActionBusy(false); }
  }

  return (
    <div className="space-y-5">
      <div role="status" className="rounded-lg border border-amber-500/30 bg-amber-500/10 px-4 py-3 text-sm text-amber-100">
        Automatic real-money execution is OFF. Rule selections are not connected to target-bound, risk-approved execution intents, and platform acceptance/results are not verified.
      </div>
      <div className="flex flex-col justify-between gap-3 md:flex-row md:items-center">
        <div>
          <div className="flex items-center gap-2 text-xs font-semibold uppercase tracking-wide text-emerald-300">
            <Bot size={14} /> Execution &amp; Risk Control
          </div>
          <h1 className="mt-1 flex items-center gap-3 text-3xl font-semibold">
            Aviator Betting · Risk Managed
            <StateBadge state={state} simulated={st.simulated} />
          </h1>
          <p className="mt-2 max-w-2xl text-sm text-zinc-400">Automatic real-money execution is OFF until the backend can bind a verified rule selection to one exact upcoming round, pass final risk checks, and confirm platform acceptance and settlement. Risk and emergency-stop controls remain available.</p>
          {st.session_id && (
            <div className="mt-1 text-xs text-zinc-500">
              session {st.session_id} · mode {st.mode} · profile {st.profile}
              {st.stop_reason ? ` · stopped: ${st.stop_reason}` : ''}
            </div>
          )}
        </div>
        <div className="flex flex-wrap gap-2">
          <button
            onClick={() => run('stop')}
            disabled={actionBusy || !active}
            className="inline-flex items-center gap-2 rounded border border-zinc-500/40 bg-zinc-500/10 px-4 py-2 text-sm font-semibold text-zinc-200 disabled:cursor-not-allowed disabled:opacity-40 hover:bg-zinc-500/20"
          >
            <Square size={16} /> Stop
          </button>
          <button
            onClick={() => run('emergency')}
            disabled={actionBusy || !active}
            className="inline-flex items-center gap-2 rounded border border-rose-500/40 bg-rose-500/10 px-4 py-2 text-sm font-semibold text-rose-200 disabled:cursor-not-allowed disabled:opacity-40 hover:bg-rose-500/20"
          >
            <OctagonX size={16} /> Emergency stop
          </button>
        </div>
      </div>

      {(error || notice) && (
        <div className={`rounded border px-4 py-3 text-sm ${error ? 'border-rose-500/40 bg-rose-500/10 text-rose-200' : 'border-emerald-500/40 bg-emerald-500/10 text-emerald-200'}`}>
          {error || notice}
        </div>
      )}
      {initialLoading && <div role="status" className="rounded border border-zinc-700 bg-zinc-900/60 px-4 py-3 text-sm text-zinc-400">Loading session, risk, and ledger data…</div>}
      {refreshError && <div role="status" className="rounded border border-amber-500/30 bg-amber-950/20 px-4 py-3 text-sm text-amber-200">
        Some Betting data could not refresh: {refreshError}. Showing the last available values{lastUpdated ? ` from ${lastUpdated.toLocaleTimeString()}` : ''}.
      </div>}

      {/* Metrics */}
      <section className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
        <Card>
          <div className="text-xs uppercase tracking-wide text-zinc-500">Current Balance (live)</div>
          <div className="mt-1 text-2xl font-semibold">{verifiedLiveBalance || '—'}</div>
          <div className="text-xs text-zinc-500">Read-only, fresh verified game browser balance</div>
        </Card>
        <Card>
          <div className="text-xs uppercase tracking-wide text-zinc-500">Profit / Loss ({currencyLabel})</div>
          <div className={`mt-1 text-2xl font-semibold ${(s.running_pnl_bif || 0) >= 0 ? 'text-emerald-300' : 'text-rose-300'}`}>
            {s.running_pnl_bif == null ? '—' : s.running_pnl_bif > 0 ? `+${s.running_pnl_bif}` : s.running_pnl_bif}
          </div>
          <div className="text-xs text-zinc-500">{s.resolved_decisions ?? 0} resolved decisions</div>
        </Card>
        <Card>
          <div className="text-xs uppercase tracking-wide text-zinc-500">Bets / Wins / Losses</div>
          <div className="mt-1 text-2xl font-semibold">{s.total_bets ?? 0} / {s.wins ?? 0} / {s.losses ?? 0}</div>
          <div className="text-xs text-zinc-500">
            {s.deferred_count ?? 0} deferred · {s.rejected_count ?? 0} rejected
          </div>
        </Card>
        <Card>
          <div className="flex items-center gap-2 text-xs uppercase tracking-wide text-zinc-500">
            Browser {s.browser_status || state} {s.last_ui_ready ? <CheckCircle2 size={14} className="text-emerald-400" /> : <Circle size={14} className="text-zinc-500" />}
          </div>
          <div className="mt-1 text-2xl font-semibold">{s.last_ui_ready ? 'Ready' : 'Not visible'}</div>
          <div className="text-xs text-zinc-500">round {s.current_round || '—'} · cashout {s.cashout || '—'}x</div>
        </Card>
      </section>

      <section className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
        <Card><div className="text-xs text-zinc-500">Session Starting Balance</div><div className="text-lg font-semibold">{s.starting_balance ?? '—'} {currencyLabel}</div></Card>
        <Card><div className="text-xs text-zinc-500">Session Goal</div><div className="text-lg font-semibold">{s.goal_balance ?? '—'} {currencyLabel}</div></Card>
        <Card><div className="text-xs text-zinc-500">Selected Profile</div><div className="text-lg font-semibold">{s.profile_label || s.profile || '—'}</div></Card>
        <Card><div className="text-xs text-zinc-500">Current Bet / Last Result</div><div className="truncate text-sm font-semibold">{s.current_bet?.decision_id || 'No active bet'} / {s.last_result?.note || '—'}</div></Card>
      </section>

      {['goal_reached', 'WIN_LIMIT_REACHED'].includes(s.stop_reason) && <div className="rounded border border-emerald-400 bg-emerald-500/15 p-4 text-xl font-bold text-emerald-200">WIN_LIMIT_REACHED — session stopped at the configured win limit.</div>}
      {['LOSS_LIMIT_REACHED', 'risk_loss_limit'].includes(s.stop_reason) && <div className="rounded border border-rose-400 bg-rose-500/15 p-4 text-xl font-bold text-rose-200">LOSS_LIMIT_REACHED — session stopped at the configured loss limit.</div>}

      <Card title="Risk Management · Part 2" action={
        risk?.emergency_stop ? <button onClick={resetEmergency} disabled={actionBusy} className="rounded border border-amber-500/40 px-3 py-1 text-xs text-amber-200 disabled:cursor-wait disabled:opacity-50">{actionBusy ? 'Resetting…' : 'Reset emergency latch'}</button> : null
      }>
        <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-5">
          <div className="rounded bg-zinc-900 p-3"><div className="text-xs text-zinc-500">Risk Profile</div><div className="font-semibold">{risk?.profile_name || '—'}</div><div className="text-xs text-zinc-500">cashout {risk?.cashout ?? '—'}x</div></div>
          <div className="rounded bg-zinc-900 p-3"><div className="text-xs text-zinc-500">Risk Level</div><div className={`font-semibold ${risk?.risk_level === 'BLOCKED' ? 'text-rose-300' : risk?.risk_level === 'NOT_EVALUATED' ? 'text-amber-300' : 'text-emerald-300'}`}>{risk?.risk_level || 'NOT_EVALUATED'}</div><div className="text-xs text-zinc-500">{risk?.risk_status || 'WAITING'}</div></div>
          <div className="rounded bg-zinc-900 p-3"><div className="text-xs text-zinc-500">Approved Bet / Maximum</div><div className="font-semibold">{risk?.current_bet_size ?? '—'} / {risk?.maximum_bet ?? '—'} {currencyLabel}</div><div className="text-xs text-zinc-500">max {(100 * (risk?.maximum_balance_percentage || 0)).toFixed(0)}% of balance</div></div>
          <div className="rounded bg-zinc-900 p-3"><div className="text-xs text-zinc-500">Session Loss / Maximum</div><div className="font-semibold">{risk?.session_loss ?? '—'} / {risk?.session_loss_limit_enabled === false ? 'Disabled' : `${risk?.maximum_session_loss ?? '—'} ${currencyLabel}`}</div><div className="text-xs text-zinc-500">P/L {risk?.profit_loss == null ? '—' : `${risk.profit_loss} ${currencyLabel}`}</div></div>
          <div className="rounded bg-zinc-900 p-3"><div className="text-xs text-zinc-500">Consecutive Losses</div><div className="font-semibold">{risk?.consecutive_losses ?? '—'} / {risk?.maximum_consecutive_losses ?? '—'}</div><div className="text-xs text-zinc-500">available {risk?.available_balance ?? '—'} {currencyLabel}</div></div>
        </div>
        <div className={`mt-3 rounded border p-3 text-sm ${risk?.risk_status === 'APPROVED' ? 'border-emerald-500/30 bg-emerald-500/10 text-emerald-200' : risk?.risk_status === 'WAITING' ? 'border-amber-500/30 bg-amber-500/10 text-amber-200' : 'border-rose-500/30 bg-rose-500/10 text-rose-200'}`}>
          <span className="font-semibold">Risk check: {{ APPROVED: 'Approved', WAITING: 'Waiting', BLOCKED: 'Blocked' }[risk?.risk_status] || 'Waiting'}</span>
          <span className="ml-2">{risk?.risk_status === 'WAITING'
            ? 'No current bet approval yet. Betting stays paused until the latest round passes the safety checks.'
            : risk?.reason || 'The system is checking whether a bet can be approved.'}</span>
          {risk?.emergency_stop && ' · Emergency stop is latched; no new bets can be approved.'}
        </div>
      </Card>

      <div className="grid gap-5 xl:grid-cols-[1fr_1fr]">
        <Card title="Session control">
          <div className="grid gap-3 sm:grid-cols-2">
            <div className="rounded bg-zinc-900 p-3"><div className="text-xs text-zinc-500">Mode</div><div className="font-semibold">{st.mode || 'SHADOW · stopped'}</div></div>
            <div className="rounded bg-zinc-900 p-3"><div className="text-xs text-zinc-500">Profile</div><div className="font-semibold">{risk?.profile_name || st.profile_label || st.profile || '—'}</div></div>
            <div className="rounded bg-zinc-900 p-3"><div className="text-xs text-zinc-500">Starting balance</div><div className="font-semibold">{s.starting_balance ?? '—'} {currencyLabel}</div></div>
            <div className="rounded bg-zinc-900 p-3"><div className="text-xs text-zinc-500">Goal</div><div className="font-semibold">{s.goal_balance ?? '—'} {currencyLabel}</div></div>
          </div>
          <p className="mt-3 text-xs leading-5 text-zinc-400">
            Real activation requires a current read-only review and explicit backend authorization. Automatic execution remains blocked until the rule-to-intent and platform verification gates are ready.
          </p>
          <p className="mt-2 flex items-center gap-2 text-[11px] text-zinc-500">
            <Radio size={12} className={active ? 'text-emerald-400' : ''} />
            Automatic mode: {active ? 'ON' : 'OFF'}. A goal is a stopping target, never a guarantee.
          </p>
        </Card>

        {/* Browser verification */}
        <Card title="Browser verification (read-only)" action={
          <button onClick={() => run('browser')} disabled={actionBusy || browserLoading} className="inline-flex items-center gap-2 rounded border border-zinc-600 px-3 py-1.5 text-xs font-semibold text-zinc-200 hover:bg-zinc-800 disabled:opacity-40">
            <Zap size={14} /> {browserLoading ? 'Checking…' : 'Check browser'}
          </button>
        }>
          {!browser && <p className="text-sm text-zinc-500">Runs a live CDP readiness probe (discover targets → read balance → probe game DOM). No clicks, no navigation.</p>}
          {browserError && <div role="status" className="mb-3 rounded border border-amber-500/30 bg-amber-950/20 p-2 text-xs text-amber-200">Latest browser check: {browserError}</div>}
          {browser && (
            <div className="space-y-3 text-sm">
              <div className="flex flex-wrap gap-2">
                <span className={`inline-flex items-center gap-1.5 rounded-full px-2.5 py-1 text-xs font-semibold ${browserReady?.connected ? 'border-emerald-500/40 bg-emerald-500/10 text-emerald-200' : 'border-rose-500/40 bg-rose-500/10 text-rose-200'}`}>
                  {browserReady?.connected ? <CheckCircle2 size={12} /> : <AlertTriangle size={12} />}
                  {browserReady?.connected ? 'browser connected' : 'not connected'}
                </span>
                <span className="rounded-full border border-zinc-700 px-2.5 py-1 text-xs text-zinc-300">{browserReady?.stage || 'unknown'}</span>
                {browserReady?.simulated && <span className="rounded-full border border-amber-500/40 px-2.5 py-1 text-xs text-amber-200">simulated</span>}
              </div>
              <div className="grid grid-cols-2 gap-2 text-xs">
                <div className="rounded bg-zinc-900 p-2"><span className="text-zinc-500">Balance: </span>{browserReady?.balance_text || '—'}</div>
                <div className="rounded bg-zinc-900 p-2"><span className="text-zinc-500">Page found: </span>{observedFlag(browserReady?.page_found)}</div>
                <div className="rounded bg-zinc-900 p-2"><span className="text-zinc-500">Game frame: </span>{observedFlag(browserReady?.game_found)}</div>
                <div className="rounded bg-zinc-900 p-2"><span className="text-zinc-500">aviator-next: </span>{observedFlag(browserReady?.aviator_next_reachable)}</div>
                <div className="col-span-2 rounded bg-zinc-900 p-2"><span className="text-zinc-500">Observed page: </span>{browserReady?.page_role || snap?.page_role || 'unknown'} · separate-page topology {observedFlag(snap?.browser_topology_verified)}</div>
              </div>
              {snap && (
                <div className="grid grid-cols-3 gap-2 text-xs">
                  <div className="rounded bg-zinc-900 p-2"><span className="text-zinc-500">Bet panel: </span>{observedFlag(snap.bet_panel)}</div>
                  <div className="rounded bg-zinc-900 p-2"><span className="text-zinc-500">UI ready: </span>{observedFlag(snap.ui_ready)}</div>
                  <div className="rounded bg-zinc-900 p-2"><span className="text-zinc-500">Place enabled: </span>{observedFlag(snap.place_bet_enabled)}</div>
                  <div className="col-span-3 rounded bg-zinc-900 p-2"><span className="text-zinc-500">Latest payouts: </span>{(snap.payouts_head || []).join(' · ') || '—'}</div>
                </div>
              )}
              {snap && <div className="grid gap-2 sm:grid-cols-2">
                {[0, 1].map(slot => {
                  const input = snap.stake_inputs?.find(item => item.slot === slot);
                  const button = snap.place_bet_buttons?.find(item => item.slot === slot);
                  const cashout = snap.cashout_controls?.find(item => item.slot === slot);
                  const cashoutTargetMatches = cashout?.displayedValue != null
                    && Number(cashout.displayedValue) === 2;
                  return <div key={slot} className="rounded border border-zinc-700 p-2 text-xs">
                    <div className="mb-1 font-semibold">Observed panel {slot + 1}</div>
                    <div>Stake field: {input ? (input.visible ? 'visible' : 'hidden') : 'not found'}</div>
                    <div>Displayed stake: {input?.value || '—'}</div>
                    <div>Button: {button ? (button.visible ? (button.disabled ? 'disabled' : 'enabled') : 'hidden') : 'not found'}</div>
                    <div>Auto cash-out: {cashout?.autoCashoutEnabled === true ? 'enabled' : cashout?.autoCashoutEnabled === false ? 'disabled' : 'unknown'}</div>
                    <div>Auto mode selected: {cashout?.autoTabSelected === true ? 'yes' : cashout?.autoTabSelected === false ? 'no' : 'unknown'}</div>
                    <div>Displayed cash-out: {cashout?.displayedValue != null ? `${cashout.displayedValue}x` : 'not found'}</div>
                    <div>2.00x value check: {cashoutTargetMatches ? 'matches' : cashout?.displayedValue != null ? 'does not match' : 'unknown'}</div>
                    <div>Platform acknowledgement: {cashout?.platformAcknowledged === true ? 'confirmed' : cashout?.platformAcknowledged === false ? 'rejected' : 'unverified'}</div>
                  </div>;
                })}
              </div>}
              {browserReady?.error && <div className="rounded border border-rose-500/30 bg-rose-500/10 p-2 text-xs text-rose-200">{printable(browserReady.error)}</div>}
              {!browser?.ok && browser?.browser?.error && <div className="rounded border border-rose-500/30 bg-rose-500/10 p-2 text-xs text-rose-200">{printable(browser.browser.error)}</div>}
            </div>
          )}
        </Card>
      </div>

      {/* Ledger */}
      <Card title="Ledger" action={<span className="text-[11px] text-zinc-500">{ledger.length} shown · ring cap 200</span>}>
        {ledger.length === 0 ? (
          <p className="text-sm text-zinc-500">No execution records are available yet.</p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-left text-sm">
              <thead>
                <tr className="border-b border-zinc-800 text-[11px] uppercase tracking-wide text-zinc-500">
                  <th className="py-2 pr-3">Time</th>
                  <th className="py-2 pr-3">Decision</th>
                  <th className="py-2 pr-3">Status</th>
                  <th className="py-2 pr-3">Stake</th>
                  <th className="py-2 pr-3">Target ×</th>
                  <th className="py-2 pr-3">Crash ×</th>
                  <th className="py-2 pr-3">P&L</th>
                  <th className="py-2">Note</th>
                </tr>
              </thead>
              <tbody>
                {ledger.map((e) => (
                  <tr key={e.entry_id} className="border-b border-zinc-800/60 text-xs">
                    <td className="py-2 pr-3 whitespace-nowrap text-zinc-400">{fmtTime(e.received_at)}</td>
                    <td className="py-2 pr-3">
                      <div className="font-medium text-zinc-200">{e.decision_id}</div>
                      <div className="text-[10px] text-zinc-500">{e.profile}{e.simulated ? ' · SIM' : ''}{e.bet_slot ? ' · slot 1' : ''}</div>
                    </td>
                    <td className="py-2 pr-3">
                      <span className={`inline-flex rounded-full border px-2 py-0.5 text-[10px] font-semibold ${STATUS_CHIP[e.status] || STATUS_CHIP.received}`}>
                        {e.status}{e.reason ? ` · ${e.reason}` : ''}
                      </span>
                    </td>
                    <td className="py-2 pr-3 text-zinc-300">{e.amount_bif}</td>
                    <td className="py-2 pr-3 text-zinc-300">{e.target_multiplier}</td>
                    <td className="py-2 pr-3 text-zinc-300">{e.crash_point != null ? e.crash_point : '—'}</td>
                    <td className={`py-2 pr-3 font-semibold ${e.pnl_bif == null ? 'text-zinc-500' : e.pnl_bif >= 0 ? 'text-emerald-300' : 'text-rose-300'}`}>
                      {e.pnl_bif == null ? '—' : e.pnl_bif > 0 ? `+${e.pnl_bif}` : e.pnl_bif}
                    </td>
                    <td className="max-w-[220px] truncate py-2 text-zinc-400" title={e.note || ''}>{e.note || '—'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>
    </div>
  );
}
