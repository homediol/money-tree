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
Risk Management
        ↓
Execution Interface
```

The registry contains the source project's Start Check policy, separate legacy
tracker, five independent research rules, and accepted combined user policy.
Each session snapshots its selected rules and configuration version. Until a
rule is enabled and selected for a session, decisions remain `NO_ACTION`; a
session with enabled rules but no TRUE proposal waits. The engine only creates
SHADOW selections and cannot authorize execution.

## Services

From the repository root, `npm start` runs the service supervisor. The
collector, backend, and dashboard can also run separately:

```bash
npm run collector
npm run backend
npm run frontend
```

The collector uses an OS lock to prevent duplicate collector owners. It owns the
browser session used for Aviator history collection, persists canonical rounds
to PostgreSQL, and uses its durable outbox when delivery is temporarily
unavailable. Complete any login, CAPTCHA, or 2FA step in the collector's
existing browser session.

Configure `backend/.env` from `backend/.env.example`. Production backend startup
requires PostgreSQL (`REQUIRE_POSTGRES=true`). The legacy JSON round file is
read only for a one-time history import. Do not commit database credentials,
API keys, or browser-session data.

For a remotely reachable dashboard, set a random `API_KEY` and restrict
`CORS_ORIGINS` to the dashboard origins. Enter the same API key in the
dashboard's Settings page. `PUBLIC_READ_ONLY=true` allows unauthenticated
dashboard reads while keeping write actions protected.

## Dashboard

- **Rules Engine:** configuration and evaluation status; initially zero rules,
  `NO_ACTION`, automatic execution `OFF`.
- **Round History:** persisted real rounds and history status.
- **Betting:** betting mode, safety controls, and emergency stop.
- **Bet Results:** execution, balance, and reconciliation records.
- **Operations & Recovery:** backend, collector, browser, and recovery status.
- **Settings:** API access and backend/history status.

Useful endpoints include `/health`, `/api/history/status`,
`/api/rules-engine/status`, `/api/rules-engine/evaluate`, `/api/risk/status`,
and `/ws/live`. The decision submission endpoint fails closed while the Rules
Engine cannot authorize execution; real-money execution remains off.

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

PostgreSQL owns canonical history, settings, audit records, risk state, and
financial execution records. The prediction-only legacy tables are retained
in a reversible PostgreSQL schema archive; real rounds and financial records
remain in their active tables.
