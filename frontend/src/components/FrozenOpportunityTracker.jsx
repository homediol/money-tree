import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Activity, AlertTriangle, CheckCircle2, Clock3, PauseCircle, PlayCircle } from 'lucide-react';
import { getFrozenOpportunityLiveBoard, startFrozenProspectiveExperiment } from '../services/api.js';

const finite = value => value !== null && value !== undefined && Number.isFinite(Number(value));
const number = value => finite(value) ? Number(value).toLocaleString() : '—';
const multiplier = value => finite(value) ? `${Number(value).toFixed(2)}x` : '—';
const score = value => finite(value) ? Number(value).toFixed(3) : '—';
const time = value => value ? new Date(value).toLocaleString() : '—';

function Metric({ label, value, detail, tone = 'neutral' }) {
  const tones = {
    neutral: 'border-zinc-700 bg-zinc-900/60 text-zinc-100',
    cyan: 'border-cyan-400/20 bg-cyan-400/[.06] text-cyan-100',
    green: 'border-emerald-400/20 bg-emerald-400/[.06] text-emerald-100',
    red: 'border-rose-400/20 bg-rose-400/[.06] text-rose-100',
    amber: 'border-amber-400/20 bg-amber-400/[.06] text-amber-100',
  };
  return <div className={`rounded-xl border p-4 ${tones[tone] || tones.neutral}`}>
    <div className="text-xs font-semibold uppercase tracking-wide text-zinc-400">{label}</div>
    <div className="mt-1 text-2xl font-bold tabular-nums">{value}</div>
    {detail && <div className="mt-1 text-xs text-zinc-500">{detail}</div>}
  </div>;
}

function statusOf(row) {
  const status = String(row?.result || row?.status || 'PENDING').toUpperCase();
  if (status === 'TRUE' && finite(row.actual_multiplier)) return 'TRUE';
  if (status === 'FALSE' && finite(row.actual_multiplier)) return 'FALSE';
  if (status === 'INVALID' || status === 'EXPIRED') return status;
  return 'PENDING';
}

function hasPreOutcomeProof(row, experiment) {
  const proof = row?.selection_order_proof || {};
  const source = Number(row?.source_round_index ?? proof.latest_source_round_index);
  const target = Number(row?.target_round_index ?? proof.target_round_index);
  const selectedAt = row?.selected_at;
  const observedAt = row?.target_observed_at;
  const selectedTime = selectedAt ? Date.parse(selectedAt) : NaN;
  const observedTime = observedAt ? Date.parse(observedAt) : NaN;
  return row?.selected === true
    && row?.target_was_absent_at_selection === true
    && proof.target_absent_at_selection === true
    && proof.proof_method === 'postgres_advisory_transaction_lock'
    && Number.isInteger(source) && Number.isInteger(target) && target === source + 1
    && String(proof.source_round_id || proof.latest_source_round_id || '') === String(row?.source_round_id || '')
    && Boolean(selectedAt) && (!observedAt || (Number.isFinite(selectedTime) && Number.isFinite(observedTime) && selectedTime < observedTime))
    && row.model_version === experiment?.model_version
    && row.model_hash === experiment?.model_hash;
}

function Result({ row }) {
  const status = statusOf(row);
  const styles = {
    TRUE: 'border-emerald-400/30 bg-emerald-400/10 text-emerald-200',
    FALSE: 'border-rose-400/30 bg-rose-400/10 text-rose-200',
    PENDING: 'border-amber-400/30 bg-amber-400/10 text-amber-100',
    INVALID: 'border-zinc-600 bg-zinc-800 text-zinc-300',
    EXPIRED: 'border-zinc-600 bg-zinc-800 text-zinc-300',
  };
  return <span className={`inline-flex rounded-full border px-2.5 py-1 text-xs font-bold ${styles[status]}`}>
    {status}{(status === 'TRUE' || status === 'FALSE') && ` · ${multiplier(row.actual_multiplier)}`}
  </span>;
}

