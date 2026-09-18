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
