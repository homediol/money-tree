# Advanced Crash Game Behaviour Detection System

## Architecture decision (2026-11-13)

This system is a separately installed and operated FastAPI application. The
existing React/Vite frontend is its only shared runtime component. Behaviour
pages use a dedicated client and `/api/v1/behaviour` namespace, and are routed
outside the Winner Predict API access gate and health polling. The new backend
does not import Winner Predict Python packages, open its database, call its
round APIs, or load its credentials.

```text
Winner Predict React shell
  ├── existing pages -> existing Winner Predict API
  └── behaviour pages -> /api/v1/behaviour -> independent Behaviour API
                                               ├── Behaviour PostgreSQL
                                               ├── analysis worker
                                               └── Orbit collector -> Orbit Data Watch
```

The Node.js service supervisor starts the Behaviour API, worker, and configured
collector as local processes. Behaviour database and Orbit settings use
`BEHAVIOUR_*` names, separate from Winner Predict's environment. Container
files remain optional deployment assets and are not used by `npm start`.

## Phase 1 evidence used

The frozen cohort is dated `2026-11-13T08:30:13.384380Z`: 171,378 source rows
reconcile to 50,123 canonical rounds. The canonical counts are H=24,339,
L=25,651, E=133. The Phase 1 CSVs, source manifests, hashes, and row-level
reconciliation are retained as import inputs and imported provenance. The
Phase 1 ordering is recorded event timestamp, then source-local index, then
research key; 8,722 rounds share timestamps and 60 adjacent local-index pairs
have timestamp inversions. Ties and inversions therefore carry explicit
ordering-quality flags.

The live next-100 reference reproduces `run_research.py::forecast_experiment`:
from position 199, form non-overlapping 100-round future blocks; for each block
calculate H/(H+L), separately retain E/100, then take the expanding arithmetic
mean of completed block outcomes. The research evaluation used the first half
of available blocks as warm-up and tested the rest chronologically. It found
MSE 0.002923 for this baseline versus 0.003144 for the contextual logistic
model. The production assessment therefore reports only this expanding
reference. Historical empirical count quantiles are displayed as descriptive
uncertainty, with their exchangeability and temporal-dependence caveats.

The 30/124 corrected motifs, alternation result, rolling windows, run lengths,
and 427 p99-gap groups seed descriptive monitoring. They are not predictive
rules. No precursor test survived BH correction; order-2 Markov sensitivity
failed on distinct-timestamp rounds; the 100-context/20-neighbour result was
selected on its test set and remains exploratory. No trained or ML model is
included.

## Data ownership and order

The independent database has its own DSN and PostgreSQL service/volume. Raw
source rows, source reconciliation decisions, canonical historical rounds,
and source-to-canonical mappings are separate immutable records. Migrations
are transactional. Reimports are idempotent by snapshot/source identity and
reject identity conflicts rather than rewriting old values.

Multiplier strings are parsed as `Decimal`; comparison to Decimal `2.00`
creates H/L/E without rounding. Event time, ingestion time, source order, and
analytical order are distinct fields. Imported historical ordering follows
the Phase 1 reproducible ordering and flags ties/inversions. Orbit's current
Data Watch response supplies a page-level `checkedAt`, a reading cursor, and
selector items; it does not supply per-round event timestamps or game round
IDs. The collector records `checkedAt` as observation/ingestion time, leaves
per-round event time unknown, and only appends snapshot deltas with a unique
verified overlap. Ambiguous/truncated snapshots remain archived and
quarantined. A generated analytical ordinal is never represented as a source
round ID or authoritative game index.

Historical time groups use the Phase 1 empirical p99 positive gap (180.0452s)
as a descriptive default, with thresholds and source/timestamp quality shown.
Live Orbit data will not be assigned event-time groups unless the provider
later supplies trustworthy per-round event times.

## Processes and API

* `api`: read-only versioned JSON endpoints, separate health checks.
* `collector`: independent Orbit client, watch discovery, cursor recovery,
  immutable reading archive, idempotent delta ingestion, retry/backoff.
* `worker`: separately polls durable unanalysed rounds; updates rolling
  features, sequence counts, state/rule evaluations and next-100 assessment.
* `import-history`: imports the frozen research records once and verifies all
  source and canonical counts, class counts, and hashes before commit.

The API is read-only. Mutations and collection controls are CLI/process
operations. Local development publishes the API only on loopback and uses a
read-only Vite proxy bound to loopback. Deployments must keep the API on a
private network and require the trusted reverse proxy to enforce the
application's access policy before forwarding `/api/v1/behaviour`. The
frontend never receives Orbit or database credentials.

## State and rules

States are deterministic descriptions computed from evidence-backed rolling
features. Minimum evidence, dominance, balance and alternation boundaries are
configurable and mirrored from the Phase 1 group/precursor definitions. A
consecutive-confirmation hysteresis prevents one-round state chatter. State
history distinguishes observed state from forecast assessment and includes
support, source quality, calculation, rule version and evidence class.

All active rules are descriptive/data-quality rules. The rules registry stores
version, formula, inputs, thresholds, minimum support, validation status and
explanation. No motif, state, context, or analogue is labelled as predictive
without new chronological validation. The baseline forecast explicitly says
that exact future multipliers are unknown.

## Frontend and isolation checks

The common shell/navigation and visual tokens are shared. Behaviour routes
must remain usable if the Winner Predict API is down; they do not mount
`SystemHealth`, `ApiAccessGate`, or the Winner Predict API client. Vite routes
the behaviour namespace to port 8100 before its generic Winner Predict `/api`
proxy. A dedicated local PostgreSQL database or SQLite development database
keeps Behaviour storage separate from Winner Predict.

Automated checks will cover pure analysis calculations, source reconciliation,
Orbit contract parsing/overlap quarantine, API independence, process/config
isolation, idempotent imports and frontend build/routes. No live Orbit request
is attempted unless independent behaviour credentials are configured.

## Known external limits

Orbit credentials are installation-specific and remain in the ignored private
`.env`; no key is committed. In this workspace the supplied credentials reach
Orbit and a configured running Aviator payout watch is selected. Some source
readings contain two exact copies of the ordered payout list. The collector
coalesces only those exact halves for round analysis, retains the complete raw
payload, and maps both DOM item indices to one canonical outcome. Initial
readings without a verified Phase 1 overlap remain quarantined, and subsequent
live rounds retain the explicit unknown cross-source boundary. The Data Watch
API exposes page-level observation times but no per-round event timestamp or
game round ID, so authoritative live event-time grouping and round identity
remain unavailable through this contract.
