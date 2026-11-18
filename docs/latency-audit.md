# Winner Predict round latency audit

Audit updated: 2026-11-03 UTC

## Implementation update — 2026-11-03 Africa/Kigali

The collector uses Playwright only; the extension, loader, and pairing path are removed. This update separates Rules Engine decisions and urgent signal acceptance from slow Rules Engine snapshot persistence. The **2,000 ms detection-to-Signal-API-acceptance target remains UNKNOWN and is not claimed as achieved**. A post-restart live Aviator sample is now available, but the collector was still running its pre-edit process image during that sample and no signal was accepted.

### Root causes confirmed in the implementation

| Path | Confirmed work on or near the live path | Change |
|---|---|---|
| Backend queue | One serial consumer handled observed decisions and committed-stage settlement. A failed committed event slept in that consumer's retry loop, and each observed evaluation synchronously waited for PostgreSQL persistence before signal publication. | The bounded 512-item priority queue keeps observed rounds FIFO ahead of commit confirmations. Rules state transitions still run on one worker. Commit retries no longer sleep in that worker. A separate bounded persistence queue coalesces snapshots; the durable SQLite inbox rows are marked processed only after PostgreSQL commits. |
| Rules persistence | `RulesEngine.observe_round()` held the engine lock while saving a large dirty session and revisiting retained evaluation/ledger data. This serialized the next round behind database work and blocked signal generation. | The live worker returns the decision without waiting for the snapshot commit. The signal is committed first; a separate single-thread writer captures state under the engine lock, writes outside the lock, and coalesces adjacent snapshots. Existing inbox events remain unprocessed and replay after failure/restart. Immutable child ledger rows are not rewritten on commit-status upgrades. |
| Signal validation | The bridge copied and summarized an entire session, including selection and evaluation history, to validate one decision. | The bridge uses a locked projection of the single selection and active rule IDs. Signal creation still waits for the authoritative local database commit and unique `(session_id,target_round_id)` constraint. |
| Collector acknowledgment | The backend already acknowledged after durable inbox append and in-memory fanout, not after the Rules Engine. Collector status serialization and repeated event-journal lock metadata syncs ran synchronously before dispatch; backend event-loop lag and SQLite append work also added time. | Round event data remains fsynced before transmission. Diagnostic status writes are coalesced for 100 ms, ephemeral lock-directory fsyncs were removed, journal appends use `fdatasync` where available, and the post-commit acknowledgment removes the outbox row in one durable rewrite. |
| Database indexes | `aviator_rounds.round_index` already has a unique ordered index. Distribution replay and signal target queries lacked matching composite indexes. | Migration adds `(target_round_id, created_at, signal_id)` on `bet_signals` and `(stream_id, stream_sequence)` on distribution events. SQLite adds pending observed/committed rule indexes. The recent-100 plan uses the existing round-index index; no redundant history index was added. |
| History context | Latest-100 reads are needed for live context; historical reporting still needs the complete archive. | Live context uses descending indexed retrieval with chronological reversal. Reporting and analytics retain full history; aggregate refresh is coalesced outside the event path. |

The 100-round limits apply only to live working context. PostgreSQL round history, collector outboxes, the backend observation inbox, and normalized Rules Engine ledgers are retained. On collector startup, the legacy-file migration still compares full identity coverage before importing; this prevents an incomplete migration and does not run per round.

The Signal API in this codebase is an internal service backed by PostgreSQL; it does not make an outbound HTTP request to a separate Signal API. Acceptance is measured after the authoritative insert/conflict transaction commits. Collector page-observation and acceptance wall timestamps can be compared on this host, but the provider's original result-available timestamp is unavailable. Monotonic clocks are never subtracted across processes. The API has no downstream-consumer acknowledgment protocol, so downstream receipt and replacement-bet completion remain unmeasured.

### Current validation and live measurement status

