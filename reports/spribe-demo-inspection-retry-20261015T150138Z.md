# SPRIBE Demo inspection retry

- Captured: 2026-10-15 15:01:38 UTC
- Mode: **READ ONLY**
- Result: **Blocked** — no shared browser is attachable from this session.

## Evidence

CUA reports no apps or browsers; `cua.listBrowsers()` is empty, and looking up the existing browser for the Demo URL returns “No browser is available.” The local CDP endpoint at `127.0.0.1:9222/json/list` refuses the connection. No browser or tab was created.

The fresh `data/bot/status.json` sidecar reports `collectorRunning=true`, `browserConnected=true`, round index **31343**, last round `aviator-0fc85d4545c6a7bf1c600687`, and phase **UNKNOWN** with no countdown or deadline. Those are sidecar values, not independently verified DOM observations.

`data/bot/browser-reliability.json` was updated at 15:01:38 UTC and reports `HEALTHY`, but identifies the shared betting page as `https://winner.rw/en/virtual/crash-games/aviator`. The session-state file lists the Demo origin among stored origins, but has no last-known URLs. This does not verify that a Demo page is open.

## Inspection result

No frame URLs, live DOM selectors/styles, canvas screenshots, phase transitions, countdown measurements, panel transitions, or DOM round IDs could be collected. The sidecar round ID and phase are recorded above only as unverified runtime metadata.

The existing status says the shared betting page is Winner Live. Navigating that page to Demo or opening another game page would change the shared one-page session, so neither was attempted. Bet, Cancel, Cash Out, stake/Auto Bet changes, and real-money transactions were not attempted.
