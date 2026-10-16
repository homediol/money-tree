# SPRIBE Demo inspection retry

- **Generated:** 2026-10-15 14:51:15 UTC
- **Mode:** READ ONLY
- **Status:** `BLOCKED_NO_SHARED_BROWSER`
- **Demo origin:** `https://aviator-demo.spribegaming.com/`
- **Supplied URL token:** excluded

## Retry result

CUA returned no browser, and lookup by the Demo origin found no existing browser. The process check found no collector or Chromium owner. DevTools port 9222, backend port 8000, and PostgreSQL port 5432 were closed. I did not create a browser or change the stale BrowserManager lock.

The collector sidecar reports `COLLECTING`, round `aviator-da441fd7a998595f5dee785f`, and a recent acknowledgement, but its PID is absent; the collector supervisor reports stopped. This is unverified sidecar evidence, not proof of a live collector or independent database persistence.

## Inspection data

No frame URLs, DOM selectors/text, computed styles, canvas screenshots, phase/countdown transitions, DOM latency, or live panel transitions could be observed. The sidecar round ID is recorded in the JSON export with its source and verification limit.

Bet, Cancel, Cash Out, stake changes, Auto Bet changes, and real-money transactions were not attempted.

[Machine-readable JSON](./spribe-demo-inspection-retry-20261015T145115Z.json)
