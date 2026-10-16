from __future__ import annotations

import json
import hashlib
import os
import re
import sqlite3
import uuid
from contextlib import nullcontext
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

MAX_COLLECTOR_EVENT_ATTEMPTS = 6
_DEMO_SESSION_VOLATILE_FIELDS = {
    "context", "decisions", "latest_decision", "pending_selections",
    "rule_target_observations", "cursor_sequence", "last_round_index",
    "last_round_id", "latest_game_phase", "latest_panel_observations",
    "engine_states", "stream_id", "buffer_epoch_id",
}


def _demo_session_storage_payload(session: dict[str, Any]) -> str:
    # Isolated nonfinancial test sessions persist their configuration and
    # lifecycle; volatile round/rule state stays out of the live round ledger.
    payload = {key: value for key, value in session.items()
               if key not in _DEMO_SESSION_VOLATILE_FIELDS}
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _is_finite_money(value: Any) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and __import__("math").isfinite(float(value)) and float(value) >= 0)


def _money_equal(left: Any, right: Any) -> bool:
    return (_is_finite_money(left) and _is_finite_money(right)
            and abs(float(left) - float(right)) <= 0.005)


def _signed_money_equal(left: Any, right: Any) -> bool:
    import math
    return (isinstance(left, (int, float)) and not isinstance(left, bool)
            and isinstance(right, (int, float)) and not isinstance(right, bool)
            and math.isfinite(float(left)) and math.isfinite(float(right))
            and abs(float(left) - float(right)) <= 0.005)

SCHEMA = """
CREATE TABLE IF NOT EXISTS rounds (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  round_id TEXT,
  round_index INTEGER UNIQUE NOT NULL,
  history_epoch BIGINT NOT NULL DEFAULT 1,
  multiplier REAL NOT NULL,
  timestamp TEXT,
  target INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS collector_round_observation_events (
  round_id TEXT PRIMARY KEY,
  multiplier NUMERIC(12,2) NOT NULL CHECK (multiplier >= 1),
  observed_at TEXT NOT NULL,
  payload TEXT NOT NULL,
  event_status TEXT NOT NULL DEFAULT 'PENDING'
    CHECK (event_status IN ('PENDING','RETRY','PROCESSED','SUPERSEDED','IDENTITY_CONFLICT','EXPIRED')),
  canonical_round_index BIGINT,
  attempts INTEGER NOT NULL DEFAULT 0,
  next_attempt_at TEXT,
  last_error TEXT,
  processed_at TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS collector_round_observation_pending_idx
  ON collector_round_observation_events(event_status, next_attempt_at, created_at);
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
CREATE TABLE IF NOT EXISTS application_state (
  state_key TEXT PRIMARY KEY,
  payload TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS history_epochs (
  singleton SMALLINT PRIMARY KEY CHECK(singleton=1),
  epoch BIGINT NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS history_export_manifests (
  export_id TEXT PRIMARY KEY,
  format TEXT NOT NULL,
  history_epoch BIGINT NOT NULL,
  record_count BIGINT NOT NULL,
  first_round TEXT,
  last_round TEXT,
  schema_version TEXT NOT NULL,
  checksum_sha256 TEXT NOT NULL,
  snapshot_id TEXT,
  artifact_path TEXT NOT NULL,
  created_at TEXT NOT NULL,
  verified_at TEXT,
  verification_json TEXT,
  status TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS rule_sessions (
  session_id TEXT PRIMARY KEY,
  mode TEXT NOT NULL,
  status TEXT NOT NULL,
  started_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS rule_evaluations (
  evaluation_id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL,
  source_stream_id TEXT,
  source_round_id TEXT,
  evaluated_at TEXT NOT NULL,
  result TEXT,
  payload TEXT NOT NULL,
  UNIQUE(session_id,source_stream_id,source_round_id,evaluated_at)
);
CREATE TABLE IF NOT EXISTS rule_selections (
  selection_id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL,
  selection_kind TEXT NOT NULL,
  source_stream_id TEXT,
  source_round_id TEXT,
  target_stream_id TEXT,
  target_round_id TEXT,
  target_round_index BIGINT,
  created_at TEXT NOT NULL,
  payload TEXT NOT NULL,
  UNIQUE(session_id,selection_id)
);
CREATE TABLE IF NOT EXISTS rule_execution_intents (
  intent_id TEXT PRIMARY KEY,
  decision_id TEXT NOT NULL UNIQUE,
  selection_id TEXT NOT NULL UNIQUE,
  session_id TEXT NOT NULL,
  source_round_id TEXT,
  target_stream_id TEXT NOT NULL,
  target_round_id TEXT,
  target_round_index BIGINT NOT NULL,
  panel INTEGER NOT NULL DEFAULT 0,
  requested_stake NUMERIC,
  requested_stake_currency TEXT,
  cashout_target NUMERIC,
  execution_status TEXT NOT NULL,
  created_at TEXT NOT NULL,
  expires_at TEXT,
  payload TEXT NOT NULL,
  UNIQUE(target_stream_id,target_round_index,panel)
);
CREATE TABLE IF NOT EXISTS rule_target_outcomes (
  outcome_id TEXT PRIMARY KEY,
  selection_id TEXT NOT NULL UNIQUE,
  session_id TEXT NOT NULL,
  target_round_id TEXT,
  target_round_index BIGINT,
  multiplier NUMERIC,
  round_outcome TEXT,
  bet_result TEXT NOT NULL DEFAULT 'UNKNOWN',
  reconciled_at TEXT,
  payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS platform_balance_observations (
  observation_id TEXT PRIMARY KEY,
  amount NUMERIC,
  currency TEXT,
  observed_at TEXT NOT NULL,
  provider_identity TEXT,
  session_identity TEXT,
  observation_status TEXT NOT NULL,
  payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS rule_session_financials (
  session_id TEXT PRIMARY KEY,
  starting_balance NUMERIC,
  current_balance NUMERIC,
  balance_change NUMERIC,
  realized_betting_pl NUMERIC,
  win_count BIGINT NOT NULL DEFAULT 0,
  loss_count BIGINT NOT NULL DEFAULT 0,
  currency TEXT,
  updated_at TEXT NOT NULL,
  payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS rule_session_stop_events (
  stop_event_id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL,
  reason TEXT NOT NULL,
  stopped_at TEXT NOT NULL,
  payload TEXT NOT NULL,
  UNIQUE(session_id,reason,stopped_at)
);
CREATE TABLE IF NOT EXISTS rules_engine_configuration_versions (
  configuration_version INTEGER PRIMARY KEY,
  mode TEXT NOT NULL,
  configuration TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS rules_engine_evaluations (
  evaluation_id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL,
  rule_id TEXT NOT NULL,
  rule_version TEXT NOT NULL,
  source_stream_id TEXT NOT NULL,
  source_round_id TEXT NOT NULL,
  source_round_index BIGINT NOT NULL,
  expected_target_index BIGINT,
  eligible BOOLEAN,
  status TEXT NOT NULL,
  reason TEXT NOT NULL,
  evaluated_at TEXT NOT NULL,
  source_evidence TEXT NOT NULL,
  state_snapshot TEXT NOT NULL,
  UNIQUE(session_id,rule_id,source_stream_id,source_round_index)
);
CREATE INDEX IF NOT EXISTS rules_engine_evaluations_source_idx
  ON rules_engine_evaluations(source_stream_id,source_round_index,evaluated_at);
CREATE TABLE IF NOT EXISTS demo_stream_meta (
  singleton SMALLINT PRIMARY KEY CHECK(singleton=1), stream_id TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS demo_rounds (
  id INTEGER PRIMARY KEY AUTOINCREMENT, stream_id TEXT NOT NULL, round_id TEXT NOT NULL UNIQUE,
  round_index BIGINT NOT NULL UNIQUE, multiplier NUMERIC NOT NULL, observed_at TEXT NOT NULL,
  stored_at TEXT NOT NULL, platform_round_id TEXT, identity_status TEXT NOT NULL DEFAULT 'UNKNOWN',
  continuity_status TEXT NOT NULL DEFAULT 'UNKNOWN', payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS demo_round_observations (
  event_id TEXT PRIMARY KEY, round_id TEXT NOT NULL, observed_at TEXT NOT NULL, payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS demo_rule_sessions (
  session_id TEXT PRIMARY KEY, environment TEXT NOT NULL CHECK(environment='DEMO'),
  status TEXT NOT NULL CHECK(status IN ('START','ACTIVE','STOP','RECOVERY','EXPIRED')),
  enabled_rule_ids TEXT NOT NULL, started_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  stopped_at TEXT, expires_at TEXT NOT NULL, payload TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS demo_one_active_session_idx
  ON demo_rule_sessions(environment) WHERE status IN ('START','ACTIVE','RECOVERY');
CREATE TABLE IF NOT EXISTS demo_session_events (
  event_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, event_type TEXT NOT NULL,
  recorded_at TEXT NOT NULL, payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS demo_rule_evaluations (
  evaluation_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, rule_id TEXT NOT NULL,
  rule_version TEXT NOT NULL, source_stream_id TEXT NOT NULL, source_sequence BIGINT NOT NULL,
  source_round_id TEXT NOT NULL, source_round_index BIGINT NOT NULL, eligible BOOLEAN,
  status TEXT NOT NULL, evaluated_at TEXT NOT NULL, payload TEXT NOT NULL,
  UNIQUE(session_id,rule_id,source_stream_id,source_sequence)
);
CREATE TABLE IF NOT EXISTS demo_rule_decisions (
  decision_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, source_stream_id TEXT NOT NULL,
  source_sequence BIGINT NOT NULL, source_round_id TEXT NOT NULL, source_round_index BIGINT NOT NULL,
  target_round_index BIGINT, action TEXT NOT NULL CHECK(action IN ('SELECT','NO_SELECT','BLOCKED')),
  created_at TEXT NOT NULL, payload TEXT NOT NULL,
  UNIQUE(session_id,source_stream_id,source_sequence)
);
CREATE TABLE IF NOT EXISTS demo_panel_observations (
  observation_id TEXT PRIMARY KEY, session_id TEXT, panel_id TEXT NOT NULL,
  observed_at TEXT NOT NULL, state TEXT NOT NULL, payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS demo_execution_attempts (
  attempt_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, decision_id TEXT NOT NULL UNIQUE,
  target_round_id TEXT, target_round_index BIGINT,
  state TEXT NOT NULL, reason TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS demo_rounds_stream_idx ON demo_rounds(stream_id,id);
CREATE INDEX IF NOT EXISTS demo_sessions_status_idx ON demo_rule_sessions(status,updated_at DESC);
CREATE INDEX IF NOT EXISTS demo_evaluations_session_idx ON demo_rule_evaluations(session_id,source_sequence);
CREATE INDEX IF NOT EXISTS demo_decisions_session_idx ON demo_rule_decisions(session_id,created_at DESC);
CREATE INDEX IF NOT EXISTS demo_attempts_session_idx ON demo_execution_attempts(session_id,created_at DESC);
"""


