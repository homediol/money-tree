# Advanced Crash Game Behaviour Detection System

This is an independently operated, deterministic research and monitoring service. It shares the Winner Predict React shell but owns its Behaviour database, FastAPI process, analysis worker, migrations, logs and API. Its live-round sync reads Winner Predict's canonical PostgreSQL table in read-only mode. It contains no ML, training or inference dependencies. Its default next-100 assessment is the Phase 1 expanding baseline; all motifs, states and sequences are descriptive unless independently validated.

## Live-first overview and rolling context

The Behaviour overview reads Winner Predict's `aviator_rounds` table. The read-only sync anchors at 100 consecutive rounds shared with the Phase 1 import, matching stable round IDs and multipliers before it advances. It copies newly stored rounds into Behaviour's own database and processes them with the analysis worker. A source gap or changed identity holds the cursor and reports degraded health.

The overview shows rolling 50-round HIGH/LOW/EXACT counts, current state, recent H/L/E sequence and a dynamic outlook. Expected H/L/E counts use the Phase 1 expanding reference calculation. Current HIGH deviation from the frozen Phase 1 rate is shown separately. A historical analogue check summarizes the next 50/100 rounds after overlapping Phase 1 windows within five percentage points of the current HIGH share. It is descriptive and produces no recovery probability. Phase 1 found no precursor feature surviving correction, and the contextual forecast was worse than the expanding baseline, so analogues do not adjust the outlook.

Winner Predict's canonical rows retain platform event time when present, per-round `observed_at`, and database `stored_at` separately. The Behaviour API does not label an observation time as a platform event time. Orbit readings from earlier collection attempts remain archived for audit but are not the active live source.

## First start

Requirements: Python 3.11+, Node.js, and a local PostgreSQL database (or a local SQLite development URL).

```sh
npm run behaviour:setup
npm run behaviour:import-history
npm run behaviour:reconcile
npm start
```

`behaviour:setup` creates `behaviour_detection/.env` with private permissions and applies the schema to the configured Behaviour database. Configure a dedicated local PostgreSQL database and role, or set `BEHAVIOUR_DATABASE_URL=sqlite:///./data/behaviour/behaviour_detection.sqlite3` for a no-container local database; an explicit SQLite URL takes precedence over retained PostgreSQL settings. Behaviour's target database remains separate. The source sync reads `DATABASE_URL` from `backend/.env` in read-only mode; set `BEHAVIOUR_SOURCE_DATABASE_URL` to override it. `npm start` manages the Behaviour API, analysis worker, Winner Predict database sync, and shared frontend alongside the existing services. Use `npm run behaviour:start` to run Behaviour Detection by itself.

Start one database sync and then the local services with:

```sh
npm run behaviour:winner-sync-once
ORBIT_API_BASE_URL='http://localhost:4000' npm start
```

The sync finds a unique ordered Phase 1 overlap before copying new rows. It reads only `aviator_rounds`; it never writes to Winner Predict's database. Continuous sync runs under the Node.js supervisor and can be checked with `npm run behaviour:status`, `npm run behaviour:health`, or `npm run behaviour:logs`.

The frontend uses the shared navigation at `/behaviour`. The local Vite server and Behaviour API bind to loopback, and `/api/v1/behaviour` proxies to `127.0.0.1:8100`; this keeps the unauthenticated read-only development API local. No Orbit or database secret is sent to the browser. For a remote deployment, build the frontend with an empty `VITE_BEHAVIOUR_API_BASE_URL` and configure the trusted same-origin reverse proxy to forward `/api/v1/behaviour` to the private API listener while enforcing the deployment's access policy; keep port 8100 off public interfaces.

## Independent commands

Each command can run through `npm run behaviour:<name>` or `scripts/behaviour <command>`.

| Command | Purpose |
| --- | --- |
| `behaviour:env` | Validate private configuration without printing secrets |
| `behaviour:migrate` | Apply idempotent schema migrations |
| `behaviour:import-history` | Transactionally import all Phase 1 raw evidence and canonical rounds |
| `behaviour:reconcile` | Verify raw/source/canonical counts, H/L/E totals, time groups, baseline replay |
| `behaviour:winner-sync-once` | Verify the Phase 1 identity overlap and sync one batch from Winner Predict PostgreSQL |
| `behaviour:winner-sync` | Run the Winner Predict read-only database sync in the foreground |
| `behaviour:orbit-test` | Verify the first running payout Watch, latest snapshot, and first history page |
| `behaviour:backfill-orbit` | Run one cursor/history sync using verified snapshot overlap |
| `behaviour:collector` | Run the continuous independent Orbit collector in foreground |
| `behaviour:worker` | Run the separate durable analysis worker in foreground |
| `behaviour:start` | Start the API, worker, and configured database sync under the Node.js process manager |
| `behaviour:stop` | Stop the Node.js Behaviour process manager and its children |
| `behaviour:status` | Show local process and API health state |
| `behaviour:health` | Check liveness, readiness, and database-backed API health |
| `behaviour:logs` | Show recent local API, worker, and collector logs |

