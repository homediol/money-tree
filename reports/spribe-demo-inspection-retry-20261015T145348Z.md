# SPRIBE Demo inspection retry

- Captured: 2026-10-15 14:53:48 UTC
- Mode: **READ ONLY**
- Result: **Partial** — collector sidecar is fresh, but the shared browser cannot be attached from this session.

## Browser and collector

CUA reports no apps or browsers, and `cua.listBrowsers()` is empty. `127.0.0.1:9222/json/list` refuses the connection. No visible Chrome/Chromium or collector owner appeared in this execution namespace. No browser or tab was started.

At the same time, `data/bot/status.json` was only 0.09 seconds old and reported `collectorRunning=true`, `browserConnected=true`, round index **31328**, last round `aviator-2e4679ecff54492f93ebfeb7`, and a latest commit sample at 14:53:42 UTC with reported latency 3599.77 ms. This confirms fresh sidecar telemetry, not an independently verified process owner or database commit.

The supervisor file says collector PID 444 is stopped (`code=75`) from 13:14:39 UTC. The browser lock names PID 812909 from 13:01:15 UTC, but that PID is absent in this namespace. The stale supervision metadata conflicts with the live-updating sidecar; neither file was modified. `ss` could not inspect sockets because netlink access is denied.

## DOM and phase evidence

No page or iframe was available to inspect. Consequently, this retry verified **no live selectors**, frame URLs, DOM text/attributes/styles, canvas screenshots, phase transitions, panel transitions, or live-DOM round IDs.

The fresh status sidecar reports phase `UNKNOWN`, no visible phase indicator, empty text, no countdown, and no deadline. It describes its source as `AVIATOR_EXPLICIT_BETTING_PHASE_DOM` with selector description “visible game phase text”; that is sidecar metadata, not an independently verified selector. The 3599.77 ms figure is collector-reported commit latency and is not DOM update latency.

## Rules and safety

Rules Engine and SHADOW_REALISTIC execution results were not observed in this retry. No Bet, Cancel, Cash Out, stake, Auto Bet, or real-money action was attempted. LIVE_REAL remains disabled and unverified.

**Current blocker:** the collector status indicates fresh activity, but this session has no attachable shared browser/CDP endpoint. Live DOM and phase evidence cannot be collected until the existing browser is exposed to the inspection session.
