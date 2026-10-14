# Winner Predict Rules Engine migration

## Source audit

No Phase 1 analysis report was present in either checkout during this audit. The source was inspected directly in `src/rule-engine.js`, `src/rule-reducer.js`, `src/live-shadow-test.js`, `src/five-rule-experiment.js`, `src/combined-rules-test.js`, its three public interfaces, and the source tests and saved `combined-recorded-50.json` fixture.

The three requested source sections contain four separate execution paths: **Run Controls — Start a Check**, **Legacy — Separate Tracker**, the five-rule independent tracker card, and **Test — All Rules Together**. The five-rule card is a real additional path in the source UI, so it remains a separate destination mode instead of being hidden inside the single-rule legacy tracker.

| Destination mode | Source implementation | Window / stake / stops | Result handling |
|---|---|---|---|
| `START_CHECK` | `rule-engine.js` + `rule-reducer.js` | 50 verified rounds; source fallback for old records is 40; fixed 100 RWF at 2.00x; NET +400 / -400 stops new intents; optional next check | One source policy ledger; an intent is written from source N for N+1 before target outcome. Stop limits do not end observation of the current window. |
| `LEGACY_SEPARATE_TRACKER` | `live-shadow-test.js` | 50 verified rounds; fixed 100 RWF at 2.00x; NET +400 / -400; manual stop and recovery are local to this tracker | One separately owned instance of the older reducer. Its own requests alone advance its result state. |
| `FIVE_RULE_TRACKER` | `five-rule-experiment.js` | 50 verified rounds; five independent rule states and evaluations | Each rule keeps its own prospective state and result provenance. TRUE contributors for one target are merged into exactly one shared selection and one intent. |
| `ALL_RULES_TOGETHER` | `combined-rules-test.js` | 50 verified rounds; fixed simulated stake; optional total WIN and LOSS limits, max selections, and auto-next preference | All enabled proposals are collected before one coordinator creates at most one target selection. Count limits stop new selections; the 50-round observer continues. |

## Participating rule manifest

Every rule below has a separate session engine instance, private state, pending target, and evaluation record. Each session persists the exact participating definitions, versions, conditions, initial state, transition rules, configuration snapshot, and manifest SHA-256. Configuration changes increment a version and affect only newly started sessions.

| ID | Version | Exact condition | Initial state | State/result transition |
|---|---|---|---|---|
| `USER_PLAYING_POLICY_START_CHECK` | `AVIATOR_2X_CONSECUTIVE_STOP_V3` | Wait for L then H. The trigger H seeds the legacy reducer's previous result. In STANDARD, selected results stop on HH or LL. Two consecutive LL-ending cycles activate TWO_BET_WINS. That mode stops after two consecutive selected H outcomes. Stake 100 RWF; cashout 2.00x; NET +400/-400. | `WAIT_LOW`, `STANDARD`, no pending request, zero failed cycles / win streak. | H trigger requests N+1. Resolve only that exact request. STANDARD LL terminal L leaves state waiting for H. No decision after the 50th observed source. This preserves the older source reducer semantics. |
| `USER_PLAYING_POLICY_LEGACY_TRACKER` | `AVIATOR_2X_CONSECUTIVE_STOP_V3` | Same old reducer calculations as Start a Check, but a separately owned tracker/ledger. | `WAIT_LOW`, `STANDARD`, no pending request. | Same old reducer transitions; manual stop/recovery and counters belong only to this tracker. |
| `RECENT_5_H_GE_4` | `1.0.0` | Latest five consecutive verified outcomes including source; H is original multiplier >=2.00; TRUE if count >=4. The latest source can be L. | Warming up, no pending target. | TRUE requests source+1 once. FALSE only after a ready, verified five-round window. |
| `PATTERN_LH` | `1.0.0` | Latest two consecutive verified outcomes exactly L,H. | Warming up, no pending target. | Each matching source requests only its immediate next target. |
| `PATTERN_HH` | `1.0.0` | Latest two consecutive verified outcomes exactly H,H. | Warming up, no pending target. | Re-evaluated for every source; overlapping HHs can request distinct successive targets. |
| `SOURCE_H_MULT_2_3` | `1.0.0` | Original source multiplier >=2.00 and <3.00. | Wait, no pending target. | Exactly 2.00 qualifies; exactly 3.00 does not. Each TRUE requests source+1. |
| `AFTER_HH_CONTINUE_MAX_3` | `1.0.0` | Idle HH starts a cycle. Request up to three successive targets, one request at a time. Ignore HH while the cycle is active. | IDLE, no cycle, no pending target. | Count each matching requested target once. Complete and rearm at the source of target three's resolved outcome, regardless of WIN/LOSS. No STANDARD first-WIN or WW/LL stop applies. |
| `USER_PLAYING_POLICY` | `AVIATOR_2X_CONSECUTIVE_STOP_V3` | Accepted All Rules Together policy. STANDARD waits L,H; trigger H requests N+1 and is not a selected result. First selected H ends a STANDARD cycle; after a first selected L, continue until selected HH or LL. TWO_BET_WINS activates after two consecutive STANDARD LL cycles. | `WAIT_LOW`, `STANDARD`, no pending request, zero failed cycles / selected-result streak. | A successful STANDARD cycle resets the failure streak. TWO_BET_WINS trigger H is not a selected WIN; selected LL ends the cycle and retains special mode; selected HH clears it. Only this policy's own matching combined selection advances its result state. |

