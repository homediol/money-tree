# Prospective validation plan

The frozen exploratory shortlist is: binary_3_010, rate_5_le_2, logvol_50_le_1_0. Historical final-period results are contaminated by prior candidate/model evaluation and do not qualify as confirmation.

## Before collection

1. The user must explicitly select one `EDGE_UNVERIFIED` policy and set stake, cooldown, session loss limit, consecutive-loss limit, profit goal, and reset schedule. No rule is selected or enabled automatically.
2. Add stable platform round identity or platform order evidence to each prospective record. The historical `COLLECTOR_OBSERVATION_HASH` plus `LOCAL_SEQUENCE` metadata supports relative order only.
3. Freeze the selected policy JSON, source code version, dataset boundary, target definition, 2.00x cashout, decision expiry, and operational risk configuration. Record their hashes before observing any prospective outcome.
4. Keep the existing risk engine and all operational checks active. A failed login, browser/page health, continuity, target identity, balance, session limit, duplicate check, or risk check emits `STOP` and no bet.

## Prospective event ledger

Before each target round, persist exactly one decision with `policy_id`, `policy_version`, source and target round IDs, actual UTC decision timestamp, expiry after the next target only, trigger evidence, reason codes, and evidence status. Store the target result only after settlement. Never infer a missing round or join a feature window across a continuity gap.

## Decision record contract

The following is a ledger shape, not a betting instruction. The selected policy may emit `BET` only after the user explicitly selects it, its trigger is true for the next eligible target, and every independent operational safety gate passes. A false trigger, cooldown, or insufficient warm-up emits `WAIT`. Any stale/missing data, failed continuity, invalid target, authentication/browser/balance/session/risk/duplicate gate emits `STOP`; safety can never be overridden by policy eligibility.

```json
{
  "policy_id": "<explicitly selected policy ID>",
  "policy_version": "<frozen version>",
  "source_round_id": "<verified source round ID>",
  "target_round_id": "<next eligible verified round ID>",
  "decision": "BET | WAIT | STOP",
  "decision_timestamp_utc": "<actual ISO-8601 UTC decision time>",
  "expires_after_target_round_id": "<same target round ID>",
  "trigger_evidence": {
    "condition_id": "<candidate ID>",
    "satisfied": true,
    "feature_values": {},
    "feature_end_round_id": "<source round ID>"
  },
  "reason_codes": ["TRIGGER_MET", "ALL_OPERATIONAL_GATES_PASSED"],
  "evidence_status": "EDGE_UNVERIFIED",
  "operational_safety": {
    "authentication": "PASS",
    "browser_health": "PASS",
    "continuity": "PASS",
    "target_validity": "PASS",
    "balance": "PASS",
    "session_limits": "PASS",
    "duplicate_protection": "PASS",
    "risk_engine": "PASS"
  }
}
```

Use the policy evidence status (`HISTORICAL_EDGE_SUPPORTED`, `EDGE_UNVERIFIED`, `INSUFFICIENT_DATA`, or `REJECTED`) as `evidence_status`; this project produced only disabled `EDGE_UNVERIFIED` shortlist configs. Historical collector observation hashes are not substitutes for the stable platform round IDs required for prospective records.

## Sample and stopping rule

Use the next **2,000 eligible prospective targets** after the frozen policy selection. Make one confirmatory analysis at 2,000; do not peek or retune during collection. If fewer than 200 policy entries occur, or if any platform identity/continuity requirement fails, report `INSUFFICIENT_DATA` and continue collecting only under a separately frozen extension plan. Use Wilson 95% hit-rate intervals and a 20-round moving-block bootstrap for net return. Compare against 50% break-even and the contemporaneous unconditional baseline. A confidence interval crossing zero return is inconclusive; positive historical outcomes do not change stakes or bypass risk limits.

## Decision contract

- `BET`: the user-selected rule triggers for the next eligible target and every operational safety gate passes.
- `WAIT`: no trigger, insufficient warm-up history, or configured cooldown remains.
- `STOP`: any continuity, authentication, browser, balance, target validity, duplicate, session-limit, risk, or expiry gate fails.

Policy eligibility never overrides operational safety. Historical 2.00x settlement assumptions are not evidence of live placement or cashout execution.
