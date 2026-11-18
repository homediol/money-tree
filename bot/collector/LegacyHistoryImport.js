/** Pure helpers retained for one-time legacy history imports and their tests. */
export function selectMissingLegacyRounds(fileHistory, persisted) {
  const persistedIds = new Set(persisted.map(round => String(round.round_id)));
  const persistedIndices = new Set(persisted.map(round => Number(round.round_index)));
  const missing = [];
  let indexConflicts = 0;
  for (const round of fileHistory) {
    if (persistedIds.has(String(round.round_id))) continue;
    if (persistedIndices.has(Number(round.round_index))) {
      indexConflicts += 1;
      continue;
    }
    missing.push(round);
  }
  return { missing, indexConflicts };
}

export function canContinueReadOnlyCollection(sessionStatus) {
  return sessionStatus === 'AUTHENTICATED' || sessionStatus === 'UNKNOWN';
}
