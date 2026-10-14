# Legacy prediction removal dependency map

Audit snapshot: 2026-10-13, before removal. This map distinguishes retired prediction code from shared history, browser, betting, risk, and execution infrastructure.

Pre-cleanup verification: PostgreSQL `winner_predict.public` had 27,505 `aviator_rounds` (indexes 434–27,966; latest stored timestamp 2026-10-11 18:17:49 UTC). Ordered round identity/multiplier/timestamp/continuity checksum: `8829302a32ec62dd077aaf3f5d2ed8846464c903d0fcd49fb5bc288bc1a3715e`. No foreign keys into or out of the prediction-only table candidates were found. Betting mode was `SHADOW_REALISTIC`; 9 balance ledger entries were present and 0 execution/event/reconciliation records. The side SQLite file had 13,213 `rounds`, passed `integrity_check`, and was backed up separately.

Verified pre-cleanup backups:

- PostgreSQL custom dump: `/tmp/winner-predict-postgres-before-prediction-removal.dump`, 52,497,772 bytes, SHA-256 `2830108a85ebbc649dd13ea472d94db4b5c7acf140b1bb9024573cdba6c908ad`; `pg_restore --list` succeeded.
- SQLite backup: `/tmp/winner-predict-sqlite-before-prediction-removal.sqlite3`, 71,385,088 bytes, SHA-256 `ca1a9124180d213dd6f8f9b0d3631f1a5559d21da1801201f54a8999ec17e77b`; `PRAGMA integrity_check` returned `ok`.
- Source/artifact archive: `/tmp/winner-predict-legacy-prediction-source-and-artifacts-20261013.tar.gz`; gzip validation succeeded and it contains 1,538 paths; SHA-256 `8064809f51c411ebb96ef292f6964dbce1d92489bc182525c7877ea1dbf73fe6`.

## Runtime graph

```text
bot/collector/*
  -> PostgreSQL aviator_rounds / collector delivery state
  -> backend collector observation endpoints and round-event stream
  -> backend/main.py process_history_update / round observers
       -> AppState.reload and DatasetService feature/training cache
       -> ModelRegistry + CandidateObserver (legacy ML)
       -> OpportunityResearchEngine + V3/V4 frozen observers (legacy model/opportunity stack)
       -> StatisticalEngineService + 500-target experiment monitor
       -> AnalyticsReportEngine and websocket updates

backend/main.py lifespan
  -> Repository / real round history
  -> BettingManager + RiskManager + ReconciliationService + betting mode
  -> SystemOrchestrator (history/collector/health/balance/operations monitors)
  -> old ML/statistical observer executors and readiness-triggered training

frontend/src/main.jsx + Sidebar.jsx
  -> old Model/Prediction/Pattern/Evidence/Opportunity/Statistical Engine pages
  -> /api/ml, /api/models, /api/opportunities/selective, /api/statistical-engine,
     /api/readiness and prediction/evidence/decision endpoints

root package.json -> scripts/start-all.js
  -> legacy Flask prediction API (backend/run.py, port 5000)
  -> bot control API (backend/bot_api.py, port 5001)
  -> single Node collector, Winner Predict FastAPI (port 8000), frontend (5173)
  -> legacy aviator_enterprise ML API (port 8002, optional)
```

## Retired prediction components and consumers

