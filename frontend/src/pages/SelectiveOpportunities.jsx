import { useCallback, useEffect, useRef, useState } from 'react';
import { Activity, Clock3, Eye, EyeOff, RefreshCw, ShieldCheck, ShieldX } from 'lucide-react';
import { getFrozenOpportunityLiveBoard, startFrozenProspectiveExperiment } from '../services/api.js';

const WINDOW_SIZES = ['100', '200', '300', '400', '500'];
const finite = (value) => value !== null && value !== undefined && Number.isFinite(Number(value));
const score = (value) => finite(value) ? Number(value).toFixed(3) : '—';
const count = (value) => finite(value) ? Number(value).toLocaleString() : '—';
const pct = (value) => finite(value) ? `${(Number(value) * 100).toFixed(1)}%` : '—';
const timestamp = (value) => value ? new Date(value).toLocaleString() : '—';
const shortId = (value) => value ? String(value).length > 24 ? `${String(value).slice(0, 10)}…${String(value).slice(-8)}` : value : 'ID pending';

function StatusPill({ good, children, tone }) {
  const Icon = good ? ShieldCheck : tone === 'violet' ? Clock3 : ShieldX;
  const colors = tone === 'rose'
    ? 'border-rose-500/30 bg-rose-500/10 text-rose-200'
    : tone === 'violet'
      ? 'border-violet-500/30 bg-violet-500/10 text-violet-200'
      : tone === 'cyan'
        ? 'border-cyan-500/30 bg-cyan-500/10 text-cyan-200'
        : tone === 'neutral'
          ? 'border-zinc-700 bg-zinc-800/70 text-zinc-300'
          : good
            ? 'border-emerald-500/30 bg-emerald-500/10 text-emerald-200'
            : 'border-amber-500/30 bg-amber-500/10 text-amber-200';
  return <span className={`inline-flex items-center gap-1 rounded-full border px-2.5 py-1 text-xs font-semibold shadow-sm shadow-black/10 ${colors}`}>
    <Icon size={13} />{children}
  </span>;
}

function Metric({ label, value, note, tone = 'cyan' }) {
  const colors = {
    cyan: 'border-cyan-400/15 from-cyan-400/[.07] to-zinc-900/80',
    violet: 'border-violet-400/15 from-violet-400/[.08] to-zinc-900/80',
    emerald: 'border-emerald-400/15 from-emerald-400/[.07] to-zinc-900/80',
    amber: 'border-amber-400/15 from-amber-400/[.07] to-zinc-900/80',
    rose: 'border-rose-400/15 from-rose-400/[.07] to-zinc-900/80',
    neutral: 'border-zinc-700/80 from-zinc-700/20 to-zinc-900/80',
  };
  return <div className={`rounded-xl border bg-gradient-to-br p-4 shadow-lg shadow-black/10 transition-colors hover:border-white/15 ${colors[tone] || colors.cyan}`}>
    <div className="text-[11px] font-semibold uppercase tracking-[.12em] text-zinc-400">{label}</div>
    <div className="mt-1 break-words text-xl font-semibold text-zinc-100">{value}</div>
    {note && <div className="mt-1 text-xs leading-relaxed text-zinc-500">{note}</div>}
  </div>;
}

function ResultLabel({ row }) {
  if (row?.result === 'TRUE' && finite(row.actual_multiplier)) return <span className="rounded-full border border-emerald-400/30 bg-emerald-400/10 px-2.5 py-1 text-xs font-bold text-emerald-200">TRUE · {Number(row.actual_multiplier).toFixed(2)}x</span>;
  if (row?.result === 'FALSE' && finite(row.actual_multiplier)) return <span className="rounded-full border border-rose-400/30 bg-rose-400/10 px-2.5 py-1 text-xs font-bold text-rose-200">FALSE · {Number(row.actual_multiplier).toFixed(2)}x</span>;
  if (row?.status === 'INVALID' || row?.status === 'EXPIRED') return <span className="rounded-full border border-zinc-600 bg-zinc-800 px-2.5 py-1 text-xs font-semibold text-zinc-300">{row.status}</span>;
  return <span className="rounded-full border border-amber-400/25 bg-amber-400/10 px-2.5 py-1 text-xs font-semibold text-amber-200">{row?.status === 'WATCHING' ? 'WATCHING' : 'PENDING_RESULT'}</span>;
}

function evidenceText(evidence) {
  if (!evidence) return 'Evidence unavailable';
  const sequence = evidence.sequence;
  if (sequence && typeof sequence === 'object') {
    const parts = [];
    if (sequence.state) parts.push(String(sequence.state));
    if (finite(sequence.support)) parts.push(`support ${count(sequence.support)}`);
    if (finite(sequence.successes)) parts.push(`${count(sequence.successes)} past successes`);
    if (sequence.success_rate != null) parts.push(`past rate ${pct(sequence.success_rate)}`);
    if (parts.length) return parts.join(' · ');
  }
  return [evidence.sequence_state_5, evidence.coverage_band].filter(Boolean).join(' · ') || 'Frozen V3 feature snapshot';
}

function CandidateCard({ row }) {
  const displayRank = row.current_display_rank ?? row.rank;
  return <article className="group relative overflow-hidden rounded-xl border border-emerald-400/15 bg-gradient-to-br from-emerald-400/[.06] via-zinc-900/80 to-zinc-950 p-4 shadow-lg shadow-black/10 transition hover:border-emerald-300/35">
    <div className="pointer-events-none absolute -right-10 -top-12 h-32 w-32 rounded-full bg-emerald-400/5 blur-2xl transition group-hover:bg-emerald-400/10" />
    <div className="flex flex-wrap items-center justify-between gap-2">
      <div className="text-lg font-bold text-zinc-100"><span className="text-emerald-300">#{count(displayRank)}</span> · Target Local Index {count(row.target_round_index)}</div>
      <ResultLabel row={row} />
    </div>
    <div className="mt-3 grid gap-2 text-sm sm:grid-cols-2">
      <div><span className="text-zinc-500">Selection State:</span> <b className="text-emerald-200">{row.selection_state || 'SELECTED_OPPORTUNITY'}</b></div>
      <div><span className="text-zinc-500">Selected before outcome:</span> <b className={row.selected_before_outcome ? 'text-emerald-200' : 'text-amber-200'}>{row.selected_before_outcome ? 'YES' : 'UNVERIFIED'}</b></div>
      <div><span className="text-zinc-500">Opportunity Score:</span> <b className="text-violet-200">{score(row.opportunity_score)}</b></div>
      <div><span className="text-zinc-500">Model Score (raw):</span> <b className="text-zinc-200">{score(row.model_score)}</b><div className="text-xs text-zinc-600">Frozen ranking value; calibration not claimed</div></div>
      <div><span className="text-zinc-500">Pattern / analog:</span> <span className="text-zinc-200">{row.pattern_score ?? '—'} / {score(row.analog_score)}</span></div>
      <div><span className="text-zinc-500">Stability:</span> <span className="text-zinc-200">{row.stability_state || '—'}</span></div>
      <div><span className="text-zinc-500">Historical Rank at Observation:</span> <span className="text-zinc-200">{count(row.rank_at_observation)}</span></div>
      <div><span className="text-zinc-500">Created:</span> <span className="text-zinc-200">{timestamp(row.created_at)}</span></div>
      <div className="sm:col-span-2"><span className="text-zinc-500">Evidence:</span> <span className="text-zinc-200">{evidenceText(row.evidence)}</span></div>
      <div className="sm:col-span-2"><span className="text-zinc-500">Target ID:</span> <span className="font-mono text-xs text-zinc-300">{shortId(row.target_round_id)}</span></div>
    </div>
  </article>;
}

