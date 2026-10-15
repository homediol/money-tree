export function candidateRows(cycle, savedReport = null) {
  const isCurrentCycle = Boolean(cycle?.cycle_id || cycle?.ranking?.length
    || Object.values(cycle?.models || {}).some(row => Number.isFinite(row.rank)));
  const rowsFromCycle = cycle?.models && Object.keys(cycle.models).length ? cycle.models : null;
  const report = isCurrentCycle ? cycle : savedReport?.models ? savedReport : cycle;
  const rows = Object.entries((isCurrentCycle ? rowsFromCycle : null) || savedReport?.models || rowsFromCycle || {}).map(([algorithm, row]) => ({
    algorithm, ...row,
    test: row.test || (algorithm === report?.algorithm ? report?.test_metrics : null),
    test_brier_advantage: row.test_brier_advantage ?? (algorithm === report?.algorithm
      ? report?.overfitting_checks?.test_brier_advantage_uncertainty?.point : null),
    legacy_report: !isCurrentCycle,
  }));
  if (!isCurrentCycle) {
    rows.sort((a, b) => {
      const gate = row => row.passes_selection_gate ?? (
        Number.isFinite(row.selection_brier_advantage) && row.selection_brier_advantage >= 0.001
        && Number(row.folds_beating_baseline || 0) >= 2 && (row.walk_forward?.length || 0) >= 3
      );
      const score = row => Number.isFinite(row.selection_score) ? row.selection_score
        : Number.isFinite(row.selection?.brier_score) ? row.selection.brier_score : Infinity;
      const logLoss = row => Number.isFinite(row.selection?.log_loss) ? row.selection.log_loss : Infinity;
      return Number(gate(b)) - Number(gate(a)) || score(a) - score(b)
        || logLoss(a) - logLoss(b)
        || (a.algorithm < b.algorithm ? -1 : a.algorithm > b.algorithm ? 1 : 0);
    });
    rows.forEach((row, index) => { row.rank = Number.isFinite(row.selection?.brier_score) ? index + 1 : null; });
  } else {
    rows.sort((a, b) => (a.rank ?? Infinity) - (b.rank ?? Infinity) || a.algorithm.localeCompare(b.algorithm));
  }
  return rows;
}

export function candidateReason(row, savedReport = null) {
  if (row.rejection_reasons?.length) return row.rejection_reasons.join('; ').replaceAll('_', ' ');
  if (row.legacy_report) {
    const reasons = [];
    if (Number.isFinite(row.selection_brier_advantage) && row.selection_brier_advantage < 0.001) {
      reasons.push('selection Brier advantage below 0.001');
    }
    if (Number(row.folds_beating_baseline || 0) < 2) reasons.push('fewer than 2 of 3 walk-forward folds beat baseline');
    if ((row.walk_forward?.length || 0) < 3) reasons.push('three walk-forward results not recorded');
    if (!row.test) reasons.push('per-model final-test result not recorded');
    if (row.algorithm === savedReport?.algorithm) {
      const storedRejections = savedReport?.overfitting_checks?.rejection_reasons || [];
      if (storedRejections.length) reasons.push(...storedRejections.map(reason => reason.replaceAll('_', ' ')));
      else if (savedReport?.validated === false) reasons.push(savedReport.selection_reason || savedReport.message || 'saved candidate was not validated');
    }
    const activeVersion = savedReport?.active_model_version;
    if (activeVersion && activeVersion === savedReport?.model_version && row.algorithm === savedReport?.algorithm) {
      reasons.push('this saved report is the active model version');
    }
    reasons.push('legacy report; it does not contain a cycle-level deployment decision');
    return reasons.join('; ');
  }
  if (row.deployable) return row.selected ? 'All evaluation gates passed — rank 1' : 'Gates passed — not validation rank 1';
  return 'No cycle-level verification recorded';
}

export function candidateCycleState(cycle, savedReport, waitingReasons = []) {
  const hasCurrentCycle = Boolean(cycle?.cycle_id || cycle?.ranking?.length
    || Object.values(cycle?.models || {}).some(row => Number.isFinite(row.rank)));
  const hasSavedModels = Boolean(savedReport?.models && Object.keys(savedReport.models).length);
  const report = hasCurrentCycle ? cycle : hasSavedModels ? savedReport : cycle;
  const rows = candidateRows(cycle, savedReport);
  return {
    report,
    rows,
    legacy: !hasCurrentCycle && rows.length > 0,
    waiting: rows.length === 0
      ? waitingReasons.length ? waitingReasons.join(' · ') : 'No evaluation report has been saved yet.'
      : null,
  };
}
