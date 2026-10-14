import { useCallback, useEffect, useMemo, useState } from 'react';
import { ShieldAlert, ShieldCheck } from 'lucide-react';
import { api } from '../services/api.js';

const activationPhrase = 'ENABLE LIVE BETTING';

function reviewFingerprint({ profile, executionPanel, stakeAmount, currency,
  winEnabled, winLimit, lossEnabled, lossLimit }) {
  return JSON.stringify({ profile, executionPanel, stakeAmount, currency,
    winEnabled, winLimit, lossEnabled, lossLimit });
}

function formatMoney(value) {
  if (value == null || value === '') return 'UNVERIFIED';
  return Number.isFinite(Number(value)) ? `${Number(value).toLocaleString()} BIF` : 'UNVERIFIED';
}

export default function LiveActivation() {
  const [status, setStatus] = useState(null);
  const [review, setReview] = useState(null);
  const [reviewedFingerprint, setReviewedFingerprint] = useState('');
  const [profile, setProfile] = useState('PROFILE_A');
  const [executionPanel, setExecutionPanel] = useState('');
  const [stakeAmount, setStakeAmount] = useState('');
  const [currency, setCurrency] = useState('');
  const [winEnabled, setWinEnabled] = useState(false);
  const [winLimit, setWinLimit] = useState('');
  const [lossEnabled, setLossEnabled] = useState(false);
  const [lossLimit, setLossLimit] = useState('');
  const [confirmation, setConfirmation] = useState('');
  const [realFundsCanBeLost, setRealFundsCanBeLost] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  const currentFingerprint = useMemo(() => reviewFingerprint({
    profile, executionPanel, stakeAmount, currency,
    winEnabled, winLimit, lossEnabled, lossLimit,
  }), [profile, executionPanel, stakeAmount, currency, winEnabled, winLimit, lossEnabled, lossLimit]);
  const reviewCurrent = Boolean(review && reviewedFingerprint === currentFingerprint);
  const modeState = status?.mode || {};
  const liveReadiness = status?.live?.readiness || modeState.live?.readiness;
  const liveActive = modeState.mode === 'LIVE_REAL' && modeState.live_authorized === true;
  const automaticOn = Boolean(modeState.live?.live_active && modeState.live?.automatic_betting
    && modeState.live_authorized);
  const blockers = reviewCurrent ? (review?.blockers || []) : [];

  const refreshStatus = useCallback(async () => {
    try {
      const [modeResponse, liveResponse, currentResponse] = await Promise.all([
        api.get('/api/betting-mode/status'), api.get('/api/live/status'), api.get('/api/live/current'),
      ]);
      setStatus({ mode: modeResponse.data.mode, live: liveResponse.data, current: currentResponse.data });
    } catch {
      setError('Live execution status is unavailable from the backend.');
    }
  }, []);

  const requestReview = useCallback(async () => {
    setBusy(true);
    setError('');
    setConfirmation('');
    try {
      const params = {
        profile,
        win_limit_enabled: winEnabled,
        session_loss_limit_enabled: lossEnabled,
      };
      if (executionPanel !== '') params.execution_panel = Number(executionPanel);
      if (stakeAmount !== '') params.stake_amount = Number(stakeAmount);
      if (currency !== '') params.currency = currency;
      if (winEnabled && winLimit !== '') params.win_limit = Number(winLimit);
      if (lossEnabled && lossLimit !== '') params.session_loss_limit = Number(lossLimit);
      const { data } = await api.get('/api/betting-mode/live-review', { params });
      setReview(data);
      setReviewedFingerprint(currentFingerprint);
    } catch (failure) {
      setReview(null);
      setReviewedFingerprint('');
      const detail = failure?.response?.data?.detail;
      setError(typeof detail === 'string' ? detail : detail?.reason || 'LIVE readiness review failed.');
    } finally {
      setBusy(false);
    }
  }, [currentFingerprint, currency, executionPanel, lossEnabled, lossLimit,
    profile, stakeAmount, winEnabled, winLimit]);

  useEffect(() => {
    refreshStatus();
    const timer = window.setInterval(refreshStatus, 5000);
    return () => window.clearInterval(timer);
  }, [refreshStatus]);

  async function activateLive() {
    if (!reviewCurrent || review?.live_available !== true
        || confirmation !== activationPhrase || !realFundsCanBeLost) return;
    setBusy(true);
    setError('');
    try {
      const profileReview = review.profile || {};
      const { data } = await api.post('/api/betting-mode', {
        mode: 'LIVE_REAL',
        review_id: review.review_id,
        confirmation,
        real_funds_can_be_lost: realFundsCanBeLost,
        profile,
        execution_panel: Number(executionPanel),
        stake_amount: Number(stakeAmount),
        currency,
        real_platform_balance: review.platform_balance?.balance,
        cashout_target: profileReview.cashout_target,
        minimum_bet: profileReview.minimum_bet,
        maximum_bet: profileReview.maximum_bet,
        session_loss_limit_enabled: review.session_loss_limit_enabled,
        session_loss_limit: review.session_loss_limit,
        win_limit_enabled: review.win_limit_enabled,
        win_limit: review.win_limit,
        goal_balance: review.goal_balance,
      });
      setStatus((previous) => ({ ...previous, mode: data.status }));
      setReview(null);
      setReviewedFingerprint('');
      setConfirmation('');
      setRealFundsCanBeLost(false);
      await refreshStatus();
    } catch (failure) {
      const detail = failure?.response?.data?.detail;
      const message = typeof detail === 'string' ? detail
        : detail?.reasons?.join(', ') || detail?.error || 'LIVE activation was rejected by the backend.';
      setError(message);
      await refreshStatus();
    } finally {
      setBusy(false);
    }
  }

  async function disarmLive() {
    setBusy(true);
    setError('');
    try {
      await api.post('/api/live/stop');
      setReview(null);
      setReviewedFingerprint('');
      await refreshStatus();
    } catch {
      setError('LIVE disarm failed. The backend status must be checked before further action.');
    } finally {
      setBusy(false);
    }
  }

  async function emergencyStop() {
    setBusy(true);
    setError('');
    try {
      await api.post('/api/live/emergency-stop');
      setReview(null);
      setReviewedFingerprint('');
      await refreshStatus();
    } catch {
      setError('Emergency stop request failed. Check the backend risk status immediately.');
    } finally {
      setBusy(false);
    }
  }

  const profileReview = review?.profile || {};
  const reviewIsReady = reviewCurrent && review?.live_available === true;
  const liveSessionSnapshot = modeState.live?.session?.snapshot || {};
  const liveSessionMetrics = modeState.live?.session?.status || {};

  return <section className="space-y-4">
    <div className={`rounded-xl border p-5 ${automaticOn ? 'border-rose-500/40 bg-rose-500/5' : 'border-amber-500/30 bg-amber-500/5'}`}>
      <div className="flex items-center gap-2 font-semibold text-amber-200">
        <ShieldCheck size={18} />Automatic real-money execution: {automaticOn ? 'ON' : 'OFF'}
      </div>
      <p className="mt-2 text-sm text-zinc-300">Mode: <b>{modeState.mode || 'UNKNOWN'}</b> · Authorization: <b>{liveActive ? 'ACTIVE' : 'OFF'}</b></p>
      <p className="mt-2 text-xs text-zinc-400">A backend readiness review and explicit confirmation are required for activation. Restart, browser recovery, or login expiry never resumes LIVE automatically.</p>
      {modeState.reason && <p className="mt-2 text-xs text-zinc-400">Backend status: {modeState.reason}</p>}
      {liveReadiness?.reasons?.length > 0 && <ul className="mt-3 list-disc space-y-1 pl-5 text-xs text-amber-200">
        {liveReadiness.reasons.map((reason, index) => <li key={`live-ready-${index}`}>{reason}</li>)}
      </ul>}
      <div className="mt-4 grid gap-2 text-xs sm:grid-cols-2 xl:grid-cols-4">
        <div className="rounded bg-zinc-950/60 p-2">Selected panel: {liveSessionSnapshot.execution_panel == null ? '—' : `Panel ${liveSessionSnapshot.execution_panel + 1}`}</div>
        <div className="rounded bg-zinc-950/60 p-2">Stake: {liveSessionSnapshot.stake_amount == null ? '—' : `${liveSessionSnapshot.stake_amount} ${liveSessionSnapshot.currency || ''}`}</div>
        <div className="rounded bg-zinc-950/60 p-2">Cashout: {liveSessionSnapshot.cashout_target == null ? '2.00x required' : `${liveSessionSnapshot.cashout_target}x`}</div>
        <div className="rounded bg-zinc-950/60 p-2">Execution: {status?.current?.current_execution?.status || (automaticOn ? 'ARMED · PROVIDER GATES RECHECKED PER ACTION' : 'OFF')}</div>
        <div className="rounded bg-zinc-950/60 p-2">Session P/L: {liveSessionMetrics.profit ?? '—'} {liveSessionSnapshot.currency || ''}</div>
        <div className="rounded bg-zinc-950/60 p-2">WIN / LOSS: {liveSessionMetrics.wins ?? 0} / {liveSessionMetrics.losses ?? 0}</div>
        <div className="rounded bg-zinc-950/60 p-2 sm:col-span-2">Current balance: {liveSessionMetrics.current_balance == null ? 'UNVERIFIED' : formatMoney(liveSessionMetrics.current_balance)}</div>
      </div>
    </div>

    {error && <div role="status" className="rounded border border-amber-500/30 bg-amber-500/10 p-3 text-sm text-amber-200">{error}</div>}

    <div className="rounded-xl border border-zinc-800 bg-zinc-900/60 p-5">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h2 className="font-semibold text-zinc-100">LIVE_REAL activation review</h2>
          <p className="mt-1 text-sm text-zinc-400">Choose the panel, stake, and verified account currency. This review reads the existing Aviator frame and never submits a bet.</p>
        </div>
        <button type="button" onClick={requestReview} disabled={busy}
          className="rounded-lg border border-zinc-700 px-3 py-2 text-sm text-zinc-200 hover:bg-zinc-800 disabled:opacity-50">
          {busy ? 'Checking…' : 'Refresh readiness review'}
        </button>
      </div>

      <div className="mt-4 grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
        <label className="text-sm text-zinc-300">Risk profile
          <select value={profile} onChange={(event) => setProfile(event.target.value)} disabled={liveActive || busy}
            className="mt-1 block w-full rounded border border-zinc-700 bg-zinc-950 px-3 py-2">
            <option value="PROFILE_A">PROFILE_A</option>
            <option value="PROFILE_B">PROFILE_B</option>
          </select>
        </label>
        <label className="text-sm text-zinc-300">Execution panel
          <select value={executionPanel} onChange={(event) => setExecutionPanel(event.target.value)} disabled={liveActive || busy}
            className="mt-1 block w-full rounded border border-zinc-700 bg-zinc-950 px-3 py-2">
            <option value="">Choose a panel</option><option value="0">Panel 1</option><option value="1">Panel 2</option>
          </select>
        </label>
        <label className="text-sm text-zinc-300">Stake amount
          <input type="number" min={profileReview.minimum_bet || 1} max={profileReview.maximum_bet || undefined}
            step="1" value={stakeAmount} onChange={(event) => setStakeAmount(event.target.value)} disabled={liveActive || busy}
            className="mt-1 block w-full rounded border border-zinc-700 bg-zinc-950 px-3 py-2" />
          <span className="mt-1 block text-xs text-zinc-500">Allowed profile range: {profileReview.minimum_bet ?? '—'}–{profileReview.maximum_bet ?? '—'}</span>
        </label>
        <label className="text-sm text-zinc-300">Account currency
          <select value={currency} onChange={(event) => setCurrency(event.target.value)} disabled={liveActive || busy}
            className="mt-1 block w-full rounded border border-zinc-700 bg-zinc-950 px-3 py-2">
            <option value="">Choose verified account currency</option>
            {review?.platform_balance?.currency && <option value={review.platform_balance.currency}>{review.platform_balance.currency}</option>}
          </select>
          <span className="mt-1 block text-xs text-zinc-500">Only the currency observed in the authenticated account is selectable.</span>
        </label>
        <div className="rounded border border-zinc-800 bg-zinc-950/60 p-3 text-sm">
          <div className="text-xs uppercase text-zinc-500">Read-only platform balance</div>
          <div className="mt-1 font-semibold text-zinc-100">{review?.platform_balance?.verified ? formatMoney(review.platform_balance.balance) : 'UNVERIFIED'}</div>
          <div className="mt-1 text-xs text-zinc-500">{review?.platform_balance?.observed_at || 'No current observation'}</div>
        </div>
        <div className="rounded border border-zinc-800 bg-zinc-950/60 p-3 text-sm">
          <div className="text-xs uppercase text-zinc-500">Profile execution settings</div>
          <div className="mt-1 text-zinc-200">Required cashout: 2.00x</div>
          <div className="text-zinc-400">Default stake: {profileReview.default_stake != null ? formatMoney(profileReview.default_stake) : '—'}</div>
          <div className="text-zinc-400">Allowed stake: {profileReview.minimum_bet != null && profileReview.maximum_bet != null ? `${formatMoney(profileReview.minimum_bet)} – ${formatMoney(profileReview.maximum_bet)}` : '—'}</div>
        </div>
        <label className="flex items-center gap-2 text-sm text-zinc-300"><input type="checkbox" checked={winEnabled} disabled={liveActive || busy} onChange={(event) => setWinEnabled(event.target.checked)} />Enable session WIN limit</label>
        <label className="text-sm text-zinc-400">WIN limit amount<input type="number" min="1" value={winLimit} disabled={!winEnabled || liveActive || busy} onChange={(event) => setWinLimit(event.target.value)} className="mt-1 block w-full rounded border border-zinc-700 bg-zinc-950 px-3 py-2 text-zinc-100" /></label>
        <label className="flex items-center gap-2 text-sm text-zinc-300"><input type="checkbox" checked={lossEnabled} disabled={liveActive || busy} onChange={(event) => setLossEnabled(event.target.checked)} />Enable session LOSS limit</label>
        <label className="text-sm text-zinc-400">LOSS limit amount<input type="number" min="1" value={lossLimit} disabled={!lossEnabled || liveActive || busy} onChange={(event) => setLossLimit(event.target.value)} className="mt-1 block w-full rounded border border-zinc-700 bg-zinc-950 px-3 py-2 text-zinc-100" /></label>
        <div className="rounded border border-zinc-800 bg-zinc-950/60 p-3 text-sm sm:col-span-2 xl:col-span-3">
          <div className="text-xs uppercase text-zinc-500">Risk guardrails from backend profile</div>
          <div className="mt-1 text-zinc-300">Session loss cap: {profileReview.session_loss_limit != null ? formatMoney(profileReview.session_loss_limit) : '—'} · Maximum consecutive losses: {profileReview.maximum_consecutive_losses ?? '—'} · Maximum balance exposure: {profileReview.maximum_balance_percentage != null ? `${Number(profileReview.maximum_balance_percentage) * 100}%` : '—'}</div>
        </div>
      </div>

      <div className="mt-4 rounded-lg border border-zinc-800 bg-zinc-950/50 p-3">
        <div className="font-semibold text-zinc-200">Readiness: {reviewCurrent ? (reviewIsReady ? 'READY FOR EXPLICIT REVIEW' : 'BLOCKED') : 'REVIEW REQUIRED'}</div>
        {!reviewCurrent && <p className="mt-1 text-sm text-zinc-400">Any settings change invalidates the displayed review. Refresh it before activation.</p>}
        {reviewCurrent && blockers.length > 0 && <ul className="mt-2 list-disc space-y-1 pl-5 text-sm text-amber-200">
          {blockers.map((reason, index) => <li key={`${reason}-${index}`}>{reason}</li>)}
        </ul>}
        {reviewCurrent && blockers.length === 0 && <p className="mt-2 text-sm text-emerald-200">All backend checks passed. Type the exact confirmation below to arm LIVE_REAL.</p>}
      </div>

      {reviewIsReady && <div className="mt-4 space-y-3">
        <label className="flex items-start gap-2 text-sm text-rose-200">
          <input type="checkbox" checked={realFundsCanBeLost} onChange={(event) => setRealFundsCanBeLost(event.target.checked)} />
          I understand real funds can be lost if LIVE_REAL execution is activated.
        </label>
        <label className="min-w-64 flex-1 text-sm text-zinc-300">Type {activationPhrase}
          <input value={confirmation} onChange={(event) => setConfirmation(event.target.value)} autoComplete="off"
            className="mt-1 block w-full rounded border border-zinc-700 bg-zinc-950 px-3 py-2 text-zinc-100" />
        </label>
      </div>}
      <button type="button" onClick={activateLive}
        disabled={!reviewIsReady || busy || confirmation !== activationPhrase || !realFundsCanBeLost || liveActive}
        className="mt-4 rounded-lg border border-rose-500/40 px-4 py-2 font-semibold text-rose-200 hover:bg-rose-500/10 disabled:cursor-not-allowed disabled:opacity-40">
        START LIVE REAL
      </button>
    </div>

    <div className="flex flex-wrap gap-2">
      <button type="button" disabled={!liveActive || busy} onClick={disarmLive}
        className="rounded bg-zinc-700 px-4 py-2 font-semibold text-white hover:bg-zinc-600 disabled:cursor-not-allowed disabled:opacity-50">OFF · Disarm LIVE</button>
      <button type="button" disabled={busy} onClick={emergencyStop}
        className="inline-flex items-center gap-2 rounded bg-red-900 px-4 py-2 font-semibold text-red-100 hover:bg-red-800 disabled:opacity-50">
        <ShieldAlert size={17} />Emergency stop
      </button>
    </div>
  </section>;
}
