# SPRIBE Demo - bot Playwright read-only observation

- Browser: isolated Chrome attached through bot Playwright; 1 context, 1 page, 1 frame.
- Page: https://aviator-demo.spribegaming.com/; title Aviator.
- Monitoring: 30 seconds, 41 DOM samples; average interval 739 ms.
- Browser continuity: CDP 9223 refused at 15:25:50Z; I reopened the isolated Demo Chrome and bot Playwright confirmed it at 15:27:18Z with 1 context, 1 page, and 1 frame. The collector browser on 9222 was not redirected.
- DOM observer-to-poll delay: min 33 ms, median 440 ms, max 2947 ms.

## Verified DOM selectors

- app-bet-control.bet-control.double-bet: 2 panel containers.
- button.btn.btn-success.bet.ng-star-inserted: 2 visible, enabled green Bet buttons; both display Bet 1.00 USD and background rgb(40, 169, 9).
- Each panel displayed stake 1.00 USD. Bet tab was selected; Auto tab was unselected. This does not verify Auto Bet configuration.
- No visible Cancel or Cash Out text button was observed. No betting/settings controls were activated.
- One canvas (684 x 477) rendered the game round. No round/game ID attributes were found in the inspected DOM.

## Sampled canvas phase evidence

- 15:23:20.027Z — ROUND_RUNNING, 1.35x with aircraft in flight.
- 15:23:23.803Z — ROUND_ENDED, canvas displayed FLEW AWAY! 1.41x.
- 15:23:28.964Z — INTERMISSION, canvas displayed the UFC/Aviator official partners transition.
- 15:23:33.783Z — ROUND_RUNNING, 1.33x with aircraft in flight.
- 15:23:38.613Z — ROUND_RUNNING, 2.05x with aircraft in flight.

These are sampled screenshots, not a complete phase trace. BETTING_OPEN and BETTING_CLOSED remain UNKNOWN: the DOM exposed no reliable countdown/deadline or explicit open/closed label. The canvas multiplier is not a DOM selector.

## Screenshots

- 2026-10-15T15:23:20.027Z — ../reports/spribe-demo-bot-monitor-20261015T152312Z-canvas-00.png
- 2026-10-15T15:23:23.803Z — ../reports/spribe-demo-bot-monitor-20261015T152312Z-canvas-01.png
- 2026-10-15T15:23:28.964Z — ../reports/spribe-demo-bot-monitor-20261015T152312Z-canvas-02.png
- 2026-10-15T15:23:33.783Z — ../reports/spribe-demo-bot-monitor-20261015T152312Z-canvas-03.png
- 2026-10-15T15:23:38.613Z — ../reports/spribe-demo-bot-monitor-20261015T152312Z-canvas-04.png

## Safety and limits

- No Bet, Cancel, Cash Out, stake, Auto Bet, or Auto Cashout action was performed.
- No transaction was submitted; Demo acceptance and settlement were not tested.
- Demo observations do not verify Winner LIVE provider receipts, settlement, round identity, currency, or authorization.
