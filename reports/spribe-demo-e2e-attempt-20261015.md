# SPRIBE Demo end-to-end verification attempt

Date: 2026-10-15. This report records the current backend and read-only browser evidence. The requested Demo transaction pipeline could not pass its required target, deadline, and Demo-session gates, so no bet was attempted.

## 1. Backend and PostgreSQL

- The host API was running as an older process (PID 812900) and had PostgreSQL available. The initial `localhost:8000` refusal came from the isolated task shell's network namespace; host-level read-only probes reached the API.
- That old backend did not have the latest source changes loaded. The supervised compatibility check found the missing `collector_panel_configuration_readback_v1` capability. Once the prior process stopped, `npm run backend` started the current application under its existing supervisor. It reused Collector PID 812909 and did not start a second Collector.
- Current backend: PID 969540, instance `6dc216dd6db74a2697452f73f9f069c3`, healthy at 16:26:32Z. `/health` reports PostgreSQL 17 as `ok`.
- The new backend exposed `collector_panel_configuration_readback_v1: true`. Collector-to-backend requests were accepted, including `POST /api/collector/game-phase` (HTTP 200).
- Current Winner history showed 31,010 persisted rows. Latest saved Winner round: index 31,471, multiplier 2.64x at 16:24:27.675Z; collector heartbeat was fresh and last observed at 16:26:23.005Z. These are Winner collector records, not SPRIBE Demo records.

## 2. Rules Engine

- Rules Engine API responds `READY`; it reports 2 configured and enabled rules: `USER_PLAYING_POLICY` and `AFTER_HH_CONTINUE_MAX_3`.
- There is no active selection session. The only listed session is the earlier completed session `ddd7fa21-73c6-4b03-b202-b0d6aaacf761` (`ALL_RULES_TOGETHER`). Automatic execution is `OFF`.
- Current SHADOW readiness is `BLOCKED` by `SHADOW_ROUND_FEED_STALE`; its boundary is Winner's committed-round feed. The current endpoint does not create a Demo-specific session or accept Demo rounds.
- OpenAPI contains no Demo-specific paths. `POST /api/rules-engine/sessions` binds to the canonical Winner round feed, so I did not use it for a Demo session or mislabel Winner rounds as Demo rounds.
- No new live Demo round reached a Demo Rules Engine session. There are no per-rule results, `SELECT`, `NO_SELECT`, or duplicate-selection results to report.
- Existing `backend/app/services/rules_shadow_executor.py` remains explicitly observe-only and returns `BLOCKED / RULES_ENGINE_OBSERVE_ONLY`. It is not a Demo Executor.

## 3. SPRIBE Demo page, phases, and identity

- Playwright attached to the existing CDP 9223 browser. It had 1 context, 1 page, and 1 main frame; no child iframe was present. No page or context was created.
- Selectors observed: `app-bet-control.bet-control.double-bet` (2 panels), `button.btn.btn-success.bet.ng-star-inserted` (2 Bet buttons), and `.payouts-block .payout` (120 payout nodes).
- A 35.195-second passive sample collected 121 DOM snapshots at 257–418 ms intervals. The leading payout changed from 1.72x to 1.04x at 16:20:00.127Z and from 1.04x to 1.56x at 16:20:13.113Z. These payout-list mutations do not identify a provider target round.
- A screenshot at 16:22:05.271Z visibly showed the game canvas at 5.08x with the aircraft in flight (`ROUND_RUNNING` by visual canvas evidence). Both green Bet buttons were enabled during that flight. This demonstrates that the green button alone is not proof of the betting deadline or target. Screenshot: [spribe-demo-e2e-20261015-162205.png](spribe-demo-e2e-20261015-162205.png).
- The DOM scan found no visible `[aria-live]`, `[role=timer]`, `[role=status]`, `[data-phase]`, `[data-state]`, or countdown/timer/round-class nodes, and no numeric countdown or explicit open/close evidence. The phase detector therefore remains `UNKNOWN` for betting-window decisions. Prior FLEW AWAY and transition observations are canvas screenshots, not DOM phase selectors.
- Payout elements exposed displayed multiplier text, Angular class, and color style, but no provider round ID or round-bound data attribute. No local counter was used as a substitute identity.
- 56 incoming WebSocket frames were observed during a passive 12-second check; all were binary and none yielded verified round, phase, countdown, or deadline fields. The binary protocol was not interpreted as transaction or round evidence.

## 4. Current Demo panels and transaction results

- Fresh readback at 16:21:44.543Z: both panels visible; Auto tab selected; stake 100.00 USD; Auto Bet `OFF` (`input-switch off`); Auto Cash Out `ON` (`input-switch`) at 2.00; green Bet action enabled.
- The screenshot showed Demo balance 30,000.00 USD. No bet occurred, so no balance delta or transaction result exists.
- Demo bets attempted: **0**. Demo bets accepted: **0**. `BET_QUEUED`, `BET_ACTIVE`, Cash Out, reset, WIN/LOSS, settlement, Demo P/L, and SELECT-to-Bet latency were not observed.
- No Demo session, SELECT, or transaction was written to PostgreSQL. Winner's existing committed rounds remain intact; none was relabeled as Demo evidence.

## 5. LIVE_REAL isolation

- `/api/live/readiness` remains `ready: false`, with `real_money_execution_disabled_pending_live_qualification` and missing target identity, betting deadline, provider capabilities, authorization, and active selection session among its blockers.
- The backend capability says `real_money_execution_disabled: true`; live authorization is false and automatic execution remains OFF. No LIVE_REAL value or authorization was changed.

## 6. Code changes and tests

Changes in this pass:

- `backend/app/api/stability.py`: expose the implemented panel-readback version as a backend capability.
- `scripts/start-all.js`: require that capability before reusing an existing API process, so stale code is detected by the existing supervisor.
- `backend/tests/test_stability_capabilities.py`: cover the capability.

Verification:

- Backend focused tests (stability capability, collector direct observations, LIVE readiness, Rules Engine architecture, and observe-only executor): **33 passed**.
- Node regression tests (`health_monitor`, `panel_state_detector`, `history`, and `live_shadow_game_phase_client`): **4 files passed, 0 failures**.
- `node --check scripts/start-all.js` and `git diff --check`: passed.
- Runtime: supervised backend startup completed; `/health`, PostgreSQL, Rules Engine API, history API, and backend capabilities were verified.
- Browser verification was passive against the existing Demo page. No bet, setting change, or control click was performed. The standalone browser integration test was not run because it uses an isolated profile rather than the already-open CDP 9223 page.

## 7. Remaining work before a Demo end-to-end bet

The current implementation has no Demo event source/session or Demo Executor route. Completing that integration requires a separate Demo-scoped event/persistence path through `backend/app/api/rules_engine.py`, `backend/app/rules_engine.py`, and `backend/app/database/repository.py` (with its migration), and a Demo-only adapter in `backend/app/betting/browser_adapters.py` / `bot/collector/BrowserManager.js` that attaches only to the existing allowlisted CDP 9223 page. It must remain isolated from `browser_client.py` LIVE placement.

Execution must remain stopped until SPRIBE exposes verified, target-bound betting-window/deadline evidence and a provider round identity, and a Demo-specific active Rules Engine session produces one deduplicated SELECT. Current DOM and binary WebSocket observations did not supply those prerequisites. No successful Demo execution is claimed.