This section is a historical record of the pre-Orbit Playwright collector. The
current Round History source is the Orbit Data Watch API; the old browser
collector and its launch path have since been removed.

- **Playwright-only conversion:** `BrowserManager` now imports `chromium` from the official `playwright` package. Removed `playwright-extra`, its Puppeteer stealth plugin, the MV3 source/bridge, extension pairing settings, and the unused Selenium diagnostic driver. The existing Node Playwright collector remains the round-history owner and retains its durable outbox and ordered delivery pipeline.
- **Validation after conversion:** 52 focused BrowserManager/CDP/collector-delivery/PostgreSQL-store Node tests passed; the health regression file passed 13 tests. The full Node suite reported 141 passed and 3 failed. Those failures are in unchanged `LoginManager` test coverage: one page mock lacks Playwright's `.first()` locator method, and two login-route assertions disagree with the current session result. The run duration was 37.55 seconds. These tests did not involve the removed bridge.
- **Observed-round crash recovery:** the inbox stores both observed and committed replay payloads. Events remain pending through signal acceptance and are acknowledged only after a PostgreSQL snapshot commit. The queue is bounded and observed events retain FIFO order. Snapshot commits run on a separate one-thread executor and inbox markers are updated in a batch transaction.
- Rules regressions compare immediate versus deferred persistence for `START_CHECK`, verify stable selection IDs on event replay, compare ordered and prioritized two-stage processing, and confirm a blocked database save does not hold the Rules Engine lock. Inbox tests cover batched observed/committed recovery markers. Node tests cover one-write persisted handoff acknowledgment and outbox recovery.
- Collector tests verify newest-100 in-memory history, mutation follow-up behavior, durable outbox retries, observed-before-commit delivery, ordering, duplicate identity handling, and Playwright page reconnection. Test-injected delay measurements are test behavior checks, not production latency evidence.
- `psycopg-pool` was installed in the active backend virtual environment at version 3.3.3; it is also declared in `backend/requirements.txt`. PostgreSQL integration tests require `TEST_DATABASE_URL` and are skipped when it is absent.
- After the backend restart, the existing Playwright collector recovered its authenticated Aviator frame without a second browser process and accepted 11 real round observations in the live metric window. This was the old collector process image; the edited event-driven CDP observer was not yet loaded during these samples.
- Backend regressions for the inbox, signal bridge, latency hot path, Rules Engine, and signal API passed 141 tests. The focused Node collector/outbox/CDP/PostgreSQL suite passed 36 tests. A wider Node run including `history.test.js` reported 63 passed and one pre-existing LoginManager mock failure (`page.locator(...).first is not a function`); the isolated browser-observer tests passed.
- The measured backend receive-to-final-decision span improved from P50 12,937.70 ms/P95 178,502.44 ms (n=287, pre-change) to P50 1,967.18 ms/P95 6,305.29 ms (n=10, post-restart). Durable API inbox acceptance remains slower than the target at P50 1,946.47 ms/P95 6,267.92 ms (n=11). No signal was generated (`NO_ACTION / NOT_CONFIGURED`), so detection-to-Signal-API acceptance still has zero samples and remains UNKNOWN. Replacement-bet timing is also unmeasured.
- Backend metrics reset on process restart. The current stage comparisons are not the complete page-to-Signal-API path; provider result-available time is unavailable and the Signal API has no downstream-consumer acknowledgment protocol. The prior Qodo review was incomplete because the worktree diff was too large; it was not a clean sign-off.
- The performance endpoint separates backend receive-to-decision-ready (500 ms stage budget), decision-to-durable signal acceptance (500 ms stage budget), and collector-observation-to-acceptance (<2,000 ms). A missing correlated span is **UNKNOWN**; the backend stage cannot make the end-to-end target green. The persisted-state writer queue is displayed separately from the decision queue.
- Dashboard evaluation WebSocket hints use a bounded 256-item queue separate from ordered round processing. When slow clients fill it, the oldest hint is coalesced and the newest view remains queued; the dashboard displays queue depth and coalesced count. Round records, Rules Engine decisions, and accepted signals are persisted independently of these hints.