`npm start` starts the shared frontend and all configured local services. Behaviour logs are written under `data/behaviour/logs`. Container files are optional deployment assets; the local npm startup path does not invoke them. Do not attach the Orbit credential to the frontend service.

## Data and timestamp contract

The frozen import is the `2026-11-13T08:30:13.384380Z` snapshot: 171,378 reconciled source records and 50,123 canonical H/L/E rounds. It imports row-level source payloads, reconciliation status, SHA-256 hashes, every source-to-round mapping, 499 Phase 1 forecast reference blocks, 427 empirical timestamp groups, research findings, and the 30 corrected motifs plus descriptive comparison motifs. Import checks the Phase 1 H=24,339, L=25,651, E=133 counts and expanding-baseline replay before commit. It hashes the actual bytes of every consumed research input and records that digest for repeat-import verification. Re-import is idempotent by snapshot identity; conflicts fail instead of rewriting existing records.

The retained Orbit ingestion tools inspect a Data Watch contract that exposes reading/page IDs, `checkedAt`, and payout DOM items, not round IDs or per-round event times. These earlier readings remain archived and are not the active source. `checkedAt` is observation/ingestion time; live event time remains null. The parser preserves the visible payout value and quarantines ambiguous or unanchored readings rather than inferring missing rounds.

The legacy Orbit collector distinguishes API reachability from the upstream browser collector state. It is not started by the current Node.js Behaviour runtime.

Canonical historical order reproduces the Phase 1 timestamp/index/research-key ordering and flags shared timestamps/inversions. The generated `analysis_position` is an internal ordinal, not a provider round ID. Live rounds retain their collector order quality and timestamp uncertainty.

## Reproduction and validation

```sh
scripts/behaviour env-check
scripts/behaviour import-history
scripts/behaviour reconcile
source behaviour_detection/.venv/bin/activate
python -m pytest behaviour_detection/tests -q
npm run build --prefix frontend
```

The independent API is read-only under `/api/v1/behaviour`; health endpoints are `/health/live` and `/health/ready`. Worker transactions are per round. Stored rounds remain durable if analysis fails; the checkpoint only advances after a complete analysis commit. All-round means, extrema, log volatility and tail counts update incrementally; current quantiles are reported for rolling windows, while the full-population quantiles remain explicitly tied to the frozen Phase 1 snapshot. API, worker, and database sync logs are separate local files. Shutdown does not stop or restart either database.

The isolated Python packages are pinned in `requirements.lock` for API, database and collector runtime. `requirements-test.txt` adds only the pinned pytest test runner. The current host validation environment is Python 3.13; the container image uses Python 3.11 and the same package lock.

## Runtime data source

The sync auto-detects `DATABASE_URL` from `backend/.env` and opens a read-only PostgreSQL connection. It copies only rows after a verified overlap into Behaviour's database; it does not write to Winner Predict or send its database credentials to the browser. Set `BEHAVIOUR_SOURCE_DATABASE_URL` when the canonical source uses a different connection string. Existing Orbit payloads remain in the Behaviour database as audit records but are not polled by the current Node runtime.

Verified LIVE observations also arrive from Winner Predict's existing `/api/collector/round-observed` path before its PostgreSQL handoff. Behaviour stores these in its own `behaviour_api_round_inbox` table immediately, keyed by stream and round identity. Contiguous observations are promoted to Behaviour's `canonical_rounds` for the same ordered analyzer; gaps stay buffered until the missing verified rounds arrive. The later read-only PostgreSQL sync reconciles the same identities and does not create duplicate rounds. Unverified, recovered, conflicting, and out-of-order observations cannot create early eligibility.

Rolling HIGH SHADOW intents use a durable outbox in the existing HIGH series state. The Behaviour worker posts to Winner Predict's private `127.0.0.1:8000` endpoint using the backend `API_KEY` read server-side from `backend/.env` (or `BEHAVIOUR_SHADOW_BACKEND_API_KEY`); this credential never reaches the browser. The backend verifies that the source is the latest verified live API observation (or the latest committed round for replay), that its immediate successor has not already been observed, and that exactly one SHADOW session is active. A stale source, gap, target conflict, or ambiguous session is recorded as rejected. An additional shared-secret check can be enabled by setting the same `BEHAVIOUR_SHADOW_SHARED_SECRET` in `behaviour_detection/.env` and `backend/.env`. SHADOW settlement is reconciled back from the existing signal store and never changes the theoretical Behaviour result ledger.