function InternalScoringTable({ rows }) {
  return <div className="overflow-x-auto rounded-xl border border-cyan-400/15 bg-zinc-950/60 shadow-xl shadow-black/10">
    <table className="w-full min-w-[760px] text-left text-sm">
      <thead className="sticky top-0 bg-zinc-900/95 text-[11px] uppercase tracking-wide text-zinc-400"><tr>
        <th className="px-3 py-2">Target Local Index</th><th>Opportunity Score</th><th>Decision</th>
        <th>Historical Rank at Observation</th><th>Actual Outcome</th><th>Reason</th>
      </tr></thead>
      <tbody>{rows.map(row => <tr className="border-t border-zinc-800/80 transition-colors odd:bg-white/[.015] hover:bg-cyan-400/[.05]" key={row.assessment_id}>
        <td className="px-3 py-2.5 font-mono text-zinc-200">{count(row.target_round_index)}</td><td className="font-semibold text-violet-200">{score(row.opportunity_score)}</td>
        <td><span className={`rounded-full border px-2 py-0.5 text-[11px] font-semibold ${row.selection_state === 'NO_SIGNAL' ? 'border-zinc-700 bg-zinc-800/70 text-zinc-300' : row.selection_state === 'SELECTED_OPPORTUNITY' ? 'border-emerald-500/30 bg-emerald-500/10 text-emerald-200' : 'border-amber-500/30 bg-amber-500/10 text-amber-200'}`}>{row.selection_state || 'SCORE ONLY'}</span></td>
        <td>{count(row.rank_at_observation)}</td>
        <td>{finite(row.actual_multiplier) ? `${Number(row.actual_multiplier).toFixed(2)}x · ${row.result || 'RESOLVED'}` : row.status || 'PENDING_RESULT'}</td>
        <td className="max-w-[280px] whitespace-normal text-xs text-zinc-500">{row.selection_reason || 'No pre-outcome selection decision recorded.'}</td>
      </tr>)}
      {rows.length === 0 && <tr><td colSpan={6} className="px-3 py-5 text-zinc-500">Diagnostic scored-round detail is not available from this API response.</td></tr>}</tbody>
    </table>
  </div>;
}

function WindowPanel({ window, size }) {
  if (!window || window.status !== 'COMPLETE') return <div className="rounded-xl border border-zinc-800 bg-black/20 p-5">
    <div className="flex flex-wrap items-center justify-between gap-2"><div className="text-lg font-semibold">RETROSPECTIVE TOP-4/{size} DIAGNOSTIC</div><StatusPill tone="violet">WAITING</StatusPill></div>
    <div className="mt-2 text-sm text-violet-200">Progress {count(window?.progress || 0)}/{size} resolved, contiguous rounds</div>
    <div className="mt-3 h-2 overflow-hidden rounded-full bg-zinc-800"><div className="h-full rounded-full bg-gradient-to-r from-violet-500 to-fuchsia-400 transition-[width]" style={{ width: `${Math.min(100, Math.max(0, (Number(window?.progress || 0) / Number(size)) * 100))}%` }} /></div>
    <p className="mt-3 text-sm leading-relaxed text-zinc-500">{Math.max(0, Number(size) - Number(window?.progress || 0))} more proof-verified, prospectively scored rounds with resolved outcomes are needed. A continuity break starts a new window; prior results are not reused across it.</p>
  </div>;

  return <div className="space-y-4">
    <h3 className="flex items-center gap-2 text-lg font-semibold"><span className="h-2 w-2 rounded-full bg-emerald-400 shadow-[0_0_12px_rgba(52,211,153,.7)]"/>RETROSPECTIVE TOP-4/{size} — NOT LIVE PREDICTIONS</h3>
    <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
      <Metric tone="violet" label="Window progress" value={`${size}/${size}`} note={`Target local indexes ${count(window.start_target_round_index)}–${count(window.end_target_round_index)}`} />
      <Metric tone="emerald" label="Top-4 true / false" value={<><span className="text-emerald-300">{count(window.true)}</span><span className="text-zinc-500"> / </span><span className="text-rose-300">{count(window.false)}</span></>} />
      <Metric tone="cyan" label="Top-4 precision" value={pct(window.precision)} />
      <Metric tone="amber" label="Same-window baseline ≥2.10x" value={pct(window.baseline)} note={`${count(window.baseline_true)}/${size} actual rounds`} />
    </div>
    <div className="rounded-xl border border-violet-400/15 bg-gradient-to-br from-violet-400/[.04] to-zinc-950/80 p-4">
      <div className="text-sm text-zinc-400">Lift over the same window: <b className={window.lift >= 0 ? 'text-emerald-300' : 'text-rose-300'}>{finite(window.lift) ? `${window.lift >= 0 ? '+' : ''}${(window.lift * 100).toFixed(1)} pp` : '—'}</b></div>
      <div className="mt-4 overflow-x-auto">
        <table className="w-full min-w-[640px] text-left text-sm">
        <thead className="text-[11px] uppercase tracking-wide text-zinc-400"><tr><th className="py-2">Window Rank</th><th>Target local index</th><th>Opportunity score</th><th>Model score</th><th>Multiplier</th><th>Result</th></tr></thead>
          <tbody>{window.selected.map(row => <tr className="border-t border-zinc-800/80 transition-colors odd:bg-white/[.015] hover:bg-violet-400/[.05]" key={`${size}-${row.assessment_id}`}>
            <td className="py-2 font-bold text-violet-200">#{row.rank}</td><td className="font-mono">{count(row.target_round_index)}</td><td className="font-semibold text-violet-200">{score(row.opportunity_score)}</td><td>{score(row.model_score)}</td>
            <td>{finite(row.actual_multiplier) ? `${Number(row.actual_multiplier).toFixed(2)}x` : '—'}</td><td><ResultLabel row={row} /></td>
          </tr>)}</tbody>
        </table>
      </div>
    </div>
    <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">{Object.entries(window.rank_performance || {}).map(([key, rank]) => <Metric key={key} tone="neutral" label={`Rank ${key.slice(-1)} hit`} value={pct(rank.hit_rate)} note={`${count(rank.true)} TRUE / ${count(rank.false)} FALSE in this rolling window`} />)}</div>
  </div>;
}

