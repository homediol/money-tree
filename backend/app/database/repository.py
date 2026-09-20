from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any


SCHEMA = """
CREATE TABLE IF NOT EXISTS rounds (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
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
  decision_id TEXT NOT NULL UNIQUE, status TEXT NOT NULL, payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS shadow_ledger (
  id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL, event_type TEXT NOT NULL,
  amount REAL, balance REAL, recorded_at TEXT NOT NULL, payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS live_sessions (
  session_id TEXT PRIMARY KEY, started_at TEXT NOT NULL, ended_at TEXT,
  mode TEXT NOT NULL, snapshot TEXT NOT NULL, payload TEXT NOT NULL
);
"""


class Repository:
    def __init__(self, database_path: Path):
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.database_path)
        conn.row_factory = sqlite3.Row
        return conn

    def init(self) -> None:
        with self.connect() as conn:
            conn.executescript(SCHEMA)

    def upsert_rounds(self, rows: list[dict[str, Any]]) -> None:
        with self.connect() as conn:
            conn.executemany(
                """
                INSERT INTO rounds(round_index, multiplier, timestamp, target)
                VALUES(:round_index, :multiplier, :timestamp, :target)
                ON CONFLICT(round_index) DO UPDATE SET
                  multiplier=excluded.multiplier,
                  timestamp=excluded.timestamp,
                  target=excluded.target
                """,
                rows,
            )

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
            cur = conn.execute(
                """
                INSERT INTO signals(current_sequence, detected_pattern, probability, confidence, status, payload)
                VALUES(?, ?, ?, ?, ?, ?)
                """,
                (
                    json.dumps(signal.get("recent_multipliers", [])),
                    signal.get("current_pattern", {}).get("label", "unknown"),
                    signal.get("final_probability"),
                    signal.get("confidence", "LOW"),
                    signal.get("status", "NO SIGNAL"),
                    json.dumps(signal),
                ),
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

    def list_model_versions(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM model_versions ORDER BY id DESC").fetchall()
        return [{**dict(r), "metrics": json.loads(r["metrics"])} for r in rows]

    def save_ml_prediction(self, prediction: dict[str, Any]) -> None:
        with self.connect() as conn:
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
            conn.execute("INSERT INTO shadow_trades(execution_id,session_id,target_round_id,decision_id,status,payload) VALUES(?,?,?,?,?,?) ON CONFLICT(execution_id) DO UPDATE SET status=excluded.status,payload=excluded.payload",
                         (payload["execution_id"], payload["session_id"], str(payload["target_round_id"]), payload["decision_id"], payload["status"], json.dumps(payload)))

    def list_shadow_trades(self, session_id: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        with self.connect() as conn:
            if session_id:
                rows = conn.execute("SELECT payload FROM shadow_trades WHERE session_id=? ORDER BY rowid DESC LIMIT ?", (session_id, limit)).fetchall()
            else:
                rows = conn.execute("SELECT payload FROM shadow_trades ORDER BY rowid DESC LIMIT ?", (limit,)).fetchall()
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
