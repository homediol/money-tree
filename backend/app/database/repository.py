from __future__ import annotations

import json
import hashlib
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SCHEMA = """
CREATE TABLE IF NOT EXISTS rounds (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  round_id TEXT,
  round_index INTEGER UNIQUE NOT NULL,
  multiplier REAL NOT NULL,
  timestamp TEXT,
  target INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS patterns (
  pattern_id TEXT PRIMARY KEY,
  payload TEXT NOT NULL,
  updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS signals (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  timestamp TEXT DEFAULT CURRENT_TIMESTAMP,
  current_sequence TEXT NOT NULL,
  detected_pattern TEXT NOT NULL,
  probability REAL,
  confidence TEXT NOT NULL,
  status TEXT NOT NULL,
  payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS signal_results (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  signal_id INTEGER NOT NULL,
  actual_multiplier REAL,
  actual_outcome INTEGER,
  correct INTEGER,
  resolved_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS model_versions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  version TEXT NOT NULL,
  model_type TEXT NOT NULL,
  path TEXT,
  metrics TEXT NOT NULL,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS model_artifacts (
  model_version TEXT PRIMARY KEY,
  sha256 TEXT NOT NULL,
  artifact BLOB NOT NULL,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS training_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  started_at TEXT DEFAULT CURRENT_TIMESTAMP,
  completed_at TEXT,
  status TEXT NOT NULL,
  dataset_size INTEGER NOT NULL,
  metrics TEXT
);
CREATE TABLE IF NOT EXISTS statistics (
  key TEXT PRIMARY KEY,
  payload TEXT NOT NULL,
  updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS ml_predictions (
  prediction_id TEXT PRIMARY KEY,
  source_round_id TEXT NOT NULL,
  created_at TEXT NOT NULL,
  payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ml_observations (
  observation_id TEXT PRIMARY KEY,
  model_version TEXT NOT NULL,
  source_round_id TEXT NOT NULL,
  source_round_index INTEGER NOT NULL,
  created_at TEXT NOT NULL,
  status TEXT NOT NULL,
  payload TEXT NOT NULL,
  UNIQUE(model_version, source_round_id)
);
CREATE INDEX IF NOT EXISTS ml_observations_pending ON ml_observations(status, source_round_index);
CREATE TABLE IF NOT EXISTS opportunity_observations (
  observation_id TEXT PRIMARY KEY,
  model_version TEXT NOT NULL,
  source_round_id TEXT NOT NULL,
  source_round_index INTEGER NOT NULL,
  created_at TEXT NOT NULL,
  status TEXT NOT NULL,
  payload TEXT NOT NULL,
  UNIQUE(model_version, source_round_id)
);
CREATE INDEX IF NOT EXISTS opportunity_observations_status ON opportunity_observations(status, source_round_index);
CREATE TABLE IF NOT EXISTS opportunity_v4_assessments (
  assessment_id TEXT PRIMARY KEY,
  model_version TEXT NOT NULL,
  round_id TEXT NOT NULL,
  round_index BIGINT NOT NULL,
  observed_at TEXT NOT NULL,
  payload TEXT NOT NULL,
  UNIQUE(model_version, round_id)
);
CREATE INDEX IF NOT EXISTS opportunity_v4_assessments_round_idx ON opportunity_v4_assessments(round_index);
CREATE TABLE IF NOT EXISTS opportunity_v4_outcomes (
  assessment_id TEXT PRIMARY KEY,
  target_round_id TEXT NOT NULL,
  target_round_index BIGINT NOT NULL,
  actual_multiplier DOUBLE PRECISION NOT NULL,
  target_hit_2_10x INTEGER NOT NULL,
  resolved_at TEXT NOT NULL,
  payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS opportunity_research_gates (
  gate_hash TEXT PRIMARY KEY,
  model_version TEXT NOT NULL,
  model_hash TEXT NOT NULL,
  status TEXT NOT NULL,
  created_at TEXT NOT NULL,
  payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS opportunity_selection_policies (
  policy_hash TEXT PRIMARY KEY,
  policy_id TEXT NOT NULL,
  model_version TEXT NOT NULL,
  model_hash TEXT NOT NULL,
  status TEXT NOT NULL,
  development_cutoff_round BIGINT NOT NULL,
  created_at TEXT NOT NULL,
  payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS opportunity_experiments (
  experiment_id TEXT PRIMARY KEY,
  model_version TEXT NOT NULL,
  model_hash TEXT NOT NULL,
  gate_hash TEXT NOT NULL,
  status TEXT NOT NULL,
  start_round_index BIGINT NOT NULL,
  last_round_index BIGINT NOT NULL,
  rounds_observed INTEGER NOT NULL DEFAULT 0,
  updated_at TEXT NOT NULL,
  payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS opportunity_experiments_status_idx
  ON opportunity_experiments(status, updated_at);
CREATE TABLE IF NOT EXISTS opportunity_experiment_targets (
  experiment_id TEXT NOT NULL,
  target_round_index BIGINT NOT NULL,
  observed_ordinal INTEGER,
  source_round_index BIGINT,
  source_round_id TEXT,
  target_round_id TEXT,
  assessment_id TEXT,
  classification TEXT NOT NULL,
  selection_state TEXT,
  selection_reason TEXT,
  rank_at_selection INTEGER,
  selected BOOLEAN NOT NULL DEFAULT FALSE,
  prediction_id TEXT,
  model_version TEXT,
  model_hash TEXT,
  gate_hash TEXT,
  feature_snapshot_hash TEXT,
  opportunity_score DOUBLE PRECISION,
  selection_score DOUBLE PRECISION,
  assessment_created_at TEXT,
  selected_at TEXT,
  target_was_absent_at_selection BOOLEAN,
  target_observed_at TEXT,
  target_stored_at TEXT,
  target_created_at TEXT,
  actual_multiplier DOUBLE PRECISION,
  result TEXT,
  resolved_at TEXT,
  reason TEXT,
  payload TEXT NOT NULL,
  PRIMARY KEY(experiment_id, target_round_index)
);
CREATE INDEX IF NOT EXISTS opportunity_experiment_targets_ordinal_idx
  ON opportunity_experiment_targets(experiment_id, observed_ordinal);
CREATE TABLE IF NOT EXISTS opportunity_experiment_predictions (
  prediction_id TEXT PRIMARY KEY,
  experiment_id TEXT NOT NULL,
  target_round_index BIGINT NOT NULL,
  window_number INTEGER NOT NULL,
  rank_at_selection INTEGER NOT NULL,
  selected_at TEXT NOT NULL,
  status TEXT NOT NULL,
  actual_multiplier DOUBLE PRECISION,
  result TEXT,
  resolved_at TEXT,
  payload TEXT NOT NULL,
  UNIQUE(experiment_id, target_round_index)
);
CREATE INDEX IF NOT EXISTS opportunity_experiment_predictions_window_idx
  ON opportunity_experiment_predictions(experiment_id, window_number, selected_at);
CREATE TABLE IF NOT EXISTS opportunity_experiment_checkpoints (
  experiment_id TEXT NOT NULL,
  checkpoint_rounds INTEGER NOT NULL,
  completed_at TEXT NOT NULL,
  payload TEXT NOT NULL,
  PRIMARY KEY(experiment_id, checkpoint_rounds)
);
CREATE TABLE IF NOT EXISTS opportunity_v4_observer_queue_control (
  singleton SMALLINT PRIMARY KEY CHECK (singleton = 1),
  last_enqueued_round_index BIGINT NOT NULL,
  initialized_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS opportunity_v4_observer_queue (
  round_index BIGINT PRIMARY KEY,
  round_id TEXT NOT NULL UNIQUE,
  enqueued_at TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'PENDING',
  attempts INTEGER NOT NULL DEFAULT 0,
  assessment_id TEXT,
  reason TEXT,
  processed_at TEXT,
  payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS opportunity_v4_observer_queue_pending_idx
  ON opportunity_v4_observer_queue(status, round_index);
CREATE TABLE IF NOT EXISTS prediction_evidence (
  evidence_id TEXT PRIMARY KEY,
  prediction_id TEXT UNIQUE NOT NULL,
  source_round_id TEXT NOT NULL,
  calculated_at TEXT NOT NULL,
  payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS decision_records (
  decision_id TEXT PRIMARY KEY,
  idempotency_key TEXT UNIQUE NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS decision_audit (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  decision_id TEXT NOT NULL,
  status TEXT NOT NULL,
  recorded_at TEXT NOT NULL,
  payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS bet_executions (
  execution_id TEXT PRIMARY KEY,
  decision_id TEXT NOT NULL,
  target_round_id TEXT NOT NULL,
  profile TEXT NOT NULL,
  bet_amount REAL NOT NULL,
  cashout_target REAL NOT NULL,
  status TEXT NOT NULL,
  simulated INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  placed_at TEXT,
  resolved_at TEXT,
  result_multiplier REAL,
  balance_before REAL,
  balance_after REAL,
  profit_loss REAL,
  error TEXT,
  payload TEXT NOT NULL,
  UNIQUE(decision_id, target_round_id)
);
CREATE INDEX IF NOT EXISTS bet_executions_status_idx ON bet_executions(status);
CREATE TABLE IF NOT EXISTS execution_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  execution_id TEXT NOT NULL,
  state TEXT NOT NULL,
  recorded_at TEXT NOT NULL,
  payload TEXT NOT NULL,
  UNIQUE(execution_id, state, recorded_at)
);
CREATE TABLE IF NOT EXISTS balance_ledger (
  ledger_id TEXT PRIMARY KEY,
  execution_id TEXT,
  session_id TEXT,
  event_type TEXT NOT NULL,
  amount REAL,
  internal_expected_balance REAL,
  platform_observed_balance REAL,
  reconciled_balance REAL,
  balance_status TEXT NOT NULL,
  recorded_at TEXT NOT NULL,
  payload TEXT NOT NULL,
  UNIQUE(execution_id, event_type)
);
CREATE TABLE IF NOT EXISTS reconciliation_records (
  reconciliation_id TEXT PRIMARY KEY,
  execution_id TEXT NOT NULL UNIQUE,
  round_id TEXT NOT NULL,
  status TEXT NOT NULL,
  outcome TEXT NOT NULL,
  created_at TEXT NOT NULL,
  payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS prediction_evaluations (
  evaluation_id TEXT PRIMARY KEY,
  prediction_id TEXT NOT NULL,
  round_id TEXT NOT NULL,
  original_probability REAL NOT NULL,
  actual_multiplier REAL NOT NULL,
  actual_target INTEGER NOT NULL,
  evaluation_result TEXT NOT NULL,
  evaluated_at TEXT NOT NULL,
  prediction_snapshot TEXT NOT NULL,
  UNIQUE(prediction_id, round_id)
);
CREATE TABLE IF NOT EXISTS session_metrics (
  session_id TEXT PRIMARY KEY,
  updated_at TEXT NOT NULL,
  payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS system_audit_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  event_type TEXT NOT NULL,
  entity_id TEXT,
  recorded_at TEXT NOT NULL,
  payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS process_lifecycle (
  event_key TEXT PRIMARY KEY,
  event_type TEXT NOT NULL,
  recorded_at TEXT NOT NULL,
  pid INTEGER,
  payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS backtest_runs (
  run_id TEXT PRIMARY KEY, created_at TEXT NOT NULL, dataset_hash TEXT NOT NULL,
  config TEXT NOT NULL, payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS research_runs (
  research_id TEXT PRIMARY KEY, created_at TEXT NOT NULL, dataset_hash TEXT NOT NULL,
  config TEXT NOT NULL, payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS shadow_sessions (
  session_id TEXT PRIMARY KEY, started_at TEXT NOT NULL, payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS shadow_trades (
  execution_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, target_round_id TEXT NOT NULL,
  decision_id TEXT NOT NULL, panel INTEGER NOT NULL DEFAULT 1,
  status TEXT NOT NULL, payload TEXT NOT NULL,
  UNIQUE(session_id,target_round_id,panel)
);
CREATE TABLE IF NOT EXISTS shadow_ledger (
  id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL, event_type TEXT NOT NULL,
  amount REAL, balance REAL, recorded_at TEXT NOT NULL, payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS live_sessions (
  session_id TEXT PRIMARY KEY, started_at TEXT NOT NULL, ended_at TEXT,
  mode TEXT NOT NULL, snapshot TEXT NOT NULL, payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS betting_mode_state (
  singleton INTEGER PRIMARY KEY CHECK(singleton=1),
  mode TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS collector_state (
  singleton INTEGER PRIMARY KEY CHECK(singleton=1),
  collector_state TEXT NOT NULL,
  last_processed_round_id TEXT,
  last_round_timestamp TEXT,
  contiguous_rounds INTEGER NOT NULL DEFAULT 0,
  total_history INTEGER NOT NULL DEFAULT 0,
  warmup_status TEXT NOT NULL DEFAULT 'WARMING_UP',
  collector_session_id TEXT,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS readiness_state (
  singleton INTEGER PRIMARY KEY CHECK(singleton=1),
  snapshot TEXT NOT NULL,
  restored_from_persistence INTEGER NOT NULL DEFAULT 0,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS model_candidates (
  model_version TEXT PRIMARY KEY,
  model_type TEXT NOT NULL,
  validation_folds TEXT NOT NULL,
  metrics TEXT NOT NULL,
  rejection_reason TEXT,
  deployment_status TEXT NOT NULL,
  next_evaluation_checkpoint TEXT,
  payload TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS application_state (
  state_key TEXT PRIMARY KEY,
  payload TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS analytics_reports (
  report_id TEXT PRIMARY KEY,
  report_type TEXT NOT NULL,
  period_key TEXT,
  start_time TEXT NOT NULL,
  end_time TEXT NOT NULL,
  first_round TEXT NOT NULL,
  last_round TEXT NOT NULL,
  round_count INTEGER NOT NULL,
  data_source TEXT NOT NULL,
  generated_at TEXT NOT NULL,
  generation_time_ms REAL,
  payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS analytics_reports_type_period_idx
  ON analytics_reports(report_type, period_key, generated_at);
"""


class _PostgresCursor:
    def __init__(self, cursor):
        self._cursor = cursor

    def __getattr__(self, name):
        return getattr(self._cursor, name)


class _PostgresConnection:
    """Compatibility wrapper for the repository's qmark SQL."""
    def __init__(self, raw):
        self.raw = raw

    @staticmethod
    def _sql(sql: str, params=None) -> str:
        sql = re.sub(r"^\s*PRAGMA\b[^;]*;?", "", sql, flags=re.I)
        ignored = "INSERT OR IGNORE" in sql.upper()
        sql = sql.replace("INSERT OR IGNORE", "INSERT")
        sql = sql.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY")
        sql = re.sub(r"\bBLOB\b", "BYTEA", sql, flags=re.I)
        if isinstance(params, dict):
            sql = re.sub(r":([A-Za-z_][A-Za-z0-9_]*)", r"%(\1)s", sql)
        else:
            sql = sql.replace("?", "%s")
        if ignored and "ON CONFLICT" not in sql.upper():
            sql = sql.rstrip().rstrip(";") + " ON CONFLICT DO NOTHING"
        return sql

    def execute(self, sql, params=()):
        sql = self._sql(sql, params)
        if not sql.strip():
            return _PostgresCursor(self.raw.cursor())
        return _PostgresCursor(self.raw.execute(sql, params))

    def executemany(self, sql, seq):
        first = seq[0] if isinstance(seq, list) and seq else None
        cursor = self.raw.cursor()
        cursor.executemany(self._sql(sql, first), seq)
        return _PostgresCursor(cursor)

    def executescript(self, script):
        for statement in script.split(";"):
            if statement.strip():
                self.execute(statement)

    def __enter__(self):
        self.raw.__enter__()
        return self

    def __exit__(self, exc_type, exc, tb):
        return self.raw.__exit__(exc_type, exc, tb)

    def __getattr__(self, name):
        return getattr(self.raw, name)