| Area | Legacy components | Runtime/API/UI consumers | Cleanup boundary |
|---|---|---|---|
| ML training/inference | `backend/app/ml/*`, `backend/training/*`, `backend/prediction/*`, `backend/model.py`, `backend/models/*`, `aviator_enterprise/*` | `backend/main.py` lifespan/history dispatch; `/api/ml`, `/api/models`, training/readiness routes; `backend/app.py` Flask app; launcher services | Remove training, inference, candidate/model registries and legacy standalone prediction services. |
| Statistical/pattern prediction | `backend/app/statistical_engine/*`, `backend/app/services/{feature_engineering,pattern_engine,evidence_engine,probability_engine,signal_engine}.py` and corresponding API routes | history observer dispatch, statistics/readiness/evidence/decision responses, prediction dashboard pages | Remove inference/evidence/policy paths. Keep collector/history, risk/stop controls, execution reconciliation, generic history and health. |
| Frozen / rare opportunities | `opportunity.py`, `opportunity_v2.py`, `opportunity_v3.py`, `opportunity_v4.py`, rare policy modules and opportunity API | Frozen V3/V4 monitors, queue workers, automatic rollover, opportunity endpoints and UI | Remove observers, model evaluation, experiment interfaces and generated research artifacts. |
| Automatic evaluation | `SystemReadiness` training path, `api/readiness.py` scheduling, ML trainer/registry intervals, frozen/statistical monitors | `backend/main.py` startup and `SystemOrchestrator` monitors; legacy Flask retraining routes | Remove scheduling and all prediction executors; retain system/collector/balance/operations monitoring. |
| Frontend prediction UX | `MLDashboard`, `ModelPerformance`, `StatisticalEngine`, `SelectiveOpportunities`, `Predictions`, `PatternExplorer`, `EvidenceExplorer`, model/evidence/prediction widgets | `frontend/src/main.jsx`, `Sidebar.jsx`, Dashboard and page API calls | Replace active prediction views with the empty Rules Engine status screen. Preserve History, Betting, auth, settings, and system safety views. |

## Persistence inventory and safety boundary

`backend/app/database/repository.py` owns a single mixed schema. The authoritative round data is `aviator_rounds`; collector delivery/event tables, accounts/settings/application state, decision/audit records, bet/execution events, balance ledger, reconciliation records, risk/betting state, and shadow/live records are shared or safety-relevant and must remain intact.

Prediction-only candidates identified in the schema include `patterns`, `signals`, `signal_results`, `statistics`, `ml_predictions`, `ml_observations`, `statistical_engine_*`, `opportunity_observations`, all V3/V4 opportunity ledgers and queues, `prediction_evidence`, `prediction_evaluations`, `model_versions`, `model_artifacts`, `training_runs`, `model_candidates`, `analytics_reports`, `backtest_runs`, `research_runs`, and prediction-only decision visibility/evaluation tables. `decision_records` was confirmed to be used only by the retired prediction-backed decision path; it is archived separately. `decision_audit`, `system_audit_log`, execution, balance, reconciliation, settings, account/token, shadow, live-session, collector, and round tables remain in `public`. These candidates are moved to a reversible schema archive rather than dropped.

## Artifact and dependency inventory

- Model artifacts: `backend/trained_models/`, `backend/artifacts/`, legacy model directories under `backend/` and `aviator_enterprise/`.
- Training/derived datasets: `data/processed/training_dataset.csv`, `data/processed/dataset_status.json`, old decisions/predictions JSON, feature metadata/cache files, `backend/artifacts/training_datasets/`.
- Generated reports: `backend/reports/` (historical rule discovery, Frozen V3, statistical engine) and training logs/results.
- Runtime-only/user data excluded from cleanup: `data/roundhistory.json`, PostgreSQL round history, collector outboxes/status/logs, Chrome profile/session, SQLite database pending integrity and backup checks, and all financial/execution/audit data.
- `analysis_2x/` is an independent user research workspace and is outside this retirement; preserve it.
- Python packages with prediction-only use in the active source tree: scikit-learn, joblib, and SciPy. Keep pandas/NumPy only while shared history/analytics code still imports them. Keep PostgreSQL, Playwright, FastAPI, React, and socket dependencies.

## Removal order

1. Disconnect prediction observers, trainers, and old route registrations from startup and the launcher.
2. Add the empty Rules Engine contract/status path and replace the active dashboard view.
3. Remove prediction-only imports, modules, APIs, UI, schedules, settings, scripts, and dependencies.
4. Create and verify a backup of every retired artifact and the authoritative database before removing artifacts or changing schema.
5. Preserve prediction tables as a reversible, isolated archive unless a verified database snapshot and a strict exclusive-table check permit a specific reversible migration. Never touch shared round, financial, account, setting, audit, or execution tables.
6. Verify round counts/identity, service startup/build, single collector/browser ownership, absence of prediction workers, no-rule `NO_ACTION`, and automatic execution OFF.

