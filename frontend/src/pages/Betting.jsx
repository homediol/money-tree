import { useCallback, useEffect, useState } from 'react';
import { AlertTriangle, Check, OctagonX, ShieldCheck } from 'lucide-react';
import { api } from '../services/api.js';

function ReadinessRow({ label, value }) {
  const safe = ['READY', 'VERIFIED', 'CLEAR', 'ACTIVE', 'AVAILABLE', 'NOT_APPLIED'].includes(String(value).toUpperCase());
  return <div className="flex items-start justify-between gap-4 border-b border-zinc-800 py-2 last:border-0">
    <span className="text-sm capitalize text-zinc-300">{label.replaceAll('_', ' ')}</span>
    <span className={`text-right text-sm font-semibold ${safe ? 'text-emerald-300' : 'text-amber-200'}`}>{value ?? 'UNKNOWN'}</span>
  </div>;
}

function formatObservedMoney(value, currency) {
  if (value == null || !currency) return 'UNVERIFIED';
  return `${Number(value).toLocaleString()} ${currency}`;
}

export default function Betting() {
  const [live, setLive] = useState(null);
  const [mode, setMode] = useState(null);
  const [readiness, setReadiness] = useState(null);
  const [manualSetup, setManualSetup] = useState(null);
  const [intents, setIntents] = useState([]);
  const [executions, setExecutions] = useState([]);
  const [refreshing, setRefreshing] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [message, setMessage] = useState('');
  const [updatedAt, setUpdatedAt] = useState(null);

  const refresh = useCallback(async () => {
    setRefreshing(true);
    const results = await Promise.allSettled([
      api.get('/api/live/status'),
      api.get('/api/betting-mode/status'),
      api.get('/api/live/readiness'),
      api.get('/api/live/manual-setup'),
      api.get('/api/rules-engine/execution-intents', { params: { limit: 50 } }),
      api.get('/api/live/executions', { params: { limit: 50 } }),
    ]);
    const [liveResult, modeResult, readinessResult, setupResult, intentsResult, executionsResult] = results;
    if (liveResult.status === 'fulfilled') setLive(liveResult.value.data || null);
    if (modeResult.status === 'fulfilled') setMode(modeResult.value.data?.mode || null);
    if (readinessResult.status === 'fulfilled') setReadiness(readinessResult.value.data || null);
    if (setupResult.status === 'fulfilled') setManualSetup(setupResult.value.data?.manual_setup || null);
    if (intentsResult.status === 'fulfilled') setIntents(intentsResult.value.data?.intents || []);
    if (executionsResult.status === 'fulfilled') setExecutions(executionsResult.value.data?.executions || []);
    const failures = results.filter((result) => result.status === 'rejected');
    setError(failures.length ? 'Some Executor evidence is unavailable. Unknown states remain blocked.' : '');
    if (results.some((result) => result.status === 'fulfilled')) setUpdatedAt(new Date());
    setRefreshing(false);
  }, []);

  useEffect(() => {
    void refresh();
    const timer = window.setInterval(() => void refresh(), 10000);
    return () => window.clearInterval(timer);
  }, [refresh]);

  async function stop(emergency = false) {
    setBusy(true); setError(''); setMessage('');
    try {
      await api.post(emergency ? '/api/live/emergency-stop' : '/api/live/stop');
      setMessage(emergency ? 'Emergency stop latched.' : 'Executor disarmed.');
      await refresh();
    } catch (failure) {
      setError(failure?.response?.data?.detail?.reason || failure?.response?.data?.error || 'Executor safety action failed.');
    } finally { setBusy(false); }
  }

  const checklist = manualSetup?.checklist || [];
  const balanceVerified = checklist.find((row) => row.item === 'Balance verified')?.status === 'VERIFIED';
  const checks = readiness?.checks || {};
  const integrityChecks = Object.entries(checks).filter(([key]) => key !== 'optional_bankroll_policy');
  const openExecutions = executions.filter((row) => !['RECONCILED', 'CLOSED'].includes(String(row.status).toUpperCase()));
  const activeMode = mode?.mode || 'UNKNOWN';

  return <section className="space-y-5">
    <header className="flex flex-wrap items-end justify-between gap-4">
      <div>
        <div className="text-xs font-bold uppercase tracking-[0.18em] text-emerald-300">Component 3</div>
        <h1 className="mt-2 text-2xl font-bold">Betting Executor</h1>
        <p className="mt-2 max-w-4xl text-sm text-zinc-400">The operator sets stake, panel, Auto Cashout and Auto Bet in Aviator. This page reads those controls and reports execution integrity; it does not edit them or submit bets.</p>
      </div>
      <div className="flex flex-wrap gap-2">
        <button type="button" onClick={() => void refresh()} disabled={refreshing || busy}
          className="rounded border border-zinc-700 px-3 py-2 text-sm text-zinc-200 disabled:opacity-50">{refreshing ? 'Refreshing…' : 'Refresh evidence'}</button>
        <button type="button" onClick={() => void stop(false)} disabled={busy}
          className="rounded border border-amber-500/40 px-3 py-2 text-sm text-amber-200 disabled:opacity-50">OFF / Disarm</button>
        <button type="button" onClick={() => void stop(true)} disabled={busy}
          className="inline-flex items-center gap-2 rounded border border-rose-500/40 px-3 py-2 text-sm text-rose-200 disabled:opacity-50"><OctagonX size={15} />Emergency stop</button>
      </div>
    </header>

    {(error || message) && <div role="status" className={`rounded border px-4 py-3 text-sm ${error ? 'border-amber-500/30 bg-amber-500/10 text-amber-100' : 'border-emerald-500/30 bg-emerald-500/10 text-emerald-100'}`}>{error || message}</div>}

    <div role="status" className="rounded-lg border border-amber-500/30 bg-amber-500/10 p-4 text-sm text-amber-100">
      <div className="flex items-center gap-2 font-semibold"><AlertTriangle size={16} />REAL-MONEY EXECUTION: DISABLED</div>
      <p className="mt-1 text-xs text-amber-100/80">Refactor lock is active. A visible platform control, rule SELECT, or readiness review does not authorize a wager.</p>
    </div>

    <section className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
      <article className="rounded-xl border border-zinc-800 bg-zinc-900/70 p-4"><div className="text-xs uppercase text-zinc-500">Backend mode</div><div className="mt-2 text-lg font-semibold">{activeMode}</div><div className="mt-1 text-xs text-zinc-500">Authorization {mode?.live_authorized ? 'ACTIVE' : 'OFF'}</div></article>
      <article className="rounded-xl border border-zinc-800 bg-zinc-900/70 p-4"><div className="text-xs uppercase text-zinc-500">Manual setup</div><div className="mt-2 text-lg font-semibold">{manualSetup?.status || 'UNKNOWN'}</div><div className="mt-1 text-xs text-zinc-500">Selected panel {manualSetup?.selected_panel ?? 'UNVERIFIED'}</div></article>
      <article className="rounded-xl border border-zinc-800 bg-zinc-900/70 p-4"><div className="text-xs uppercase text-zinc-500">Observed platform balance</div><div className="mt-2 text-lg font-semibold">{balanceVerified ? formatObservedMoney(manualSetup?.account_balance, manualSetup?.account_currency) : 'UNVERIFIED'}</div><div className="mt-1 text-xs text-zinc-500">Read only · {manualSetup?.balance_observed_at || 'timestamp unavailable'}</div></article>
      <article className="rounded-xl border border-zinc-800 bg-zinc-900/70 p-4"><div className="text-xs uppercase text-zinc-500">Open execution records</div><div className="mt-2 text-lg font-semibold">{mode?.open_real_executions ?? openExecutions.length}</div><div className="mt-1 text-xs text-zinc-500">{mode?.reconciliation_required ? 'Settlement reconciliation required' : 'No open record reported'}</div></article>
    </section>

    <section className="rounded-xl border border-zinc-800 bg-zinc-900/50 p-4">
      <h2 className="text-sm font-semibold">Optional bankroll policies</h2>
      <p className="mt-1 text-xs text-zinc-400">Profit and loss targets, exposure percentages, risk profiles and automatic stake sizing have been removed from this workflow.</p>
      <div className="mt-2 max-w-xl"><ReadinessRow label="Policy status" value={readiness?.optional_bankroll_policy || 'NOT_APPLIED'} /></div>
    </section>

    <section className="grid gap-5 lg:grid-cols-2">
      <article className="rounded-xl border border-zinc-800 bg-zinc-900/60 p-5">
        <h2 className="text-lg font-semibold">Manual Aviator verification</h2>
        <p className="mt-1 text-xs text-zinc-400">Read-only panel and account evidence. Nothing here changes platform controls.</p>
        <div className="mt-3 grid gap-x-8 md:grid-cols-2">
          {checklist.map((row) => <ReadinessRow key={row.item} label={row.item} value={row.status} />)}
        </div>
        <div className="mt-4 grid gap-3 sm:grid-cols-2">
          <div className="rounded bg-zinc-950/70 p-3"><div className="text-xs text-zinc-500">Observed stake</div><div className="mt-1 font-semibold">{manualSetup?.observed_stake != null ? formatObservedMoney(manualSetup.observed_stake, manualSetup.observed_currency) : 'UNVERIFIED'}</div></div>
          <div className="rounded bg-zinc-950/70 p-3"><div className="text-xs text-zinc-500">Account currency</div><div className="mt-1 font-semibold">{manualSetup?.account_currency || 'UNVERIFIED'}</div></div>
          <div className="rounded bg-zinc-950/70 p-3"><div className="text-xs text-zinc-500">Pending bet states</div><div className="mt-1 font-semibold">{manualSetup?.pending_bet_states?.join(' · ') || 'UNKNOWN'}</div></div>
          <div className="rounded bg-zinc-950/70 p-3"><div className="text-xs text-zinc-500">Evidence timestamp</div><div className="mt-1 break-all text-xs font-semibold">{manualSetup?.observed_at || '—'}</div></div>
        </div>
      </article>

      <article className="rounded-xl border border-zinc-800 bg-zinc-900/60 p-5">
        <div className="flex items-center gap-2"><ShieldCheck size={18} className="text-emerald-300" /><h2 className="text-lg font-semibold">Mandatory execution-integrity checks</h2></div>
        <p className="mt-1 text-xs text-zinc-400">Authorization, target identity and window, duplicate prevention, fresh panel/stake readback, platform acceptance and settlement are required independently of bankroll policy.</p>
        <div className="mt-3">
          {integrityChecks.map(([key, value]) => <ReadinessRow key={key} label={key} value={value} />)}
        </div>
        <div className="mt-4 rounded border border-amber-500/25 bg-amber-500/5 p-3">
          <div className="text-sm font-semibold text-amber-100">Provider capabilities</div>
          <div className="mt-2 grid gap-x-6 md:grid-cols-2">
            {Object.entries(readiness?.provider_capabilities || {}).map(([key, value]) => <ReadinessRow key={key} label={key} value={value === true ? 'VERIFIED' : value === false ? 'UNVERIFIED' : 'UNKNOWN'} />)}
            {!Object.keys(readiness?.provider_capabilities || {}).length && <p className="text-xs text-amber-100/80">Capability evidence unavailable; unsupported behavior stays unverified.</p>}
          </div>
        </div>
        {readiness?.reasons?.length > 0 && <ul className="mt-4 list-disc space-y-1 pl-5 text-xs text-amber-100">{readiness.reasons.map((reason) => <li key={reason}>{reason}</li>)}</ul>}
      </article>
    </section>

    <section className="rounded-xl border border-zinc-800 bg-zinc-900/60 p-5">
      <h2 className="text-lg font-semibold">Durable SELECT intents</h2>
      <p className="mt-1 text-xs text-zinc-400">Each shared target decision remains deduplicated and observe-only. Intents are not wager submissions.</p>
      <div className="mt-3 overflow-x-auto">
        <table className="w-full min-w-[680px] text-left text-xs">
          <thead className="text-zinc-500"><tr><th className="py-2">Decision</th><th>Target</th><th>Rules</th><th>State</th><th>Blockers</th></tr></thead>
          <tbody>{intents.slice(0, 20).map((intent) => <tr key={intent.intent_id || intent.decision_id} className="border-t border-zinc-800 align-top">
            <td className="py-2 break-all">{intent.intent_id || intent.decision_id || '—'}</td>
            <td>{intent.target_round_id || `#${intent.target_round_index ?? '—'}`}</td>
            <td>{intent.selected_rule_ids?.join(', ') || '—'}</td>
            <td>{intent.execution_status || intent.status || 'OBSERVE_ONLY'}</td>
            <td>{intent.blockers?.join(', ') || 'Execution disabled'}</td>
          </tr>)}</tbody>
        </table>
        {!intents.length && <p className="py-4 text-sm text-zinc-500">No durable SELECT intents are currently available.</p>}
      </div>
    </section>

    <p className="text-right text-xs text-zinc-600">Updated {updatedAt?.toLocaleTimeString() || '—'}</p>
  </section>;
}
