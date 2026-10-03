import { useCallback, useEffect, useRef, useState } from 'react';
import {
  AlertTriangle, Bot, CheckCircle2, Circle, OctagonX, Play, Radio, Square, Zap,
} from 'lucide-react';
import Card from '../components/Card.jsx';
import BettingModeControl from '../components/BettingModeControl.jsx';
import { getWebSocketUrl } from '../auth.js';
import {
  checkBettingBrowser, emergencyStopBetting, getBettingLedger, getBettingProfiles,
  getBettingStatus, getRiskProfiles, getRiskStatus, resetRiskEmergency,
  setRiskProfile, startBettingSession, stopBettingSession,
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

function errMsg(error) {
  return error?.response?.data?.message || error?.message || 'Request failed';
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
  const [profiles, setProfiles] = useState(null);
  const [ledger, setLedger] = useState([]);
  const [browser, setBrowser] = useState(null);
  const [risk, setRisk] = useState(null);
  const [riskProfiles, setRiskProfiles] = useState(null);
  const [browserLoading, setBrowserLoading] = useState(false);
  const [actionBusy, setActionBusy] = useState(false);
  const [notice, setNotice] = useState(null);
  const [error, setError] = useState(null);

  const [profileKey, setProfileKey] = useState(null);
  const [startingBalance, setStartingBalance] = useState('');
  const [goalBalance, setGoalBalance] = useState('');

  const wsRef = useRef(null);

  const st = status?.status || status || {};
  const state = st.state || 'IDLE';
  const active = ACTIVE_STATES.has(state);
  const canStart = Boolean(profileKey && startingBalance !== '' && goalBalance !== ''
    && Number(startingBalance) >= 0 && Number(goalBalance) > Number(startingBalance));

  const refresh = useCallback(async () => {
    try {
      const [s, p, l, r, rp] = await Promise.all([
        getBettingStatus(), getBettingProfiles(), getBettingLedger(50),
        getRiskStatus(), getRiskProfiles(),
      ]);
      setStatus(s);
      setProfiles(p);
      setLedger(l?.entries || []);
      setRisk(r?.status || null);
      setRiskProfiles(rp?.profiles || null);
      const live = s?.status || s || {};
      const selected = r?.status?.selected_profile || live.profile;
      if (selected) setProfileKey(selected);
      if (live.starting_balance != null) setStartingBalance(String(live.starting_balance));
      if (live.goal_balance != null) setGoalBalance(String(live.goal_balance));
      setError(null);
    } catch (e) {
      setError(errMsg(e));
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
    let browserTimer = null;
    const pollBrowser = async () => {
      try {
        const liveBrowser = await checkBettingBrowser(true);
        if (!closed) setBrowser(liveBrowser);
      } catch (e) {
        if (!closed) setError(errMsg(e));
      }
    };
    pollBrowser();
    browserTimer = window.setInterval(pollBrowser, 5000);
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
      if (action === 'start') {
        const payload = {
          starting_balance: Number(startingBalance),
          goal_balance: Number(goalBalance),
          profile: profileKey,
        };
        const res = await startBettingSession(payload);
        setStatus(res);
        setNotice('Automatic betting started. Waiting for browser verification and an authorized decision.');
        refresh();
      } else if (action === 'stop') {
        await stopBettingSession();
        setNotice('Stop requested.');
        refresh();
      } else if (action === 'emergency') {
        if (!window.confirm('Emergency stop? (Forces the session loop to halt at the next safe point.)')) return;
        await emergencyStopBetting();
        setNotice('Emergency stop requested.');
        refresh();
      } else if (action === 'browser') {
        setBrowserLoading(true);
        const res = await checkBettingBrowser(true);
        setBrowser(res);
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
  const snap = browser?.snapshot;

  async function chooseProfile(key) {
    setProfileKey(key);
    if (active) return;
    try {
      await setRiskProfile(key);
      await refresh();
    } catch (e) {
      setError(errMsg(e));
    }
  }

  async function resetEmergency() {
    try {
      await resetRiskEmergency();
      setNotice('Emergency-stop latch reset. Automatic betting remains off until explicitly started.');
      await refresh();
    } catch (e) { setError(errMsg(e)); }
  }

  return (
    <div className="space-y-5">
      <BettingModeControl />
      <div className="flex flex-col justify-between gap-3 md:flex-row md:items-center">
        <div>
          <div className="flex items-center gap-2 text-xs font-semibold uppercase tracking-wide text-emerald-300">
            <Bot size={14} /> Execution &amp; Risk Control
          </div>
          <h1 className="mt-1 flex items-center gap-3 text-3xl font-semibold">
            Aviator Betting · Risk Managed
            <StateBadge state={state} simulated={st.simulated} />
          </h1>
          <p className="mt-2 max-w-2xl text-sm text-zinc-400">Control automatic execution, inspect browser readiness, and monitor bankroll safety. No bet is placed without an external authorized decision and risk approval.</p>
          {st.session_id && (
            <div className="mt-1 text-xs text-zinc-500">
              session {st.session_id} · mode {st.mode} · profile {st.profile}
              {st.stop_reason ? ` · stopped: ${st.stop_reason}` : ''}
            </div>
          )}
        </div>
        <div className="flex flex-wrap gap-2">
          <button
            onClick={() => run('start')}
            disabled={actionBusy || active || !canStart}
            className="inline-flex items-center gap-2 rounded border border-emerald-500/40 bg-emerald-500/10 px-4 py-2 text-sm font-semibold text-emerald-200 disabled:cursor-not-allowed disabled:opacity-40 hover:bg-emerald-500/20"
          >
            <Play size={16} /> Start
          </button>
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

      {/* Metrics */}
      <section className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
        <Card>
          <div className="text-xs uppercase tracking-wide text-zinc-500">Current Balance (live)</div>
          <div className="mt-1 text-2xl font-semibold">{browserReady?.balance_text || s.last_balance_text || '—'}</div>
          <div className="text-xs text-zinc-500">Read-only from the connected game browser</div>
        </Card>
        <Card>
          <div className="text-xs uppercase tracking-wide text-zinc-500">Profit / Loss (BIF)</div>
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
        <Card><div className="text-xs text-zinc-500">Session Starting Balance</div><div className="text-lg font-semibold">{s.starting_balance ?? '—'} BIF</div></Card>
        <Card><div className="text-xs text-zinc-500">Session Goal</div><div className="text-lg font-semibold">{s.goal_balance ?? '—'} BIF</div></Card>
        <Card><div className="text-xs text-zinc-500">Selected Profile</div><div className="text-lg font-semibold">{s.profile_label || s.profile || '—'}</div></Card>
        <Card><div className="text-xs text-zinc-500">Current Bet / Last Result</div><div className="truncate text-sm font-semibold">{s.current_bet?.decision_id || 'No active bet'} / {s.last_result?.note || '—'}</div></Card>
      </section>

      {s.stop_reason === 'goal_reached' && <div className="rounded border border-emerald-400 bg-emerald-500/15 p-4 text-xl font-bold text-emerald-200">🎯 Goal Reached</div>}

      <Card title="Risk Management · Part 2" action={
        risk?.emergency_stop ? <button onClick={resetEmergency} className="rounded border border-amber-500/40 px-3 py-1 text-xs text-amber-200">Reset emergency latch</button> : null
      }>
        <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-5">
          <div className="rounded bg-zinc-900 p-3"><div className="text-xs text-zinc-500">Risk Profile</div><div className="font-semibold">{risk?.profile_name || '—'}</div><div className="text-xs text-zinc-500">cashout {risk?.cashout ?? '—'}x</div></div>
          <div className="rounded bg-zinc-900 p-3"><div className="text-xs text-zinc-500">Risk Level</div><div className={`font-semibold ${risk?.risk_level === 'BLOCKED' ? 'text-rose-300' : risk?.risk_level === 'NOT_EVALUATED' ? 'text-amber-300' : 'text-emerald-300'}`}>{risk?.risk_level || 'NOT_EVALUATED'}</div><div className="text-xs text-zinc-500">{risk?.risk_status || 'WAITING'}</div></div>
          <div className="rounded bg-zinc-900 p-3"><div className="text-xs text-zinc-500">Approved Bet / Maximum</div><div className="font-semibold">{risk?.current_bet_size ?? '—'} / {risk?.maximum_bet ?? '—'} BIF</div><div className="text-xs text-zinc-500">max {(100 * (risk?.maximum_balance_percentage || 0)).toFixed(0)}% of balance</div></div>
          <div className="rounded bg-zinc-900 p-3"><div className="text-xs text-zinc-500">Session Loss / Maximum</div><div className="font-semibold">{risk?.session_loss ?? '—'} / {risk?.maximum_session_loss ?? '—'} BIF</div><div className="text-xs text-zinc-500">P/L {risk?.profit_loss == null ? '—' : `${risk.profit_loss} BIF`}</div></div>
          <div className="rounded bg-zinc-900 p-3"><div className="text-xs text-zinc-500">Consecutive Losses</div><div className="font-semibold">{risk?.consecutive_losses ?? '—'} / {risk?.maximum_consecutive_losses ?? '—'}</div><div className="text-xs text-zinc-500">available {risk?.available_balance ?? '—'} BIF</div></div>
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
        {/* Session controls */}
        <Card title="Betting Control">
          <div className="grid gap-4 sm:grid-cols-2">
            <label className="block text-sm">
              <span className="text-xs font-semibold uppercase tracking-wide text-zinc-400">Starting Balance (BIF)</span>
              <input type="number" min="0" value={startingBalance} onChange={(e) => setStartingBalance(e.target.value)} className="mt-1 w-full rounded border border-zinc-700 bg-zinc-900 px-3 py-2 text-sm" />
            </label>
            <label className="block text-sm">
              <span className="text-xs font-semibold uppercase tracking-wide text-zinc-400">Goal Balance (BIF)</span>
              <input type="number" min="1" value={goalBalance} onChange={(e) => setGoalBalance(e.target.value)} className="mt-1 w-full rounded border border-zinc-700 bg-zinc-900 px-3 py-2 text-sm" />
            </label>
            <fieldset className="sm:col-span-2">
              <legend className="text-xs font-semibold uppercase tracking-wide text-zinc-400">Betting Profile</legend>
              <div className="mt-2 grid gap-2 sm:grid-cols-2">
                {(profiles?.keys || ['PROFILE_A', 'PROFILE_B']).map((key) => (
                  <label key={key} className={`cursor-pointer rounded border p-3 ${profileKey === key ? 'border-emerald-500 bg-emerald-500/10' : 'border-zinc-700 bg-zinc-900'}`}>
                    <input type="radio" className="mr-2" checked={profileKey === key} onChange={() => chooseProfile(key)} />
                    {riskProfiles?.[key]?.name || profiles?.profiles?.[key]?.name || key}
                  </label>
                ))}
              </div>
            </fieldset>
          </div>
          <p className="mt-3 flex items-center gap-2 text-[11px] text-zinc-500">
            <Radio size={12} className={active ? 'text-emerald-400' : ''} />
            Automatic mode: {active ? 'ON' : 'OFF'}. The goal is a stopping target, never a guarantee.
          </p>
        </Card>

        {/* Browser verification */}
        <Card title="Browser verification (read-only)" action={
          <button onClick={() => run('browser')} disabled={actionBusy || browserLoading} className="inline-flex items-center gap-2 rounded border border-zinc-600 px-3 py-1.5 text-xs font-semibold text-zinc-200 hover:bg-zinc-800 disabled:opacity-40">
            <Zap size={14} /> {browserLoading ? 'Checking…' : 'Check browser'}
          </button>
        }>
          {!browser && <p className="text-sm text-zinc-500">Runs a live CDP readiness probe (discover targets → read balance → probe game DOM). No clicks, no navigation.</p>}
          {browser && (
            <div className="space-y-3 text-sm">
              <div className="flex flex-wrap gap-2">
                <span className={`inline-flex items-center gap-1.5 rounded-full px-2.5 py-1 text-xs font-semibold ${browserReady?.connected ? 'border-emerald-500/40 bg-emerald-500/10 text-emerald-200' : 'border-rose-500/40 bg-rose-500/10 text-rose-200'}`}>
                  {browserReady?.connected ? <CheckCircle2 size={12} /> : <AlertTriangle size={12} />}
                  {browserReady?.connected ? 'browser connected' : 'not connected'}
                </span>
                <span className="rounded-full border border-zinc-700 px-2.5 py-1 text-xs text-zinc-300">{browserReady?.stage}</span>
                {browserReady?.simulated && <span className="rounded-full border border-amber-500/40 px-2.5 py-1 text-xs text-amber-200">simulated</span>}
              </div>
              <div className="grid grid-cols-2 gap-2 text-xs">
                <div className="rounded bg-zinc-900 p-2"><span className="text-zinc-500">Balance: </span>{browserReady?.balance_text || '—'}</div>
                <div className="rounded bg-zinc-900 p-2"><span className="text-zinc-500">Page found: </span>{String(browserReady?.page_found)}</div>
                <div className="rounded bg-zinc-900 p-2"><span className="text-zinc-500">Game frame: </span>{String(browserReady?.game_found)}</div>
                <div className="rounded bg-zinc-900 p-2"><span className="text-zinc-500">aviator-next: </span>{String(browserReady?.aviator_next_reachable)}</div>
              </div>
              {snap && (
                <div className="grid grid-cols-3 gap-2 text-xs">
                  <div className="rounded bg-zinc-900 p-2"><span className="text-zinc-500">Bet panel: </span>{String(snap.bet_panel)}</div>
                  <div className="rounded bg-zinc-900 p-2"><span className="text-zinc-500">UI ready: </span>{String(snap.ui_ready)}</div>
                  <div className="rounded bg-zinc-900 p-2"><span className="text-zinc-500">Place enabled: </span>{String(snap.place_bet_enabled)}</div>
                  <div className="col-span-3 rounded bg-zinc-900 p-2"><span className="text-zinc-500">Latest payouts: </span>{(snap.payouts_head || []).join(' · ') || '—'}</div>
                </div>
              )}
              {snap && <div className="grid gap-2 sm:grid-cols-2">
                {[0, 1].map(slot => {
                  const input = snap.stake_inputs?.find(item => item.slot === slot);
                  const button = snap.place_bet_buttons?.find(item => item.slot === slot);
                  return <div key={slot} className="rounded border border-zinc-700 p-2 text-xs">
                    <div className="mb-1 font-semibold">Observed panel {slot + 1}</div>
                    <div>Stake field: {input ? (input.visible ? 'visible' : 'hidden') : 'not found'}</div>
                    <div>Displayed stake: {input?.value || '—'}</div>
                    <div>Button: {button ? (button.visible ? (button.disabled ? 'disabled' : 'enabled') : 'hidden') : 'not found'}</div>
                    <div>Cash-out: configured by authorized execution only</div>
                  </div>;
                })}
              </div>}
              {browserReady?.error && <div className="rounded border border-rose-500/30 bg-rose-500/10 p-2 text-xs text-rose-200">{browserReady.error}</div>}
              {!browser?.ok && browser?.browser?.error && <div className="rounded border border-rose-500/30 bg-rose-500/10 p-2 text-xs text-rose-200">{browser.browser.error}</div>}
            </div>
          )}
        </Card>
      </div>

      {/* Ledger */}
      <Card title="Ledger" action={<span className="text-[11px] text-zinc-500">{ledger.length} shown · ring cap 200</span>}>
        {ledger.length === 0 ? (
          <p className="text-sm text-zinc-500">No decisions recorded yet. Start a session and submit decisions to see entries here.</p>
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
