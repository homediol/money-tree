import { candidateCycleState, candidateReason } from '../services/modelCandidates.js';

const number = value => Number.isFinite(value) ? value.toFixed(6) : '—';

export default function ModelCandidates({ cycle, savedReport = null, activeModelVersion = null, waitingReasons = [] }) {
  const { report, rows, legacy, waiting } = candidateCycleState(cycle, savedReport, waitingReasons);
  const deployedVersion = cycle?.active_model_version || activeModelVersion || report?.active_model_version;
  return <div>
    <p className="mb-3 text-xs text-zinc-400">Validation-only ranking, frozen before final verification. Lower ranking score is better; selection-gate qualifiers rank first, then candidates sort by the composite score, selection log loss and model ID. Each model uses the same past-only features, chronological splits and baselines. Test results cannot change the order or promote a runner-up.</p>
    <p className="mb-3 break-all text-xs text-zinc-300">Cycle: {report?.cycle_id || (legacy ? 'Saved legacy evaluation' : 'No saved evaluation')} · Concurrency: {report?.model_concurrency ?? '—'} · Outcome: {report?.deployment_outcome || (deployedVersion ? 'Existing model; cycle outcome unavailable' : report ? 'NOT_DEPLOYABLE' : 'Not recorded')} · Deployed: {deployedVersion || 'NONE'}</p>
    {legacy && <p className="mb-3 rounded border border-amber-500/30 bg-amber-500/10 p-2 text-xs text-amber-200">Showing the saved evaluation because this backend has not published a multi-model cycle report. Ranks below are reconstructed from validation metrics only. Missing test results and deployment decisions are shown as unavailable.</p>}
    {waiting ? <p className="text-xs text-amber-300">No candidate report is available. {waiting}</p> :
      <div className="overflow-x-auto"><table className="w-full text-left text-xs">
        <thead className="text-zinc-500"><tr>{['Rank / model', 'Training time', 'Ranking score ↓', 'Validation Brier', 'Validation advantage', 'Walk-forward', 'Untouched test Brier / advantage', 'Result / reason'].map(label => <th key={label} className="px-2 py-2">{label}</th>)}</tr></thead>
        <tbody>{rows.map(row => <tr key={row.algorithm} className="border-t border-zinc-800 align-top">
          <td className="max-w-xs px-2 py-3"><div className="font-semibold">{row.rank ?? '—'} · {row.algorithm}{row.selected && ' · SELECTED'}</div><div className="mt-1 break-all text-zinc-500">{row.model_id || (legacy ? `Saved under ${report?.model_version || 'unknown model version'}` : 'Candidate ID unavailable')}</div></td>
          <td className="px-2 py-3">{Number.isFinite(row.training_time_seconds) ? `${row.training_time_seconds.toFixed(2)}s` : '—'}</td>
          <td className="px-2 py-3">{number(row.selection_score)}<div className="text-zinc-500">lower is better</div></td>
          <td className="px-2 py-3">{number(row.selection?.brier_score)}<div className="text-zinc-500">ranking holdout</div></td>
          <td className="px-2 py-3">{number(row.selection_brier_advantage)}</td>
          <td className="px-2 py-3">{row.folds_beating_baseline ?? 0}/{row.walk_forward?.length ?? 0} passed
            {!!row.walk_forward?.length && <details className="mt-1 text-zinc-400"><summary className="cursor-pointer">Fold results</summary>{row.walk_forward.map((fold, index) => <div key={index} className="mt-1">Fold {index + 1}: Brier {number(fold.metrics?.brier_score)}, advantage {number(fold.brier_advantage)} · train [0,{fold.train_end}), evaluate [{fold.evaluation_start},{fold.evaluation_end})</div>)}</details>}
          </td>
          <td className="px-2 py-3">{number(row.test?.brier_score)} / {number(row.test_brier_advantage)}</td>
          <td className="min-w-48 px-2 py-3"><div className={row.deployable ? 'text-emerald-300' : 'text-amber-300'}>{row.status || (legacy ? 'SAVED LEGACY REPORT' : 'UNKNOWN')}</div><div className="mt-1 text-zinc-400">{candidateReason(row, report)}</div></td>
        </tr>)}</tbody>
      </table></div>}
  </div>;
}
