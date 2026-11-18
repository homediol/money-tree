# Adaptive Dominance Intelligence Engine (ADIE)

ADIE is a deterministic, explainable observer inside the existing Winner Predict SHADOW backend. It receives observed rounds from the existing `RoundObservationInbox` / ordered round event worker. Its event is persisted before the observed-stage acknowledgement returns to the collector. It does not use a second collector, API service, or round database, and it does not change Rules Engine selection or execute wagers.

## Runtime contract

- Source: existing `/api/collector/round-observed` accepted event and its ordered worker. The PostgreSQL committed-stage replay fills stream sequence and commit time when available.
- Eligibility: only fresh (`observed_at` to API receive within 15 seconds), `LIVE`, `VERIFIED` observed rounds increment activity or permit new forecasts. `GAP_BEFORE`, `UNVERIFIED`, recovered, stale, or missing identity timing does not create a new forecast. A continuity break makes an active window `INCOMPLETE`.
- Identity: `position` is an ADIE-local sequence allocated from observed events. Provider IDs and target identities remain unknown until observed; no future provider IDs are inferred.
- Outcome classes: HIGH `>2.00x`, LOW `<2.00x`, EXACT `=2.00x`. Simulated fixed-stake settlement treats EXACT as a HIGH win and a LOW loss; dominance counts retain EXACT separately. A win is +1, a loss −1.
- Lifecycle: selected windows are transactionally committed as `LOCKED` (offset zero) or `WAITING` (delayed), then `ACTIVE`, `COMPLETED`, or objectively `INCOMPLETE`. Forecast direction, horizon, offset, and sequence boundaries are immutable. Settlement rows are idempotent.
- Policy: `adie-shadow-exploratory-v1`, cohort `prospective-2026-11-16`. Every eligible round records a decision and abstention. Normal selection currently abstains because no Phase 2 context evidence passed corrected validation. An alternating HIGH/LOW `FORCED_EXPLORATORY` 5-round immediate window is committed at 100 eligible rounds without a selection; the UI marks the watch at 80. This is research coverage, not a prediction or profitability claim.
- Frozen policy configuration: `backend/app/resources/adie_policy_v1.json`. Its SHA-256 is copied into every prospective feature record and selected forecast evidence; change the policy version and evaluation cohort before altering it.
- Candidate envelope: HIGH/LOW, 5/10/20/30/50 rounds, offsets 0–10. Primary horizons 5/10 are emphasized for feedback cadence only. Delayed candidates lack validated offset-specific live qualification and therefore cannot pass the locked normal policy.
- Historical context: `backend/scripts/build_adie_research_reference.py` creates `backend/app/resources/adie_research_reference.csv.gz` from Phase 2 `round_level_features.csv.gz` and `context_similarity_evaluation.csv.gz`. Runtime nearest contexts use past-only fingerprints and 60-position spacing. Their outcome aggregates are retrospective, overlapping, exploratory summaries and do not qualify a forecast.
- Research evidence: Phase 2 found zero corrected individual features, interactions, and primary window comparisons. HIGH-5 offset-6 (`q=.118`) and width-20 persistence (`q=.054`) were exploratory. No predictive edge is validated. The exact findings are in `research_phase2_2026-11-16/reports/`.

## Persistence and APIs

Tables are created idempotently when ADIE initializes against the existing repository database. The deployment SQL is `backend/migrations/20261116_adie_shadow_intelligence.sql`; rollback SQL is provided beside it. ADIE initialization failure is isolated and does not stop existing SHADOW rules.

- `GET /api/adie/status`
- `GET /api/adie/forecasts?limit=100&status=COMPLETED`
- `GET /api/adie/decisions?limit=100`
- `GET /api/adie/performance`

These endpoints use the existing authenticated application API boundary. The dashboard is `/dominance-intelligence`.

## Reproduce the compact research reference

From the project root:

```bash
python backend/scripts/build_adie_research_reference.py
```

The script requires the Phase 2 round-level features and context similarity evaluation artifacts. It reports a hard error if either is missing. No ML, tuning, or black-box scoring is used.

## Validation and interpretation

Prospective results start at the first ADIE observed event after deployment. The policy and cohort are versioned; there is no online tuning. Overlapping windows are tracked per forecast and must not be treated as independent validation samples. Fixed HIGH/LOW baselines are displayed from Phase 2 non-EXACT rates. Accounting is simulated SHADOW accounting, not realized betting profit.

Before making predictive claims, collect a locked prospective cohort across multiple chronological periods with complete continuity, retain every abstention and forced window, compare against fixed-direction/horizon and unconditional baselines with block-aware uncertainty and multiplicity correction, and validate the selection rule on a later untouched cohort. Material policy changes require a new policy version and cohort.

## Current implementation limits

- The existing collector does not expose provider-global next-round IDs before settlement. ADIE therefore locks exact local sequence positions and fills round IDs only when observed.
- Phase 2's available context evaluation artifact summarizes immediate horizon contexts; it does not supply defensible delayed-offset context distributions. Normal selection remains abstention-first for all offsets.
- The initial prospective policy is intentionally conservative and currently expected to produce forced exploratory windows only. Timing diagnostics beyond API received / observed / decision committed are limited by the existing event contract; provider page-open and round-open timestamps are unknown.
- The engine retains up to 200 recent rounds for incremental features and the fixed Phase 2 reference in process memory. Durable ledgers and round-level identity are in the existing PostgreSQL database.
