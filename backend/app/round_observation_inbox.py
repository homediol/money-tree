"""Small durable API-side handoff for pending round observations.

The collector already has its own durable outbox. This local SQLite inbox lets
the API acknowledge an observation and publish it to connected SSE clients
without waiting for PostgreSQL. Rows remain available to reconnecting clients
until a committed event is durably recorded in PostgreSQL.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import threading
import time
from functools import partial
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


async def run_round_observation_inbox(app: Any, function, *args, priority: bool = False, **kwargs):
    """Keep local durable inbox I/O off the shared PostgreSQL worker pool."""
    executor_name = ("round_observation_inbox_priority_executor" if priority
                     else "round_observation_inbox_executor")
    executor = getattr(app.state, executor_name, None)
    if executor is None:
        return await asyncio.to_thread(function, *args, **kwargs)
    loop = asyncio.get_running_loop()
    submitted_at = time.perf_counter()
    call = partial(function, *args, **kwargs)

    def run():
        started_at = time.perf_counter()
        result = call()
        completed_at = time.perf_counter()
        if priority:
            log.info("[ROUND_INBOX_EXECUTOR] operation=%s queue_ms=%.3f work_ms=%.3f",
                     getattr(function, "__name__", "unknown"),
                     (started_at - submitted_at) * 1000.0,
                     (completed_at - started_at) * 1000.0)
        return result

    return await loop.run_in_executor(executor, run)


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
        # The collector keeps each round in its own fsynced outbox until the
        # committed event is acknowledged. This inbox is a replay cache for
        # observed-stage delivery, so NORMAL avoids forcing two disk flushes
        # per round without making the collector's source of truth volatile.
        connection.execute("PRAGMA synchronous=NORMAL")
        # Keep SQLite's automatic WAL checkpoint off the receipt transaction.
        # A passive checkpoint runs from the background monitor instead.
        connection.execute("PRAGMA wal_autocheckpoint=0")
        return connection

    def checkpoint(self, *, min_wal_bytes: int = 4 * 1024 * 1024) -> dict[str, int | bool]:
        """Checkpoint a large WAL outside the collector acknowledgement path."""
        wal_path = Path(f"{self.path}-wal")
        try:
            wal_bytes = wal_path.stat().st_size
        except FileNotFoundError:
            return {"skipped": True, "wal_bytes": 0, "frames": 0, "checkpointed": 0}
        if wal_bytes < max(0, int(min_wal_bytes)):
            return {"skipped": True, "wal_bytes": wal_bytes, "frames": 0, "checkpointed": 0}

        started = time.perf_counter()
        connection = self._connect()
        try:
            busy, frames, checkpointed = connection.execute(
                "PRAGMA wal_checkpoint(PASSIVE)").fetchone()
        finally:
            connection.close()
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        result = {"skipped": False, "wal_bytes": wal_bytes,
                  "frames": int(frames or 0), "checkpointed": int(checkpointed or 0)}
        if busy or result["checkpointed"] < result["frames"] or elapsed_ms >= 250.0:
            log.warning("[ROUND_INBOX_CHECKPOINT] busy=%s frames=%s checkpointed=%s wal_bytes=%s elapsed_ms=%.3f",
                        busy, frames, checkpointed, wal_bytes, elapsed_ms)
        return result

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
                        rules_processed INTEGER NOT NULL DEFAULT 0,
                        committed_event_json TEXT,
                        committed_rules_processed INTEGER NOT NULL DEFAULT 1,
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
                    CREATE TABLE IF NOT EXISTS round_observation_inbox_counters (
                        singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                        pending_records INTEGER NOT NULL,
                        pending_bytes INTEGER NOT NULL
                    );
                """)
                columns = {str(row["name"]) for row in connection.execute(
                    "PRAGMA table_info(round_observation_inbox)").fetchall()}
                if "rules_processed" not in columns:
                    connection.execute("ALTER TABLE round_observation_inbox "
                                       "ADD COLUMN rules_processed INTEGER NOT NULL DEFAULT 0")
                if "committed_event_json" not in columns:
                    connection.execute("ALTER TABLE round_observation_inbox "
                                       "ADD COLUMN committed_event_json TEXT")
                if "committed_rules_processed" not in columns:
                    # Existing committed rows predate durable commit-stage replay.
                    # Mark those as processed; every new commit below stores its
                    # replay payload and explicitly resets this bit to zero.
                    connection.execute("ALTER TABLE round_observation_inbox "
                                       "ADD COLUMN committed_rules_processed INTEGER NOT NULL DEFAULT 1")
                connection.execute("""CREATE INDEX IF NOT EXISTS round_observation_inbox_rules_pending_idx
                    ON round_observation_inbox(status,rules_processed,inbox_sequence)""")
                connection.execute("""CREATE INDEX IF NOT EXISTS round_observation_inbox_committed_rules_pending_idx
                    ON round_observation_inbox(status,committed_rules_processed,inbox_sequence)""")
                # Older inbox versions did not track this stage. Replay only
                # verified observations; unverified rows cannot create a rule
                # decision and should not hold startup recovery open.
                connection.execute("""UPDATE round_observation_inbox
                    SET rules_processed=1
                    WHERE status='PENDING' AND rules_processed=0
                      AND (COALESCE(json_extract(event_json,'$.event_type'),'')!='round.observed'
                        OR COALESCE(json_extract(event_json,'$.durability'),'')!='PENDING'
                        OR COALESCE(json_extract(event_json,'$.continuity_status'),'')!='VERIFIED')""")
                if connection.execute(
                    "SELECT 1 FROM round_observation_inbox_counters WHERE singleton=1"
                ).fetchone() is None:
                    pending = connection.execute("""SELECT COUNT(*) AS records,
                        COALESCE(SUM(length(CAST(event_json AS BLOB))
                            + length(CAST(observation_json AS BLOB))),0) AS bytes
                        FROM round_observation_inbox WHERE status='PENDING'""").fetchone()
                    connection.execute("""INSERT INTO round_observation_inbox_counters
                        (singleton,pending_records,pending_bytes) VALUES(1,?,?)""",
                        (int(pending["records"] or 0), int(pending["bytes"] or 0)))
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

    @staticmethod
    def _requires_rule_processing(event: dict[str, Any]) -> bool:
        return (event.get("event_type") == "round.observed"
                and event.get("durability") == "PENDING"
                and event.get("continuity_status") == "VERIFIED")

    def append(self, events: list[dict[str, Any]], observations: list[dict[str, Any]],
               *, received_at: str) -> dict[str, Any]:
        if len(events) != len(observations):
            raise ValueError("round_observation_batch_length_mismatch")
        append_started = time.perf_counter()
        lock_requested = append_started
        accepted: list[dict[str, Any]] = []
        duplicate_count = 0
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            lock_acquired = time.perf_counter()
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                pending = connection.execute("""SELECT pending_records,pending_bytes
                    FROM round_observation_inbox_counters WHERE singleton=1""").fetchone()
                pending_records = int(pending["pending_records"])
                pending_bytes = int(pending["pending_bytes"])
                for event, observation in zip(events, observations):
                    observation_id, round_id = self._validate(event, observation)
                    event_json = self._canonical(event)
                    observation_json = self._canonical(observation)
                    prior = connection.execute("""SELECT event_json,observation_json,status,rules_processed
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
                            prior_requires_rules = self._requires_rule_processing(prior_event)
                            new_requires_rules = self._requires_rule_processing(event)
                            if prior_requires_rules and not new_requires_rules:
                                # A lower-trust duplicate must not replace the
                                # verified event that still needs processing.
                                accepted.append(prior_event)
                                continue
                            prior_bytes = len(prior["event_json"].encode("utf-8")) + len(prior["observation_json"].encode("utf-8"))
                            event_bytes = len(event_json.encode("utf-8")) + len(observation_json.encode("utf-8"))
                            if pending_bytes - prior_bytes + event_bytes > self.max_bytes:
                                raise ObservationInboxCapacityError(
                                    f"round observation inbox capacity reached ({pending_records}/{self.max_records} records, "
                                    f"{pending_bytes - prior_bytes + event_bytes}/{self.max_bytes} bytes); no event was discarded")
                            rules_processed = int(prior["rules_processed"] or 0)
                            if new_requires_rules and not prior_requires_rules:
                                rules_processed = 0
                            elif not new_requires_rules:
                                rules_processed = 1
                            connection.execute("""UPDATE round_observation_inbox
                                SET event_json=?,observation_json=?,received_at=?,updated_at=?,rules_processed=?
                                WHERE observation_id=?""",
                                (event_json, observation_json, received_at, now, rules_processed, observation_id))
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
                    rules_processed = int(not self._requires_rule_processing(event))
                    connection.execute("""INSERT INTO round_observation_inbox
                        (observation_id,round_id,event_json,observation_json,received_at,updated_at,rules_processed)
                        VALUES(?,?,?,?,?,?,?)""",
                        (observation_id, round_id, event_json, observation_json, received_at, now,
                         rules_processed))
                    pending_records += 1
                    pending_bytes += event_bytes
                    accepted.append(event)
                connection.execute("""UPDATE round_observation_inbox_counters
                    SET pending_records=?,pending_bytes=? WHERE singleton=1""",
                    (pending_records, pending_bytes))
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
            finally:
                connection.close()
        append_finished = time.perf_counter()
        append_ms = (append_finished - append_started) * 1000.0
        if append_ms >= 250.0:
            log.warning("[ROUND_INBOX_APPEND] batch=%s lock_wait_ms=%.3f work_ms=%.3f total_ms=%.3f",
                        len(events), (lock_acquired - lock_requested) * 1000.0,
                        (append_finished - lock_acquired) * 1000.0, append_ms)
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

    def pending_rule_events_page(self, *, after_sequence: int = 0,
                                 limit: int = 500) -> dict[str, Any]:
        """Return unprocessed verified observations for ordered crash recovery."""
        bounded = min(max(int(limit), 1), 500)
        cursor = max(0, int(after_sequence))
        connection = self._connect()
        try:
            rows = connection.execute("""SELECT inbox_sequence,event_json FROM round_observation_inbox
                WHERE status='PENDING' AND rules_processed=0 AND inbox_sequence>?
                ORDER BY inbox_sequence LIMIT ?""", (cursor, bounded + 1)).fetchall()
        finally:
            connection.close()
        has_more = len(rows) > bounded
        page_rows = rows[:bounded]
        events = [json.loads(row["event_json"]) for row in page_rows]
        next_cursor = int(page_rows[-1]["inbox_sequence"]) if page_rows else cursor
        return {"events": events, "next_cursor": next_cursor, "has_more": has_more}

    def pending_committed_rule_events_page(self, *, after_sequence: int = 0,
                                           limit: int = 500) -> dict[str, Any]:
        """Return committed events not yet applied to ordered Rules Engine state."""
        bounded = min(max(int(limit), 1), 500)
        cursor = max(0, int(after_sequence))
        connection = self._connect()
        try:
            rows = connection.execute("""SELECT inbox_sequence,committed_event_json
                FROM round_observation_inbox
                WHERE status='COMMITTED' AND committed_rules_processed=0
                  AND committed_event_json IS NOT NULL AND inbox_sequence>?
                ORDER BY inbox_sequence LIMIT ?""", (cursor, bounded + 1)).fetchall()
        finally:
            connection.close()
        has_more = len(rows) > bounded
        page_rows = rows[:bounded]
        events = [json.loads(row["committed_event_json"]) for row in page_rows]
        for event in events:
            event["delivery_mode"] = "RECOVERED"
        next_cursor = int(page_rows[-1]["inbox_sequence"]) if page_rows else cursor
        return {"events": events, "next_cursor": next_cursor, "has_more": has_more}

    def mark_rules_processed(self, observation_id: str) -> bool:
        """Durably acknowledge completion of the observed Rules Engine stage."""
        with self._lock:
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                cursor = connection.execute("""UPDATE round_observation_inbox
                    SET rules_processed=1,updated_at=?
                    WHERE observation_id=? AND status='PENDING' AND rules_processed=0""",
                    (datetime.now(timezone.utc).isoformat(), str(observation_id)))
                connection.execute("COMMIT")
                return cursor.rowcount > 0
            except Exception:
                connection.execute("ROLLBACK")
                raise
            finally:
                connection.close()

    def mark_rules_processed_many(self, receipts: list[tuple[str, str]]) -> int:
        """Acknowledge covered rule events in one SQLite transaction."""
        if not receipts:
            return 0
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                connection.executemany("""UPDATE round_observation_inbox
                    SET rules_processed=1,updated_at=?
                    WHERE observation_id=? AND status='PENDING' AND rules_processed=0""",
                    [(now, str(observation_id)) for stage, observation_id in receipts
                     if stage == "observed"])
                connection.executemany("""UPDATE round_observation_inbox
                    SET committed_rules_processed=1,updated_at=?
                    WHERE observation_id=? AND status='COMMITTED'
                      AND committed_rules_processed=0""",
                    [(now, str(observation_id)) for stage, observation_id in receipts
                     if stage == "committed"])
                changed = max(0, connection.total_changes)
                connection.execute("COMMIT")
                return changed
            except Exception:
                connection.execute("ROLLBACK")
                raise
            finally:
                connection.close()

    def mark_committed_rules_processed(self, observation_id: str) -> bool:
        """Durably acknowledge commit-stage processing after Rules Engine success."""
        with self._lock:
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                cursor = connection.execute("""UPDATE round_observation_inbox
                    SET committed_rules_processed=1,updated_at=?
                    WHERE observation_id=? AND status='COMMITTED'
                      AND committed_rules_processed=0""",
                    (datetime.now(timezone.utc).isoformat(), str(observation_id)))
                connection.execute("COMMIT")
                return cursor.rowcount > 0
            except Exception:
                connection.execute("ROLLBACK")
                raise
            finally:
                connection.close()

    def has_pending_round_index(self, round_index: int) -> bool:
        """Fail closed if the next target was already observed but not committed."""
        connection = self._connect()
        try:
            row = connection.execute("""SELECT 1 FROM round_observation_inbox
                WHERE status='PENDING'
                  AND CAST(json_extract(event_json, '$.round_index') AS INTEGER)=?
                LIMIT 1""", (int(round_index),)).fetchone()
        finally:
            connection.close()
        return row is not None

    def pending_round_for_index(self, round_index: int) -> dict[str, Any] | None:
        """Return the verified observation needed to account for a late target.

        A boolean presence check prevents a backdated selection, but the Rules
        Engine also needs the observed multiplier to advance its private policy
        state without recording a bet. Conflicting observations fail closed.
        """
        connection = self._connect()
        try:
            rows = connection.execute("""SELECT observation_id,inbox_sequence,event_json
                FROM round_observation_inbox
                WHERE status='PENDING'
                  AND CAST(json_extract(event_json, '$.round_index') AS INTEGER)=?
                ORDER BY inbox_sequence""", (int(round_index),)).fetchall()
        finally:
            connection.close()
        if not rows:
            return None
        events = []
        identities = set()
        for row in rows:
            event = json.loads(row["event_json"])
            identity = (event.get("stream_id"), event.get("round_id"), event.get("round_index"))
            identities.add(identity)
            event.setdefault("observation_id", row["observation_id"])
            event["pending_sequence"] = int(row["inbox_sequence"])
            events.append(event)
        if len(identities) != 1:
            return None
        return events[-1]

    def pending_page(self, *, after_sequence: int = 0, limit: int = 100,
                     observation_id: str | None = None, recovered: bool = True) -> dict[str, Any]:
        bounded = min(max(int(limit), 1), 500)
        cursor = max(0, int(after_sequence))
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
        event_json = self._canonical(event)
        with self._lock:
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                prior = connection.execute("""SELECT length(CAST(event_json AS BLOB)) AS event_bytes
                    FROM round_observation_inbox WHERE observation_id=? AND status='PENDING'""",
                    (observation_id,)).fetchone()
                connection.execute("UPDATE round_observation_inbox SET event_json=?,updated_at=? WHERE observation_id=? AND status='PENDING'",
                                   (event_json, datetime.now(timezone.utc).isoformat(), observation_id))
                if prior is not None:
                    connection.execute("""UPDATE round_observation_inbox_counters
                        SET pending_bytes=pending_bytes+?-? WHERE singleton=1""",
                        (len(event_json.encode("utf-8")), int(prior["event_bytes"] or 0)))
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
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

    def mark_committed_many(self, receipts: list[tuple]) -> None:
        """Acknowledge a committed batch with one SQLite transaction."""
        if not receipts:
            return
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                ids = list(dict.fromkeys(str(receipt[0]) for receipt in receipts))
                pending_delta = {"records": 0, "bytes": 0}
                # Receipt batches are bounded by the collector API. Use chunks
                # to stay below SQLite's variable limit on older deployments.
                for offset in range(0, len(ids), 500):
                    chunk = ids[offset:offset + 500]
                    placeholders = ",".join("?" for _ in chunk)
                    pending = connection.execute(f"""SELECT COUNT(*) AS records,
                        COALESCE(SUM(length(CAST(event_json AS BLOB))
                            + length(CAST(observation_json AS BLOB))),0) AS bytes
                        FROM round_observation_inbox WHERE status='PENDING'
                          AND observation_id IN ({placeholders})""", chunk).fetchone()
                    pending_delta["records"] += int(pending["records"] or 0)
                    pending_delta["bytes"] += int(pending["bytes"] or 0)
                normalized = []
                for receipt in receipts:
                    observation_id, sequence = receipt[:2]
                    event = receipt[2] if len(receipt) > 2 else None
                    normalized.append((int(sequence),
                        self._canonical(event) if isinstance(event, dict) else None,
                        0 if isinstance(event, dict) else 1, now, str(observation_id)))
                connection.executemany("""UPDATE round_observation_inbox SET status='COMMITTED',
                    committed_sequence=?,committed_event_json=COALESCE(?,committed_event_json),
                    committed_rules_processed=CASE
                        WHEN status='COMMITTED' AND committed_rules_processed=1 THEN 1
                        ELSE ? END,
                    last_error=NULL,updated_at=? WHERE observation_id=?""", normalized)
                if pending_delta["records"]:
                    connection.execute("""UPDATE round_observation_inbox_counters SET
                        pending_records=MAX(0,pending_records-?),pending_bytes=MAX(0,pending_bytes-?)
                        WHERE singleton=1""",
                        (pending_delta["records"], pending_delta["bytes"]))
                latest_sequence = int(connection.execute(
                    "SELECT COALESCE(MAX(inbox_sequence),0) FROM round_observation_inbox"
                ).fetchone()[0] or 0)
                if latest_sequence and latest_sequence % 1000 < len(ids):
                    cutoff = latest_sequence - self.tombstone_limit
                    if cutoff > 0:
                        connection.execute("""DELETE FROM round_observation_inbox
                            WHERE status='COMMITTED' AND committed_rules_processed=1
                              AND inbox_sequence<=?""", (cutoff,))
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
            finally:
                connection.close()

    def snapshot(self) -> dict[str, Any]:
        connection = self._connect()
        try:
            counters = connection.execute("""SELECT pending_records,pending_bytes
                FROM round_observation_inbox_counters WHERE singleton=1""").fetchone()
            oldest_row = connection.execute("""SELECT MIN(received_at) AS oldest_received_at
                FROM round_observation_inbox WHERE status='PENDING'""").fetchone()
            total = connection.execute("SELECT COUNT(*) AS n FROM round_observation_inbox").fetchone()
            committed_pending = connection.execute("""SELECT COUNT(*) AS n
                FROM round_observation_inbox WHERE status='COMMITTED'
                  AND committed_rules_processed=0""").fetchone()
        finally:
            connection.close()
        oldest = oldest_row["oldest_received_at"]
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
            "backlog_size": int(counters["pending_records"] or 0),
            "backlog_bytes": int(counters["pending_bytes"] or 0),
            "capacity_records": self.max_records,
            "capacity_bytes": self.max_bytes,
            "oldest_pending_observation_age_seconds": round(age_seconds, 3) if age_seconds is not None else None,
            "oldest_pending_observation_received_at": oldest,
            "retained_tombstones": int(total["n"] or 0) - int(counters["pending_records"] or 0),
            "committed_rules_pending": int(committed_pending["n"] or 0),
        }