**Before/after:** the receive-to-final-decision stage moved from P50 12.94 s/P95 178.50 s (n=287) to P50 1.97 s/P95 6.31 s (n=10) in the post-restart backend sample. Durable API acceptance measured separately at P50 1.95 s/P95 6.27 s (n=11), so the full priority path still has tail latency above the 2-second objective. The end-to-end detection-to-Signal-API target is **not verified** because no signal acceptance occurred in the sample.

### Live revalidation after backend restart — 2026-11-03 07:23 UTC

The collector recovered its game frame and delivered real round observations #23436–#23446. The backend process had the priority-path code loaded, but the still-running collector process had not yet loaded the edited CDP binding implementation. Percentiles use nearest-rank; P99 equals the maximum for these small samples.

| Stage | Samples | Average | P50 | P95 | P99 | Maximum |
|---|---:|---:|---:|---:|---:|---:|
| Playwright page mutation to multiplier extraction | 11 | 2.50 ms | 0.70 ms | 11.30 ms | 11.30 ms | 11.30 ms |
| Playwright multiplier extraction | 11 | 2.41 ms | 0.70 ms | 11.20 ms | 11.20 ms | 11.20 ms |
| Collector Node receive to validation | 11 | 64.21 ms | 57.97 ms | 143.90 ms | 143.90 ms | 143.90 ms |
| API durable inbox append | 11 | 2,509.90 ms | 1,946.47 ms | 6,267.92 ms | 6,267.92 ms | 6,267.92 ms |
| API receipt to SSE publication / HTTP acceptance | 11 | 2,535.63 ms | 1,949.78 ms | 6,288.97 ms | 6,288.97 ms | 6,288.97 ms |
| Backend receive to decision ready | 10 | 2,707.10 ms | 1,966.57 ms | 6,304.79 ms | 6,304.79 ms | 6,304.79 ms |
| Backend receive to final decision | 10 | 2,707.71 ms | 1,967.18 ms | 6,305.29 ms | 6,305.29 ms | 6,305.29 ms |
| Rules Engine evaluation, observed and committed stages mixed | 22 | 28.02 ms | 2.05 ms | 188.82 ms | 294.47 ms | 294.47 ms |

The observed-stage worker logs show low queue wait and evaluation for the latest live event (95.41 ms queue, 2.05 ms evaluation, 117.23 ms total); most of its backend receive-to-HTTP-acceptance time preceded rule evaluation while the durable inbox append completed. At the time of this sample the Rules Engine returned `NO_ACTION / NOT_CONFIGURED`. There were zero successful Signal API acceptances, so detection-to-acceptance percentiles are **UNKNOWN**, not zero and not inferred from the backend decision span.

Only 11 live rounds were available before this measurement snapshot, not the requested 100. The sample also predates activation of the edited event-driven CDP collector source; a fresh collector restart and new live rounds are required to measure that code. Dashboard, analytics, and PostgreSQL persistence are outside the signal acceptance span; the collector retains its durable outbox until committed persistence acknowledgment.

### Live backend snapshot before code restart — 2026-11-03 05:38 UTC

These are samples from the running pre-change process. The receive-to-final-decision span includes the durable inbox append, queue wait, marker lookup, Rules Engine, and signal transaction. Component spans overlap and must not be added together.

