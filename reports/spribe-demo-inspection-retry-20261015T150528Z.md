# SPRIBE Demo inspection retry

- Captured: 2026-10-15 15:05:28 UTC
- Mode: **READ ONLY**
- Result: **Partial** — Chrome was launched at the supplied Demo URL, but CUA cannot inspect or attach to that window.

## Browser attempt

CUA initially showed no browsers. Its IAB and Chrome browser providers both reported unavailable. Following the user's explicit instruction, Chrome was started with an isolated temporary profile and the supplied Demo URL. The process remains running, but CUA still lists no apps or browsers and cannot attach. Therefore, successful page load is **not verified**, and no DOM, iframe, or screenshot evidence was collected.

The existing browser sidecar reports the collector-associated betting page as Winner Live at `https://winner.rw/en/virtual/crash-games/aviator`. That page was left untouched. The new Chrome window is separate from the collector browser and cannot supply shared-session evidence.

## Inspection and safety

Frame URLs, selectors, text, attributes, computed styles, canvas screenshots, phase/countdown changes, round IDs, DOM latency, and panel transitions remain unverified. Bet, Cancel, Cash Out, stake/Auto Bet changes, and real-money transactions were not attempted. The Demo token is omitted from this report.
