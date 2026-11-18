const CONTROLLABLE_SESSION_STATUSES = new Set(['RUNNING', 'SETTLING', 'PAUSED']);

export function ruleSelectionRequest(selectionMode, selectedIds, availableRules) {
  if (selectionMode === 'behaviour') {
    return { rule_ids: [], automatic_rule_selection: false,
      adaptive_rule_selection: false, behaviour_detection_enabled: true };
  }
  if (selectionMode === 'automatic') {
    return { rule_ids: [], automatic_rule_selection: true };
  }
  if (selectionMode === 'adaptive') {
    return { rule_ids: [], automatic_rule_selection: false, adaptive_rule_selection: true };
  }
  const enabled = new Set((availableRules || []).filter((rule) => rule.enabled)
    .map((rule) => rule.rule_id));
  return { rule_ids: [...new Set((selectedIds || []).filter((id) => enabled.has(id)))],
    automatic_rule_selection: false };
}

export function sessionSelectionLabel(session) {
  const strategyId = session?.strategy_id || session?.configuration_snapshot?.strategy_id;
  if (strategyId === 'BEHAVIOUR_ROLLING_HIGH'
      || session?.behaviour_detection_enabled === true
      || session?.configuration_snapshot?.behaviour_detection_enabled === true) {
    return 'Behaviour Detection';
  }
  if (strategyId === 'ADAPTIVE' || session?.adaptive_rule_selection === true) return 'Adaptive';
  if (strategyId === 'AUTOMATIC' || session?.automatic_rule_selection === true) return 'Automatic';
  return 'Manual';
}

export function sessionSelectionDetail(session) {
  if (sessionSelectionLabel(session) === 'Behaviour Detection') {
    return 'Rolling HIGH eligibility · Rules Engine and SHADOW handle signal execution and settlement';
  }
  if (session?.adaptive_rule_selection === true) {
    return `Current portfolio: ${session.active_rule_ids?.join(', ') || 'none'} · version ${session.adaptive_selection_version ?? 0}`;
  }
  if (session?.automatic_rule_selection === true) {
    return `Current choice: ${session.active_rule_ids?.join(', ') || 'none'}`;
  }
  return `Selected rules: ${session?.rule_ids?.join(', ') || 'none'}`;
}

export function isRuleSelectionApplied(session, { mode, selectionMode, selectedIds, availableRules } = {}) {
  if (!session || session.mode !== mode) return false;
  const requested = ruleSelectionRequest(selectionMode, selectedIds, availableRules);
  const behaviourStrategySelected = session.strategy_id === 'BEHAVIOUR_ROLLING_HIGH'
    || session.configuration_snapshot?.strategy_id === 'BEHAVIOUR_ROLLING_HIGH';
  if (Boolean(session.automatic_rule_selection) !== Boolean(requested.automatic_rule_selection)
      || Boolean(session.adaptive_rule_selection) !== Boolean(requested.adaptive_rule_selection)
      || (requested.behaviour_detection_enabled === true
        && session.behaviour_detection_enabled !== true
        && session.configuration_snapshot?.behaviour_detection_enabled !== true
        && !behaviourStrategySelected)) {
    return false;
  }
  if (selectionMode !== 'manual' && selectionMode !== 'behaviour') return true;
  if (selectionMode === 'behaviour') return (session.rule_ids || []).length === 0;
  const currentIds = [...new Set(Array.isArray(session.rule_ids) ? session.rule_ids : [])];
  return currentIds.length === requested.rule_ids.length
    && requested.rule_ids.every((ruleId) => currentIds.includes(ruleId));
}

function mergeSession(existing, updated) {
  if (!existing || existing.session_id !== updated?.session_id) return updated;
  return { ...existing, ...updated };
}

export function isControllableSession(session) {
  return Boolean(session?.session_id && CONTROLLABLE_SESSION_STATUSES.has(session.status));
}

export function updateSessionListAfterAction(sessions, updated) {
  if (!updated?.session_id) return Array.isArray(sessions) ? sessions : [];
  const current = Array.isArray(sessions) ? sessions : [];
  const existing = current.find((session) => session.session_id === updated.session_id);
  const rows = [mergeSession(existing, updated),
    ...current.filter((session) => session.session_id !== updated.session_id)];
  return rows.sort((left, right) => String(right.started_at || '')
    .localeCompare(String(left.started_at || '')));
}

export function updateStatusAfterSessionAction(status, updated) {
  if (!updated?.session_id) return status;
  const current = status || {};
  const next = { ...current };
  const active = current.active_session;
  const latest = current.latest_session;

  if (active?.session_id === updated.session_id || (!active && isControllableSession(updated))) {
    next.active_session = isControllableSession(updated)
      ? mergeSession(active, updated) : null;
  }
  if (!latest || latest.session_id === updated.session_id) {
    next.latest_session = mergeSession(latest, updated);
  }
  if (Array.isArray(current.sessions)) {
    next.sessions = updateSessionListAfterAction(current.sessions, updated);
  }
  if (current.reporting_session_id === updated.session_id
      || next.active_session?.session_id === updated.session_id) {
    next.reporting_session_id = updated.session_id;
    next.reporting_session_status = updated.status;
    next.selection_count = updated.selection_count ?? current.selection_count;
    next.total_wins = updated.results?.WIN ?? current.total_wins;
    next.total_losses = updated.results?.LOSS ?? current.total_losses;
    next.pending = updated.results?.PENDING ?? current.pending;
    next.net_outcome = updated.net_outcome ?? current.net_outcome;
  }
  next.status = next.active_session?.status
    || (Number(next.rules_configured) > 0 ? 'READY' : 'NOT_CONFIGURED');
  return next;
}