`TWO_BET_WINS` is a mode inside each policy engine, not a registered duplicate rule. The Start Check source reducer and the accepted combined policy have a real semantic conflict: the old reducer seeds its sequence with trigger H and the old special mode stops at two consecutive selected wins; the combined accepted policy does not count trigger H and uses first-selected-WIN / WW/LL semantics. The migration keeps those as separate policy IDs and session modes. It does not silently relabel old results as the newer policy.

## Data, identity, and safety contract

Prospective source decisions are created from ordered collector `round.observed` events marked `PENDING` and `VERIFIED`; the collector event carries an observed completed multiplier and a durable outbox entry. The decision and selection are persisted before the matching `round.persisted` PostgreSQL confirmation. That confirmation advances the committed cursor and updates persistence metadata without reevaluating or changing the original decision timestamp. Historical/recovered `round.persisted` events can resolve an already saved target, but cannot create a selection when the target round already exists. H/L uses the supplied multiplier value, threshold 2.00, and no display rounding in the engine. Invalid shared input yields BLOCKED; insufficient rule history yields `eligible: null` and `UNAVAILABLE_HISTORY`. No unavailable result becomes FALSE or LOSS.

The shared selection records source N, expected target N+1, all contributing IDs and reasons, and its decision timestamp before the target resolves. A target resolves once by stream and round index to WIN (>=2.00), LOSS (<2.00), or stays PENDING when identity/outcome evidence is unavailable. The source event dispatcher uses the durable round-distribution cursor; duplicate persisted events are idempotent. The collector does not depend on the engine and execution is never authorized here.

Rule configuration, presets, private states, cursors, decisions, outcomes, and counters persist atomically as a versioned JSON snapshot in the existing `application_state` record (`rules_engine_state_v1`). The repository also writes the runtime ledgers in the same database transaction: session/configuration snapshots, one row per rule evaluation, shared selections and contributors, blocked execution intents, target outcomes, platform balance observations, reconciled session financials, and stop events. Duplicate source observations upsert the same per-rule evaluation row; an intent is unique per target and betting panel. The full additive normalized PostgreSQL schema and registry seed remain in `backend/migrations/20261014_rules_engine_shadow.sql`, with a table-specific rollback; that standalone migration has not been applied. The current session has not verified PostgreSQL runtime writes against the configured authoritative server. No real round, account, financial, audit, or execution data was changed during implementation.

## Verification limits

The copied fixture in `backend/tests/fixtures/combined-recorded-50.json` is the source project's saved completed experiment, marked `HISTORICAL_REPLAY_FIXTURE`; it is used only by isolated tests. It contains collector-observation hashes, not provider-issued round IDs. The source code embeds registry version `historical_rule_discovery_v1` and implementation SHA `e62c32bfdec992eece3e0b9212954414ec8aa95ae7cbfe078ecf222a63bc57f3`; a separate raw frozen registry file was not found, so that embedded hash was not independently re-derived from an artifact.

The destination's existing `aviator_rounds.multiplier` column is `NUMERIC(12,2)`. The engine does not round values, but any precision already discarded by the collector's persisted schema cannot be recovered here. The live collector and backend ports were not running during this work, so no live incoming-round SHADOW proof is claimed.

## Final verification

- Destination backend suite: **214 passed, 2 skipped**. The skipped PostgreSQL persistence cases require `TEST_DATABASE_URL`.
- Rules migration and source parity suite: included in the full backend run; it replays the source project's saved fixture against the source implementations for Start Check, Legacy Separate Tracker, five independent trackers, and All Rules Together.
- Source project Node suite: **139 passed** in the earlier isolated run.
- Frontend production build: **passed**, 1,842 modules transformed.
- PostgreSQL 17/main is currently DOWN; `http://localhost:8000/health` refused the connection. Starting PostgreSQL was attempted and failed because `sudo` requires an interactive password. No service was started, no fresh database dump was created for this migration, and the additive Rules Engine SQL was not applied.
- The already verified pre-cleanup PostgreSQL dump remains at `/tmp/winner-predict-postgres-before-prediction-removal.dump` (SHA-256 `2830108a85ebbc649dd13ea472d94db4b5c7acf140b1bb9024573cdba6c908ad`). It predates this Rules Engine migration and is not presented as the required fresh pre-migration backup.
- No live collector, browser, backend, PostgreSQL persistence, or real-data SHADOW check is claimed. Automatic real-money execution remains OFF.