class Repository:
    def __init__(self, database_path: Path, database_url: str | None = None,
                 require_postgres: bool = False):
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.database_url = database_url or os.getenv("DATABASE_URL") or os.getenv("POSTGRES_URL") or os.getenv("WINNER_DATABASE_URL")
        self.require_postgres = bool(require_postgres or os.getenv("REQUIRE_POSTGRES", "false").lower() == "true")
        if self.require_postgres and not self.database_url:
            raise RuntimeError("REQUIRE_POSTGRES is enabled but DATABASE_URL is not configured")

    def ensure_opportunity_experiment_schema(self) -> None:
        """Apply only the idempotent PostgreSQL DDL needed by the durable experiment ledger.

        This is intentionally narrower than ``init()`` so an operator can repair
        a missing experiment migration without replaying unrelated history/data
        backfills while the live collector is writing rounds.
        """
        if not self.database_url:
            raise RuntimeError("authoritative PostgreSQL is required for prospective experiments")
        tables = {
            "opportunity_research_gates",
            "opportunity_selection_policies",
            "opportunity_experiments",
            "opportunity_experiment_targets",
            "opportunity_experiment_predictions",
            "opportunity_experiment_checkpoints",
            "opportunity_v4_observer_queue_control",
            "opportunity_v4_observer_queue",
        }
        indexes_on = {
            "opportunity_experiments",
            "opportunity_experiment_targets",
            "opportunity_experiment_predictions",
            "opportunity_experiment_checkpoints",
            "opportunity_v4_observer_queue",
        }
        statements = []
        for raw in SCHEMA.split(";"):
            statement = raw.strip()
            table_match = re.match(r"CREATE TABLE IF NOT EXISTS\s+([a-zA-Z0-9_]+)", statement, re.I)
            index_match = re.match(r"CREATE (?:UNIQUE )?INDEX IF NOT EXISTS\s+[a-zA-Z0-9_]+\s+ON\s+([a-zA-Z0-9_]+)", statement, re.I)
            if table_match and table_match.group(1) in tables:
                statements.append(statement)
            elif index_match and index_match.group(1) in indexes_on:
                statements.append(statement)
        found_tables = {re.match(r"CREATE TABLE IF NOT EXISTS\s+([a-zA-Z0-9_]+)", item, re.I).group(1)
                        for item in statements
                        if re.match(r"CREATE TABLE IF NOT EXISTS\s+([a-zA-Z0-9_]+)", item, re.I)}
        if found_tables != tables:
            raise RuntimeError("experiment schema declarations are incomplete: " + ", ".join(sorted(tables - found_tables)))
        with self.connect() as conn:
            for statement in statements:
                conn.execute(statement)
            present = {row["table_name"] for row in conn.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema=current_schema() "
                "AND table_name IN (?,?,?,?,?,?,?,?)",
                tuple(sorted(tables)),
            ).fetchall()}
            if present != tables:
                raise RuntimeError("PostgreSQL experiment schema migration did not create every required table")

    def connect(self):
        if self.database_url:
            try:
                import psycopg
                from psycopg.rows import dict_row
                return _PostgresConnection(psycopg.connect(self.database_url, row_factory=dict_row))
            except Exception:
                if self.require_postgres:
                    raise
                raise
        # Concurrent API, history-monitor and operations tasks use separate
        # connections. WAL permits readers during writes; busy_timeout turns a
        # short write overlap into bounded waiting instead of a request-level
        # "database is locked" failure.
        conn = sqlite3.connect(self.database_path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def init(self) -> None:
        if self.database_url:
            with self.connect() as conn:
                conn.executescript(SCHEMA)
                # This is the same authoritative table used by the Node collector.
                conn.execute("""CREATE TABLE IF NOT EXISTS aviator_rounds (
                    id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                    round_id TEXT NOT NULL UNIQUE,
                    round_index BIGINT NOT NULL UNIQUE,
                    multiplier NUMERIC(12,2) NOT NULL CHECK (multiplier >= 1),
                    timestamp TIMESTAMPTZ,
                    source TEXT NOT NULL DEFAULT 'collector',
                    raw JSONB NOT NULL DEFAULT '{}'::jsonb,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )""")
                conn.execute("ALTER TABLE aviator_rounds ADD COLUMN IF NOT EXISTS platform_timestamp TIMESTAMPTZ")
                conn.execute("ALTER TABLE aviator_rounds ADD COLUMN IF NOT EXISTS platform_round_id TEXT")
                conn.execute("ALTER TABLE aviator_rounds ADD COLUMN IF NOT EXISTS platform_round_index BIGINT")
                conn.execute("ALTER TABLE aviator_rounds ADD COLUMN IF NOT EXISTS observed_at TIMESTAMPTZ")
                conn.execute("ALTER TABLE aviator_rounds ADD COLUMN IF NOT EXISTS stored_at TIMESTAMPTZ")
                conn.execute("ALTER TABLE aviator_rounds ADD COLUMN IF NOT EXISTS local_round_index BIGINT")
                conn.execute("ALTER TABLE aviator_rounds ADD COLUMN IF NOT EXISTS round_identity_type TEXT")
                conn.execute("ALTER TABLE aviator_rounds ADD COLUMN IF NOT EXISTS round_index_source TEXT")
                conn.execute("ALTER TABLE aviator_rounds ADD COLUMN IF NOT EXISTS identity_confidence TEXT")
                conn.execute("ALTER TABLE aviator_rounds ADD COLUMN IF NOT EXISTS continuity_verified BOOLEAN NOT NULL DEFAULT FALSE")
                conn.execute("ALTER TABLE aviator_rounds ADD COLUMN IF NOT EXISTS gap_before BOOLEAN NOT NULL DEFAULT FALSE")
                conn.execute("ALTER TABLE aviator_rounds ADD COLUMN IF NOT EXISTS continuity_proof TEXT")
                conn.execute("UPDATE aviator_rounds SET stored_at=COALESCE(stored_at,created_at)")
                conn.execute("UPDATE aviator_rounds SET local_round_index=COALESCE(local_round_index,round_index)")
                conn.execute("UPDATE aviator_rounds SET observed_at=COALESCE(observed_at,timestamp) WHERE round_id ~ '^aviator-[0-9a-f]{24}$'")
                conn.execute("UPDATE aviator_rounds SET round_identity_type=COALESCE(round_identity_type,'COLLECTOR_OBSERVATION_HASH'), round_index_source=COALESCE(round_index_source,'LOCAL_SEQUENCE'), identity_confidence=COALESCE(identity_confidence,CASE WHEN continuity_verified THEN 'OVERLAP_VERIFIED_ORDER_ONLY' ELSE 'UNKNOWN' END), continuity_proof=COALESCE(continuity_proof,'LEGACY_UNVERIFIED')")
                conn.execute("UPDATE aviator_rounds SET platform_timestamp=NULL WHERE round_id ~ '^aviator-[0-9a-f]{24}$'")
                conn.execute("ALTER TABLE rounds ADD COLUMN IF NOT EXISTS round_id TEXT")
                conn.execute("UPDATE rounds SET round_id = round_index::text WHERE round_id IS NULL")
                conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS rounds_round_id_idx ON rounds(round_id)")
                conn.execute("ALTER TABLE shadow_trades ADD COLUMN IF NOT EXISTS panel INTEGER NOT NULL DEFAULT 1")
                conn.execute("ALTER TABLE shadow_trades DROP CONSTRAINT IF EXISTS shadow_trades_decision_id_key")
                existing_duplicates = conn.execute("""SELECT 1 FROM shadow_trades
                    GROUP BY session_id,target_round_id,panel HAVING COUNT(*)>1 LIMIT 1""").fetchone()
                if not existing_duplicates:
                    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS shadow_trades_session_round_panel_idx ON shadow_trades(session_id,target_round_id,panel)")
            return
        with self.connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.executescript(SCHEMA)
            columns = {row[1] for row in conn.execute("PRAGMA table_info(rounds)").fetchall()}
            if "round_id" not in columns:
                conn.execute("ALTER TABLE rounds ADD COLUMN round_id TEXT")
            conn.execute("UPDATE rounds SET round_id = CAST(round_index AS TEXT) WHERE round_id IS NULL")
            conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS rounds_round_id_idx ON rounds(round_id)")
            shadow_columns = {row[1] for row in conn.execute("PRAGMA table_info(shadow_trades)").fetchall()}
            if "panel" not in shadow_columns:
                indexes = conn.execute("PRAGMA index_list(shadow_trades)").fetchall()
                decision_unique = any(
                    row[2] and [col[2] for col in conn.execute(f"PRAGMA index_info('{row[1]}')").fetchall()] == ["decision_id"]
                    for row in indexes
                )
                if decision_unique:
                    existing_duplicates = conn.execute("""SELECT 1 FROM shadow_trades
                        GROUP BY session_id,target_round_id HAVING COUNT(*)>1 LIMIT 1""").fetchone()
                    uniqueness = "" if existing_duplicates else ", UNIQUE(session_id,target_round_id,panel)"
                    conn.execute("ALTER TABLE shadow_trades RENAME TO shadow_trades_legacy")
                    conn.execute(f"""CREATE TABLE shadow_trades (
                        execution_id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
                        target_round_id TEXT NOT NULL, decision_id TEXT NOT NULL,
                        panel INTEGER NOT NULL DEFAULT 1, status TEXT NOT NULL,
                        payload TEXT NOT NULL{uniqueness})""")
                    conn.execute("""INSERT INTO shadow_trades(execution_id,session_id,target_round_id,decision_id,panel,status,payload)
                                   SELECT execution_id,session_id,target_round_id,decision_id,1,status,payload FROM shadow_trades_legacy""")
                    conn.execute("DROP TABLE shadow_trades_legacy")
                else:
                    conn.execute("ALTER TABLE shadow_trades ADD COLUMN panel INTEGER NOT NULL DEFAULT 1")
                    duplicates = conn.execute("""SELECT 1 FROM shadow_trades
                        GROUP BY session_id,target_round_id,panel HAVING COUNT(*)>1 LIMIT 1""").fetchone()
                    if not duplicates:
                        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS shadow_trades_session_round_panel_idx ON shadow_trades(session_id,target_round_id,panel)")

    def save_betting_mode(self, mode: str, payload: dict[str, Any]) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO betting_mode_state(singleton,mode,updated_at,payload) VALUES(1,?,?,?) "
                "ON CONFLICT(singleton) DO UPDATE SET mode=excluded.mode, "
                "updated_at=excluded.updated_at,payload=excluded.payload",
                (mode, payload["updated_at"], json.dumps(payload)),
            )

    def load_betting_mode(self) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT mode,updated_at,payload FROM betting_mode_state WHERE singleton=1"
            ).fetchone()
        if not row:
            return None
        return {"mode": row["mode"], "updated_at": row["updated_at"],
                **json.loads(row["payload"])}

    def upsert_rounds(self, rows: list[dict[str, Any]]) -> None:
        if not rows:
            return
        rows = [{**row, "round_id": str(row.get("round_id") or row["round_index"])} for row in rows]
        with self.connect() as conn:
            conn.executemany(
                """
                INSERT INTO rounds(round_id, round_index, multiplier, timestamp, target)
                VALUES(:round_id, :round_index, :multiplier, :timestamp, :target)
                ON CONFLICT(round_id) DO UPDATE SET
                  round_index=excluded.round_index,
                  multiplier=excluded.multiplier,
                  timestamp=excluded.timestamp,
                  target=excluded.target
                """,
                rows,
            )

    @staticmethod
    def _round_rows_to_dicts(rows) -> list[dict[str, Any]]:
        return [{**dict(row), "round_id": str(row["round_id"] or row["round_index"]),
                 "round_index": int(row["round_index"]), "multiplier": float(row["multiplier"]),
                "timestamp": str(row["timestamp"]) if row["timestamp"] is not None else None}
                | {"platform_timestamp": str(row["platform_timestamp"]) if row["platform_timestamp"] is not None else None,
                   "platform_round_id": str(row["platform_round_id"]) if row["platform_round_id"] is not None else None,
                   "platform_round_index": int(row["platform_round_index"]) if row["platform_round_index"] is not None else None,
                   "local_round_index": int(row["local_round_index"]) if row["local_round_index"] is not None else int(row["round_index"]),
                   "observed_at": str(row["observed_at"]) if row["observed_at"] is not None else None,
                   "stored_at": str(row["stored_at"]) if row["stored_at"] is not None else None,
                   "round_identity_type": row["round_identity_type"],
                   "round_index_source": row["round_index_source"],
                   "identity_confidence": row["identity_confidence"],
                   "continuity_verified": bool(row["continuity_verified"]),
                   "gap_before": bool(row["gap_before"]),
                   "continuity_proof": row["continuity_proof"]}
                for row in rows]

    def load_rounds(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT round_id, round_index, multiplier, timestamp, "
                + ("platform_round_id, platform_round_index, local_round_index, platform_timestamp, observed_at, COALESCE(stored_at,created_at) AS stored_at, round_identity_type, round_index_source, identity_confidence, continuity_verified, gap_before, continuity_proof " if self.database_url else "NULL AS platform_round_id, NULL AS platform_round_index, round_index AS local_round_index, NULL AS platform_timestamp, NULL AS observed_at, NULL AS stored_at, 'LEGACY_UNKNOWN' AS round_identity_type, 'LEGACY_UNKNOWN' AS round_index_source, 'UNKNOWN' AS identity_confidence, 0 AS continuity_verified, 0 AS gap_before, 'LEGACY_UNVERIFIED' AS continuity_proof ") + " FROM " +
                ("aviator_rounds" if self.database_url else "rounds") + " ORDER BY round_index ASC"
            ).fetchall()
        return self._round_rows_to_dicts(rows)

    def load_verified_round_suffix(self) -> list[dict[str, Any]]:
        """Load only the latest overlap-verified segment for prospective scoring."""
        if not self.database_url:
            return []
        projection = ("round_id, round_index, multiplier, timestamp, platform_round_id, platform_round_index, "
                      "local_round_index, platform_timestamp, observed_at, COALESCE(stored_at,created_at) AS stored_at, "
                      "round_identity_type, round_index_source, identity_confidence, continuity_verified, gap_before, continuity_proof")
        with self.connect() as conn:
            rows = conn.execute(
                f"""SELECT {projection} FROM aviator_rounds
                    WHERE round_index >= (
                        SELECT COALESCE(MAX(round_index), 0) FROM aviator_rounds
                        WHERE continuity_verified=FALSE OR gap_before=TRUE
                    ) AND continuity_verified=TRUE
                    ORDER BY round_index ASC"""
            ).fetchall()
        return self._round_rows_to_dicts(rows)

    def save_statistics(self, key: str, payload: dict[str, Any]) -> None:
        """Persist a database-derived statistics snapshot for API consumers."""
        with self.connect() as conn:
            conn.execute("""INSERT INTO statistics(key,payload,updated_at) VALUES(?,?,CURRENT_TIMESTAMP)
                ON CONFLICT(key) DO UPDATE SET payload=excluded.payload,updated_at=CURRENT_TIMESTAMP""",
                (str(key), json.dumps(payload, separators=(",", ":"), default=str)))

    def load_statistics(self, key: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT payload FROM statistics WHERE key=?", (str(key),)).fetchone()
        return json.loads(row["payload"]) if row else None

    def save_process_lifecycle_event(self, payload: dict[str, Any]) -> None:
        self.save_process_lifecycle_events([payload])

    def save_process_lifecycle_events(self, events: list[dict[str, Any]]) -> None:
        if not events:
            return
        rows = []
        for payload in events:
            event_key = f"{payload.get('service')}:{payload.get('pid')}:{payload.get('timestamp')}:{payload.get('event')}"
            rows.append((event_key, str(payload.get("event", "unknown")),
                         str(payload.get("timestamp", "")), payload.get("pid"),
                         json.dumps(payload, separators=(",", ":"), default=str)))
        with self.connect() as conn:
            conn.executemany("""INSERT INTO process_lifecycle(event_key,event_type,recorded_at,pid,payload)
                VALUES(?,?,?,?,?) ON CONFLICT(event_key) DO NOTHING""", rows)

    def list_process_lifecycle_events(self, limit: int = 500) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute("SELECT payload FROM process_lifecycle ORDER BY recorded_at DESC LIMIT ?",
                                (min(max(int(limit), 1), 5000),)).fetchall()
        return [json.loads(row["payload"]) for row in reversed(rows)]

    def save_analytics_report(self, report: dict[str, Any]) -> bool:
        """Insert an immutable report snapshot. Existing IDs are never updated."""
        with self.connect() as conn:
            cursor = conn.execute(
                """INSERT INTO analytics_reports(
                       report_id,report_type,period_key,start_time,end_time,first_round,last_round,
                       round_count,data_source,generated_at,generation_time_ms,payload)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(report_id) DO NOTHING""",
                (report["report_id"], report["report_type"], report.get("period_key"),
                 report["start_time"], report["end_time"], report["first_round"],
                 report["last_round"], int(report["round_count"]), report["data_source"],
                 report["generated_at"], report.get("generation_time_ms"),
                 json.dumps(report, separators=(",", ":"))),
            )
            return cursor.rowcount == 1

    def get_analytics_report(self, report_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT payload FROM analytics_reports WHERE report_id=?", (report_id,)).fetchone()
        return json.loads(row["payload"]) if row else None

    def list_analytics_reports(self, report_type: str | None = None, limit: int = 100,
                               offset: int = 0) -> list[dict[str, Any]]:
        limit = min(max(int(limit), 1), 1000)
        offset = max(int(offset), 0)
        with self.connect() as conn:
            if report_type:
                rows = conn.execute("""SELECT payload FROM analytics_reports WHERE report_type=?
                    ORDER BY generated_at DESC,report_id DESC LIMIT ? OFFSET ?""", (report_type, limit, offset)).fetchall()
            else:
                rows = conn.execute("""SELECT payload FROM analytics_reports
                    ORDER BY generated_at DESC,report_id DESC LIMIT ? OFFSET ?""", (limit, offset)).fetchall()
        return [json.loads(row["payload"]) for row in rows]

    def list_analytics_report_periods(self, report_type: str) -> set[str]:
        with self.connect() as conn:
            rows = conn.execute("SELECT period_key FROM analytics_reports WHERE report_type=? AND period_key IS NOT NULL",
                                (report_type,)).fetchall()
        return {str(row["period_key"]) for row in rows}

    def get_analytics_report_by_period(self, report_type: str, period_key: str) -> dict[str, Any] | None:
        """Fetch an immutable report by its deterministic period boundary."""
        with self.connect() as conn:
            row = conn.execute("SELECT payload FROM analytics_reports WHERE report_type=? AND period_key=? LIMIT 1",
                               (report_type, period_key)).fetchone()
        return json.loads(row["payload"]) if row else None

    def latest_round_marker(self) -> tuple | None:
        if not self.database_url:
            return None
        with self.connect() as conn:
            row = conn.execute("""SELECT round_index,round_id,COALESCE(platform_timestamp,timestamp) AS timestamp,multiplier
                                  FROM aviator_rounds ORDER BY round_index DESC LIMIT 1""").fetchone()
        return (int(row["round_index"]), str(row["round_id"]), str(row["timestamp"]), float(row["multiplier"])) if row else None

    def initialize_frozen_observer_queue(self) -> dict[str, Any]:
        """Create the durable queue cursor at the current DB tail once.

        On first deployment, prior targets are deliberately excluded: this
        queue is for future source rounds only and is not a backfill path.
        Subsequent backend/observer restarts restore the existing cursor.
        """
        if not self.database_url:
            return {"available": False, "reason": "PostgreSQL is required"}
        with self.connect() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(hashtext(?))", ("opportunity_v4_observer_queue",))
            row = conn.execute("SELECT last_enqueued_round_index,initialized_at FROM opportunity_v4_observer_queue_control WHERE singleton=1").fetchone()
            if row:
                return {"available": True, "last_enqueued_round_index": int(row["last_enqueued_round_index"]),
                        "initialized_at": self._db_time_text(row["initialized_at"]), "restored": True}
            latest = conn.execute("SELECT round_index FROM aviator_rounds ORDER BY round_index DESC LIMIT 1").fetchone()
            # Include the current live tail once: its next target may still be
            # absent, so it is a valid prospective source. Older rows are
            # skipped and never reconstructed as predictions.
            index = max(0, int(latest["round_index"]) - 1) if latest else 0
            now = conn.execute("SELECT clock_timestamp() AS now").fetchone()["now"]
            now_text = self._db_time_text(now)
            conn.execute("""INSERT INTO opportunity_v4_observer_queue_control
                (singleton,last_enqueued_round_index,initialized_at,updated_at) VALUES(1,?,?,?)""",
                (index, now_text, now_text))
            return {"available": True, "last_enqueued_round_index": index,
                    "initialized_at": now_text, "restored": False}

    def enqueue_frozen_observer_rounds(self, limit: int = 500) -> dict[str, Any]:
        """Append every new PostgreSQL round to the queue in local round order."""
        if not self.database_url:
            return {"enqueued": 0, "last_enqueued_round_index": None}
        batch_limit = min(max(int(limit), 1), 2000)
        with self.connect() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(hashtext(?))", ("opportunity_v4_observer_queue",))
            cursor = conn.execute("SELECT last_enqueued_round_index FROM opportunity_v4_observer_queue_control WHERE singleton=1").fetchone()
            if not cursor:
                return {"enqueued": 0, "last_enqueued_round_index": None, "initialized": False}
            previous = int(cursor["last_enqueued_round_index"])
            rows = conn.execute("""SELECT round_id,round_index,multiplier,observed_at,
                COALESCE(stored_at,created_at) AS stored_at,continuity_verified,gap_before,
                identity_confidence,platform_round_id,platform_round_index
                FROM aviator_rounds WHERE round_index>? ORDER BY round_index ASC LIMIT ?""",
                (previous, batch_limit)).fetchall()
            if not rows:
                return {"enqueued": 0, "last_enqueued_round_index": previous, "initialized": True}
            now = conn.execute("SELECT clock_timestamp() AS now").fetchone()["now"]
            now_text = self._db_time_text(now)
            for row in rows:
                payload = {key: row[key] for key in row.keys()}
                payload["round_index"] = int(payload["round_index"])
                payload["multiplier"] = float(payload["multiplier"])
                payload["continuity_verified"] = bool(payload["continuity_verified"])
                payload["gap_before"] = bool(payload["gap_before"])
                conn.execute("""INSERT INTO opportunity_v4_observer_queue
                    (round_index,round_id,enqueued_at,status,payload) VALUES(?,?,?,'PENDING',?)
                    ON CONFLICT(round_index) DO NOTHING""",
                    (payload["round_index"], str(payload["round_id"]), now_text, json.dumps(payload, default=str)))
            last_index = int(rows[-1]["round_index"])
            conn.execute("""UPDATE opportunity_v4_observer_queue_control SET
                last_enqueued_round_index=?,updated_at=? WHERE singleton=1""", (last_index, now_text))
            return {"enqueued": len(rows), "last_enqueued_round_index": last_index, "initialized": True}

    def frozen_observer_queue_tick(self, limit: int = 500) -> dict[str, Any]:
        """Atomically enqueue, expire stale sources, and return the live head.

        This is the observer hot path. Combining its queue/marker reads in one
        PostgreSQL transaction avoids opening several connections per poll.
        """
        if not self.database_url:
            return {"available": False, "marker": None, "queue_item": None, "stale_items": []}
        batch_limit = min(max(int(limit), 1), 2000)
        with self.connect() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(hashtext(?))", ("opportunity_v4_observer_queue",))
            marker = conn.execute("""SELECT round_index,round_id,
                COALESCE(platform_timestamp,timestamp) AS timestamp,multiplier
                FROM aviator_rounds ORDER BY round_index DESC LIMIT 1""").fetchone()
            control = conn.execute("SELECT last_enqueued_round_index FROM opportunity_v4_observer_queue_control WHERE singleton=1").fetchone()
            if not marker:
                return {"available": True, "marker": None, "queue_item": None, "stale_items": []}
            latest_index = int(marker["round_index"])
            if not control:
                # First deployment begins at the current tail. Historical rows
                # are not added to this prospective source queue.
                start_index = max(0, latest_index - 1)
                now = conn.execute("SELECT clock_timestamp() AS now").fetchone()["now"]
                now_text = self._db_time_text(now)
                conn.execute("""INSERT INTO opportunity_v4_observer_queue_control
                    (singleton,last_enqueued_round_index,initialized_at,updated_at)
                    VALUES(1,?,?,?) ON CONFLICT(singleton) DO NOTHING""",
                    (start_index, now_text, now_text))
                control = conn.execute("SELECT last_enqueued_round_index FROM opportunity_v4_observer_queue_control WHERE singleton=1").fetchone()
            previous = int(control["last_enqueued_round_index"])
            rows = conn.execute("""SELECT round_id,round_index,multiplier,observed_at,
                COALESCE(stored_at,created_at) AS stored_at,continuity_verified,gap_before,
                identity_confidence,platform_round_id,platform_round_index
                FROM aviator_rounds WHERE round_index>? ORDER BY round_index ASC LIMIT ?""",
                (previous, batch_limit)).fetchall()
            now = conn.execute("SELECT clock_timestamp() AS now").fetchone()["now"]
            now_text = self._db_time_text(now)
            for row in rows:
                payload = {key: row[key] for key in row.keys()}
                payload["round_index"] = int(payload["round_index"])
                payload["multiplier"] = float(payload["multiplier"])
                payload["continuity_verified"] = bool(payload["continuity_verified"])
                payload["gap_before"] = bool(payload["gap_before"])
                conn.execute("""INSERT INTO opportunity_v4_observer_queue
                    (round_index,round_id,enqueued_at,status,payload) VALUES(?,?,?,'PENDING',?)
                    ON CONFLICT(round_index) DO NOTHING""",
                    (payload["round_index"], str(payload["round_id"]), now_text, json.dumps(payload, default=str)))
            last_enqueued = int(rows[-1]["round_index"]) if rows else previous
            if last_enqueued > previous:
                conn.execute("UPDATE opportunity_v4_observer_queue_control SET last_enqueued_round_index=?,updated_at=? WHERE singleton=1",
                             (last_enqueued, now_text))
            stale = conn.execute("""UPDATE opportunity_v4_observer_queue
                SET status='MISSED',attempts=attempts+1,
                    reason='TARGET_ARRIVED_BEFORE_ASSESSMENT',processed_at=?
                WHERE status='PENDING' AND round_index<?
                RETURNING round_index,round_id""", (now_text, latest_index)).fetchall()
            head = conn.execute("""SELECT round_index,round_id,status,attempts,payload FROM opportunity_v4_observer_queue
                WHERE status='PENDING' AND round_index<=? ORDER BY round_index ASC LIMIT 1""",
                (latest_index,)).fetchone()
            item = ({**json.loads(head["payload"]), "status": head["status"],
                     "attempts": int(head["attempts"])} if head else None)
            return {
                "available": True,
                "marker": (latest_index, str(marker["round_id"]), self._db_time_text(marker["timestamp"]),
                           float(marker["multiplier"])),
                "queue_item": item,
                "stale_items": [dict(row) for row in stale],
                "enqueued": len(rows),
                "last_enqueued_round_index": last_enqueued,
                "queue_initialized": True,
            }

    def next_frozen_observer_queue_item(self) -> dict[str, Any] | None:
        if not self.database_url:
            return None
        with self.connect() as conn:
            row = conn.execute("""SELECT round_index,round_id,status,attempts,payload
                FROM opportunity_v4_observer_queue WHERE status='PENDING'
                ORDER BY round_index ASC LIMIT 1""").fetchone()
        return ({**json.loads(row["payload"]), "status": row["status"],
                 "attempts": int(row["attempts"])} if row else None)

    def expire_stale_frozen_observer_queue_items(self, latest_round_index: int) -> list[dict[str, Any]]:
        """Mark every queued source whose target already exists, in one DB transaction.

        Queue entries are never replayed after their target arrives. Bulk expiry
        lets the worker reach the current live tail without spending a polling
        interval on each already-late item.
        """
        if not self.database_url:
            return []
        with self.connect() as conn:
            now = conn.execute("SELECT clock_timestamp() AS now").fetchone()["now"]
            now_text = self._db_time_text(now)
            rows = conn.execute("""UPDATE opportunity_v4_observer_queue
                SET status='MISSED',attempts=attempts+1,
                    reason='TARGET_ARRIVED_BEFORE_ASSESSMENT',processed_at=?
                WHERE status='PENDING' AND round_index<?
                RETURNING round_index,round_id""",
                (now_text, int(latest_round_index))).fetchall()
        return [dict(row) for row in rows]

    def finish_frozen_observer_queue_item(self, round_index: int, *, status: str,
                                          assessment_id: str | None = None,
                                          reason: str | None = None) -> dict[str, Any] | None:
        """Acknowledge one source marker without altering experiment history."""
        if not self.database_url:
            return None
        allowed = {"PROCESSED", "MISSED", "RETRY"}
        if status not in allowed:
            raise ValueError("invalid observer queue status")
        with self.connect() as conn:
            now = conn.execute("SELECT clock_timestamp() AS now").fetchone()["now"]
            now_text = self._db_time_text(now)
            if status == "RETRY":
                conn.execute("""UPDATE opportunity_v4_observer_queue SET attempts=attempts+1,
                    reason=?,payload=(payload::jsonb || jsonb_build_object('last_attempt_reason',?))::text
                    WHERE round_index=? AND status='PENDING'""", (reason, reason, int(round_index)))
            else:
                conn.execute("""UPDATE opportunity_v4_observer_queue SET status=?,attempts=attempts+1,
                    assessment_id=?,reason=?,processed_at=? WHERE round_index=? AND status='PENDING'""",
                    (status, assessment_id, reason, now_text, int(round_index)))
            row = conn.execute("SELECT round_index,round_id,status,attempts,reason,processed_at FROM opportunity_v4_observer_queue WHERE round_index=?",
                               (int(round_index),)).fetchone()
        return dict(row) if row else None

    def frozen_observer_queue_snapshot(self) -> dict[str, Any]:
        if not self.database_url:
            return {"available": False, "pending": None, "processed": 0, "missed": 0}
        with self.connect() as conn:
            counts = conn.execute("""SELECT COUNT(*) FILTER(WHERE status='PENDING') AS pending,
                COUNT(*) FILTER(WHERE status='PROCESSED') AS processed,
                COUNT(*) FILTER(WHERE status='MISSED') AS missed,
                MAX(round_index) FILTER(WHERE status='PROCESSED') AS last_processed_round_index,
                MAX(round_index) FILTER(WHERE status='MISSED') AS last_missed_round_index
                FROM opportunity_v4_observer_queue""").fetchone()
            quality = conn.execute("""SELECT COUNT(*) FILTER(WHERE q.status='PROCESSED'
                    AND a.payload::jsonb->>'scorable'='true'
                    AND (a.payload::jsonb->>'opportunity_score') IS NOT NULL) AS scored,
                COUNT(*) FILTER(WHERE q.status='PROCESSED' AND a.assessment_id IS NOT NULL
                    AND a.payload::jsonb->>'scorable' IS DISTINCT FROM 'true') AS invalid
                FROM opportunity_v4_observer_queue q
                LEFT JOIN opportunity_v4_assessments a ON a.assessment_id=q.assessment_id""").fetchone()
            control = conn.execute("SELECT last_enqueued_round_index,initialized_at FROM opportunity_v4_observer_queue_control WHERE singleton=1").fetchone()
            pending = conn.execute("""SELECT round_index,round_id,enqueued_at,attempts,reason FROM opportunity_v4_observer_queue
                WHERE status='PENDING' ORDER BY round_index ASC LIMIT 1""").fetchone()
        return {"available": True, "pending_count": int(counts["pending"] or 0),
                "processed_count": int(counts["processed"] or 0), "missed_count": int(counts["missed"] or 0),
                "post_fix_scored": int(quality["scored"] or 0),
                "post_fix_invalid": int(quality["invalid"] or 0),
                "post_fix_missed": int(counts["missed"] or 0),
                "post_fix_coverage": (float(quality["scored"] or 0) /
                    max(1, int(quality["scored"] or 0) + int(counts["missed"] or 0))),
                "last_processed_round_index": (int(counts["last_processed_round_index"])
                                                if counts["last_processed_round_index"] is not None else None),
                "last_missed_round_index": (int(counts["last_missed_round_index"])
                                             if counts["last_missed_round_index"] is not None else None),
                "last_enqueued_round_index": int(control["last_enqueued_round_index"]) if control else None,
                "initialized_at": self._db_time_text(control["initialized_at"]) if control else None,
                "next_pending": dict(pending) if pending else None}

    def latest_round_live_details(self) -> dict[str, Any] | None:
        """Return the latest persisted real round and its collector proof metadata."""
        if not self.database_url:
            return None
        with self.connect() as conn:
            row = conn.execute("""SELECT round_id,round_index,platform_round_id,platform_round_index,
                                       platform_timestamp,observed_at,COALESCE(stored_at,created_at) AS stored_at,
                                       multiplier,continuity_verified,gap_before,identity_confidence
                                  FROM aviator_rounds ORDER BY round_index DESC LIMIT 1""").fetchone()
        if not row:
            return None
        return {
            "round_id": str(row["round_id"]), "round_index": int(row["round_index"]),
            "platform_round_id": str(row["platform_round_id"]) if row["platform_round_id"] is not None else None,
            "platform_round_index": int(row["platform_round_index"]) if row["platform_round_index"] is not None else None,
            "platform_timestamp": row["platform_timestamp"], "observed_at": row["observed_at"],
            "stored_at": row["stored_at"], "multiplier": float(row["multiplier"]),
            "continuity_verified": bool(row["continuity_verified"]), "gap_before": bool(row["gap_before"]),
            "identity_confidence": row["identity_confidence"],
        }

    def live_round_continuity_snapshot(self, recent_limit: int = 205) -> dict[str, Any]:
        """Read the latest row, recent tail, and verified suffix from one DB snapshot."""
        if not self.database_url:
            return {"latest_round": None, "recent_rounds": [], "verified_suffix": []}
        projection = ("round_id, round_index, multiplier, timestamp, platform_round_id, platform_round_index, "
                     "local_round_index, platform_timestamp, observed_at, COALESCE(stored_at,created_at) AS stored_at, "
                     "round_identity_type, round_index_source, identity_confidence, continuity_verified, gap_before, continuity_proof")
        with self.connect() as conn:
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
            summary = conn.execute("""SELECT COUNT(*) AS row_count,
                MIN(round_index) AS minimum_round_index, MAX(round_index) AS maximum_round_index
                FROM aviator_rounds""").fetchone()
            latest = conn.execute(
                f"SELECT {projection} FROM aviator_rounds ORDER BY round_index DESC LIMIT 1"
            ).fetchone()
            recent_desc = conn.execute(
                f"SELECT {projection} FROM aviator_rounds ORDER BY round_index DESC LIMIT ?",
                (max(2, int(recent_limit)),),
            ).fetchall()
            suffix = conn.execute(
                f"""SELECT {projection} FROM aviator_rounds
                    WHERE round_index >= (
                        SELECT COALESCE(MAX(round_index), 0) FROM aviator_rounds
                        WHERE continuity_verified=FALSE OR gap_before=TRUE
                    ) AND continuity_verified=TRUE
                    ORDER BY round_index ASC"""
            ).fetchall()
        latest_rows = self._round_rows_to_dicts([latest]) if latest else []
        row_count = int(summary["row_count"] or 0) if summary else 0
        minimum_index = (int(summary["minimum_round_index"])
                         if summary and summary["minimum_round_index"] is not None else None)
        maximum_index = (int(summary["maximum_round_index"])
                         if summary and summary["maximum_round_index"] is not None else None)
        return {
            "latest_round": latest_rows[0] if latest_rows else None,
            "recent_rounds": self._round_rows_to_dicts(list(reversed(recent_desc))),
            "verified_suffix": self._round_rows_to_dicts(suffix),
            "round_storage_summary": {
                "row_count": row_count,
                "minimum_round_index": minimum_index,
                "maximum_round_index": maximum_index,
                "max_minus_count": (maximum_index - row_count) if maximum_index is not None else None,
                "index_origin_offset": max(0, minimum_index - 1) if minimum_index is not None else None,
                "missing_indexes_within_range": (max(0, maximum_index - minimum_index + 1 - row_count)
                                                 if minimum_index is not None and maximum_index is not None else None),
            },
        }

    def count_rounds_after(self, round_index: int) -> int:
        table = "aviator_rounds" if self.database_url else "rounds"
        with self.connect() as conn:
            row = conn.execute(f"SELECT COUNT(*) AS n FROM {table} WHERE round_index>?",
                               (int(round_index),)).fetchone()
        return int(row["n"] if row else 0)

    def round_continuity_range(self, first_index: int, last_index: int) -> dict[int, dict[str, Any]]:
        """Return identity and collector proof fields for a bounded round range."""
        if int(last_index) < int(first_index):
            return {}
        if self.database_url:
            query = """SELECT round_index,round_id,COALESCE(stored_at,created_at) AS stored_at,
                             continuity_verified,gap_before,
                             identity_confidence,continuity_proof
                      FROM aviator_rounds WHERE round_index BETWEEN ? AND ? ORDER BY round_index"""
        else:
            query = """SELECT round_index,round_id,NULL AS stored_at,
                             FALSE AS continuity_verified,FALSE AS gap_before,
                             'UNKNOWN' AS identity_confidence,'LEGACY_UNVERIFIED' AS continuity_proof
                      FROM rounds WHERE round_index BETWEEN ? AND ? ORDER BY round_index"""
        with self.connect() as conn:
            rows = conn.execute(query, (int(first_index), int(last_index))).fetchall()
        return {int(row["round_index"]): dict(row) for row in rows}

    def import_legacy_rounds(self, rows: list[dict[str, Any]]) -> int:
        """Import missing legacy rounds once, including when collection began first."""
        if not self.database_url or not rows:
            return 0
        values = [(str(row["round_id"]), int(row["round_index"]), float(row["multiplier"]), row["timestamp"])
                  for row in rows]
        with self.connect() as conn:
            if conn.execute("SELECT 1 FROM application_state WHERE state_key='legacy_rounds_imported'").fetchone():
                return 0
            conn.executemany("""INSERT INTO aviator_rounds(round_id,round_index,multiplier,timestamp,source)
                                VALUES(?,?,?,?,'legacy_import') ON CONFLICT DO NOTHING""", values)
            conn.execute("""INSERT INTO application_state(state_key,payload,updated_at)
                            VALUES('legacy_rounds_imported','{}',CURRENT_TIMESTAMP) ON CONFLICT DO NOTHING""")
        return len(values)

    def save_collector_state(self, state: dict[str, Any]) -> None:
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO collector_state(
                    singleton,collector_state,last_processed_round_id,last_round_timestamp,
                    contiguous_rounds,total_history,warmup_status,collector_session_id,updated_at)
                   VALUES(1,?,?,?,?,?,?,?,?)
                   ON CONFLICT(singleton) DO UPDATE SET
                    collector_state=excluded.collector_state,
                    last_processed_round_id=excluded.last_processed_round_id,
                    last_round_timestamp=excluded.last_round_timestamp,
                    contiguous_rounds=excluded.contiguous_rounds,
                    total_history=excluded.total_history,
                    warmup_status=excluded.warmup_status,
                    collector_session_id=excluded.collector_session_id,
                    updated_at=excluded.updated_at""",
                (state.get("collector_state", "COLLECTING"), state.get("last_processed_round_id"),
                 state.get("last_round_timestamp"), int(state.get("contiguous_rounds", 0)),
                 int(state.get("total_history", 0)), state.get("warmup_status", "WARMING_UP"),
                 state.get("collector_session_id"), state["updated_at"]),
            )

    def load_collector_state(self) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM collector_state WHERE singleton=1").fetchone()
        return dict(row) if row else None

    def rebuild_collector_state(self, *, required_rounds: int = 100,
                                collector_session_id: str | None = None) -> dict[str, Any]:
        """Recompute the latest continuity window from durable rounds."""
        from datetime import datetime, timezone
        rows = self.load_rounds()
        # Round index defines the observed sequence. Sorting by timestamp
        # hides clock inversions and can report a false restored streak.
        rows.sort(key=lambda row: int(row["round_index"]))
        contiguous = 1 if rows and rows[-1].get("continuity_verified") else 0
        for index in range(len(rows) - 1, 0, -1):
            current, previous = rows[index], rows[index - 1]
            if int(previous["round_index"]) + 1 != int(current["round_index"]):
                break
            if (not current.get("continuity_verified") or not previous.get("continuity_verified")
                    or current.get("gap_before")):
                break
            if str(current.get("round_id")) == str(previous.get("round_id")):
                break
            contiguous += 1
        latest = rows[-1] if rows else {}
        state = {
            "collector_state": "COLLECTING" if rows else "WAITING",
            "last_processed_round_id": str(latest.get("round_id")) if latest else None,
            "last_round_timestamp": latest.get("timestamp") if latest else None,
            "contiguous_rounds": contiguous,
            "total_history": len(rows),
            "warmup_status": "READY" if contiguous >= required_rounds else "WARMING_UP",
            "collector_session_id": collector_session_id,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "restored_from_persistence": bool(rows),
        }
        self.save_collector_state(state)
        return state

    def save_readiness_snapshot(self, snapshot: dict[str, Any], *, restored: bool = False) -> None:
        from datetime import datetime, timezone
        updated = snapshot.get("updated_at") or datetime.now(timezone.utc).isoformat()
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO readiness_state(singleton,snapshot,restored_from_persistence,updated_at)
                   VALUES(1,?,?,?) ON CONFLICT(singleton) DO UPDATE SET
                   snapshot=excluded.snapshot, restored_from_persistence=excluded.restored_from_persistence,
                   updated_at=excluded.updated_at""",
                (json.dumps(snapshot), int(restored), updated),
            )

    def load_readiness_snapshot(self) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT snapshot,restored_from_persistence,updated_at FROM readiness_state WHERE singleton=1").fetchone()
        if not row:
            return None
        snapshot = json.loads(row["snapshot"])
        snapshot["restored_from_persistence"] = bool(row["restored_from_persistence"])
        snapshot["persisted_at"] = row["updated_at"]
        return snapshot

    def save_model_candidate(self, payload: dict[str, Any]) -> None:
        from datetime import datetime, timezone
        updated_at = payload.get("updated_at") or payload.get("trained_at") or datetime.now(timezone.utc).isoformat()
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO model_candidates(model_version,model_type,validation_folds,metrics,
                   rejection_reason,deployment_status,next_evaluation_checkpoint,payload,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(model_version) DO UPDATE SET
                   model_type=excluded.model_type,validation_folds=excluded.validation_folds,
                   metrics=excluded.metrics,rejection_reason=excluded.rejection_reason,
                   deployment_status=excluded.deployment_status,next_evaluation_checkpoint=excluded.next_evaluation_checkpoint,
                   payload=excluded.payload,updated_at=excluded.updated_at""",
                (payload["model_version"], payload.get("algorithm", "unknown"),
                 json.dumps(payload.get("validation_folds", payload.get("walk_forward", []))),
                 json.dumps(payload.get("metrics", payload)), payload.get("rejection_reason"),
                 payload.get("deployment_status", "NOT_DEPLOYABLE"), payload.get("next_evaluation_checkpoint"),
                 json.dumps(payload), updated_at),
            )

    def list_model_candidates(self, limit: int = 100) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute("SELECT payload FROM model_candidates ORDER BY updated_at DESC LIMIT ?", (limit,)).fetchall()
        return [json.loads(row["payload"]) for row in rows]

    def save_application_state(self, key: str, payload: dict[str, Any], updated_at: str) -> None:
        with self.connect() as conn:
            conn.execute("""INSERT INTO application_state(state_key,payload,updated_at) VALUES(?,?,?)
                           ON CONFLICT(state_key) DO UPDATE SET payload=excluded.payload,updated_at=excluded.updated_at""",
                         (key, json.dumps(payload), updated_at))

    def save_application_state_once(self, key: str, payload: dict[str, Any], created_at: str) -> dict[str, Any]:
        with self.connect() as conn:
            conn.execute("""INSERT INTO application_state(state_key,payload,updated_at) VALUES(?,?,?)
                           ON CONFLICT(state_key) DO NOTHING""",
                         (key, json.dumps(payload), created_at))
            row = conn.execute("SELECT payload FROM application_state WHERE state_key=?", (key,)).fetchone()
        return json.loads(row["payload"])

    def load_application_state(self, key: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT payload FROM application_state WHERE state_key=?", (key,)).fetchone()
        return json.loads(row["payload"]) if row else None

    def save_patterns(self, patterns: list[dict[str, Any]]) -> None:
        with self.connect() as conn:
            conn.executemany(
                """
                INSERT INTO patterns(pattern_id, payload, updated_at)
                VALUES(:pattern_id, :payload, CURRENT_TIMESTAMP)
                ON CONFLICT(pattern_id) DO UPDATE SET
                  payload=excluded.payload,
                  updated_at=CURRENT_TIMESTAMP
                """,
                [{"pattern_id": p["pattern_id"], "payload": json.dumps(p)} for p in patterns],
            )

    def save_signal(self, signal: dict[str, Any]) -> int:
        with self.connect() as conn:
            values = (
                json.dumps(signal.get("recent_multipliers", [])),
                signal.get("current_pattern", {}).get("label", "unknown"),
                signal.get("final_probability"),
                signal.get("confidence", "LOW"),
                signal.get("status", "NO SIGNAL"),
                json.dumps(signal),
            )
            if self.database_url:
                cur = conn.execute(
                    """
                    INSERT INTO signals(current_sequence, detected_pattern, probability, confidence, status, payload)
                    VALUES(?, ?, ?, ?, ?, ?) RETURNING id
                    """,
                    values,
                )
                row = cur.fetchone()
                return int(row["id"])
            cur = conn.execute(
                """
                INSERT INTO signals(current_sequence, detected_pattern, probability, confidence, status, payload)
                VALUES(?, ?, ?, ?, ?, ?)
                """,
                values,
            )
            return int(cur.lastrowid)

    def list_signals(self, limit: int = 100) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM signals ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [{**dict(r), "payload": json.loads(r["payload"])} for r in rows]

    def save_model_version(self, version: str, model_type: str, path: str | None, metrics: dict[str, Any]) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO model_versions(version, model_type, path, metrics) VALUES(?, ?, ?, ?)",
                (version, model_type, path, json.dumps(metrics)),
            )

    def save_model_artifact(self, version: str, artifact: bytes, sha256: str) -> None:
        """Persist trained model weights in the authoritative database."""
        with self.connect() as conn:
            conn.execute("""INSERT INTO model_artifacts(model_version,sha256,artifact)
                VALUES(?,?,?) ON CONFLICT(model_version) DO UPDATE SET
                sha256=excluded.sha256,artifact=excluded.artifact""",
                (str(version), str(sha256), artifact))

    def load_model_artifact(self, version: str) -> bytes | None:
        with self.connect() as conn:
            row = conn.execute("SELECT artifact FROM model_artifacts WHERE model_version=?",
                               (str(version),)).fetchone()
        return bytes(row["artifact"]) if row else None

    def list_model_versions(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM model_versions ORDER BY id DESC").fetchall()
        return [{**dict(r), "metrics": json.loads(r["metrics"])} for r in rows]

    def save_ml_prediction(self, prediction: dict[str, Any]) -> None:
        with self.connect() as conn:
            if self.database_url:
                conn.execute(
                    """INSERT INTO ml_predictions(prediction_id, source_round_id, created_at, payload)
                       VALUES(?, ?, ?, ?)
                       ON CONFLICT(prediction_id) DO UPDATE SET
                       source_round_id=excluded.source_round_id,
                       created_at=excluded.created_at,
                       payload=excluded.payload""",
                    (prediction["prediction_id"], prediction["source_round_id"],
                     prediction["created_at"], json.dumps(prediction)),
                )
            else:
                conn.execute(
                    "INSERT OR REPLACE INTO ml_predictions(prediction_id, source_round_id, created_at, payload) VALUES(?, ?, ?, ?)",
                    (prediction["prediction_id"], prediction["source_round_id"], prediction["created_at"], json.dumps(prediction)),
                )

    def latest_ml_prediction(self) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT payload FROM ml_predictions ORDER BY created_at DESC LIMIT 1").fetchone()
        return json.loads(row["payload"]) if row else None

    def list_ml_predictions(self, limit: int = 25) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute("SELECT payload FROM ml_predictions ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        return [json.loads(row["payload"]) for row in rows]

    def save_ml_observation(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Record an experimental forecast once, never overwrite it after an outcome."""
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO ml_observations(observation_id,model_version,source_round_id,
                   source_round_index,created_at,status,payload) VALUES(?,?,?,?,?,?,?)
                   ON CONFLICT(model_version,source_round_id) DO NOTHING""",
                (payload["observation_id"], payload["model_version"], payload["source_round_id"],
                 payload["source_round_index"], payload["created_at"], payload["status"], json.dumps(payload)),
            )
            row = conn.execute(
                "SELECT payload FROM ml_observations WHERE model_version=? AND source_round_id=?",
                (payload["model_version"], payload["source_round_id"]),
            ).fetchone()
        return json.loads(row["payload"])

    def list_ml_observations(self, *, pending: bool = False, limit: int = 200,
                             model_version: str | None = None) -> list[dict[str, Any]]:
        clauses, params = [], []
        if pending:
            clauses.append("status='PENDING'")
        if model_version:
            clauses.append("model_version=?")
            params.append(model_version)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self.connect() as conn:
            rows = conn.execute("SELECT payload FROM ml_observations" + where +
                                " ORDER BY created_at DESC LIMIT ?", (*params, limit)).fetchall()
        return [json.loads(row["payload"]) for row in rows]

    def resolve_ml_observation(self, payload: dict[str, Any]) -> None:
        with self.connect() as conn:
            conn.execute("UPDATE ml_observations SET status=?,payload=? WHERE observation_id=? AND status='PENDING'",
                         (payload["status"], json.dumps(payload), payload["observation_id"]))

    def save_opportunity_observation(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Freeze a selective signal/abstention once per model and source round."""
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO opportunity_observations(observation_id,model_version,source_round_id,
                   source_round_index,created_at,status,payload) VALUES(?,?,?,?,?,?,?)
                   ON CONFLICT(model_version,source_round_id) DO NOTHING""",
                (payload["observation_id"], payload["model_version"], payload["source_round_id"],
                 payload["source_round_index"], payload["created_at"], payload["status"], json.dumps(payload)),
            )
            row = conn.execute("SELECT payload FROM opportunity_observations WHERE model_version=? AND source_round_id=?",
                               (payload["model_version"], payload["source_round_id"])).fetchone()
        return json.loads(row["payload"])

    def list_opportunity_observations(self, *, status: str | None = None,
                                      limit: int = 200) -> list[dict[str, Any]]:
        clauses, params = [], []
        if status:
            clauses.append("status=?")
            params.append(status)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self.connect() as conn:
            rows = conn.execute("SELECT payload FROM opportunity_observations" + where +
                                " ORDER BY created_at DESC LIMIT ?", (*params, limit)).fetchall()
        return [json.loads(row["payload"]) for row in rows]

    def resolve_opportunity_observation(self, payload: dict[str, Any]) -> None:
        with self.connect() as conn:
            conn.execute("""UPDATE opportunity_observations SET status=?,payload=?
                           WHERE observation_id=? AND status='PENDING'""",
                         (payload["status"], json.dumps(payload), payload["observation_id"]))

    def save_opportunity_research_gate_once(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self.connect() as conn:
            conn.execute("""INSERT INTO opportunity_research_gates
                (gate_hash,model_version,model_hash,status,created_at,payload)
                VALUES(?,?,?,?,?,?) ON CONFLICT(gate_hash) DO NOTHING""",
                (payload["gate_hash"], payload["model_version"], payload["model_hash"],
                 payload["status"], payload["created_at"], json.dumps(payload)))
            row = conn.execute("SELECT payload FROM opportunity_research_gates WHERE gate_hash=?",
                               (payload["gate_hash"],)).fetchone()
        return json.loads(row["payload"])

    def load_opportunity_research_gate(self, model_version: str, model_hash: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("""SELECT payload FROM opportunity_research_gates
                WHERE model_version=? AND model_hash=? ORDER BY created_at DESC LIMIT 1""",
                (model_version, model_hash)).fetchone()
        return json.loads(row["payload"]) if row else None

    def load_rare_opportunity_selection_policy(self, model_version: str, model_hash: str) -> dict[str, Any] | None:
        if not self.database_url:
            return None
        with self.connect() as conn:
            row = conn.execute("""SELECT payload FROM opportunity_selection_policies
                WHERE model_version=? AND model_hash=? ORDER BY created_at DESC LIMIT 1""",
                (model_version, model_hash)).fetchone()
        return json.loads(row["payload"]) if row else None

    def load_rare_opportunity_policy_development_rows(self, *, model_version: str,
                                                       model_hash: str,
                                                       target_cutoff_round: int) -> list[dict[str, Any]]:
        """Strict pre-outcome frozen assessments resolved before the dev cutoff."""
        if not self.database_url:
            return []
        with self.connect() as conn:
            rows = conn.execute("""SELECT a.payload AS assessment_payload,
                    o.target_round_index,o.target_round_id,o.actual_multiplier,o.resolved_at,o.payload AS outcome_payload
                FROM opportunity_v4_assessments AS a
                JOIN opportunity_v4_outcomes AS o ON o.assessment_id=a.assessment_id
                JOIN aviator_rounds AS target ON target.round_index=o.target_round_index
                    AND target.round_id=o.target_round_id AND target.multiplier=o.actual_multiplier::numeric
                WHERE a.model_version=? AND a.round_index<? AND o.target_round_index<=?
                ORDER BY o.target_round_index ASC""",
                (model_version, int(target_cutoff_round), int(target_cutoff_round))).fetchall()
        output = []
        for row in rows:
            assessment = json.loads(row["assessment_payload"])
            proof = assessment.get("round_order_proof") or {}
            try:
                source_index = int(assessment["round_index"])
                target_index = int(row["target_round_index"])
                score = float(assessment["opportunity_score"])
                multiplier = float(row["actual_multiplier"])
            except (KeyError, TypeError, ValueError):
                continue
            if not (assessment.get("scorable") is True and assessment.get("assessment_immutable") is True
                    and assessment.get("model_configuration_hash") == model_hash
                    and assessment.get("target_round_index") == target_index
                    and target_index == source_index + 1 and target_index <= int(target_cutoff_round)
                    and proof.get("assessment_source_round_index") == source_index
                    and proof.get("assessment_source_round_id") == str(assessment.get("round_id"))
                    and proof.get("target_outcome_round_index") == target_index
                    and proof.get("source_was_latest_at_assessment_commit") is True
                    and proof.get("target_absent_at_assessment_commit") is True
                    and proof.get("proof_method") == "postgres_advisory_transaction_lock"):
                continue
            output.append({"source_round_index": source_index, "target_round_index": target_index,
                           "score": score, "actual_multiplier": multiplier,
                           "is_true": multiplier >= 2.0,
                           "assessment_id": assessment.get("assessment_id"),
                           "created_at": assessment.get("created_at") or assessment.get("observed_at"),
                           "resolved_at": row["resolved_at"]})
        return output

    def save_rare_opportunity_selection_policy_once(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not self.database_url:
            raise RuntimeError("PostgreSQL is required to freeze a prospective selection policy")
        with self.connect() as conn:
            conn.execute("""INSERT INTO opportunity_selection_policies
                (policy_hash,policy_id,model_version,model_hash,status,development_cutoff_round,created_at,payload)
                VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(policy_hash) DO NOTHING""",
                (payload["policy_hash"], payload["policy_id"], payload["model_version"],
                 payload["model_hash"], payload["status"], payload["development_cutoff_round"],
                 payload["created_at"], json.dumps(payload)))
            row = conn.execute("SELECT payload FROM opportunity_selection_policies WHERE policy_hash=?",
                               (payload["policy_hash"],)).fetchone()
        return json.loads(row["payload"])

    def activate_rare_opportunity_selection_policy(self, experiment_id: str,
                                                    policy: dict[str, Any]) -> dict[str, Any] | None:
        """Atomically set the policy boundary after the last already-frozen score."""
        if not self.database_url or policy.get("status") != "FROZEN":
            return None
        with self.connect() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(hashtext(?))", ("aviator_rounds",))
            conn.execute("SELECT pg_advisory_xact_lock(hashtext(?))", ("opportunity_prospective_experiment",))
            row = conn.execute("SELECT payload FROM opportunity_experiments WHERE experiment_id=? AND status='ACTIVE'",
                               (experiment_id,)).fetchone()
            if not row:
                return None
            experiment = json.loads(row["payload"])
            if experiment.get("model_version") != policy.get("model_version") or experiment.get("model_hash") != policy.get("model_hash"):
                return None
            if experiment.get("selection_policy_hash"):
                return experiment
            latest = conn.execute("SELECT round_id,round_index FROM aviator_rounds ORDER BY round_index DESC LIMIT 1").fetchone()
            if not latest:
                return None
            latest_index = int(latest["round_index"])
            existing_source = conn.execute("""SELECT 1 FROM opportunity_v4_assessments
                WHERE model_version=? AND round_id=? LIMIT 1""",
                (experiment["model_version"], str(latest["round_id"]))).fetchone()
            activation_source = latest_index + (1 if existing_source else 0)
            activation_target = activation_source + 1
            now = conn.execute("SELECT clock_timestamp() AS now").fetchone()["now"]
            now_text = self._db_time_text(now)
            experiment.update({"gate_hash": policy["policy_hash"],
                "selection_policy_id": policy["policy_id"], "selection_policy_hash": policy["policy_hash"],
                "selection_policy_status": "FROZEN", "selection_policy_reason": None,
                "selection_policy_activation_at": now_text,
                "policy_activation_source_round_index": activation_source,
                "policy_activation_target_index": activation_target,
                "policy_development_cutoff_round": int(policy["development_cutoff_round"]),
                "max_selections": 4, "four_selection_policy": "DEFINED",
                "target_threshold": 2.0, "updated_at": now_text})
            conn.execute("""UPDATE opportunity_experiments SET gate_hash=?,updated_at=?,payload=?
                WHERE experiment_id=?""",
                (policy["policy_hash"], now_text, json.dumps(experiment), experiment_id))
            return experiment

    def active_opportunity_experiment(self) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("""SELECT payload FROM opportunity_experiments
                WHERE status IN ('ACTIVE','COLLECTING','SCORING','DEGRADED','PAUSED')
                ORDER BY updated_at DESC LIMIT 1""").fetchone()
        return json.loads(row["payload"]) if row else None

    def latest_opportunity_experiment(self) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT payload FROM opportunity_experiments ORDER BY updated_at DESC LIMIT 1").fetchone()
        return json.loads(row["payload"]) if row else None

    def create_opportunity_experiment(self, payload: dict[str, Any]) -> dict[str, Any]:
        payload = dict(payload)
        with self.connect() as conn:
            if self.database_url:
                # Serialize the start boundary against the live collector. The
                # first experiment target is the next round after this locked
                # PostgreSQL tail, never an already observed outcome.
                conn.execute("SELECT pg_advisory_xact_lock(hashtext(?))", ("aviator_rounds",))
                conn.execute("SELECT pg_advisory_xact_lock(hashtext(?))", ("opportunity_prospective_experiment",))
                if payload.pop("start_from_next_round", False):
                    marker = conn.execute("SELECT round_id,round_index FROM aviator_rounds ORDER BY round_index DESC LIMIT 1").fetchone()
                    if not marker:
                        return {"status": "PAUSED", "reason": "no persisted real PostgreSQL round is available"}
                    source_index = int(marker["round_index"])
                    payload.update({
                        "start_source_round_index": source_index,
                        "start_source_round_id": str(marker["round_id"]),
                        "start_round_index": source_index + 1,
                        "first_target_round_index": source_index + 1,
                        "last_round_index": source_index,
                    })
            existing = conn.execute("""SELECT payload FROM opportunity_experiments
                WHERE status IN ('ACTIVE','COLLECTING','SCORING','DEGRADED','PAUSED')
                ORDER BY updated_at DESC LIMIT 1""").fetchone()
            if existing:
                return json.loads(existing["payload"])
            conn.execute("""INSERT INTO opportunity_experiments
                (experiment_id,model_version,model_hash,gate_hash,status,start_round_index,last_round_index,
                 rounds_observed,updated_at,payload) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (payload["experiment_id"], payload["model_version"], payload["model_hash"],
                 payload["gate_hash"], payload["status"], payload["start_round_index"],
                 payload["last_round_index"], payload.get("rounds_observed", 0),
                 payload["updated_at"], json.dumps(payload)))
        return payload

    def update_opportunity_experiment(self, payload: dict[str, Any]) -> None:
        with self.connect() as conn:
            conn.execute("""UPDATE opportunity_experiments SET status=?,last_round_index=?,
                rounds_observed=?,updated_at=?,payload=? WHERE experiment_id=?""",
                (payload["status"], payload["last_round_index"], payload["rounds_observed"],
                 payload["updated_at"], json.dumps(payload), payload["experiment_id"]))

    def opportunity_experiment_targets(self, experiment_id: str) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute("""SELECT payload FROM opportunity_experiment_targets
                WHERE experiment_id=? ORDER BY COALESCE(observed_ordinal,2147483647),target_round_index""",
                (experiment_id,)).fetchall()
        return [json.loads(row["payload"]) for row in rows]

    def opportunity_experiment_predictions(self, experiment_id: str) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute("""SELECT payload FROM opportunity_experiment_predictions
                WHERE experiment_id=? ORDER BY selected_at,target_round_index""", (experiment_id,)).fetchall()
        return [json.loads(row["payload"]) for row in rows]

    def opportunity_experiment_checkpoints(self, experiment_id: str) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute("""SELECT payload FROM opportunity_experiment_checkpoints
                WHERE experiment_id=? ORDER BY checkpoint_rounds""", (experiment_id,)).fetchall()
        return [json.loads(row["payload"]) for row in rows]

    def persist_opportunity_experiment_checkpoint(self, experiment_id: str, report: dict[str, Any]) -> dict[str, Any]:
        with self.connect() as conn:
            conn.execute("""INSERT INTO opportunity_experiment_checkpoints
                (experiment_id,checkpoint_rounds,completed_at,payload) VALUES(?,?,?,?)
                ON CONFLICT(experiment_id,checkpoint_rounds) DO NOTHING""",
                (experiment_id, report["checkpoint_rounds"], report["completed_at"], json.dumps(report)))
            row = conn.execute("""SELECT payload FROM opportunity_experiment_checkpoints
                WHERE experiment_id=? AND checkpoint_rounds=?""",
                (experiment_id, report["checkpoint_rounds"])).fetchone()
        return json.loads(row["payload"])

    def _insert_experiment_target(self, conn, payload: dict[str, Any]) -> None:
        conn.execute("""INSERT INTO opportunity_experiment_targets
            (experiment_id,target_round_index,observed_ordinal,source_round_index,source_round_id,
             target_round_id,assessment_id,classification,selection_state,selection_reason,rank_at_selection,
             selected,prediction_id,model_version,model_hash,gate_hash,feature_snapshot_hash,
             opportunity_score,selection_score,assessment_created_at,selected_at,target_was_absent_at_selection,
             target_observed_at,target_stored_at,target_created_at,actual_multiplier,result,resolved_at,reason,payload)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(experiment_id,target_round_index) DO NOTHING""",
            (payload["experiment_id"], payload["target_round_index"], payload.get("observed_ordinal"),
             payload.get("source_round_index"), payload.get("source_round_id"), payload.get("target_round_id"),
             payload.get("assessment_id"), payload["classification"], payload.get("selection_state"),
             payload.get("selection_reason"), payload.get("rank_at_selection"), bool(payload.get("selected")),
             payload.get("prediction_id"), payload.get("model_version"), payload.get("model_hash"),
             payload.get("gate_hash"), payload.get("feature_snapshot_hash"), payload.get("opportunity_score"),
             payload.get("selection_score"), payload.get("assessment_created_at"), payload.get("selected_at"),
             payload.get("target_was_absent_at_selection"), payload.get("target_observed_at"),
             payload.get("target_stored_at"), payload.get("target_created_at"), payload.get("actual_multiplier"),
             payload.get("result"), payload.get("resolved_at"), payload.get("reason"), json.dumps(payload)))

    def _update_experiment_target(self, conn, payload: dict[str, Any]) -> None:
        conn.execute("""UPDATE opportunity_experiment_targets SET observed_ordinal=?,source_round_index=?,
            source_round_id=?,target_round_id=?,assessment_id=?,classification=?,selection_state=?,
            selection_reason=?,rank_at_selection=?,selected=?,prediction_id=?,model_version=?,model_hash=?,
            gate_hash=?,feature_snapshot_hash=?,opportunity_score=?,selection_score=?,assessment_created_at=?,
            selected_at=?,target_was_absent_at_selection=?,target_observed_at=?,target_stored_at=?,
            target_created_at=?,actual_multiplier=?,result=?,resolved_at=?,reason=?,payload=?
            WHERE experiment_id=? AND target_round_index=?""",
            (payload.get("observed_ordinal"), payload.get("source_round_index"), payload.get("source_round_id"),
             payload.get("target_round_id"), payload.get("assessment_id"), payload["classification"],
             payload.get("selection_state"), payload.get("selection_reason"), payload.get("rank_at_selection"),
             bool(payload.get("selected")), payload.get("prediction_id"), payload.get("model_version"),
             payload.get("model_hash"), payload.get("gate_hash"), payload.get("feature_snapshot_hash"),
             payload.get("opportunity_score"), payload.get("selection_score"), payload.get("assessment_created_at"),
             payload.get("selected_at"), payload.get("target_was_absent_at_selection"), payload.get("target_observed_at"),
             payload.get("target_stored_at"), payload.get("target_created_at"), payload.get("actual_multiplier"),
             payload.get("result"), payload.get("resolved_at"), payload.get("reason"), json.dumps(payload),
             payload["experiment_id"], payload["target_round_index"]))

    def select_latest_preoutcome_assessment_for_experiment(self, experiment_id: str) -> dict[str, Any] | None:
        """Apply a just-frozen research gate to the current next-round assessment under the collector lock."""
        if not self.database_url:
            return None
        with self.connect() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(hashtext(?))", ("aviator_rounds",))
            experiment_row = conn.execute("SELECT payload FROM opportunity_experiments WHERE experiment_id=? AND status='ACTIVE'",
                                          (experiment_id,)).fetchone()
            if not experiment_row:
                return None
            experiment = json.loads(experiment_row["payload"])
            latest = conn.execute("SELECT round_id,round_index FROM aviator_rounds ORDER BY round_index DESC LIMIT 1").fetchone()
            if not latest or int(latest["round_index"]) < int(experiment["start_round_index"]):
                return None
            source_index = int(latest["round_index"])
            target_index = source_index + 1
            if conn.execute("SELECT 1 FROM aviator_rounds WHERE round_index=? LIMIT 1", (target_index,)).fetchone():
                return None
            assessment_row = conn.execute("""SELECT payload FROM opportunity_v4_assessments
                WHERE model_version=? AND round_id=?""",
                (experiment["model_version"], str(latest["round_id"]))).fetchone()
            gate_row = conn.execute("SELECT payload FROM opportunity_research_gates WHERE gate_hash=?",
                                    (experiment["gate_hash"],)).fetchone()
            if not assessment_row or not gate_row:
                return None
            assessment, gate = json.loads(assessment_row["payload"]), json.loads(gate_row["payload"])
            source_proof = assessment.get("round_order_proof") or {}
            if not (assessment.get("scorable") is True and assessment.get("assessment_immutable") is True
                    and int(assessment.get("round_index", -1)) == source_index
                    and int(assessment.get("target_round_index", -1)) == target_index
                    and str(assessment.get("round_id")) == str(latest["round_id"])
                    and assessment.get("model_configuration_hash") == experiment.get("model_hash")
                    and source_proof.get("target_absent_at_assessment_commit") is True
                    and source_proof.get("source_was_latest_at_assessment_commit") is True
                    and source_proof.get("proof_method") == "postgres_advisory_transaction_lock"):
                return None
            if conn.execute("""SELECT 1 FROM opportunity_experiment_targets
                WHERE experiment_id=? AND target_round_index=?""", (experiment_id, target_index)).fetchone():
                return None
            ordinal = int(experiment.get("rounds_observed", 0)) + 1
            window_number = (ordinal - 1) // 100
            selected_count = int(conn.execute("""SELECT COUNT(*) AS n FROM opportunity_experiment_predictions
                WHERE experiment_id=? AND window_number=?""", (experiment_id, window_number)).fetchone()["n"])
            score = float(assessment["opportunity_score"])
            policy = gate.get("policy") or {}
            threshold = float(policy.get("score_threshold", float("inf")))
            can_select = score >= threshold and selected_count < int(policy.get("max_selections_per_100", 4))
            selected_at = conn.execute("SELECT clock_timestamp() AS selected_at").fetchone()["selected_at"]
            selected_at = selected_at.isoformat() if hasattr(selected_at, "isoformat") else str(selected_at)
            reason = ("FROZEN_DEVELOPMENT_TOP_5_PERCENT_CUTOFF_SLOT_AVAILABLE" if can_select else
                      "FROZEN_RESEARCH_CUTOFF_SLOTS_FULL" if score >= threshold else
                      "BELOW_FROZEN_DEVELOPMENT_CUTOFF")
            feature_hash = hashlib.sha256(json.dumps(
                assessment.get("feature_snapshot") or {}, sort_keys=True,
                separators=(",", ":"), default=str).encode()).hexdigest()
            prediction_id = __import__("uuid").uuid4().hex if can_select else None
            decision = {
                "experiment_id": experiment_id, "target_round_index": target_index,
                "observed_ordinal": ordinal, "source_round_index": source_index,
                "source_round_id": str(latest["round_id"]), "target_round_id": None,
                "target_identity_if_known": None, "assessment_id": assessment["assessment_id"],
                "classification": "ELIGIBLE_SCORED", "selection_state": "SELECTED" if can_select else "NO_SIGNAL",
                "selection_reason": reason, "rank_at_selection": selected_count + 1 if can_select else None,
                "selected": bool(can_select), "prediction_id": prediction_id,
                "model_version": experiment["model_version"], "model_hash": experiment["model_hash"],
                "feature_version": assessment.get("feature_version"), "score_version": assessment.get("score_version"),
                "gate_hash": gate["gate_hash"], "feature_snapshot_hash": feature_hash,
                "opportunity_score": score, "selection_score": score,
                "assessment_created_at": assessment.get("created_at") or assessment.get("observed_at"),
                "selected_at": selected_at if can_select else None,
                "target_was_absent_at_selection": True,
                "selection_order_proof": {"latest_source_round_index": source_index,
                                           "latest_source_round_id": str(latest["round_id"]),
                                           "target_round_index": target_index,
                                           "target_absent_at_selection": True,
                                           "proof_method": "postgres_advisory_transaction_lock"},
                "target_observed_at": None, "target_stored_at": None, "target_created_at": None,
                "actual_multiplier": None, "result": "PENDING" if can_select else None,
                "resolved_at": None, "reason": reason,
            }
            if can_select:
                prediction = {**decision, "window_number": window_number, "status": "PENDING"}
                conn.execute("""INSERT INTO opportunity_experiment_predictions
                    (prediction_id,experiment_id,target_round_index,window_number,rank_at_selection,
                     selected_at,status,payload) VALUES(?,?,?,?,?,?,?,?)""",
                    (prediction_id, experiment_id, target_index, window_number, selected_count + 1,
                     selected_at, "PENDING", json.dumps(prediction)))
            self._insert_experiment_target(conn, decision)
            return decision

    @staticmethod
    def _parse_aware_time(value):
        from datetime import datetime, timezone
        if not value:
            return None
        try:
            parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _db_time_text(value):
        return value.isoformat() if hasattr(value, "isoformat") else (str(value) if value is not None else None)

    def sync_active_opportunity_experiment(self) -> dict[str, Any] | None:
        """Append newly arrived authoritative PostgreSQL rounds to one durable experiment."""
        if not self.database_url:
            return None
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        with self.connect() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(hashtext(?))", ("opportunity_prospective_experiment",))
            experiment_row = conn.execute("""SELECT experiment_id,payload FROM opportunity_experiments
                WHERE status IN ('ACTIVE','COLLECTING','SCORING','DEGRADED','PAUSED')
                ORDER BY updated_at DESC LIMIT 1""").fetchone()
            if not experiment_row:
                return None
            experiment = json.loads(experiment_row["payload"])
            last_index = int(experiment.get("last_round_index", experiment["start_round_index"]))
            rows = conn.execute("""SELECT round_id,round_index,multiplier,platform_round_id,
                platform_round_index,platform_timestamp,observed_at,COALESCE(stored_at,created_at) AS stored_at,
                created_at,continuity_verified,gap_before,identity_confidence,continuity_proof
                FROM aviator_rounds WHERE round_index>? ORDER BY round_index ASC""", (last_index,)).fetchall()
            if not rows:
                return experiment
            min_source_index = max(last_index, int(experiment.get("start_source_round_index",
                                                                  experiment.get("start_round_index", last_index))))
            max_source_index = int(rows[-1]["round_index"]) - 1
            if max_source_index >= min_source_index:
                # Only the source assessments for newly observed experiment
                # targets are relevant. Decoding the complete frozen history
                # on every round made experiment sync contend with live scoring.
                assessment_rows = conn.execute("""SELECT payload
                    FROM opportunity_v4_assessments WHERE model_version=? AND round_index>=? AND round_index<=?
                    ORDER BY round_index ASC""",
                    (experiment.get("model_version"), min_source_index, max_source_index)).fetchall()
                assessments = [json.loads(item["payload"]) for item in assessment_rows]
            else:
                assessments = []
            assessment_by_target = {}
            for assessment in assessments:
                try:
                    if (assessment.get("model_version") != experiment.get("model_version")
                            or assessment.get("model_configuration_hash") != experiment.get("model_hash")):
                        continue
                    target_index = int(assessment.get("target_round_index", int(assessment["round_index"]) + 1))
                    if target_index not in assessment_by_target:
                        assessment_by_target[target_index] = assessment
                except (KeyError, TypeError, ValueError):
                    continue
            ordinal = int(experiment.get("rounds_observed", 0))
            previous_index = last_index
            for row in rows:
                index = int(row["round_index"])
                for missing_index in range(previous_index + 1, index):
                    gap = {"experiment_id": experiment["experiment_id"], "target_round_index": missing_index,
                           "observed_ordinal": ordinal + 1, "classification": "GAP", "selection_state": None,
                           "selected": False, "reason": "MISSING_ROUND_INDEX_BETWEEN_PERSISTED_ROUNDS",
                           "target_was_absent_at_selection": None}
                    existing_gap = conn.execute("""SELECT payload FROM opportunity_experiment_targets
                        WHERE experiment_id=? AND target_round_index=?""",
                        (experiment["experiment_id"], missing_index)).fetchone()
                    if existing_gap:
                        old = json.loads(existing_gap["payload"])
                        gap.update({key: value for key, value in old.items() if key not in {"classification", "reason"}})
                        gap["classification"] = "GAP"
                        gap["reason"] = "MISSING_ROUND_INDEX_BETWEEN_PERSISTED_ROUNDS"
                        if gap.get("selected"):
                            gap["result"] = "INVALID"
                            gap["resolved_at"] = now
                        self._update_experiment_target(conn, gap)
                    else:
                        self._insert_experiment_target(conn, gap)
                    prediction_row = conn.execute("""SELECT prediction_id,payload FROM opportunity_experiment_predictions
                        WHERE experiment_id=? AND target_round_index=?""",
                        (experiment["experiment_id"], missing_index)).fetchone()
                    if prediction_row:
                        prediction = json.loads(prediction_row["payload"])
                        prediction.update({"status": "INVALID", "result": "INVALID",
                                          "invalid_reason": "MISSING_TARGET_ROUND_INDEX"})
                        conn.execute("UPDATE opportunity_experiment_predictions SET status='INVALID',result='INVALID',payload=? WHERE prediction_id=?",
                                     (json.dumps(prediction), prediction_row["prediction_id"]))
                ordinal += 1
                assessment = assessment_by_target.get(index)
                source_index = int(assessment.get("round_index", -1)) if assessment else None
                proof = (assessment or {}).get("round_order_proof") or {}
                proof_valid = bool(assessment and assessment.get("assessment_immutable") is True
                                   and assessment.get("scorable") is True
                                   and source_index == index - 1
                                   and str(proof.get("assessment_source_round_id")) == str(assessment.get("round_id"))
                                   and proof.get("assessment_source_round_index") == source_index
                                   and proof.get("target_outcome_round_index") == index
                                   and proof.get("source_was_latest_at_assessment_commit") is True
                                   and proof.get("target_absent_at_assessment_commit") is True
                                   and proof.get("proof_method") == "postgres_advisory_transaction_lock"
                                   and assessment.get("model_configuration_hash") == experiment.get("model_hash")
                    and source_index >= int(experiment.get("start_source_round_index",
                                                          experiment["start_round_index"]))
                                   and bool(row["continuity_verified"]) and not bool(row["gap_before"])
                                   and str(row["identity_confidence"] or "UNKNOWN").upper() not in {"UNKNOWN", "UNVERIFIED"})
                if bool(row["gap_before"]):
                    classification, reason = "GAP", "COLLECTOR_REPORTED_GAP_BEFORE_ROUND"
                elif not bool(row["continuity_verified"]) or str(row["identity_confidence"] or "UNKNOWN").upper() in {"UNKNOWN", "UNVERIFIED"}:
                    classification, reason = "UNVERIFIED", "ROUND_IDENTITY_OR_CONTINUITY_NOT_VERIFIED"
                elif not assessment:
                    classification, reason = "MISSED", "NO_PRE_OUTCOME_ASSESSMENT"
                elif not assessment.get("scorable"):
                    classification, reason = "INVALID", str(assessment.get("failed_gate") or "ASSESSMENT_NOT_SCORABLE")
                elif not proof_valid:
                    classification, reason = "INVALID", "PRE_OUTCOME_ORDER_OR_EXPERIMENT_BOUNDARY_NOT_PROVEN"
                else:
                    classification, reason = "ELIGIBLE_SCORED", None
                existing_row = conn.execute("""SELECT payload FROM opportunity_experiment_targets
                    WHERE experiment_id=? AND target_round_index=?""",
                    (experiment["experiment_id"], index)).fetchone()
                target_payload = json.loads(existing_row["payload"]) if existing_row else {
                    "experiment_id": experiment["experiment_id"], "target_round_index": index,
                    "observed_ordinal": ordinal, "selected": False,
                }
                target_payload.update({
                    "experiment_id": experiment["experiment_id"], "target_round_index": index,
                    "observed_ordinal": target_payload.get("observed_ordinal") or ordinal,
                    "target_round_id": str(row["round_id"]), "target_observed_at": self._db_time_text(row["observed_at"]),
                    "target_stored_at": self._db_time_text(row["stored_at"]),
                    "target_created_at": self._db_time_text(row["created_at"]),
                    "target_identity_if_known": row["platform_round_id"],
                    "target_platform_round_index": row["platform_round_index"],
                    "target_identity_confidence": row["identity_confidence"],
                    "actual_multiplier": float(row["multiplier"]), "classification": classification,
                    "reason": reason, "resolved_at": now if classification == "ELIGIBLE_SCORED" else None,
                })
                if assessment:
                    target_payload.setdefault("source_round_index", source_index)
                    target_payload.setdefault("source_round_id", assessment.get("round_id"))
                    target_payload.setdefault("assessment_id", assessment.get("assessment_id"))
                    target_payload.setdefault("model_version", assessment.get("model_version"))
                    target_payload.setdefault("model_hash", assessment.get("model_configuration_hash"))
                    target_payload.setdefault("opportunity_score", assessment.get("opportunity_score"))
                    target_payload.setdefault("selection_score", assessment.get("opportunity_score"))
                    target_payload.setdefault("assessment_created_at", assessment.get("created_at") or assessment.get("observed_at"))
                selected = bool(target_payload.get("selected"))
                if selected:
                    selected_at = self._parse_aware_time(target_payload.get("selected_at"))
                    outcome_at = self._parse_aware_time(row["observed_at"])
                    timely = bool(proof_valid and target_payload.get("target_was_absent_at_selection") is True
                                  and selected_at is not None
                                  and (not outcome_at or selected_at < outcome_at))
                    if timely:
                        result = "TRUE" if float(row["multiplier"]) >= float(experiment.get("target_threshold", 2.10)) else "FALSE"
                        target_payload.update({"result": result, "resolved_at": now})
                        prediction_row = conn.execute("""SELECT prediction_id,payload FROM opportunity_experiment_predictions
                            WHERE experiment_id=? AND target_round_index=?""",
                            (experiment["experiment_id"], index)).fetchone()
                        if prediction_row:
                            prediction = json.loads(prediction_row["payload"])
                            prediction.update({"actual_multiplier": float(row["multiplier"]),
                                               "result": result, "status": result, "resolved_at": now,
                                               "target_round_id": str(row["round_id"]),
                                               "target_identity_if_known": row["platform_round_id"],
                                               "target_platform_round_index": row["platform_round_index"],
                                               "target_observed_at": self._db_time_text(row["observed_at"]),
                                               "target_stored_at": self._db_time_text(row["stored_at"])})
                            conn.execute("""UPDATE opportunity_experiment_predictions SET status=?,actual_multiplier=?,
                                result=?,resolved_at=?,payload=? WHERE prediction_id=?""",
                                (result, float(row["multiplier"]), result, now, json.dumps(prediction), prediction_row["prediction_id"]))
                    else:
                        target_payload.update({"result": "INVALID", "resolved_at": now,
                                               "reason": "INVALID_LATE_PREDICTION_OR_UNTRUSTED_TIME_ORDER"})
                        prediction_row = conn.execute("""SELECT prediction_id,payload FROM opportunity_experiment_predictions
                            WHERE experiment_id=? AND target_round_index=?""",
                            (experiment["experiment_id"], index)).fetchone()
                        if prediction_row:
                            prediction = json.loads(prediction_row["payload"])
                            prediction.update({"status": "INVALID", "result": "INVALID",
                                               "invalid_reason": target_payload["reason"], "resolved_at": now,
                                               "target_round_id": str(row["round_id"]),
                                               "target_identity_if_known": row["platform_round_id"],
                                               "target_platform_round_index": row["platform_round_index"],
                                               "target_observed_at": self._db_time_text(row["observed_at"]),
                                               "target_stored_at": self._db_time_text(row["stored_at"])})
                            conn.execute("""UPDATE opportunity_experiment_predictions SET status='INVALID',
                                result='INVALID',resolved_at=?,payload=? WHERE prediction_id=?""",
                                (now, json.dumps(prediction), prediction_row["prediction_id"]))
                if existing_row:
                    self._update_experiment_target(conn, target_payload)
                else:
                    self._insert_experiment_target(conn, target_payload)
                previous_index = index
            experiment["last_round_index"] = int(rows[-1]["round_index"])
            experiment["rounds_observed"] = ordinal
            target_rows = conn.execute("SELECT payload FROM opportunity_experiment_targets WHERE experiment_id=?",
                                       (experiment["experiment_id"],)).fetchall()
            target_payloads = [json.loads(item["payload"]) for item in target_rows]
            observed_targets = [item for item in target_payloads if item.get("observed_ordinal") is not None
                                and item.get("classification") != "GAP"]
            predictions = conn.execute("SELECT status,payload FROM opportunity_experiment_predictions WHERE experiment_id=?",
                                       (experiment["experiment_id"],)).fetchall()
            prediction_payloads = [json.loads(item["payload"]) for item in predictions]
            experiment.update({
                "rounds_scored": sum(item.get("classification") == "ELIGIBLE_SCORED" for item in observed_targets),
                "rounds_missed": sum(item.get("classification") == "MISSED" for item in observed_targets),
                "rounds_invalid": sum(item.get("classification") == "INVALID" for item in observed_targets),
                "rounds_gaps": sum(item.get("classification") == "GAP" for item in target_payloads),
                "rounds_unverified": sum(item.get("classification") == "UNVERIFIED" for item in observed_targets),
                "predictions_selected": len(prediction_payloads),
                "predictions_resolved": sum(item.get("status") in {"TRUE", "FALSE"} for item in prediction_payloads),
                "predictions_invalid": sum(item.get("status") == "INVALID" for item in prediction_payloads),
                "true_predictions": sum(item.get("status") == "TRUE" for item in prediction_payloads),
                "false_predictions": sum(item.get("status") == "FALSE" for item in prediction_payloads),
                "pending_predictions": sum(item.get("status") == "PENDING" for item in prediction_payloads),
                "no_signal_count": sum(item.get("selection_state") == "NO_SIGNAL" for item in target_payloads),
                "watch_count": sum(item.get("selection_state") == "WATCH" for item in target_payloads),
                "updated_at": now,
            })
            if ordinal >= 500 and experiment.get("pending_predictions", 0) == 0:
                experiment["status"] = "COMPLETE"
            conn.execute("""UPDATE opportunity_experiments SET status=?,last_round_index=?,
                rounds_observed=?,updated_at=?,payload=? WHERE experiment_id=?""",
                (experiment["status"], experiment["last_round_index"], experiment["rounds_observed"],
                 now, json.dumps(experiment), experiment["experiment_id"]))
            return experiment

    def save_v4_assessment(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        """Atomically insert only while the source remains the latest DB round."""
        stored_payload = dict(payload)
        model_scores = stored_payload.get("model_scores") or {}
        stored_payload.setdefault("model_score", model_scores.get("logistic_regression_probability"))
        stored_payload.setdefault("configuration_hash", stored_payload.get("model_configuration_hash"))
        stored_payload.setdefault("stability_state", (stored_payload.get("stability") or {}).get("state"))
        stored_payload.setdefault("status", "PENDING_RESULT" if stored_payload.get("scorable") else "INVALID")
        with self.connect() as conn:
            if self.database_url:
                # Serialize against the collector's insert lock. This closes
                # the race between a current-history read and assessment
                # persistence: if the target arrived first, we do not write a
                # retrospective assessment that could look prospective.
                conn.execute("SELECT pg_advisory_xact_lock(hashtext(?))", ("aviator_rounds",))
                latest = conn.execute("SELECT round_id,round_index FROM aviator_rounds ORDER BY round_index DESC LIMIT 1").fetchone()
                if (not latest or str(latest["round_id"]) != str(payload["round_id"])
                        or int(latest["round_index"]) != int(payload["round_index"])):
                    return None
                target = conn.execute("SELECT 1 FROM aviator_rounds WHERE round_index=? LIMIT 1",
                                      (int(payload["target_round_index"]),)).fetchone()
                if target:
                    return None
                proof_time = conn.execute("SELECT clock_timestamp() AS proof_time").fetchone()["proof_time"]
                stored_payload["round_order_proof"] = {
                    "assessment_source_round_index": int(payload["round_index"]),
                    "assessment_source_round_id": str(payload["round_id"]),
                    "latest_round_at_assessment_commit_index": int(latest["round_index"]),
                    "latest_round_at_assessment_commit_id": str(latest["round_id"]),
                    "target_outcome_round_index": int(payload["target_round_index"]),
                    "source_was_latest_at_assessment_commit": True,
                    "target_absent_at_assessment_commit": True,
                    "proof_method": "postgres_advisory_transaction_lock",
                    "proof_time": proof_time.isoformat() if hasattr(proof_time, "isoformat") else str(proof_time),
                }
            if stored_payload.get("scorable") and stored_payload.get("opportunity_score") is not None:
                # Freeze the score's rank among up to 100 previous scored records
                # visible at commit time. This is descriptive metadata only and
                # never changes the frozen V3 scoring or the later window rank.
                previous = conn.execute(
                    """SELECT round_id,round_index,
                              payload::jsonb->>'scorable' AS scorable,
                              payload::jsonb->>'opportunity_score' AS opportunity_score,
                              payload::jsonb->>'target_round_index' AS target_round_index,
                              payload::jsonb->>'model_configuration_hash' AS model_hash,
                              payload::jsonb->'round_order_proof'->>'target_absent_at_assessment_commit' AS target_absent,
                              payload::jsonb->'round_order_proof'->>'source_was_latest_at_assessment_commit' AS source_latest,
                              payload::jsonb->'round_order_proof'->>'proof_method' AS proof_method
                       FROM opportunity_v4_assessments
                       WHERE model_version=? AND round_index<?
                       ORDER BY round_index DESC LIMIT 500""",
                    (stored_payload["model_version"], int(stored_payload["round_index"])),
                ).fetchall()
                prior_scores = []
                for row in previous:
                    try:
                        score = row["opportunity_score"]
                        if (row["scorable"] == "true" and score is not None
                                and row["target_absent"] == "true"
                                and row["source_latest"] == "true"
                                and row["proof_method"] == "postgres_advisory_transaction_lock"
                                and row["model_hash"] == stored_payload.get("model_configuration_hash")):
                            source_index = int(row["round_index"])
                            target_index = int(row["target_round_index"] or source_index + 1)
                            prior_scores.append((target_index, float(score)))
                    except (KeyError, TypeError, ValueError):
                        continue
                prior_scores = sorted(prior_scores, key=lambda value: value[0])[-99:]
                score = float(stored_payload["opportunity_score"])
                target_index = int(stored_payload.get("target_round_index", int(stored_payload["round_index"]) + 1))
                stored_payload["rank_at_observation"] = 1 + sum(
                    prior_score > score or (prior_score == score and prior_index < target_index)
                    for prior_index, prior_score in prior_scores
                )
                stored_payload["rank_at_observation_basis"] = "up to 99 prior proof-verified frozen scores; score only"
            insert_cursor = conn.execute(
                """INSERT INTO opportunity_v4_assessments(assessment_id,model_version,round_id,round_index,observed_at,payload)
                   VALUES(?,?,?,?,?,?) ON CONFLICT(model_version,round_id) DO NOTHING""",
                (stored_payload["assessment_id"], stored_payload["model_version"], stored_payload["round_id"],
                 stored_payload["round_index"], stored_payload["observed_at"], json.dumps(stored_payload)),
            )
            row = conn.execute("SELECT payload FROM opportunity_v4_assessments WHERE model_version=? AND round_id=?",
                               (stored_payload["model_version"], stored_payload["round_id"])).fetchone()
            if (insert_cursor.rowcount == 1 and stored_payload.get("scorable")
                    and stored_payload.get("opportunity_score") is not None):
                # Rare-opportunity policy decisions are kept in the experiment
                # ledger, separate from immutable V3 assessment scores.
                experiment_row = conn.execute("""SELECT experiment_id,payload FROM opportunity_experiments
                    WHERE status IN ('ACTIVE','COLLECTING','SCORING','DEGRADED','PAUSED')
                    ORDER BY updated_at DESC LIMIT 1""").fetchone()
                if experiment_row:
                    experiment = json.loads(experiment_row["payload"])
                    policy_row = conn.execute("SELECT payload FROM opportunity_selection_policies WHERE policy_hash=?",
                                              (experiment.get("selection_policy_hash"),)).fetchone()
                    if (policy_row and experiment.get("selection_policy_status") == "FROZEN"
                            and experiment.get("model_hash") == stored_payload.get("model_configuration_hash")):
                        from app.ml.rare_opportunity_policy import RareOpportunitySelectionPolicyV1

                        policy = json.loads(policy_row["payload"])
                        rules = policy.get("selection_rules") or {}
                        target_index = int(stored_payload.get("target_round_index", -1))
                        source_index = int(stored_payload.get("round_index", -1))
                        activation_index = int(experiment.get("policy_activation_target_index", 2**63 - 1))
                        proof = stored_payload.get("round_order_proof") or {}
                        can_decide = bool(stored_payload.get("scorable") is True
                            and target_index == source_index + 1 and target_index >= activation_index
                            and proof.get("assessment_source_round_index") == source_index
                            and proof.get("assessment_source_round_id") == str(stored_payload.get("round_id"))
                            and proof.get("target_outcome_round_index") == target_index
                            and proof.get("target_absent_at_assessment_commit") is True
                            and proof.get("source_was_latest_at_assessment_commit") is True
                            and proof.get("proof_method") == "postgres_advisory_transaction_lock")
                        state, reason = (RareOpportunitySelectionPolicyV1.decide(
                            float(stored_payload["opportunity_score"]), policy) if can_decide
                            else ("NO_SIGNAL", "ASSESSMENT_NOT_ELIGIBLE_FOR_PROSPECTIVE_SELECTION"))
                        selected_count = int(conn.execute("""SELECT COUNT(*) AS n FROM opportunity_experiment_predictions
                            WHERE experiment_id=?""", (experiment["experiment_id"],)).fetchone()["n"])
                        selected = bool(can_decide and state == "SELECTED" and selected_count < 4)
                        if state == "SELECTED" and not selected:
                            state, reason = "NO_SIGNAL", "MAXIMUM_FOUR_SELECTIONS_ALREADY_USED"
                        ordinal = int(experiment.get("rounds_observed", 0)) + 1
                        score = float(stored_payload["opportunity_score"])
                        selected_at = None
                        if selected:
                            stamp = (conn.execute("SELECT clock_timestamp() AS selected_at").fetchone()["selected_at"]
                                     if self.database_url else datetime.now(timezone.utc))
                            selected_at = self._db_time_text(stamp)
                        feature_hash = hashlib.sha256(json.dumps(
                            stored_payload.get("feature_snapshot") or {}, sort_keys=True,
                            separators=(",", ":"), default=str).encode()).hexdigest()
                        existing = conn.execute("""SELECT 1 FROM opportunity_experiment_targets
                            WHERE experiment_id=? AND target_round_index=?""",
                            (experiment["experiment_id"], target_index)).fetchone()
                        if not existing:
                            decision = {
                                "experiment_id": experiment["experiment_id"], "target_round_index": target_index,
                                "observed_ordinal": ordinal, "source_round_index": source_index,
                                "source_round_id": str(stored_payload["round_id"]), "target_round_id": None,
                                "assessment_id": stored_payload["assessment_id"],
                                "classification": "ELIGIBLE_SCORED", "selection_state": state,
                                "selection_reason": reason, "rank_at_selection": selected_count + 1 if selected else None,
                                "selected": selected, "prediction_id": None,
                                "model_version": experiment["model_version"], "model_hash": experiment["model_hash"],
                                "feature_version": stored_payload.get("feature_version"),
                                "score_version": stored_payload.get("score_version"),
                                "gate_hash": policy["policy_hash"], "policy_id": policy["policy_id"],
                                "feature_snapshot_hash": feature_hash, "opportunity_score": score,
                                "selection_score": score,
                                "assessment_created_at": stored_payload.get("created_at") or stored_payload.get("observed_at"),
                                "selected_at": selected_at, "target_was_absent_at_selection": bool(can_decide),
                                "selection_order_proof": {"source_round_index": source_index,
                                    "source_round_id": str(stored_payload["round_id"]),
                                    "target_round_index": target_index, "target_absent_at_selection": True,
                                    "proof_method": "postgres_advisory_transaction_lock"},
                                "actual_multiplier": None, "result": "PENDING" if selected else None,
                                "resolved_at": None, "reason": reason,
                            }
                            if selected:
                                prediction_id = __import__("uuid").uuid4().hex
                                decision["prediction_id"] = prediction_id
                                prediction = {**decision, "window_number": 0, "status": "PENDING", "selected": True}
                                conn.execute("""INSERT INTO opportunity_experiment_predictions
                                    (prediction_id,experiment_id,target_round_index,window_number,rank_at_selection,
                                     selected_at,status,payload) VALUES(?,?,?,?,?,?,?,?)
                                    ON CONFLICT(experiment_id,target_round_index) DO NOTHING""",
                                    (prediction_id, experiment["experiment_id"], target_index, 0,
                                     decision["rank_at_selection"], selected_at, "PENDING", json.dumps(prediction)))
                            self._insert_experiment_target(conn, decision)
        return json.loads(row["payload"])

    def list_v4_assessments(self, limit: int = 100000) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute("SELECT payload FROM opportunity_v4_assessments ORDER BY round_index ASC LIMIT ?",
                                (min(max(int(limit), 1), 500000),)).fetchall()
        return [json.loads(row["payload"]) for row in rows]

    def list_unresolved_v4_assessments(self, first_source_round_index: int,
                                       last_source_round_index: int) -> list[dict[str, Any]]:
        """Load only pending assessments whose outcomes can exist in a verified suffix."""
        first_index = int(first_source_round_index)
        last_index = int(last_source_round_index)
        if last_index < first_index:
            return []
        with self.connect() as conn:
            rows = conn.execute(
                """SELECT a.payload FROM opportunity_v4_assessments AS a
                   LEFT JOIN opportunity_v4_outcomes AS o ON o.assessment_id=a.assessment_id
                   WHERE a.model_version=? AND a.round_index>=? AND a.round_index<=?
                     AND o.assessment_id IS NULL
                   ORDER BY a.round_index ASC""",
                ("V3_FROZEN_2026-10-03", first_index, last_index),
            ).fetchall()
        return [json.loads(row["payload"]) for row in rows]

    def get_v4_assessment(self, round_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT payload FROM opportunity_v4_assessments WHERE model_version=? AND round_id=?",
                               ("V3_FROZEN_2026-10-03", str(round_id))).fetchone()
        return json.loads(row["payload"]) if row else None

    def latest_v4_assessment(self, model_version: str) -> dict[str, Any] | None:
        """Read one indexed latest assessment for low-latency live status."""
        with self.connect() as conn:
            row = conn.execute("""SELECT payload FROM opportunity_v4_assessments
                WHERE model_version=? ORDER BY round_index DESC LIMIT 1""",
                (str(model_version),)).fetchone()
        return json.loads(row["payload"]) if row else None

    def save_v4_outcome_once(self, payload: dict[str, Any]) -> None:
        """Append target outcome separately from its frozen assessment."""
        stored_payload = dict(payload)
        result = bool(stored_payload.get("is_true_2_10x", stored_payload.get("target_hit_2_10x")))
        stored_payload["is_true_2_10x"] = result
        stored_payload["target_hit_2_10x"] = result
        stored_payload.setdefault("status", "TRUE" if result else "FALSE")
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO opportunity_v4_outcomes(assessment_id,target_round_id,target_round_index,
                   actual_multiplier,target_hit_2_10x,resolved_at,payload) VALUES(?,?,?,?,?,?,?)
                   ON CONFLICT(assessment_id) DO NOTHING""",
                (stored_payload["assessment_id"], stored_payload["target_round_id"], stored_payload["target_round_index"],
                 stored_payload["actual_multiplier"], int(result), stored_payload["resolved_at"], json.dumps(stored_payload)),
            )

    def list_v4_outcomes(self, limit: int = 100000) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute("SELECT payload FROM opportunity_v4_outcomes ORDER BY target_round_index ASC LIMIT ?",
                                (min(max(int(limit), 1), 500000),)).fetchall()
        return [json.loads(row["payload"]) for row in rows]

    def save_evidence_snapshot(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        """Persist once per prediction; an existing snapshot is never replaced."""
        with self.connect() as conn:
            conn.execute(
                """INSERT OR IGNORE INTO prediction_evidence
                   (evidence_id, prediction_id, source_round_id, calculated_at, payload)
                   VALUES(?, ?, ?, ?, ?)""",
                (snapshot["evidence_id"], snapshot["prediction_id"], snapshot["source_round_id"],
                 snapshot["calculated_at"], json.dumps(snapshot)),
            )
            row = conn.execute("SELECT payload FROM prediction_evidence WHERE prediction_id = ?",
                               (snapshot["prediction_id"],)).fetchone()
        return json.loads(row["payload"])

    def evidence_for_prediction(self, prediction_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT payload FROM prediction_evidence WHERE prediction_id = ?",
                               (prediction_id,)).fetchone()
        return json.loads(row["payload"]) if row else None

    def latest_evidence(self) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT payload FROM prediction_evidence ORDER BY calculated_at DESC LIMIT 1").fetchone()
        return json.loads(row["payload"]) if row else None

    def list_evidence(self, limit: int = 25) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute("SELECT payload FROM prediction_evidence ORDER BY calculated_at DESC LIMIT ?",
                                (limit,)).fetchall()
        return [json.loads(row["payload"]) for row in rows]

    def save_decision(self, decision: dict[str, Any]) -> None:
        """Update the current record and append an immutable lifecycle audit row."""
        payload = json.dumps(decision)
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO decision_records(decision_id, idempotency_key, created_at, updated_at, payload)
                   VALUES(?, ?, ?, ?, ?)
                   ON CONFLICT(decision_id) DO UPDATE SET updated_at=excluded.updated_at, payload=excluded.payload""",
                (decision["decision_id"], decision["idempotency_key"], decision["created_at"],
                 decision["updated_at"], payload),
            )
            conn.execute(
                "INSERT INTO decision_audit(decision_id, status, recorded_at, payload) VALUES(?, ?, ?, ?)",
                (decision["decision_id"], decision["status"], decision["updated_at"], payload),
            )

    def decision_by_key(self, key: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT payload FROM decision_records WHERE idempotency_key = ?", (key,)).fetchone()
        return json.loads(row["payload"]) if row else None

    def latest_decision(self) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT payload FROM decision_records ORDER BY updated_at DESC LIMIT 1").fetchone()
        return json.loads(row["payload"]) if row else None

    def list_decisions(self, limit: int = 25) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute("SELECT payload FROM decision_records ORDER BY updated_at DESC LIMIT ?", (limit,)).fetchall()
        return [json.loads(row["payload"]) for row in rows]

    def decision_audit(self, decision_id: str) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute("SELECT payload FROM decision_audit WHERE decision_id = ? ORDER BY id", (decision_id,)).fetchall()
        return [json.loads(row["payload"]) for row in rows]

    def create_execution(self, execution: dict[str, Any]) -> bool:
        """Atomically reserve decision+round. False means already reserved."""
        with self.connect() as conn:
            cur = conn.execute(
                """INSERT OR IGNORE INTO bet_executions(
                     execution_id, decision_id, target_round_id, profile,
                     bet_amount, cashout_target, status, simulated, created_at,
                     updated_at, balance_before, payload)
                   VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (execution["execution_id"], execution["decision_id"],
                 execution["target_round_id"], execution["profile"],
                 execution["bet_amount"], execution["cashout_target"],
                 execution["status"], int(bool(execution.get("simulated"))),
                 execution["created_at"], execution["updated_at"],
                 execution.get("balance_before"), json.dumps(execution)),
            )
            return cur.rowcount == 1

    def update_execution(self, execution: dict[str, Any]) -> None:
        with self.connect() as conn:
            conn.execute(
                """UPDATE bet_executions SET status=?, updated_at=?, placed_at=?,
                     resolved_at=?, result_multiplier=?, balance_before=?,
                     balance_after=?, profit_loss=?, error=?, payload=?
                   WHERE execution_id=?""",
                (execution["status"], execution["updated_at"], execution.get("placed_at"),
                 execution.get("resolved_at"), execution.get("result_multiplier"),
                 execution.get("balance_before"), execution.get("balance_after"),
                 execution.get("profit_loss"), execution.get("error"),
                 json.dumps(execution), execution["execution_id"]),
            )

    def execution_by_id(self, execution_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT payload FROM bet_executions WHERE execution_id=?",
                               (execution_id,)).fetchone()
        return json.loads(row["payload"]) if row else None

    def open_executions(self) -> list[dict[str, Any]]:
        terminal = ("RECONCILED", "CLOSED", "FAILED", "EXPIRED", "REJECTED")
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT payload FROM bet_executions WHERE status NOT IN (?,?,?,?,?) ORDER BY created_at",
                terminal,
            ).fetchall()
        return [json.loads(row["payload"]) for row in rows]

    def append_execution_event(self, execution_id: str, state: str,
                               recorded_at: str, payload: dict[str, Any]) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO execution_events(execution_id,state,recorded_at,payload) VALUES(?,?,?,?)",
                (execution_id, state, recorded_at, json.dumps(payload)),
            )

    def execution_events(self, execution_id: str) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT state,recorded_at,payload FROM execution_events WHERE execution_id=? ORDER BY id",
                (execution_id,),
            ).fetchall()
        return [{**json.loads(row["payload"]), "state": row["state"],
                 "recorded_at": row["recorded_at"]} for row in rows]

    def append_ledger(self, row: dict[str, Any]) -> bool:
        with self.connect() as conn:
            cur = conn.execute(
                """INSERT OR IGNORE INTO balance_ledger(
                     ledger_id,execution_id,session_id,event_type,amount,
                     internal_expected_balance,platform_observed_balance,
                     reconciled_balance,balance_status,recorded_at,payload)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (row["ledger_id"], row.get("execution_id"), row.get("session_id"),
                 row["event_type"], row.get("amount"), row.get("internal_expected_balance"),
                 row.get("platform_observed_balance"), row.get("reconciled_balance"),
                 row["balance_status"], row["recorded_at"], json.dumps(row)),
            )
            return cur.rowcount == 1

    def list_ledger(self, limit: int = 100) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute("SELECT payload FROM balance_ledger ORDER BY recorded_at DESC LIMIT ?",
                                (limit,)).fetchall()
        return [json.loads(row["payload"]) for row in rows]

    def save_reconciliation(self, row: dict[str, Any], execution: dict[str, Any],
                            ledger_rows: list[dict[str, Any]]) -> bool:
        """Commit result, immutable reconciliation, and financial entries together."""
        with self.connect() as conn:
            cur = conn.execute(
                """INSERT OR IGNORE INTO reconciliation_records(
                     reconciliation_id,execution_id,round_id,status,outcome,created_at,payload)
                   VALUES(?,?,?,?,?,?,?)""",
                (row["reconciliation_id"], row["execution_id"], row["round_id"],
                 row["status"], row["outcome"], row["created_at"], json.dumps(row)),
            )
            if cur.rowcount != 1:
                return False
            conn.execute(
                """UPDATE bet_executions SET status=?,updated_at=?,resolved_at=?,
                     result_multiplier=?,balance_after=?,profit_loss=?,error=?,payload=?
                   WHERE execution_id=?""",
                (execution["status"], execution["updated_at"], execution.get("resolved_at"),
                 execution.get("result_multiplier"), execution.get("balance_after"),
                 execution.get("profit_loss"), execution.get("error"),
                 json.dumps(execution), execution["execution_id"]),
            )
            for item in ledger_rows:
                conn.execute(
                    """INSERT OR IGNORE INTO balance_ledger(
                         ledger_id,execution_id,session_id,event_type,amount,
                         internal_expected_balance,platform_observed_balance,
                         reconciled_balance,balance_status,recorded_at,payload)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (item["ledger_id"], item.get("execution_id"), item.get("session_id"),
                     item["event_type"], item.get("amount"), item.get("internal_expected_balance"),
                     item.get("platform_observed_balance"), item.get("reconciled_balance"),
                     item["balance_status"], item["recorded_at"], json.dumps(item)),
                )
            conn.execute(
                "INSERT INTO system_audit_log(event_type,entity_id,recorded_at,payload) VALUES(?,?,?,?)",
                ("RECONCILIATION", execution["execution_id"], row["created_at"], json.dumps(row)),
            )
        return True

    def list_reconciliations(self, limit: int = 100) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute("SELECT payload FROM reconciliation_records ORDER BY created_at DESC LIMIT ?",
                                (limit,)).fetchall()
        return [json.loads(row["payload"]) for row in rows]

    def save_prediction_evaluation(self, row: dict[str, Any]) -> bool:
        with self.connect() as conn:
            cur = conn.execute(
                """INSERT OR IGNORE INTO prediction_evaluations(
                     evaluation_id,prediction_id,round_id,original_probability,
                     actual_multiplier,actual_target,evaluation_result,evaluated_at,prediction_snapshot)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (row["evaluation_id"], row["prediction_id"], row["round_id"],
                 row["original_probability"], row["actual_multiplier"],
                 int(row["actual_target"]), row["evaluation_result"], row["evaluated_at"],
                 json.dumps(row["prediction_snapshot"])),
            )
            return cur.rowcount == 1

    def list_prediction_evaluations(self, limit: int = 100) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM prediction_evaluations ORDER BY evaluated_at DESC LIMIT ?", (limit,),
            ).fetchall()
        return [{**dict(row), "prediction_snapshot": json.loads(row["prediction_snapshot"])} for row in rows]

    def save_session_metrics(self, session_id: str, updated_at: str, payload: dict[str, Any]) -> None:
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO session_metrics(session_id,updated_at,payload) VALUES(?,?,?)
                   ON CONFLICT(session_id) DO UPDATE SET updated_at=excluded.updated_at,payload=excluded.payload""",
                (session_id, updated_at, json.dumps(payload)),
            )

    def list_session_metrics(self, limit: int = 50) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute("SELECT payload FROM session_metrics ORDER BY updated_at DESC LIMIT ?",
                                (limit,)).fetchall()
        return [json.loads(row["payload"]) for row in rows]

    def execution_for(self, decision_id: str, target_round_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT payload FROM bet_executions WHERE decision_id=? AND target_round_id=?",
                (decision_id, target_round_id),
            ).fetchone()
        return json.loads(row["payload"]) if row else None

    def list_executions(self, limit: int = 50) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT payload FROM bet_executions ORDER BY created_at DESC LIMIT ?", (limit,),
            ).fetchall()
        return [json.loads(row["payload"]) for row in rows]

    def save_live_session(self, row: dict[str, Any]) -> bool:
        with self.connect() as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO live_sessions(session_id,started_at,ended_at,mode,snapshot,payload) VALUES(?,?,?,?,?,?)",
                (row["session_id"], row["started_at"], row.get("ended_at"), row.get("mode", "LIVE_ACTIVE"),
                 json.dumps(row.get("snapshot", {})), json.dumps(row)),
            )
            return cur.rowcount == 1

    def update_live_session(self, row: dict[str, Any]) -> None:
        with self.connect() as conn:
            conn.execute("UPDATE live_sessions SET ended_at=?,mode=?,payload=? WHERE session_id=?",
                         (row.get("ended_at"), row.get("mode"), json.dumps(row), row["session_id"]))

    def list_live_sessions(self, limit: int = 50) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute("SELECT payload FROM live_sessions ORDER BY started_at DESC LIMIT ?", (limit,)).fetchall()
        return [json.loads(row["payload"]) for row in rows]

    def list_audit_log(self, limit: int = 100) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute("SELECT event_type,entity_id,recorded_at,payload FROM system_audit_log ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [{**dict(row), "payload": json.loads(row["payload"])} for row in rows]

    def reconcile_incomplete_executions(self) -> int:
        """Never retry an ambiguous placed operation after process restart."""
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        terminal = ("RECONCILED", "CLOSED", "FAILED", "EXPIRED", "REJECTED")
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT execution_id, payload FROM bet_executions WHERE status NOT IN (?,?,?,?,?)",
                terminal,
            ).fetchall()
            for row in rows:
                payload = json.loads(row["payload"])
                payload.update(status="UNKNOWN", lifecycle_state="UNKNOWN", updated_at=now,
                               error="restart_reconciliation_required")
                conn.execute(
                    "UPDATE bet_executions SET status='UNKNOWN', updated_at=?, error=?, payload=? WHERE execution_id=?",
                    (now, payload["error"], json.dumps(payload), row["execution_id"]),
                )
        return len(rows)

    def save_backtest(self, payload: dict[str, Any]) -> bool:
        with self.connect() as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO backtest_runs(run_id,created_at,dataset_hash,config,payload) VALUES(?,?,?,?,?)",
                (payload["run_id"], payload["created_at"], payload["dataset"]["sha256"],
                 json.dumps(payload["config"]), json.dumps(payload)),
            )
            return cur.rowcount == 1

    def list_backtests(self, limit: int = 50) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute("SELECT payload FROM backtest_runs ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        return [json.loads(row["payload"]) for row in rows]

    def backtest_by_id(self, run_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT payload FROM backtest_runs WHERE run_id=?", (run_id,)).fetchone()
        return json.loads(row["payload"]) if row else None

    def save_research(self, payload: dict[str, Any]) -> bool:
        with self.connect() as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO research_runs(research_id,created_at,dataset_hash,config,payload) VALUES(?,?,?,?,?)",
                (payload["research_id"], payload["created_at"], payload["dataset"]["sha256"],
                 json.dumps(payload["config"]), json.dumps(payload)),
            )
            return cur.rowcount == 1

    def list_research(self, limit: int = 25) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute("SELECT payload FROM research_runs ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        return [json.loads(row["payload"]) for row in rows]

    def research_by_id(self, research_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT payload FROM research_runs WHERE research_id=?", (research_id,)).fetchone()
        return json.loads(row["payload"]) if row else None

    def save_shadow_session(self, payload: dict[str, Any]) -> None:
        with self.connect() as conn:
            conn.execute("INSERT INTO shadow_sessions(session_id,started_at,payload) VALUES(?,?,?) ON CONFLICT(session_id) DO UPDATE SET payload=excluded.payload",
                         (payload["session_id"], payload.get("started_at", ""), json.dumps(payload)))

    def list_shadow_sessions(self, limit: int = 50) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute("SELECT payload FROM shadow_sessions ORDER BY started_at DESC LIMIT ?", (limit,)).fetchall()
        return [json.loads(row["payload"]) for row in rows]

    def shadow_session(self, session_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT payload FROM shadow_sessions WHERE session_id=?", (session_id,)).fetchone()
        return json.loads(row["payload"]) if row else None

    def save_shadow_trade(self, payload: dict[str, Any]) -> None:
        with self.connect() as conn:
            conn.execute("INSERT INTO shadow_trades(execution_id,session_id,target_round_id,decision_id,panel,status,payload) VALUES(?,?,?,?,?,?,?) ON CONFLICT(execution_id) DO UPDATE SET status=excluded.status,payload=excluded.payload",
                         (payload["execution_id"], payload["session_id"], str(payload["target_round_id"]), payload["decision_id"], int(payload.get("panel", 1)), payload["status"], json.dumps(payload)))

    def list_shadow_trades(self, session_id: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        with self.connect() as conn:
            order_column = "execution_id" if self.database_url else "rowid"
            if session_id:
                rows = conn.execute(f"SELECT payload FROM shadow_trades WHERE session_id=? ORDER BY {order_column} DESC LIMIT ?", (session_id, limit)).fetchall()
            else:
                rows = conn.execute(f"SELECT payload FROM shadow_trades ORDER BY {order_column} DESC LIMIT ?", (limit,)).fetchall()
        return [json.loads(row["payload"]) for row in rows]

    def append_shadow_ledger(self, session_id: str, event_type: str, amount: float, balance: float) -> None:
        from datetime import datetime, timezone
        payload = {"session_id": session_id, "event_type": event_type, "amount": amount, "balance": balance,
                   "recorded_at": datetime.now(timezone.utc).isoformat(), "mode": "SHADOW"}
        with self.connect() as conn:
            conn.execute("INSERT INTO shadow_ledger(session_id,event_type,amount,balance,recorded_at,payload) VALUES(?,?,?,?,?,?)",
                         (session_id, event_type, amount, balance, payload["recorded_at"], json.dumps(payload)))

    def list_shadow_ledger(self, session_id: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
        with self.connect() as conn:
            query = "SELECT payload FROM shadow_ledger ORDER BY id DESC LIMIT ?" if not session_id else "SELECT payload FROM shadow_ledger WHERE session_id=? ORDER BY id DESC LIMIT ?"
            rows = conn.execute(query, (limit,) if not session_id else (session_id, limit)).fetchall()
        return [json.loads(row["payload"]) for row in rows]