| Stage | Samples | Average | P50 | P95 | P99 | Maximum |
|---|---:|---:|---:|---:|---:|---:|
| Backend receive to final decision | 287 | 35,164.61 ms | 12,937.70 ms | 178,502.44 ms | 250,991.86 ms | 256,248.20 ms |
| Backend round queue wait | 578 | 32,844.08 ms | 6,020.77 ms | 172,391.22 ms | 235,679.38 ms | 246,951.35 ms |
| Rules Engine total | 578 | 5,313.37 ms | 4,365.51 ms | 11,504.93 ms | 15,451.65 ms | 19,287.94 ms |
| Rules database persistence | 574 | 4,876.60 ms | 3,937.68 ms | 10,884.32 ms | 14,981.74 ms | 17,653.67 ms |
| Signal create/durable acknowledgment | 46 | 4,048.06 ms | 3,335.85 ms | 10,775.52 ms | unavailable | 10,986.23 ms |
| Collector HTTP acknowledgment | 210 | 4,209.41 ms | 2,563.27 ms | 12,012.45 ms | 14,284.88 ms | 14,889.52 ms |
| Backend inbox append | 291 | 1,548.43 ms | 955.30 ms | 4,847.23 ms | 9,429.48 ms | 10,778.76 ms |
| `USER_PLAYING_POLICY` evaluation | unavailable | 5.50 ms | unavailable | unavailable | unavailable | unavailable |

The correlated collector-detection-to-acceptance sample was unavailable, so its P50/P95/P99/maximum and the <2,000 ms target remain **UNKNOWN**. Replacement-bet completion is also unmeasured.

### Earlier task-intake audit figures

The user-supplied audit was another pre-change sample. Missing percentiles were not inferred.

| Stage | Average | P50 | P95 | P99 | Maximum |
|---|---:|---:|---:|---:|---:|
| Collector HTTP acknowledgment | unavailable | 2,374 ms | unavailable | unavailable | unavailable |
| Backend inbox append | 1,487 ms | unavailable | unavailable | unavailable | unavailable |
| Backend event queue wait | unavailable | 2,160 ms | 41,616 ms | unavailable | unavailable |
| Rules Engine total | 5,475 ms | unavailable | unavailable | unavailable | unavailable |
| Rules database persistence | 5,030 ms | unavailable | unavailable | unavailable | unavailable |
| Signal creation and durable acknowledgment | 4,204 ms | unavailable | unavailable | unavailable | unavailable |
| Backend receive to final decision | unavailable | 11,146 ms | unavailable | unavailable | unavailable |
| `USER_PLAYING_POLICY` evaluation | 6 ms | unavailable | unavailable | unavailable | unavailable |

## Result from the initial pre-change audit (2026-11-02)

The sections below preserve measurements and notes from the earlier collector revision, which included the now-removed MV3 path. They are historical evidence only; the current project does not ship or start that path.

The 2,000 ms end-to-end target is **not achieved**. The measured pre-change collector-to-API round trip has a 2,194 ms P50. The backend event worker had a 2,395 ms P50 queue wait and a 70,557 ms P95 queue wait before rules evaluation. The latest backend-only sample after instrumentation is also incomplete: the collector is `AUTH_REQUIRED`, its last successful round was at 20:50:05 UTC, and no new round has traversed the instrumented path.

No timing is inferred across the page, Node collector, and Python backend clock domains. Round IDs are the correlation key. The provider does not expose the original result-available timestamp, and signal consumers do not send delivery acknowledgments.

## Measured baseline

Backend access-log window: 2026-11-02 20:00–20:45 UTC. Collector rolling metrics snapshot: approximately 20:50 UTC. These are actual application samples, not synthetic benchmark delays.

