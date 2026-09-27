import { useCallback, useEffect, useState } from 'react';
import { api } from '../services/api.js';

const phrase = 'ENABLE LIVE BETTING';

function money(value) {
  return value == null || !Number.isFinite(Number(value))
    ? 'UNKNOWN' : `${Number(value).toLocaleString()} BIF`;
}

export default function BettingModeControl() {
  const [status, setStatus] = useState(null);
  const [readiness, setReadiness] = useState(null);
  const [profile, setProfile] = useState('PROFILE_A');
  const [goal, setGoal] = useState('');
  const [review, setReview] = useState(null);
  const [confirmText, setConfirmText] = useState('');
  const [confirmed, setConfirmed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  const refresh = useCallback(async () => {
    try {
      const [{ data }, { data: readinessData }] = await Promise.all([
        api.get('/api/betting-mode/status'),
        api.get('/api/readiness'),
      ]);
      setStatus(data.mode);
      setReadiness(readinessData.readiness);
      setError('');
    } catch (e) {
      if (e?.code !== 'ERR_BACKEND_RECONNECTING') setError(e?.response?.data?.detail?.message || e.message);
    }
  }, []);

  useEffect(() => {
    refresh();
    const timer = setInterval(refresh, 5000);
    return () => clearInterval(timer);
  }, [refresh]);

  async function selectShadow() {
    setBusy(true); setError('');
    try {
      const { data } = await api.post('/api/betting-mode', {
        mode: 'SHADOW_REALISTIC', profile, goal_balance: goal ? Number(goal) : null,
      });
      setStatus(data.status); setReview(null); setGoal('');
    } catch (e) {
      setError(e?.response?.data?.detail?.reasons?.join(', ') || e?.response?.data?.detail?.message || e.message);
    } finally { setBusy(false); }
  }

  async function requestLiveReview() {
    setBusy(true); setError(''); setReview(null); setConfirmed(false); setConfirmText('');
    try {
      const { data } = await api.get('/api/betting-mode/live-review', {
        params: { profile, goal_balance: goal ? Number(goal) : undefined },
      });
      setReview(data);
    } catch (e) {
      setError(e?.response?.data?.detail?.message || e?.response?.data?.detail?.reason || e.message);
    } finally { setBusy(false); }
  }

  async function activateLive() {
    if (!review || confirmText !== phrase || !confirmed || !review.live_available) return;
    setBusy(true); setError('');
    try {
      const { data } = await api.post('/api/betting-mode', {
        mode: 'LIVE_REAL', review_id: review.review_id, confirmation: phrase,
        profile: review.profile.profile,
        real_platform_balance: review.platform_balance.balance,
        cashout_target: review.cashout_target,
        minimum_bet: review.approved_bet_limits.minimum,
        maximum_bet: review.approved_bet_limits.maximum,
        session_loss_limit: review.session_loss_limit,
        goal_balance: review.goal_balance,
      });
      setStatus(data.status); setReview(null);
    } catch (e) {
      setError(e?.response?.data?.detail?.reasons?.join(', ') || e?.response?.data?.detail?.message || e.message);
      refresh();
    } finally { setBusy(false); }
  }

  const mode = status?.mode || 'SHADOW_REALISTIC';
  const pending = Number(status?.open_real_executions || 0) > 0;
  const shadow = status?.shadow || {};
  const training = readiness?.automatic_training || {};
  const contiguous = readiness?.history?.continuous_rounds ?? 0;
  const requiredContiguous = readiness?.history?.required_rounds ?? 100;
  const newRounds = training.new_rounds_since_training ?? 0;
  const minimumNewRounds = training.minimum_new_rounds ?? 250;
  const readinessReasons = readiness?.overall?.reasons || [];
  const shadowBlockers = Array.from(new Set([
    ...(training.blockers || []),
    ...readinessReasons,
    ...(readinessReasons.length === 1 && readinessReasons[0] === 'decision_expired'
      ? ['awaiting_fresh_decision'] : []),
  ].filter(Boolean)));
  const badge = mode === 'LIVE_REAL' ? 'LIVE — REAL MONEY'
    : pending ? 'SHADOW — REAL EXECUTION RECONCILIATION REQUIRED'
      : 'SHADOW — NO REAL MONEY';

  return <section className="mb-5 rounded-xl border border-zinc-700 bg-zinc-900 p-4" aria-label="Betting mode">
    <div className="flex flex-wrap items-center justify-between gap-3">
      <div>
        <div className="text-xs font-bold uppercase tracking-widest text-zinc-400">Betting mode</div>
        <div className={`mt-1 text-sm font-bold ${mode === 'LIVE_REAL' ? 'text-red-300' : pending ? 'text-amber-200' : 'text-emerald-300'}`}>
          {badge}
        </div>
      </div>
      <div className="flex flex-wrap items-center gap-2">
        <label className={`rounded border px-3 py-2 text-sm ${mode === 'SHADOW_REALISTIC' ? 'border-emerald-400 text-emerald-200' : 'border-zinc-700 text-zinc-400'}`}>
          <input className="mr-2 accent-emerald-400" type="radio" name="betting-mode" checked={mode === 'SHADOW_REALISTIC'} onChange={() => {}} />
          SHADOW_REALISTIC
        </label>
        <button type="button" disabled={busy || mode === 'SHADOW_REALISTIC' && status?.shadow_running}
          onClick={selectShadow} className="rounded bg-emerald-700 px-3 py-2 text-sm font-semibold disabled:opacity-50">
          {status?.shadow_running ? 'Shadow active' : 'Start / switch to Shadow'}
        </button>
        <label className={`rounded border px-3 py-2 text-sm ${mode === 'LIVE_REAL' ? 'border-red-400 text-red-200' : 'border-zinc-700 text-zinc-400'}`}>
          <input className="mr-2 accent-red-500" type="radio" name="betting-mode" checked={mode === 'LIVE_REAL'} onChange={() => {}} />
          LIVE_REAL
        </label>
        <button type="button" disabled={busy || mode === 'LIVE_REAL'} onClick={requestLiveReview}
          className="rounded bg-red-800 px-3 py-2 text-sm font-semibold disabled:opacity-50">
          Review LIVE activation
        </button>
      </div>
    </div>

    <div className="mt-3 flex flex-wrap items-end gap-3 border-t border-zinc-800 pt-3">
      <label className="text-xs text-zinc-400">Profile
        <select value={profile} onChange={e => setProfile(e.target.value)} className="ml-2 rounded bg-zinc-800 p-2 text-sm text-zinc-100">
          <option value="PROFILE_A">PROFILE_A</option><option value="PROFILE_B">PROFILE_B</option>
        </select>
      </label>
      <label className="text-xs text-zinc-400">Goal (BIF)
        <input type="number" min="1" value={goal} onChange={e => setGoal(e.target.value)} className="ml-2 w-36 rounded bg-zinc-800 p-2 text-sm text-zinc-100" placeholder="Set goal" />
      </label>
      {status?.reason && <span className="text-xs text-zinc-400">{status.reason}</span>}
    </div>

    {error && <p role="alert" className="mt-3 rounded border border-amber-500/40 bg-amber-950/30 p-2 text-sm text-amber-100">{error}</p>}

    {mode === 'SHADOW_REALISTIC' && status?.shadow_running && <div className="mt-4 rounded-lg border border-emerald-500/30 bg-emerald-950/20 p-3 text-sm">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <span className="font-semibold text-emerald-200">Shadow ledger progress</span>
        <span className="text-zinc-300">{shadow.bets || 0} bets · {money(shadow.profit || 0)} P/L</span>
      </div>
      <div className="mt-2 grid gap-1 text-xs text-zinc-300 sm:grid-cols-2 lg:grid-cols-5">
        <span>New rounds: {newRounds}/{minimumNewRounds}</span>
        <span>Contiguous: {contiguous}/{requiredContiguous}</span>
        <span>Cooldown: {training.cooldown_status || 'UNKNOWN'}</span>
        <span>Training lock: {training.training_lock || 'UNKNOWN'}</span>
        <span>Next evaluation: {training.next_evaluation || 'WAITING'}</span>
      </div>
      {shadow.bets === 0 && <p className="mt-2 text-amber-200">
        No shadow trade is eligible yet. The shared prediction and risk gates are still waiting.
      </p>}
      {!!shadowBlockers.length && <div className="mt-2 text-xs text-amber-200">
        {shadowBlockers.map(reason => <div key={reason}>{reason}</div>)}
      </div>}
    </div>}

    {review && <div role="dialog" aria-modal="true" aria-label="Review LIVE activation" className="mt-4 rounded-lg border border-red-500/50 bg-red-950/20 p-4">
      <h2 className="font-semibold text-red-200">Review LIVE_REAL — real money can be used</h2>
      <dl className="mt-3 grid gap-2 text-sm sm:grid-cols-2 lg:grid-cols-3">
        <div><dt className="text-zinc-400">Real platform balance</dt><dd>{review.platform_balance?.verified ? money(review.platform_balance.balance) : 'UNKNOWN — not verified'}</dd></div>
        <div><dt className="text-zinc-400">Profile</dt><dd>{review.profile?.profile} · {review.profile?.profile_name}</dd></div>
        <div><dt className="text-zinc-400">Cashout target</dt><dd>{review.cashout_target}x</dd></div>
        <div><dt className="text-zinc-400">Approved bet limits</dt><dd>{money(review.approved_bet_limits?.minimum)} – {money(review.approved_bet_limits?.maximum)}</dd></div>
        <div><dt className="text-zinc-400">Session loss limit</dt><dd>{money(review.session_loss_limit)}</dd></div>
        <div><dt className="text-zinc-400">Goal</dt><dd>{money(review.goal_balance)}</dd></div>
      </dl>
      {!!review.blockers?.length && <div className="mt-3 rounded bg-zinc-950/70 p-3 text-xs text-amber-200">
        <div className="mb-1 font-semibold">LIVE activation blockers</div>
        {review.blockers.map(reason => <div key={reason}>{reason}</div>)}
      </div>}
      <label className="mt-3 flex items-start gap-2 text-sm text-red-100">
        <input type="checkbox" checked={confirmed} onChange={e => setConfirmed(e.target.checked)} />
        I reviewed the real balance, profile, cashout, bet limits, loss limit and goal.
      </label>
      <label className="mt-3 block text-xs text-zinc-300">Type {phrase} to confirm
        <input value={confirmText} onChange={e => setConfirmText(e.target.value)} className="mt-1 block w-full rounded bg-zinc-800 p-2 text-sm" autoComplete="off" />
      </label>
      <div className="mt-4 flex gap-2">
        <button disabled={busy || !confirmed || confirmText !== phrase || !review.live_available}
          onClick={activateLive} className="rounded bg-red-700 px-4 py-2 font-bold disabled:opacity-40">
          Confirm LIVE_REAL
        </button>
        <button disabled={busy} onClick={() => setReview(null)} className="rounded bg-zinc-700 px-4 py-2">Cancel</button>
      </div>
    </div>}
  </section>;
}
