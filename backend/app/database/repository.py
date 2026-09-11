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

