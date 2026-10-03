from __future__ import annotations

import json
import os
import re
import sqlite3
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

    def load_rounds(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT round_id, round_index, multiplier, timestamp FROM " +
                ("aviator_rounds" if self.database_url else "rounds") + " ORDER BY round_index ASC"
            ).fetchall()
        return [{**dict(row), "round_id": str(row["round_id"] or row["round_index"]),
                 "round_index": int(row["round_index"]), "multiplier": float(row["multiplier"]),
                "timestamp": str(row["timestamp"]) if row["timestamp"] is not None else None}
                for row in rows]

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
            row = conn.execute("""SELECT round_index,round_id,timestamp,multiplier
                                  FROM aviator_rounds ORDER BY round_index DESC LIMIT 1""").fetchone()
        return (int(row["round_index"]), str(row["round_id"]), str(row["timestamp"]), float(row["multiplier"])) if row else None

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
        contiguous = 0
        if rows:
            contiguous = 1
        for index in range(len(rows) - 1, 0, -1):
            current, previous = rows[index], rows[index - 1]
            if int(previous["round_index"]) + 1 != int(current["round_index"]):
                break
            if current.get("timestamp") and previous.get("timestamp"):
                try:
                    delta = (datetime.fromisoformat(str(current["timestamp"]).replace("Z", "+00:00")) -
                             datetime.fromisoformat(str(previous["timestamp"]).replace("Z", "+00:00"))).total_seconds()
                    if delta < 0 or delta > 120:
                        break
                except ValueError:
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