function ProspectiveExperimentPanel({ experiment, live, startAllowed, onStart, starting }) {
  const [activeCheckpoint, setActiveCheckpoint] = useState(100);
  if (!experiment?.experiment_id) return <section className="rounded-2xl border border-amber-400/20 bg-amber-400/[.04] p-5">
    <div className="flex flex-wrap items-center justify-between gap-3"><div><h2 className="text-xl font-semibold">REAL PROSPECTIVE EXPERIMENT</h2>
      <p className="mt-2 text-sm text-amber-100">{experiment?.reason || 'Not started. Starting creates a new durable 500-round timeline from the current real PostgreSQL tail.'}</p></div>
      <button type="button" onClick={onStart} disabled={!startAllowed || starting} className="rounded-lg border border-emerald-400/30 bg-emerald-400/10 px-4 py-2 text-sm font-semibold text-emerald-100 hover:bg-emerald-400/20 disabled:cursor-not-allowed disabled:opacity-50">{starting ? 'Starting…' : 'Start prospective experiment'}</button>
    </div><p className="mt-2 text-xs text-zinc-500">Requires a fresh authenticated collector, active observer, completed 102-round warm-up, and PostgreSQL write. Starting is an explicit operator action; restarts never reset an existing experiment.</p>
  </section>;
  const progress = Math.min(100, Number(experiment.primary_progress || 0));
  const fullProgress = Math.min(500, Number(experiment.full_progress || 0));
  const gate = experiment.research_gate || {};
  const predictions = Array.isArray(experiment.selected_predictions) ? experiment.selected_predictions : [];
  const slots = Math.max(0, 4 - predictions.filter(row => row.selected).length);
  const checkpoints = experiment.checkpoint_reports || {};
  const checkpointReport = checkpoints[String(activeCheckpoint)];
  const progressStatus = experiment.status === 'COMPLETE' ? 'COMPLETE' : live
    ? (Number(experiment.rounds_observed || 0) === 0 ? 'ACTIVE' : 'COLLECTING') : 'PAUSED';
  const resultText = row => row.status === 'TRUE' || row.status === 'FALSE'
    ? <span className={row.status === 'TRUE' ? 'font-bold text-emerald-300' : 'font-bold text-rose-300'}>{row.status} · {finite(row.actual_multiplier) ? `${Number(row.actual_multiplier).toFixed(2)}x` : '—'}</span>
    : <span className={row.status === 'INVALID' ? 'text-rose-300' : 'text-amber-200'}>{row.status || 'PENDING'}</span>;

  return <section className="space-y-4 rounded-2xl border border-emerald-400/20 bg-gradient-to-br from-emerald-400/[.07] via-zinc-900/70 to-zinc-950 p-4 shadow-xl shadow-black/15 sm:p-5" aria-labelledby="prospective-experiment-title">
    <div className="flex flex-wrap items-start justify-between gap-3">
      <div><div className="text-[11px] font-bold uppercase tracking-[.16em] text-emerald-300">Frozen V3 · separate research-only selection gate</div>
        <h2 id="prospective-experiment-title" className="mt-1 text-xl font-semibold">REAL PROSPECTIVE EXPERIMENT</h2>
        <p className="mt-1 text-sm text-zinc-400">Only selections committed before the target round are predictions. Automatic real-money execution is OFF.</p>
      </div>
      <StatusPill good={live && experiment.status !== 'COMPLETE'} tone={!live ? 'rose' : undefined}>{progressStatus}{!live ? ' · LIVE DATA STALE' : ''}</StatusPill>
    </div>
    <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
      <Metric tone="violet" label="Model / hash" value={experiment.model_version || '—'} note={experiment.model_hash || 'Model hash unavailable'} />
      <Metric tone="amber" label="Target" value={`≥ ${Number(experiment.target_threshold || 2.10).toFixed(2)}x`} note="TRUE only after the real target outcome arrives" />
      <Metric tone="cyan" label="Experiment ID" value={experiment.experiment_id} note={`Started ${timestamp(experiment.started_at)} · local start index ${count(experiment.start_round_index)}`} />
      <Metric tone="neutral" label="Research gate" value={gate.status || 'NOT_DEFINED'} note={gate.policy?.selection_rule || 'No development-only gate available'} />
    </div>

    {experiment.status === 'COMPLETE' && <div className="flex flex-wrap items-center justify-between gap-3 rounded-xl border border-cyan-400/20 bg-cyan-400/[.05] p-3 text-sm"><span className="text-cyan-100">This experiment is complete. A new timeline requires an explicit operator action.</span><button type="button" onClick={onStart} disabled={!startAllowed || starting} className="rounded-lg border border-cyan-300/30 bg-cyan-300/10 px-3 py-2 font-semibold text-cyan-100 hover:bg-cyan-300/20 disabled:cursor-not-allowed disabled:opacity-50">{starting ? 'Starting…' : 'Start new experiment'}</button></div>}

    <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
      <Metric tone="cyan" label="Observed" value={`${count(progress)}/100 · ${count(fullProgress)}/500`} note={`Last persisted index ${count(experiment.last_round_index)}`} />
      <Metric tone="emerald" label="Successfully scored" value={count(experiment.rounds_scored)} note={`${count(experiment.rounds_missed)} missed · ${count(experiment.rounds_invalid)} invalid`} />
      <Metric tone="amber" label="Selections / current 100" value={`${count(experiment.selection_slots_used)}/4`} note={`${count(slots)} available · window ${count(experiment.current_window_number)}`} />
      <Metric tone="emerald" label="TRUE / FALSE / pending" value={<><span className="text-emerald-300">{count(experiment.true_predictions)}</span><span className="text-zinc-500"> / </span><span className="text-rose-300">{count(experiment.false_predictions)}</span><span className="text-zinc-500"> / </span><span className="text-amber-200">{count(experiment.pending_predictions)}</span></>} note={`Gaps ${count(experiment.rounds_gaps)} · unverified ${count(experiment.rounds_unverified)}`} />
    </div>

    <div className="rounded-xl border border-emerald-400/15 bg-black/20 p-4">
      <div className="flex flex-wrap items-end justify-between gap-3"><div>
        <div className="text-sm font-semibold text-zinc-200">Primary checkpoint {count(progress)}/100</div>
        <div className="mt-1 text-xs text-zinc-500">Full evaluation: {count(fullProgress)}/500 observed real rounds</div>
      </div><div className="text-sm font-semibold text-emerald-200">{progress}%</div></div>
      <div className="mt-3 h-3 overflow-hidden rounded-full bg-zinc-800 ring-1 ring-inset ring-white/5" role="progressbar" aria-label="Prospective experiment progress" aria-valuemin="0" aria-valuemax="100" aria-valuenow={progress}>
        <div className="h-full rounded-full bg-gradient-to-r from-emerald-500 via-cyan-400 to-sky-300 transition-[width] duration-500" style={{ width: `${progress}%` }}/>
      </div>
      <div className="mt-3 flex items-center justify-between text-xs text-zinc-500"><span>Full evaluation · {count(fullProgress)}/500</span><span>{((fullProgress / 500) * 100).toFixed(1)}%</span></div>
      <div className="mt-1 h-2 overflow-hidden rounded-full bg-zinc-800" role="progressbar" aria-label="Full 500-round prospective experiment progress" aria-valuemin="0" aria-valuemax="500" aria-valuenow={fullProgress}>
        <div className="h-full rounded-full bg-gradient-to-r from-violet-500 via-cyan-400 to-emerald-300 transition-[width] duration-500" style={{ width: `${(fullProgress / 500) * 100}%` }}/>
      </div>
    </div>

    <div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-5" aria-label="Immutable experiment checkpoints">
      {[100, 200, 300, 400, 500].map(size => {
        const report = checkpoints[String(size)];
        const checkpointProgress = Math.min(size, Number(experiment.rounds_observed || 0));
        return <button type="button" key={size} onClick={() => setActiveCheckpoint(size)} aria-pressed={activeCheckpoint === size} className={`rounded-xl border p-3 text-left transition ${activeCheckpoint === size ? 'border-cyan-300/40 bg-cyan-300/[.07]' : 'border-zinc-700/70 bg-black/20 hover:border-cyan-300/25'}`}>
          <div className="flex items-center justify-between text-xs font-bold uppercase tracking-wide text-zinc-300"><span>{size} rounds</span><span className={report ? 'text-emerald-300' : 'text-amber-200'}>{report ? 'SAVED' : `${count(checkpointProgress)}/${size}`}</span></div>
          <div className="mt-2 text-sm text-zinc-400">{report ? `${count(report.true)}/${count(report.selected)} TRUE · precision ${pct(report.precision)}` : 'Checkpoint not complete'}</div>
          {report && <div className="mt-1 text-xs text-zinc-500">Baseline {pct(report.baseline_rate)} · lift {finite(report.absolute_lift) ? `${(report.absolute_lift * 100).toFixed(1)} pp` : '—'}</div>}
        </button>;
      })}
    </div>

    <div className="rounded-xl border border-cyan-400/15 bg-zinc-950/55 p-4">
      <div className="flex flex-wrap items-center justify-between gap-2"><h3 className="font-semibold text-cyan-100">CHECKPOINT {activeCheckpoint} · PRE-OUTCOME SELECTIONS</h3>
        {checkpointReport && <span className="text-xs text-zinc-500">Saved {timestamp(checkpointReport.completed_at)}</span>}</div>
      {checkpointReport ? <>
        <div className="mt-3 grid gap-2 sm:grid-cols-2 lg:grid-cols-5 text-sm">
          <div>Selected: <b className="text-zinc-100">{count(checkpointReport.selected)}</b></div>
          <div>TRUE / FALSE: <b className="text-emerald-200">{count(checkpointReport.true)}</b> / <b className="text-rose-200">{count(checkpointReport.false)}</b></div>
          <div>Precision: <b className="text-zinc-100">{pct(checkpointReport.precision)}</b></div>
          <div>95% Wilson CI: <b className="text-zinc-100">{finite(checkpointReport.precision_ci95?.lower) ? `${pct(checkpointReport.precision_ci95.lower)}–${pct(checkpointReport.precision_ci95.upper)}` : '—'}</b></div>
          <div>Baseline / lift: <b className="text-zinc-100">{pct(checkpointReport.baseline_rate)} / {finite(checkpointReport.absolute_lift) ? `${(checkpointReport.absolute_lift * 100).toFixed(1)} pp` : '—'}</b></div>
        </div>
        <div className="mt-3 overflow-x-auto"><table className="w-full min-w-[600px] text-left text-sm"><thead className="text-[11px] uppercase tracking-wide text-zinc-500"><tr><th className="py-2">Selection</th><th>Target local index</th><th>Score</th><th>Actual multiplier</th><th>Result</th></tr></thead><tbody>
          {(checkpointReport.selected_predictions || []).map((row, index) => <tr key={row.prediction_id || `${activeCheckpoint}-${row.target_round_index}`} className="border-t border-zinc-800/80"><td className="py-2 text-emerald-200">#{count(row.rank_at_selection || index + 1)}</td><td className="font-mono">{count(row.target_round_index)}</td><td className="text-violet-200">{score(row.opportunity_score)}</td><td>{finite(row.actual_multiplier) ? `${Number(row.actual_multiplier).toFixed(2)}x` : '—'}</td><td>{resultText(row)}</td></tr>)}
          {(!checkpointReport.selected_predictions || checkpointReport.selected_predictions.length === 0) && <tr><td colSpan={5} className="py-3 text-zinc-500">No selections were frozen during this checkpoint period.</td></tr>}
        </tbody></table></div>
      </> : <p className="mt-2 text-sm text-zinc-500">Waiting for {count(Math.max(0, activeCheckpoint - Number(experiment.rounds_observed || 0)))} more real experiment rounds and resolution of any selected predictions. No retrospective replacements are allowed.</p>}
    </div>

    <div className="overflow-x-auto rounded-xl border border-emerald-400/15 bg-zinc-950/60">
      <div className="border-b border-zinc-800 px-3 py-3"><h3 className="font-semibold text-zinc-100">LIVE PROSPECTIVE SELECTED OPPORTUNITIES</h3><p className="mt-1 text-xs text-zinc-500">Selection order is the order the frozen research rule selected each candidate; it is not a calibrated probability or hindsight rank.</p></div>
      <table className="w-full min-w-[720px] text-left text-sm"><thead className="bg-zinc-900/80 text-[11px] uppercase tracking-wide text-zinc-400"><tr><th className="px-3 py-2">Selection</th><th>Target local index</th><th>Opportunity score</th><th>Selected at</th><th>Actual</th><th>Result</th></tr></thead>
        <tbody>
          {predictions.map((row, index) => <tr key={row.prediction_id || row.target_round_index} className="border-t border-zinc-800/80 odd:bg-white/[.015]">
            <td className="px-3 py-2.5 font-bold text-emerald-200">#{count(row.rank_at_selection || index + 1)}</td>
            <td className="font-mono text-zinc-200">{count(row.target_round_index)}</td><td className="font-semibold text-violet-200">{score(row.opportunity_score)}</td>
            <td className="text-zinc-300">{timestamp(row.selected_at)}</td><td>{finite(row.actual_multiplier) ? `${Number(row.actual_multiplier).toFixed(2)}x` : '—'}</td><td>{resultText(row)}</td>
          </tr>)}
          {Array.from({ length: slots }, (_, index) => <tr key={`available-${index}`} className="border-t border-zinc-800/80 text-zinc-600"><td className="px-3 py-2.5">AVAILABLE</td><td>—</td><td>—</td><td>—</td><td>—</td><td>—</td></tr>)}
          {predictions.length === 0 && slots === 0 && <tr><td colSpan={6} className="px-3 py-4 text-zinc-500">No eligible selection is recorded for this 100-round block.</td></tr>}
        </tbody>
      </table>
    </div>

    <div className="rounded-xl border border-cyan-400/10 bg-black/15 p-3">
      <div className="text-sm font-semibold text-cyan-200">Internal score monitor · last {count(experiment.internal_score_monitor?.length || 0)} experiment targets</div>
      <div className="mt-2 max-h-48 overflow-auto">
        <table className="w-full min-w-[560px] text-left text-xs"><thead className="sticky top-0 bg-zinc-950 text-zinc-500"><tr><th className="py-1">Target</th><th>Score</th><th>Decision</th><th>Data state</th><th>Reason</th></tr></thead>
          <tbody>{(experiment.internal_score_monitor || []).slice(-20).map(row => <tr key={`${row.target_round_index}-${row.assessment_id || 'round'}`} className="border-t border-zinc-800/70"><td className="py-1.5 font-mono">{count(row.target_round_index)}</td><td className="text-violet-200">{score(row.opportunity_score)}</td><td>{row.selection_state || 'NO_SIGNAL'}</td><td>{row.classification}</td><td className="max-w-[260px] truncate text-zinc-500">{row.selection_reason || row.reason || '—'}</td></tr>)}</tbody>
        </table>
      </div>
    </div>
    <p className="text-xs text-zinc-500">The research gate is frozen from the existing development score distribution and has not been prospectively validated. It is separate from V3 and does not alter the V3 model hash or score. A gap is counted in data quality and never rewrites prior experiment progress.</p>
  </section>;
}

export default function SelectiveOpportunities() {
  const [data, setData] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [updatedAt, setUpdatedAt] = useState(0);
  const [activeWindow, setActiveWindow] = useState('100');
  const [showInternalScoring, setShowInternalScoring] = useState(false);
  const [startingExperiment, setStartingExperiment] = useState(false);
  const requestInFlight = useRef(false);

  const refresh = useCallback(async () => {
    if (requestInFlight.current) return;
    requestInFlight.current = true;
    setBusy(true);
    try {
      const next = await getFrozenOpportunityLiveBoard();
      setData(next);
      setUpdatedAt(Date.now());
      setError('');
    } catch (cause) {
      setError(cause?.response?.data?.detail || cause.message || 'Live opportunity status is unavailable.');
    } finally {
      requestInFlight.current = false;
      setBusy(false);
    }
  }, []);

  const startExperiment = useCallback(async () => {
    if (startingExperiment) return;
    setStartingExperiment(true);
    try {
      await startFrozenProspectiveExperiment();
      setError('');
      await refresh();
    } catch (cause) {
      const detail = cause?.response?.data?.detail;
      setError((typeof detail === 'string' ? detail : detail?.reason) || cause.message || 'Could not start the prospective experiment.');
    } finally {
      setStartingExperiment(false);
    }
  }, [refresh, startingExperiment]);

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

  const collector = data?.collector || {};
  const observerLive = Boolean(data?.observer_active);
  const collectorLive = collector.status === 'LIVE';
  const pageFresh = updatedAt > 0 && Date.now() - updatedAt < 30000 && !error;
  const lastRound = data?.last_real_round || {};
  const roundStorage = data?.round_storage_summary || {};
  const roundStoredAt = lastRound.stored_at || collector.last_postgresql_write;
  const roundStoredAge = roundStoredAt ? Date.now() - Date.parse(roundStoredAt) : Infinity;
  const liveRoundFresh = Number.isFinite(roundStoredAge) && roundStoredAge >= 0 && roundStoredAge <= 120000;
  const observerCaughtUp = data?.observer_caught_up_to_latest_round === true;
  const observerLag = finite(data?.observer_lag_rounds) ? Number(data.observer_lag_rounds) : Infinity;
  const observerHeartbeatFresh = finite(data?.observer_heartbeat_age_seconds)
    && Number(data.observer_heartbeat_age_seconds) <= 30;
  // A just-arrived PostgreSQL row can beat the observer's next poll by one or
  // two indexes. Treat that as catch-up, not a stale source; larger or old
  // backlogs remain visibly stale.
  const observerNear = observerCaughtUp || (observerLive && observerHeartbeatFresh && observerLag <= 2);
  const live = collectorLive && observerLive && observerNear && pageFresh && liveRoundFresh;
  const alignment = data?.real_data_alignment || {};
  const scoringWarmup = data?.scoring_warmup || {};
  const warmupKnown = finite(scoringWarmup.verified_rounds) && finite(scoringWarmup.required_rounds);
  const warmupProgress = warmupKnown ? Number(scoringWarmup.verified_rounds) : null;
  const warmupRequired = warmupKnown ? Number(scoringWarmup.required_rounds) : null;
  // Progress and denominator both come from the backend's verified-round
  // snapshot. Deriving only the display percentage keeps the bar visible for
  // older API responses that have the counts but not a percentage field.
  const warmupPercent = finite(scoringWarmup.progress_percent)
    ? Number(scoringWarmup.progress_percent)
    : warmupKnown && warmupRequired > 0 ? (warmupProgress / warmupRequired) * 100 : null;
  const warmupRemaining = finite(scoringWarmup.estimated_rounds_remaining)
    ? scoringWarmup.estimated_rounds_remaining : scoringWarmup.remaining_rounds;
  const warmupComplete = warmupKnown && warmupProgress >= warmupRequired;
  const frozenModelLoaded = data ? data.frozen_model_loaded === true : null;
  const frozenModelLoadState = data ? (frozenModelLoaded ? 'LOADED' : 'NOT_LOADED') : 'UNKNOWN';
  const frozenModelStatus = data?.frozen_model_status || 'UNKNOWN';
  const observerStatus = data?.observer_status || (data ? (observerLive ? 'ACTIVE' : 'PAUSED') : 'UNKNOWN');
  const warmupReset = scoringWarmup.reset || null;
  const alignmentStatus = alignment.display_status || (alignment.status === 'VERIFIED_10_ROUND' ? 'VERIFIED' : 'VERIFYING');
  const alignmentVerified = alignmentStatus === 'VERIFIED';
  const alignmentDegraded = alignmentStatus === 'DEGRADED';
  const alignmentProgress = finite(alignment.consecutive_rounds) ? Number(alignment.consecutive_rounds) : 0;
  const alignmentBatchProgress = finite(alignment.current_batch_progress)
    ? Number(alignment.current_batch_progress) : alignmentProgress;
  const showLiveSections = live && !error;
  const experiment = data?.real_prospective_experiment || {};
  const internalScoring = Array.isArray(data?.live_internal_scoring) ? data.live_internal_scoring : [];
  const staleReason = error || (!pageFresh ? 'waiting for a fresh backend status response'
    : !liveRoundFresh ? 'latest PostgreSQL round is older than 120 seconds'
        : !collectorLive ? (collector.reason || 'collector is not live')
          : !observerLive ? (data?.observer_reason || 'observer is not live')
          : !observerNear ? `observer backlog is ${count(observerLag)} round(s) or its heartbeat is old` : null);
  const activeWindowData = data?.rolling_windows?.[activeWindow];
  const runtimeLabel = !live ? 'LIVE OBSERVATION PAUSED'
    : alignmentDegraded ? 'ALIGNMENT DEGRADED'
      : !alignmentVerified ? 'VERIFYING DATA SOURCE'
      : frozenModelStatus === 'WARMING_UP' ? 'FROZEN V3 WARMING UP'
        : 'PROSPECTIVE OBSERVER RUNNING';
  const alignmentLabel = alignmentVerified ? 'VERIFIED' : alignmentDegraded ? 'DEGRADED' : `VERIFYING · ${count(alignmentBatchProgress)}/10`;

  return <div className="space-y-6 pb-6">
    <header className="relative overflow-hidden rounded-2xl border border-cyan-400/15 bg-gradient-to-br from-cyan-400/[.08] via-zinc-900/95 to-emerald-950/30 p-5 shadow-xl shadow-black/20 sm:p-6">
      <div className="pointer-events-none absolute -right-12 -top-16 h-48 w-48 rounded-full bg-cyan-400/[.07] blur-3xl" />
      <div className="relative flex flex-wrap items-end justify-between gap-4">
      <div>
        <div className={`text-xs font-bold uppercase tracking-[.2em] ${live ? 'text-emerald-200' : 'text-amber-200'}`}>REAL-TIME MODEL: {runtimeLabel}</div>
        <div className="mt-1 text-xs font-semibold text-amber-200">REAL-DATA ALIGNMENT: {alignmentLabel}</div>
        <h1 className="mt-2 bg-gradient-to-r from-white via-cyan-100 to-emerald-200 bg-clip-text text-3xl font-bold text-transparent">Selective Opportunity Observer</h1>
        <p className="mt-2 max-w-3xl text-sm text-zinc-400">Opportunity Score is a ranking value, not a calibrated probability. Prospective window results are reported separately from live selected opportunities.</p>
      </div>
      <div className="flex flex-wrap items-center gap-2">
        <StatusPill good={live && alignmentVerified} tone={alignmentDegraded ? 'rose' : undefined}>{!live ? 'LIVE OBSERVATION PAUSED' : alignmentVerified ? 'LIVE ALIGNMENT VERIFIED' : alignmentDegraded ? `ALIGNMENT DEGRADED · ${count(alignmentBatchProgress)}/10` : `ALIGNMENT VERIFYING · ${count(alignmentBatchProgress)}/10`}</StatusPill>
        <button onClick={refresh} disabled={busy} className="inline-flex items-center gap-2 rounded-lg border border-cyan-400/25 bg-cyan-400/5 px-4 py-2 text-sm font-semibold text-cyan-100 transition hover:border-cyan-300/50 hover:bg-cyan-400/10 disabled:opacity-50"><RefreshCw size={15} className={busy ? 'animate-spin' : ''}/>Refresh</button>
      </div>
      </div>
    </header>

    <section className={`space-y-4 rounded-2xl border p-4 shadow-lg shadow-black/10 sm:p-5 ${alignmentDegraded ? 'border-rose-400/30 bg-gradient-to-br from-rose-500/[.08] to-zinc-950/80' : alignmentVerified ? 'border-emerald-400/20 bg-gradient-to-br from-emerald-400/[.06] to-zinc-950/80' : 'border-amber-400/20 bg-gradient-to-br from-amber-400/[.05] to-zinc-950/80'}`} aria-labelledby="real-data-alignment-title">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div><div className="text-[11px] font-bold uppercase tracking-[.16em] text-cyan-300">Collector to observer proof</div><h2 id="real-data-alignment-title" className="mt-1 text-xl font-semibold">REAL-DATA ALIGNMENT: {alignmentStatus}</h2></div>
        <StatusPill good={alignmentVerified} tone={alignmentDegraded ? 'rose' : undefined}>Current Batch: {count(alignmentBatchProgress)}/10</StatusPill>
      </div>
      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-5">
        <Metric tone="violet" label="Completed batches" value={count(alignment.completed_batches)} note={alignment.batch_counters_tracking_since ? `Tracked since ${timestamp(alignment.batch_counters_tracking_since)}.` : 'Prior completed-batch totals were not retained.'} />
        <Metric tone="emerald" label="Passed" value={count(alignment.passed_batches)} />
        <Metric tone="rose" label="Failed" value={count(alignment.failed_batches)} />
        <Metric tone="cyan" label="Total rounds verified" value={count(alignment.total_rounds_verified)} note={alignment.batch_counters_tracking_since ? 'Counted since alignment batch tracking began.' : 'Historical total unavailable from the old rolling-only audit.'} />
        <Metric label="Consecutive aligned rounds" value={count(alignment.consecutive_aligned_rounds ?? alignmentProgress)} />
        <Metric label="Observer lag" value={finite(alignment.observer_lag ?? data?.observer_lag_rounds) ? `${count(alignment.observer_lag ?? data?.observer_lag_rounds)} rounds` : '—'} />
        <Metric label="Last verified target" value={alignment.last_verified_target_round_index ? `Local index ${count(alignment.last_verified_target_round_index)}` : '—'} />
        <Metric tone="emerald" label="Last completed batch" value={alignment.last_completed_batch ? `${count(alignment.last_completed_batch.start_round_index)}–${count(alignment.last_completed_batch.end_round_index)} · ${alignment.last_completed_batch.rounds}/10 ${alignment.last_completed_batch.result}` : alignment.batch_counters_tracking_since ? 'None in this tracking period' : 'Prior history unavailable'} />
      </div>
      {alignment.reset && <div className="rounded-xl border border-amber-300/30 bg-amber-400/[.07] p-3 text-sm text-amber-100"><b>ALIGNMENT RESET</b><div className="mt-1 grid gap-1 sm:grid-cols-2"><span>Previous: {count(alignment.reset.previous_progress)}/10</span><span>New: {count(alignment.reset.current_progress)}/10</span><span>Round: {count(alignment.reset.round_index)}{alignment.reset.failed_round_index ? ` · break at ${count(alignment.reset.failed_round_index)}` : ''}</span><span>Reason: {alignment.reset.reason || 'OTHER'}</span></div></div>}
      {alignmentDegraded && alignment.last_failure && <div className="rounded-xl border border-rose-300/30 bg-rose-500/[.08] p-3 text-sm text-rose-100"><b>ALIGNMENT DEGRADED</b><div className="mt-1 grid gap-1 sm:grid-cols-2"><span>Failed round: {count(alignment.last_failure.failed_round_index)}</span><span>Reason: {alignment.last_failure.reason || 'OTHER'}</span><span>Collector index{alignment.last_failure.indices_are_current_snapshot ? ' (current)' : ' at detection'}: {count(alignment.last_failure.collector_index)}</span><span>Observer index{alignment.last_failure.indices_are_current_snapshot ? ' (current)' : ' at detection'}: {count(alignment.last_failure.observer_index)}</span><span>Observer lag{alignment.last_failure.indices_are_current_snapshot ? ' (current)' : ' at detection'}: {finite(alignment.last_failure.observer_lag) ? `${count(alignment.last_failure.observer_lag)} rounds` : '—'}</span><span>Missing assessment: {alignment.last_failure.missing_assessment == null ? 'UNKNOWN' : alignment.last_failure.missing_assessment ? 'YES' : 'NO'}</span>{alignment.last_failure.detail && <span className="sm:col-span-2">Detail: {alignment.last_failure.detail}</span>}</div></div>}
      <p className="text-xs text-zinc-500">Alignment verification, frozen-model warm-up (102 rounds), and prospective Top-4 windows are independent counters.</p>
    </section>

    {(data || error) && staleReason && <div className="flex items-start gap-3 rounded-xl border border-amber-400/25 bg-gradient-to-r from-amber-400/[.08] to-zinc-900/60 p-3 text-sm text-amber-100 shadow-lg shadow-black/10"><span className="mt-0.5 h-2 w-2 shrink-0 rounded-full bg-amber-300 shadow-[0_0_10px_rgba(252,211,77,.8)]"/><span><b>LIVE DATA STALE</b> · {staleReason}</span></div>}
    {collector.reason === 'authentication is not verified' && <div className="rounded-xl border border-rose-400/25 bg-gradient-to-r from-rose-500/[.08] to-zinc-900/60 p-3 text-sm text-rose-100"><b>AUTHENTICATION REQUIRED</b> · Sign in to Winner.rw in the existing collector-managed browser. The observer will resume only after that same session is verified and fresh rounds reach PostgreSQL.</div>}
    {live && !observerCaughtUp && <div className="rounded-xl border border-sky-400/25 bg-gradient-to-r from-sky-400/[.08] to-zinc-900/60 p-3 text-sm text-sky-100">OBSERVER CATCHING UP · {count(observerLag)} PostgreSQL round(s) behind; the heartbeat is current.</div>}

    <section className="space-y-4 rounded-xl border border-sky-900/70 bg-sky-950/10 p-4 sm:p-5" aria-labelledby="frozen-warmup-title">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h2 id="frozen-warmup-title" className="text-xl font-semibold">Frozen Model: {frozenModelStatus.replaceAll('_', ' ')}</h2>
          <p className="mt-1 text-sm text-zinc-400">Model: {data?.frozen_model_version || '—'} · Model Loaded: {frozenModelLoaded === null ? 'UNKNOWN' : frozenModelLoaded ? 'YES' : 'NO'} · Training: {data?.training_status || '—'}</p>
        </div>
        <StatusPill good={warmupComplete}>Warm-up: {warmupKnown ? `${warmupComplete ? 'COMPLETE' : 'WARMING UP'} · ${count(warmupProgress)}/${count(warmupRequired)}` : 'UNKNOWN'}</StatusPill>
      </div>

      <div className="flex flex-wrap gap-2">
        <StatusPill good={data && collectorLive}>Collector: {data ? (collectorLive ? 'LIVE' : 'STALE') : 'UNKNOWN'}</StatusPill>
        <StatusPill good={frozenModelLoaded === true}>Frozen Model: {frozenModelLoadState}</StatusPill>
        <StatusPill good={observerStatus === 'ACTIVE'}>Observer: {observerStatus}</StatusPill>
        <StatusPill good={alignmentVerified} tone={alignmentDegraded ? 'rose' : undefined}>Real-data alignment: {data ? alignmentStatus : 'UNKNOWN'}</StatusPill>
      </div>

      <div className="rounded-lg border border-zinc-800 bg-black/20 p-4">
        <div className="flex flex-wrap items-end justify-between gap-2">
          <div>
            <div className="text-sm font-semibold text-zinc-200">Warm-up Progress</div>
            <div className="mt-1 text-2xl font-bold tabular-nums text-zinc-100">{warmupKnown ? `${count(warmupProgress)} / ${count(warmupRequired)}` : 'Backend data unavailable'}</div>
          </div>
          <div className="text-right">
            <div className="text-xl font-semibold tabular-nums text-sky-200">{warmupPercent !== null ? `${warmupPercent.toFixed(1)}%` : '—'}</div>
            <div className="mt-1 inline-flex items-center rounded-full border border-amber-300/40 bg-amber-400/15 px-2.5 py-1 text-xs font-semibold text-amber-100">Remaining: {finite(warmupRemaining) ? `${count(warmupRemaining)} rounds` : '—'}</div>
          </div>
        </div>
        <div className="mt-3 h-5 overflow-hidden rounded-full bg-gradient-to-r from-amber-400 via-orange-400 to-rose-400 ring-1 ring-inset ring-amber-200/70 shadow-[inset_0_1px_3px_rgba(0,0,0,.45)]" role="progressbar" aria-label="Frozen model warm-up" aria-valuemin="0" aria-valuemax={warmupRequired || 102} aria-valuenow={warmupKnown ? warmupProgress : undefined} aria-valuetext={warmupKnown && warmupPercent !== null ? `${warmupProgress} of ${warmupRequired}, ${warmupPercent.toFixed(1)} percent` : 'Warm-up progress unavailable'}>
          {warmupKnown && warmupPercent !== null && <div className="h-full rounded-full bg-gradient-to-r from-cyan-300 via-sky-400 to-emerald-400 shadow-[0_0_14px_rgba(34,211,238,.45)] transition-[width] duration-500" style={{ width: `${Math.min(100, Math.max(0, warmupPercent))}%` }} />}
        </div>
        <div className="mt-2 flex flex-wrap items-center justify-between gap-2 text-xs text-zinc-400"><span>0</span><span className="inline-flex items-center gap-3"><span className="inline-flex items-center gap-1.5"><i className="h-2 w-2 rounded-full bg-cyan-300"/>Complete</span><span className="inline-flex items-center gap-1.5 font-medium text-amber-200"><i className="h-2 w-2 rounded-full bg-gradient-to-r from-amber-400 to-rose-400"/>Remaining</span></span><span>{warmupRequired ? count(warmupRequired) : '102'}</span></div>
        <p className="mt-2 text-xs leading-relaxed text-zinc-400">Warm-up is the current consecutive verified-history requirement. Scored rounds below are cumulative frozen assessments and can exceed {warmupRequired || 102}.</p>
      </div>

      {warmupReset && <div className="rounded-lg border border-rose-800 bg-rose-950/30 p-3 text-sm text-rose-100">
        <b className="tracking-wide">WARM-UP RESET</b>{!warmupReset.active && <span className="ml-2 text-rose-200/70">(recorded reset; current run has recovered)</span>}
        <div className="mt-1 grid gap-1 sm:grid-cols-2">
          <span>Previous: {count(warmupReset.previous_progress)}/{count(warmupRequired || 102)}</span>
          <span>Current: {count(warmupReset.current_progress)}/{count(warmupRequired || 102)}</span>
          <span>Break at round: {count(warmupReset.break_at_round_index)}</span>
          <span>Reason: {warmupReset.reason || 'OTHER'}</span>
        </div>
      </div>}

      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <Metric label="Current contiguous verified rounds" value={count(scoringWarmup.current_contiguous_verified_rounds)} note={`Required: ${count(warmupRequired)}`} />
        <Metric label="Last processed round" value={count(data?.last_processed_round_index)} note={`${shortId(data?.last_processed_round_id)} · processed at ${timestamp(data?.last_processed_at)}`} />
        <Metric label="Last real collector round" value={count(collector.last_real_round_index ?? lastRound.round_index)} note={`${finite(lastRound.multiplier) ? `${Number(lastRound.multiplier).toFixed(2)}x · ` : ''}Platform ID ${shortId(collector.last_platform_round_id ?? lastRound.platform_round_id)} · stored ${timestamp(lastRound.stored_at)}`} />
        <Metric label="Observer lag" value={finite(data?.observer_lag_rounds) ? `${count(data.observer_lag_rounds)} rounds` : '—'} note={observerStatus === 'LAGGING' ? 'Observer is behind PostgreSQL' : 'Round-index difference from PostgreSQL'} />
        <Metric label="Last assessment" value={data?.last_assessment_target_round_index ? `Target ${count(data.last_assessment_target_round_index)}` : '—'} note={`Source ${count(data?.last_assessment_round_index)} · ${timestamp(data?.last_assessment_created_at)}`} />
        <Metric label="Reason" value={scoringWarmup.reason || (scoringWarmup.status === 'COMPLETE' ? 'WARMUP_COMPLETE' : '—')} />
        <Metric label="Continuity break" value={scoringWarmup.continuity_break_reason || '—'} note={scoringWarmup.continuity_break_at_round_index ? `At local round ${count(scoringWarmup.continuity_break_at_round_index)}` : undefined} />
        <Metric label="Frozen configuration hash" value={data?.frozen_configuration_hash || '—'} />
      </div>
    </section>

    <ProspectiveExperimentPanel experiment={experiment} live={live && !error}
      startAllowed={live && !error && warmupComplete && observerStatus === 'ACTIVE'}
      onStart={startExperiment} starting={startingExperiment}/>

    <section className="space-y-4 rounded-2xl border border-violet-400/15 bg-gradient-to-br from-violet-500/[.05] via-zinc-900/60 to-zinc-950/80 p-4 shadow-xl shadow-black/10 sm:p-5">
      <div className="flex flex-wrap items-start justify-between gap-3"><div><div className="text-[11px] font-bold uppercase tracking-[.16em] text-violet-300">Historical ranking diagnostic</div><h2 className="mt-1 text-xl font-semibold text-zinc-100">RETROSPECTIVE TOP-4 — NOT LIVE PREDICTIONS</h2><p className="mt-1 text-sm text-zinc-400">This legacy diagnostic selects the four highest frozen scores after the resolved sample exists. It is separate from the durable pre-outcome experiment above and is not prospective prediction evidence.</p></div><span className="rounded-full border border-violet-400/20 bg-violet-400/[.07] px-3 py-1 text-xs font-medium text-violet-200">100–500 rounds</span></div>
      <div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-5" role="tablist" aria-label="Prospective Top-4 window size">
        {WINDOW_SIZES.map(size => {
          const item = data?.rolling_windows?.[size];
          const active = activeWindow === size;
          const complete = item?.status === 'COMPLETE';
          const progress = Number(item?.progress || 0);
          const remaining = Math.max(0, Number(size) - progress);
          return <button key={size} role="tab" aria-selected={active} onClick={() => setActiveWindow(size)} className={`rounded-xl border p-3 text-left transition duration-200 focus:outline-none focus:ring-2 focus:ring-violet-400/50 ${active ? 'border-violet-400/50 bg-gradient-to-br from-violet-500/20 to-zinc-900 shadow-lg shadow-violet-950/40' : 'border-zinc-700/80 bg-black/20 hover:border-violet-400/30 hover:bg-violet-400/[.05]'}`}>
            <span className="flex items-center justify-between gap-2 text-xs font-bold uppercase tracking-wide text-violet-200"><span>TOP-4/{size}</span>{complete && <span className="h-1.5 w-1.5 rounded-full bg-emerald-400"/>}</span>
            <span className={`mt-1 block text-sm font-semibold ${complete ? 'text-emerald-300' : 'text-amber-200'}`}>{complete ? 'COMPLETE' : 'WAITING'}</span>
            <span className="mt-1 block text-xs text-zinc-300">{count(progress)}/{size} resolved</span>
            <span className="mt-2 block h-1 overflow-hidden rounded-full bg-zinc-800"><span className={`block h-full rounded-full ${complete ? 'bg-gradient-to-r from-emerald-500 to-cyan-300' : 'bg-gradient-to-r from-violet-500 to-fuchsia-400'}`} style={{ width: `${Math.min(100, (progress / Number(size)) * 100)}%` }}/></span>
            <span className="mt-1 block text-xs text-zinc-500">{complete ? `${count(item.true)}/4 TRUE · ${pct(item.precision)} precision` : `${count(remaining)} more resolved rounds needed`}</span>
          </button>;
        })}
      </div>
      <WindowPanel window={activeWindowData} size={activeWindow}/>
    </section>

    <section className="rounded-2xl border border-zinc-700/80 bg-gradient-to-br from-zinc-800/50 via-zinc-900/80 to-zinc-950 p-4 shadow-xl shadow-black/15 sm:p-5">
      <div className="flex flex-wrap items-center gap-2">
        <StatusPill good={collectorLive}>Collector: {data ? (collectorLive ? 'LIVE' : 'STALE') : 'UNKNOWN'}</StatusPill>
        <StatusPill good={observerStatus === 'ACTIVE'}>Observer: {observerStatus}</StatusPill>
        <StatusPill good={frozenModelStatus === 'ACTIVE'}>Frozen model: {frozenModelStatus}</StatusPill>
        <span className="rounded-full border border-zinc-700 bg-zinc-800/60 px-3 py-1 text-xs text-zinc-400">Automatic real-money execution: <b className="text-emerald-300">OFF</b></span>
      </div>
      {!collectorLive && collector.reason && <p className="mt-2 text-sm text-amber-200">Collector reason: {collector.reason}</p>}
      {data?.observer_reason && <p className="mt-2 text-sm text-zinc-400">Observer: {data.observer_reason}{finite(data.observer_lag_rounds) && data.observer_lag_rounds > 0 ? ` · lag ${count(data.observer_lag_rounds)} rounds` : ''}</p>}
      <div className="mt-4 grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <Metric tone="emerald" label="Browser / authentication" value={`${collector.browser_connected ? 'CONNECTED' : 'DISCONNECTED'} / ${collector.authenticated ? 'VERIFIED' : 'REQUIRED'}`} note={`History ${collector.history_page_connected ? 'healthy' : 'down'} · Aviator frame ${collector.aviator_frame_connected ? 'healthy' : 'down'}`} />
        <Metric tone="cyan" label="History / Betting pages" value={`${collector.history_page?.alive ? 'History ready' : 'History down'} / ${collector.betting_page?.state || 'unknown'}`} note={collector.betting_page?.url || 'Same-context Betting page'} />
        <Metric tone="cyan" label="PostgreSQL persisted rows · COUNT(*)" value={count(roundStorage.row_count)} note="Total rows in aviator_rounds from the same database snapshot." />
        <Metric label="Latest local round index · MAX" value={count(lastRound.round_index)} note={`This is a local index, not a platform round ID · Platform ID ${shortId(lastRound.platform_round_id)} · stored ${timestamp(lastRound.stored_at)}`} />
        <Metric tone="amber" label="MAX index − row count" value={count(roundStorage.max_minus_count)} note={`${count(roundStorage.index_origin_offset)} index positions before the first stored row + ${count(roundStorage.missing_indexes_within_range)} missing indexes inside the stored range.`} />
        <Metric tone="violet" label="Frozen model version" value={data?.frozen_model_version || '—'} note={`Hash ${data?.frozen_configuration_hash || 'unavailable'}`} />
        <Metric tone="cyan" label="Real scored rounds" value={count(experiment.rounds_scored)} note={`${count(experiment.rounds_missed)} missed · ${count(experiment.rounds_invalid)} invalid in this durable experiment`} />
        <Metric label="Last V3 assessment" value={data?.last_assessment_target_round_index ? `Target local index ${count(data.last_assessment_target_round_index)}` : '—'} note={`${data?.last_assessment_scorable ? 'SCORE FROZEN' : 'WARM-UP ONLY'} · source ${count(data?.last_assessment_round_index)} · ${timestamp(data?.last_assessment_created_at)}`} />
        <Metric label="Last resolved local index" value={data?.last_resolved_round_index ? count(data.last_resolved_round_index) : '—'} note={timestamp(data?.last_resolved_at)} />
        <Metric label="Observer heartbeat" value={timestamp(data?.observer_heartbeat)} note={finite(data?.observer_heartbeat_age_seconds) ? `${Number(data.observer_heartbeat_age_seconds).toFixed(1)} seconds ago` : 'No heartbeat'} />
      </div>
      <div className="mt-3 flex items-center gap-2 rounded-lg border border-zinc-800 bg-black/20 px-3 py-2 text-xs text-zinc-500"><Clock3 size={13} className="text-cyan-300"/>Status refreshed {updatedAt ? new Date(updatedAt).toLocaleTimeString() : '—'} · {data?.observer_caught_up_to_latest_round ? 'observer caught up to PostgreSQL' : 'observer processing persisted rounds'}</div>
    </section>

    <section className="space-y-4 rounded-2xl border border-cyan-400/15 bg-gradient-to-br from-cyan-400/[.04] via-zinc-900/60 to-zinc-950/80 p-4 shadow-xl shadow-black/10 sm:p-5">
      <div className="flex items-end justify-between gap-3">
        <div><div className="text-[11px] font-bold uppercase tracking-[.16em] text-cyan-300">Frozen model activity</div><h2 className="mt-1 text-xl font-semibold">EXPERIMENT ACTIVITY</h2><p className="mt-1 text-sm text-zinc-400">Experiment progress is separate from continuity and 102-round warm-up. Only durable pre-outcome selections count as predictions.</p></div>
      </div>
      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-5">
        <Metric tone="cyan" label="Observed / scored" value={`${count(experiment.rounds_observed)} / ${count(experiment.rounds_scored)}`} note="Durable real rounds / proof-verified pre-outcome scores." />
        <Metric tone="neutral" label="No Signal" value={count(experiment.no_signal_count)} note="Below the separately frozen development cutoff." />
        <Metric tone="amber" label="Watch" value={count(experiment.watch_count)} note="Passed cutoff after the four slots were used." />
        <Metric tone="emerald" label="Selected" value={count(experiment.predictions_selected)} note="Committed before each target outcome." />
        <Metric tone="rose" label="Missed / invalid" value={`${count(experiment.rounds_missed)} / ${count(experiment.rounds_invalid)}`} note={`Gaps ${count(experiment.rounds_gaps)} · unverified ${count(experiment.rounds_unverified)}`} />
      </div>
      <p className="rounded-lg border border-amber-400/20 bg-amber-400/[.05] p-3 text-sm text-amber-100">V3 itself has no validated live selection gate. The current selection rule is separately frozen as RESEARCH ONLY from the original development score distribution and has not been tuned on prospective outcomes.</p>
      {scoringWarmup.status === 'WARMING_UP' && <p className="rounded-lg border border-sky-900 bg-sky-950/20 p-3 text-sm text-sky-100">Frozen V3 scoring is waiting for a verified contiguous segment of 102 real rounds. Resolved-window progress counts only scored, proof-verified outcomes.</p>}
    </section>

    <section className="space-y-4 rounded-2xl border border-cyan-400/10 bg-zinc-950/35 p-4 sm:p-5">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div><div className="text-[11px] font-bold uppercase tracking-[.16em] text-cyan-300">Optional diagnostics</div><h2 className="mt-1 text-xl font-semibold">LIVE INTERNAL SCORING</h2><p className="mt-1 max-w-3xl text-sm text-zinc-400">Technical details showing internal scores for individual rounds. These are not selected opportunities.</p></div>
        <div className="flex items-center gap-3">
          <Activity className={live && alignmentVerified ? 'text-emerald-300' : 'text-amber-300'} size={20}/>
          <button type="button" onClick={() => setShowInternalScoring(value => !value)} aria-expanded={showInternalScoring} aria-controls="live-internal-scoring-content" className="inline-flex items-center gap-2 rounded-lg border border-cyan-300/25 bg-cyan-300/[.06] px-3 py-2 text-sm font-semibold text-cyan-100 transition hover:border-cyan-200/50 hover:bg-cyan-300/[.12]">
            {showInternalScoring ? <><EyeOff size={15}/>Hide details</> : <><Eye size={15}/>Show details</>}
          </button>
        </div>
      </div>
      {showInternalScoring && <div id="live-internal-scoring-content">
        {showLiveSections
          ? <InternalScoringTable rows={internalScoring}/>
          : <div className="rounded-xl border border-amber-400/20 bg-amber-400/[.04] p-5 text-sm text-amber-100/80">LIVE DATA STALE · internal scores are hidden until collector, observer, and API are current.</div>}
      </div>}
    </section>

    <footer className="rounded-2xl border border-amber-400/20 bg-gradient-to-r from-amber-400/[.07] via-zinc-900/80 to-zinc-950 p-4 text-sm text-amber-100 shadow-lg shadow-black/10">
      REAL-TIME MODEL: {live ? 'PROSPECTIVE OBSERVER RUNNING' : 'LIVE OBSERVATION PAUSED'} · REAL-DATA ALIGNMENT: {alignmentLabel} · AUTOMATIC REAL-MONEY EXECUTION FROM TOP-4: OFF.
      <div className="mt-1 text-xs text-amber-200/70">{alignmentVerified ? `The last 10 consecutive collector rounds matched PostgreSQL and proof-backed pre-outcome assessments through local index ${count(alignment.latest_verified_round_index)}.` : `${alignment.reason || 'The 10-round collector, PostgreSQL, observer, and API alignment proof is still incomplete.'} No prospective performance claim is made.`}</div>
    </footer>
  </div>;
}
