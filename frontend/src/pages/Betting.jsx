import { useCallback, useEffect, useRef, useState } from 'react';
import {
  AlertTriangle, Bot, CheckCircle2, Circle, OctagonX, Play, Radio, Send, Square, Zap,
} from 'lucide-react';
import Card from '../components/Card.jsx';
import {
  checkBettingBrowser, emergencyStopBetting, getBettingLedger, getBettingProfiles,
  getBettingStatus, startBettingSession, stopBettingSession, submitBettingDecision,
} from '../services/api.js';

const WS_URL = import.meta.env.VITE_WS_URL || 'ws://localhost:8000/ws/live';

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
  const [browserLoading, setBrowserLoading] = useState(false);
  const [actionBusy, setActionBusy] = useState(false);
  const [notice, setNotice] = useState(null);
  const [error, setError] = useState(null);

  const [mode, setMode] = useState('SIMULATION');
  const [profileKey, setProfileKey] = useState('PROFILE_A');
  const [label, setLabel] = useState('');
  const [targetProfit, setTargetProfit] = useState('');
  const [maxLoss, setMaxLoss] = useState('');
  const [maxRounds, setMaxRounds] = useState('');

  const [demoId, setDemoId] = useState('');
  const [demoMult, setDemoMult] = useState('2.0');
  const [demoAmount, setDemoAmount] = useState('');
  const [demoSlot, setDemoSlot] = useState(0);
  const [demoBusy, setDemoBusy] = useState(false);

  const wsRef = useRef(null);

  const st = status?.status || status || {};
  const state = st.state || 'IDLE';
  const active = ACTIVE_STATES.has(state);

  const refresh = useCallback(async () => {
    try {
      const [s, p, l] = await Promise.all([getBettingStatus(), getBettingProfiles(), getBettingLedger(50)]);
      setStatus(s);
      setProfiles(p);
      setLedger(l?.entries || []);
      setError(null);
    } catch (e) {
      setError(errMsg(e));
    }
  }, []);

  useEffect(() => {
    let closed = false;
    let retryTimer = null;
    let attempts = 0;
    const poll = window.setInterval(() => {
      if (!closed) refresh();
    }, 4000);

    function connect() {
      if (closed) return;
      const ws = new WebSocket(WS_URL);
      wsRef.current = ws;
      ws.onopen = () => { attempts = 0; };
      ws.onmessage = (event) => {
        if (closed) return;
        try {
          const msg = JSON.parse(event.data);
          if (!msg?.type || !msg.type.startsWith('betting:')) return; // ignore other /ws/live traffic
          if (msg.type === 'betting:state') {
            setStatus((prev) => ({ ...prev, status: { ...(prev?.status || {}), ...msg } }));
            refresh();
          } else if (msg.type === 'betting:ledger') {
            refresh();
          }
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
    connect();
    return () => {
      closed = true;
      window.clearInterval(poll);
      if (retryTimer) window.clearTimeout(retryTimer);
      if (wsRef.current) wsRef.current.close();
    };
  }, [refresh]);

  async function run(action) {
    setActionBusy(true);
    setNotice(null);
    setError(null);
    try {
      if (action === 'start') {
        const payload = { action: 'start', mode, profile: profileKey };
        if (label.trim()) payload.label = label.trim();
        if (targetProfit !== '') payload.target_profit_bif = Number(targetProfit);
        if (maxLoss !== '') payload.max_loss_bif = Number(maxLoss);
        if (maxRounds !== '') payload.max_rounds = Number(maxRounds);
        const res = await startBettingSession(payload);
        setStatus(res);
        setNotice(mode === 'SIMULATION' ? 'Simulation session started — no real bets are placed.' : 'Session start requested.');
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

  async function submitDemo() {
    setDemoBusy(true);
    setNotice(null);
    setError(null);
    try {
      const body = {
        decision_id: demoId || `manual-${Date.now()}`,
        profile: profileKey,
        target_multiplier: Number(demoMult),
        bet_slot: demoSlot,
        source: 'manual-demo',
      };
      if (demoAmount !== '') body.amount_bif = Number(demoAmount);
      const res = await submitBettingDecision(body);
      const e = res?.entry || {};
      const text = e.status === 'deferred'
        ? `Decision ${e.decision_id}: deferred — ${e.reason} (${e.note})`
        : e.status === 'rejected'
          ? `Decision ${e.decision_id}: rejected — ${e.reason} (${e.note})`
          : `Decision ${e.decision_id}: ${e.status}${e.simulated ? ' (simulated)' : ''} — ${e.note}`;
      setNotice(text);
      refresh();
    } catch (err) {
      setError(errMsg(err));
    } finally {
      setDemoBusy(false);
    }
  }

  const s = st;
  const browserReady = browser?.browser;
  const snap = browser?.snapshot;

  return (
    <div className="space-y-5">
      <div className="flex flex-col justify-between gap-3 md:flex-row md:items-center">
        <div>
          <div className="flex items-center gap-2 text-xs font-semibold uppercase tracking-wide text-emerald-300">
            <Bot size={14} /> Betting Automation · Part 1 (read-only)
          </div>
          <h1 className="mt-1 flex items-center gap-3 text-3xl font-semibold">
            Aviator Betting
            <StateBadge state={state} simulated={st.simulated} />
          </h1>
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
            disabled={actionBusy || active}
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
          <div className="text-xs uppercase tracking-wide text-zinc-500">Game balance (observed)</div>
          <div className="mt-1 text-2xl font-semibold">{s.last_balance_text || '—'}</div>
          <div className="text-xs text-zinc-500">{s.simulated ? 'simulated' : 'read-only from live browser'}</div>
        </Card>
        <Card>
          <div className="text-xs uppercase tracking-wide text-zinc-500">Running P&L (BIF)</div>
          <div className={`mt-1 text-2xl font-semibold ${(s.running_pnl_bif || 0) >= 0 ? 'text-emerald-300' : 'text-rose-300'}`}>
            {s.running_pnl_bif == null ? '—' : s.running_pnl_bif > 0 ? `+${s.running_pnl_bif}` : s.running_pnl_bif}
          </div>
          <div className="text-xs text-zinc-500">{s.resolved_decisions ?? 0} resolved decisions</div>
        </Card>
        <Card>
          <div className="text-xs uppercase tracking-wide text-zinc-500">Placement ledger</div>
          <div className="mt-1 text-2xl font-semibold">{s.ledger_size ?? 0}</div>
          <div className="text-xs text-zinc-500">
            {s.placed_count ?? 0} placed · {s.deferred_count ?? 0} deferred · {s.rejected_count ?? 0} rejected
          </div>
        </Card>
        <Card>
          <div className="flex items-center gap-2 text-xs uppercase tracking-wide text-zinc-500">
            UI readiness {s.last_ui_ready ? <CheckCircle2 size={14} className="text-emerald-400" /> : <Circle size={14} className="text-zinc-500" />}
          </div>
          <div className="mt-1 text-2xl font-semibold">{s.last_ui_ready ? 'Ready' : 'Not visible'}</div>
          <div className="text-xs text-zinc-500">latest crash {s.latest_crash != null ? `${s.latest_crash}x` : '—'}</div>
        </Card>
      </section>

      <div className="grid gap-5 xl:grid-cols-[1fr_1fr]">
        {/* Session controls */}
        <Card title="Session">
          <div className="grid gap-4 sm:grid-cols-2">
            <label className="block text-sm">
              <span className="text-xs font-semibold uppercase tracking-wide text-zinc-400">Mode</span>
              <select value={mode} onChange={(e) => setMode(e.target.value)} className="mt-1 w-full rounded border border-zinc-700 bg-zinc-900 px-3 py-2 text-sm">
                <option value="SIMULATION">Simulation (no real bets)</option>
                <option value="REAL">Real (read-only verification)</option>
              </select>
              <span className="mt-1 block text-[11px] leading-tight text-zinc-500">
                {mode === 'REAL'
                  ? 'Connects read-only to the live Aviator browser. Part 1 never places a real bet: valid decisions are deferred.'
                  : 'Deterministic demo against a scripted game loop. Clearly marked, never presented as a real bet.'}
              </span>
            </label>
            <label className="block text-sm">
              <span className="text-xs font-semibold uppercase tracking-wide text-zinc-400">Profile</span>
              <select value={profileKey} onChange={(e) => setProfileKey(e.target.value)} className="mt-1 w-full rounded border border-zinc-700 bg-zinc-900 px-3 py-2 text-sm">
                {(profiles?.keys || ['PROFILE_A', 'PROFILE_B']).map((k) => (
                  <option key={k} value={k}>{k}</option>
                ))}
              </select>
              {profiles?.profiles?.[profileKey] && (
                <span className="mt-1 block text-[11px] leading-tight text-zinc-500">
                  {profiles.profiles[profileKey].label} · base {profiles.profiles[profileKey].base_target}x ·
                  default stake {profiles.profiles[profileKey].default_amount_bif} BIF
                </span>
              )}
            </label>
            <label className="block text-sm sm:col-span-2">
              <span className="text-xs font-semibold uppercase tracking-wide text-zinc-400">Label (optional)</span>
              <input value={label} onChange={(e) => setLabel(e.target.value)} placeholder="e.g. weekend-live" className="mt-1 w-full rounded border border-zinc-700 bg-zinc-900 px-3 py-2 text-sm" />
            </label>
            <label className="block text-sm">
              <span className="text-xs font-semibold uppercase tracking-wide text-zinc-400">Target profit BIF</span>
              <input type="number" min="0" value={targetProfit} onChange={(e) => setTargetProfit(e.target.value)} placeholder="none" className="mt-1 w-full rounded border border-zinc-700 bg-zinc-900 px-3 py-2 text-sm" />
            </label>
            <label className="block text-sm">
              <span className="text-xs font-semibold uppercase tracking-wide text-zinc-400">Max loss BIF</span>
              <input type="number" min="0" value={maxLoss} onChange={(e) => setMaxLoss(e.target.value)} placeholder="none" className="mt-1 w-full rounded border border-zinc-700 bg-zinc-900 px-3 py-2 text-sm" />
            </label>
            <label className="block text-sm sm:col-span-2">
              <span className="text-xs font-semibold uppercase tracking-wide text-zinc-400">Max rounds</span>
              <input type="number" min="1" value={maxRounds} onChange={(e) => setMaxRounds(e.target.value)} placeholder="none (run until stopped)" className="mt-1 w-full rounded border border-zinc-700 bg-zinc-900 px-3 py-2 text-sm" />
            </label>
          </div>
          <p className="mt-3 flex items-center gap-2 text-[11px] text-zinc-500">
            <Radio size={12} className={active ? 'text-emerald-400' : ''} />
            Auto-stop goals only trigger after a round resolves (simulation). A REAL session is read-only in Part 1 and produces no resolutions.
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
              {browserReady?.error && <div className="rounded border border-rose-500/30 bg-rose-500/10 p-2 text-xs text-rose-200">{browserReady.error}</div>}
              {!browser?.ok && browser?.browser?.error && <div className="rounded border border-rose-500/30 bg-rose-500/10 p-2 text-xs text-rose-200">{browser.browser.error}</div>}
            </div>
          )}
        </Card>
      </div>

      {/* Demo decision (external contract) */}
      <Card title="Submit decision (external contract)" action={
        <span className="text-[11px] text-zinc-500">The module never predicts — decisions arrive through this contract.</span>
      }>
        <div className="grid gap-3 sm:grid-cols-6">
          <label className="block text-sm sm:col-span-2">
            <span className="text-xs font-semibold uppercase tracking-wide text-zinc-400">Decision id</span>
            <input value={demoId} onChange={(e) => setDemoId(e.target.value)} placeholder={`manual-${Date.now()}`} className="mt-1 w-full rounded border border-zinc-700 bg-zinc-900 px-3 py-2 text-sm" />
          </label>
          <label className="block text-sm">
            <span className="text-xs font-semibold uppercase tracking-wide text-zinc-400">Target ×</span>
            <input type="number" step="0.01" min="1.01" value={demoMult} onChange={(e) => setDemoMult(e.target.value)} className="mt-1 w-full rounded border border-zinc-700 bg-zinc-900 px-3 py-2 text-sm" />
          </label>
          <label className="block text-sm">
            <span className="text-xs font-semibold uppercase tracking-wide text-zinc-400">Stake BIF (blank = profile)</span>
            <input type="number" min="100" value={demoAmount} onChange={(e) => setDemoAmount(e.target.value)} placeholder="profile default" className="mt-1 w-full rounded border border-zinc-700 bg-zinc-900 px-3 py-2 text-sm" />
          </label>
          <label className="block text-sm">
            <span className="text-xs font-semibold uppercase tracking-wide text-zinc-400">Bet slot</span>
            <select value={demoSlot} onChange={(e) => setDemoSlot(Number(e.target.value))} className="mt-1 w-full rounded border border-zinc-700 bg-zinc-900 px-3 py-2 text-sm">
              <option value={0}>0 (first)</option>
              <option value={1}>1 (second)</option>
            </select>
          </label>
          <div className="flex items-end">
            <button onClick={submitDemo} disabled={demoBusy || !active} className="inline-flex w-full items-center justify-center gap-2 rounded border border-sky-500/40 bg-sky-500/10 px-4 py-2 text-sm font-semibold text-sky-200 hover:bg-sky-500/20 disabled:cursor-not-allowed disabled:opacity-40">
              <Send size={15} /> Submit
            </button>
          </div>
        </div>
      </Card>

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