## Removal completion record (2026-10-14)

The post-cleanup runtime is history/collector, account and session handling, betting safety, risk, reconciliation, operations/recovery, descriptive round distribution, and an empty Rules Engine. The former root-level Flask prediction helpers (`trainer.py`, `ws_server.py`, `risk_engine.py`, `risk_statistics.py`, and `utils.py`) and the obsolete permutation-validation script were found disconnected from the active FastAPI application and removed after archival. The active `/ws/live` WebSocket remains in `backend/main.py`.

Before the PostgreSQL schema move, a full ordered-row snapshot of `aviator_rounds` had 27,505 rows, index range 434–27,966, and SHA-256 `7eb9eacaffb06004b451209db4d83665f3b4988008799b6b3b3d0429ede0c4fa` (canonical JSON of every column, ordered by `round_index`). The retired-table preflight found no foreign keys crossing the archive boundary; the two Statistical Engine foreign keys are internal to tables moved together. Existing data remains intact in the reversible archive schema.

The remaining legacy source and old prediction-output archive is `/tmp/winner-predict-remaining-legacy-and-old-prediction-data-20261014.tar.gz` (1,903 paths, gzip validation passed, SHA-256 `6e29b6a17cbae706f5113211f6b03a818e3550db2c447093298388aaf816de12`). It includes the disconnected legacy sources, the unused backend npm package tree, and the old JSON prediction outputs before their removal.

The retired root Python dependency manifest was separately archived at `/tmp/winner-predict-retired-root-python-dependencies-20261014.tar.gz`; gzip validation passed and SHA-256 is `14ddb8cdce9c695223b5162ed4b4a681e85f71f328bcadb5a2c3a4c3b3b56e217`.

## Final verification (2026-10-14)

- Backend tests: 187 passed; 2 PostgreSQL persistence cases skipped because `TEST_DATABASE_URL` is not configured.
- Collector tests: 10 passed. JavaScript syntax checks passed for the legacy collector entrypoints and `PostgresRoundStore.js`.
- Frontend production build passed (1,842 modules transformed).
- Active backend/frontend source and dependency manifests have no references to the retired ML, statistical, model-registry, frozen opportunity, or old prediction APIs. Collector comments were also cleared of obsolete ML wording.
- The full authoritative round table was verified immediately after the reversible schema migration: 27,505 rows, indexes 434–27,966, and the full-row SHA-256 above matched before and after. No shared financial, account, audit, execution, collector, or round tables were dropped.
- At final runtime verification the configured PostgreSQL 17 cluster was stopped. A configured-database connection attempt failed; starting the cluster required sudo credentials unavailable in this session. A live backend startup against PostgreSQL therefore remains unverified. The verified PostgreSQL dump and SQLite backup remain available in `/tmp`.
- No backend, collector, or Chrome/Chromium process was running during the process check. The persisted collector status was `OFFLINE`; its last successful collection was `2026-10-11T18:17:49.158Z`. The stored PID/running flags were stale and did not correspond to a live process. No ML/statistical worker process was running.
- The Rules Engine status is `NOT_CONFIGURED`, with zero rules, `NO_ACTION`, and automatic execution `OFF`. No real-money execution was initiated.

## Rules Engine migration follow-up (2026-10-14)

A final search found the obsolete root `fix_setup.sh`, which recreated a Flask/TensorFlow/scikit-learn training environment, overwrote the active backend requirements file, and ran the retired trainer. It was removed after creating and SHA-256-verifying `/tmp/winner-predict-fix-setup-before-rules-migration-20261014.sh` (`b7fbc6d73794b77e8faf400828bf47e2c9d7738e5e2c22b5018caa26c963da34`). The current backend retains pandas for shared history loading; NumPy remains a pandas/runtime dependency and the independent `analysis_2x` research workspace is preserved.
