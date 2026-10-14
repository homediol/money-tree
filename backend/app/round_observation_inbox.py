"""Small durable API-side handoff for pending round observations.

The collector already has its own durable outbox. This local SQLite inbox lets
the API acknowledge an observation and publish it to connected SSE clients
without waiting for PostgreSQL. Rows remain available to reconnecting clients
until a committed event is durably recorded in PostgreSQL.
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import threading
import time
from functools import partial
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


async def run_round_observation_inbox(app: Any, function, *args, **kwargs):
    """Keep local durable inbox I/O off the shared PostgreSQL worker pool."""
    executor = getattr(app.state, "round_observation_inbox_executor", None)
    if executor is None:
        return await asyncio.to_thread(function, *args, **kwargs)
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(executor, partial(function, *args, **kwargs))


class ObservationInboxCapacityError(RuntimeError):
    pass


class ObservationIdentityConflict(ValueError):
    pass


class RoundObservationInbox:
    def __init__(self, path: str | Path, *, max_records: int = 50_000,
                 max_bytes: int = 64 * 1024 * 1024, tombstone_limit: int = 10_000):
        self.path = Path(path)
        self.max_records = max(1, int(max_records))
        self.max_bytes = max(1024, int(max_bytes))
        self.tombstone_limit = max(100, int(tombstone_limit))
        self._lock = threading.RLock()
        self.initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5.0, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute("PRAGMA synchronous=FULL")
        return connection

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            connection = self._connect()
            try:
                connection.execute("PRAGMA journal_mode=WAL")
                connection.executescript("""
                    CREATE TABLE IF NOT EXISTS round_observation_inbox (
                        inbox_sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                        observation_id TEXT NOT NULL UNIQUE,
                        round_id TEXT NOT NULL UNIQUE,
                        event_json TEXT NOT NULL,
                        observation_json TEXT NOT NULL,
                        received_at TEXT NOT NULL,
                        status TEXT NOT NULL DEFAULT 'PENDING'
                            CHECK(status IN ('PENDING','COMMITTED')),
                        pg_staged INTEGER NOT NULL DEFAULT 0,
                        attempts INTEGER NOT NULL DEFAULT 0,
                        next_attempt_at REAL NOT NULL DEFAULT 0,
                        last_error TEXT,
                        committed_sequence INTEGER,
                        updated_at TEXT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS round_observation_inbox_pending_idx
                        ON round_observation_inbox(status, pg_staged, next_attempt_at, inbox_sequence);
                    CREATE INDEX IF NOT EXISTS round_observation_inbox_pending_round_idx
                        ON round_observation_inbox(status,
                            CAST(json_extract(event_json, '$.round_index') AS INTEGER), inbox_sequence);
                """)
                os.chmod(self.path, 0o600)
                pages = max(256, (self.max_bytes * 2 + 4095) // 4096)
                connection.execute(f"PRAGMA max_page_count={int(pages)}")
            finally:
                connection.close()

    @staticmethod
    def _canonical(value: dict[str, Any]) -> str:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)

    @staticmethod
    def _validate(event: dict[str, Any], observation: dict[str, Any]) -> tuple[str, str]:
        observation_id = str(event.get("observation_id") or "").strip()
        round_id = str(event.get("round_id") or "").strip()
        if not observation_id or not round_id or observation_id != str(observation.get("observation_id") or round_id):
            raise ValueError("round_observation_identity_missing_or_mismatched")
        if str(observation.get("round_id") or "") != round_id:
            raise ValueError("round_observation_identity_missing_or_mismatched")
        return observation_id, round_id

    def append(self, events: list[dict[str, Any]], observations: list[dict[str, Any]],
               *, received_at: str) -> dict[str, Any]:
        if len(events) != len(observations):
            raise ValueError("round_observation_batch_length_mismatch")
        accepted: list[dict[str, Any]] = []
        duplicate_count = 0
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                pending = connection.execute("""SELECT COUNT(*) AS records,
                    COALESCE(SUM(length(event_json)+length(observation_json)),0) AS bytes
                    FROM round_observation_inbox WHERE status='PENDING'""").fetchone()
                pending_records = int(pending["records"] or 0)
                pending_bytes = int(pending["bytes"] or 0)
                for event, observation in zip(events, observations):
                    observation_id, round_id = self._validate(event, observation)
                    event_json = self._canonical(event)
                    observation_json = self._canonical(observation)
                    prior = connection.execute("""SELECT event_json,observation_json,status
                        FROM round_observation_inbox WHERE observation_id=? OR round_id=?""",
                        (observation_id, round_id)).fetchone()
                    if prior:
                        prior_event = json.loads(prior["event_json"])
                        if (str(prior_event.get("observation_id")) != observation_id
                                or str(prior_event.get("round_id")) != round_id
                                or float(prior_event.get("multiplier")) != float(event.get("multiplier"))
                                or str(prior_event.get("observed_at") or "") != str(event.get("observed_at") or "")):
                            raise ObservationIdentityConflict("round_observation_identity_conflict")
                        if prior["status"] == "PENDING":
                            prior_bytes = len(prior["event_json"].encode("utf-8")) + len(prior["observation_json"].encode("utf-8"))
                            event_bytes = len(event_json.encode("utf-8")) + len(observation_json.encode("utf-8"))
                            if pending_bytes - prior_bytes + event_bytes > self.max_bytes:
                                raise ObservationInboxCapacityError(
                                    f"round observation inbox capacity reached ({pending_records}/{self.max_records} records, "
                                    f"{pending_bytes - prior_bytes + event_bytes}/{self.max_bytes} bytes); no event was discarded")
                            connection.execute("""UPDATE round_observation_inbox
                                SET event_json=?,observation_json=?,received_at=?,updated_at=?
                                WHERE observation_id=?""",
                                (event_json, observation_json, received_at, now, observation_id))
                            pending_bytes += event_bytes - prior_bytes
                            accepted.append(event)
                        else:
                            duplicate_count += 1
                        continue
                    event_bytes = len(event_json.encode("utf-8")) + len(observation_json.encode("utf-8"))
                    if pending_records + 1 > self.max_records or pending_bytes + event_bytes > self.max_bytes:
                        raise ObservationInboxCapacityError(
                            f"round observation inbox capacity reached ({pending_records}/{self.max_records} records, "
                            f"{pending_bytes}/{self.max_bytes} bytes); no event was discarded")
                    connection.execute("""INSERT INTO round_observation_inbox
                        (observation_id,round_id,event_json,observation_json,received_at,updated_at)
                        VALUES(?,?,?,?,?,?)""",
                        (observation_id, round_id, event_json, observation_json, received_at, now))
                    pending_records += 1
                    pending_bytes += event_bytes
                    accepted.append(event)
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
            finally:
                connection.close()
        # The pending count and byte total are already current from the write
        # transaction above. Reopening SQLite for a second full snapshot here
        # added 1-2 seconds to every collector acknowledgement on the growing
        # production inbox and caused the collector's 3-second HTTP deadline to
        # expire. Return transaction-local counts; detailed age/tombstone stats
        # remain available through snapshot() for explicit status requests.
        return {
            "events": accepted,
            "duplicates": duplicate_count,
            "backlog_size": pending_records,
            "backlog_bytes": pending_bytes,
        }

    def pending_events(self, *, limit: int = 500, observation_id: str | None = None,
                       recovered: bool = False) -> list[dict[str, Any]]:
        bounded = min(max(int(limit), 1), 500)
        with self._lock:
            connection = self._connect()
            try:
                if observation_id:
                    rows = connection.execute("""SELECT event_json FROM round_observation_inbox
                        WHERE status='PENDING' AND observation_id=? ORDER BY inbox_sequence LIMIT ?""",
                        (str(observation_id), bounded)).fetchall()
                else:
                    rows = connection.execute("""SELECT event_json FROM round_observation_inbox
                        WHERE status='PENDING' ORDER BY inbox_sequence LIMIT ?""", (bounded,)).fetchall()
            finally:
                connection.close()
        events = [json.loads(row["event_json"]) for row in rows]
        if recovered:
            for event in events:
                event["delivery_mode"] = "RECOVERED"
        return events

    def has_pending_round_index(self, round_index: int) -> bool:
        """Fail closed if the next target was already observed but not committed."""
        with self._lock:
            connection = self._connect()
            try:
                row = connection.execute("""SELECT 1 FROM round_observation_inbox
                    WHERE status='PENDING'
                      AND CAST(json_extract(event_json, '$.round_index') AS INTEGER)=?
                    LIMIT 1""", (int(round_index),)).fetchone()
            finally:
                connection.close()
        return row is not None

    def pending_page(self, *, after_sequence: int = 0, limit: int = 100,
                     observation_id: str | None = None, recovered: bool = True) -> dict[str, Any]:
        bounded = min(max(int(limit), 1), 500)
        cursor = max(0, int(after_sequence))
        with self._lock:
            connection = self._connect()
            try:
                latest_row = connection.execute("SELECT COALESCE(MAX(inbox_sequence),0) AS latest FROM round_observation_inbox").fetchone()
                latest = int(latest_row["latest"] or 0)
                if observation_id:
                    rows = connection.execute("""SELECT inbox_sequence,event_json FROM round_observation_inbox
                        WHERE status='PENDING' AND observation_id=? ORDER BY inbox_sequence LIMIT ?""",
                        (str(observation_id), bounded + 1)).fetchall()
                else:
                    rows = connection.execute("""SELECT inbox_sequence,event_json FROM round_observation_inbox
                        WHERE status='PENDING' AND inbox_sequence>? AND inbox_sequence<=?
                        ORDER BY inbox_sequence LIMIT ?""", (cursor, latest, bounded + 1)).fetchall()
            finally:
                connection.close()
        has_more = len(rows) > bounded
        page_rows = rows[:bounded]
        events = []
        for row in page_rows:
            event = json.loads(row["event_json"])
            event["pending_sequence"] = int(row["inbox_sequence"])
            if recovered:
                event["delivery_mode"] = "RECOVERED"
            events.append(event)
        next_cursor = (int(page_rows[-1]["inbox_sequence"]) if has_more and page_rows
                       else latest if not observation_id else cursor)
        return {"events": events, "next_cursor": next_cursor,
                "has_more": has_more, "latest_sequence": latest}

    def unstaged_observations(self, *, limit: int = 100, now: float | None = None) -> list[dict[str, Any]]:
        bounded = min(max(int(limit), 1), 500)
        with self._lock:
            connection = self._connect()
            try:
                rows = connection.execute("""SELECT observation_id,observation_json FROM round_observation_inbox
                    WHERE status='PENDING' AND pg_staged=0 AND next_attempt_at<=?
                    ORDER BY inbox_sequence LIMIT ?""", (time.time() if now is None else now, bounded)).fetchall()
            finally:
                connection.close()
        return [{"observation_id": row["observation_id"], **json.loads(row["observation_json"])}
                for row in rows]

    def mark_staged(self, observation_id: str) -> None:
        with self._lock:
            connection = self._connect()
            try:
                connection.execute("UPDATE round_observation_inbox SET pg_staged=1,last_error=NULL,updated_at=? WHERE observation_id=?",
                                   (datetime.now(timezone.utc).isoformat(), str(observation_id)))
            finally:
                connection.close()

    def mark_staged_many(self, observation_ids: list[str]) -> None:
        """Mark a staging batch with one durable SQLite transaction."""
        ids = list(dict.fromkeys(str(value) for value in observation_ids if str(value)))
        if not ids:
            return
        now = datetime.now(timezone.utc).isoformat()
        placeholders = ",".join("?" for _ in ids)
        with self._lock:
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    f"UPDATE round_observation_inbox SET pg_staged=1,last_error=NULL,updated_at=? "
                    f"WHERE observation_id IN ({placeholders})",
                    (now, *ids),
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
            finally:
                connection.close()

    def update_event(self, event: dict[str, Any]) -> None:
        observation_id = str(event.get("observation_id") or "")
        if not observation_id:
            raise ValueError("round_observation_identity_missing")
        with self._lock:
            connection = self._connect()
            try:
                connection.execute("UPDATE round_observation_inbox SET event_json=?,updated_at=? WHERE observation_id=? AND status='PENDING'",
                                   (self._canonical(event), datetime.now(timezone.utc).isoformat(), observation_id))
            finally:
                connection.close()

    def record_stage_failure(self, observation_id: str, error: str) -> int:
        with self._lock:
            connection = self._connect()
            try:
                row = connection.execute("SELECT attempts FROM round_observation_inbox WHERE observation_id=?",
                                         (str(observation_id),)).fetchone()
                attempts = int(row["attempts"] or 0) + 1 if row else 1
                delay = min(30.0, 0.25 * (2 ** min(attempts - 1, 7)))
                connection.execute("""UPDATE round_observation_inbox SET attempts=?,next_attempt_at=?,
                    last_error=?,updated_at=? WHERE observation_id=? AND status='PENDING'""",
                    (attempts, time.time() + delay, str(error)[:500],
                     datetime.now(timezone.utc).isoformat(), str(observation_id)))
                return attempts
            finally:
                connection.close()

    def mark_committed(self, observation_id: str, committed_sequence: int) -> None:
        self.mark_committed_many([(observation_id, committed_sequence)])

    def mark_committed_many(self, receipts: list[tuple[str, int]]) -> None:
        """Acknowledge a committed batch with one SQLite transaction."""
        if not receipts:
            return
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                connection.executemany("""UPDATE round_observation_inbox SET status='COMMITTED',
                    committed_sequence=?,last_error=NULL,updated_at=? WHERE observation_id=?""",
                    [(int(sequence), now, str(observation_id))
                     for observation_id, sequence in receipts])
                connection.execute("""DELETE FROM round_observation_inbox WHERE status='COMMITTED'
                    AND inbox_sequence NOT IN (SELECT inbox_sequence FROM round_observation_inbox
                        WHERE status='COMMITTED' ORDER BY inbox_sequence DESC LIMIT ?)""",
                    (self.tombstone_limit,))
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
            finally:
                connection.close()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            connection = self._connect()
            try:
                row = connection.execute("""SELECT COUNT(*) AS backlog_size,
                    COALESCE(SUM(length(event_json)+length(observation_json)),0) AS backlog_bytes,
                    MIN(received_at) AS oldest_received_at
                    FROM round_observation_inbox WHERE status='PENDING'""").fetchone()
                total = connection.execute("SELECT COUNT(*) AS n FROM round_observation_inbox").fetchone()
            finally:
                connection.close()
        oldest = row["oldest_received_at"]
        age_seconds = None
        if oldest:
            try:
                parsed = datetime.fromisoformat(str(oldest).replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                age_seconds = max(0.0, (datetime.now(timezone.utc) - parsed.astimezone(timezone.utc)).total_seconds())
            except (TypeError, ValueError, OverflowError):
                pass
        return {
            "backlog_size": int(row["backlog_size"] or 0),
            "backlog_bytes": int(row["backlog_bytes"] or 0),
            "capacity_records": self.max_records,
            "capacity_bytes": self.max_bytes,
            "oldest_pending_observation_age_seconds": round(age_seconds, 3) if age_seconds is not None else None,
            "oldest_pending_observation_received_at": oldest,
            "retained_tombstones": int(total["n"] or 0) - int(row["backlog_size"] or 0),
        }
