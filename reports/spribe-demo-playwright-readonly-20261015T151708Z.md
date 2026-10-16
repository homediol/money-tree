# SPRIBE Demo Playwright Read-only Inspection

- Captured: 2026-10-15T15:17:08.512Z
- Finished: 2026-10-15T15:18:25.373Z
- Browser: bot Playwright attached to isolated Chrome; 1 context, 1 page
- Final page: https://aviator-demo.spribegaming.com/
- Navigation: domcontentloaded
- Read-only: no bet, cancel, cash out, or settings interaction
- Observation: 22 polls over 67.3s
- Mutation-to-poll: 15 samples; min 278.60009765625 ms, median 510 ms, max 1184.2001953125 ms

## Frames and observed selectors

### frame-0-https://aviator-demo.spribegaming.com/
- URL: https://aviator-demo.spribegaming.com/
- Title: Aviator
- Ready state: complete
- Canvas count: 1
- Body excerpt: 30,000.00USD All Bets Previous Top 4,958.81 193/344 Bets Total win USD Player Bet USD X Win USD d***7 100.00 3.11x 311.00 d***7 100.00 d***3 100.00 d***8 100.00 2.53x 253.00 d***8 100.00 d***4 100.00 d***9 100.00 d***9 100.00 3.24x 324.00 d***2 100.00 d***9 100.00 d***2 100.00 d***4 100.00 2.84x 284.00 d***6 100.00 2.29x 229.00 d***6 100.00 d***2 100.00 d***4 100.00 3.19x 319.00 d***1 100.00 d***1 100.00 d***2 100.00 4.38x 438.00 d***3 100.00 d***5 100.00 d***3 100.00 d***4 100.00 1.83x 183.00 d***2 100.00 d***8 100.00 d***5 100.00 2.91x 291.00 d***8 100.00 d***4 100.00 1.83x 183.00 d***3 100.00 d***8 100.00 d***6 100.00 1.32x 132.00 d***1 99.33 Provably Fair Game Powered by 2.74x 1.00x 1.18x 1.06x 1.01x 1.03x 2.63x 1.84x 2.97x 57.17x 14.76x 1.81x 1.65x 2.69x 18.20x 1.00x 2.50x 2.17x 3.50x 1.05x 1.36x 3.20x 1.10x 17.15x 2.00x 10.78x 1.00x FUN MODE 140 Bet Auto 1 2 5 10 Bet 1.00 USD Bet Auto 1 2 5 10 Bet 1.00 USD
- Verified selectors (derived from live DOM and locator-count checked):
  - body.scroll-desktop.scroll-hide.ng-tns-0-2 -> 1 match(es), text=30,000.00USD All Bets Previous Top 4,958.81 193/344 Bets Total win USD Player Be, visible=true, disabled=false, background=rgb(0, 0, 0)
  - app-game.ng-star-inserted -> 1 match(es), text=30,000.00USD All Bets Previous Top 4,958.81 193/344 Bets Total win USD Player Be, visible=true, disabled=false, background=rgba(0, 0, 0, 0)
  - div.main-wrapper.hide-chat-bar -> 1 match(es), text=30,000.00USD All Bets Previous Top 4,958.81 193/344 Bets Total win USD Player Be, visible=true, disabled=false, background=rgba(0, 0, 0, 0)
  - div.main-container -> 1 match(es), text=30,000.00USD All Bets Previous Top 4,958.81 193/344 Bets Total win USD Player Be, visible=true, disabled=false, background=rgb(14, 14, 14)
  - div.user-wrapper.ng-star-inserted.dropdown -> 1 match(es), text=demo_23310ChangeAvatarSoundMusicAnimation Free bets My bet history Game limits H, visible=true, disabled=false, background=rgba(0, 0, 0, 0)
  - div.dropdown-toggle.user.w-100.d-flex.align-items-center.position-relative.disabled-on-game-focused -> 1 match(es), text=demo_23310ChangeAvatarSoundMusicAnimation Free bets My bet history Game limits H, visible=true, disabled=false, background=rgba(0, 0, 0, 0)
  - div.w-100.h-100 -> 1 match(es), text=All Bets Previous Top 4,958.81 193/344 Bets Total win USD Player Bet USD X Win U, visible=true, disabled=false, background=rgba(0, 0, 0, 0)
  - div.game-container -> 1 match(es), text=All Bets Previous Top 4,958.81 193/344 Bets Total win USD Player Bet USD X Win U, visible=true, disabled=false, background=rgba(0, 0, 0, 0)
  - div.bets-widget-container -> 1 match(es), text=All Bets Previous Top 4,958.81 193/344 Bets Total win USD Player Bet USD X Win U, visible=true, disabled=false, background=rgba(0, 0, 0, 0)
  - div.bets-block -> 1 match(es), text=All Bets Previous Top 4,958.81 193/344 Bets Total win USD Player Bet USD X Win U, visible=true, disabled=false, background=rgb(27, 28, 29)
  - app-all-bets-tab.data-list.ng-star-inserted -> 1 match(es), text=4,958.81 193/344 Bets Total win USD Player Bet USD X Win USD d***7 100.00 3.11x , visible=true, disabled=false, background=rgba(0, 0, 0, 0)
  - div.d-flex.h-100.flex-column.ng-star-inserted -> 1 match(es), text=4,958.81 193/344 Bets Total win USD Player Bet USD X Win USD d***7 100.00 3.11x , visible=true, disabled=false, background=rgba(0, 0, 0, 0)
  - span.cashout-value -> 1 match(es), text=4,958.81, visible=true, disabled=false, background=rgba(0, 0, 0, 0)
  - app-bets-list.bets-list -> 1 match(es), text=Player Bet USD X Win USD d***7 100.00 3.11x 311.00 d***7 100.00 d***3 100.00 d**, visible=true, disabled=false, background=rgba(0, 0, 0, 0)
  - div.bets-list-header -> 1 match(es), text=Player Bet USD X Win USD, visible=true, disabled=false, background=rgba(0, 0, 0, 0)
  - span.bets-list-header-item.bet -> 1 match(es), text=Bet USD, visible=true, disabled=false, background=rgba(0, 0, 0, 0)
  - div.bet-list-item.cashout -> 14 match(es), text=d***7 100.00 3.11x 311.00, visible=true, disabled=false, background=rgb(16, 17, 18)
  - div.bet-list-item-column.player -> 32 match(es), text=d***7, visible=true, disabled=false, background=rgba(0, 0, 0, 0)
  - div.bet-list-item-column.bet -> 32 match(es), text=100.00, visible=true, disabled=false, background=rgba(0, 0, 0, 0)
  - div.bet-list-item-column.x -> 32 match(es), text=3.11x, visible=true, disabled=false, background=rgba(0, 0, 0, 0)
  - div.bet-list-item-column.win -> 32 match(es), text=311.00, visible=true, disabled=false, background=rgba(0, 0, 0, 0)
  - div.bet-list-item -> 32 match(es), text=d***7 100.00, visible=true, disabled=false, background=rgb(16, 17, 18)
  - div.bet-list-item-column.player -> 32 match(es), text=d***7, visible=true, disabled=false, background=rgba(0, 0, 0, 0)
  - div.bet-list-item-column.bet -> 32 match(es), text=100.00, visible=true, disabled=false, background=rgba(0, 0, 0, 0)
  - div.bet-list-item -> 32 match(es), text=d***3 100.00, visible=true, disabled=false, background=rgb(16, 17, 18)
  - div.bet-list-item-column.player -> 32 match(es), text=d***3, visible=true, disabled=false, background=rgba(0, 0, 0, 0)
  - div.bet-list-item-column.bet -> 32 match(es), text=100.00, visible=true, disabled=false, background=rgba(0, 0, 0, 0)
  - div.bet-list-item.cashout -> 14 match(es), text=d***8 100.00 2.53x 253.00, visible=true, disabled=false, background=rgb(16, 17, 18)
  - div.bet-list-item-column.player -> 32 match(es), text=d***8, visible=true, disabled=false, background=rgba(0, 0, 0, 0)
  - div.bet-list-item-column.bet -> 32 match(es), text=100.00, visible=true, disabled=false, background=rgba(0, 0, 0, 0)
  - div.bet-list-item-column.x -> 32 match(es), text=2.53x, visible=true, disabled=false, background=rgba(0, 0, 0, 0)
  - div.bet-list-item-column.win -> 32 match(es), text=253.00, visible=true, disabled=false, background=rgba(0, 0, 0, 0)
  - div.bet-list-item -> 32 match(es), text=d***8 100.00, visible=true, disabled=false, background=rgb(16, 17, 18)
  - div.bet-list-item-column.player -> 32 match(es), text=d***8, visible=true, disabled=false, background=rgba(0, 0, 0, 0)
  - div.bet-list-item-column.bet -> 32 match(es), text=100.00, visible=true, disabled=false, background=rgba(0, 0, 0, 0)
  - div.bet-list-item -> 32 match(es), text=d***4 100.00, visible=true, disabled=false, background=rgb(16, 17, 18)
  - div.bet-list-item-column.player -> 32 match(es), text=d***4, visible=true, disabled=false, background=rgba(0, 0, 0, 0)
  - div.bet-list-item-column.bet -> 32 match(es), text=100.00, visible=true, disabled=false, background=rgba(0, 0, 0, 0)
  - div.bet-list-item -> 32 match(es), text=d***9 100.00, visible=true, disabled=false, background=rgb(16, 17, 18)
  - div.bet-list-item-column.player -> 32 match(es), text=d***9, visible=true, disabled=false, background=rgba(0, 0, 0, 0)
  - div.bet-list-item-column.bet -> 32 match(es), text=100.00, visible=true, disabled=false, background=rgba(0, 0, 0, 0)
  - div.bet-list-item.cashout -> 14 match(es), text=d***9 100.00 3.24x 324.00, visible=true, disabled=false, background=rgb(16, 17, 18)
  - div.bet-list-item-column.player -> 32 match(es), text=d***9, visible=true, disabled=false, background=rgba(0, 0, 0, 0)
  - div.bet-list-item-column.bet -> 32 match(es), text=100.00, visible=true, disabled=false, background=rgba(0, 0, 0, 0)
  - div.bet-list-item-column.x -> 32 match(es), text=3.24x, visible=true, disabled=false, background=rgba(0, 0, 0, 0)
  - div.bet-list-item-column.win -> 32 match(es), text=324.00, visible=true, disabled=false, background=rgba(0, 0, 0, 0)
  - div.bet-list-item -> 32 match(es), text=d***2 100.00, visible=true, disabled=false, background=rgb(16, 17, 18)
  - div.bet-list-item-column.player -> 32 match(es), text=d***2, visible=true, disabled=false, background=rgba(0, 0, 0, 0)
  - div.bet-list-item-column.bet -> 32 match(es), text=100.00, visible=true, disabled=false, background=rgba(0, 0, 0, 0)
  - div.bet-list-item -> 32 match(es), text=d***9 100.00, visible=true, disabled=false, background=rgb(16, 17, 18)

## Phase classification

DOM text did not provide a reliable phase signal. The earlier text-only heuristic matched historical bet-list multipliers, so this capture is classified UNKNOWN. See the subsequent canvas observations for visually verified ROUND_RUNNING, ROUND_ENDED, and INTERMISSION samples.

## Screenshots
- ../reports/spribe-demo-playwright-readonly-20261015T151708Z-page.png
- ../reports/spribe-demo-playwright-readonly-20261015T151708Z-frame-0-canvas-0.png

## Evidence limits
- Read-only inspection; no betting controls or settings activated.
- Phases use observed DOM text and visible controls; absent reliable evidence remains UNKNOWN.
- Mutation latency is local observer-to-poll delay, not provider/network latency.
- Demo evidence does not verify Winner LIVE acceptance, settlement, currency, or authorization.
