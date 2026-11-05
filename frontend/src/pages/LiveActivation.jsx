import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { ShieldAlert, ShieldCheck } from 'lucide-react';
import { api } from '../services/api.js';
import { statusRequestError } from '../services/statusRequestError.js';

const activationPhrase = 'ENABLE LIVE BETTING';
const fingerprint = ({ currency, winEnabled, winLimit }) =>
  JSON.stringify({ currency, winEnabled, winLimit });

function formatMoney(value, currency = '') {
  if (value == null || value === '') return 'UNVERIFIED';
  const amount = Number(value);
  return Number.isFinite(amount) ? `${amount.toLocaleString()} ${currency}`.trim() : 'UNVERIFIED';
}

function DetailValue({ label, value }) {
  return <div>
    <div className="text-xs uppercase tracking-wide text-zinc-500">{label}</div>
    <div className="mt-1 text-sm text-zinc-200">{value == null || value === '' ? 'UNKNOWN' : value}</div>
  </div>;
}

export default function LiveActivation({ dashboard = null }) {
  const statusRefreshInFlight = useRef(false);
  const [status, setStatus] = useState(null);
  const [review, setReview] = useState(null);
  const [reviewedFingerprint, setReviewedFingerprint] = useState('');
  const [currency, setCurrency] = useState('');
  const [winEnabled, setWinEnabled] = useState(false);
  const [winLimit, setWinLimit] = useState('');
  const [confirmation, setConfirmation] = useState('');
  const [realFundsCanBeLost, setRealFundsCanBeLost] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [statusError, setStatusError] = useState('');
  const [showLiveDetails, setShowLiveDetails] = useState(false);

  const currentFingerprint = useMemo(() => fingerprint({ currency, winEnabled, winLimit }),
    [currency, winEnabled, winLimit]);
  const reviewCurrent = Boolean(review && reviewedFingerprint === currentFingerprint);
  const modeState = status?.mode || {};
  const liveReadiness = status?.live?.readiness || modeState.live?.readiness;
  const liveActive = modeState.mode === 'LIVE_REAL' && modeState.live_authorized === true;
  const automaticOn = Boolean(modeState.live?.live_active && modeState.live?.automatic_betting
    && modeState.live_authorized);
  const blockers = reviewCurrent ? (review?.blockers || []) : [];
  const dashboardReadiness = dashboard?.execution_readiness || {};
  const aviatorReadiness = dashboard?.shared_aviator_readiness || {};
  const cashoutConfiguration = dashboard?.cashout_configuration || {};
  const financials = dashboard?.financials || {};
  const balance = dashboard?.balance || {};
  const executionIntents = dashboard?.execution_intents || [];
  const dashboardPanels = cashoutConfiguration.panels || [];
  const readinessStatus = dashboardReadiness.status || liveReadiness?.status || 'UNKNOWN';
  const readinessReasons = [...new Set([
    ...(dashboardReadiness.blockers || []),
    ...(liveReadiness?.reasons || []),
  ])];
  const emergencyStopStatus = modeState.emergency_stop?.latched == null
    ? 'UNKNOWN' : modeState.emergency_stop.latched ? 'LATCHED' : 'CLEAR';
  const authorizationStatus = modeState.mode == null ? 'UNKNOWN' : liveActive ? 'ACTIVE' : 'OFF';
  const automaticStatus = status?.mode == null ? 'UNKNOWN' : automaticOn ? 'ON' : 'OFF';

  const refreshStatus = useCallback(async () => {
    if (statusRefreshInFlight.current) return;
    statusRefreshInFlight.current = true;
    try {
      const [modeResponse, liveResponse, currentResponse] = await Promise.all([
        api.get('/api/betting-mode/status'), api.get('/api/live/status'), api.get('/api/live/current'),
      ]);
      setStatus({ mode: modeResponse.data.mode, live: liveResponse.data, current: currentResponse.data });
      setStatusError('');
    } catch (failure) {
      setStatus(null);
      setStatusError(statusRequestError(failure, 'Live execution status'));
    } finally {
      statusRefreshInFlight.current = false;
    }
  }, []);

  async function performLiveAction(action, fallbackMessage) {
    setBusy(true);
    setError('');
    try {
      await action();
      await refreshStatus();
    } catch (failure) {
      const detail = failure?.response?.data?.detail;
      setError(typeof detail === 'string' ? detail : fallbackMessage);
    } finally {
      setBusy(false);
    }
  }

  const readBalance = () => performLiveAction(
    () => api.get('/api/live/balance'), 'Balance read failed. Check the live status before retrying.',
  );
  const refreshCashout = () => performLiveAction(
    () => api.post('/api/live/cashout/refresh'), 'Cashout verification failed. Check panel status before retrying.',
  );

  const requestReview = useCallback(async () => {
    setBusy(true);
    setError('');
    setConfirmation('');
    try {
      const makeRequest = async (accountCurrency) => {
        const params = { win_limit_enabled: winEnabled };
        if (accountCurrency) params.currency = accountCurrency;
        if (winEnabled && winLimit !== '') params.win_limit = Number(winLimit);
        return (await api.get('/api/betting-mode/live-review', { params })).data;
      };
      let selectedCurrency = currency;
      let data = await makeRequest(selectedCurrency);
      if (!selectedCurrency && data?.platform_balance?.currency) {
        selectedCurrency = data.platform_balance.currency;
        data = await makeRequest(selectedCurrency);
        setCurrency(selectedCurrency);
      }
      setReview(data);
      setReviewedFingerprint(fingerprint({ currency: selectedCurrency, winEnabled, winLimit }));
    } catch (failure) {
      setReview(null);
      setReviewedFingerprint('');
      const detail = failure?.response?.data?.detail;
      setError(typeof detail === 'string' ? detail : detail?.reason || 'LIVE readiness review failed.');
    } finally {
      setBusy(false);
    }
  }, [currency, winEnabled, winLimit]);

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
      const { data } = await api.post('/api/betting-mode', {
        mode: 'LIVE_REAL',
        review_id: review.review_id,
        confirmation,
        real_funds_can_be_lost: realFundsCanBeLost,
        real_platform_balance: review.platform_balance?.balance,
        cashout_target: 2.0,
        execution_panel: review.execution_panel,
        stake_amount: review.stake_amount,
        currency: review.currency,
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
      setError('LIVE disarm failed. Check the backend status before further action.');
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
      setError('Emergency stop request failed. Check the backend status immediately.');
    } finally {
      setBusy(false);
    }
  }

  const reviewIsReady = reviewCurrent && review?.live_available === true;
  const liveSessionSnapshot = modeState.live?.session?.snapshot || {};
  const liveSessionMetrics = modeState.live?.session?.status || {};
  const accountCurrency = review?.platform_balance?.currency || currency;

  return <section aria-label="Real-money controls">
    <div className={automaticOn ? 'rounded-xl border border-rose-500/40 bg-rose-500/5 p-5' : 'rounded-xl border border-amber-500/30 bg-amber-500/5 p-5'}>
      <div className="flex flex-wrap items-center justify-between gap-4">
        <div>
          <h2 className="flex items-center gap-2 font-semibold text-amber-200">
            <ShieldCheck size={18} />Automatic real-money execution: {automaticStatus}
          </h2>
          <p className="mt-2 text-sm text-zinc-300">
            Mode: <b>{modeState.mode || 'UNKNOWN'}</b> · Authorization: <b>{authorizationStatus}</b> · Emergency stop: <b>{emergencyStopStatus}</b>
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <button type="button" aria-expanded={showLiveDetails} aria-controls="real-money-details"
            onClick={() => setShowLiveDetails((visible) => !visible)}
            className="rounded-lg border border-zinc-700 px-3 py-2 text-sm text-zinc-200 hover:bg-zinc-800">
            {showLiveDetails ? 'Hide details' : 'View details'}
          </button>
          <button type="button" disabled={!liveActive || busy} onClick={disarmLive}
            className="rounded border border-zinc-700 px-3 py-2 text-sm text-zinc-200 hover:bg-zinc-800 disabled:cursor-not-allowed disabled:opacity-50">
            OFF · Disarm LIVE
          </button>
          <button type="button" disabled={busy} onClick={emergencyStop}
            className="inline-flex items-center gap-2 rounded bg-red-900 px-3 py-2 text-sm font-semibold text-red-100 hover:bg-red-800 disabled:opacity-50">
            <ShieldAlert size={17} />Emergency stop
          </button>
        </div>
      </div>

      {statusError && <div role="status" className="mt-4 rounded border border-amber-500/30 bg-amber-500/10 p-3 text-sm text-amber-200">{statusError}</div>}
      {error && <div role="status" className="mt-4 rounded border border-amber-500/30 bg-amber-500/10 p-3 text-sm text-amber-200">{error}</div>}

      <div id="real-money-details" hidden={!showLiveDetails}>
        <div className="mt-5 border-t border-zinc-800 pt-5">
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div>
              <h3 className="font-semibold text-zinc-100">LIVE_REAL readiness and platform status</h3>
              <p className="mt-1 text-sm text-zinc-400">Readiness: {readinessStatus} · Cashout target: 2.00x · Platform acknowledgement: {cashoutConfiguration.platform_acknowledged === true ? 'confirmed' : 'unverified'}</p>
            </div>
            <div className="flex flex-wrap gap-2">
              <button type="button" onClick={readBalance} disabled={busy}
                className="rounded-lg border border-zinc-700 px-3 py-2 text-sm text-zinc-200 hover:bg-zinc-800 disabled:opacity-50">Read balance</button>
              <button type="button" onClick={refreshCashout} disabled={busy || liveActive}
                className="rounded-lg border border-zinc-700 px-3 py-2 text-sm text-zinc-200 hover:bg-zinc-800 disabled:opacity-50">
                {busy ? 'Verifying…' : 'Set and verify both panels · 2.00x'}
              </button>
            </div>
          </div>
          <p className="mt-3 text-xs text-zinc-400">A fresh backend review and explicit confirmation are required for activation. Restart, browser recovery, or login expiry never resumes LIVE automatically. Rule selections are not accepted bets; this review does not submit a bet.</p>
          {modeState.reason && <p className="mt-2 text-xs text-zinc-400">Backend status: {modeState.reason}</p>}
          {readinessReasons.length > 0 && <ul className="mt-3 list-disc space-y-1 pl-5 text-xs text-amber-200">
            {readinessReasons.map((reason, index) => <li key={'live-ready-' + index}>{reason}</li>)}
          </ul>}

          <div className="mt-5 grid gap-x-5 gap-y-4 sm:grid-cols-2 xl:grid-cols-4">
            <DetailValue label="Execution" value={!status?.mode ? 'UNKNOWN' : status?.current?.current_execution?.status || (automaticOn ? 'ARMED · PROVIDER GATES RECHECKED PER ACTION' : 'OFF')} />
            <DetailValue label="Selected panel" value={liveSessionSnapshot.execution_panel == null ? '—' : 'Panel ' + (liveSessionSnapshot.execution_panel + 1)} />
            <DetailValue label="Fixed stake" value={liveSessionSnapshot.stake_amount == null ? '—' : liveSessionSnapshot.stake_amount + ' ' + (liveSessionSnapshot.currency || '')} />
            <DetailValue label="Cashout" value={liveSessionSnapshot.cashout_target == null ? '2.00x required' : liveSessionSnapshot.cashout_target + 'x'} />
            <DetailValue label="Session P/L" value={(liveSessionMetrics.profit ?? '—') + ' ' + (liveSessionSnapshot.currency || '')} />
            <DetailValue label="Session WIN / LOSS" value={(liveSessionMetrics.wins ?? 0) + ' / ' + (liveSessionMetrics.losses ?? 0)} />
            <DetailValue label="LIVE session balance" value={liveSessionMetrics.current_balance == null ? 'UNVERIFIED' : formatMoney(liveSessionMetrics.current_balance, liveSessionSnapshot.currency)} />
            {dashboard && <>
            <DetailValue label="Shared browser" value={aviatorReadiness.browser_connected} />
            <DetailValue label="Aviator page" value={aviatorReadiness.aviator_page_ready} />
            <DetailValue label="Game iframe" value={aviatorReadiness.game_iframe_ready} />
            <DetailValue label="History observer" value={aviatorReadiness.history_observer_ready} />
            <DetailValue label="Betting controls" value={aviatorReadiness.betting_controls_ready} />
            <DetailValue label="Panel readiness detail" value={aviatorReadiness.betting_ui_configuration?.error_reason || '—'} />
            <DetailValue label="Cashout configuration" value={aviatorReadiness.cashout_configured} />
            <DetailValue label="Cashout readback age" value={aviatorReadiness.cashout_initialization?.age_seconds == null ? 'unknown' : aviatorReadiness.cashout_initialization.age_seconds + 's'} />
            <DetailValue label="Starting balance" value={financials.starting_balance ?? 'UNAVAILABLE'} />
            <DetailValue label="Current verified balance" value={balance.verified ? formatMoney(balance.balance, balance.currency) : 'UNVERIFIED'} />
            <DetailValue label="Balance change" value={financials.balance_change ?? 'UNAVAILABLE'} />
            <DetailValue label="Realized betting P/L" value={dashboard.realized_betting_pl == null ? 'UNAVAILABLE' : dashboard.realized_betting_pl + ' ' + (financials.currency || balance.currency || 'currency unverified')} />
            <DetailValue label="Confirmed WIN / LOSS" value={(financials.win_count ?? 0) + ' / ' + (financials.loss_count ?? 0)} />
            <DetailValue label="Profit target" value={dashboard?.session_controls?.profit_target ?? 'Not set'} />
            <DetailValue label="Balance provider" value={balance.provider_identity || 'unverified'} />
            <DetailValue label="Application session" value={balance.application_session_id || balance.session_identity || 'unassigned'} />
            </>}
          </div>

          {dashboard && <>
          <div className="mt-5 grid gap-4 sm:grid-cols-2">
            {[0, 1].map((index) => {
              const panel = dashboardPanels[index] || {};
              return <DetailValue key={'cashout-panel-' + index} label={'Panel ' + (index + 1) + ' cashout'}
                value={(panel.status || 'UNKNOWN') + ' · Auto Bet ' + (panel.auto_bet || 'unknown') + ' · Auto Cashout ' + (panel.auto_cashout || 'unknown') + ' · ' + (panel.multiplier || '—') + 'x'} />;
            })}
          </div>

          {executionIntents.length > 0 && <details className="mt-5">
            <summary className="cursor-pointer text-sm font-semibold">Durable execution intents (blocked)</summary>
            <div className="mt-3 space-y-3 text-xs">{executionIntents.map((intent) => <div key={intent.intent_id}>
              <div>{intent.execution_status} · target round #{intent.target_round_index} · stake {intent.requested_stake ?? '—'} · cashout {intent.cashout_target}x</div>
              <div className="mt-1 text-zinc-500">Rules: {intent.selected_rule_ids?.join(', ') || '—'} · blockers: {intent.blockers?.join(', ')}</div>
            </div>)}</div>
          </details>}
          </>}
        </div>

        <div className="mt-5 border-t border-zinc-800 pt-5">
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div>
              <h3 className="font-semibold text-zinc-100">LIVE_REAL activation review</h3>
              <p className="mt-1 text-sm text-zinc-400">Readiness: {reviewCurrent ? (reviewIsReady ? 'READY FOR EXPLICIT REVIEW' : 'BLOCKED') : 'REVIEW REQUIRED'}</p>
              <p className="mt-1 text-xs text-zinc-500">Stake comes from the Rules Engine’s fixed simulated stake. The review reads the authenticated account and never submits a bet.</p>
            </div>
            <button type="button" onClick={requestReview} disabled={busy}
              className="rounded-lg border border-zinc-700 px-3 py-2 text-sm text-zinc-200 hover:bg-zinc-800 disabled:opacity-50">
              {busy ? 'Checking…' : 'Refresh readiness review'}
            </button>
          </div>

          <div className="mt-4 grid gap-4 sm:grid-cols-2 xl:grid-cols-3">
            <DetailValue label="Fixed simulated stake (RWF)"
              value={review?.stake_amount == null ? 'Not configured' : formatMoney(review.stake_amount, review.stake_configuration?.requested_stake_currency || 'RWF')} />
            <DetailValue label="Selected execution panel"
              value={review?.execution_panel == null ? review?.stake_configuration?.reason || 'Not selected in Rules Engine' : 'Panel ' + (review.execution_panel + 1)} />
            <label className="text-sm text-zinc-300">Account currency
              <select value={currency} onChange={(event) => setCurrency(event.target.value)} disabled={liveActive || busy}
                className="mt-1 block w-full rounded border border-zinc-700 bg-zinc-950 px-3 py-2">
                <option value="">Choose verified account currency</option>
                {review?.platform_balance?.currency && <option value={review.platform_balance.currency}>{review.platform_balance.currency}</option>}
              </select>
              <span className="mt-1 block text-xs text-zinc-500">Only currency read from the authenticated account can be used.</span>
            </label>
            <DetailValue label="Read-only platform balance"
              value={review?.platform_balance?.verified ? formatMoney(review.platform_balance.balance, accountCurrency) : 'UNVERIFIED'} />
            <label className="flex items-center gap-2 text-sm text-zinc-300"><input type="checkbox" checked={winEnabled} disabled={liveActive || busy} onChange={(event) => setWinEnabled(event.target.checked)} />Enable session WIN target</label>
            <label className="text-sm text-zinc-400">WIN target amount<input type="number" min="1" value={winLimit} disabled={!winEnabled || liveActive || busy} onChange={(event) => setWinLimit(event.target.value)} className="mt-1 block w-full rounded border border-zinc-700 bg-zinc-950 px-3 py-2 text-zinc-100" /></label>
          </div>

          <div className="mt-4 text-sm text-zinc-300">Review result: <b>{reviewCurrent ? (reviewIsReady ? 'READY FOR EXPLICIT REVIEW' : 'BLOCKED') : 'REVIEW REQUIRED'}</b></div>
          {!reviewCurrent && <p className="mt-1 text-xs text-zinc-400">Any setting change invalidates this review. Refresh it before activation.</p>}
          {reviewCurrent && blockers.length > 0 && <ul className="mt-2 list-disc space-y-1 pl-5 text-xs text-amber-200">
            {blockers.map((reason, index) => <li key={reason + '-' + index}>{reason}</li>)}
          </ul>}
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
      </div>
    </div>
  </section>;
}