| Stage / measurement | Samples | Average | P50 | P95 | P99 | Maximum |
|---|---:|---:|---:|---:|---:|---:|
| Collector HTTP round trip through backend acknowledgment | 132 | unavailable | 2,194 ms | 12,504 ms | 14,426 ms | 14,758 ms |
| Backend durable inbox append | 85 | 1,622 ms | 952 ms | 5,570 ms | 8,091 ms | 8,091 ms |
| Backend API receipt to SSE publication | 85 | 1,914 ms | 1,112 ms | 6,032 ms | 9,369 ms | 9,369 ms |
| Backend event-worker queue wait | 184 | 14,909 ms | 2,395 ms | 70,557 ms | 88,058 ms | 90,019 ms |
| Rules Engine event evaluation | 184 | 2,865 ms | 2,089 ms | 7,537 ms | 10,197 ms | 11,258 ms |
| Collector local observation to durable append | 102 | unavailable | 661 ms | 1,875 ms | 3,619 ms | 4,285 ms |
| PostgreSQL round write | 80 | unavailable | 402 ms | 2,974 ms | 7,441 ms | 7,441 ms |
| Collector observation to PostgreSQL commit, including backlog | 130 | unavailable | 11,221 ms | 127,434 ms | 127,434 ms | 140,147 ms |
| Collector durable observation append SQL work | 83 | 953 ms | 672 ms | 3,064 ms | unavailable | 4,171 ms |

The observation-to-PostgreSQL-commit span includes time waiting behind work; it is not database execution time. The extension’s page-event-to-extraction metrics had zero samples in the old collector process.

## Historical instrumentation snapshot (2026-11-02)

Backend restarted under `scripts/start-all.js` at 21:15:19 UTC. At 21:17:48 UTC, its event-loop monitor had 1,178 real samples: average 42.28 ms, P50 1.35 ms, P95 150.18 ms, P99 528.43 ms, maximum 17,578.29 ms. This is event-loop scheduling lag, not round processing time.

