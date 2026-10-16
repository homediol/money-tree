import { useCallback, useEffect, useState } from 'react';
import { ShieldAlert, ShieldCheck } from 'lucide-react';
import { api } from '../services/api.js';

const activationPhrase = 'ENABLE LIVE BETTING';

function formatMoney(value, currency) {
  if (value == null || !currency) return 'UNVERIFIED';
  return `${Number(value).toLocaleString()} ${currency}`;
}

function ReviewRow({ label, value }) {
  const ok = value === 'VERIFIED' || value === 'CLEAR' || value === 'READY' || value === 'NOT_APPLIED';
  return <div className="flex items-start justify-between gap-4 border-b border-zinc-800 py-2 last:border-0">
    <span className="text-sm text-zinc-300">{label}</span>
    <span className={`text-right text-sm font-semibold ${ok ? 'text-emerald-300' : 'text-amber-200'}`}>{value || 'UNAVAILABLE'}</span>
  </div>;
}

export default function LiveActivation() {
  const [configuration, setConfiguration] = useState(null);
  const [executionPanel, setExecutionPanel] = useState('');
  const [manualSetup, setManualSetup] = useState(null);
  const [balance, setBalance] = useState(null);
  const [modeStatus, setModeStatus] = useState(null);
  const [liveReadiness, setLiveReadiness] = useState(null);
  const [review, setReview] = useState(null);
  const [confirmation, setConfirmation] = useState('');
  const [realFundsCanBeLost, setRealFundsCanBeLost] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [message, setMessage] = useState('');

  const refresh = useCallback(async () => {
    const results = await Promise.allSettled([
      api.get('/api/betting-mode/live-configuration'),
      api.get('/api/live/manual-setup'),
      api.get('/api/live/balance'),
      api.get('/api/betting-mode/status'),
      api.get('/api/live/readiness'),
    ]);
    const [configResult, manualResult, balanceResult, statusResult, readinessResult] = results;
    if (configResult.status === 'fulfilled') {
      const value = configResult.value.data?.configuration || null;
      setConfiguration(value);
      setExecutionPanel(value?.execution_panel === 0 || value?.execution_panel === 1
        ? String(value.execution_panel) : '');
    }
    setManualSetup(manualResult.status === 'fulfilled'
      ? manualResult.value.data?.manual_setup || null
      : { status: 'UNAVAILABLE', reason: 'Shared Aviator read-only verification is unavailable.' });
    setBalance(balanceResult.status === 'fulfilled'
      ? balanceResult.value.data?.platform_balance || null
      : balanceResult.reason?.response?.data?.platform_balance || null);
    if (statusResult.status === 'fulfilled') setModeStatus(statusResult.value.data?.mode || null);
    if (readinessResult.status === 'fulfilled') {
      setLiveReadiness(readinessResult.value.data || null);
      setReview((current) => current ? { ...current, readiness: readinessResult.value.data } : null);
    }
  }, []);

  useEffect(() => {
    refresh();
    const timer = window.setInterval(refresh, 15000);
    return () => window.clearInterval(timer);
  }, [refresh]);

  const savePanel = async () => {
    setBusy(true); setError(''); setMessage(''); setReview(null);
    try {
      const panel = executionPanel === '' ? null : Number(executionPanel);
      const saved = await api.post('/api/betting-mode/live-configuration', { execution_panel: panel });
      const readback = await api.get('/api/betting-mode/live-configuration');
      const stored = readback.data?.configuration;
      if (stored?.execution_panel !== panel || stored?.configuration_version !== 2) {
        throw new Error('Executor panel configuration readback did not match the saved choice.');
      }
      setConfiguration(stored);
      setMessage(saved.data?.activation_required
        ? 'Executor panel saved. Platform stake, Auto Cashout and Auto Bet remain operator controlled.'
        : 'Executor panel saved.');
      await refresh();
    } catch (failure) {
      const detail = failure?.response?.data?.detail;
      setError(typeof detail === 'string' ? detail : failure.message || 'Could not save the Executor panel.');
    } finally { setBusy(false); }
  };

  const requestReview = async () => {
    setBusy(true); setError(''); setMessage(''); setConfirmation(''); setRealFundsCanBeLost(false);
    try {
      if (executionPanel === '') throw new Error('Choose the panel the Executor would use.');
      await api.post('/api/betting-mode/live-configuration', { execution_panel: Number(executionPanel) });
      const { data } = await api.get('/api/betting-mode/live-review', {
        params: { execution_panel: Number(executionPanel) },
      });
      setReview({ ...data, receivedAt: Date.now() });
      setLiveReadiness(data?.readiness || null);
      setManualSetup(data?.readiness?.manual_setup || manualSetup);
      setBalance(data?.platform_balance || null);
      setMessage('Fresh backend review complete. This review does not change platform controls or authorize execution.');
    } catch (failure) {
      const detail = failure?.response?.data?.detail;
      setError(typeof detail === 'string' ? detail : detail?.reason || failure.message || 'LIVE review failed.');
    } finally { setBusy(false); }
  };

  const activate = async () => {
    if (!review?.review_id || review.live_available !== true) return;
    setBusy(true); setError('');
    try {
      await api.post('/api/betting-mode', {
        mode: 'LIVE_REAL', review_id: review.review_id,
        execution_panel: Number(executionPanel), confirmation,
        real_funds_can_be_lost: realFundsCanBeLost,
      });
      setMessage('LIVE_REAL activation accepted by backend.');
      setReview(null);
      await refresh();
    } catch (failure) {
      const detail = failure?.response?.data?.detail;
      const reasons = detail?.reasons;
      setError(Array.isArray(reasons) ? reasons.join(' · ')
        : typeof detail === 'string' ? detail : detail?.error || 'LIVE_REAL remains blocked by backend review.');
      await refresh();
    } finally { setBusy(false); }
  };

  const stop = async (emergency = false) => {
    setBusy(true); setError('');
    try {
      await api.post(emergency ? '/api/live/emergency-stop' : '/api/live/stop');
      setMessage(emergency ? 'Emergency stop latched.' : 'LIVE session disarmed.');
      await refresh();
    } catch (failure) {
      const detail = failure?.response?.data?.detail;
      setError(typeof detail === 'string' ? detail : 'Could not update LIVE executor state.');
    } finally { setBusy(false); }
  };

  const checklist = manualSetup?.checklist || [];
  const readiness = review?.readiness || null;
  const blockers = review?.blockers || [];
  const capabilities = readiness?.checks?.platform_capabilities;
  const authorized = modeStatus?.live_authorized === true;
  const realMoneyEnabled = readiness?.checks?.real_money_execution === 'ENABLED';
  const blockerMatrix = liveReadiness?.blocker_matrix || readiness?.blocker_matrix || [];

  return <section className="space-y-5">
    <header>
      <div className="text-xs font-bold uppercase tracking-[0.18em] text-emerald-300">Betting Executor · integrity review</div>
      <h1 className="mt-2 text-2xl font-bold">LIVE activation safety</h1>
      <p className="mt-2 max-w-4xl text-sm text-zinc-400">
        You set the platform stake, execution panel, Auto Cashout and Auto Bet. Winner Predict only reads those controls. Optional bankroll targets and stake sizing are removed.
      </p>
    </header>

    {(error || message) && <div role="status" className={`rounded border px-4 py-3 text-sm ${error ? 'border-rose-500/40 bg-rose-500/10 text-rose-200' : 'border-emerald-500/30 bg-emerald-500/10 text-emerald-200'}`}>{error || message}</div>}

    <div className="grid gap-4 md:grid-cols-3">
      <article className="rounded-xl border border-zinc-800 bg-zinc-900/70 p-4">
        <div className="text-xs uppercase text-zinc-500">Backend mode</div>
        <div className="mt-2 text-lg font-semibold">{modeStatus?.mode || 'UNKNOWN'}</div>
        <div className="mt-1 text-xs text-zinc-500">LIVE authorization {authorized ? 'ACTIVE' : 'OFF'}</div>
      </article>
      <article className="rounded-xl border border-zinc-800 bg-zinc-900/70 p-4">
        <div className="text-xs uppercase text-zinc-500">Read-only platform balance</div>
        <div className="mt-2 text-lg font-semibold">{balance?.verified ? formatMoney(balance.balance, balance.currency) : 'UNVERIFIED'}</div>
        <div className="mt-1 text-xs text-zinc-500">Observed {balance?.observed_at ? new Date(balance.observed_at).toLocaleString() : '—'}</div>
      </article>
      <article className="rounded-xl border border-zinc-800 bg-zinc-900/70 p-4">
        <div className="text-xs uppercase text-zinc-500">Executor target panel</div>
        <label className="mt-2 block text-sm text-zinc-300">
          Panel used if execution is later enabled
          <select aria-label="Executor target panel" value={executionPanel}
            disabled={busy || authorized}
            onChange={(event) => { setExecutionPanel(event.target.value); setReview(null); }}
            className="mt-1 block w-full rounded border border-zinc-700 bg-zinc-950 px-3 py-2 text-zinc-100">
            <option value="">Choose panel</option><option value="0">Panel 1</option><option value="1">Panel 2</option>
          </select>
        </label>
        <button type="button" onClick={savePanel} disabled={busy || authorized}
          className="mt-3 rounded border border-zinc-700 px-3 py-1.5 text-xs font-semibold hover:bg-zinc-800 disabled:opacity-40">Save target panel</button>
      </article>
    </div>

    <section className="rounded-xl border border-zinc-800 bg-zinc-900/60 p-5">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div><h2 className="text-lg font-semibold">Manual platform verification</h2>
          <p className="mt-1 text-xs text-zinc-400">Fresh read-only evidence. No clicks, stake edits, cashout edits, Auto Bet toggles, or bet submissions are performed.</p></div>
        <span className={`rounded-full border px-3 py-1 text-xs font-semibold ${manualSetup?.status === 'VERIFIED' ? 'border-emerald-500/40 text-emerald-200' : 'border-amber-500/40 text-amber-200'}`}>
          {manualSetup?.status || 'CHECKING'}
        </span>
      </div>
      <div className="mt-4 grid gap-x-8 md:grid-cols-2">
        {checklist.map((item) => <ReviewRow key={item.item} label={item.item} value={item.status} />)}
      </div>
      <div className="mt-4 grid gap-3 sm:grid-cols-3">
        <div className="rounded bg-zinc-950/70 p-3"><div className="text-xs text-zinc-500">Selected panel stake</div><div className="mt-1 font-semibold">{manualSetup?.observed_stake != null ? `${manualSetup.observed_stake} ${manualSetup.observed_currency || ''}` : 'UNVERIFIED'}</div></div>
        <div className="rounded bg-zinc-950/70 p-3"><div className="text-xs text-zinc-500">Account</div><div className="mt-1 font-semibold">{manualSetup?.account_currency || 'UNVERIFIED'}</div></div>
        <div className="rounded bg-zinc-950/70 p-3"><div className="text-xs text-zinc-500">Open bets</div><div className="mt-1 font-semibold">{Array.isArray(manualSetup?.pending_bet_states) ? manualSetup.pending_bet_states.join(' · ') : 'UNKNOWN'}</div></div>
      </div>
    </section>

    <section className="rounded-xl border border-zinc-800 bg-zinc-900/50 p-4">
      <h2 className="text-sm font-semibold">Optional bankroll policies</h2>
      <p className="mt-1 text-xs text-zinc-400">Profit and loss targets, exposure percentages, risk profiles and automatic stake sizing are removed. They do not participate in execution-integrity approval.</p>
      <div className="mt-2 max-w-xl"><ReviewRow label="Policy status" value={String(readiness?.optional_bankroll_policy || 'NOT_APPLIED')} /></div>
    </section>

    <section className="rounded-xl border border-zinc-800 bg-zinc-900/60 p-5">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div><h2 className="text-lg font-semibold">Execution integrity review</h2>
          <p className="mt-1 text-sm text-zinc-400">Authorization, live target identity, betting window, deduplication, fresh panel and stake evidence, acceptance and settlement are separate mandatory checks.</p></div>
        <button type="button" onClick={requestReview} disabled={busy || executionPanel === ''}
          className="rounded bg-emerald-500 px-4 py-2 text-sm font-semibold text-zinc-950 disabled:opacity-40">{busy ? 'Checking…' : 'Run fresh backend review'}</button>
      </div>
      {readiness && <div className="mt-4 grid gap-x-8 md:grid-cols-2">
        {Object.entries(readiness.checks || {}).filter(([key]) => key !== 'optional_bankroll_policy').map(([key, value]) => <ReviewRow key={key} label={key.replaceAll('_', ' ')} value={String(value)} />)}
      </div>}
      <section className="mt-5 border-t border-zinc-800 pt-4" aria-labelledby="live-blocker-matrix-title">
        <h3 id="live-blocker-matrix-title" className="text-sm font-semibold">Original LIVE blocker status</h3>
        <div className="mt-3 space-y-2">
          {blockerMatrix.map((item) => <details key={item.original_blocker} className="rounded border border-zinc-800 bg-zinc-950/50 p-3">
            <summary className="flex cursor-pointer list-none flex-wrap items-center justify-between gap-3 text-sm">
              <span className="font-medium text-zinc-200">{item.original_blocker}</span>
              <span className="rounded border border-amber-500/30 px-2 py-1 text-[10px] font-semibold text-amber-200">{item.status}</span>
            </summary>
            <dl className="mt-3 grid gap-2 text-xs md:grid-cols-[150px_1fr]">
              {[['Root cause', item.root_cause], ['Fix implemented', item.fix_implemented],
                ['Evidence source', item.evidence_source], ['Test result', item.test_result],
                ['Remaining requirement', item.remaining_requirement]].map(([label, value]) => <div key={label} className="contents">
                  <dt className="font-semibold text-zinc-400">{label}</dt><dd className="text-zinc-300">{value}</dd>
                </div>)}
            </dl>
          </details>)}
          {!blockerMatrix.length && <p className="text-xs text-amber-200">Blocker evidence matrix is unavailable until the backend returns a readiness snapshot.</p>}
        </div>
      </section>
      {review && <div className="mt-4 rounded border border-amber-500/30 bg-amber-500/5 p-4">
        <div className="flex items-center gap-2 font-semibold text-amber-100"><ShieldAlert size={17} />{review.live_available ? 'Review passed' : 'LIVE_REAL remains blocked'}</div>
        <p className="mt-1 text-xs text-zinc-400">Review created {review.receivedAt ? new Date(review.receivedAt).toLocaleString() : '—'} · expires in {review.review_expires_in_s ?? '—'} seconds.</p>
        {blockers.length > 0 && <ul className="mt-3 list-disc space-y-1 pl-5 text-sm text-amber-200">{blockers.map((reason, index) => <li key={`${reason}-${index}`}>{reason}</li>)}</ul>}
        {capabilities !== 'VERIFIED' && <p className="mt-3 text-sm text-amber-200">Provider execution, acceptance and settlement capabilities remain unverified. No capability is inferred from the visible controls.</p>}
      </div>}
      <div className="mt-4 rounded border border-zinc-800 bg-zinc-950/50 p-4">
        <div className="flex items-center gap-2 text-sm font-semibold"><ShieldCheck size={17} className={authorized ? 'text-emerald-300' : 'text-zinc-400'} />Explicit operator authorization</div>
        <p className="mt-1 text-xs text-zinc-400">Requires an authenticated backend operator, a fresh passing review, the exact confirmation phrase and acknowledgement that funds can be lost. The LIVE qualification and provider evidence gates currently prevent activation.</p>
        <label className="mt-3 flex items-start gap-2 text-sm text-rose-200"><input type="checkbox" checked={realFundsCanBeLost} onChange={(event) => setRealFundsCanBeLost(event.target.checked)} />I understand real funds can be lost.</label>
        <label className="mt-3 block text-sm text-zinc-300">Type {activationPhrase}
          <input value={confirmation} onChange={(event) => setConfirmation(event.target.value)} autoComplete="off" className="mt-1 block w-full rounded border border-zinc-700 bg-zinc-950 px-3 py-2 text-zinc-100" />
        </label>
        <button type="button" onClick={activate} disabled={busy || !review?.review_id || !review.live_available || !realMoneyEnabled || confirmation !== activationPhrase || !realFundsCanBeLost || authorized}
          className="mt-3 rounded border border-rose-500/40 px-4 py-2 text-sm font-semibold text-rose-200 disabled:cursor-not-allowed disabled:opacity-40">START LIVE REAL</button>
      </div>
      <div className="mt-4 flex flex-wrap gap-2">
        <button type="button" onClick={() => stop(false)} disabled={busy || !authorized} className="rounded bg-zinc-700 px-4 py-2 text-sm font-semibold disabled:opacity-40">OFF · Disarm LIVE</button>
        <button type="button" onClick={() => stop(true)} disabled={busy} className="rounded bg-red-900 px-4 py-2 text-sm font-semibold text-red-100 disabled:opacity-40"><ShieldAlert size={15} className="mr-1 inline" />Emergency stop</button>
      </div>
    </section>
  </section>;
}
