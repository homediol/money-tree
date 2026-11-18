from __future__ import annotations

import json
import hashlib
import os
import re
import sqlite3
import uuid
from collections import deque
from contextlib import nullcontext
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
import time
from typing import Any

MAX_COLLECTOR_EVENT_ATTEMPTS = 6
RETIRED_EXECUTION_TABLES = (
    "decision_audit",
    "bet_executions",
    "execution_events",
    "balance_ledger",
    "reconciliation_records",
    "session_metrics",
    "live_sessions",
    "betting_mode_state",
    "rule_execution_intents",
    "platform_balance_observations",
    "rule_session_financials",
)


def _strip_retired_financial_outcomes(conn) -> None:
    """Keep canonical SHADOW outcomes while removing retired platform receipts."""
    rows = conn.execute("SELECT selection_id,payload FROM rule_target_outcomes").fetchall()
    for row in rows:
        try:
            payload = row["payload"]
            payload = json.loads(payload) if isinstance(payload, str) else dict(payload or {})
        except (TypeError, ValueError, json.JSONDecodeError):
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        for key in ("financial_result", "bet_result", "execution_id", "platform_execution_id",
                    "platform_evidence", "balance_evidence"):
            payload.pop(key, None)
        payload["evidence_type"] = "CANONICAL_ROUND_RESULT_ONLY"
        conn.execute("UPDATE rule_target_outcomes SET payload=? WHERE selection_id=?",
            (json.dumps(payload, sort_keys=True, separators=(",", ":")),
             str(row["selection_id"])))


def _drop_retired_outcome_columns(conn, *, postgres: bool) -> None:
    if postgres:
        columns = {row["column_name"] for row in conn.execute("""SELECT column_name
            FROM information_schema.columns
            WHERE table_schema=current_schema() AND table_name='rule_target_outcomes'""").fetchall()}
    else:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(rule_target_outcomes)").fetchall()}
    for name in ("bet_result", "reconciled_at"):
        if name in columns:
            conn.execute(f"ALTER TABLE rule_target_outcomes DROP COLUMN {name}")