export default function FrozenOpportunityTracker({ advancedOpen, onAdvancedToggle, children }) {
  const [data, setData] = useState(null);
  const [updatedAt, setUpdatedAt] = useState(0);
  const [error, setError] = useState('');
  const [starting, setStarting] = useState(false);
  const [, setClockNow] = useState(Date.now());
  const requestInFlight = useRef(false);

  const refresh = useCallback(async () => {
    if (requestInFlight.current) return;
    requestInFlight.current = true;
    try {
      const next = await getFrozenOpportunityLiveBoard();
      setData(next);
      setUpdatedAt(Date.now());
      setError('');
    } catch (failure) {
      setError(failure?.response?.data?.detail?.reason || failure?.response?.data?.detail || failure.message || 'Opportunity status unavailable');
    } finally {
      requestInFlight.current = false;
    }
  }, []);

  useEffect(() => {
    let cancelled = false;
    let timer;
    const poll = async () => {
      await refresh();
      if (!cancelled) timer = globalThis.setTimeout(poll, 3000);
    };
    poll();
    return () => {
      cancelled = true;
      if (timer) globalThis.clearTimeout(timer);
    };
  }, [refresh]);

  useEffect(() => {
    const timer = globalThis.setInterval(() => setClockNow(Date.now()), 3000);
    return () => globalThis.clearInterval(timer);
  }, []);

  const collector = data?.collector || {};
  const experiment = data?.real_prospective_experiment || {};
  const warmup = data?.scoring_warmup || {};
  const experimentSnapshotPresent = Boolean(data
    && Object.prototype.hasOwnProperty.call(data, 'real_prospective_experiment')
    && data.real_prospective_experiment
    && typeof data.real_prospective_experiment === 'object');
  const experimentStateKnown = experimentSnapshotPresent && experiment.state_available !== false;
  const observed = Math.max(0, Number(experiment.rounds_observed || 0));
  const completed = experimentStateKnown ? Math.min(500, observed) : null;
  const remaining = experimentStateKnown ? Math.max(0, 500 - completed) : null;
  const warmupProgress = Math.max(0, Number(warmup.verified_rounds || 0));
  const warmupRequired = Math.max(1, Number(warmup.required_rounds || 102));
  const warmupComplete = warmupProgress >= warmupRequired;
  const lag = finite(data?.observer_lag_rounds) ? Number(data.observer_lag_rounds) : Infinity;
  const heartbeatAge = finite(data?.observer_heartbeat_age_seconds) ? Number(data.observer_heartbeat_age_seconds) : Infinity;
  const apiFresh = updatedAt > 0 && Date.now() - updatedAt <= 12000 && !error;
  const collectorLive = apiFresh && collector.status === 'LIVE' && collector.authenticated === true;
  const observerLive = apiFresh && data?.observer_active === true
    && (data?.observer_caught_up_to_latest_round === true || (lag <= 2 && heartbeatAge <= 45));
  const pipelineHealthy = collectorLive && observerLive;
  // Older live backend processes expose frozen_model_active but not the newer
  // loaded/status fields. Accept those backend-authoritative aliases so the
  // dashboard does not mistake an available frozen V3 ranker for NOT LOADED.
  const modelLoaded = data?.frozen_model_loaded === true
    || data?.frozen_model_active === true
    || ['ACTIVE', 'WARMING_UP', 'LOADED'].includes(String(data?.frozen_model_status || '').toUpperCase());
  // Model readiness is independent of collector/observer health. A lagging
  // observer must degrade the data pipeline, not relabel a loaded, warmed
  // frozen model as PAUSED.
  const modelStatus = !data ? 'UNKNOWN'
    : !modelLoaded ? 'NOT LOADED'
      : !warmupComplete ? 'WARMING UP' : 'ACTIVE';
  const modelReason = !modelLoaded ? 'Frozen V3 configuration is not loaded'
    : !warmupComplete ? 'INSUFFICIENT_CONTIGUOUS_HISTORY' : null;
  const hasExperiment = Boolean(experiment.experiment_id);
  const selections = useMemo(() => (Array.isArray(experiment.selected_predictions) ? experiment.selected_predictions : [])
    .filter(row => hasPreOutcomeProof(row, experiment))
    .sort((a, b) => Number(a.target_round_index) - Number(b.target_round_index)), [experiment]);
  const totals = useMemo(() => selections.reduce((summary, row) => {
    summary.selected += 1;
    const key = { TRUE: 'trueCount', FALSE: 'falseCount', PENDING: 'pendingCount', INVALID: 'invalidCount', EXPIRED: 'expiredCount' }[statusOf(row)];
    summary[key] += 1;
    return summary;
  }, { selected: 0, trueCount: 0, falseCount: 0, pendingCount: 0, invalidCount: 0, expiredCount: 0 }), [selections]);
  const scored = Number(experiment.rounds_scored || 0);
  const missed = Number(experiment.rounds_missed || 0);
  const coverage = experiment.assessment_coverage || {};
  const overallCoverage = finite(coverage.coverage) ? Number(coverage.coverage) : null;
  const postFixCoverage = finite(coverage.post_fix_coverage) ? Number(coverage.post_fix_coverage) : null;
  const policyStatus = experiment.selection_policy_status || 'NOT_ACTIVE';
  const fourSelectionPolicy = experiment.four_selection_policy || 'NOT_DEFINED';
  const startBlockedReason = !experimentStateKnown ? 'saved experiment state is not confirmed by the backend'
    : !apiFresh ? 'live backend status is stale'
      : !collectorLive ? (collector.reason || 'the real collector is not live')
        : !modelLoaded ? 'the frozen V3 model is not loaded'
          : !warmupComplete ? `model warm-up is ${number(warmupProgress)}/${number(warmupRequired)}`
            : !observerLive ? (data?.observer_active === true
              ? `the observer is catching up${finite(lag) ? ` (${number(lag)} rounds behind)` : ''}`
              : (data?.observer_reason || 'the prospective observer is not active'))
              : modelStatus !== 'ACTIVE' ? `frozen model status is ${modelStatus}` : null;
  const canStart = startBlockedReason === null;

  const startExperiment = async () => {
    const isNew = experiment.status === 'COMPLETE';
    if (isNew && !globalThis.confirm('Start a new 500-round experiment? The completed experiment will remain saved, and a separate timeline will be created.')) return;
    setStarting(true);
    try {
      await startFrozenProspectiveExperiment();
      await refresh();
    } catch (failure) {
      const detail = failure?.response?.data?.detail;
      setError(typeof detail === 'string' ? detail : detail?.reason || failure.message || 'Could not start the experiment');
    } finally {
      setStarting(false);
    }
  };

  const lastRound = data?.last_real_round || {};
  const resultThreshold = finite(experiment.target_threshold) ? Number(experiment.target_threshold) : 2;
  const resultThresholdText = experimentStateKnown ? resultThreshold.toFixed(2) : '—';
  const has500RoundFourSelectionPolicy = fourSelectionPolicy === 'DEFINED';
  const pipelineReason = !apiFresh ? 'Live status is stale'
    : !collectorLive ? (collector.reason || 'Collector is stale')
      : !observerLive ? (data?.observer_reason || 'Observer is lagging') : null;

  return <div className="space-y-6">
    <section className="rounded-2xl border border-cyan-400/20 bg-gradient-to-br from-cyan-400/[.07] via-zinc-900 to-emerald-950/20 p-5 shadow-xl shadow-black/20 sm:p-6">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <div className="text-xs font-bold uppercase tracking-[.18em] text-cyan-200">Frozen opportunity model</div>
          <h2 className="mt-2 text-2xl font-bold">500-Round Rare Opportunity Tracker</h2>
          <p className="mt-2 max-w-2xl text-sm text-zinc-400">Real prospective rounds and genuine pre-outcome model selections. Scores are frozen; outcomes are recorded only after each target round arrives.</p>
        </div>
        <div className={`rounded-xl border px-4 py-3 ${modelStatus === 'ACTIVE' ? 'border-emerald-400/30 bg-emerald-400/10 text-emerald-100' : modelStatus === 'WARMING UP' ? 'border-amber-400/30 bg-amber-400/10 text-amber-100' : modelStatus === 'UNKNOWN' ? 'border-zinc-600 bg-zinc-800/70 text-zinc-300' : 'border-rose-400/30 bg-rose-400/10 text-rose-100'}`}>
          <div className="text-[11px] font-bold uppercase tracking-wide opacity-75">Frozen Model</div>
          <div className="mt-0.5 text-lg font-bold">{modelStatus}</div>
          {!apiFresh && data && <div className="mt-1 text-[11px] font-medium">Last confirmed · live status stale</div>}
        </div>
      </div>
      <div className="mt-2 text-xs font-semibold uppercase tracking-wide text-zinc-500">Training: {data?.training_status || 'UNKNOWN'}</div>
      {modelLoaded && !warmupComplete && <div className="mt-5 rounded-xl border border-amber-400/20 bg-amber-400/[.05] p-4">
        <div className="flex flex-wrap items-end justify-between gap-2"><div><div className="text-sm font-semibold text-amber-100">{modelStatus === 'WARMING UP' ? 'Warm-up' : 'Last verified warm-up'}</div><div className="mt-1 text-2xl font-bold tabular-nums">{number(Math.min(warmupRequired, warmupProgress))}/{number(warmupRequired)}</div></div><div className="text-right text-sm text-amber-100">Remaining: <b>{number(Math.max(0, warmupRequired - warmupProgress))}</b></div></div>
        <div className="mt-3 h-3 overflow-hidden rounded-full bg-zinc-800" role="progressbar" aria-label="Frozen model warm-up" aria-valuemin="0" aria-valuemax={warmupRequired} aria-valuenow={Math.min(warmupRequired, warmupProgress)}><div className="h-full rounded-full bg-gradient-to-r from-amber-400 to-emerald-300 transition-[width] duration-500" style={{ width: `${Math.min(100, (warmupProgress / warmupRequired) * 100)}%` }}/></div>
      </div>}
      {(modelStatus === 'NOT LOADED' || modelStatus === 'UNKNOWN') && <div className="mt-4 flex items-start gap-2 rounded-lg border border-rose-400/20 bg-rose-400/[.05] p-3 text-sm text-rose-100"><PauseCircle size={18} className="mt-0.5 shrink-0"/><span>{modelReason || 'Frozen model status is unavailable.'}</span></div>}
      {modelStatus === 'ACTIVE' && <div className="mt-4 flex items-center gap-2 text-sm font-medium text-emerald-200"><CheckCircle2 size={17}/>Frozen model is active · warm-up complete ({number(warmupRequired)}/{number(warmupRequired)}) · training off</div>}
      {modelStatus === 'ACTIVE' && !pipelineHealthy && <div className="mt-2 text-xs text-amber-200">The frozen model is active; live scoring is limited while the data pipeline catches up.</div>}
    </section>

    <section className="space-y-5 rounded-2xl border border-emerald-400/20 bg-gradient-to-br from-emerald-400/[.05] via-zinc-900/80 to-zinc-950 p-5 shadow-xl shadow-black/15" aria-labelledby="rare-opportunity-progress">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div><div className="text-xs font-bold uppercase tracking-[.16em] text-emerald-300">Prospective experiment · {hasExperiment ? (experiment.status || 'ACTIVE') : experimentStateKnown ? 'NOT STARTED' : 'STATE UNKNOWN'}</div><h3 id="rare-opportunity-progress" className="mt-1 text-xl font-bold">500-ROUND RARE OPPORTUNITY SEARCH</h3><p className="mt-1 text-sm text-zinc-400">{experimentStateKnown ? `Truth threshold: actual multiplier ≥ ${resultThresholdText}x.` : 'Truth threshold unavailable until PostgreSQL reconnects.'} Frozen V3 scoring is unchanged.</p><p className="mt-1 text-xs text-zinc-500">Selection policy: <b className={policyStatus === 'FROZEN' ? 'text-emerald-200' : 'text-amber-200'}>{policyStatus}</b> · Four-selection cap: {has500RoundFourSelectionPolicy ? 'DEFINED' : experimentStateKnown ? 'NOT DEFINED' : 'UNKNOWN'}{experiment.policy_activation_target_index ? ` · Active from target ${number(experiment.policy_activation_target_index)}` : ''}</p>{experiment.selection_policy_reason && <p className="mt-1 text-xs text-amber-200">{experiment.selection_policy_reason}</p>}</div>
        {experimentStateKnown && (!hasExperiment || experiment.status === 'COMPLETE') && <button type="button" onClick={startExperiment} disabled={!canStart || starting} className="inline-flex items-center gap-2 rounded-lg border border-emerald-300/30 bg-emerald-300/10 px-4 py-2 text-sm font-semibold text-emerald-100 hover:bg-emerald-300/20 disabled:cursor-not-allowed disabled:opacity-40"><PlayCircle size={17}/>{starting ? 'Starting…' : hasExperiment ? 'Start new experiment' : 'Start experiment'}</button>}
      </div>

      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-6">
        <Metric label="Completed" tone="cyan" value={`${number(completed)} / 500`} detail={hasExperiment ? 'Real rounds observed in experiment' : experimentStateKnown ? 'Experiment not started' : 'PostgreSQL state unavailable'} />
        <Metric label="Remaining" value={number(remaining)} />
        <Metric label="Selected opportunities" tone="cyan" value={experimentStateKnown ? `${number(totals.selected)}${has500RoundFourSelectionPolicy ? ' / 4' : ''}` : '—'} detail={hasExperiment ? `${number(scored)} scored · ${number(missed)} missed` : experimentStateKnown ? 'No experiment selections yet' : 'Saved state unavailable'} />
        <Metric label="TRUE" tone="green" value={experimentStateKnown ? number(totals.trueCount) : '—'} detail={experimentStateKnown ? `Actual ≥ ${resultThresholdText}x` : 'Outcome state unavailable'} />
        <Metric label="FALSE" tone="red" value={experimentStateKnown ? number(totals.falseCount) : '—'} />
        <Metric label="PENDING" tone="amber" value={experimentStateKnown ? number(totals.pendingCount) : '—'} detail={totals.invalidCount + totals.expiredCount ? `${number(totals.invalidCount + totals.expiredCount)} invalid` : undefined} />
      </div>

      {experimentStateKnown && hasExperiment && <div className="rounded-xl border border-zinc-700 bg-black/20 px-4 py-3 text-sm text-zinc-300">
        <b>Prospective assessment coverage:</b> {number(scored)} scored / {number(scored + missed)} eligible targets
        {overallCoverage !== null && ` · ${(overallCoverage * 100).toFixed(1)}%`}
        <span className="mx-2 text-zinc-600">|</span>
        <b>After observer queue fix:</b> {number(coverage.post_fix_scored || 0)} scored / {number((coverage.post_fix_scored || 0) + (coverage.post_fix_missed || 0))} processed targets
        {postFixCoverage !== null && ` · ${(postFixCoverage * 100).toFixed(1)}%`}
      </div>}

      <div className="rounded-xl border border-emerald-400/15 bg-black/20 p-4">
        <div className="flex items-end justify-between gap-3"><div><div className="text-sm font-semibold text-zinc-200">500-ROUND PROGRESS</div><div className="mt-1 text-2xl font-bold tabular-nums text-zinc-100">{number(completed)} / 500</div></div><div className="text-right text-sm font-semibold text-emerald-200">{finite(completed) ? `${((completed / 500) * 100).toFixed(1)}%` : '—'}</div></div>
        <div className="mt-3 h-5 overflow-hidden rounded-full bg-zinc-800 ring-1 ring-inset ring-white/10" role="progressbar" aria-label="500-round experiment progress" aria-valuemin="0" aria-valuemax="500" aria-valuenow={finite(completed) ? completed : undefined}><div className="h-full rounded-full bg-gradient-to-r from-emerald-500 via-cyan-400 to-sky-300 transition-[width] duration-500" style={{ width: `${finite(completed) ? (completed / 500) * 100 : 0}%` }}/></div>
        <div className="mt-2 text-right text-sm text-zinc-400">{number(remaining)} rounds remaining</div>
      </div>

      {!experimentStateKnown && <div className="rounded-lg border border-rose-400/20 bg-rose-400/[.05] p-3 text-sm text-rose-100">Experiment status is unavailable. The latest backend response did not confirm whether a 500-round experiment is saved, so starting is disabled until that state is returned.</div>}
      {experimentStateKnown && !hasExperiment && <div className={`rounded-lg border p-3 text-sm ${canStart ? 'border-amber-400/20 bg-amber-400/[.05] text-amber-100' : 'border-zinc-700 bg-zinc-900/70 text-zinc-300'}`}>{canStart ? 'No 500-round experiment has been started. Select Start experiment to begin the durable timeline with real incoming rounds.' : `No 500-round experiment has been started. Start is waiting because ${startBlockedReason}.`}</div>}
      {experiment.status === 'COMPLETE' && <div className="rounded-lg border border-cyan-400/20 bg-cyan-400/[.05] p-3 text-sm text-cyan-100">500 rounds complete. This experiment remains saved; starting another creates a separate durable timeline.</div>}
      {experiment.status === 'COMPLETE' && <div className="rounded-xl border border-emerald-400/20 bg-emerald-400/[.05] p-4"><div className="font-semibold text-emerald-100">500-ROUND EXPERIMENT COMPLETE</div><div className="mt-2 text-sm text-zinc-300">Model successfully found <b className="text-emerald-200">{number(totals.trueCount)}</b> true ≥{resultThreshold.toFixed(2)}x rounds among its selected opportunities.</div><div className="mt-1 text-sm text-zinc-400">Precision on selected opportunities: {totals.trueCount + totals.falseCount > 0 ? `${number(totals.trueCount)} / ${number(totals.trueCount + totals.falseCount)} = ${((totals.trueCount / (totals.trueCount + totals.falseCount)) * 100).toFixed(1)}%` : 'not available'}. This is not accuracy across all 500 rounds.</div></div>}
    </section>

    <section className="overflow-hidden rounded-2xl border border-zinc-700 bg-zinc-900/40">
      <div className="border-b border-zinc-800 p-5"><div className="text-xs font-bold uppercase tracking-[.16em] text-cyan-300">Pre-outcome selections only</div><h3 className="mt-1 text-xl font-bold">SELECTED OPPORTUNITIES</h3><p className="mt-1 text-sm text-zinc-400">Internal scores that were not selected are excluded. Each row below has stored source → target order proof.</p></div>
      {selections.length === 0 ? <div className="p-6 text-sm text-zinc-400">{!experimentStateKnown ? 'Saved experiment records are unavailable until PostgreSQL reconnects.' : hasExperiment ? 'No verified model-selected opportunities have been recorded in this experiment yet.' : 'Start the experiment to record prospective selections. No candidates are being filled in.'}</div> : <div className="divide-y divide-zinc-800">
        {selections.map((row, index) => <article key={row.prediction_id || row.target_round_index} className="grid gap-3 p-4 sm:grid-cols-[auto_1fr_auto] sm:items-center">
          <div className="grid h-10 w-10 place-items-center rounded-full border border-cyan-300/20 bg-cyan-300/[.08] font-bold text-cyan-100">#{index + 1}</div>
          <div><div className="font-semibold text-zinc-100">Target local index {number(row.target_round_index)} <span className="font-normal text-zinc-500">· predicted ≥{resultThresholdText}x</span></div><div className="mt-1 text-xs text-zinc-500">Selected {time(row.selected_at)}{row.target_identity_if_known ? ` · Platform ID ${row.target_identity_if_known}` : ''}</div><div className="mt-1 text-xs text-zinc-500">Opportunity score: {score(row.opportunity_score ?? row.selection_score)} <span className="text-zinc-600">(ranking score, not probability)</span></div></div>
          <div className="sm:text-right"><div className="text-xs uppercase tracking-wide text-zinc-500">Actual result</div><div className="mt-1"><Result row={row}/></div></div>
        </article>)}
      </div>}
    </section>

    <div className={`flex items-start gap-2 rounded-xl border p-3 text-sm ${pipelineHealthy ? 'border-emerald-400/20 bg-emerald-400/[.04] text-emerald-100' : 'border-amber-400/20 bg-amber-400/[.05] text-amber-100'}`}>
      {pipelineHealthy ? <Activity size={17} className="mt-0.5 shrink-0"/> : <AlertTriangle size={17} className="mt-0.5 shrink-0"/>}
      <div><b>Data pipeline: {pipelineHealthy ? 'HEALTHY' : 'DEGRADED'}</b>{!pipelineHealthy && <span> · {pipelineReason || 'Waiting for current backend status'}</span>}{error && <span> · Latest API error: {String(error)}</span>}</div>
    </div>

    <details open={advancedOpen} onToggle={event => onAdvancedToggle?.(event.currentTarget.open)} className="rounded-2xl border border-zinc-700 bg-zinc-950/50">
      <summary className="cursor-pointer list-none p-4 text-sm font-semibold text-zinc-200"><span className="inline-flex items-center gap-2"><Clock3 size={16} className="text-cyan-300"/>Advanced / Diagnostics</span><span className="ml-2 text-xs font-normal text-zinc-500">Observer, alignment, identity, and model details</span></summary>
      <div className="space-y-4 border-t border-zinc-800 p-4">
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
          <Metric label="Model version" value={data?.frozen_model_version || '—'} />
          <Metric label="Configuration hash" value={data?.frozen_configuration_hash || '—'} />
          <Metric label="Loaded / training" value={`${modelLoaded ? 'YES' : 'NO'} / ${data?.training_status || 'UNKNOWN'}`} />
          <Metric label="Observer / alignment" value={`${data?.observer_status || 'UNKNOWN'} / ${data?.real_data_alignment?.display_status || 'UNKNOWN'}`} />
          <Metric label="Warm-up contiguous" value={`${number(warmup.current_contiguous_verified_rounds ?? warmupProgress)}/${number(warmupRequired)}`} detail={warmup.continuity_break_reason || warmup.reason || 'No current break reason'} />
          <Metric label="Collector / authentication" value={`${collector.status || 'UNKNOWN'} / ${collector.authenticated ? 'VERIFIED' : 'UNVERIFIED'}`} detail={collector.reason || 'No collector warning'} />
          <Metric label="Observer lag / heartbeat" value={`${finite(data?.observer_lag_rounds) ? number(data.observer_lag_rounds) : '—'} rounds / ${finite(data?.observer_heartbeat_age_seconds) ? `${Number(data.observer_heartbeat_age_seconds).toFixed(0)}s` : '—'}`} />
          <Metric label="Last real round" value={number(lastRound.round_index)} detail={`${multiplier(lastRound.multiplier)} · stored ${time(lastRound.stored_at)}`} />
          <Metric label="Last processed / assessment target" value={`${number(data?.last_processed_round_index)} / ${number(data?.last_assessment_target_round_index)}`} detail={`Assessment ${time(data?.last_assessment_created_at)}`} />
          <Metric label="Experiment ID / start index" value={experiment.experiment_id || (experimentStateKnown ? 'Not started' : 'Unavailable')} detail={`${time(experiment.started_at)} · ${number(experiment.start_round_index)}`} />
          <Metric label="Missed / invalid / gaps" value={`${number(experiment.rounds_missed || 0)} / ${number(experiment.rounds_invalid || 0)} / ${number(experiment.rounds_gaps || 0)}`} detail={`${number(experiment.rounds_unverified || 0)} unverified`} />
          <Metric label="Rare selection policy" value={policyStatus} detail={experiment.rare_selection_policy?.policy_hash || 'No frozen policy hash'} />
          <Metric label="Development data" value={`${number(experiment.rare_selection_policy?.development_sample_count)} rows`} detail={`Cutoff ${number(experiment.rare_selection_policy?.development_cutoff_round)} · active outcomes excluded`} />
          <Metric label="Observer queue" value={`${number(experiment.observer_queue?.pending_count)} pending`} detail={`${number(experiment.observer_queue?.processed_count)} processed · ${number(experiment.observer_queue?.missed_count)} missed`} />
          <Metric label="After-fix coverage" value={postFixCoverage === null ? '—' : `${(postFixCoverage * 100).toFixed(1)}%`} detail={`${number(coverage.post_fix_scored || 0)} scored · ${number(coverage.post_fix_missed || 0)} missed`} />
          <Metric label="Selection rule" value={experiment.rare_selection_policy?.selection_rules?.selection_threshold !== undefined ? `Score ≥ ${score(experiment.rare_selection_policy.selection_rules.selection_threshold)}` : (experiment.rare_selection_policy?.status || 'NOT_DEFINED')} detail="Development-only evaluation; active experiment outcomes excluded" />
        </div>
        <div className="text-xs text-zinc-500">Last API refresh: {time(updatedAt ? new Date(updatedAt).toISOString() : null)} · Automatic real-money execution: OFF</div>
        {children}
      </div>
    </details>
  </div>;
}
