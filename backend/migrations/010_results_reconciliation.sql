-- Part 10: append-only results, reconciliation and financial history.
CREATE TABLE IF NOT EXISTS execution_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, execution_id TEXT NOT NULL,
  state TEXT NOT NULL, recorded_at TEXT NOT NULL, payload TEXT NOT NULL,
  UNIQUE(execution_id, state, recorded_at)
);
CREATE TABLE IF NOT EXISTS balance_ledger (
  ledger_id TEXT PRIMARY KEY, execution_id TEXT, session_id TEXT,
  event_type TEXT NOT NULL, amount REAL, internal_expected_balance REAL,
  platform_observed_balance REAL, reconciled_balance REAL,
  balance_status TEXT NOT NULL, recorded_at TEXT NOT NULL, payload TEXT NOT NULL,
  UNIQUE(execution_id, event_type)
);
CREATE TABLE IF NOT EXISTS reconciliation_records (
  reconciliation_id TEXT PRIMARY KEY, execution_id TEXT NOT NULL UNIQUE,
  round_id TEXT NOT NULL, status TEXT NOT NULL, outcome TEXT NOT NULL,
  created_at TEXT NOT NULL, payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS prediction_evaluations (
  evaluation_id TEXT PRIMARY KEY, prediction_id TEXT NOT NULL, round_id TEXT NOT NULL,
  original_probability REAL NOT NULL, actual_multiplier REAL NOT NULL,
  actual_target INTEGER NOT NULL, evaluation_result TEXT NOT NULL,
  evaluated_at TEXT NOT NULL, prediction_snapshot TEXT NOT NULL,
  UNIQUE(prediction_id, round_id)
);
CREATE TABLE IF NOT EXISTS session_metrics (
  session_id TEXT PRIMARY KEY, updated_at TEXT NOT NULL, payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS system_audit_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT, event_type TEXT NOT NULL,
  entity_id TEXT, recorded_at TEXT NOT NULL, payload TEXT NOT NULL
);