# Retired prediction and research ledgers remain intact in PostgreSQL under a
# reversible archive schema. Active startup must not recreate them in public.




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

    @staticmethod
    def _collector_observation_matches(round_id: str) -> bool:
        """Fail closed unless the collector still sees this as its latest round."""
        status_path = Path(__file__).resolve().parents[3] / "data" / "bot" / "status.json"
        try:
            status = json.loads(status_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        return (str(status.get("lastRoundId") or "") == str(round_id)
                and str(status.get("lastObservedRoundId") or "") == str(round_id))

    def init(self) -> None:
        if self.database_url:
            with self.connect() as conn:
                conn.executescript(SCHEMA)
                conn.execute("ALTER TABLE rule_execution_intents ALTER COLUMN cashout_target DROP NOT NULL")
                conn.execute("ALTER TABLE collector_round_observation_events DROP CONSTRAINT IF EXISTS collector_round_observation_events_event_status_check")
                conn.execute("""ALTER TABLE collector_round_observation_events
                    ADD CONSTRAINT collector_round_observation_events_event_status_check
                    CHECK (event_status IN ('PENDING','RETRY','PROCESSED','SUPERSEDED','IDENTITY_CONFLICT','EXPIRED'))""")
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
                conn.execute("ALTER TABLE aviator_rounds ADD COLUMN IF NOT EXISTS history_epoch BIGINT NOT NULL DEFAULT 1")
                conn.execute("INSERT INTO history_epochs(singleton,epoch,updated_at) VALUES(1,1,NOW()) ON CONFLICT(singleton) DO NOTHING")
                conn.execute("UPDATE aviator_rounds SET stored_at=COALESCE(stored_at,created_at)")
                conn.execute("UPDATE aviator_rounds SET local_round_index=COALESCE(local_round_index,round_index)")
                conn.execute("UPDATE aviator_rounds SET observed_at=COALESCE(observed_at,timestamp) WHERE round_id ~ '^aviator-[0-9a-f]{24}$'")
                conn.execute("UPDATE aviator_rounds SET round_identity_type=COALESCE(round_identity_type,'COLLECTOR_OBSERVATION_HASH'), round_index_source=COALESCE(round_index_source,'LOCAL_SEQUENCE'), identity_confidence=COALESCE(identity_confidence,CASE WHEN continuity_verified THEN 'OVERLAP_VERIFIED_ORDER_ONLY' ELSE 'UNKNOWN' END), continuity_proof=COALESCE(continuity_proof,'LEGACY_UNVERIFIED')")
                conn.execute("UPDATE aviator_rounds SET platform_timestamp=NULL WHERE round_id ~ '^aviator-[0-9a-f]{24}$'")
                conn.execute("""CREATE TABLE IF NOT EXISTS round_distribution_meta (
                    singleton SMALLINT PRIMARY KEY CHECK (singleton=1),
                    stream_id TEXT NOT NULL UNIQUE,
                    continuity_gaps BIGINT NOT NULL DEFAULT 0,
                    continuity_unknown BIGINT NOT NULL DEFAULT 0,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )""")
                conn.execute("ALTER TABLE round_distribution_meta ADD COLUMN IF NOT EXISTS continuity_gaps BIGINT NOT NULL DEFAULT 0")
                conn.execute("ALTER TABLE round_distribution_meta ADD COLUMN IF NOT EXISTS continuity_unknown BIGINT NOT NULL DEFAULT 0")
                conn.execute("INSERT INTO round_distribution_meta(singleton,stream_id) VALUES(1,?) ON CONFLICT(singleton) DO NOTHING",
                             (uuid.uuid4().hex,))
                conn.execute("""CREATE TABLE IF NOT EXISTS round_distribution_events (
                    stream_sequence BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                    stream_id TEXT NOT NULL,
                    event_id TEXT NOT NULL,
                    round_id TEXT NOT NULL,
                    round_index BIGINT NOT NULL,
                    published_at TIMESTAMPTZ,
                    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    UNIQUE(stream_id,event_id),
                    UNIQUE(stream_id,round_id)
                )""")
                conn.execute("CREATE INDEX IF NOT EXISTS round_distribution_events_round_idx ON round_distribution_events(round_index)")
                conn.execute("DELETE FROM round_distribution_events WHERE stream_id<>(SELECT stream_id FROM round_distribution_meta WHERE singleton=1)")
                conn.execute("""CREATE TABLE IF NOT EXISTS external_project_tokens (
                    token_id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    label TEXT NOT NULL DEFAULT '',
                    token_hash TEXT NOT NULL UNIQUE,
                    token_prefix TEXT NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    revoked_at TIMESTAMPTZ
                )""")
                conn.execute("CREATE INDEX IF NOT EXISTS external_project_tokens_project_idx ON external_project_tokens(project_id,created_at)")
                conn.execute("ALTER TABLE rounds ADD COLUMN IF NOT EXISTS round_id TEXT")
                conn.execute("ALTER TABLE rounds ADD COLUMN IF NOT EXISTS history_epoch BIGINT NOT NULL DEFAULT 1")
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
            intent_columns = {row[1]: row for row in conn.execute(
                "PRAGMA table_info(rule_execution_intents)").fetchall()}
            cashout_column = intent_columns.get("cashout_target")
            if cashout_column and cashout_column[3]:
                conn.execute("ALTER TABLE rule_execution_intents RENAME TO rule_execution_intents_legacy_v1")
                conn.execute("""CREATE TABLE rule_execution_intents (
                    intent_id TEXT PRIMARY KEY,
                    decision_id TEXT NOT NULL UNIQUE,
                    selection_id TEXT NOT NULL UNIQUE,
                    session_id TEXT NOT NULL,
                    source_round_id TEXT,
                    target_stream_id TEXT NOT NULL,
                    target_round_id TEXT,
                    target_round_index BIGINT NOT NULL,
                    panel INTEGER NOT NULL DEFAULT 0,
                    requested_stake NUMERIC,
                    requested_stake_currency TEXT,
                    cashout_target NUMERIC,
                    execution_status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT,
                    payload TEXT NOT NULL,
                    UNIQUE(target_stream_id,target_round_index,panel)
                )""")
                conn.execute("""INSERT INTO rule_execution_intents(
                    intent_id,decision_id,selection_id,session_id,source_round_id,target_stream_id,
                    target_round_id,target_round_index,panel,requested_stake,requested_stake_currency,
                    cashout_target,execution_status,created_at,expires_at,payload)
                    SELECT intent_id,decision_id,selection_id,session_id,source_round_id,target_stream_id,
                    target_round_id,target_round_index,panel,requested_stake,requested_stake_currency,
                    cashout_target,execution_status,created_at,expires_at,payload
                    FROM rule_execution_intents_legacy_v1""")
                conn.execute("DROP TABLE rule_execution_intents_legacy_v1")
            conn.execute("INSERT OR IGNORE INTO history_epochs(singleton,epoch,updated_at) VALUES(1,1,CURRENT_TIMESTAMP)")
            columns = {row[1] for row in conn.execute("PRAGMA table_info(rounds)").fetchall()}
            if "round_id" not in columns:
                conn.execute("ALTER TABLE rounds ADD COLUMN round_id TEXT")
            if "history_epoch" not in columns:
                conn.execute("ALTER TABLE rounds ADD COLUMN history_epoch BIGINT NOT NULL DEFAULT 1")
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
            epoch_row = conn.execute("SELECT epoch FROM history_epochs WHERE singleton=1").fetchone()
            epoch = max(1, int(epoch_row["epoch"] or 1)) if epoch_row else 1
            rows = [{**row, "history_epoch": epoch} for row in rows]
            conn.executemany(
                """
                INSERT INTO rounds(round_id, round_index, history_epoch, multiplier, timestamp, target)
                VALUES(:round_id, :round_index, :history_epoch, :multiplier, :timestamp, :target)
                ON CONFLICT(round_id) DO UPDATE SET
                  round_index=excluded.round_index,
                  history_epoch=excluded.history_epoch,
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

    def load_recent_rounds(self, limit: int) -> list[dict[str, Any]]:
        """Read the newest canonical rounds for the History page, oldest first."""
        table = "aviator_rounds" if self.database_url else "rounds"
        with self.connect() as conn:
            rows = conn.execute(
                f"SELECT round_id,round_index,multiplier,timestamp FROM {table} "
                "ORDER BY round_index DESC LIMIT ?",
                (min(max(int(limit), 1), 5000),),
            ).fetchall()
        return [{"round_id": str(row["round_id"]) if row["round_id"] is not None else None,
                 "round_index": int(row["round_index"]),
                 "multiplier": float(row["multiplier"]),
                 "timestamp": self._db_time_text(row["timestamp"])}
                for row in reversed(rows)]

    def round_history_status(self) -> dict[str, Any]:
        """Return the exact history count and two newest rounds without loading the full ledger."""
        table = "aviator_rounds" if self.database_url else "rounds"
        with self.connect() as conn:
            count_row = conn.execute(f"SELECT COUNT(*) AS round_count FROM {table}").fetchone()
            recent = conn.execute(
                f"SELECT round_id,round_index,multiplier,timestamp FROM {table} "
                "ORDER BY round_index DESC LIMIT 2"
            ).fetchall()

        def as_round(row):
            if not row:
                return None
            return {
                "round_id": str(row["round_id"] or row["round_index"]),
                "round_index": int(row["round_index"]),
                "timestamp": self._db_time_text(row["timestamp"]),
                "multiplier": float(row["multiplier"]),
            }

        ordered = list(reversed(recent))
        return {
            "count": int(count_row["round_count"] or 0) if count_row else 0,
            "latest": as_round(ordered[-1]) if ordered else None,
            "previous": as_round(ordered[-2]) if len(ordered) > 1 else None,
        }

    def current_history_epoch(self) -> int:
        """Return the authoritative history generation, defaulting to epoch 1."""
        with self.connect() as conn:
            try:
                row = conn.execute("SELECT epoch FROM history_epochs WHERE singleton=1").fetchone()
            except Exception:
                return 1
        return max(1, int(row["epoch"] or 1)) if row else 1

    def load_rounds_from(self, round_index: int) -> list[dict[str, Any]]:
        """Load a live history tail, including an overlap for identity rechecks."""
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT round_id, round_index, multiplier, timestamp, "
                + ("platform_round_id, platform_round_index, local_round_index, platform_timestamp, observed_at, COALESCE(stored_at,created_at) AS stored_at, round_identity_type, round_index_source, identity_confidence, continuity_verified, gap_before, continuity_proof " if self.database_url else "NULL AS platform_round_id, NULL AS platform_round_index, round_index AS local_round_index, NULL AS platform_timestamp, NULL AS observed_at, NULL AS stored_at, 'LEGACY_UNKNOWN' AS round_identity_type, 'LEGACY_UNKNOWN' AS round_index_source, 'UNKNOWN' AS identity_confidence, 0 AS continuity_verified, 0 AS gap_before, 'LEGACY_UNVERIFIED' AS continuity_proof ")
                + " FROM " + ("aviator_rounds" if self.database_url else "rounds")
                + " WHERE round_index>=? ORDER BY round_index ASC",
                (int(round_index),),
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

    def verified_round_suffix_count(self) -> int:
        """Return the backend-authoritative live suffix length without row decoding."""
        if not self.database_url:
            return 0
        with self.connect() as conn:
            row = conn.execute("""SELECT COUNT(*) AS n FROM aviator_rounds
                WHERE round_index >= (
                    SELECT COALESCE(MAX(round_index),0) FROM aviator_rounds
                    WHERE continuity_verified=FALSE OR gap_before=TRUE
                ) AND continuity_verified=TRUE""").fetchone()
        return int(row["n"] or 0)



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






    def latest_round_marker(self) -> tuple | None:
        if not self.database_url:
            return None
        with self.connect() as conn:
            row = conn.execute("""SELECT round_index,round_id,COALESCE(platform_timestamp,timestamp) AS timestamp,multiplier
                                  FROM aviator_rounds ORDER BY round_index DESC LIMIT 1""").fetchone()
        return (int(row["round_index"]), str(row["round_id"]), str(row["timestamp"]), float(row["multiplier"])) if row else None

    def round_metadata(self, round_id: str, round_index: int) -> dict[str, Any] | None:
        """Read timing/continuity for one exact source identity."""
        with self.connect() as conn:
            if self.database_url:
                row = conn.execute("""SELECT round_id,round_index,observed_at,stored_at,timestamp,
                    gap_before,continuity_verified,identity_confidence
                    FROM aviator_rounds WHERE round_id=? AND round_index=?""",
                    (str(round_id), int(round_index))).fetchone()
            else:
                row = conn.execute("""SELECT round_id,round_index,timestamp,NULL AS stored_at,
                    NULL AS observed_at,0 AS gap_before,0 AS continuity_verified,
                    'UNKNOWN' AS identity_confidence FROM rounds
                    WHERE round_id=? AND round_index=?""",
                    (str(round_id), int(round_index))).fetchone()
        return dict(row) if row else None

    def round_identity_details(self, round_id: str, round_index: int) -> dict[str, Any] | None:
        """Read the persisted identity and continuity proof for one exact round."""
        with self.connect() as conn:
            if self.database_url:
                row = conn.execute("""SELECT round_id,round_index,platform_round_id,platform_round_index,
                    round_identity_type,round_index_source,identity_confidence,continuity_verified,
                    gap_before,continuity_proof FROM aviator_rounds
                    WHERE round_id=? AND round_index=?""", (str(round_id), int(round_index))).fetchone()
            else:
                row = conn.execute("""SELECT round_id,round_index,NULL AS platform_round_id,
                    NULL AS platform_round_index,'LEGACY_UNKNOWN' AS round_identity_type,
                    'LEGACY_UNKNOWN' AS round_index_source,'UNKNOWN' AS identity_confidence,
                    0 AS continuity_verified,0 AS gap_before,'LEGACY_UNVERIFIED' AS continuity_proof
                    FROM rounds WHERE round_id=? AND round_index=?""",
                    (str(round_id), int(round_index))).fetchone()
        if not row:
            return None
        result = dict(row)
        result["round_index"] = int(result["round_index"])
        result["continuity_verified"] = bool(result["continuity_verified"])
        result["gap_before"] = bool(result["gap_before"])
        return result

    def round_metadata_for_sources(self, sources: list[tuple[str, int]]) -> dict[tuple[str, int], dict[str, Any]]:
        unique = list(dict.fromkeys((str(round_id), int(index)) for round_id, index in sources))[:2000]
        if not unique:
            return {}
        indexes = sorted({index for _round_id, index in unique})
        placeholders = ",".join("?" for _ in indexes)
        table = "aviator_rounds" if self.database_url else "rounds"
        timing_columns = ("observed_at,stored_at,timestamp,gap_before,continuity_verified,identity_confidence"
                          if self.database_url else "NULL AS observed_at,NULL AS stored_at,timestamp,"
                          "0 AS gap_before,0 AS continuity_verified,'UNKNOWN' AS identity_confidence")
        with self.connect() as conn:
            rows = conn.execute(f"SELECT round_id,round_index,{timing_columns} FROM {table} "
                                f"WHERE round_index IN ({placeholders})", tuple(indexes)).fetchall()
        wanted = set(unique)
        return {(str(row["round_id"]), int(row["round_index"])): dict(row)
                for row in rows if (str(row["round_id"]), int(row["round_index"])) in wanted}













    def reconciliation_for_execution(self, execution_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT payload FROM reconciliation_records WHERE execution_id=?",
                               (str(execution_id),)).fetchone()
        return json.loads(row["payload"]) if row else None





    def stage_collector_round_observations(self, observations: list[dict[str, Any]]) -> int:
        """Durably stage verified direct-page observations before round persistence.

        The event is only a wake-up/evidence envelope. Downstream consumers must
        wait for a matching canonical round row before scoring it.
        """
        if not observations:
            return 0
        staged = 0
        with self.connect() as conn:
            for observation in observations:
                round_id = str(observation["round_id"])
                multiplier = round(float(observation["multiplier"]), 2)
                observed_at = str(observation["observed_at"])
                payload = json.dumps(observation, sort_keys=True, separators=(",", ":"), default=str)
                now = datetime.now(timezone.utc).isoformat()
                cursor = conn.execute(
                    """INSERT INTO collector_round_observation_events
                       (round_id,multiplier,observed_at,payload,event_status,created_at,updated_at)
                       VALUES (?,?,?,?,'PENDING',?,?) ON CONFLICT(round_id) DO NOTHING""",
                    (round_id, multiplier, observed_at, payload, now, now),
                )
                staged += max(0, int(getattr(cursor, "rowcount", 0) or 0))
                prior = conn.execute(
                    "SELECT multiplier,observed_at,payload FROM collector_round_observation_events WHERE round_id=?",
                    (round_id,),
                ).fetchone()
                if not prior:
                    raise RuntimeError("collector_observation_stage_missing_after_insert")
                prior_payload = json.loads(prior["payload"])
                if (round(float(prior["multiplier"]), 2) != multiplier
                        or str(prior["observed_at"]) != observed_at
                        or prior_payload.get("platform_round_id") != observation.get("platform_round_id")):
                    raise ValueError(f"collector_round_identity_conflict:{round_id}")
        return staged

    def pending_collector_round_observations(self, limit: int = 100) -> list[dict[str, Any]]:
        """Return staged direct observations that now have canonical round rows."""
        if not self.database_url:
            table = "rounds"
            stored_at_expr = "r.timestamp"
        else:
            table = "aviator_rounds"
            stored_at_expr = "COALESCE(r.stored_at,r.created_at)"
        now = datetime.now(timezone.utc).isoformat()
        with self.connect() as conn:
            rows = conn.execute(
                f"""SELECT e.round_id,e.multiplier AS direct_multiplier,e.observed_at AS direct_observed_at,
                           {stored_at_expr} AS canonical_stored_at,
                           e.payload,e.event_status,e.attempts,
                           r.round_id AS canonical_round_id,r.round_index AS canonical_round_index,
                           r.multiplier AS canonical_multiplier
                    FROM collector_round_observation_events e
                    JOIN {table} r ON r.round_id=e.round_id
                    WHERE e.event_status IN ('PENDING','RETRY')
                      AND (e.next_attempt_at IS NULL OR e.next_attempt_at<=?)
                    ORDER BY r.round_index ASC LIMIT ?""",
                (now, max(1, min(int(limit), 500))),
            ).fetchall()
        return [dict(row) for row in rows]

    def retry_collector_round_observation(self, round_id: str, error: str,
                                          retry_after_seconds: float = 1.0) -> None:
        now = datetime.now(timezone.utc)
        next_attempt = now.timestamp() + max(0.1, float(retry_after_seconds))
        with self.connect() as conn:
            conn.execute(
                """UPDATE collector_round_observation_events
                   SET event_status=CASE WHEN attempts+1>=? THEN 'EXPIRED' ELSE 'RETRY' END,
                       attempts=attempts+1,
                       next_attempt_at=CASE WHEN attempts+1>=? THEN NULL ELSE ? END,
                       last_error=CASE WHEN attempts+1>=? THEN ? ELSE ? END,updated_at=?
                   WHERE round_id=? AND event_status IN ('PENDING','RETRY')""",
                (MAX_COLLECTOR_EVENT_ATTEMPTS, MAX_COLLECTOR_EVENT_ATTEMPTS,
                 datetime.fromtimestamp(next_attempt, timezone.utc).isoformat(),
                 MAX_COLLECTOR_EVENT_ATTEMPTS, f"RETRY_LIMIT_EXHAUSTED:{str(error)[:960]}",
                 str(error)[:1000], now.isoformat(), str(round_id)),
            )

    def finish_collector_round_observation(self, round_id: str, status: str,
                                           canonical_round_index: int | None = None,
                                           reason: str | None = None) -> None:
        if status not in {"PROCESSED", "SUPERSEDED", "IDENTITY_CONFLICT", "EXPIRED"}:
            raise ValueError("invalid_collector_observation_event_status")
        now = datetime.now(timezone.utc).isoformat()
        with self.connect() as conn:
            conn.execute(
                """UPDATE collector_round_observation_events
                   SET event_status=?,canonical_round_index=?,last_error=?,processed_at=?,updated_at=?
                   WHERE round_id=? AND event_status IN ('PENDING','RETRY')""",
                (status, canonical_round_index, str(reason)[:1000] if reason else None,
                 now, now, str(round_id)),
            )

    def latest_round_timing(self) -> dict[str, Any] | None:
        """Return cheap persisted-round timing for health dashboards."""
        if not self.database_url:
            return None
        with self.connect() as conn:
            row = conn.execute("""SELECT round_index,observed_at,
                COALESCE(stored_at,created_at) AS stored_at,
                EXTRACT(EPOCH FROM (COALESCE(stored_at,created_at)-observed_at))*1000 AS collector_latency_ms
                FROM aviator_rounds ORDER BY round_index DESC LIMIT 1""").fetchone()
        return dict(row) if row else None

    def collector_observation_queue_snapshot(self) -> dict[str, Any]:
        """Return authoritative pending direct-round event depth and oldest age."""
        if not self.database_url:
            return {"depth": 0, "expired_jobs": 0, "oldest_job_age_seconds": None}
        with self.connect() as conn:
            row = conn.execute("""SELECT COUNT(*) FILTER (WHERE event_status IN ('PENDING','RETRY')) AS depth,
                COUNT(*) FILTER (WHERE event_status='EXPIRED') AS expired_jobs,
                MIN(created_at) FILTER (WHERE event_status IN ('PENDING','RETRY')) AS oldest_job_created_at
                FROM collector_round_observation_events""").fetchone()
        created_at = row.get("oldest_job_created_at") if row else None
        age_seconds = None
        if created_at is not None:
            try:
                parsed = (created_at if isinstance(created_at, datetime)
                          else datetime.fromisoformat(str(created_at).replace("Z", "+00:00")))
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                age_seconds = max(0.0, (datetime.now(timezone.utc) - parsed.astimezone(timezone.utc)).total_seconds())
            except (ValueError, TypeError, OverflowError):
                age_seconds = None
        return {"depth": int(row["depth"] or 0), "expired_jobs": int(row.get("expired_jobs") or 0),
                "oldest_job_age_seconds": age_seconds}

    @staticmethod
    def _round_distribution_payload(row: dict[str, Any] | None) -> dict[str, Any] | None:
        if not row:
            return None
        payload = row.get("payload")
        if isinstance(payload, str):
            payload = json.loads(payload)
        return dict(payload or {})

    def round_distribution_snapshot(self) -> dict[str, Any]:
        if not self.database_url:
            raise RuntimeError("round distribution requires authoritative PostgreSQL")
        with self.connect() as conn:
            meta = conn.execute("SELECT stream_id,continuity_gaps,continuity_unknown FROM round_distribution_meta WHERE singleton=1").fetchone()
            row = conn.execute("""SELECT MIN(stream_sequence) AS earliest_sequence,
                       MAX(stream_sequence) AS latest_sequence,COUNT(*) AS retained_events,
                (SELECT payload FROM round_distribution_events
                        WHERE stream_id=? ORDER BY stream_sequence DESC LIMIT 1) AS latest_payload
                FROM round_distribution_events WHERE stream_id=?""",
                (meta["stream_id"], meta["stream_id"])).fetchone()
        return {
            "stream_id": str(meta["stream_id"]),
            "continuity_gaps": int(meta.get("continuity_gaps") or 0),
            "continuity_unknown": int(meta.get("continuity_unknown") or 0),
            "earliest_sequence": int(row["earliest_sequence"]) if row and row["earliest_sequence"] is not None else None,
            "latest_sequence": int(row["latest_sequence"]) if row and row["latest_sequence"] is not None else 0,
            "retained_events": int(row["retained_events"] or 0) if row else 0,
            "latest": self._round_distribution_payload({"payload": row.get("latest_payload")}) if row else None,
        }

    def round_distribution_stream_id(self) -> str:
        if not self.database_url:
            raise RuntimeError("round distribution requires authoritative PostgreSQL")
        with self.connect() as conn:
            row = conn.execute("SELECT stream_id FROM round_distribution_meta WHERE singleton=1").fetchone()
        if not row:
            raise RuntimeError("round distribution stream metadata is not initialized")
        return str(row["stream_id"])

    def canonical_round_matches(self, round_id: str, multiplier: float) -> bool:
        if not self.database_url:
            raise RuntimeError("round distribution requires authoritative PostgreSQL")
        with self.connect() as conn:
            row = conn.execute("SELECT multiplier FROM aviator_rounds WHERE round_id=?",
                               (str(round_id),)).fetchone()
        return bool(row and abs(float(row["multiplier"]) - float(multiplier)) <= 0.0001)

    def round_distribution_events(self, stream_id: str, after_sequence: int,
                                  through_sequence: int, limit: int = 500) -> list[dict[str, Any]]:
        if not self.database_url:
            raise RuntimeError("round distribution requires authoritative PostgreSQL")
        bounded = min(max(int(limit), 1), 500)
        with self.connect() as conn:
            rows = conn.execute("""SELECT payload FROM round_distribution_events
                WHERE stream_id=? AND stream_sequence>? AND stream_sequence<=?
                ORDER BY stream_sequence ASC LIMIT ?""",
                (str(stream_id), int(after_sequence), int(through_sequence), bounded)).fetchall()
        return [self._round_distribution_payload(row) for row in rows]

    def count_round_distribution_after(self, stream_id: str, after_sequence: int,
                                       through_sequence: int) -> int:
        if not self.database_url:
            raise RuntimeError("round distribution requires authoritative PostgreSQL")
        with self.connect() as conn:
            row = conn.execute("""SELECT COUNT(*) AS event_count FROM round_distribution_events
                WHERE stream_id=? AND stream_sequence>? AND stream_sequence<=?""",
                (str(stream_id), int(after_sequence), int(through_sequence))).fetchone()
        return int(row["event_count"] or 0) if row else 0

    def persist_round_distribution_event(self, round_id: str, expected_multiplier: float,
                                         observation: dict[str, Any], retention: int = 100000,
                                         *, connection: Any | None = None,
                                         prune: bool = True) -> dict[str, Any]:
        """Create one replayable committed event only after its canonical PG row exists."""
        if not self.database_url:
            raise RuntimeError("round distribution requires authoritative PostgreSQL")
        with (nullcontext(connection) if connection is not None else self.connect()) as conn:
            meta = conn.execute("SELECT stream_id FROM round_distribution_meta WHERE singleton=1").fetchone()
            stream_id = str(meta["stream_id"])
            canonical = conn.execute("""SELECT round_id,round_index,multiplier,platform_round_id,
                    platform_round_index,platform_timestamp,observed_at,COALESCE(stored_at,created_at) AS stored_at,
                    round_identity_type,round_index_source,identity_confidence,continuity_verified,gap_before,source
                FROM aviator_rounds WHERE round_id=?""", (str(round_id),)).fetchone()
            if not canonical:
                raise ValueError("canonical_postgresql_round_not_found")
            if str(canonical["round_id"]) != str(round_id) or abs(float(canonical["multiplier"]) - float(expected_multiplier)) > 0.0001:
                raise ValueError("direct_observation_does_not_match_canonical_round")

            def iso(value):
                return value.isoformat() if hasattr(value, "isoformat") else value

            observed_at = observation.get("observed_at") or iso(canonical["observed_at"])
            if canonical["platform_round_id"] is not None:
                identity_source = "PLATFORM_ROUND_ID"
            elif canonical["platform_round_index"] is not None:
                identity_source = "PLATFORM_ROUND_INDEX"
            else:
                identity_source = ("COLLECTOR_OBSERVATION_HASH"
                                   if canonical["round_identity_type"] == "COLLECTOR_OBSERVATION_HASH"
                                   else "LOCAL_SEQUENCE" if canonical["round_index_source"] == "LOCAL_SEQUENCE"
                                   else "UNKNOWN")
            continuity_status = ("GAP_BEFORE" if bool(canonical["gap_before"])
                                 else "VERIFIED" if bool(canonical["continuity_verified"])
                                 else "UNVERIFIED")
            legacy_event_id = f"aviator-round:{round_id}"
            event_id = f"{legacy_event_id}:persisted"
            observation_id = str(observation.get("observation_id") or round_id)
            payload = {
                "schema_version": "1.0",
                "event_id": event_id,
                "legacy_event_id": legacy_event_id,
                "event_type": "round.persisted",
                "stage": "PERSISTED",
                "stream_id": stream_id,
                "stream_sequence": None,
                "committed_sequence": None,
                "observation_id": observation_id,
                "round_id": str(canonical["round_id"]),
                "identity_source": identity_source,
                "multiplier": float(canonical["multiplier"]),
                "platform_timestamp": iso(canonical["platform_timestamp"]),
                "observed_at": iso(observed_at),
                "stored_at": iso(canonical["stored_at"]),
                "published_at": None,
                "commit_acknowledged_at": iso(observation.get("commit_acknowledged_at")),
                "observation_to_commit_ms": observation.get("observation_to_commit_ms"),
                "commit_latency_measurement_clock": ("COLLECTOR_MONOTONIC"
                    if observation.get("observation_to_commit_ms") is not None else None),
                "durability": "COMMITTED",
                "delivery_mode": str(observation.get("delivery_mode") or "LIVE"),
                "continuity_status": continuity_status,
                "timing_uncertainty": {"next_round_open": "UNKNOWN"},
                "game_phase": observation.get("game_phase"),
                "round_index": int(canonical["round_index"]),
                "platform_round_id": str(canonical["platform_round_id"]) if canonical["platform_round_id"] is not None else None,
                "platform_round_index": int(canonical["platform_round_index"]) if canonical["platform_round_index"] is not None else None,
            }
            inserted = conn.execute("""INSERT INTO round_distribution_events
                    (stream_id,event_id,round_id,round_index,payload)
                VALUES(?,?,?,?,?::jsonb) ON CONFLICT DO NOTHING
                RETURNING stream_sequence""",
                (stream_id, event_id, str(round_id), int(canonical["round_index"]),
                 json.dumps(payload, sort_keys=True, separators=(",", ":")))).fetchone()
            if inserted:
                sequence = int(inserted["stream_sequence"])
                payload["stream_sequence"] = sequence
                payload["committed_sequence"] = sequence
                conn.execute("UPDATE round_distribution_events SET payload=?::jsonb WHERE stream_id=? AND stream_sequence=?",
                             (json.dumps(payload, sort_keys=True, separators=(",", ":")), stream_id, sequence))
                if continuity_status != "VERIFIED":
                    if continuity_status == "GAP_BEFORE":
                        conn.execute("UPDATE round_distribution_meta SET continuity_gaps=continuity_gaps+1 WHERE singleton=1")
                    else:
                        conn.execute("UPDATE round_distribution_meta SET continuity_unknown=continuity_unknown+1 WHERE singleton=1")
                if prune:
                    bounded_retention = min(max(int(retention), 1000), 1_000_000)
                    conn.execute("""DELETE FROM round_distribution_events WHERE stream_id=? AND stream_sequence NOT IN
                        (SELECT stream_sequence FROM round_distribution_events WHERE stream_id=?
                         ORDER BY stream_sequence DESC LIMIT ?)""",
                        (stream_id, stream_id, bounded_retention))
                return payload
            existing = conn.execute("SELECT payload FROM round_distribution_events WHERE stream_id=? AND round_id=?",
                                    (stream_id, str(round_id))).fetchone()
            prior = self._round_distribution_payload(existing)
            if not prior:
                raise ValueError("round_distribution_event_identity_conflict")
            if prior.get("event_id") == legacy_event_id:
                # Upgrade the legacy single-stage identifier in place while
                # retaining it for older receiver diagnostics.
                sequence = int(prior.get("stream_sequence") or 0) or None
                prior.update({
                    "schema_version": "1.0",
                    "legacy_event_id": legacy_event_id,
                    "event_id": event_id,
                    "event_type": "round.persisted",
                    "stage": "PERSISTED",
                    "stream_id": stream_id,
                    "stream_sequence": sequence,
                    "committed_sequence": sequence,
                    "observation_id": observation_id,
                    "round_id": str(canonical["round_id"]),
                    "identity_source": identity_source,
                    "multiplier": float(canonical["multiplier"]),
                    "platform_timestamp": iso(canonical["platform_timestamp"]),
                    "observed_at": iso(observed_at),
                    "stored_at": iso(canonical["stored_at"]),
                    "published_at": prior.get("published_at"),
                    "durability": "COMMITTED",
                    "continuity_status": continuity_status,
                    "delivery_mode": str(observation.get("delivery_mode") or prior.get("delivery_mode") or "RECOVERED"),
                    "round_index": int(canonical["round_index"]),
                    "platform_round_id": (str(canonical["platform_round_id"])
                                           if canonical["platform_round_id"] is not None else None),
                    "platform_round_index": (int(canonical["platform_round_index"])
                                             if canonical["platform_round_index"] is not None else None),
                    "commit_acknowledged_at": iso(observation.get("commit_acknowledged_at")),
                    "observation_to_commit_ms": observation.get("observation_to_commit_ms"),
                    "commit_latency_measurement_clock": ("COLLECTOR_MONOTONIC"
                        if observation.get("observation_to_commit_ms") is not None else None),
                    "timing_uncertainty": prior.get("timing_uncertainty") or {"next_round_open": "UNKNOWN"},
                    "game_phase": observation.get("game_phase") or prior.get("game_phase"),
                })
                conn.execute("UPDATE round_distribution_events SET event_id=?,payload=?::jsonb WHERE stream_id=? AND round_id=?",
                             (event_id, json.dumps(prior, sort_keys=True, separators=(",", ":")),
                              stream_id, str(round_id)))
            if prior.get("event_id") != event_id:
                raise ValueError("round_distribution_event_identity_conflict")
            return prior

    def mark_round_distribution_event_published(self, stream_id: str, sequence: int,
                                                *, connection: Any | None = None) -> dict[str, Any] | None:
        if not self.database_url:
            raise RuntimeError("round distribution requires authoritative PostgreSQL")
        with (nullcontext(connection) if connection is not None else self.connect()) as conn:
            row = conn.execute("SELECT payload,published_at FROM round_distribution_events WHERE stream_id=? AND stream_sequence=?",
                               (str(stream_id), int(sequence))).fetchone()
            if not row:
                return None
            payload = self._round_distribution_payload(row)
            if row["published_at"] is None:
                published_at = datetime.now(timezone.utc)
                payload["published_at"] = published_at.isoformat()
                conn.execute("UPDATE round_distribution_events SET published_at=?,payload=?::jsonb WHERE stream_id=? AND stream_sequence=?",
                             (published_at, json.dumps(payload, sort_keys=True, separators=(",", ":")),
                              str(stream_id), int(sequence)))
            return payload

    def persist_round_distribution_batch(self, observations: list[dict[str, Any]],
                                         retention: int = 100000) -> list[dict[str, Any]]:
        """Commit ordered canonical handoffs in one PostgreSQL transaction."""
        if not observations:
            return []
        bounded_retention = min(max(int(retention), 1000), 1_000_000)
        with self.connect() as conn:
            events = []
            for observation in observations:
                event = self.persist_round_distribution_event(
                    observation["round_id"], observation["multiplier"], observation,
                    bounded_retention, connection=conn, prune=False)
                if event.get("published_at") is None:
                    event = self.mark_round_distribution_event_published(
                        event["stream_id"], event["stream_sequence"], connection=conn)
                events.append(event)
            stream_id = events[0]["stream_id"]
            conn.execute("""DELETE FROM round_distribution_events WHERE stream_id=? AND stream_sequence NOT IN
                (SELECT stream_sequence FROM round_distribution_events WHERE stream_id=?
                 ORDER BY stream_sequence DESC LIMIT ?)""",
                (stream_id, stream_id, bounded_retention))
        return events

    def page_round_distribution(self, stream_id: str, after_sequence: int,
                                limit: int = 500) -> list[dict[str, Any]]:
        snapshot = self.round_distribution_snapshot()
        return self.round_distribution_events(stream_id, after_sequence,
            int(snapshot["latest_sequence"]), min(max(int(limit), 1), 500))

    def create_external_project_token(self, token_id: str, project_id: str, label: str,
                                      token_hash: str, token_prefix: str) -> None:
        if not self.database_url:
            raise RuntimeError("external project tokens require authoritative PostgreSQL")
        with self.connect() as conn:
            conn.execute("""INSERT INTO external_project_tokens(token_id,project_id,label,token_hash,token_prefix)
                VALUES(?,?,?,?,?)""", (token_id, project_id, label, token_hash, token_prefix))

    def external_project_token(self, token_hash: str) -> dict[str, Any] | None:
        if not self.database_url:
            raise RuntimeError("external project tokens require authoritative PostgreSQL")
        with self.connect() as conn:
            row = conn.execute("""SELECT token_id,project_id,label,token_prefix,created_at
                FROM external_project_tokens WHERE token_hash=? AND revoked_at IS NULL""",
                (token_hash,)).fetchone()
        return dict(row) if row else None

    def revoke_external_project_token(self, project_id: str, token_id: str) -> bool:
        if not self.database_url:
            raise RuntimeError("external project tokens require authoritative PostgreSQL")
        with self.connect() as conn:
            result = conn.execute("""UPDATE external_project_tokens SET revoked_at=NOW()
                WHERE project_id=? AND token_id=? AND revoked_at IS NULL""", (project_id, token_id))
        return bool(result.rowcount)








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
            query = """SELECT round_index,round_id,multiplier,COALESCE(stored_at,created_at) AS stored_at,
                             timestamp,continuity_verified,gap_before,identity_confidence,continuity_proof,
                             platform_round_id,platform_round_index,round_identity_type,round_index_source
                      FROM aviator_rounds WHERE round_index BETWEEN ? AND ? ORDER BY round_index"""
        else:
            query = """SELECT round_index,round_id,multiplier,NULL AS stored_at,timestamp,
                             FALSE AS continuity_verified,FALSE AS gap_before,
                             'UNKNOWN' AS identity_confidence,'LEGACY_UNVERIFIED' AS continuity_proof,
                             NULL AS platform_round_id,NULL AS platform_round_index,
                             'LEGACY_UNKNOWN' AS round_identity_type,'LEGACY_UNKNOWN' AS round_index_source
                      FROM rounds WHERE round_index BETWEEN ? AND ? ORDER BY round_index"""
        with self.connect() as conn:
            rows = conn.execute(query, (int(first_index), int(last_index))).fetchall()
        return {int(row["round_index"]): dict(row) for row in rows}

    def round_continuity_for_indices(self, indices: list[int]) -> dict[int, dict[str, Any]]:
        unique = sorted({int(index) for index in indices})[:5000]
        if not unique:
            return {}
        placeholders = ",".join("?" for _ in unique)
        if self.database_url:
            query = f"""SELECT round_index,round_id,multiplier,COALESCE(stored_at,created_at) AS stored_at,
                timestamp,continuity_verified,gap_before,identity_confidence,continuity_proof,
                platform_round_id,platform_round_index,round_identity_type,round_index_source
                FROM aviator_rounds WHERE round_index IN ({placeholders})"""
        else:
            query = f"""SELECT round_index,round_id,multiplier,NULL AS stored_at,timestamp,
                FALSE AS continuity_verified,FALSE AS gap_before,'UNKNOWN' AS identity_confidence,
                'LEGACY_UNVERIFIED' AS continuity_proof,NULL AS platform_round_id,
                NULL AS platform_round_index,'LEGACY_UNKNOWN' AS round_identity_type,
                'LEGACY_UNKNOWN' AS round_index_source FROM rounds
                WHERE round_index IN ({placeholders})"""
        with self.connect() as conn:
            rows = conn.execute(query, tuple(unique)).fetchall()
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





    def save_application_state(self, key: str, payload: dict[str, Any], updated_at: str) -> None:
        with self.connect() as conn:
            conn.execute("""INSERT INTO application_state(state_key,payload,updated_at) VALUES(?,?,?)
                           ON CONFLICT(state_key) DO UPDATE SET payload=excluded.payload,updated_at=excluded.updated_at""",
                         (key, json.dumps(payload), updated_at))

    def save_rules_engine_snapshot(self, payload: dict[str, Any], updated_at: str) -> None:
        """Persist the engine snapshot and its durable, queryable rule ledger.

        Selection intents are recorded as blocked audit-queue entries. This
        method never authorizes or submits a platform action.
        """
        state = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        with self.connect() as conn:
            conn.execute("""INSERT INTO application_state(state_key,payload,updated_at)
                VALUES('rules_engine_state_v1',?,?) ON CONFLICT(state_key) DO UPDATE SET
                payload=excluded.payload,updated_at=excluded.updated_at""", (state, updated_at))
            configuration_version = int(payload.get("configuration_version") or 0)
            configuration = json.dumps(payload.get("rule_configuration") or {},
                                       sort_keys=True, separators=(",", ":"))
            conn.execute("""INSERT INTO rules_engine_configuration_versions(
                configuration_version,mode,configuration,created_at) VALUES(?,?,?,?)
                ON CONFLICT(configuration_version) DO NOTHING""",
                (configuration_version, "GLOBAL", configuration, updated_at))
            sessions = payload.get("sessions") or {}
            for session_id, session in sessions.items():
                session_payload = json.dumps(session, sort_keys=True, separators=(",", ":"))
                conn.execute("""INSERT INTO rule_sessions(session_id,mode,status,started_at,updated_at,payload)
                    VALUES(?,?,?,?,?,?) ON CONFLICT(session_id) DO UPDATE SET
                    mode=excluded.mode,status=excluded.status,updated_at=excluded.updated_at,
                    payload=excluded.payload""",
                    (str(session_id), session.get("mode", "UNKNOWN"), session.get("status", "UNKNOWN"),
                     session.get("started_at") or updated_at, updated_at, session_payload))
                for evaluation in session.get("evaluations", []):
                    source = evaluation.get("source_round") or {}
                    ev_at = str(evaluation.get("evaluated_at") or updated_at)
                    identity = "\0".join((str(session_id), str(source.get("streamId") or ""),
                                           str(source.get("roundId") or ""), ev_at))
                    evaluation_id = hashlib.sha256(identity.encode("utf-8")).hexdigest()
                    evaluation_payload = json.dumps(evaluation, sort_keys=True, separators=(",", ":"))
                    conn.execute("""INSERT OR IGNORE INTO rule_evaluations(
                        evaluation_id,session_id,source_stream_id,source_round_id,evaluated_at,result,payload)
                        VALUES(?,?,?,?,?,?,?)""",
                        (evaluation_id, str(session_id), source.get("streamId"), source.get("roundId"),
                         ev_at, str(evaluation.get("decision", {}).get("action") or "WAIT"), evaluation_payload))
                    for rule_evaluation in evaluation.get("rule_evaluations", []):
                        rule_source = rule_evaluation.get("source_round") or source
                        source_stream = str(rule_source.get("streamId") or "")
                        source_round = str(rule_source.get("roundId") or "")
                        source_index = rule_source.get("roundIndex")
                        rule_id = str(rule_evaluation.get("rule_id") or "")
                        if (not rule_id or not source_stream or not source_round
                                or isinstance(source_index, bool) or not isinstance(source_index, int)):
                            continue
                        rule_identity = "\0".join((str(session_id), rule_id, source_stream,
                            str(source_index)))
                        rule_evaluation_id = hashlib.sha256(rule_identity.encode("utf-8")).hexdigest()
                        expected = rule_evaluation.get("target_round") or {}
                        source_evidence = json.dumps({
                            "source_round": rule_source,
                            "expected_target": expected,
                            "proposal": rule_evaluation.get("proposal"),
                        }, sort_keys=True, separators=(",", ":"))
                        state_snapshot = json.dumps(rule_evaluation.get("state_snapshot") or {},
                            sort_keys=True, separators=(",", ":"))
                        conn.execute("""INSERT INTO rules_engine_evaluations(
                            evaluation_id,session_id,rule_id,rule_version,source_stream_id,
                            source_round_id,source_round_index,expected_target_index,eligible,
                            status,reason,evaluated_at,source_evidence,state_snapshot)
                            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                            ON CONFLICT(session_id,rule_id,source_stream_id,source_round_index)
                            DO UPDATE SET rule_version=excluded.rule_version,
                            expected_target_index=excluded.expected_target_index,
                            eligible=excluded.eligible,status=excluded.status,reason=excluded.reason,
                            evaluated_at=excluded.evaluated_at,source_evidence=excluded.source_evidence,
                            state_snapshot=excluded.state_snapshot""",
                            (rule_evaluation_id, str(session_id), rule_id,
                             str(rule_evaluation.get("rule_version") or "UNKNOWN"), source_stream,
                             source_round, source_index,
                             expected.get("roundIndex") if isinstance(expected, dict) else None,
                             rule_evaluation.get("eligible"), str(rule_evaluation.get("status") or "UNKNOWN"),
                             str(rule_evaluation.get("reason") or ""), ev_at, source_evidence,
                             state_snapshot))

                selection_rows = []
                for selection in session.get("selections", []):
                    selection_rows.append(("SHARED", selection))
                for rule_id, rows in (session.get("rule_selections") or {}).items():
                    for selection in rows:
                        selection_rows.append((f"INDEPENDENT:{rule_id}", selection))
                for selection_kind, selection in selection_rows:
                    expected = selection.get("expected_target") or {}
                    source = selection.get("source_round") or {}
                    selection_id = str(selection.get("selection_id") or "")
                    if not selection_id:
                        continue
                    selection_payload = {**selection, "selection_kind": selection_kind}
                    conn.execute("""INSERT INTO rule_selections(
                        selection_id,session_id,selection_kind,source_stream_id,source_round_id,
                        target_stream_id,target_round_id,target_round_index,created_at,payload)
                        VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(session_id,selection_id) DO UPDATE SET
                        target_round_id=excluded.target_round_id,payload=excluded.payload""",
                        (selection_id, str(session_id), selection_kind, source.get("streamId"),
                         source.get("roundId"), expected.get("streamId"),
                         (selection.get("actual_target") or {}).get("roundId"),
                         expected.get("roundIndex"), str(selection.get("selected_at") or updated_at),
                         json.dumps(selection_payload, sort_keys=True, separators=(",", ":"))))

                    actual = selection.get("actual_target")
                    if actual or selection.get("actual_multiplier") is not None:
                        outcome_payload = {
                            "selection_id": selection_id,
                            "session_id": str(session_id),
                            "target_round_id": (actual or {}).get("roundId"),
                            "target_round_index": (actual or {}).get("roundIndex") or expected.get("roundIndex"),
                            "multiplier": selection.get("actual_multiplier"),
                            "round_outcome": ("H" if selection.get("actual_multiplier") is not None
                                              and float(selection["actual_multiplier"]) >= 2 else
                                              "L" if selection.get("actual_multiplier") is not None else None),
                            "bet_result": "UNKNOWN",
                            "evidence_type": "CANONICAL_ROUND_RESULT_ONLY",
                            "recorded_at": selection.get("resolved_at") or updated_at,
                        }
                        outcome_id = hashlib.sha256(selection_id.encode("utf-8")).hexdigest()
                        previous_outcome = conn.execute(
                            "SELECT bet_result,reconciled_at,payload FROM rule_target_outcomes WHERE selection_id=?",
                            (selection_id,)).fetchone()
                        if (previous_outcome and previous_outcome["reconciled_at"]
                                and previous_outcome["bet_result"] in {"WIN", "LOSS"}):
                            # Rule-session snapshots may be saved after the platform
                            # receipt. Preserve its financial result while refreshing
                            # the independent canonical-round observation fields.
                            previous_payload = json.loads(previous_outcome["payload"])
                            outcome_payload["bet_result"] = previous_outcome["bet_result"]
                            outcome_payload["financial_result"] = previous_payload.get("financial_result")
                            outcome_payload["evidence_type"] = previous_payload.get("evidence_type")
                        conn.execute("""INSERT INTO rule_target_outcomes(
                            outcome_id,selection_id,session_id,target_round_id,target_round_index,
                            multiplier,round_outcome,bet_result,reconciled_at,payload)
                            VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(selection_id) DO UPDATE SET
                            target_round_id=excluded.target_round_id,target_round_index=excluded.target_round_index,
                            multiplier=excluded.multiplier,round_outcome=excluded.round_outcome,
                            payload=excluded.payload""",
                            (outcome_id, selection_id, str(session_id), outcome_payload["target_round_id"],
                             outcome_payload["target_round_index"], outcome_payload["multiplier"],
                             outcome_payload["round_outcome"], "UNKNOWN", outcome_payload["recorded_at"],
                             json.dumps(outcome_payload, sort_keys=True, separators=(",", ":"))))

                    if selection_kind == "SHARED":
                        target_stream = str(expected.get("streamId") or "")
                        target_index = expected.get("roundIndex")
                        if target_stream and isinstance(target_index, int) and not isinstance(target_index, bool):
                            actual_target = selection.get("actual_target") or {}
                            config = session.get("configuration_snapshot") or {}
                            intent = dict(selection.get("execution_intent") or {})
                            intent.update({
                                "decision_id": selection_id,
                                "decision_timestamp": intent.get("decision_timestamp")
                                    or selection.get("selected_at") or updated_at,
                                "source_round_id": source.get("roundId"),
                                "target_round_id": actual_target.get("roundId") or intent.get("target_round_id"),
                                "provider_target_round_id": actual_target.get("providerRoundId")
                                    or intent.get("provider_target_round_id"),
                                "target_stream_id": target_stream,
                                "target_round_index": target_index,
                                "target_round_ref": actual_target or expected,
                                "target_identity_status": "VERIFIED" if actual_target.get("providerRoundId")
                                    else intent.get("target_identity_status", "UNVERIFIED"),
                                "selected_rule_ids": selection.get("contributing_rule_ids")
                                    or ([selection.get("rule_id")] if selection.get("rule_id") else []),
                                "created_at": intent.get("created_at") or selection.get("selected_at") or updated_at,
                                "expires_at": selection.get("expires_at") or intent.get("expires_at")
                                    or expected.get("expiresAt") or selection.get("resolved_at"),
                                "session_id": str(session_id),
                                "selected_panel": (intent.get("selected_panel")
                                    if type(intent.get("selected_panel")) is int
                                    and intent.get("selected_panel") in {0, 1} else None),
                                "panel": int(intent.get("selected_panel")
                                    if type(intent.get("selected_panel")) is int
                                    and intent.get("selected_panel") in {0, 1}
                                    else intent.get("panel", -1)),
                                "execution_status": intent.get("execution_status") or "BLOCKED_READ_ONLY",
                            })
                            if int(session.get("configuration_schema_version") or 1) >= 2:
                                # New observation sessions do not own stake, cashout,
                                # or selected-panel values. Preserve the separate
                                # operator setup and do not copy legacy bankroll
                                # settings into fresh intent records.
                                for field in ("requested_stake", "requested_stake_currency",
                                              "stake", "currency", "cashout_target"):
                                    intent.pop(field, None)
                            else:
                                # Keep already persisted v1 evidence readable for
                                # history; never use it to configure new sessions.
                                intent.setdefault("requested_stake", config.get("requested_stake",
                                    config.get("fixed_stake", config.get("fixed_stake_rwf", selection.get("stake_rwf")))))
                                intent.setdefault("requested_stake_currency",
                                                  config.get("stake_currency"))
                                intent.setdefault("cashout_target", 2.0)
                            intent.setdefault("decision_id", selection_id)
                            intent.setdefault("session_id", str(session_id))
                            intent.setdefault("selected_rule_ids", selection.get("contributing_rule_ids") or [])
                            if actual_target.get("roundId") and selection.get("resolved_at"):
                                intent["target_completed_at"] = selection.get("resolved_at")
                                intent["execution_eligibility"] = "EXPIRED"
                                if intent.get("execution_status") not in {
                                    "ACCEPTED", "PENDING", "UNKNOWN", "RECONCILIATION_REQUIRED",
                                }:
                                    intent["execution_status"] = "EXPIRED"
                                    blockers = list(intent.get("blockers") or [])
                                    if "TARGET_ALREADY_COMPLETED" not in blockers:
                                        blockers.append("TARGET_ALREADY_COMPLETED")
                                    intent["blockers"] = blockers
                            selection["execution_intent"] = intent
                            if not intent.get("intent_id"):
                                intent["intent_id"] = hashlib.sha256(
                                    (f"{target_stream}\0{target_index}" if int(
                                        session.get("configuration_schema_version") or 1) >= 2
                                     else f"{target_stream}\0{target_index}\0{intent['panel']}").encode("utf-8")
                                ).hexdigest()
                            intent.setdefault("blockers", [
                                "REAL_MONEY_EXECUTION_DISABLED",
                                "EXPLICIT_SESSION_AUTHORIZATION_REQUIRED",
                                "TARGET_ROUND_ID_UNVERIFIED" if not intent["target_round_id"] else
                                    "PLATFORM_ACCEPTANCE_NOT_VERIFIED",
                                "BETTING_DEADLINE_UNVERIFIED",
                                "OPERATOR_STAKE_AND_PANEL_READBACK_REQUIRED",
                                "VERIFIED_BALANCE_AND_SUFFICIENT_FUNDS_REQUIRED",
                                "CASHOUT_PLATFORM_ACKNOWLEDGEMENT_UNVERIFIED",
                                "PLATFORM_ACCEPTANCE_AND_SETTLEMENT_UNVERIFIED",
                            ])
                            intent_json = json.dumps(intent, sort_keys=True, separators=(",", ":"))
                            conn.execute("""INSERT OR IGNORE INTO rule_execution_intents(
                                intent_id,decision_id,selection_id,session_id,source_round_id,
                                target_stream_id,target_round_id,target_round_index,panel,requested_stake,
                                requested_stake_currency,cashout_target,execution_status,created_at,expires_at,payload)
                                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                                (intent["intent_id"], selection_id, selection_id, str(session_id), source.get("roundId"),
                                 target_stream, intent.get("target_round_id"), target_index, intent["panel"],
                                 intent.get("requested_stake"),
                                 intent.get("requested_stake_currency"), intent.get("cashout_target"),
                                 intent["execution_status"],
                                intent["created_at"], intent["expires_at"], intent_json))
                            # The exact completed target identity may become
                            # available after the prospective intent was
                            # recorded. Upgrade only that same immutable intent;
                            # never replace another session's target reservation.
                            conn.execute("""UPDATE rule_execution_intents SET
                                target_round_id=COALESCE(?,target_round_id),
                                expires_at=COALESCE(?,expires_at),
                                panel=?,
                                execution_status=CASE
                                  WHEN execution_status IN ('ACCEPTED','PENDING','UNKNOWN','RECONCILIATION_REQUIRED')
                                    THEN execution_status ELSE ? END,
                                payload=?
                                WHERE intent_id=?""",
                                (intent.get("target_round_id"), intent.get("expires_at"),
                                 intent["panel"], intent["execution_status"], intent_json,
                                 intent["intent_id"]))

                reason = session.get("selection_stop_reason") or session.get("stop_reason")
                stopped_at = ((session.get("selection_stopped_at") or session.get("ended_at")
                               or session.get("stopped_at")) if session.get("selection_stop_reason")
                              else session.get("ended_at") or session.get("stopped_at"))
                if reason and stopped_at:
                    event_id = hashlib.sha256(f"{session_id}\0{reason}\0{stopped_at}".encode("utf-8")).hexdigest()
                    event = {"stop_event_id": event_id, "session_id": str(session_id),
                             "reason": str(reason), "stopped_at": str(stopped_at),
                             "status": session.get("status")}
                    conn.execute("""INSERT OR IGNORE INTO rule_session_stop_events(
                        stop_event_id,session_id,reason,stopped_at,payload) VALUES(?,?,?,?,?)""",
                        (event_id, str(session_id), str(reason), str(stopped_at),
                         json.dumps(event, sort_keys=True, separators=(",", ":"))))
                financials = {
                    "session_id": str(session_id), "starting_balance": None, "current_balance": None,
                    "balance_change": None, "realized_betting_pl": None,
                    "win_count": 0, "loss_count": 0, "currency": None,
                    "updated_at": updated_at,
                    "rule_outcome_counts": {
                        "nonfinancial": True,
                        "wins": session.get("total_wins", 0),
                        "losses": session.get("total_losses", 0),
                    },
                    "actual_betting_financials_status": "NO_RECONCILED_BETS",
                }
                conn.execute("""INSERT INTO rule_session_financials(
                    session_id,starting_balance,current_balance,balance_change,realized_betting_pl,
                    win_count,loss_count,currency,updated_at,payload) VALUES(?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(session_id) DO UPDATE SET updated_at=excluded.updated_at,payload=excluded.payload""",
                    (str(session_id), None, None, None, None, 0, 0, None, updated_at,
                     json.dumps(financials, sort_keys=True, separators=(",", ":"))))

    def record_platform_balance_observation(self, observation: dict[str, Any]) -> dict[str, Any] | None:
        amount = observation.get("balance")
        if amount is None:
            amount = observation.get("displayed_amount")
        if (isinstance(amount, bool) or not isinstance(amount, (int, float))
                or not __import__("math").isfinite(float(amount)) or amount < 0):
            return None
        observation_id = uuid.uuid4().hex
        payload = {**observation, "observation_id": observation_id}
        with self.connect() as conn:
            conn.execute("""INSERT INTO platform_balance_observations(
                observation_id,amount,currency,observed_at,provider_identity,session_identity,
                observation_status,payload) VALUES(?,?,?,?,?,?,?,?)""",
                (observation_id, float(amount), observation.get("currency"),
                 str(observation.get("observed_at") or datetime.now(timezone.utc).isoformat()),
                 observation.get("provider_identity"), observation.get("session_identity"),
                str(observation.get("observation_status") or
                    ("VERIFIED_READ_ONLY" if observation.get("verified") else "UNVERIFIED_READ_ONLY")),
                json.dumps(payload, sort_keys=True, separators=(",", ":"))))
        # These observations are raw read-only evidence only. Do not bind a
        # session baseline or calculate bankroll growth between snapshots;
        # transaction settlement is reconciled from its own receipts.
        return payload

    def latest_platform_balance_observation(self) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT payload FROM platform_balance_observations ORDER BY observed_at DESC LIMIT 1").fetchone()
        return json.loads(row["payload"]) if row else None

    @staticmethod
    def _verified_reconciled_platform_execution(value: dict[str, Any]) -> bool:
        """Require accepted and settled receipt plus same-session verified balances."""
        from app.betting.platform_state import (
            PlatformBetState,
            classify_platform_bet_result,
            classify_platform_bet_state,
        )

        evidence = value.get("platform_evidence") or {}
        result = classify_platform_bet_result(evidence)
        accepted_stake = evidence.get("accepted_stake")
        payout = evidence.get("payout")
        return bool(
            value.get("status") == "RECONCILED" and value.get("simulated") is not True
            and classify_platform_bet_state(evidence) == PlatformBetState.ACCEPTED
            and result in {"WIN", "LOSS"} and value.get("result") == result
            and str(value.get("target_round_id")) == str(evidence.get("target_round_id"))
            and value.get("balance_status") == "VERIFIED"
            and _money_equal(accepted_stake, value.get("bet_amount"))
            and _is_finite_money(payout)
            and _signed_money_equal(value.get("profit_loss"), float(payout) - float(accepted_stake))
            and (evidence.get("balance_currency") or evidence.get("platform_balance_currency"))
                == (evidence.get("accepted_currency") or evidence.get("currency"))
            and evidence.get("balance_session_identity") == evidence.get("session_identity")
            and evidence.get("balance_before_verified") is True
            and bool(evidence.get("balance_provider_identity"))
            and bool(evidence.get("balance_observed_at"))
            and _is_finite_money(evidence.get("platform_observed_balance"))
            and _money_equal(value.get("balance_after"), evidence.get("platform_observed_balance"))
        )

    def _reconciled_session_executions(self, session_id: str) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute("SELECT payload FROM bet_executions WHERE status='RECONCILED'").fetchall()
        outcomes = [json.loads(row["payload"]) for row in rows]
        return [value for value in outcomes
                if str(value.get("session_id") or "") == str(session_id)
                and self._verified_reconciled_platform_execution(value)]

    def reconciled_session_financials(self, session_id: str) -> dict[str, Any]:
        """Aggregate only receipt-backed, balance-reconciled platform transactions."""
        outcomes = self._reconciled_session_executions(session_id)
        profits = [float(row["profit_loss"]) for row in outcomes]
        currencies = {
            str((row.get("platform_evidence") or {}).get("accepted_currency")
                or (row.get("platform_evidence") or {}).get("currency"))
            for row in outcomes
        }
        currency_consistent = len(currencies) == 1
        wins = sum(1 for row in outcomes if row.get("result") == "WIN")
        losses = sum(1 for row in outcomes if row.get("result") == "LOSS")
        return {"realized_betting_pl": sum(profits) if profits and currency_consistent else None,
                "win_count": wins, "loss_count": losses,
                "reconciled_bets": len(outcomes),
                "currency": next(iter(currencies)) if currency_consistent and currencies else None,
                "currency_status": ("VERIFIED" if currency_consistent and currencies else
                                    "MIXED" if currencies else "UNAVAILABLE"),
                "actual_betting_financials_status": "RECONCILED" if outcomes else "NO_RECONCILED_BETS"}

    def rule_execution_intent(self, decision_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT payload FROM rule_execution_intents WHERE decision_id=?",
                               (str(decision_id),)).fetchone()
        return json.loads(row["payload"]) if row else None

    def record_rule_shadow_execution(self, decision_id: str,
                                     observation: dict[str, Any]) -> dict[str, Any] | None:
        """Persist paper-executor state without changing LIVE execution status."""
        if not isinstance(observation, dict):
            return None
        with self.connect() as conn:
            row = conn.execute("SELECT payload FROM rule_execution_intents WHERE decision_id=?",
                               (str(decision_id),)).fetchone()
            if not row:
                return None
            intent = json.loads(row["payload"])
            previous = intent.get("shadow_execution")
            previous = previous if isinstance(previous, dict) else {}
            merged = {**previous, **observation}
            trade_ids = list(dict.fromkeys(
                [*(previous.get("trade_ids") or []), *(observation.get("trade_ids") or [])]))
            if trade_ids:
                merged["trade_ids"] = trade_ids
            intent["shadow_execution"] = merged
            conn.execute("UPDATE rule_execution_intents SET payload=? WHERE decision_id=?",
                         (json.dumps(intent, sort_keys=True, separators=(",", ":")), str(decision_id)))
            return intent

    def _rule_intent_matches_canonical_target(self, intent: dict[str, Any],
                                              target_round_id: str) -> bool:
        """Resolve an as-yet unnamed target only from its committed index/stream."""
        target_index = intent.get("target_round_index")
        stream_id = str(intent.get("target_stream_id") or "")
        if (isinstance(target_index, bool) or not isinstance(target_index, int)
                or not stream_id or not target_round_id):
            return False
        try:
            with self.connect() as conn:
                row = conn.execute("SELECT round_index FROM aviator_rounds WHERE round_id=?",
                                   (str(target_round_id),)).fetchone()
                meta = conn.execute("SELECT stream_id FROM round_distribution_meta WHERE singleton=1").fetchone()
            return bool(row and meta and int(row["round_index"]) == target_index
                        and str(meta["stream_id"]) == stream_id)
        except Exception:
            return False

    def record_rule_execution_reconciliation(self, execution: dict[str, Any],
                                             reconciliation: dict[str, Any]) -> dict[str, Any]:
        """Link one verified receipt to its shared rule selection, idempotently."""
        if (reconciliation.get("status") != "RECONCILED"
                or reconciliation.get("outcome") not in {"WIN", "LOSS"}
                or not self._verified_reconciled_platform_execution(execution)
                or str(reconciliation.get("round_id")) != str(execution.get("target_round_id"))):
            return {"ok": False, "reason": "verified_reconciliation_required"}
        decision_id = str(execution.get("decision_id") or "")
        intent = self.rule_execution_intent(decision_id)
        if not intent:
            return {"ok": False, "reason": "rule_execution_intent_not_found"}
        session_id = str(intent.get("session_id") or "")
        selection_id = str(intent.get("selection_id") or decision_id)
        intent_target_id = str(intent.get("target_round_id") or "")
        if not intent_target_id and self._rule_intent_matches_canonical_target(
                intent, str(execution.get("target_round_id") or "")):
            intent_target_id = str(execution.get("target_round_id"))
            intent["target_round_id"] = intent_target_id
        if (not session_id or str(execution.get("session_id") or "") != session_id
                or str(intent.get("decision_id") or decision_id) != decision_id
                or selection_id != decision_id
                or intent_target_id != str(execution.get("target_round_id") or "")):
            return {"ok": False, "reason": "rule_execution_intent_identity_mismatch"}

        stamp = str(reconciliation.get("created_at") or execution.get("resolved_at") or
                    datetime.now(timezone.utc).isoformat())
        evidence = execution.get("platform_evidence") or {}
        financial_result = {
            "selection_id": selection_id, "session_id": session_id,
            "execution_id": execution.get("execution_id"),
            "target_round_id": execution.get("target_round_id"),
            "result": execution.get("result"), "profit_loss": execution.get("profit_loss"),
            "currency": evidence.get("accepted_currency") or evidence.get("currency"),
            "reconciled_at": stamp,
        }
        with self.connect() as conn:
            existing_row = conn.execute("SELECT bet_result,reconciled_at,payload FROM rule_target_outcomes WHERE selection_id=?",
                                        (selection_id,)).fetchone()
            if (existing_row and existing_row["reconciled_at"]
                    and existing_row["bet_result"] in {"WIN", "LOSS"}):
                saved = json.loads(existing_row["payload"])
                prior = saved.get("financial_result") or {}
                if prior.get("execution_id") != execution.get("execution_id"):
                    return {"ok": False, "reason": "selection_already_has_financial_result"}
                return {"ok": True, "duplicate": True, "session_id": session_id,
                        "financials": self.reconciled_session_financials(session_id)}
            intent["execution_status"] = "RECONCILED"
            intent["platform_execution_id"] = execution.get("execution_id")
            intent["financial_result"] = financial_result
            intent["blockers"] = []
            conn.execute("""UPDATE rule_execution_intents SET execution_status=?,target_round_id=?,payload=?
                WHERE decision_id=?""",
                         ("RECONCILED", intent_target_id,
                          json.dumps(intent, sort_keys=True, separators=(",", ":")), decision_id))
            previous_payload = json.loads(existing_row["payload"]) if existing_row else {}
            target_multiplier = execution.get("result_multiplier")
            outcome_payload = {
                **previous_payload,
                "selection_id": selection_id, "session_id": session_id,
                "target_round_id": execution.get("target_round_id"),
                "target_round_index": intent.get("target_round_index"),
                "multiplier": target_multiplier,
                "round_outcome": ("H" if isinstance(target_multiplier, (int, float))
                                  and target_multiplier >= 2.0 else "L"),
                "bet_result": execution.get("result"), "financial_result": financial_result,
                "evidence_type": "RECEIPT_AND_BALANCE_RECONCILED",
                "recorded_at": stamp,
            }
            outcome_id = hashlib.sha256(selection_id.encode("utf-8")).hexdigest()
            conn.execute("""INSERT INTO rule_target_outcomes(
                outcome_id,selection_id,session_id,target_round_id,target_round_index,
                multiplier,round_outcome,bet_result,reconciled_at,payload)
                VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(selection_id) DO UPDATE SET
                target_round_id=excluded.target_round_id,target_round_index=excluded.target_round_index,
                multiplier=excluded.multiplier,round_outcome=excluded.round_outcome,
                bet_result=excluded.bet_result,reconciled_at=excluded.reconciled_at,payload=excluded.payload""",
                (outcome_id, selection_id, session_id, execution.get("target_round_id"),
                 intent.get("target_round_index"), target_multiplier, outcome_payload["round_outcome"],
                 execution.get("result"), stamp,
                json.dumps(outcome_payload, sort_keys=True, separators=(",", ":"))))
        financials = self.sync_rule_session_financials(session_id, execution)
        return {"ok": True, "duplicate": False, "session_id": session_id,
                "financials": financials}

    def sync_rule_session_financials(self, session_id: str,
                                     execution: dict[str, Any] | None = None) -> dict[str, Any]:
        """Persist verified transaction totals without inventing a start balance."""
        totals = self.reconciled_session_financials(session_id)
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM rule_session_financials WHERE session_id=?",
                               (str(session_id),)).fetchone()
        old = json.loads(row["payload"]) if row else {}
        # Keep any pre-existing baseline fields intact for historical audit,
        # but never derive new balance growth or bankroll movement values.
        starting = old.get("starting_balance", row["starting_balance"] if row else None)
        starting_currency = old.get("starting_balance_currency")
        current = old.get("current_balance", row["current_balance"] if row else None)
        current_currency = old.get("current_balance_currency")
        balance_change = old.get("balance_change", row["balance_change"] if row else None)
        updated_at = old.get("updated_at")
        evidence = (execution or {}).get("platform_evidence") or {}
        if execution and _is_finite_money(execution.get("balance_after")) \
                and execution.get("balance_status") == "VERIFIED":
            current = float(execution["balance_after"])
            current_currency = evidence.get("balance_currency") or evidence.get("accepted_currency")
            updated_at = evidence.get("balance_observed_at") or execution.get("resolved_at")
        row_payload = {
            **old, **totals, "session_id": str(session_id),
            "starting_balance": starting, "starting_balance_currency": starting_currency,
            "current_balance": current, "current_balance_currency": current_currency,
            "balance_change": balance_change,
            "currency": totals.get("currency") or current_currency,
            "updated_at": updated_at or datetime.now(timezone.utc).isoformat(),
        }
        with self.connect() as conn:
            conn.execute("""INSERT INTO rule_session_financials(
                session_id,starting_balance,current_balance,balance_change,realized_betting_pl,
                win_count,loss_count,currency,updated_at,payload) VALUES(?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(session_id) DO UPDATE SET
                starting_balance=excluded.starting_balance,current_balance=excluded.current_balance,
                balance_change=excluded.balance_change,realized_betting_pl=excluded.realized_betting_pl,
                win_count=excluded.win_count,loss_count=excluded.loss_count,currency=excluded.currency,
                updated_at=excluded.updated_at,payload=excluded.payload""",
                (str(session_id), starting, current, balance_change, totals.get("realized_betting_pl"),
                 totals["win_count"], totals["loss_count"], row_payload.get("currency"),
                row_payload["updated_at"], json.dumps(row_payload, sort_keys=True, separators=(",", ":"))))
        return row_payload

    def bind_rule_session_starting_balance(self, session_id: str,
                                           observation: dict[str, Any]) -> dict[str, Any] | None:
        """Retired: platform observations do not create bankroll baselines."""
        del session_id, observation
        return None

    def list_reconciled_rule_executions(self, session_id: str) -> list[dict[str, Any]]:
        result = []
        for execution in self._reconciled_session_executions(session_id):
            intent = self.rule_execution_intent(str(execution.get("decision_id") or ""))
            target_matches = bool(intent and (
                str(intent.get("target_round_id") or "") == str(execution.get("target_round_id"))
                or self._rule_intent_matches_canonical_target(
                    intent, str(execution.get("target_round_id") or ""))))
            if intent and str(intent.get("session_id")) == str(session_id) and target_matches:
                result.append(execution)
        return result

    def mark_rule_execution_pending(self, execution: dict[str, Any], reason: str) -> dict[str, Any] | None:
        decision_id = str(execution.get("decision_id") or "")
        intent = self.rule_execution_intent(decision_id)
        target_matches = bool(intent and (
            str(intent.get("target_round_id") or "") == str(execution.get("target_round_id") or "")
            or self._rule_intent_matches_canonical_target(
                intent, str(execution.get("target_round_id") or ""))))
        if (not intent or str(intent.get("session_id") or "") != str(execution.get("session_id") or "")
                or not target_matches):
            return None
        if intent.get("execution_status") == "RECONCILED":
            return None
        intent.update({"execution_status": "RECONCILIATION_PENDING",
                       "platform_execution_id": execution.get("execution_id"),
                       "reconciliation_blocker": str(reason)})
        with self.connect() as conn:
            conn.execute("UPDATE rule_execution_intents SET execution_status=?,payload=? WHERE decision_id=?",
                         ("RECONCILIATION_PENDING",
                          json.dumps(intent, sort_keys=True, separators=(",", ":")), decision_id))
        return intent

    def has_open_rule_session_executions(self, session_id: str) -> bool:
        for execution in self.open_executions():
            intent = self.rule_execution_intent(str(execution.get("decision_id") or ""))
            if intent and str(intent.get("session_id") or "") == str(session_id):
                return True
        return False

    def rules_session_financials(self, session_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT payload FROM rule_session_financials WHERE session_id=?",
                               (str(session_id),)).fetchone()
        return json.loads(row["payload"]) if row else None

    def list_rule_execution_intents(self, limit: int = 100) -> list[dict[str, Any]]:
        bounded = max(1, min(int(limit), 1000))
        with self.connect() as conn:
            rows = conn.execute("SELECT payload FROM rule_execution_intents ORDER BY created_at DESC LIMIT ?",
                                (bounded,)).fetchall()
        return [json.loads(row["payload"]) for row in rows]

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















    def decision_audit(self, decision_id: str) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute("SELECT payload FROM decision_audit WHERE decision_id = ? ORDER BY id", (decision_id,)).fetchall()
        return [json.loads(row["payload"]) for row in rows]

    def list_decision_audit_snapshots(self, limit: int = 5000) -> list[dict[str, Any]]:
        bounded = max(1, min(int(limit), 10000))
        with self.connect() as conn:
            rows = conn.execute("SELECT payload FROM decision_audit ORDER BY id DESC LIMIT ?", (bounded,)).fetchall()
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
    def start_demo_rule_session(self, session: dict[str, Any]) -> dict[str, Any]:
        session_id = str(session["session_id"])
        now = str(session["started_at"])
        encoded_rules = json.dumps(session.get("enabled_rule_ids") or [])
        payload = _demo_session_storage_payload(session)
        with self.connect() as conn:
            conn.execute("INSERT INTO demo_rule_sessions(session_id,environment,status,enabled_rule_ids,started_at,updated_at,stopped_at,expires_at,payload) VALUES(?,?,?,?,?,?,?,?,?)",
                         (session_id, "DEMO", "START", encoded_rules, now, now, None,
                          str(session["expires_at"]), payload))
            for event_type in ("START", "ACTIVE"):
                event = {"session_id": session_id, "environment": "DEMO", "event_type": event_type,
                         "recorded_at": now}
                conn.execute("INSERT INTO demo_session_events(event_id,session_id,event_type,recorded_at,payload) VALUES(?,?,?,?,?)",
                             (f"{session_id}:{event_type}", session_id, event_type, now,
                              json.dumps(event, sort_keys=True, separators=(",", ":"))))
            session["status"] = "ACTIVE"
            payload = _demo_session_storage_payload(session)
            conn.execute("UPDATE demo_rule_sessions SET status='ACTIVE',payload=? WHERE session_id=?",
                         (payload, session_id))
        return session

    def update_demo_rule_session(self, session: dict[str, Any], event_type: str | None = None,
                                 evidence: dict[str, Any] | None = None) -> None:
        session_id = str(session["session_id"])
        now = str(session.get("updated_at") or datetime.now(timezone.utc).isoformat())
        with self.connect() as conn:
            conn.execute("UPDATE demo_rule_sessions SET status=?,updated_at=?,stopped_at=?,expires_at=?,payload=? WHERE session_id=?",
                         (str(session["status"]), now, session.get("stopped_at"), str(session["expires_at"]),
                         _demo_session_storage_payload(session), session_id))
            if event_type:
                event_id = hashlib.sha256(f"{session_id}\0{event_type}\0{now}".encode()).hexdigest()
                payload = {"session_id": session_id, "environment": "DEMO", "event_type": event_type,
                           "recorded_at": now, "evidence": evidence or {}}
                conn.execute("INSERT INTO demo_session_events(event_id,session_id,event_type,recorded_at,payload) VALUES(?,?,?,?,?) ON CONFLICT(event_id) DO NOTHING",
                             (event_id, session_id, event_type, now,
                              json.dumps(payload, sort_keys=True, separators=(",", ":"))))

    def demo_rule_session(self, session_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT payload FROM demo_rule_sessions WHERE session_id=?", (str(session_id),)).fetchone()
        return json.loads(row["payload"]) if row else None

    def list_demo_rule_sessions(self, limit: int = 50) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute("SELECT payload FROM demo_rule_sessions ORDER BY started_at DESC LIMIT ?",
                                (max(1, int(limit)),)).fetchall()
        return [json.loads(row["payload"]) for row in rows]

    def recover_demo_rule_sessions(self, now: str) -> int:
        with self.connect() as conn:
            rows = conn.execute("SELECT session_id,payload FROM demo_rule_sessions WHERE status='ACTIVE'").fetchall()
            for row in rows:
                session = json.loads(row["payload"])
                session.update(status="RECOVERY", updated_at=now,
                               recovery_reason="BACKEND_RESTART_REQUIRES_EXPLICIT_RECOVERY")
                conn.execute("UPDATE demo_rule_sessions SET status='RECOVERY',updated_at=?,payload=? WHERE session_id=?",
                             (now, _demo_session_storage_payload(session), row["session_id"]))
                event = {"session_id": row["session_id"], "environment": "DEMO", "event_type": "RECOVERY",
                         "recorded_at": now, "evidence": {"reason": session["recovery_reason"]}}
                event_id = hashlib.sha256(f"{row['session_id']}\0RECOVERY\0{now}".encode()).hexdigest()
                conn.execute("INSERT INTO demo_session_events(event_id,session_id,event_type,recorded_at,payload) VALUES(?,?,?,?,?) ON CONFLICT(event_id) DO NOTHING",
                             (event_id, row["session_id"], "RECOVERY", now,
                              json.dumps(event, sort_keys=True, separators=(",", ":"))))
        return len(rows)

    def save_demo_evaluation(self, session: dict[str, Any], evaluations: list[dict[str, Any]],
                             decision: dict[str, Any], attempt: dict[str, Any] | None = None) -> bool:
        session_id = str(session["session_id"])
        source = decision["source_round"]
        source_stream_id = str(source.get("stream_id") or source.get("streamId") or "")
        source_sequence = int(source.get("stream_sequence") or source.get("streamSequence") or 0)
        source_round_id = str(source.get("round_id") or source.get("roundId") or "")
        source_round_index = int(source.get("round_index") or source.get("roundIndex") or 0)
        with self.connect() as conn:
            for evaluation in evaluations:
                key = f"{session_id}\0{evaluation['rule_id']}\0{source_stream_id}\0{source_sequence}"
                evaluation_id = hashlib.sha256(key.encode()).hexdigest()
                conn.execute("""INSERT INTO demo_rule_evaluations(
                    evaluation_id,session_id,rule_id,rule_version,source_stream_id,source_sequence,
                    source_round_id,source_round_index,eligible,status,evaluated_at,payload)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(session_id,rule_id,source_stream_id,source_sequence) DO NOTHING""",
                    (evaluation_id, session_id, str(evaluation["rule_id"]), str(evaluation.get("rule_version") or ""),
                     source_stream_id, source_sequence, source_round_id,
                     source_round_index, evaluation.get("eligible"), str(evaluation.get("status") or "UNKNOWN"),
                     str(decision["created_at"]), json.dumps(evaluation, sort_keys=True, separators=(",", ":"))))
            decision_result = conn.execute("""INSERT INTO demo_rule_decisions(
                decision_id,session_id,source_stream_id,source_sequence,source_round_id,source_round_index,
                target_round_index,action,created_at,payload) VALUES(?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(session_id,source_stream_id,source_sequence) DO NOTHING RETURNING decision_id""",
                (str(decision["decision_id"]), session_id, source_stream_id,
                 source_sequence, source_round_id, source_round_index,
                 decision.get("target_round_index"), str(decision["action"]), str(decision["created_at"]),
                 json.dumps(decision, sort_keys=True, separators=(",", ":")))).fetchone()
            if not decision_result:
                return False
            if attempt:
                conn.execute("""INSERT INTO demo_execution_attempts(
                    attempt_id,session_id,decision_id,target_round_id,target_round_index,state,reason,
                    created_at,updated_at,payload) VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(decision_id) DO NOTHING""",
                    (str(attempt["attempt_id"]), session_id, str(attempt["decision_id"]),
                     attempt.get("target_round_id"), attempt.get("target_round_index"),
                     str(attempt["state"]), str(attempt.get("reason") or ""), str(attempt["created_at"]),
                     str(attempt["updated_at"]), json.dumps(attempt, sort_keys=True, separators=(",", ":"))))
            conn.execute("UPDATE demo_rule_sessions SET updated_at=?,payload=? WHERE session_id=?",
                         (str(session["updated_at"]), _demo_session_storage_payload(session), session_id))
        return True

    def update_demo_execution_attempt(self, attempt: dict[str, Any]) -> None:
        with self.connect() as conn:
            conn.execute("UPDATE demo_execution_attempts SET state=?,reason=?,updated_at=?,payload=? WHERE attempt_id=?",
                         (str(attempt["state"]), attempt.get("reason"), str(attempt["updated_at"]),
                          json.dumps(attempt, sort_keys=True, separators=(",", ":")), str(attempt["attempt_id"])))

    def claim_demo_execution_attempt(self, now: str) -> dict[str, Any] | None:
        """Claim one eligible Demo attempt exactly once before its browser action."""
        with self.connect() as conn:
            rows = conn.execute("""SELECT a.attempt_id,a.payload FROM demo_execution_attempts a
                JOIN demo_rule_sessions s ON s.session_id=a.session_id
                WHERE a.state='EXECUTION_ELIGIBLE' AND s.environment='DEMO' AND s.status='ACTIVE'
                ORDER BY a.created_at LIMIT 100""").fetchall()
            for row in rows:
                attempt = json.loads(row["payload"])
                if attempt.get("dispatch_claimed_at"):
                    continue
                token = uuid.uuid4().hex
                attempt.update(dispatch_claim_token=token, dispatch_claimed_at=now,
                               dispatch_state="CLAIMED", updated_at=now)
                encoded = json.dumps(attempt, sort_keys=True, separators=(",", ":"))
                changed = conn.execute("""UPDATE demo_execution_attempts SET updated_at=?,payload=?
                    WHERE attempt_id=? AND state='EXECUTION_ELIGIBLE' AND payload=?""",
                    (now, encoded, str(row["attempt_id"]), row["payload"])).rowcount
                if changed == 1:
                    return attempt
        return None

    def complete_demo_execution_attempt(self, attempt: dict[str, Any], token: str) -> bool:
        """Persist a Demo adapter result only for its one-time claim token."""
        attempt_id = str(attempt.get("attempt_id") or "")
        now = str(attempt.get("updated_at") or datetime.now(timezone.utc).isoformat())
        with self.connect() as conn:
            row = conn.execute("SELECT payload,state FROM demo_execution_attempts WHERE attempt_id=?",
                               (attempt_id,)).fetchone()
            if not row:
                return False
            current = json.loads(row["payload"])
            if (not token or current.get("dispatch_claim_token") != token
                    or current.get("dispatch_state") != "CLAIMED"):
                return False
            if current.get("state") not in {"EXECUTION_ELIGIBLE", "UNKNOWN"}:
                return False
            merged = {**current, **attempt, "dispatch_state": "COMPLETED", "updated_at": now}
            conn.execute("UPDATE demo_execution_attempts SET state=?,reason=?,updated_at=?,payload=? WHERE attempt_id=?",
                         (str(merged.get("state") or "UNKNOWN"), merged.get("reason"), now,
                          json.dumps(merged, sort_keys=True, separators=(",", ":")), attempt_id))
            return True

    def expire_demo_execution_claims(self, before: str) -> int:
        """Fail closed when the browser owner disappears after a one-time claim."""
        expired = 0
        with self.connect() as conn:
            rows = conn.execute("SELECT attempt_id,payload FROM demo_execution_attempts WHERE state='EXECUTION_ELIGIBLE'").fetchall()
            for row in rows:
                attempt = json.loads(row["payload"])
                claimed_at = str(attempt.get("dispatch_claimed_at") or "")
                if not claimed_at or claimed_at > before or attempt.get("dispatch_state") != "CLAIMED":
                    continue
                attempt.update(state="UNKNOWN", reason="DEMO_ACTION_RESULT_NOT_RECONCILED",
                               dispatch_state="EXPIRED", updated_at=before,
                               blockers=["DEMO_ACTION_RESULT_NOT_RECONCILED"])
                attempt.setdefault("transitions", []).append({"state": "UNKNOWN", "at": before,
                    "reason": attempt["reason"]})
                conn.execute("UPDATE demo_execution_attempts SET state='UNKNOWN',reason=?,updated_at=?,payload=? WHERE attempt_id=? AND state='EXECUTION_ELIGIBLE'",
                             (attempt["reason"], before,
                              json.dumps(attempt, sort_keys=True, separators=(",", ":")),
                              str(row["attempt_id"])))
                expired += 1
        return expired

    def record_demo_panel_observations(self, session_id: str | None,
                                       observations: list[dict[str, Any]]) -> None:
        with self.connect() as conn:
            for row in observations:
                panel_id = str(row.get("panel_id") or "")
                if panel_id not in {"PANEL_1", "PANEL_2"}:
                    continue
                observed_at = str(row.get("observed_at") or datetime.now(timezone.utc).isoformat())
                identity = str(row.get("observation_id") or hashlib.sha256(
                    f"{session_id}\0{panel_id}\0{observed_at}".encode()).hexdigest())
                conn.execute("INSERT INTO demo_panel_observations(observation_id,session_id,panel_id,observed_at,state,payload) VALUES(?,?,?,?,?,?) ON CONFLICT(observation_id) DO NOTHING",
                             (identity, session_id, panel_id, observed_at,
                              str(row.get("state") or "UNKNOWN"),
                              json.dumps(row, sort_keys=True, separators=(",", ":"))))

    def demo_session_events(self, session_id: str, limit: int = 500) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute("SELECT payload FROM demo_session_events WHERE session_id=? ORDER BY recorded_at DESC LIMIT ?",
                                (str(session_id), max(1, int(limit)))).fetchall()
        return [json.loads(row["payload"]) for row in rows]

    def demo_session_evaluations(self, session_id: str, limit: int = 500) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute("SELECT payload FROM demo_rule_evaluations WHERE session_id=? ORDER BY source_sequence DESC,rule_id LIMIT ?",
                                (str(session_id), max(1, int(limit)))).fetchall()
        return [json.loads(row["payload"]) for row in reversed(rows)]

    def demo_session_decisions(self, session_id: str, limit: int = 500) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute("SELECT payload FROM demo_rule_decisions WHERE session_id=? ORDER BY created_at DESC LIMIT ?",
                                (str(session_id), max(1, int(limit)))).fetchall()
        return [json.loads(row["payload"]) for row in reversed(rows)]

    def demo_execution_attempts(self, session_id: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        with self.connect() as conn:
            if session_id:
                rows = conn.execute("SELECT payload FROM demo_execution_attempts WHERE session_id=? ORDER BY created_at DESC LIMIT ?",
                                    (str(session_id), max(1, int(limit)))).fetchall()
            else:
                rows = conn.execute("SELECT payload FROM demo_execution_attempts ORDER BY created_at DESC LIMIT ?",
                                    (max(1, int(limit)),)).fetchall()
        return [json.loads(row["payload"]) for row in rows]
