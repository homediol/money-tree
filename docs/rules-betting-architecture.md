# Rules Betting Architecture

## Rule evaluation and shared selection

`RulesEngine` owns the preserved source rule definitions from Run Controls —
Start a Check, Legacy — Separate Tracker, and Test — All Rules Together. A
session snapshots its enabled rule IDs and versions. Each selected rule is
evaluated independently against the same verified source round and the same
immediate target (`N + 1`). Disabled rules do not participate.

`ALL_RULES_TOGETHER` combines all TRUE proposals for that target into one
selection. The selection contains every contributing rule ID, while the
durable execution-intent uniqueness key is `(target stream, target round
index, panel)`. Several TRUE rules therefore cannot create several orders.
Zero TRUE proposals create no selection or intent. `FIVE_RULE_TRACKER` keeps
independent observational ledgers and does not turn its separate tracker rows
into separate paid orders.

Start Check and Legacy keep their own policy state, limits, and shadow ledger.
Their configured profit/loss thresholds are source-rule session controls.
All Rules Together owns its shared WIN count, LOSS count, and maximum selected
round count; one shared bet increments those counters once regardless of how
many rules contributed. The five independent trackers keep per-rule outcome
counts. Their observations are not counted as paid bets.

## Fixed cashout and platform observations

The session cashout target is fixed at `2.00x`. The read-only browser snapshot
records the Auto tab, displayed value, and switch state. `configuration_valid`
means those observed controls match the requested setting. `verified` also
requires separate platform acknowledgement. A visible `2.00` input alone is
not an acknowledgement. No code changes the Auto tab, stake, switch, or bet
button during readiness checks.

Balance amount and currency are parsed from the account display. The provider
identity is the observed site host. The provider session identity stays null
unless the platform exposes a verified identity; the app's rules/live session
ID is stored separately as `application_session_id`. A missing currency keeps
balance change unavailable. A currency mismatch also keeps it unavailable.

`Balance Change` is current verified balance minus the first verified starting
balance for the app session, only when both use the same known currency.
Realized betting P/L sums only nonsimulated, reconciled bet executions.
The difference between balance change and reconciled betting P/L is recorded
as an unclassified non-betting movement; this does not claim a deposit or
withdrawal unless a platform transaction record identifies it.

## Durable intent and execution boundary

The repository persists session snapshots and normalized evaluations,
selections, target observations, execution intents, platform balance
observations, session financials, and stop events. Unique target/panel keys
make duplicate intent insertion idempotent. Current rule selections produce
`BLOCKED_READ_ONLY` intent records for audit and UI display; they do not enter a
financial execution queue.

The collector remains the owner of one browser and one context, with distinct
History and Betting pages. Its page-role target IDs are published to the
backend. The betting reader refuses to fall back to the History page when the
Betting role is absent. Readiness requires exactly one browser, one context,
and two pages. Recovery does not authorize or replay a saved intent.

The platform transaction classifier requires explicit acceptance/rejection
evidence. A button click, enabled button, round multiplier, or visible cashout
field cannot settle a bet. Unverified placement and result evidence remains
`UNKNOWN`.

Real-money action is disabled in both the high-level backend and the
low-level browser client until separate authorization and platform verification
are delivered. Automated tests use mocks; none submits a platform bet.
