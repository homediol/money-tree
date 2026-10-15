import { useCallback, useEffect, useMemo, useState } from 'react';
import { ShieldAlert, ShieldCheck } from 'lucide-react';
import { api } from '../services/api.js';

const activationPhrase = 'ENABLE LIVE BETTING';

function reviewFingerprint({ profile, executionPanel, stakeAmount, currency,
  winEnabled, winLimit, lossEnabled, lossLimit, winCountEnabled,
  winCountLimit, lossCountEnabled, lossCountLimit }) {
  return JSON.stringify({ profile, executionPanel, stakeAmount, currency,
    winEnabled, winLimit, lossEnabled, lossLimit, winCountEnabled,
    winCountLimit, lossCountEnabled, lossCountLimit });
}

function formatMoney(value, currency = 'BIF') {
  if (value == null || value === '') return 'UNVERIFIED';
  return Number.isFinite(Number(value)) ? `${Number(value).toLocaleString()} ${currency}` : 'UNVERIFIED';
}

function isFreshVerifiedBalance(balance) {
  if (balance?.verified !== true || balance?.fresh !== true
      || balance?.currency_verified !== true || !balance?.currency || !balance?.observed_at) return false;
  const observed = Date.parse(balance.observed_at);
  const age = (Date.now() - observed) / 1000;
  return Number.isFinite(observed) && age >= 0 && age <= 20;
}

function positiveIntegerInput(value, fallback) {
  const text = value == null ? '' : String(value).trim();
  const parsed = Number(text);
  return text && Number.isInteger(parsed) && parsed >= 1 ? text : fallback;
}

function betStakeFromButton(value) {
  const match = String(value || '').match(/^\s*Bet\s+([\d,]+)(?:\.0+)?\s+BIF\s*$/i);
  if (!match) return null;
  const amount = Number(match[1].replaceAll(',', ''));
  return Number.isSafeInteger(amount) && amount > 0 ? amount : null;
}

