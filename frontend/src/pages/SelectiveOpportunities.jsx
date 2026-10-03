import { useCallback, useEffect, useState } from 'react';
import { Brain, RefreshCw, Search, ShieldAlert } from 'lucide-react';
import { getSelectiveOpportunity, runSelectiveOpportunityResearch } from '../services/api.js';

const valid = (v) => v !== null && v !== undefined && v !== '' && Number.isFinite(Number(v));
const num = (v, digits = 2) => valid(v) ? Number(v).toFixed(digits) : '—';
const pct = (v) => valid(v) ? `${(Number(v) * 100).toFixed(1)}%` : '—';
const count = (v) => valid(v) ? Number(v).toLocaleString() : '—';
const Panel = ({ title, note, children }) => <section className="rounded-xl border border-zinc-800 bg-zinc-900/60 p-5"><h2 className="font-semibold text-zinc-100">{title}</h2>{note && <p className="mt-1 text-xs text-zinc-500">{note}</p>}<div className="mt-4">{children}</div></section>;
const Card = ({ label, value, note }) => <div className="rounded-lg border border-zinc-800 bg-black/20 p-4"><div className="text-xs uppercase tracking-wide text-zinc-500">{label}</div><div className="mt-1 text-xl font-semibold text-zinc-100">{value}</div>{note && <div className="mt-1 text-xs text-zinc-500">{note}</div>}</div>;