SCHEMA = """
CREATE TABLE IF NOT EXISTS rounds (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  round_id TEXT,
  round_index INTEGER UNIQUE NOT NULL,
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
CREATE TABLE IF NOT EXISTS bet_signals (
  signal_id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL,
  round_id TEXT NOT NULL,
  target_round_id TEXT NOT NULL,
  created_at TEXT NOT NULL,
  settlement_status TEXT NOT NULL DEFAULT 'PENDING',
  payload TEXT NOT NULL,
  UNIQUE(session_id,target_round_id)
);
CREATE INDEX IF NOT EXISTS bet_signals_created_idx ON bet_signals(created_at DESC);
CREATE INDEX IF NOT EXISTS bet_signals_target_round_created_idx
  ON bet_signals(target_round_id,created_at,signal_id);
CREATE TABLE IF NOT EXISTS behaviour_strategy_settlement_outbox (
  signal_id TEXT PRIMARY KEY,
  status TEXT NOT NULL DEFAULT 'PENDING',
  payload TEXT NOT NULL,
  attempts INTEGER NOT NULL DEFAULT 0,
  last_error TEXT,
  created_at TEXT NOT NULL,
  delivered_at TEXT
);
CREATE INDEX IF NOT EXISTS behaviour_strategy_settlement_pending_idx
  ON behaviour_strategy_settlement_outbox(status,created_at);
CREATE TABLE IF NOT EXISTS rule_target_outcomes (
  outcome_id TEXT PRIMARY KEY,
  selection_id TEXT NOT NULL UNIQUE,
  session_id TEXT NOT NULL,
  target_round_id TEXT,
  target_round_index BIGINT,
  multiplier NUMERIC,
  round_outcome TEXT,
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
    def __init__(self, raw, *, release=None):
        self.raw = raw
        self._release = release

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
        try:
            return self.raw.__exit__(exc_type, exc, tb)
        finally:
            if self._release is not None:
                self._release(self.raw)
                self._release = None

    def __getattr__(self, name):
        return getattr(self.raw, name)


class Repository:
    def __init__(self, database_path: Path, database_url: str | None = None,
                 require_postgres: bool = False):
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.database_url = database_url or os.getenv("DATABASE_URL") or os.getenv("POSTGRES_URL") or os.getenv("WINNER_DATABASE_URL")
        self.require_postgres = bool(require_postgres or os.getenv("REQUIRE_POSTGRES", "false").lower() == "true")
        self._pg_pool = None
        self._pg_pool_lock = Lock()
        self._rules_engine_snapshot_lock = Lock()
        self._pool_wait_samples_ms = deque(maxlen=1000)
        self._pool_metrics_lock = Lock()
        # Snapshot persistence still refreshes the authoritative session state
        # on every round, but immutable ledger rows are written only when their
        # contents change. Replaying up to 500 historical evaluations per
        # session on every round was producing thousands of redundant PG
        # statements and starving collector/status requests.
        self._rules_engine_ledger_fingerprints: dict[tuple[str, str], str] = {}
        # Rule-evaluation children are immutable once created. The small
        # parent signature lets persistence detect commit upgrades without
        # re-encoding every nested historical rule result on every round.
        self._rules_engine_evaluation_states: dict[str, str] = {}
        if self.require_postgres and not self.database_url:
            raise RuntimeError("REQUIRE_POSTGRES is enabled but DATABASE_URL is not configured")

    @staticmethod
    def _rules_engine_fingerprint(value: Any) -> str:
        # Callers that persist ledger rows already encode their payloads once.
        # Hash that canonical JSON string directly instead of JSON-encoding the
        # string again (which hashes a quoted/escaped wrapper and wastes CPU).
        canonical = (value if isinstance(value, str) else
                     json.dumps(value, sort_keys=True, separators=(",", ":"), default=str))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @staticmethod
    def _rules_engine_evaluation_state(evaluation: dict[str, Any]) -> str:
        projection = {
            "evaluated_at": evaluation.get("evaluated_at"),
            "source_round": evaluation.get("source_round"),
            "target_round": evaluation.get("target_round"),
            "decision": evaluation.get("decision"),
            "persistence_status": evaluation.get("persistence_status"),
            "committed_stream_sequence": evaluation.get("committed_stream_sequence"),
            "commit_evidence": evaluation.get("commit_evidence"),
        }
        encoded = json.dumps(projection, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def prime_rules_engine_ledger_cache(self, payload: dict[str, Any]) -> bool:
        """Prime existing ledger rows and report sessions missing from the DB."""
        fingerprints = self._rules_engine_ledger_fingerprints
        with self.connect() as conn:
            persisted_session_ids = {str(row["session_id"])
                for row in conn.execute("SELECT session_id FROM rule_sessions").fetchall()}
        missing_session = False
        for session_id, session in (payload.get("sessions") or {}).items():
            session_id = str(session_id)
            session_payload = json.dumps(session, sort_keys=True, separators=(",", ":"))
            session_key = ("session", session_id)
            session_persisted = session_id in persisted_session_ids
            if session_persisted:
                fingerprints[session_key] = self._rules_engine_fingerprint(session_payload)
            else:
                # Legacy snapshots may contain sessions that were never
                # copied to the queryable ledger. Leave the whole session's
                # ledger unprimed so startup persistence writes it to the DB.
                fingerprints.pop(session_key, None)
                missing_session = True
            for evaluation in session.get("evaluations", []):
                source = evaluation.get("source_round") or {}
                evaluated_at = str(evaluation.get("evaluated_at") or "")
                identity = "\0".join((str(session_id), str(source.get("streamId") or ""),
                                       str(source.get("roundId") or ""), evaluated_at))
                evaluation_id = hashlib.sha256(identity.encode("utf-8")).hexdigest()
                if session_persisted:
                    self._rules_engine_evaluation_states[evaluation_id] = (
                        self._rules_engine_evaluation_state(evaluation))
                else:
                    self._rules_engine_evaluation_states.pop(evaluation_id, None)
                evaluation_payload = json.dumps(evaluation, sort_keys=True, separators=(",", ":"))
                evaluation_key = ("evaluation", evaluation_id)
                if session_persisted:
                    fingerprints[evaluation_key] = self._rules_engine_fingerprint(evaluation_payload)
                else:
                    fingerprints.pop(evaluation_key, None)
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
                    row_payload = {
                        "rule_version": str(rule_evaluation.get("rule_version") or "UNKNOWN"),
                        "expected_target_index": expected.get("roundIndex") if isinstance(expected, dict) else None,
                        "eligible": rule_evaluation.get("eligible"),
                        "status": str(rule_evaluation.get("status") or "UNKNOWN"),
                        "reason": str(rule_evaluation.get("reason") or ""),
                        "evaluated_at": evaluated_at,
                        "source_evidence": {
                            "source_round": rule_source,
                            "expected_target": expected,
                            "proposal": rule_evaluation.get("proposal"),
                        },
                        "state_snapshot": rule_evaluation.get("state_snapshot") or {},
                    }
                    rule_evaluation_key = ("rule_evaluation", rule_evaluation_id)
                    if session_persisted:
                        fingerprints[rule_evaluation_key] = self._rules_engine_fingerprint(row_payload)
                    else:
                        fingerprints.pop(rule_evaluation_key, None)
        return missing_session

    def connect(self):
        if self.database_url:
            try:
                pool = self._postgres_pool()
                started = time.perf_counter()
                raw = pool.getconn(timeout=float(os.getenv("WINNER_DB_POOL_ACQUIRE_TIMEOUT_SECONDS", "3")))
                elapsed_ms = (time.perf_counter() - started) * 1000.0
                with self._pool_metrics_lock:
                    self._pool_wait_samples_ms.append(elapsed_ms)
                return _PostgresConnection(raw, release=pool.putconn)
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

    def _postgres_pool(self):
        pool = self._pg_pool
        if pool is not None:
            return pool
        with self._pg_pool_lock:
            if self._pg_pool is not None:
                return self._pg_pool
            from psycopg.rows import dict_row
            from psycopg_pool import ConnectionPool

            minimum = max(1, int(os.getenv("WINNER_DB_POOL_MIN_SIZE", "1")))
            maximum = max(minimum, int(os.getenv("WINNER_DB_POOL_MAX_SIZE", "12")))
            timeout = max(1.0, float(os.getenv("WINNER_DB_POOL_ACQUIRE_TIMEOUT_SECONDS", "3")))
            pool = ConnectionPool(
                conninfo=self.database_url,
                min_size=minimum,
                max_size=maximum,
                timeout=timeout,
                kwargs={"row_factory": dict_row},
                open=True,
            )
            pool.wait(timeout=timeout)
            self._pg_pool = pool
            return pool

    def connection_pool_metrics(self) -> dict[str, Any]:
        """Expose pool wait samples and current capacity without querying PostgreSQL."""
        with self._pool_metrics_lock:
            values = sorted(self._pool_wait_samples_ms)
        def percentile(fraction: float):
            if not values:
                return None
            import math
            return round(values[min(len(values) - 1, max(0, math.ceil(fraction * len(values)) - 1))], 3)
        pool_stats = self._pg_pool.get_stats() if self._pg_pool is not None else {}
        return {
            "samples": len(values),
            "average_ms": round(sum(values) / len(values), 3) if values else None,
            "p50_ms": percentile(0.50), "p95_ms": percentile(0.95),
            "p99_ms": percentile(0.99), "max_ms": round(values[-1], 3) if values else None,
            "pool": {key: pool_stats.get(key) for key in (
                "pool_size", "pool_available", "requests_waiting", "requests_queued",
                "requests_wait_ms", "requests_num") if key in pool_stats},
        }

    def close(self) -> None:
        pool = self._pg_pool
        self._pg_pool = None
        if pool is not None:
            pool.close()

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
                for table in RETIRED_EXECUTION_TABLES:
                    conn.execute(f"DROP TABLE IF EXISTS {table}")
                conn.executescript(SCHEMA)
                _strip_retired_financial_outcomes(conn)
                _drop_retired_outcome_columns(conn, postgres=True)
                conn.execute("DELETE FROM application_state WHERE state_key IN ('operational_safety','risk_runtime')")
                conn.execute("ALTER TABLE bet_signals ADD COLUMN IF NOT EXISTS settlement_status TEXT NOT NULL DEFAULT 'PENDING'")
                conn.execute("UPDATE bet_signals SET settlement_status='COMMITTED' WHERE settlement_status='PENDING' AND payload::jsonb->>'settlement_status'='COMMITTED'")
                conn.execute("CREATE INDEX IF NOT EXISTS bet_signals_settlement_created_idx ON bet_signals(settlement_status,created_at)")
                conn.execute("CREATE INDEX IF NOT EXISTS bet_signals_target_round_created_idx ON bet_signals(target_round_id,created_at,signal_id)")
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
                conn.execute("UPDATE aviator_rounds SET stored_at=created_at WHERE stored_at IS NULL")
                conn.execute("UPDATE aviator_rounds SET local_round_index=round_index WHERE local_round_index IS NULL")
                conn.execute("UPDATE aviator_rounds SET observed_at=timestamp WHERE observed_at IS NULL AND timestamp IS NOT NULL AND round_id ~ '^aviator-[0-9a-f]{24}$'")
                conn.execute("UPDATE aviator_rounds SET round_identity_type=COALESCE(round_identity_type,'COLLECTOR_OBSERVATION_HASH'), round_index_source=COALESCE(round_index_source,'LOCAL_SEQUENCE'), identity_confidence=COALESCE(identity_confidence,CASE WHEN continuity_verified THEN 'OVERLAP_VERIFIED_ORDER_ONLY' ELSE 'UNKNOWN' END), continuity_proof=COALESCE(continuity_proof,'LEGACY_UNVERIFIED') WHERE round_identity_type IS NULL OR round_index_source IS NULL OR identity_confidence IS NULL OR continuity_proof IS NULL")
                conn.execute("UPDATE aviator_rounds SET platform_timestamp=NULL WHERE platform_timestamp IS NOT NULL AND round_id ~ '^aviator-[0-9a-f]{24}$'")
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
                conn.execute("CREATE INDEX IF NOT EXISTS round_distribution_events_stream_sequence_idx ON round_distribution_events(stream_id,stream_sequence)")
                conn.execute("DELETE FROM round_distribution_events WHERE stream_id<>(SELECT stream_id FROM round_distribution_meta WHERE singleton=1)")
                conn.execute("""CREATE TABLE IF NOT EXISTS external_project_tokens (
                    token_id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    label TEXT NOT NULL DEFAULT '',
                    token_hash TEXT NOT NULL UNIQUE,
                    token_prefix TEXT NOT NULL,
                    key_type TEXT NOT NULL DEFAULT 'consumer',
                    scopes TEXT NOT NULL DEFAULT '[\"signals:read\",\"rules:read\",\"metrics:read\"]',
                    console_managed BOOLEAN NOT NULL DEFAULT FALSE,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    last_used_at TIMESTAMPTZ,
                    expires_at TIMESTAMPTZ,
                    revoked_at TIMESTAMPTZ
                )""")
                conn.execute("ALTER TABLE external_project_tokens ADD COLUMN IF NOT EXISTS key_type TEXT NOT NULL DEFAULT 'consumer'")
                conn.execute("ALTER TABLE external_project_tokens ADD COLUMN IF NOT EXISTS scopes TEXT NOT NULL DEFAULT '[\"signals:read\",\"rules:read\",\"metrics:read\"]'")
                conn.execute("ALTER TABLE external_project_tokens ADD COLUMN IF NOT EXISTS console_managed BOOLEAN NOT NULL DEFAULT FALSE")
                conn.execute("ALTER TABLE external_project_tokens ADD COLUMN IF NOT EXISTS last_used_at TIMESTAMPTZ")
                conn.execute("ALTER TABLE external_project_tokens ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ")
                conn.execute("""CREATE TABLE IF NOT EXISTS api_key_audit_log (
                    audit_id TEXT PRIMARY KEY, action TEXT NOT NULL, token_id TEXT,
                    key_name TEXT, actor TEXT NOT NULL DEFAULT 'owner',
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""")
                conn.execute("CREATE INDEX IF NOT EXISTS api_key_audit_created_idx ON api_key_audit_log(created_at DESC)")
                conn.execute("CREATE INDEX IF NOT EXISTS external_project_tokens_project_idx ON external_project_tokens(project_id,created_at)")
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
            for table in RETIRED_EXECUTION_TABLES:
                conn.execute(f"DROP TABLE IF EXISTS {table}")
            conn.executescript(SCHEMA)
            _strip_retired_financial_outcomes(conn)
            _drop_retired_outcome_columns(conn, postgres=False)
            conn.execute("DELETE FROM application_state WHERE state_key IN ('operational_safety','risk_runtime')")
            bet_signal_columns = {row[1] for row in conn.execute("PRAGMA table_info(bet_signals)").fetchall()}
            if "settlement_status" not in bet_signal_columns:
                conn.execute("ALTER TABLE bet_signals ADD COLUMN settlement_status TEXT NOT NULL DEFAULT 'PENDING'")
                for row in conn.execute("SELECT signal_id,target_round_id,payload FROM bet_signals").fetchall():
                    try:
                        payload = json.loads(row["payload"]) if isinstance(row["payload"], str) else row["payload"]
                    except (TypeError, json.JSONDecodeError):
                        payload = {}
                    if isinstance(payload, dict) and payload.get("settlement_status") == "COMMITTED":
                        conn.execute("UPDATE bet_signals SET settlement_status='COMMITTED' WHERE signal_id=? AND target_round_id=?",
                                     (str(row["signal_id"]), str(row["target_round_id"])))
            conn.execute("CREATE INDEX IF NOT EXISTS bet_signals_settlement_created_idx ON bet_signals(settlement_status,created_at)")
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

    def load_recent_round_context(self, limit: int = 100) -> list[dict[str, Any]]:
        """Load an indexed, bounded live context while retaining full history in storage."""
        bounded = min(max(int(limit), 1), 5000)
        table = "aviator_rounds" if self.database_url else "rounds"
        if self.database_url:
            projection = ("round_id,round_index,multiplier,timestamp,platform_round_id,platform_round_index,"
                          "local_round_index,platform_timestamp,observed_at,COALESCE(stored_at,created_at) AS stored_at,"
                          "round_identity_type,round_index_source,identity_confidence,continuity_verified,gap_before,continuity_proof")
        else:
            projection = ("round_id,round_index,multiplier,timestamp,NULL AS platform_round_id,"
                          "NULL AS platform_round_index,round_index AS local_round_index,"
                          "NULL AS platform_timestamp,NULL AS observed_at,NULL AS stored_at,"
                          "'LEGACY_UNKNOWN' AS round_identity_type,'LEGACY_UNKNOWN' AS round_index_source,"
                          "'UNKNOWN' AS identity_confidence,0 AS continuity_verified,0 AS gap_before,"
                          "'LEGACY_UNVERIFIED' AS continuity_proof")
        with self.connect() as conn:
            rows = conn.execute(
                f"SELECT {projection} FROM {table} ORDER BY round_index DESC LIMIT ?",
                (bounded,),
            ).fetchall()
        return self._round_rows_to_dicts(list(reversed(rows)))

    def round_history_stats(self) -> dict[str, Any]:
        """Aggregate historical reporting in the database without materializing every round."""
        table = "aviator_rounds" if self.database_url else "rounds"
        if self.database_url:
            with self.connect() as conn:
                row = conn.execute(f"""SELECT COUNT(*) AS total, MIN(multiplier) AS minimum,
                    MAX(multiplier) AS maximum, AVG(multiplier) AS mean,
                    percentile_cont(0.5) WITHIN GROUP (ORDER BY multiplier) AS median,
                    COUNT(*) FILTER (WHERE multiplier<1.5) AS below_1_5x,
                    COUNT(*) FILTER (WHERE multiplier>=2) AS at_least_2x,
                    COUNT(*) FILTER (WHERE multiplier>=5) AS at_least_5x,
                    COUNT(*) FILTER (WHERE multiplier>=10) AS at_least_10x
                    FROM {table}""").fetchone()
            return {
                "total": int(row["total"] or 0),
                "min": float(row["minimum"]) if row["minimum"] is not None else None,
                "max": float(row["maximum"]) if row["maximum"] is not None else None,
                "mean": float(row["mean"]) if row["mean"] is not None else None,
                "median": float(row["median"]) if row["median"] is not None else None,
                "threshold_counts": {
                    "below_1_5x": int(row["below_1_5x"] or 0),
                    "at_least_2x": int(row["at_least_2x"] or 0),
                    "at_least_5x": int(row["at_least_5x"] or 0),
                    "at_least_10x": int(row["at_least_10x"] or 0),
                },
            }
        import statistics
        with self.connect() as conn:
            rows = conn.execute(f"SELECT multiplier FROM {table} ORDER BY multiplier").fetchall()
        values = [float(row["multiplier"]) for row in rows]
        return {
            "total": len(values), "min": min(values) if values else None,
            "max": max(values) if values else None,
            "mean": statistics.fmean(values) if values else None,
            "median": statistics.median(values) if values else None,
            "threshold_counts": {
                "below_1_5x": sum(value < 1.5 for value in values),
                "at_least_2x": sum(value >= 2 for value in values),
                "at_least_5x": sum(value >= 5 for value in values),
                "at_least_10x": sum(value >= 10 for value in values),
            },
        }

    def round_history_status(self) -> dict[str, Any]:
        """Return the exact history count and two newest rounds without loading the full ledger."""
        table = "aviator_rounds" if self.database_url else "rounds"
        verified_columns = ("stored_at,continuity_verified,gap_before,identity_confidence,round_index_source"
                            if self.database_url else
                            "NULL AS stored_at,0 AS continuity_verified,0 AS gap_before,"
                            "'UNKNOWN' AS identity_confidence,'UNKNOWN' AS round_index_source")
        with self.connect() as conn:
            count_row = conn.execute(f"SELECT COUNT(*) AS round_count,MIN(timestamp) AS first_timestamp FROM {table}").fetchone()
            recent = conn.execute(
                f"SELECT round_id,round_index,multiplier,timestamp,{verified_columns} FROM {table} "
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
                "stored_at": self._db_time_text(row["stored_at"]),
                "continuity_verified": bool(row["continuity_verified"]),
                "gap_before": bool(row["gap_before"]),
                "identity_confidence": row["identity_confidence"],
                "round_index_source": row["round_index_source"],
            }

        ordered = list(reversed(recent))
        return {
            "count": int(count_row["round_count"] or 0) if count_row else 0,
            "first_timestamp": self._db_time_text(count_row["first_timestamp"])
                if count_row and count_row["first_timestamp"] is not None else None,
            "latest": as_round(ordered[-1]) if ordered else None,
            "previous": as_round(ordered[-2]) if len(ordered) > 1 else None,
        }

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

    def committed_round_distribution_event_by_id(self, round_id: str) -> dict[str, Any] | None:
        """Load a committed distribution event by its canonical round identity."""
        if not self.database_url:
            return None
        with self.connect() as conn:
            row = conn.execute("""SELECT payload FROM round_distribution_events
                WHERE round_id=? ORDER BY stream_sequence DESC LIMIT 1""", (str(round_id),)).fetchone()
        return self._round_distribution_payload(row)

    def committed_round_distribution_event_by_index(self, stream_id: str,
                                                     round_index: int) -> dict[str, Any] | None:
        """Load a committed distribution event by its canonical stream slot."""
        if not self.database_url:
            return None
        with self.connect() as conn:
            row = conn.execute("""SELECT e.payload FROM round_distribution_events e
                JOIN aviator_rounds r ON r.round_id=e.round_id AND r.round_index=e.round_index
                WHERE e.stream_id=? AND e.round_index=? ORDER BY e.stream_sequence DESC LIMIT 1""",
                (str(stream_id), int(round_index))).fetchone()
        return self._round_distribution_payload(row)

    def round_distribution_events_page(self, stream_id: str, *, through_sequence: int,
                                       limit: int = 500, after_sequence: int = 0,
                                       before_sequence: int | None = None,
                                       search: str | None = None,
                                       min_multiplier: float | None = None,
                                       max_multiplier: float | None = None,
                                       order: str = "asc") -> list[dict[str, Any]]:
        """Return a filtered, cursor-paginated page of committed round events."""
        if not self.database_url:
            raise RuntimeError("round distribution requires authoritative PostgreSQL")
        direction = str(order).lower()
        if direction not in {"asc", "desc"}:
            raise ValueError("round event order must be asc or desc")
        bounded = min(max(int(limit), 1), 501)
        conditions = ["stream_id=?", "stream_sequence<=?"]
        params: list[Any] = [str(stream_id), int(through_sequence)]
        if direction == "asc":
            conditions.append("stream_sequence>?")
            params.append(max(0, int(after_sequence)))
        else:
            if before_sequence is not None:
                conditions.append("stream_sequence<?")
                params.append(int(before_sequence))
        if search:
            conditions.append(
                "POSITION(LOWER(?) IN LOWER(COALESCE(payload->>'round_id', ''))) > 0")
            params.append(str(search))
        if min_multiplier is not None:
            conditions.append("(payload->>'multiplier')::numeric>=?")
            params.append(float(min_multiplier))
        if max_multiplier is not None:
            conditions.append("(payload->>'multiplier')::numeric<=?")
            params.append(float(max_multiplier))
        params.append(bounded)
        query = f"""SELECT payload FROM round_distribution_events
            WHERE {' AND '.join(conditions)}
            ORDER BY stream_sequence {direction.upper()} LIMIT ?"""
        with self.connect() as conn:
            rows = conn.execute(query, tuple(params)).fetchall()
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
                                         prune: bool = True,
                                         stream_id: str | None = None) -> dict[str, Any]:
        """Create one replayable committed event only after its canonical PG row exists."""
        if not self.database_url:
            raise RuntimeError("round distribution requires authoritative PostgreSQL")
        with (nullcontext(connection) if connection is not None else self.connect()) as conn:
            if stream_id is None:
                meta = conn.execute("SELECT stream_id FROM round_distribution_meta WHERE singleton=1").fetchone()
                stream_id = str(meta["stream_id"])
            else:
                stream_id = str(stream_id)
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
            published_at = datetime.now(timezone.utc).isoformat()
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
                "published_at": published_at,
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
                    if sequence > bounded_retention and sequence % 1000 == 0:
                        conn.execute("DELETE FROM round_distribution_events WHERE stream_id=? AND stream_sequence<=?",
                                     (stream_id, sequence - bounded_retention))
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
                                         retention: int = 100000, *,
                                         stream_id: str | None = None) -> list[dict[str, Any]]:
        """Commit ordered canonical handoffs in one PostgreSQL transaction."""
        if not observations:
            return []
        bounded_retention = min(max(int(retention), 1000), 1_000_000)
        with self.connect() as conn:
            events = []
            for observation in observations:
                event = self.persist_round_distribution_event(
                    observation["round_id"], observation["multiplier"], observation,
                    bounded_retention, connection=conn, prune=False, stream_id=stream_id)
                if event.get("published_at") is None:
                    published_at = datetime.now(timezone.utc)
                    event["published_at"] = published_at.isoformat()
                    conn.execute("""UPDATE round_distribution_events SET published_at=?,payload=?::jsonb
                        WHERE stream_id=? AND stream_sequence=?""",
                        (published_at,
                         json.dumps(event, sort_keys=True, separators=(",", ":")),
                         event["stream_id"], event["stream_sequence"]))
                events.append(event)
            stream_id = events[0]["stream_id"]
            latest_sequence = max(int(event["stream_sequence"]) for event in events)
            if (latest_sequence > bounded_retention
                    and latest_sequence % 1000 < len(events)):
                conn.execute("DELETE FROM round_distribution_events WHERE stream_id=? AND stream_sequence<=?",
                             (stream_id, latest_sequence - bounded_retention))
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

    def list_managed_api_keys(self) -> list[dict[str, Any]]:
        if not self.database_url:
            raise RuntimeError("API key management requires authoritative PostgreSQL")
        with self.connect() as conn:
            rows = conn.execute("""SELECT token_id,project_id,label,token_prefix,key_type,scopes,console_managed,
                created_at,last_used_at,expires_at,revoked_at
                FROM external_project_tokens ORDER BY created_at DESC,token_id DESC""").fetchall()
        result = []
        for row in rows:
            item = dict(row)
            try:
                item["scopes"] = json.loads(item.get("scopes") or "[]")
            except (TypeError, json.JSONDecodeError):
                item["scopes"] = []
            result.append(item)
        return result

    def create_managed_api_key(self, *, token_id: str, project_id: str, label: str,
                               token_hash: str, token_prefix: str, key_type: str,
                               scopes: list[str], expires_at: str | None,
                               rotate_token_id: str | None = None) -> dict[str, Any] | None:
        if not self.database_url:
            raise RuntimeError("API key management requires authoritative PostgreSQL")
        encoded_scopes = json.dumps(scopes, separators=(",", ":"))
        with self.connect() as conn:
            if rotate_token_id:
                prior = conn.execute("""SELECT token_id FROM external_project_tokens
                    WHERE token_id=? AND revoked_at IS NULL FOR UPDATE""", (rotate_token_id,)).fetchone()
                if not prior:
                    return None
            conn.execute("""INSERT INTO external_project_tokens(
                token_id,project_id,label,token_hash,token_prefix,key_type,scopes,console_managed,expires_at)
                VALUES(?,?,?,?,?,?,?,TRUE,?)""",
                (token_id, project_id, label, token_hash, token_prefix, key_type,
                 encoded_scopes, expires_at))
            conn.execute("""INSERT INTO api_key_audit_log(audit_id,action,token_id,key_name,actor)
                VALUES(?,?,?,?,?)""", (uuid.uuid4().hex, "CREATED", token_id, label, "owner"))
            if rotate_token_id:
                conn.execute("""UPDATE external_project_tokens SET revoked_at=CURRENT_TIMESTAMP
                    WHERE token_id=? AND revoked_at IS NULL""", (rotate_token_id,))
                conn.execute("""INSERT INTO api_key_audit_log(audit_id,action,token_id,key_name,actor)
                    VALUES(?,?,?,?,?)""", (uuid.uuid4().hex, "ROTATED", rotate_token_id, label, "owner"))
        return {"token_id": token_id, "project_id": project_id, "label": label,
                "token_prefix": token_prefix, "key_type": key_type, "scopes": list(scopes),
                "expires_at": expires_at}

    def revoke_managed_api_key(self, token_id: str) -> dict[str, Any] | None:
        if not self.database_url:
            raise RuntimeError("API key management requires authoritative PostgreSQL")
        with self.connect() as conn:
            row = conn.execute("""UPDATE external_project_tokens SET revoked_at=CURRENT_TIMESTAMP
                WHERE token_id=? AND revoked_at IS NULL
                RETURNING token_id,label,key_type""", (token_id,)).fetchone()
            if row:
                conn.execute("""INSERT INTO api_key_audit_log(audit_id,action,token_id,key_name,actor)
                    VALUES(?,?,?,?,?)""", (uuid.uuid4().hex, "REVOKED", token_id, row["label"], "owner"))
        return dict(row) if row else None

    def record_api_key_audit(self, action: str, *, token_id: str | None = None,
                             key_name: str | None = None, actor: str = "owner") -> None:
        if not self.database_url:
            raise RuntimeError("API key audit requires authoritative PostgreSQL")
        with self.connect() as conn:
            conn.execute("""INSERT INTO api_key_audit_log(audit_id,action,token_id,key_name,actor)
                VALUES(?,?,?,?,?)""", (uuid.uuid4().hex, action, token_id, key_name, actor))

    def external_project_token(self, token_hash: str) -> dict[str, Any] | None:
        if not self.database_url:
            raise RuntimeError("external project tokens require authoritative PostgreSQL")
        with self.connect() as conn:
            row = conn.execute("""SELECT token_id,project_id,label,token_prefix,key_type,scopes,console_managed,
                    created_at,last_used_at,expires_at
                FROM external_project_tokens WHERE token_hash=? AND revoked_at IS NULL""",
                (token_hash,)).fetchone()
        item = dict(row) if row else None
        if item:
            try:
                item["scopes"] = json.loads(item.get("scopes") or "[]")
            except (TypeError, json.JSONDecodeError):
                item["scopes"] = []
        return item

    def touch_external_project_token(self, token_id: str) -> None:
        if not self.database_url:
            raise RuntimeError("external project tokens require authoritative PostgreSQL")
        with self.connect() as conn:
            conn.execute("UPDATE external_project_tokens SET last_used_at=CURRENT_TIMESTAMP WHERE token_id=? AND revoked_at IS NULL",
                         (token_id,))

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
                                collector_session_id: str | None = None,
                                total_history: int | None = None) -> dict[str, Any]:
        """Recompute the latest continuity window from durable rounds."""
        from datetime import datetime, timezone
        rows = self.load_recent_round_context(max(100, int(required_rounds) + 1))
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
            "total_history": (self.round_history_status()["count"]
                              if total_history is None else max(0, int(total_history))),
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
        """Serialize concurrent rule snapshots and reject stale generations."""
        with self._rules_engine_snapshot_lock:
            self._save_rules_engine_snapshot(payload, updated_at)

    def _save_rules_engine_snapshot(self, payload: dict[str, Any], updated_at: str) -> None:
        """Persist the engine snapshot and its durable, queryable rule ledger.

        Each selected target and its canonical round outcome are recorded in
        the durable SHADOW ledger.
        """
        # Session records already have their own durable table. Keep the
        # singleton application_state row compact and merge rule_sessions on
        # load; copying every historical evaluation into this row on every
        # round made each checkpoint proportional to the entire experiment
        # archive.
        persisted_state = dict(payload)
        persisted_state.pop("sessions", None)
        persisted_state["_rules_engine_sessions_source"] = "rule_sessions"
        state = json.dumps(persisted_state, sort_keys=True, separators=(",", ":"))
        fingerprint_updates: dict[tuple[str, str], str] = {}
        evaluation_state_updates: dict[str, str] = {}

        def ledger_changed(kind: str, identity: str, value: Any) -> bool:
            key = (kind, identity)
            fingerprint = self._rules_engine_fingerprint(value)
            if (fingerprint_updates.get(key)
                    or self._rules_engine_ledger_fingerprints.get(key)) == fingerprint:
                return False
            fingerprint_updates[key] = fingerprint
            return True

        with self.connect() as conn:
            existing_state = conn.execute(
                "SELECT payload FROM application_state WHERE state_key='rules_engine_state_v1'").fetchone()
            incoming_generation = int(payload.get("_rules_engine_generation") or 0)
            if existing_state:
                stored_payload = existing_state["payload"]
                if isinstance(stored_payload, str):
                    try:
                        stored_payload = json.loads(stored_payload)
                    except (TypeError, json.JSONDecodeError):
                        stored_payload = {}
                stored_generation = int((stored_payload or {}).get("_rules_engine_generation") or 0)
                if stored_generation > incoming_generation:
                    return
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
                session_id = str(session_id)
                session_payload = json.dumps(session, sort_keys=True, separators=(",", ":"))
                if ledger_changed("session", session_id, session_payload):
                    conn.execute("""INSERT INTO rule_sessions(session_id,mode,status,started_at,updated_at,payload)
                        VALUES(?,?,?,?,?,?) ON CONFLICT(session_id) DO UPDATE SET
                        mode=excluded.mode,status=excluded.status,updated_at=excluded.updated_at,
                        payload=excluded.payload""",
                        (session_id, session.get("mode", "UNKNOWN"), session.get("status", "UNKNOWN"),
                         session.get("started_at") or updated_at, updated_at, session_payload))
                else:
                    # If the persisted session body did not change, its
                    # queryable rule ledgers need no additional writes.
                    continue
                for evaluation in session.get("evaluations", []):
                    source = evaluation.get("source_round") or {}
                    ev_at = str(evaluation.get("evaluated_at") or updated_at)
                    identity = "\0".join((str(session_id), str(source.get("streamId") or ""),
                                           str(source.get("roundId") or ""), ev_at))
                    evaluation_id = hashlib.sha256(identity.encode("utf-8")).hexdigest()
                    evaluation_state = self._rules_engine_evaluation_state(evaluation)
                    is_new_evaluation = evaluation_id not in self._rules_engine_evaluation_states
                    evaluation_changed = (is_new_evaluation
                        or self._rules_engine_evaluation_states.get(evaluation_id) != evaluation_state)
                    if evaluation_changed:
                        evaluation_payload = json.dumps(evaluation, sort_keys=True, separators=(",", ":"))
                        if ledger_changed("evaluation", evaluation_id, evaluation_payload):
                            conn.execute("""INSERT INTO rule_evaluations(
                                evaluation_id,session_id,source_stream_id,source_round_id,evaluated_at,result,payload)
                                VALUES(?,?,?,?,?,?,?) ON CONFLICT(evaluation_id) DO UPDATE SET
                                result=excluded.result,payload=excluded.payload""",
                                (evaluation_id, str(session_id), source.get("streamId"), source.get("roundId"),
                                 ev_at, str(evaluation.get("decision", {}).get("action") or "WAIT"), evaluation_payload))
                        evaluation_state_updates[evaluation_id] = evaluation_state

                    # The nested rule evaluation rows are immutable after the
                    # parent is first recorded. Commit confirmation only changes
                    # the parent's durability metadata; skip all old children.
                    if not is_new_evaluation:
                        continue
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
                        rule_row_fingerprint = {
                            "rule_version": str(rule_evaluation.get("rule_version") or "UNKNOWN"),
                            "expected_target_index": expected.get("roundIndex") if isinstance(expected, dict) else None,
                            "eligible": rule_evaluation.get("eligible"),
                            "status": str(rule_evaluation.get("status") or "UNKNOWN"),
                            "reason": str(rule_evaluation.get("reason") or ""),
                            "evaluated_at": ev_at,
                            "source_evidence": json.loads(source_evidence),
                            "state_snapshot": json.loads(state_snapshot),
                        }
                        if ledger_changed("rule_evaluation", rule_evaluation_id, rule_row_fingerprint):
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
                    selection_json = json.dumps(selection_payload, sort_keys=True, separators=(",", ":"))
                    if ledger_changed("selection", selection_id, selection_json):
                        conn.execute("""INSERT INTO rule_selections(
                            selection_id,session_id,selection_kind,source_stream_id,source_round_id,
                            target_stream_id,target_round_id,target_round_index,created_at,payload)
                            VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(session_id,selection_id) DO UPDATE SET
                            target_round_id=excluded.target_round_id,payload=excluded.payload""",
                            (selection_id, str(session_id), selection_kind, source.get("streamId"),
                             source.get("roundId"), expected.get("streamId"),
                             (selection.get("actual_target") or {}).get("roundId"),
                             expected.get("roundIndex"), str(selection.get("selected_at") or updated_at),
                             selection_json))

                    actual = selection.get("actual_target")
                    if actual or selection.get("actual_multiplier") is not None:
                        outcome_payload = {
                            "selection_id": selection_id,
                            "session_id": str(session_id),
                            "target_round_id": (actual or {}).get("roundId"),
                            "target_round_index": (actual or {}).get("roundIndex") or expected.get("roundIndex"),
                            "multiplier": selection.get("actual_multiplier"),
                            "round_outcome": ("H" if selection.get("actual_multiplier") is not None
                                              and float(selection["actual_multiplier"]) > 2 else
                                              "L" if selection.get("actual_multiplier") is not None else None),
                            "evidence_type": "CANONICAL_ROUND_RESULT_ONLY",
                            "recorded_at": selection.get("resolved_at") or updated_at,
                        }
                        outcome_id = hashlib.sha256(selection_id.encode("utf-8")).hexdigest()
                        if ledger_changed("target_outcome", selection_id, outcome_payload):
                            conn.execute("""INSERT INTO rule_target_outcomes(
                                outcome_id,selection_id,session_id,target_round_id,target_round_index,
                            multiplier,round_outcome,payload)
                            VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(selection_id) DO UPDATE SET
                                target_round_id=excluded.target_round_id,target_round_index=excluded.target_round_index,
                                multiplier=excluded.multiplier,round_outcome=excluded.round_outcome,
                                payload=excluded.payload""",
                                (outcome_id, selection_id, str(session_id), outcome_payload["target_round_id"],
                                 outcome_payload["target_round_index"], outcome_payload["multiplier"],
                                 outcome_payload["round_outcome"],
                                 json.dumps(outcome_payload, sort_keys=True, separators=(",", ":"))))

                stop_events = list(session.get("stop_history") or [])
                if not stop_events:
                    reason = session.get("selection_stop_reason") or session.get("stop_reason")
                    stopped_at = (session.get("selection_stopped_at") or session.get("stop_at")
                                  or session.get("ended_at") or session.get("stopped_at"))
                    if reason and stopped_at:
                        stop_events = [{"reason": reason, "stopped_at": stopped_at,
                            "source": session.get("selection_stop_source") or session.get("stop_source")
                                      or "LEGACY_UNVERIFIED",
                            "source_round": {"round_id": session.get("selection_stopped_at_round_id")
                                                           or session.get("stop_round_id")},
                            "scope": "LEGACY_UNVERIFIED", "details": {}}]
                for stop in stop_events:
                    reason = stop.get("reason")
                    stopped_at = stop.get("stopped_at")
                    if not reason or not stopped_at:
                        continue
                    # The table's unique key remains (session, reason, time),
                    # while the payload retains the source and scope.
                    event_id = hashlib.sha256(f"{session_id}\0{reason}\0{stopped_at}".encode("utf-8")).hexdigest()
                    event = {"stop_event_id": event_id, "session_id": str(session_id),
                             "reason": str(reason), "stopped_at": str(stopped_at),
                             "status": session.get("status"),
                             "source": stop.get("source", "LEGACY_UNVERIFIED"),
                             "source_round": stop.get("source_round"),
                             "scope": stop.get("scope", "UNKNOWN"),
                             "details": stop.get("details") or {}}
                    event_json = json.dumps(event, sort_keys=True, separators=(",", ":"))
                    if ledger_changed("stop_event", event_id, event_json):
                        conn.execute("""INSERT OR IGNORE INTO rule_session_stop_events(
                            stop_event_id,session_id,reason,stopped_at,payload) VALUES(?,?,?,?,?)""",
                            (event_id, str(session_id), str(reason), str(stopped_at), event_json))
        # Only record cache updates after the transaction committed. If the
        # transaction failed, a retry must execute every skipped ledger write.
        self._rules_engine_ledger_fingerprints.update(fingerprint_updates)
        self._rules_engine_evaluation_states.update(evaluation_state_updates)

    def delete_rules_engine_session(self, session_id: str, snapshot: dict[str, Any],
                                    deleted_at: str) -> None:
        """Remove one finished session through the same transaction path as bulk deletes."""
        result = self.delete_rules_engine_sessions([session_id], snapshot, deleted_at)
        if result["failures"]:
            failure = result["failures"][0]
            if failure["reason"] == "SESSION_NOT_FOUND":
                raise KeyError(failure["reason"])
            raise ValueError(failure["reason"])

    def _rule_session_deletion_data(self, conn, session_id: str) -> dict[str, Any]:
        row = conn.execute("SELECT status,payload FROM rule_sessions WHERE session_id=?",
                           (session_id,)).fetchone()
        if not row:
            raise KeyError("SESSION_NOT_FOUND")
        persisted = json.loads(row["payload"])
        stored_status = persisted.get("status")
        observed = persisted.get("observed_rounds")
        window = persisted.get("window_size")
        if (stored_status in {"RUNNING", "SETTLING"}
                and isinstance(observed, int) and not isinstance(observed, bool)
                and isinstance(window, int) and not isinstance(window, bool)
                and window > 0 and observed >= window):
            # Compatibility for completed windows written by older code that
            # left the persisted state at SETTLING.
            stored_status = "COMPLETED"
        if stored_status not in {"COMPLETED", "SETTLING", "PAUSED", "MANUALLY_STOPPED", "RESET", "RECONFIGURED"}:
            raise ValueError("SESSION_NOT_FINISHED")
        if persisted.get("auto_next_armed"):
            raise ValueError("SESSION_AUTO_NEXT_ARMED")
        selections = list(persisted.get("selections") or [])
        selections.extend(item for rows in (persisted.get("rule_selections") or {}).values()
                          for item in rows)
        return {"persisted": persisted, "stored_status": stored_status,
                "selections": selections}

    def delete_rules_engine_sessions(self, session_ids: list[str], snapshot: dict[str, Any],
                                     deleted_at: str) -> dict[str, Any]:
        """Delete eligible sessions in one transaction while retaining audit evidence."""
        unique_ids = list(dict.fromkeys(str(session_id) for session_id in session_ids))
        prepared: list[dict[str, Any]] = []
        failures: list[dict[str, str]] = []
        with self.connect() as conn:
            snapshot_sessions = snapshot.get("sessions") or {}
            existing_session_ids = {str(row["session_id"])
                for row in conn.execute("SELECT session_id FROM rule_sessions").fetchall()}
            for session_id, session in snapshot_sessions.items():
                session_id = str(session_id)
                if session_id in existing_session_ids:
                    continue
                session_payload = json.dumps(session, sort_keys=True, separators=(",", ":"))
                conn.execute("""INSERT INTO rule_sessions(session_id,mode,status,started_at,updated_at,payload)
                    VALUES(?,?,?,?,?,?) ON CONFLICT(session_id) DO NOTHING""",
                    (session_id, session.get("mode", "UNKNOWN"), session.get("status", "UNKNOWN"),
                     session.get("started_at") or deleted_at, deleted_at, session_payload))
                existing_session_ids.add(session_id)

            for session_id in unique_ids:
                try:
                    deletion_data = self._rule_session_deletion_data(conn, session_id)
                except KeyError as exc:
                    failures.append({"session_id": session_id, "reason": str(exc.args[0])})
                except ValueError as exc:
                    failures.append({"session_id": session_id, "reason": str(exc)})
                else:
                    prepared.append({"session_id": session_id, **deletion_data})

            for candidate in prepared:
                session_id = candidate["session_id"]
                persisted = candidate["persisted"]
                selections = candidate["selections"]
                # Keep stop events and the immutable system audit trail.
                for table in ("rule_target_outcomes", "rule_selections", "rule_evaluations",
                              "rules_engine_evaluations", "rule_sessions"):
                    conn.execute(f"DELETE FROM {table} WHERE session_id=?", (session_id,))
                audit = {"session_id": session_id, "mode": persisted.get("mode"),
                         "status": candidate["stored_status"],
                         "started_at": persisted.get("started_at"), "deleted_at": deleted_at,
                         "selection_count": len(selections),
                         "deleted_pending_session_events": sum(
                             isinstance(record, dict) and record.get("persistence_status") == "PENDING"
                             for record in (persisted.get("event_records") or {}).values()),
                         "settling_observer_stopped": candidate["stored_status"] == "SETTLING",
                         "deleted_shadow_results": {
                             result: sum(item.get("status") == result for item in selections)
                             for result in ("WIN", "LOSS", "PENDING")
                         }}
                conn.execute("""INSERT INTO system_audit_log(event_type,entity_id,recorded_at,payload)
                    VALUES(?,?,?,?)""", ("RULE_SHADOW_SESSION_DELETED", session_id, deleted_at,
                                           json.dumps(audit, sort_keys=True, separators=(",", ":"))))

            if prepared:
                state_payload = dict(snapshot)
                state_payload.pop("sessions", None)
                state_payload["_rules_engine_sessions_source"] = "rule_sessions"
                state = json.dumps(state_payload, sort_keys=True, separators=(",", ":"))
                conn.execute("""INSERT INTO application_state(state_key,payload,updated_at)
                    VALUES('rules_engine_state_v1',?,?) ON CONFLICT(state_key) DO UPDATE SET
                    payload=excluded.payload,updated_at=excluded.updated_at""", (state, deleted_at))

        return {"deleted_session_ids": [row["session_id"] for row in prepared],
                "failures": failures}

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
            if not row:
                return None
            payload = json.loads(row["payload"])
            if (key == "rules_engine_state_v1"
                    and payload.get("_rules_engine_sessions_source") == "rule_sessions"):
                rows = conn.execute("SELECT session_id,payload FROM rule_sessions ORDER BY started_at,session_id").fetchall()
                payload["sessions"] = {str(item["session_id"]): json.loads(item["payload"])
                                       for item in rows}
            return payload




































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















    def list_audit_log(self, limit: int = 100) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute("SELECT event_type,entity_id,recorded_at,payload FROM system_audit_log ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [{**dict(row), "payload": json.loads(row["payload"])} for row in rows]

    @staticmethod
    def _bet_signal_from_row(row) -> dict[str, Any] | None:
        if not row:
            return None
        payload = row["payload"]
        return json.loads(payload) if isinstance(payload, str) else dict(payload or {})

    def bet_signal_for_target(self, session_id: str, target_round_id: str) -> dict[str, Any] | None:
        """Return the immutable BET signal already assigned to this session target."""
        with self.connect() as conn:
            row = conn.execute("""SELECT payload FROM bet_signals
                WHERE session_id=? AND target_round_id=?""",
                (str(session_id), str(target_round_id))).fetchone()
        return self._bet_signal_from_row(row)

    def bet_signal_by_id(self, signal_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT payload FROM bet_signals WHERE signal_id=? LIMIT 1",
                               (str(signal_id),)).fetchone()
        return self._bet_signal_from_row(row)

    def bet_signals_for_target_round(self, target_round_id: str) -> list[dict[str, Any]]:
        """Return durable signals waiting on one exact Aviator target round."""
        with self.connect() as conn:
            rows = conn.execute("""SELECT payload FROM bet_signals
                WHERE target_round_id=? ORDER BY created_at,signal_id""",
                (str(target_round_id),)).fetchall()
        return [self._bet_signal_from_row(row) for row in rows if row]

    def bet_signals_for_target_round_index(self, stream_id: str,
                                           round_index: int) -> list[dict[str, Any]]:
        """Return index-only SHADOW signals assigned to one canonical stream slot."""
        target_key = f"shadow-index:{stream_id}:{int(round_index)}"
        with self.connect() as conn:
            rows = conn.execute("""SELECT payload FROM bet_signals
                WHERE target_round_id=? ORDER BY created_at,signal_id""",
                (target_key,)).fetchall()
        return [self._bet_signal_from_row(row) for row in rows if row]

    def insert_bet_signal(self, payload: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
        """Persist one signal per session/target with a server publication timestamp."""
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
        with self.connect() as conn:
            result = conn.execute("""INSERT INTO bet_signals(
                signal_id,session_id,round_id,target_round_id,created_at,settlement_status,payload)
                VALUES(?,?,?,?,?,?,?) ON CONFLICT(session_id,target_round_id) DO NOTHING""",
                (str(payload["signal_id"]), str(payload["session_id"]), str(payload["round_id"]),
                 str(payload["target_round_id"]), datetime.now(timezone.utc).isoformat(),
                 str(payload.get("settlement_status") or "PENDING"), encoded))
            inserted = int(getattr(result, "rowcount", 0) or 0) == 1
            row = conn.execute("""SELECT payload FROM bet_signals
                WHERE session_id=? AND target_round_id=?""",
                (str(payload["session_id"]), str(payload["target_round_id"]))).fetchone()
        existing = self._bet_signal_from_row(row)
        return inserted, existing or payload

    def accept_bet_signal(self, payload: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
        """Atomically insert or resolve one idempotent signal in a single DB checkout."""
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
        session_id = str(payload["session_id"])
        target_round_id = str(payload["target_round_id"])
        signal_id = str(payload["signal_id"])
        with self.connect() as conn:
            result = conn.execute("""INSERT INTO bet_signals(
                signal_id,session_id,round_id,target_round_id,created_at,settlement_status,payload)
                VALUES(?,?,?,?,?,?,?) ON CONFLICT DO NOTHING""",
                (signal_id, session_id, str(payload["round_id"]), target_round_id,
                 datetime.now(timezone.utc).isoformat(),
                 str(payload.get("settlement_status") or "PENDING"), encoded))
            inserted = int(getattr(result, "rowcount", 0) or 0) == 1
            if inserted:
                return True, dict(payload)
            row = conn.execute("""SELECT payload FROM bet_signals
                WHERE (session_id=? AND target_round_id=?) OR signal_id=?
                ORDER BY CASE WHEN session_id=? AND target_round_id=? THEN 0 ELSE 1 END
                LIMIT 1""",
                (session_id, target_round_id, signal_id, session_id, target_round_id)).fetchone()
        existing = self._bet_signal_from_row(row)
        if existing is None:
            raise RuntimeError("signal_conflict_row_not_found")
        return False, existing

    def update_bet_signal_settlement(self, signal_id: str, target_round_id: str,
                                     settlement: dict[str, Any]) -> dict[str, Any] | None:
        """Attach committed result evidence without changing the signal identity."""
        with self.connect() as conn:
            row = conn.execute("""SELECT payload FROM bet_signals
                WHERE signal_id=? AND target_round_id=? LIMIT 1""",
                (str(signal_id), str(target_round_id))).fetchone()
            payload = self._bet_signal_from_row(row)
            if payload is None:
                return None
            if payload.get("settlement_status") == "COMMITTED":
                if payload.get("source_id") == "BEHAVIOUR_DETECTION":
                    self._enqueue_behaviour_settlement(conn, payload,
                        payload.get("settlement") or settlement)
                return payload
            updated = dict(payload)
            updated["settlement_status"] = "COMMITTED"
            updated["settlement"] = dict(settlement)
            if (updated.get("target_identity_status") == "INDEX_ONLY_SHADOW"
                    and settlement.get("target_round_id")):
                updated["resolved_target_round_id"] = str(settlement["target_round_id"])
            encoded = json.dumps(updated, sort_keys=True, separators=(",", ":"), default=str)
            conn.execute("""UPDATE bet_signals SET payload=?,settlement_status='COMMITTED'
                WHERE signal_id=? AND target_round_id=?""",
                (encoded, str(signal_id), str(target_round_id)))
            if updated.get("source_id") == "BEHAVIOUR_DETECTION":
                self._enqueue_behaviour_settlement(conn, updated, settlement)
        return updated

    @staticmethod
    def _enqueue_behaviour_settlement(conn, signal: dict[str, Any],
                                     settlement: dict[str, Any]) -> None:
        evidence = settlement.get("evidence") if isinstance(settlement.get("evidence"), dict) else {}
        committed_evidence = evidence.get("persistence_confirmation")
        if isinstance(committed_evidence, dict):
            # SHADOW first observes a round, then enriches it with the durable
            # commit receipt. Behaviour requires the receipt as the settlement
            # authority, so promote those fields instead of forwarding the
            # earlier PENDING observation as top-level evidence.
            evidence = {**evidence, **committed_evidence}
        target_round_id = str(settlement.get("target_round_id") or signal.get("target_round_id") or "")
        target_stream_id = str(settlement.get("target_stream_id") or evidence.get("stream_id")
                               or signal.get("target_stream_id") or "")
        target_round_index = int(settlement.get("target_round_index")
                                 or signal.get("target_round_index") or 0)
        event = {
            "source_id": "BEHAVIOUR_DETECTION", "strategy_id": signal.get("strategy_id"),
            "signal_id": str(signal.get("signal_id") or ""),
            "session_id": str(signal.get("session_id") or ""),
            "series_id": str(signal.get("series_id") or ""),
            "part": str(signal.get("part") or ""),
            "batch_id": str(signal.get("batch_id") or ""),
            "configuration_version": int(signal.get("configuration_version") or 0),
            "signal_target_round_id": str(signal.get("target_round_id") or ""),
            "signal_lock_status": "DURABLY_LOCKED",
            "settlement_status": "COMMITTED",
            "target_round_id": target_round_id,
            "target_round_identity": f"winner-predict:{target_round_id}",
            "target_stream_id": target_stream_id,
            "target_round_index": target_round_index,
            "actual_multiplier": settlement.get("actual_multiplier"),
            "result": str(settlement.get("result") or "").upper(),
            "net_profit": settlement.get("profit_loss_rwf"),
            "evidence": {**evidence, "event_type": evidence.get("event_type"),
                         "durability": evidence.get("durability"),
                         "continuity_status": evidence.get("continuity_status"),
                         "target_round_id": target_round_id,
                         "target_stream_id": target_stream_id,
                         "target_round_index": target_round_index},
        }
        conn.execute("""INSERT INTO behaviour_strategy_settlement_outbox(
            signal_id,status,payload,created_at) VALUES(?,?,?,?)
            ON CONFLICT(signal_id) DO UPDATE SET payload=excluded.payload
            WHERE behaviour_strategy_settlement_outbox.status='PENDING'""",
            (event["signal_id"], "PENDING",
             json.dumps(event, sort_keys=True, separators=(",", ":"), default=str),
             datetime.now(timezone.utc).isoformat()))

    def pending_behaviour_strategy_settlements(self, limit: int = 100) -> list[dict[str, Any]]:
        bounded = min(max(int(limit), 1), 1000)
        with self.connect() as conn:
            rows = conn.execute("""SELECT payload FROM behaviour_strategy_settlement_outbox
                WHERE status='PENDING' ORDER BY created_at,signal_id LIMIT ?""", (bounded,)).fetchall()
        return [json.loads(row["payload"]) for row in rows]

    def behaviour_strategy_settlement_feedback(self, signal_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("""SELECT payload FROM behaviour_strategy_settlement_outbox
                WHERE signal_id=? LIMIT 1""", (str(signal_id),)).fetchone()
        return json.loads(row["payload"]) if row else None

    def mark_behaviour_strategy_settlement_delivered(self, signal_id: str, *,
            delivered: bool, error: str | None = None) -> None:
        status = "DELIVERED" if delivered else "PENDING"
        delivered_at = datetime.now(timezone.utc).isoformat() if delivered else None
        with self.connect() as conn:
            conn.execute("""UPDATE behaviour_strategy_settlement_outbox
                SET status=?, attempts=attempts+1, last_error=?, delivered_at=? WHERE signal_id=?""",
                (status, (str(error)[:500] if error else None), delivered_at, str(signal_id)))

    def pending_bet_signals(self, limit: int = 1000) -> list[dict[str, Any]]:
        """Return a bounded oldest-first batch for settlement recovery."""
        bounded = min(max(int(limit), 1), 5000)
        with self.connect() as conn:
            rows = conn.execute("""SELECT payload FROM bet_signals
                WHERE settlement_status='PENDING' ORDER BY created_at,signal_id LIMIT ?""",
                (bounded,)).fetchall()
        return [self._bet_signal_from_row(row) for row in rows if row]

    def list_bet_signals(self, limit: int = 100, *, session_id: str | None = None) -> list[dict[str, Any]]:
        bounded = min(max(int(limit), 1), 1000)
        page_size = min(max(bounded, 16), 1000)
        accepted: list[dict[str, Any]] = []
        offset = 0
        with self.connect() as conn:
            while len(accepted) < bounded:
                if session_id:
                    rows = conn.execute("""SELECT payload FROM bet_signals WHERE session_id=?
                        ORDER BY created_at DESC,signal_id DESC LIMIT ? OFFSET ?""",
                        (str(session_id), page_size, offset)).fetchall()
                else:
                    rows = conn.execute("""SELECT payload FROM bet_signals
                        ORDER BY created_at DESC,signal_id DESC LIMIT ? OFFSET ?""",
                        (page_size, offset)).fetchall()
                signals = [self._bet_signal_from_row(row) for row in rows if row]
                accepted.extend(signal for signal in signals
                                if signal and signal.get("distribution_status")
                                in (None, "ACCEPTED"))
                offset += len(rows)
                if len(rows) < page_size:
                    break
        return accepted[:bounded]

    def count_bet_signals(self) -> int:
        with self.connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS total FROM bet_signals").fetchone()
        return int(row["total"] if row else 0)

    def shadow_signal_bridge_status_snapshot(self) -> dict[str, Any]:
        """Load durable counters and latest signal for the built-in bridge."""
        with self.connect() as conn:
            counts = conn.execute("""SELECT COUNT(*) AS published_count,
                COALESCE(SUM(CASE WHEN settlement_status='COMMITTED' THEN 1 ELSE 0 END),0)
                    AS settled_count
                FROM bet_signals WHERE signal_id LIKE ?""", ("shadow-%",)).fetchone()
            latest = conn.execute("""SELECT payload FROM bet_signals
                WHERE signal_id LIKE ?
                ORDER BY created_at DESC,signal_id DESC LIMIT 1""", ("shadow-%",)).fetchone()
        return {
            "published_count": int(counts["published_count"] if counts else 0),
            "settled_count": int(counts["settled_count"] if counts else 0),
            "last_signal": self._bet_signal_from_row(latest),
        }

    def has_committed_round_distribution_event(self, round_id: str) -> bool:
        """Check exact identity in the canonical persisted round distribution."""
        if not self.database_url:
            return False
        with self.connect() as conn:
            row = conn.execute("SELECT 1 FROM round_distribution_events WHERE round_id=? LIMIT 1",
                               (str(round_id),)).fetchone()
        return bool(row)

    def has_committed_round_distribution_index(self, stream_id: str, round_index: int) -> bool:
        """Check the authoritative round table, including rounds not yet distributed.

        Historical distribution events can outlive a replaced round history and
        reuse an index in the same stream. They do not prove the current target
        has completed; the canonical round table does.
        """
        if not self.database_url:
            return False
        with self.connect() as conn:
            row = conn.execute("""SELECT 1 FROM aviator_rounds r
                JOIN round_distribution_meta m ON m.singleton=1
                WHERE m.stream_id=? AND r.round_index=? LIMIT 1""",
                (str(stream_id), int(round_index))).fetchone()
        return bool(row)

    def has_committed_round_distribution_identity(self, stream_id: str, round_id: str,
                                                  round_index: int) -> bool:
        """Check that a source event matches its stream, ID and canonical index."""
        if not self.database_url:
            return False
        with self.connect() as conn:
            row = conn.execute("""SELECT 1 FROM round_distribution_events
                WHERE stream_id=? AND round_id=? AND round_index=? LIMIT 1""",
                (str(stream_id), str(round_id), int(round_index))).fetchone()
        return bool(row)

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