export default function LiveActivation() {
  const [status, setStatus] = useState(null);
  const [platformBalance, setPlatformBalance] = useState(null);
  const [profileCatalog, setProfileCatalog] = useState({});
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
  const [winCountEnabled, setWinCountEnabled] = useState(true);
  const [winCountLimit, setWinCountLimit] = useState('2');
  const [lossCountEnabled, setLossCountEnabled] = useState(true);
  const [lossCountLimit, setLossCountLimit] = useState('200');
  const [confirmation, setConfirmation] = useState('');
  const [realFundsCanBeLost, setRealFundsCanBeLost] = useState(false);
  const [busy, setBusy] = useState(false);
  const [busyAction, setBusyAction] = useState('');
  const [error, setError] = useState('');
  const [savedMessage, setSavedMessage] = useState('');
  const [manualSetup, setManualSetup] = useState(null);
  const [manualSetupBusy, setManualSetupBusy] = useState(false);

  const refreshManualSetup = useCallback(async () => {
    setManualSetupBusy(true);
    setManualSetup(null);
    try {
      const { data } = await api.get('/api/live/manual-setup');
      setManualSetup(data?.manual_setup || null);
    } catch (failure) {
      const detail = failure?.response?.data?.detail;
      setManualSetup({ status: 'UNAVAILABLE', error: typeof detail === 'string' ? detail : 'Shared Aviator read-only verification is unavailable.' });
    } finally {
      setManualSetupBusy(false);
    }
  }, []);

  const currentFingerprint = useMemo(() => reviewFingerprint({
    profile, executionPanel, stakeAmount, currency,
    winEnabled, winLimit, lossEnabled, lossLimit,
    winCountEnabled, winCountLimit, lossCountEnabled, lossCountLimit,
  }), [profile, executionPanel, stakeAmount, currency, winEnabled, winLimit,
    lossEnabled, lossLimit, winCountEnabled, winCountLimit, lossCountEnabled, lossCountLimit]);
  const reviewCurrent = Boolean(review && reviewedFingerprint === currentFingerprint);
  const modeState = status?.mode || {};
  const liveReadiness = status?.live?.readiness || modeState.live?.readiness;
  const liveActive = modeState.mode === 'LIVE_REAL' && modeState.live_authorized === true;
  const automaticOn = Boolean(modeState.live?.live_active && modeState.live?.automatic_betting
    && modeState.live_authorized);
  const blockers = reviewCurrent ? (review?.blockers || []) : [];
  const liveBalanceVerified = isFreshVerifiedBalance(platformBalance);
  const reviewBalanceVerified = isFreshVerifiedBalance(review?.platform_balance);
  const verifiedCurrency = reviewBalanceVerified ? review.platform_balance.currency
    : liveBalanceVerified ? platformBalance.currency : '';
  const profileReview = review?.profile?.profile === profile
    ? review.profile : (profileCatalog[profile] || {});
  const freshPanelSnapshot = reviewBalanceVerified ? review.platform_balance
    : liveBalanceVerified ? platformBalance : null;
  const selectedPanelReadback = executionPanel === '' ? null
    : (freshPanelSnapshot?.betting_controls?.panels || []).find((panel) =>
      panel.slot === Number(executionPanel) || panel.panel_id === `PANEL_${Number(executionPanel) + 1}`) || null;
  const selectedButtonStake = betStakeFromButton(selectedPanelReadback?.button_text);
  const selectedPanelStakeMatches = Boolean(selectedPanelReadback
    && selectedPanelReadback.state === 'BET_AVAILABLE'
    && selectedPanelReadback.pending_bet_state === 'CLEAR'
    && selectedPanelReadback.currency === 'BIF'
    && Number.isSafeInteger(Number(stakeAmount))
    && Number(selectedPanelReadback.displayed_stake) === Number(stakeAmount)
    && selectedButtonStake === Number(stakeAmount));

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

  const refreshBalance = useCallback(async () => {
    try {
      const { data } = await api.get('/api/live/balance');
      const observation = data?.platform_balance || null;
      setPlatformBalance(observation);
      if (isFreshVerifiedBalance(observation)) {
        setCurrency((current) => current || observation.currency);
      }
    } catch (failure) {
      // A failed read invalidates the displayed value immediately; do not leave
      // the previous successful observation visible as if it were current.
      setPlatformBalance(failure?.response?.data?.platform_balance || {
        verified: false, fresh: false, balance: null, currency: null,
        error: failure?.response?.data?.detail || 'balance_read_failed',
      });
    }
  }, []);

  const requestReview = useCallback(async () => {
    setReview(null);
    setReviewedFingerprint('');
    setError('');
    setSavedMessage('');
    setConfirmation('');
    setRealFundsCanBeLost(false);
    const missing = [];
    if (executionPanel === '') missing.push('execution panel');
    const parsedStake = Number(stakeAmount);
    if (!stakeAmount || !Number.isSafeInteger(parsedStake) || parsedStake <= 0) missing.push('whole-number stake amount in BIF');
    else if ((profileReview.minimum_bet != null && parsedStake < profileReview.minimum_bet)
      || (profileReview.maximum_bet != null && parsedStake > profileReview.maximum_bet)) missing.push('stake within the selected profile range');
    if (!currency) missing.push('verified account currency');
    else if (currency !== 'BIF') missing.push('BIF account currency (values are never converted)');
    if (winEnabled && (!winLimit || !Number.isFinite(Number(winLimit)) || Number(winLimit) <= 0)) missing.push('profit target');
    if (lossEnabled && (!lossLimit || !Number.isFinite(Number(lossLimit)) || Number(lossLimit) <= 0)) missing.push('loss limit');
    if (winCountEnabled && (!winCountLimit || !Number.isInteger(Number(winCountLimit)) || Number(winCountLimit) < 1)) missing.push('confirmed WIN count');
    if (lossCountEnabled && (!lossCountLimit || !Number.isInteger(Number(lossCountLimit)) || Number(lossCountLimit) < 1)) missing.push('confirmed LOSS count');
    if (missing.length) {
      setReview(null);
      setReviewedFingerprint('');
      setError(`Complete the required fields: ${missing.join(', ')}.`);
      return;
    }
    setBusy(true);
    setBusyAction('review');
    try {
      await api.post('/api/betting-mode/live-configuration', {
        profile,
        execution_panel: Number(executionPanel),
        stake_amount: Number(stakeAmount),
        currency,
        profit_target_enabled: winEnabled,
        profit_target: winEnabled ? Number(winLimit) : null,
        loss_limit_enabled: lossEnabled,
        loss_limit: lossEnabled ? Number(lossLimit) : null,
        win_count_limit_enabled: winCountEnabled,
        win_count_limit: winCountEnabled ? Number(winCountLimit) : null,
        loss_count_limit_enabled: lossCountEnabled,
        loss_count_limit: lossCountEnabled ? Number(lossCountLimit) : null,
        cashout_target: 2,
      });
      const params = {
        profile,
        win_limit_enabled: winEnabled,
        session_loss_limit_enabled: lossEnabled,
        reconciled_win_limit_enabled: winCountEnabled,
        reconciled_loss_count_limit_enabled: lossCountEnabled,
      };
      if (executionPanel !== '') params.execution_panel = Number(executionPanel);
      if (stakeAmount !== '') params.stake_amount = Number(stakeAmount);
      if (currency !== '') params.currency = currency;
      if (winEnabled && winLimit !== '') params.win_limit = Number(winLimit);
      if (lossEnabled && lossLimit !== '') params.session_loss_limit = Number(lossLimit);
      if (winCountEnabled && winCountLimit !== '') params.reconciled_win_limit_count = Number(winCountLimit);
      if (lossCountEnabled && lossCountLimit !== '') params.reconciled_loss_count_limit = Number(lossCountLimit);
      const { data } = await api.get('/api/betting-mode/live-review', { params });
      setReview(data);
      setReviewedFingerprint(currentFingerprint);
      setPlatformBalance(data?.platform_balance || null);
      setSavedMessage('Configuration saved. LIVE remains off until review and explicit authorization pass.');
    } catch (failure) {
      setReview(null);
      setReviewedFingerprint('');
      const detail = failure?.response?.data?.detail;
      setError(typeof detail === 'string' ? detail : detail?.reason || 'LIVE readiness review failed.');
    } finally {
      setBusy(false);
      setBusyAction('');
    }
  }, [currentFingerprint, currency, executionPanel, lossEnabled, lossLimit,
    profile, stakeAmount, winEnabled, winLimit, winCountEnabled, winCountLimit,
    lossCountEnabled, lossCountLimit,
    profileReview.minimum_bet, profileReview.maximum_bet]);

  const saveConfiguration = useCallback(async () => {
    // A save always invalidates the visible challenge, even when the values
    // happen to be unchanged. The new draft must be reviewed again.
    setReview(null);
    setReviewedFingerprint('');
    setConfirmation('');
    setRealFundsCanBeLost(false);
    setBusy(true);
    setBusyAction('save');
    setError('');
    setSavedMessage('');
    try {
      const parsedStake = stakeAmount === '' ? null : Number(stakeAmount);
      if (parsedStake != null && (!Number.isSafeInteger(parsedStake) || parsedStake <= 0
          || (profileReview.minimum_bet != null && parsedStake < profileReview.minimum_bet)
          || (profileReview.maximum_bet != null && parsedStake > profileReview.maximum_bet))) {
        throw new Error('Stake must be a whole-number BIF amount within the selected profile range.');
      }
      if (currency && currency !== 'BIF') {
        throw new Error('LIVE_REAL currently requires verified BIF. No currency conversion was applied.');
      }
      if ((winEnabled && (!winLimit || !Number.isFinite(Number(winLimit)) || Number(winLimit) <= 0))
          || (lossEnabled && (!lossLimit || !Number.isFinite(Number(lossLimit)) || Number(lossLimit) <= 0))) {
        throw new Error('Enter a positive BIF amount for each enabled monetary stop.');
      }
      if ((winCountEnabled && (!Number.isInteger(Number(winCountLimit)) || Number(winCountLimit) < 1))
          || (lossCountEnabled && (!Number.isInteger(Number(lossCountLimit)) || Number(lossCountLimit) < 1))) {
        throw new Error('Enter a positive whole-number result count for each enabled count stop.');
      }
      const payload = {
        profile,
        execution_panel: executionPanel === '' ? null : Number(executionPanel),
        stake_amount: parsedStake,
        currency: currency || null,
        profit_target_enabled: winEnabled,
        profit_target: winEnabled && winLimit !== '' ? Number(winLimit) : null,
        loss_limit_enabled: lossEnabled,
        loss_limit: lossEnabled && lossLimit !== '' ? Number(lossLimit) : null,
        win_count_limit_enabled: winCountEnabled,
        win_count_limit: winCountEnabled && winCountLimit !== '' ? Number(winCountLimit) : null,
        loss_count_limit_enabled: lossCountEnabled,
        loss_count_limit: lossCountEnabled && lossCountLimit !== '' ? Number(lossCountLimit) : null,
        cashout_target: 2,
      };
      const { data } = await api.post('/api/betting-mode/live-configuration', payload);
      // Confirm persistence using a separate read, not just the POST echo.
      const { data: readback } = await api.get('/api/betting-mode/live-configuration');
      const saved = readback?.configuration;
      const fieldsMatch = Boolean(saved) && Object.entries(payload)
        .every(([key, value]) => saved[key] === value);
      if (!fieldsMatch) throw new Error('Saved configuration readback did not match the values entered.');
      const missing = saved?.missing_configuration || data?.configuration?.missing_configuration || [];
      setSavedMessage(missing.length
        ? `Draft saved. Still required: ${missing.join(', ')}.`
        : 'Configuration saved. Run the fresh readiness review before authorization.');
    } catch (failure) {
      const detail = failure?.response?.data?.detail;
      setError(typeof failure?.message === 'string' && failure.message !== 'Request failed with status code 422'
        ? failure.message : typeof detail === 'string' ? detail : detail?.reason || 'Configuration could not be saved.');
    } finally {
      setBusy(false);
      setBusyAction('');
    }
  }, [currency, executionPanel, lossCountEnabled, lossCountLimit, lossEnabled,
    lossLimit, profile, profileReview.maximum_bet, profileReview.minimum_bet,
    stakeAmount, winCountEnabled, winCountLimit, winEnabled, winLimit]);

  useEffect(() => {
    refreshStatus();
    const timer = window.setInterval(refreshStatus, 5000);
    return () => window.clearInterval(timer);
  }, [refreshStatus]);

  useEffect(() => { void refreshManualSetup(); }, [refreshManualSetup]);

  useEffect(() => {
    let mounted = true;
    Promise.allSettled([
      api.get('/api/betting-mode/live-configuration'),
      api.get('/api/risk/profiles'),
      api.get('/api/betting/profiles'),
    ]).then(([configurationResult, riskResult, bettingResult]) => {
      if (!mounted) return;
      const riskProfiles = riskResult.status === 'fulfilled' ? riskResult.value.data?.profiles || {} : {};
      const bettingProfiles = bettingResult.status === 'fulfilled' ? bettingResult.value.data?.profiles || {} : {};
      const catalog = {};
      for (const key of ['PROFILE_A', 'PROFILE_B']) {
        const risk = riskProfiles[key] || {};
        const betting = bettingProfiles[key] || {};
        if (Object.keys(risk).length || Object.keys(betting).length) {
          catalog[key] = {
            profile: key,
            cashout_target: risk.cashout ?? betting.base_target ?? betting.cashout,
            default_stake: betting.default_amount_bif,
            minimum_bet: risk.minimum_bet ?? betting.min_amount_bif,
            maximum_bet: risk.maximum_bet ?? betting.max_amount_bif,
            session_loss_limit: risk.maximum_session_loss ?? betting.max_loss_bif,
            maximum_consecutive_losses: risk.maximum_consecutive_losses,
            maximum_balance_percentage: risk.maximum_balance_percentage,
          };
        }
      }
      setProfileCatalog(catalog);
      if (configurationResult.status !== 'fulfilled') return;
      const saved = configurationResult.value.data?.configuration;
      if (!saved) return;
      if (saved.profile) setProfile(saved.profile);
      if (saved.execution_panel === 0 || saved.execution_panel === 1) setExecutionPanel(String(saved.execution_panel));
      // Only restore monetary values whose stored unit is explicitly BIF.
      // Other currencies are never reinterpreted or converted.
      if (saved.currency === 'BIF') {
        if (saved.stake_amount != null) setStakeAmount(String(saved.stake_amount));
        setWinLimit(saved.profit_target == null ? '' : String(saved.profit_target));
        setLossLimit(saved.loss_limit == null ? '' : String(saved.loss_limit));
      }
      setCurrency(saved.currency || '');
      setWinEnabled(Boolean(saved.profit_target_enabled));
      setLossEnabled(Boolean(saved.loss_limit_enabled));
      setWinCountEnabled(saved.win_count_limit_enabled == null ? true : Boolean(saved.win_count_limit_enabled));
      setWinCountLimit(positiveIntegerInput(saved.win_count_limit, '2'));
      setLossCountEnabled(saved.loss_count_limit_enabled == null ? true : Boolean(saved.loss_count_limit_enabled));
      setLossCountLimit(positiveIntegerInput(saved.loss_count_limit, '200'));
    });
    return () => { mounted = false; };
  }, []);

  useEffect(() => {
    if (!review || !reviewedFingerprint || currentFingerprint === reviewedFingerprint) return;
    setReview(null);
    setReviewedFingerprint('');
    setConfirmation('');
    setRealFundsCanBeLost(false);
    setSavedMessage('');
  }, [currentFingerprint, review, reviewedFingerprint]);

  useEffect(() => {
    refreshBalance();
    const timer = window.setInterval(refreshBalance, 15000);
    return () => window.clearInterval(timer);
  }, [refreshBalance]);

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
        reconciled_win_limit_enabled: review.reconciled_win_limit_enabled,
        reconciled_win_limit_count: review.reconciled_win_limit_count,
        reconciled_loss_count_limit_enabled: review.reconciled_loss_count_limit_enabled,
        reconciled_loss_count_limit: review.reconciled_loss_count_limit,
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

  const reviewIsReady = reviewCurrent && review?.live_available === true;
  const liveSessionSnapshot = modeState.live?.session?.snapshot || {};
  const liveExecutionStop = modeState.live?.live_execution_stop || {};

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
        <div className="rounded bg-zinc-950/60 p-2">Reconciled session P/L: {liveExecutionStop.status === 'UNCONFIGURED' ? '—' : liveExecutionStop.realized_profit_loss ?? 0} {liveExecutionStop.currency || liveSessionSnapshot.currency || ''}</div>
        <div className="rounded bg-zinc-950/60 p-2">Reconciled WIN / LOSS: {liveExecutionStop.status === 'UNCONFIGURED' ? '—' : `${liveExecutionStop.win_count ?? 0} / ${liveExecutionStop.loss_count ?? 0}`}</div>
        <div className="rounded bg-zinc-950/60 p-2 sm:col-span-2">Current verified platform balance: {liveBalanceVerified ? formatMoney(platformBalance.balance, platformBalance.currency) : 'UNVERIFIED'}<div className="mt-1 text-xs text-zinc-500">Observed: {liveBalanceVerified ? platformBalance.observed_at : 'No fresh verified observation'} · {liveBalanceVerified ? platformBalance.currency : 'currency unverified'}</div></div>
      </div>
    </div>

    <div className="rounded-xl border border-sky-500/30 bg-zinc-900/60 p-5" aria-label="MANUAL AVIATOR SETUP">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h2 className="font-semibold text-zinc-100">MANUAL AVIATOR SETUP</h2>
          <p className="mt-1 text-sm text-zinc-400">Configure these controls yourself in the open Aviator game. Verify stake while the selected panel’s Bet view is visible, then verify Auto Cash Out from its Auto tab. The read-only review combines recent readbacks from this same frame and shows their timestamps; it never changes tabs, toggles, stake inputs, or betting controls.</p>
        </div>
        <button type="button" onClick={refreshManualSetup} disabled={manualSetupBusy}
          className="rounded-lg border border-sky-500/40 px-3 py-2 text-sm text-sky-100 hover:bg-sky-500/10 disabled:opacity-50">
          {manualSetupBusy ? 'Reading platform state…' : 'REFRESH VERIFICATION'}
        </button>
      </div>
      <div className="mt-3 text-xs text-zinc-500">
        Selected panel: {manualSetup?.selected_panel ? `Panel ${manualSetup.selected_panel}` : 'not configured'} ·
        {' '}Expected stake: {manualSetup?.expected_stake == null ? '—' : `${manualSetup.expected_stake} ${manualSetup.expected_currency || ''}`} ·
        {' '}Observed: {manualSetup?.observed_stake == null ? 'UNAVAILABLE' : `${manualSetup.observed_stake} ${manualSetup.observed_currency || ''}`} ·
        {' '}Stake readback: {manualSetup?.stake_readback_observed_at || 'never'} ·
        {' '}Account balance: {manualSetup?.account_balance == null ? 'UNVERIFIED' : formatMoney(manualSetup.account_balance, manualSetup.account_currency)} ·
        {' '}Observed at: {manualSetup?.observed_at || 'never'}
      </div>
      {manualSetup?.error && <p role="status" className="mt-3 text-sm text-amber-200">{manualSetup.error}</p>}
      <ol className="mt-4 grid gap-2 sm:grid-cols-2 xl:grid-cols-3">
        {(manualSetup?.checklist || [
          'Browser authenticated', 'Aviator game opened', 'Execution panel selected',
          'Stake manually entered', 'Auto Cashout manually enabled', 'Cashout multiplier 2.00x',
          'Auto Bet OFF', 'Account currency verified', 'Balance verified', 'No active or pending bet',
        ].map((item) => ({ item, status: 'UNAVAILABLE' }))).map((entry, index) => (
          <li key={`${entry.item}-${index}`} className="flex items-center justify-between gap-3 rounded border border-zinc-800 bg-zinc-950/60 px-3 py-2 text-sm">
            <span className="text-zinc-300">{index + 1}. {entry.item}</span>
            <span className={entry.status === 'VERIFIED' || entry.status === 'STAKE_VERIFIED' || entry.status === 'CASHOUT_VERIFIED'
              ? 'text-emerald-300' : entry.status === 'MISMATCH' || entry.status === 'STAKE_MISMATCH' || entry.status === 'CASHOUT_MISMATCH'
                ? 'text-amber-300' : 'text-zinc-500'}>{entry.status}</span>
          </li>
        ))}
      </ol>
      {manualSetup?.status !== 'VERIFIED' && <p className="mt-3 text-xs text-amber-200">
        Manual correction: select the configured execution panel, enter the saved BIF stake, open Auto, enable Auto Cash Out at exactly 2.00x, and keep Auto Bet OFF. Do not adjust settings while either panel has an active or queued bet; refresh verification afterward.
      </p>}
      <div className="mt-3 grid gap-2 sm:grid-cols-2">
        {(manualSetup?.cashout_panels || []).map((panel) => <div key={panel.panel_id} className="rounded border border-zinc-800 bg-zinc-950/60 p-3 text-xs text-zinc-400">
          <div className="font-semibold text-zinc-200">{panel.panel_id} · {panel.status}</div>
          <div>Auto Cash Out {panel.auto_cashout || 'UNAVAILABLE'} · {panel.multiplier || '—'}x · Auto Bet {panel.auto_bet || 'UNAVAILABLE'}</div>
          <div>Readback: {panel.observed_at || 'never'} · {panel.source || 'UNAVAILABLE'}</div>
          {panel.failures?.length > 0 && <div className="mt-1">Evidence: {panel.failures.join(', ')}</div>}
        </div>)}
      </div>
    </div>

    {error && <div role="status" className="rounded border border-amber-500/30 bg-amber-500/10 p-3 text-sm text-amber-200">{error}</div>}

    <div className="rounded-xl border border-zinc-800 bg-zinc-900/60 p-5">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h2 className="font-semibold text-zinc-100">LIVE_REAL activation review</h2>
          <p className="mt-1 text-sm text-zinc-400">Choose the panel, stake, and verified account currency. This review reads the existing Aviator frame and never submits a bet.</p>
        </div>
        <div className="flex flex-wrap gap-2">
          <button type="button" onClick={saveConfiguration} disabled={busy || liveActive}
            className="rounded-lg border border-zinc-700 px-3 py-2 text-sm text-zinc-200 hover:bg-zinc-800 disabled:opacity-50">
            {busyAction === 'save' ? 'Saving…' : 'Save configuration'}
          </button>
          <button type="button" onClick={requestReview} disabled={busy}
            className="rounded-lg border border-zinc-700 px-3 py-2 text-sm text-zinc-200 hover:bg-zinc-800 disabled:opacity-50">
            {busyAction === 'review' ? 'Checking…' : 'Refresh readiness review'}
          </button>
        </div>
      </div>

      {savedMessage && <div role="status" className="mt-3 rounded border border-emerald-500/30 bg-emerald-500/10 p-3 text-sm text-emerald-200">{savedMessage}</div>}

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
            required aria-required="true" className="mt-1 block w-full rounded border border-zinc-700 bg-zinc-950 px-3 py-2">
            <option value="">Choose a panel</option><option value="0">Panel 1</option><option value="1">Panel 2</option>
          </select>
          <span className="mt-1 block text-xs text-zinc-500">Required</span>
        </label>
        <label className="text-sm text-zinc-300">Stake amount (BIF)
          <input type="number" min={profileReview.minimum_bet ?? 1} max={profileReview.maximum_bet ?? undefined}
            step="1" required aria-required="true" placeholder="Enter a whole-number stake in BIF" value={stakeAmount} onChange={(event) => setStakeAmount(event.target.value)} disabled={liveActive || busy}
            className="mt-1 block w-full rounded border border-zinc-700 bg-zinc-950 px-3 py-2" />
          <span className="mt-1 block text-xs text-zinc-500">Required · allowed profile range: {profileReview.minimum_bet ?? 'profile limits unavailable'}–{profileReview.maximum_bet ?? 'profile limits unavailable'} BIF. Saving a draft does not require balance verification; review does.</span>
        </label>
        <label className="text-sm text-zinc-300">Account currency
          <select value={currency} onChange={(event) => setCurrency(event.target.value)} disabled={liveActive || busy || !verifiedCurrency}
            required aria-required="true"
            className="mt-1 block w-full rounded border border-zinc-700 bg-zinc-950 px-3 py-2">
            <option value="">Choose verified account currency</option>
            {verifiedCurrency && <option value={verifiedCurrency}>{verifiedCurrency}</option>}
            {currency && currency !== verifiedCurrency && <option value={currency} disabled>{currency} (saved; not freshly verified)</option>}
          </select>
          <span className="mt-1 block text-xs text-zinc-500">Required · only freshly verified currency can be selected. LIVE_REAL requires BIF; values are never converted.</span>
        </label>
        <div aria-live="polite" className={`rounded border p-3 text-sm ${selectedPanelReadback && !selectedPanelStakeMatches ? 'border-amber-500/40 bg-amber-500/5' : 'border-zinc-800 bg-zinc-950/60'}`}>
          <div className="text-xs uppercase text-zinc-500">Selected panel readback · read only</div>
          {!executionPanel && <div className="mt-1 text-zinc-300">Choose Panel 1 or Panel 2 to inspect its visible stake.</div>}
          {executionPanel && !selectedPanelReadback && <div className="mt-1 text-zinc-300">No fresh selected-panel observation.</div>}
          {selectedPanelReadback && <>
            <div className="mt-1 text-zinc-100">Panel {Number(executionPanel) + 1}: {selectedPanelReadback.state || 'UNKNOWN'} · {selectedPanelReadback.button_text || 'button text unavailable'}</div>
            <div className="mt-1 text-zinc-300">Configured: {stakeAmount || '—'} BIF · displayed: {selectedPanelReadback.displayed_stake ?? 'UNVERIFIED'} {selectedPanelReadback.currency || ''} · {selectedPanelStakeMatches ? 'STAKE MATCH' : 'STAKE MISMATCH / UNVERIFIED'}</div>
            <div className="mt-1 text-xs text-zinc-500">Observed: {selectedPanelReadback.observed_at || 'timestamp unavailable'} · This does not verify an open betting window, provider acceptance, or settlement.</div>
          </>}
        </div>
        <div className="rounded border border-zinc-800 bg-zinc-950/60 p-3 text-sm">
          <div className="text-xs uppercase text-zinc-500">Read-only platform balance</div>
          <div className="mt-1 font-semibold text-zinc-100">{reviewBalanceVerified ? formatMoney(review.platform_balance.balance, review.platform_balance.currency) : 'UNVERIFIED'}</div>
          <div className="mt-1 text-xs text-zinc-500">{reviewBalanceVerified ? review.platform_balance.observed_at : 'No fresh verified observation'}</div>
        </div>
        <fieldset className="rounded-lg border border-zinc-700 bg-zinc-950/50 p-4 sm:col-span-2 xl:col-span-3">
          <legend className="px-2 font-semibold text-zinc-100">Receipt-backed session stops</legend>
          <p className="mb-4 text-sm text-zinc-400">Only accepted, settled bets with a verified matching balance count. Pending and unknown transactions do not change these totals.</p>
          <div className="grid gap-3 sm:grid-cols-2">
            <div className="rounded border border-zinc-800 p-3">
              <label className="flex items-center gap-2 text-sm text-zinc-200"><input type="checkbox" checked={winEnabled} disabled={liveActive || busy} onChange={(event) => setWinEnabled(event.target.checked)} />Stop at profit target</label>
              <label className="mt-2 block text-sm text-zinc-400">Profit target (BIF)
                <input type="number" min="1" step="any" required={winEnabled} aria-required={winEnabled} value={winLimit} placeholder="Enter amount in BIF" disabled={!winEnabled || liveActive || busy} onChange={(event) => setWinLimit(event.target.value)} className="mt-1 block w-full rounded border border-zinc-700 bg-zinc-950 px-3 py-2 text-zinc-100 disabled:opacity-50" />
                {winEnabled && !winLimit && <span className="mt-1 block text-xs text-rose-300">Required while this stop is enabled</span>}
              </label>
            </div>
            <div className="rounded border border-zinc-800 p-3">
              <label className="flex items-center gap-2 text-sm text-zinc-200"><input type="checkbox" checked={lossEnabled} disabled={liveActive || busy} onChange={(event) => setLossEnabled(event.target.checked)} />Stop at loss limit</label>
              <label className="mt-2 block text-sm text-zinc-400">Loss limit (BIF)
                <input type="number" min="1" step="any" required={lossEnabled} aria-required={lossEnabled} value={lossLimit} placeholder="Enter amount in BIF" disabled={!lossEnabled || liveActive || busy} onChange={(event) => setLossLimit(event.target.value)} className="mt-1 block w-full rounded border border-zinc-700 bg-zinc-950 px-3 py-2 text-zinc-100 disabled:opacity-50" />
                {lossEnabled && !lossLimit && <span className="mt-1 block text-xs text-rose-300">Required while this stop is enabled</span>}
              </label>
            </div>
            <div className="rounded border border-zinc-800 p-3">
              <label className="flex items-center gap-2 text-sm text-zinc-200"><input type="checkbox" checked={winCountEnabled} disabled={liveActive || busy} onChange={(event) => setWinCountEnabled(event.target.checked)} />Stop after confirmed WIN count</label>
              <label className="mt-2 block text-sm text-zinc-400">Confirmed WIN count
                <input type="number" min="1" step="1" required={winCountEnabled} aria-required={winCountEnabled} value={winCountLimit} placeholder="Enter count" disabled={!winCountEnabled || liveActive || busy} onChange={(event) => setWinCountLimit(event.target.value)} className="mt-1 block w-full rounded border border-zinc-700 bg-zinc-950 px-3 py-2 text-zinc-100 disabled:opacity-50" />
                {winCountEnabled && !winCountLimit && <span className="mt-1 block text-xs text-rose-300">Required while this stop is enabled</span>}
              </label>
            </div>
            <div className="rounded border border-zinc-800 p-3">
              <label className="flex items-center gap-2 text-sm text-zinc-200"><input type="checkbox" checked={lossCountEnabled} disabled={liveActive || busy} onChange={(event) => setLossCountEnabled(event.target.checked)} />Stop after confirmed LOSS count</label>
              <label className="mt-2 block text-sm text-zinc-400">Confirmed LOSS count
                <input type="number" min="1" step="1" required={lossCountEnabled} aria-required={lossCountEnabled} value={lossCountLimit} placeholder="Enter count" disabled={!lossCountEnabled || liveActive || busy} onChange={(event) => setLossCountLimit(event.target.value)} className="mt-1 block w-full rounded border border-zinc-700 bg-zinc-950 px-3 py-2 text-zinc-100 disabled:opacity-50" />
                {lossCountEnabled && !lossCountLimit && <span className="mt-1 block text-xs text-rose-300">Required while this stop is enabled</span>}
              </label>
            </div>
          </div>
        </fieldset>

        <div className="rounded border border-zinc-800 bg-zinc-950/60 p-3 text-sm">
          <div className="text-xs uppercase text-zinc-500">Profile execution settings</div>
          <div className="mt-1 text-zinc-200">Required cashout: 2.00x</div>
          <div className="text-zinc-400">Default stake: {profileReview.default_stake != null ? formatMoney(profileReview.default_stake, review?.platform_balance?.currency || currency || 'BIF') : '—'}</div>
          <div className="text-zinc-400">Allowed stake: {profileReview.minimum_bet != null && profileReview.maximum_bet != null ? `${formatMoney(profileReview.minimum_bet, review?.platform_balance?.currency || currency || 'BIF')} – ${formatMoney(profileReview.maximum_bet, review?.platform_balance?.currency || currency || 'BIF')}` : '—'}</div>
        </div>
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
