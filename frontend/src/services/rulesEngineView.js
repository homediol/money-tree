function isExpiredStatus(value) {
  return String(value ?? '').trim().toUpperCase() === 'EXPIRED';
}

// Expired execution intents and target selections remain in the backend audit
// history, but are not actionable/current state and should not appear on the
// Rules Engine dashboard.
export function currentExecutionIntents(intents = []) {
  return intents.filter((intent) => !isExpiredStatus(intent?.execution_status));
}

export function currentSelections(selections = []) {
  return selections.filter((selection) => !isExpiredStatus(selection?.status));
}
