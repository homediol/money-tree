# SPRIBE Demo inspection

- **Generated:** 2026-10-15 14:49:19 UTC
- **Mode:** READ ONLY
- **Status:** `BLOCKED_NO_ATTACHED_BROWSER`
- **Demo origin:** `https://aviator-demo.spribegaming.com/`
- **Supplied URL token:** excluded

## Runtime evidence

CUA exposed no browser. No Chromium or collector process was found, and DevTools port 9222 was closed. The BrowserManager lock names PID 812909, but that process is absent. The collector supervisor reports `running: false`.

The status sidecar conflicts with those owner checks: it reports `AUTH_REQUIRED`, `collectorRunning: true`, a recent heartbeat, and round `aviator-17d9931dd5f80788f375d88d`. Because its PID is absent, this is sidecar evidence only; current collection and persistence are not independently verified. Backend port 8000 and PostgreSQL port 5432 were also closed.

## Requested inspection results

| Observation | Result |
|---|---|
| Frame URLs | Not captured; no attached browser |
| DOM selectors and text | Not captured; no live DOM inspected |
| Computed styles | Not captured |
| Canvas screenshots | None |
| Phase/countdown transitions | None observed |
| DOM update latency | Not measurable |
| Round identifiers | One sidecar-reported ID; not verified from live DOM |
| Panel transitions | None observed |

## Blocked actions

Bet, Cancel, Cash Out, stake changes, Auto Bet changes, and real-money transactions were not attempted. No platform controls were used.

The stale BrowserManager lock was left untouched to honor READ ONLY mode. Full machine-readable evidence is in [the JSON export](./spribe-demo-inspection-20261015T144919Z.json).
