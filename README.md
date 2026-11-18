# Winner Predict

Winner Predict collects real Aviator rounds into PostgreSQL and provides a dashboard for round history, betting safety, operations, and recovery. The migrated Rules Engine is available in SHADOW mode; all rules start disabled, so the initial decision is `NO_ACTION`. There are no active ML or statistical prediction services, and automatic real-money execution remains `OFF`.

## Architecture

```text
Real Round Collector
        ↓
PostgreSQL History
        ↓
Rules Engine (SHADOW; rules disabled by default)
        ↓
Independent Rule Evaluations
        ↓
Rule Aggregator
        ↓
Decision Interface
        ↓
Existing Betting Executor
        ↓
Execution Result and Reconciliation
```

The former Risk Management subsystem has been removed from the active
application, API, and dashboard. The operator emergency stop is an independent
operational latch. Fixed simulated stake is configured in the Rules Engine;
cashout remains fixed at 2.00x. LIVE_REAL still requires explicit operator
authorization and verified platform, target, deadline, acceptance, and
settlement evidence.

The registry contains the source project's Start Check policy, separate legacy
tracker, five independent research rules, and accepted combined user policy.
Each session snapshots its selected rules and configuration version. Until a
rule is enabled and selected for a session, decisions remain `NO_ACTION`; a
session with enabled rules but no TRUE proposal waits. The engine only creates
SHADOW selections and cannot authorize execution.

## Services

From the repository root, `npm start` runs the Node.js service supervisor for
Winner Predict, Behaviour Detection, and the shared dashboard. Behaviour
Detection's API, analysis worker, and configured Orbit collector are managed as
local processes; startup does not use Docker. Its dedicated database must be
available locally before the Behaviour service can become ready. To run only
Behaviour Detection, use `npm run behaviour:start`.

The Winner Predict collector, backend, and dashboard can also run separately:

```bash
npm run collector
npm run backend
npm run frontend
```

The collector uses an OS lock to prevent duplicate owners and collects Round
History exclusively through the Orbit Data Watch API. It rediscovers the saved
payouts watch, replays cursor-paginated readings, and stores canonical rounds in
PostgreSQL with a durable delivery outbox. The authenticated owner SSE stream
delivers each saved reading as soon as Orbit records it and reconnects after
collector or backend restarts. Configure `ORBIT_API_BASE_URL` and
`ORBIT_API_KEY` in `backend/.env`; the key stays server-side. Collection does
not depend on an open Data Watch dashboard page or create a watch on restart.

Orbit currently reads the selected Chrome page on the watch's saved interval
(the existing watch is set to 1000 ms). The SSE connection removes collector
polling after a reading is stored, but it cannot make Orbit observe page changes
before its next browser read. Data Watch returns up to 200 selector matches per
read; the collector reports a truncated reading if the page returns more. The
saved Chrome connection and target game page must be available; collection
reports the source as waiting or unhealthy and continues retrying while either
is disconnected.

Configure `backend/.env` from `backend/.env.example`. Production backend startup
requires PostgreSQL (`REQUIRE_POSTGRES=true`). The legacy JSON round file is
read only for a one-time history import. Do not commit database credentials,
API keys, or browser-session data.

For remote use, set a random `API_KEY` and restrict `CORS_ORIGINS` to trusted
dashboard origins. Remote dashboard access requires that owner key. A dashboard
opened on localhost uses a short-lived HttpOnly session without exposing the key
to frontend code; sensitive LIVE authorization still requires the owner bearer
key. The legacy `PUBLIC_ACCESS` and `PUBLIC_READ_ONLY` settings no longer open
private routes. The only anonymous integration reads are the committed-round
endpoints `GET /api/v1/rounds/latest` and `GET /api/v1/rounds`; see the
[public API guide](docs/public-api.md).

## Dashboard

- **Rules Engine:** configuration and evaluation status, LIVE safety controls,
  execution readiness, and verified financial summaries; initially zero rules,
  `NO_ACTION`, automatic execution `OFF`.
- **Round History:** persisted real rounds and history status.
- Recovery runs automatically during backend startup; shared execution and audit history remains available to backend reconciliation and safety services.
- **Settings:** API access and backend/history status.
- **Rules Signal API:** live health and BET signal monitoring, API checks,
  owner-managed scoped keys, and in-app integration documentation.

The Rules Signal API consumer surface is rooted at `/api/v1`. External consumer
keys are read-only and scoped; an internal publisher key can only write
authoritative signal events. Generate and rotate keys from the owner-only
Rules Signal API page. PostgreSQL stores only key hashes. The publisher must
call `POST /api/v1/publisher/heartbeat` to appear connected; a disconnected
publisher status is retained until a real heartbeat or valid signal arrives.
The Python API distributes Node Rules Engine results and never places bets.

Useful endpoints include `/health`, `/api/history/status`,
`/api/rules-engine/status`, `/api/rules-engine/evaluate`, `/api/live/status`,
and `/ws/live`. The decision submission endpoint forwards only a matching,
unexpired target-bound selection through the existing executor; LIVE_REAL
remains blocked unless each independent authorization and platform check passes.

See [the Rules Engine migration and operating notes](docs/rules-engine-migration.md)
for the source rule manifest, mode differences, configuration, persistence,
and verification limits. Runtime restart state is saved under
`application_state.rules_engine_state_v1`; the same transaction writes the
session, per-rule evaluation, shared selection, blocked intent, target outcome,
balance/P&L, configuration-version, and stop-event ledgers. The repository
creates the required runtime tables during normal database initialization.
`backend/migrations/20261014_rules_engine_shadow.sql` is an additional
standalone normalized schema and is not required for the current runtime path.

## Verification

```bash
cd backend
. .venv/bin/activate
pytest -q
```

Build the frontend with:

```bash
cd frontend
npm run build
```

PostgreSQL owns canonical history, settings, audit records, the operational
emergency-stop latch, and financial execution records. There are no Risk
Management-specific tables; old generic application-state snapshots and
historical execution profile values remain for compatibility and audit. The prediction-only legacy tables are retained
in a reversible PostgreSQL schema archive; real rounds and financial records
remain in their active tables.
