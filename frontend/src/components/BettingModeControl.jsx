import { useCallback, useEffect, useRef, useState } from 'react';
import { api } from '../services/api.js';
import ModelProgressMessage from './ModelProgressMessage.jsx';

const phrase = 'ENABLE LIVE BETTING';

function money(value) {
  return value == null || !Number.isFinite(Number(value))
    ? 'UNKNOWN' : `${Number(value).toLocaleString()} BIF`;
}

function metric(value) {
  return value == null || !Number.isFinite(Number(value)) ? 'UNKNOWN' : Number(value).toLocaleString();
}

function panelText(panel, index, mode) {
  if (!panel.enabled) return `disabled`;
  const target = mode === 'AUTOMATIC' ? (index === 0 ? '2.00' : '1.50') : panel.cashout;
  return `${mode === 'MANUAL' ? money(panel.stake) : 'Risk sized'} @ ${target}x`;
}

export default function BettingModeControl({ showProgress = true }) {
  const [status, setStatus] = useState(null);
  const [readiness, setReadiness] = useState(null);
  const [profile, setProfile] = useState('PROFILE_A');
  const [goal, setGoal] = useState('');
  const [startingBalance, setStartingBalance] = useState('');
  const [sessionType, setSessionType] = useState('AUTOMATIC');
  const [panels, setPanels] = useState([
    { enabled: true, stake: '1000', cashout: '2.00' },
    { enabled: true, stake: '1000', cashout: '2.00' },
  ]);
  const [riskProfiles, setRiskProfiles] = useState(null);
  const [configureShadow, setConfigureShadow] = useState(false);
  const [reviewShadow, setReviewShadow] = useState(false);
  const [confirmShadow, setConfirmShadow] = useState(false);
  const [review, setReview] = useState(null);
  const [confirmText, setConfirmText] = useState('');
  const [confirmed, setConfirmed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [refreshError, setRefreshError] = useState('');
  const refreshInFlight = useRef(false);

  const refresh = useCallback(async () => {
    if (refreshInFlight.current) return;
    refreshInFlight.current = true;
    try {
      const [{ data }, { data: readinessData }] = await Promise.all([
        api.get('/api/betting-mode/status'),
        api.get('/api/readiness'),
      ]);
      setStatus(data.mode);
      setReadiness(readinessData.readiness);
      setRefreshError('');
    } catch (e) {
      if (e?.code !== 'ERR_BACKEND_RECONNECTING') setRefreshError(e?.response?.data?.detail?.message || e.message);
    } finally {
      refreshInFlight.current = false;
    }
  }, []);

  useEffect(() => {
    refresh();
    const timer = setInterval(refresh, 5000);
    return () => clearInterval(timer);
  }, [refresh]);

  async function selectShadow() {
    setBusy(true); setError(''); setConfigureShadow(true); setReviewShadow(false); setConfirmShadow(false);
    try {
      const [{ data }, { data: riskData }] = await Promise.all([
        api.get('/api/betting-mode/shadow-review'), api.get('/api/risk/profiles'),
      ]);
      if (data.platform_balance?.verified) setStartingBalance(String(data.platform_balance.balance));
      else throw new Error(data.reason || 'Real platform balance is not verified.');
      setRiskProfiles(riskData.profiles || null);
    } catch (e) {
      setError(e?.response?.data?.detail?.reasons?.join(', ') || e?.response?.data?.detail?.message || e.message);
    } finally { setBusy(false); }
  }

  function updatePanel(index, field, value) {
    setPanels(current => current.map((panel, i) => i === index ? { ...panel, [field]: value } : panel));
    setReviewShadow(false); setConfirmShadow(false);
  }

  async function startShadow() {
    if (!reviewShadow || !confirmShadow) return;
    setBusy(true); setError('');
    try {
      const configuration = {
        mode: sessionType,
        maximum_combined_exposure: sessionType === 'MANUAL'
          ? panels.reduce((n, panel) => n + (panel.enabled ? Number(panel.stake || 0) : 0), 0)
          : automaticExposureLimit,
        panels: panels.map(panel => ({ enabled: panel.enabled,
          stake: sessionType === 'MANUAL' ? Number(panel.stake) : null,
          cashout: Number(panel.cashout) })),
      };
      const { data } = await api.post('/api/betting-mode', {
        mode: 'SHADOW_REALISTIC', profile,
        starting_balance: Number(startingBalance), goal_balance: Number(goal), configuration,
      });
      setStatus(data.status); setReview(null); setConfigureShadow(false); setReviewShadow(false); setConfirmShadow(false);
    } catch (e) {
      setError(e?.response?.data?.detail?.reasons?.join(', ') || e?.response?.data?.detail?.message || e.message);
    } finally { setBusy(false); }
  }

  async function shadowAction(action) {
    if (action === 'emergency-stop' && !window.confirm('Emergency Stop: block new betting and pause this session?')) return;
    setBusy(true); setError('');
    try {
      if (action === 'emergency-stop') await api.post('/api/betting/emergency-stop');
      else await api.post(`/api/shadow/${action}`);
      await refresh();
    } catch (e) {
      setError(e?.response?.data?.detail?.message || e?.response?.data?.message || e.message);
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

  const mode = status?.mode || 'UNKNOWN';
  const pending = Number(status?.open_real_executions || 0) > 0;
  const shadow = status?.shadow || {};
  const training = readiness?.automatic_training || {};
  const readinessReasons = readiness?.overall?.reasons || [];
  const shadowBlockers = Array.from(new Set([
    ...(training.blockers || []),
    ...readinessReasons,
    ...(readinessReasons.length === 1 && readinessReasons[0] === 'decision_expired'
      ? ['awaiting_fresh_decision'] : []),
  ].filter(Boolean)));
  const selectedRisk = riskProfiles?.[profile] || {};
  const automaticExposureLimit = Math.min(Number(startingBalance || 0),
    Number(selectedRisk.maximum_bet || 0),
    Number(startingBalance || 0) * Number(selectedRisk.maximum_balance_percentage || 0));
  const manualExposure = panels.reduce((n, panel) => n + (panel.enabled ? Number(panel.stake || 0) : 0), 0);
  const manualValid = panels.filter(panel => panel.enabled).every(panel =>
    Number.isSafeInteger(Number(panel.stake))
      && Number(panel.stake) >= Number(selectedRisk.minimum_bet || 1)
      && Number(panel.stake) <= Number(selectedRisk.maximum_bet || 0)
      && Number(panel.cashout) >= 1.01
      && Number(panel.cashout) <= Number(selectedRisk.cashout || 0));
  const riskLimitsAvailable = Number.isFinite(Number(selectedRisk.maximum_bet))
    && Number.isFinite(Number(selectedRisk.minimum_bet))
    && Number.isFinite(Number(selectedRisk.maximum_balance_percentage));
  const configValid = riskLimitsAvailable && startingBalance && goal
    && Number.isFinite(Number(startingBalance)) && Number(goal) > Number(startingBalance)
    && panels.some(panel => panel.enabled)
    && (sessionType !== 'MANUAL' || manualValid && manualExposure <= Number(startingBalance) * Number(selectedRisk.maximum_balance_percentage || 0));
  const badge = mode === 'LIVE_REAL' ? 'LIVE — REAL MONEY'
    : pending ? 'SHADOW — REAL EXECUTION RECONCILIATION REQUIRED'
      : mode === 'UNKNOWN' ? 'CHECKING MODE' : 'SHADOW — NO REAL MONEY';

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
          <input className="mr-2 accent-emerald-400" type="radio" name="betting-mode" disabled={busy || !status} checked={mode === 'SHADOW_REALISTIC'} onChange={selectShadow} />
          SHADOW_REALISTIC
        </label>
        <button type="button" disabled={busy || !status || mode === 'SHADOW_REALISTIC' && status?.shadow_running}
          onClick={selectShadow} className="rounded bg-emerald-700 px-3 py-2 text-sm font-semibold disabled:opacity-50">
          {status?.shadow_running ? 'Shadow active' : 'Start / switch to Shadow'}
        </button>
        <label className={`rounded border px-3 py-2 text-sm ${mode === 'LIVE_REAL' ? 'border-red-400 text-red-200' : 'border-zinc-700 text-zinc-400'}`}>
          <input className="mr-2 accent-red-500" type="radio" name="betting-mode" disabled={busy || !status} checked={mode === 'LIVE_REAL'} onChange={requestLiveReview} />
          LIVE_REAL
        </label>
        <button type="button" disabled={busy || !status || mode === 'LIVE_REAL'} onClick={requestLiveReview}
          className="rounded bg-red-800 px-3 py-2 text-sm font-semibold disabled:opacity-50">
          Review LIVE activation
        </button>
      </div>
    </div>

    {!configureShadow && <div className="mt-3 flex flex-wrap items-end gap-3 border-t border-zinc-800 pt-3">
      <label className="text-xs text-zinc-400">Profile
        <select value={profile} onChange={e => { setProfile(e.target.value); setReviewShadow(false); setConfirmShadow(false); }} className="ml-2 rounded bg-zinc-800 p-2 text-sm text-zinc-100">
          <option value="PROFILE_A">PROFILE_A</option><option value="PROFILE_B">PROFILE_B</option>
        </select>
      </label>
      <label className="text-xs text-zinc-400">Goal (BIF)
        <input type="number" min="1" value={goal} onChange={e => setGoal(e.target.value)} className="ml-2 w-36 rounded bg-zinc-800 p-2 text-sm text-zinc-100" placeholder="Set goal" />
      </label>
      {status?.reason && <span className="text-xs text-zinc-400">{status.reason}</span>}
    </div>}

    {error && <p role="alert" className="mt-3 rounded border border-amber-500/40 bg-amber-950/30 p-2 text-sm text-amber-100">{error}</p>}
    {refreshError && <p className="mt-2 text-xs text-amber-200">Mode status could not refresh: {refreshError}</p>}
    {status && <p className="mt-3 text-xs text-zinc-400">{status.shadow_running ? 'Shadow session is running.' : mode === 'SHADOW_REALISTIC' ? 'Shadow is selected; the session is stopped. Use Start / switch to Shadow to start it.' : 'LIVE activation requires the review and confirmation below.'}</p>}
    {showProgress && <div className="mt-3"><ModelProgressMessage readiness={readiness} /></div>}

      {mode === 'SHADOW_REALISTIC' && status?.shadow_running && <div className="mt-4 rounded-lg border border-emerald-500/30 bg-emerald-950/20 p-3 text-sm">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <span className="font-semibold text-emerald-200">Shadow ledger progress</span>
        <span className="text-zinc-300">{metric(shadow.bets)} bets · {money(shadow.profit)} P/L</span>
      </div>
      <div className="mt-2 grid gap-1 text-xs text-zinc-300 sm:grid-cols-2 lg:grid-cols-5">
        <span>New rounds: {metric(training.new_rounds_since_training)}/{metric(training.minimum_new_rounds)}</span>
        <span>Contiguous: {metric(readiness?.history?.continuous_rounds)}/{metric(readiness?.history?.required_rounds)}</span>
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
      <div className="mt-3 grid gap-2 text-xs sm:grid-cols-2 lg:grid-cols-4">
        <span className="text-zinc-300">Session configuration: {shadow.configuration?.mode || 'UNKNOWN'}</span>
        <span className="text-zinc-300">Current verified real balance: {money(shadow.verified_real_balance)}{shadow.real_balance_observed_at ? ` · ${new Date(shadow.real_balance_observed_at).toLocaleTimeString()}` : ''}</span>
        <span className="text-zinc-300">Starting verified real balance: {money(shadow.starting_balance)}</span>
        <span className="text-zinc-300">Paper balance: {money(shadow.current_balance)}</span>
        <span className="text-zinc-300">Round exposure: {money(shadow.current_round_exposure)}</span>
        <span className="text-zinc-300">Total exposure: {money(shadow.total_exposure)}</span>
        <span className="text-zinc-300">Combined P/L: {money(shadow.profit)}</span>
        <span className="text-zinc-300">Current/last round: {shadow.pending_rounds?.[0] || shadow.latest_round_id || 'WAITING'}</span>
        {[1, 2].map(panel => {
          const config = shadow.configuration?.panels?.find(item => item.panel === panel);
          const summary = shadow.panel_summary?.[String(panel)] || {};
          return <span key={panel} className="rounded bg-zinc-900 p-2 text-zinc-200">
            Panel {panel}: {config ? (config.enabled ? `enabled · ${shadow.configuration?.mode === 'MANUAL' ? money(config.stake) : 'Risk sized'} @ ${money(config.cashout)}x` : 'disabled') : 'UNKNOWN'} · {metric(summary.bets)} bets · {metric(summary.wins)} wins · {metric(summary.losses)} losses · P/L {money(summary.pnl)}
          </span>;
        })}
      </div>
      {shadow.stop_reason === 'GOAL_REACHED' && <div className="mt-3 rounded border border-emerald-400 bg-emerald-500/15 p-3 font-bold text-emerald-200">GOAL_REACHED — automatic executions stopped.</div>}
      {shadow.emergency_stop && <div className="mt-3 rounded border border-rose-400 bg-rose-500/15 p-3 font-bold text-rose-200">EMERGENCY STOP — new betting is blocked.</div>}
      <div className="mt-3 flex flex-wrap gap-2">
        <button disabled={busy || shadow.mode !== 'SHADOW'} onClick={() => shadowAction('pause')} className="rounded bg-amber-700 px-3 py-2 text-xs font-semibold disabled:opacity-40">Pause</button>
        <button disabled={busy || !status?.shadow_running} onClick={() => shadowAction('stop')} className="rounded bg-zinc-700 px-3 py-2 text-xs font-semibold disabled:opacity-40">Stop</button>
        <button disabled={busy || !status?.shadow_running} onClick={() => shadowAction('emergency-stop')} className="rounded bg-rose-800 px-3 py-2 text-xs font-semibold disabled:opacity-40">Emergency Stop</button>
      </div>
      </div>}

    {configureShadow && <div className="mt-4 rounded-lg border border-cyan-500/30 bg-zinc-950/60 p-4" aria-label="Shadow session configuration">
      <h2 className="font-semibold text-cyan-200">Configure SHADOW_REALISTIC session</h2>
      <p className="mt-1 text-xs text-zinc-400">Uses actual completed round outcomes and the shared Decision/Risk pipeline. It never clicks platform Bet or Cashout controls.</p>
      <div className="mt-3 grid gap-3 sm:grid-cols-2">
        <label className="text-xs text-zinc-400">Risk profile
          <select value={profile} onChange={e => { setProfile(e.target.value); setReviewShadow(false); setConfirmShadow(false); }} className="mt-1 block w-full rounded bg-zinc-800 p-2 text-sm text-zinc-100">
            <option value="PROFILE_A">PROFILE_A · 2.00x</option><option value="PROFILE_B">PROFILE_B · 1.50x</option>
          </select>
        </label>
        <label className="text-xs text-zinc-400">Betting configuration
          <select value={sessionType} onChange={e => { setSessionType(e.target.value); setReviewShadow(false); setConfirmShadow(false); }} className="mt-1 block w-full rounded bg-zinc-800 p-2 text-sm text-zinc-100">
            <option value="AUTOMATIC">AUTOMATIC — Risk sizes the combined round exposure</option>
            <option value="MANUAL">MANUAL — I set each panel stake and cashout</option>
          </select>
        </label>
        <label className="text-xs text-zinc-400">Verified starting balance (BIF)
          <input type="number" min="0" value={startingBalance} onChange={e => { setStartingBalance(e.target.value); setReviewShadow(false); }} className="mt-1 block w-full rounded bg-zinc-800 p-2 text-sm text-zinc-100" />
        </label>
        <label className="text-xs text-zinc-400">Goal balance (BIF)
          <input type="number" min="1" value={goal} onChange={e => { setGoal(e.target.value); setReviewShadow(false); }} className="mt-1 block w-full rounded bg-zinc-800 p-2 text-sm text-zinc-100" />
        </label>
      </div>
      <div className="mt-3 grid gap-3 md:grid-cols-2">
        {panels.map((panel, index) => <fieldset key={index} className="rounded border border-zinc-700 p-3">
          <legend className="px-1 text-sm font-semibold">Panel {index + 1}</legend>
          <label className="flex items-center gap-2 text-xs text-zinc-300"><input type="checkbox" checked={panel.enabled} onChange={e => updatePanel(index, 'enabled', e.target.checked)} />Enabled</label>
          <div className="mt-2 grid grid-cols-2 gap-2">
            <label className="text-xs text-zinc-400">Stake (BIF){sessionType === 'AUTOMATIC' && <span className="block text-zinc-500">Allocated from Risk approval</span>}
              <input type="number" min="1" disabled={sessionType === 'AUTOMATIC'} value={panel.stake} onChange={e => updatePanel(index, 'stake', e.target.value)} className="mt-1 w-full rounded bg-zinc-800 p-2 text-sm text-zinc-100 disabled:opacity-50" />
            </label>
            <label className="text-xs text-zinc-400">Cashout target (x)
              <input type="number" min="1.01" max="100" step="0.01" disabled={sessionType === 'AUTOMATIC'} value={sessionType === 'AUTOMATIC' ? (index === 0 ? '2.00' : '1.50') : panel.cashout} onChange={e => updatePanel(index, 'cashout', e.target.value)} className="mt-1 w-full rounded bg-zinc-800 p-2 text-sm text-zinc-100 disabled:opacity-50" />
            </label>
          </div>
        </fieldset>)}
      </div>
      {sessionType === 'MANUAL' && !manualValid && <p className="mt-3 text-xs text-amber-200">Each enabled panel needs a whole stake within {money(selectedRisk.minimum_bet)}–{money(selectedRisk.maximum_bet)} and a cashout at or below this profile’s {selectedRisk.cashout || '—'}x limit.</p>}
      {sessionType === 'MANUAL' && manualValid && manualExposure > Number(startingBalance || 0) * Number(selectedRisk.maximum_balance_percentage || 0) && <p className="mt-3 text-xs text-amber-200">Combined panel stake exceeds this profile’s maximum balance percentage.</p>}
      <div className="mt-3 flex flex-wrap gap-2">
        <button disabled={busy || !configValid} onClick={() => { setReviewShadow(true); setConfirmShadow(false); }} className="rounded bg-cyan-700 px-3 py-2 text-sm font-semibold disabled:opacity-40">Review session</button>
        <button disabled={busy} onClick={() => { setConfigureShadow(false); setReviewShadow(false); }} className="rounded bg-zinc-700 px-3 py-2 text-sm">Cancel</button>
      </div>
      {reviewShadow && <div className="mt-3 rounded border border-amber-500/30 bg-amber-950/20 p-3 text-sm" role="dialog" aria-label="Confirm Shadow session">
        <h3 className="font-semibold text-amber-200">Confirm session configuration</h3>
        <div className="mt-2 grid gap-1 text-xs text-zinc-200 sm:grid-cols-2">
          <span>Mode: {sessionType}</span><span>Risk profile: {profile}</span><span>Platform balance: {money(startingBalance)}</span><span>Goal: {money(goal)}</span>
          <span>Panel 1: {panelText(panels[0], 0, sessionType)}</span><span>Panel 2: {panelText(panels[1], 1, sessionType)}</span>
          <span>Maximum possible round exposure: {sessionType === 'MANUAL' ? money(manualExposure) : `up to ${money(automaticExposureLimit)}, further capped by Decision/Risk approval`}</span>
          <span>Session loss limit: {money(selectedRisk.maximum_session_loss)} · maximum {((Number(selectedRisk.maximum_balance_percentage || 0)) * 100).toFixed(0)}% of balance · {selectedRisk.maximum_consecutive_losses ?? '—'} consecutive losses</span><span>Emergency Stop and duplicate/pending-result checks enforced</span><span>Execution: SHADOW_REALISTIC · no real controls</span>
        </div>
        <label className="mt-3 flex items-center gap-2 text-xs text-zinc-200"><input type="checkbox" checked={confirmShadow} onChange={e => setConfirmShadow(e.target.checked)} />I reviewed this session and confirm the configuration.</label>
        <button disabled={busy || !confirmShadow} onClick={startShadow} className="mt-3 rounded bg-emerald-700 px-4 py-2 font-semibold disabled:opacity-40">Start SHADOW session</button>
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
