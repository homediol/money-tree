# SPRIBE Demo runtime verification

Observed on 2026-10-15 using the official SPRIBE Aviator Demo. This report separates live Demo observations, unit tests, Winner runtime status, and provider-only evidence.

## Runtime and browser topology

- Demo page: `https://aviator-demo.spribegaming.com/`, title `Aviator`.
- At the 16:08:27Z Collector-to-HealthMonitor check: 1 browser, 1 context, 1 page, and 1 frame. The frame was the page's **main frame**; no iframe element was observed in this Demo integration.
- Demo was reached through the bot's Playwright CDP connection on port 9223. This was not the Winner collector browser on port 9222.
- A 40.239-second Collector observation captured 85 samples. Two changes to the leading payout were observed (7.68x at 15:38:52.037Z and 1.18x at 15:39:02.349Z). They were DOM observations in the Demo session and were not written to Winner PostgreSQL.
- The Winner API was unavailable at final check (`127.0.0.1:8000` refused the connection), so the updated API response and application startup could not be verified. The existing Winner collector had previously reported `AUTH_REQUIRED`/stale and no Aviator page; it did not own this Demo page.

## Requested Demo observations

| # | Requirement | Live evidence and result |
|---:|---|---|
| 1 | Game frame, URL, DOM | Demo top-level URL above; one main frame, canvas 684x421 in the phase sample. No iframe element or provider round-ID attribute was found. Verified selectors: `app-bet-control.bet-control.double-bet` (2 panels), `button.btn.btn-success.bet.ng-star-inserted` (2 green Bet buttons), `.payouts-block .payout` (historical/rolling payout nodes). |
| 2 | FLEW AWAY and multiplier | Canvas screenshots showed `FLEW AWAY! 17.48x` at 15:36:33.712Z and `FLEW AWAY! 1.14x` at 15:36:43.221Z. This is screenshot/canvas evidence, not a DOM selector. |
| 3 | Betting countdown | No explicit numeric countdown or deadline was exposed in the inspected DOM. The transition animation/progress bar is not deadline evidence. `BETTING_OPEN`, `BETTING_CLOSED`, and countdown remain `UNKNOWN`. |
| 4 | Round identity | No provider round ID or stable per-round DOM identity was found. `.payouts-block .payout` exposes payout results, but order alone does not establish the next target's provider identity. |
| 5 | Green Bet | Two buttons matched `button.btn.btn-success.bet.ng-star-inserted`; both were visible/enabled, text `Bet 100.00 USD`, computed background `rgb(40, 169, 9)`. Panel state `BET_AVAILABLE`. The stake was observed as configured in Demo, not changed by the observer. |
| 6 | Red Cancel / queued | No Cancel button was observed because no bet was placed. `BET_QUEUED` was not observed. |
| 7 | Yellow Cash Out / active | No Cash Out button was observed because no bet was placed. `BET_ACTIVE` was not observed. |
| 8 | Auto Cashout 2.00x | Fresh readback at 16:08:27.821Z on both panels: Auto Cash Out ON, input `2.00`, status `VERIFIED`, expiry 16:08:42.821Z. |
| 9 | Auto Bet OFF | Fresh readback at 16:08:27.821Z on both panels: switch class `input-switch off`, status `VERIFIED`. |
| 10 | Bet acknowledgement | No Demo bet was submitted; no provider/UI acceptance acknowledgement was available. |
| 11 | WIN/LOSS, settlement, balance | No bet, result attribution, settlement receipt, or transaction-related balance delta was observed. A multiplier alone is not bet settlement evidence. |
| 12 | SELECT-to-Bet latency | Unavailable because no SELECT reached a bet action. Separate Collector observer-to-Node latency across 85 samples: min 6 ms, median 49 ms, p95 400 ms, max 675 ms. This is not execution or provider latency. |

### Observed canvas phases

- 15:36:33.712Z: `FLEW AWAY! 17.48x`.
- 15:36:34.951Z: UFC/Aviator transition screen.
- 15:36:43.221Z: `FLEW AWAY! 1.14x`.
- Other samples showed an active multiplier/flight. The sample intervals were variable (about 1.1 seconds median, up to about 3.1 seconds), so this is not a complete phase trace.
- The production phase/window detector correctly remained `UNKNOWN` without reliable explicit DOM evidence. No verified transition to `BETTING_OPEN` or `BETTING_CLOSED` was made.

## Implementation completed in this pass

- Added fresh, read-only panel configuration observation and a 15-second evidence expiry in `bot/collector/PanelStateDetector.js` and `bot/collector/Collector.js`.
- Routed the two independent observations through `bot/collector/HealthMonitor.js` and `bot/collector/index.js`; older observations do not replace newer observations.
- Validated freshness, panel identity, currency, stake, Auto Bet, and Auto Cash Out readback in `backend/main.py` and `backend/app/history_collector.py`; Rules Engine readiness exposes both panel records in `backend/app/api/rules_engine.py`.
- The Demo end-to-end in-memory readback confirmed both panels at USD 100, Auto Bet OFF, Auto Cash Out ON at 2.00x, and 15-second freshness. It did not persist Demo rounds to Winner's PostgreSQL or API status.

## Rules Engine and execution flow