export default function SelectiveOpportunities() {
  const [data, setData] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const refresh = useCallback(async () => {
    try { setData(await getSelectiveOpportunity()); setError(''); }
    catch (cause) { setError(cause?.response?.data?.detail || cause.message || 'Could not load opportunity research.'); }
  }, []);
  useEffect(() => { refresh(); const timer = window.setInterval(refresh, 30000); return () => window.clearInterval(timer); }, [refresh]);
  const run = async () => {
    setBusy(true); setError('');
    try { setData(await runSelectiveOpportunityResearch()); }
    catch (cause) { setError(cause?.response?.data?.detail || cause.message || 'Research run failed.'); }
    finally { setBusy(false); }
  };
  const test = data?.untouched_test || {};
  const shadow = data?.shadow || {};
  const current = data?.current_opportunity || {};
  const ci = test.precision_ci95 || {};
  const ready = Boolean(data?.shadow_ready);

  return <div className="space-y-6">
    <header className="flex flex-wrap items-end justify-between gap-4">
      <div><div className="text-xs font-bold uppercase tracking-[.2em] text-violet-300">Selective prediction · research only</div>
        <h1 className="mt-2 text-3xl font-bold">Rare ≥2.10x Opportunity Detector</h1>
        <p className="mt-2 max-w-3xl text-sm text-zinc-400">The normal result is NO_SIGNAL. Thresholds are selected on chronological validation data and checked on one untouched final block. This detector cannot authorize or place bets.</p>
      </div>
      <div className="flex gap-2"><button onClick={refresh} className="inline-flex items-center gap-2 rounded-lg border border-zinc-700 px-4 py-2 text-sm hover:bg-zinc-800"><RefreshCw size={16}/>Refresh</button>
        <button onClick={run} disabled={busy} className="inline-flex items-center gap-2 rounded-lg bg-violet-600 px-4 py-2 text-sm font-semibold text-white disabled:opacity-50"><Search size={16}/>{busy ? 'Research running…' : 'Run historical research'}</button></div>
    </header>
    {error && <div className="rounded-lg border border-rose-800 bg-rose-950/30 p-3 text-sm text-rose-200">{error}</div>}
    {!data && !error && <div className="text-sm text-zinc-500">Loading verified research state…</div>}
    {data && <>
      <div className={`rounded-xl border p-4 ${ready ? 'border-emerald-800 bg-emerald-950/20' : 'border-amber-800 bg-amber-950/20'}`}>
        <div className="flex items-start gap-3"><ShieldAlert size={20} className={ready ? 'text-emerald-300' : 'text-amber-300'}/><div>
          <div className="font-semibold">{data.status || 'NOT_EVALUATED'}</div>
          <p className="mt-1 text-sm text-zinc-300">{ready ? 'Historical gates passed. The frozen detector is being evaluated in SHADOW only.' : (data.reasons || [data.reason || 'No candidate is currently validated.']).join(' · ')}</p>
        </div></div>
      </div>
      <Panel title="Current opportunity" note={`Mode: ${data.mode || 'UNKNOWN'} · Primary target is ≥${num(data.target, 2)}x`}>
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
          <Card label="State" value={current.status || 'NO_SIGNAL'} note={(current.reason_codes || []).join(', ') || 'No reason supplied'} />
          <Card label="Probability ≥2.10x" value={pct(current.probability_2_1x)} />
          <Card label="Opportunity score" value={num(current.opportunity_score, 1)} note={`Threshold ${pct(current.threshold)}`} />
          <Card label="Model / evidence" value={current.model_version || data.best_selective_model || 'No validated model'} note={current.evidence_strength || 'NONE'} />
        </div>
      </Panel>
      <Panel title="Historical untouched test" note="Final chronological test was not used to select the model or threshold.">
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
          <Card label="Clean rounds" value={count(data.clean_rounds)} /><Card label="Eligible targets" value={count(data.rounds_evaluated)} />
          <Card label="Test rounds" value={count(test.rounds_evaluated)} /><Card label="Validation threshold" value={pct(data.selected_threshold)} />
          <Card label="Signals / 100" value={num(test.signals_per_100)} /><Card label="Signals / 500" value={num(test.signals_per_500)} />
          <Card label="True / false signals" value={`${count(test.true_signals)} / ${count(test.false_signals)}`} />
          <Card label="Precision · 95% Wilson CI" value={pct(test.precision)} note={ci.lower == null ? 'No signals' : `${pct(ci.lower)} – ${pct(ci.upper)}`} />
          <Card label="Coverage" value={pct(test.coverage)} /><Card label="Abstention" value={pct(test.abstention_rate)} />
          <Card label="P(≥2.10x) baseline" value={pct(test.base_rate_observed ?? data.baseline_rate)} />
          <Card label="P(≥2.10x | SIGNAL)" value={pct(test.precision)} />
          <Card label="Lift over baseline" value={test.lift == null ? '—' : `${num(test.lift * 100)} pp`} note={test.lift_ratio == null ? '' : `${num(test.lift_ratio)}× baseline`} />
          <Card label="Lift 95% interval" value={`${num(test.lift_ci95?.[0] == null ? null : test.lift_ci95[0] * 100)} – ${num(test.lift_ci95?.[1] == null ? null : test.lift_ci95[1] * 100)} pp`} />
          <Card label="Brier / log loss" value={`${num(test.brier_score, 4)} / ${num(test.log_loss, 4)}`} />
          <Card label="Maximum signal gap" value={count(test.max_signal_gap)} />
          <Card label="Calibration ECE" value={num(test.calibration?.expected_calibration_error, 4)} />
        </div>
      </Panel>
      <div className="grid gap-4 lg:grid-cols-2">
        <Panel title="SHADOW observation" note="Each source-round estimate is frozen before its target. Unproven timing or gaps resolve UNKNOWN.">
          <div className="grid grid-cols-2 gap-3"><Card label="Rounds evaluated" value={count(shadow.rounds_evaluated)} />
            <Card label="Signals / true / false" value={`${count(shadow.signals_generated)} / ${count(shadow.true_signals)} / ${count(shadow.false_signals)}`} />
            <Card label="Precision" value={pct(shadow.precision)} note={shadow.precision_ci95?.lower == null ? '' : `${pct(shadow.precision_ci95.lower)} – ${pct(shadow.precision_ci95.upper)}`} />
            <Card label="Coverage / abstention" value={`${pct(shadow.coverage)} / ${pct(shadow.abstention_rate)}`} />
            <Card label="Rounds since last signal" value={count(shadow.rounds_since_last_signal)} />
            <Card label="Pending / unknown" value={`${count(shadow.pending)} / ${count(shadow.unknown)}`} />
            <Card label="Baseline / signal rate" value={`${pct(shadow.baseline_rate)} / ${pct(shadow.signal_rate)}`} />
            <Card label="Lift" value={shadow.lift == null ? '—' : `${num(shadow.lift * 100)} pp`} />
          </div>
        </Panel>
        <Panel title="Validation and method" note="Thresholds come from validation quantiles; no signal frequency is forced.">
          <div className="space-y-2 text-sm text-zinc-300">
            <div>Best validation model: <b>{data.best_selective_model || 'None qualified'}</b></div>
            <div>Walk-forward: <b>{data.validation?.walk_forward_positive_blocks ?? '—'} / {data.validation?.walk_forward_blocks ?? '—'} blocks positive</b></div>
            <div>500-round stability: <b>{data.block_stability?.positive_lift_blocks ?? '—'} / {data.block_stability?.independent_blocks ?? '—'} blocks positive</b></div>
            <div>1,000-round blocks: <b>{data.block_stability?.['1000_round']?.positive_lift_blocks ?? '—'} / {data.block_stability?.['1000_round']?.independent_blocks ?? '—'} positive</b></div>
            <div>Daily stability: <b>{data.block_stability?.daily?.positive_lift_periods ?? '—'} / {data.block_stability?.daily?.periods ?? '—'} periods positive</b></div>
            <div>Calibration: <b>{data.calibration?.method || 'not fitted'} · ECE {num(data.calibration?.expected_calibration_error, 4)}</b></div>
            <div>Model agreement: <b>{data.model_agreement ? `${count(data.model_agreement.selected_signals_with_two_or_more_models)} of ${count(data.validation?.signals)} validation signals had ≥2 families agree` : 'Not measured'}</b></div>
            <div>Families tested: <b>{(data.algorithms_tested || []).map(row => row.model).filter(Boolean).join(', ') || 'None'}</b></div>
            <div>SHADOW_READY: <b>{ready ? 'YES' : 'NO'}</b></div>
          </div>
          <div className="mt-4 grid grid-cols-2 gap-2 text-xs text-zinc-400">{Object.entries(data.comparison_baselines || {}).map(([label, item]) => <div key={label} className="rounded border border-zinc-800 p-2">Historical {label}: {pct(item.rate)} · {count(item.successes)}/{count(item.rounds)}</div>)}</div>
        </Panel>
      </div>
      <Panel title="Precision versus coverage" note="Thresholds shown are validation-only. Untouched-test metrics above use only the single preselected threshold.">
        {(data.threshold_curves || []).length ? <div className="overflow-x-auto"><table className="w-full min-w-[700px] text-left text-sm"><thead className="text-xs uppercase text-zinc-500"><tr><th className="py-2">Threshold</th><th>Signals</th><th>Coverage</th><th>Precision</th><th>95% CI</th><th>Lift</th><th>Gate</th></tr></thead><tbody>{data.threshold_curves.map((row, index) => <tr key={`${row.threshold}-${index}`} className="border-t border-zinc-800"><td className="py-2">{pct(row.threshold)}</td><td>{count(row.signals)}</td><td>{pct(row.coverage)}</td><td>{pct(row.precision)}</td><td>{pct(row.precision_ci95?.lower)} – {pct(row.precision_ci95?.upper)}</td><td>{row.lift == null ? '—' : `${num(row.lift * 100)} pp`}</td><td>{row.selection_qualified ? 'QUALIFIED' : 'ABSTAIN'}</td></tr>)}</tbody></table></div> : <div className="text-sm text-zinc-500">No threshold curves yet. Run historical research to evaluate candidates.</div>}
      </Panel>
      <div className="rounded-lg border border-zinc-800 bg-zinc-900/40 p-4 text-xs text-zinc-500"><Brain className="mr-2 inline" size={15}/>HISTORICAL and BACKTEST are not SHADOW evidence. LIVE use is not enabled. This research stream cannot change Decision, Risk, or execution.</div>
    </>}
  </div>;
}