There are no post-change round-processing percentiles. At 21:15:15 UTC, the collector heartbeat was fresh but its state was `AUTH_REQUIRED`, `loggedIn` was false, and its last successful round was `aviator-7d8095e7ec42ebd3365340d3` (#23013) at 20:50:05 UTC. The live backend queue was empty and idle. Stage timings and the total decision latency therefore remain **UNKNOWN**, not GREEN.

At 21:17:44 UTC, the live performance endpoint returned `UNKNOWN` for total latency, a zero-depth idle queue, and `collector_current=false`. It excludes the older collector HTTP/PostgreSQL samples when choosing the current slowest stage; the only current samples were backend event-loop lag.

Two actual GETs to `/api/v1/signals/latest/action` completed in 1,001 ms and 89 ms in backend access logs after restart. These were action-read API responses, not signal creation or external delivery acknowledgments. No external API acknowledgment samples exist.

## Bottlenecks found in the initial audit

| Location | Measured evidence | Cause / next action |
|---|---|---|
| `backend/main.py`, `_run_round_event_processor` / `_process_round_event` | Queue wait P50 2,395 ms, P95 70,557 ms, max 90,019 ms | Ordered single-worker backlog is the largest measured delay. Keep round order and deduplication; collect fresh per-round queue and execution spans after collector authentication is restored before changing worker concurrency. |
| `backend/app/rules_engine.py`, `RulesEngine.observe_round` and `_persist` | Evaluation P50 2,089 ms, P95 7,537 ms; snapshot/database substage data was not available in the old process | New timing separates lock wait, policy, standard rules, target checks, aggregation, snapshot serialization, and persistence. Do not alter betting rules; use fresh samples to identify which substage consumes the budget. |
| `backend/main.py`, `collector_round_observed` and `RoundObservationInbox` append | Inbox append P50 952 ms, P95 5,570 ms; collector RTT P50 2,194 ms | Durable acceptance dominates collector acknowledgment. Preserve durable-before-ack semantics; fresh correlated measurements must separate inbox storage, API work, and local transport before changing the contract. |
| Collector persistence and PostgreSQL handoff (`bot/collector/DurableRoundOutbox.js`, `PostgresRoundStore.js`) | PostgreSQL write P50 402 ms, P95 2,974 ms; observation-to-commit P50 11,221 ms and P95 127,434 ms | The larger span is mostly queued/backlogged time, not SQL execution alone. Inspect the live outbox and commit queue after login returns; do not weaken durability. |
| Runtime load | One baseline snapshot showed load averages around 19–21 on 4 CPUs, Python around 34% CPU, a Chrome renderer around 56%, and 41 Chrome processes | Resource contention plausibly contributes, but this snapshot does not prove causation. Re-measure under a normal authenticated browser session. |
| Browser page observer | Old page-to-extraction sample count was zero | The earlier observer watched broad DOM changes. The current Playwright-installed observer filters relevant result/phase mutations and debounces 10 ms. |

## Instrumentation changes from the initial audit

- Added round-ID correlation and monotonic timings for API validation, durable inbox append, queue wait, rules evaluation, decision subfunctions, signal persistence/acknowledgment, and event-loop lag.
- Added page-local `performance.now()` spans for mutation-to-extraction and DOM extraction; those durations cross into Node as measured durations, without subtracting unrelated browser-page and Node clock origins.
- Added `/api/rules-engine/performance` and a Rules Engine dashboard with average, P50, P95, P99, maximum, sample counts, slowest current stage, queue status, and GREEN/WARNING/CRITICAL thresholds. Missing decision timing remains UNKNOWN. Stale collector metrics are labeled stale and excluded from the current slowest-stage calculation.
- Reduced the result observer debounce from 350 ms to 10 ms and limited callbacks to relevant DOM fields while retaining the periodic reconciliation scan.
- Reduced Rules Engine page polling from 5 seconds to 15 seconds, refreshed auxiliary lists every 60 seconds, and debounced event-driven refreshes.
- Made the browser observer schedule a follow-up scan when a relevant mutation arrives while another scan is in progress, so a busy scan cannot silently discard that update.
- Kept round evaluation ordered and retained durable inbox/outbox, idempotency, target identity, and existing policies.

## Function-level timings from the initial audit

The new backend records separate spans for `USER_PLAYING_POLICY`, standard policies including `TWO_BET_WINS`, selected-result evaluation, target identity checks, decision aggregation, persistence, and the overall Rules Engine evaluation. There are no fresh round samples for these functions after instrumentation because the collector is unauthenticated. The prior log contains only aggregate worker evaluation time, so per-function before/after values cannot be reported honestly.

## Validation and coverage from the initial audit

- Backend focused regression suite: 41 tests passed, including inbox ordering, Rules Engine behavior, bridge handoff, and performance-status thresholds.
- The earlier Node collector and MV3 suites passed 15 collector pipeline/client tests and 8 MV3 collector tests. These historical checks covered behavior, not performance; the MV3 suite has since been removed with that integration.
- Frontend production build passed.
- No artificial delays were used as performance evidence.
- A real post-change round, rapid-round load, external API destinations, slow external responses, live network interruption, and Playwright page reconnection were not benchmarked in that audit. Isolated tests cover outbox retries, timeout behavior, duplicate identities, and collector reconnection, but these are not live performance measurements. The collector was `AUTH_REQUIRED`, no new round was arriving, and external consumers have no acknowledgment protocol. The local CUA browser inventory also exposed no browser tabs.

The Qodo review was incomplete (`incomplete_input`) because the existing worktree diff was about 625 KB; it returned 119 findings and is not a clean sign-off. I verified its collector `priority` warning is a false positive: the wrapper consumes that keyword to choose an executor. Its busy-scan warning was valid and is fixed above. It also identified that unchanged multiplier history has no distinct round identity; without a provider round ID, forcing an event for an unchanged snapshot could create a duplicate, so this remains an identity limitation. A separate finding about the final `max_selections` execution gate would change betting execution behavior and was left untouched under the instruction to preserve betting logic.

## Current target assessment

**The 2,000 ms target is not verified and is not claimed as achieved.** The pre-change process had severe correlated receive-to-decision and queue delays, shown above. Current code has not yet produced a post-restart real round because the collector is `AUTH_REQUIRED`; post-change P50, P95, P99, and maximum are **UNKNOWN**. The provider does not expose its original result-available timestamp, the Signal API has no downstream-consumer acknowledgment protocol, and replacement-bet completion relative to next-round start is unmeasured.