- No active Rules Engine selection session was verified for the Demo run. The earlier Winner status showed no active session and zero selections; the most recent backend API could not be queried because the backend was stopped/unreachable.
- No new Demo completed-round event was fed into an active Rules Engine session. Therefore there is no Demo `TRUE`/`FALSE`, `SELECT`/`NO_SELECT`, deduplication result, or simulated execution result to report.
- The existing `backend/app/services/rules_shadow_executor.py` returns `BLOCKED` with `RULES_ENGINE_OBSERVE_ONLY`; it is a retired observe-only adapter and does not connect SELECT to Demo execution.
- No bet was clicked. A green Bet button alone does not verify the betting window or target identity. The required active session, target-bound SELECT, explicit deadline, and round identity were absent, so the Demo sequence failed closed before execution.

## LIVE_REAL blockers and classification

| Original blocker | Status | Remaining evidence/action |
|---|---|---|
| REAL-MONEY EXECUTION: DISABLED | UNRESOLVED | `REAL_MONEY_EXECUTION_ENABLED` remains false; `browser_client.place_bet()` raises a fail-closed error. |
| `real_money_execution_disabled_during_refactoring` | RESOLVED_SHARED_CODE | Obsolete temporary-refactor wording was replaced with pending LIVE qualification. The execution gate was not enabled. |
| Backend live placement permission not enabled | UNRESOLVED | Requires an explicit authorized configuration decision after qualification. |
| Both Winner LIVE panels freshly set to 2.00x, Auto Bet OFF | REQUIRES_WINNER_LIVE_VERIFICATION | Demo panel readback cannot qualify Winner LIVE settings. |
| Shared Winner LIVE phase/window UNKNOWN | REQUIRES_WINNER_LIVE_VERIFICATION | Must establish actual Winner LIVE frame/DOM evidence. |
| No explicit live betting deadline | REQUIRES_WINNER_LIVE_VERIFICATION | Verify a fresh countdown tied to the exact target round. |
| Provider transaction adapter incomplete | UNRESOLVED | `backend/app/betting/browser_adapters.py` still lacks verified Winner LIVE transaction capabilities. |
| `bet_submission` unverified | REQUIRES_PROVIDER_AUTHORIZATION | Needs authorized provider submission and acceptance evidence. |
| `target_identity` unverified | REQUIRES_WINNER_LIVE_VERIFICATION | Need provider-issued identity for the exact next round. |
| `betting_deadline` unverified | REQUIRES_WINNER_LIVE_VERIFICATION | Need explicit, target-bound deadline evidence. |
| `cashout_acknowledgement` unverified | REQUIRES_PROVIDER_AUTHORIZATION | UI state does not prove provider acknowledgement. |
| `acceptance_receipt` unverified | REQUIRES_PROVIDER_AUTHORIZATION | Need authentic Winner LIVE acceptance receipt. |
| `settlement_receipt` unverified | REQUIRES_PROVIDER_AUTHORIZATION | Need independent provider settlement evidence and reconciliation. |
| `currency_validation` unverified | REQUIRES_WINNER_LIVE_VERIFICATION | Verify Winner account, panel, and transaction currency. |
| Next target lacks verified provider round identity | REQUIRES_WINNER_LIVE_VERIFICATION | Bind a selection to the exact provider round before dispatch. |
| `EXPLICIT_LIVE_SESSION_AUTHORIZATION_REQUIRED` | REQUIRES_PROVIDER_AUTHORIZATION | No explicit Winner LIVE authorization was issued; authorization must not survive restart/login expiry. |
| Active Rules Engine selection session required | UNRESOLVED | Start and verify an active session on fresh, committed rounds. |

Demo evidence resolves shared panel-readback code only. It does not resolve Winner LIVE provider requirements or authorization. LIVE_REAL remains disabled and is not ready.

## Test results

- `backend/.venv/bin/python -m pytest backend/tests/test_collector_direct_observations.py backend/tests/test_live_execution_readiness_api.py backend/tests/test_rules_betting_architecture.py -q`: **30 passed**.
- Node regression tests (`health_monitor`, `panel_state_detector`, `history`, `live_shadow_game_phase_client`): **4 files passed, 0 failures**.
- Demo live observation: 85 samples over 40.239 seconds; two leading-payout changes; panel config readback end-to-end in memory passed. No Demo bet or real-money action was performed.
- Application startup/API check: **not running**; `localhost:8000` connection refused at final verification.

## Exact files needing further changes

- `backend/app/services/rules_shadow_executor.py`: replace retired observe-only path with a separately gated Demo-only executor and receipt/state tracking; do not connect it to LIVE_REAL placement.
- `backend/main.py`, `backend/app/rules_engine.py`, and `backend/app/api/rules_engine.py`: add a Demo event source/session lifecycle that receives completed Demo rounds and exposes deduplicated rule decisions to a Demo executor.
- `bot/collector/Collector.js` and `backend/app/betting/platform_state.py`: only classify phases as open/closed when reliable, target-bound deadline evidence is found; current Demo evidence does not support that.
- `backend/app/betting/browser_adapters.py`, `backend/app/betting/browser_client.py`, and `backend/app/live.py`: Winner LIVE capability/receipt verification and authorization remain incomplete; keep low-level placement fail-closed.
- Restart the Winner backend under its existing process supervisor to verify the updated readiness API after the code changes. Do not create another collector/browser writer to do so.

