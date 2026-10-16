"""Bounded, process-local round observations. Nothing in this buffer is durable."""
from __future__ import annotations

from collections import OrderedDict
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from threading import RLock
from typing import Any, Mapping
from uuid import uuid4


DEFAULT_ROUND_BUFFER_CAPACITY = 5000


class InMemoryRoundBuffer:
    """Chronological, deduplicated round window cleared on process restart."""

    def __init__(self, capacity: int = DEFAULT_ROUND_BUFFER_CAPACITY):
        self.capacity = max(100, int(capacity))
        self._lock = RLock()
        self._epoch_id = uuid4().hex
        self._collector_session_id: str | None = None
        self._sequence = 0
        self._rows: list[dict[str, Any]] = []
        self._seen: OrderedDict[str, str] = OrderedDict()
        self._reset_count = 0

    @staticmethod
    def _fingerprint(row: Mapping[str, Any]) -> str:
        stable = {key: row.get(key) for key in (
            "multiplier", "timestamp", "observed_at", "platform_round_id",
            "platform_round_index", "round_index", "continuity_verified", "gap_before",
        )}
        return hashlib.sha256(json.dumps(stable, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()

    def reset(self, reason: str = "PROCESS_RESTART") -> None:
        with self._lock:
            self._epoch_id = uuid4().hex
            self._collector_session_id = None
            self._sequence = 0
            self._rows.clear()
            self._seen.clear()
            self._reset_count += 1

    def append(self, raw: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(raw, Mapping):
            raise ValueError("ROUND_OBSERVATION_REQUIRED")
        round_id = str(raw.get("round_id") or raw.get("observation_id") or "").strip()
        try:
            multiplier = float(raw.get("multiplier"))
        except (TypeError, ValueError):
            raise ValueError("ROUND_MULTIPLIER_INVALID") from None
        if not round_id or multiplier < 1 or multiplier == float("inf") or multiplier != multiplier:
            raise ValueError("ROUND_IDENTITY_OR_MULTIPLIER_INVALID")
        observed_at = str(raw.get("observed_at") or raw.get("timestamp") or datetime.now(timezone.utc).isoformat())
        try:
            stamp = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
        except ValueError:
            raise ValueError("ROUND_OBSERVATION_TIMESTAMP_INVALID") from None
        if stamp.tzinfo is None:
            raise ValueError("ROUND_OBSERVATION_TIMESTAMP_INVALID")

        with self._lock:
            collector_id = str(raw.get("collector_session_id") or "") or None
            reset = bool(self._collector_session_id and collector_id
                         and self._collector_session_id != collector_id)
            if reset:
                self._epoch_id = uuid4().hex
                self._sequence = 0
                self._rows.clear()
                self._seen.clear()
                self._reset_count += 1
            if collector_id:
                self._collector_session_id = collector_id

            fingerprint = self._fingerprint(raw)
            prior = self._seen.get(round_id)
            if prior is not None:
                if prior != fingerprint:
                    raise ValueError("ROUND_OBSERVATION_IDENTITY_CONFLICT")
                existing = next((row for row in self._rows if row["round_id"] == round_id), None)
                return {"inserted": False, "reset": reset, "round": deepcopy(existing),
                        "snapshot": self.snapshot()}

            self._sequence += 1
            try:
                round_index = int(raw.get("round_index") or self._sequence)
            except (TypeError, ValueError):
                round_index = self._sequence
            row = {
                **deepcopy(dict(raw)),
                "round_id": round_id,
                "observation_id": str(raw.get("observation_id") or round_id),
                "round_index": round_index,
                "multiplier": round(multiplier, 4),
                "observed_at": observed_at,
                "stream_id": self._epoch_id,
                "stream_sequence": self._sequence,
                "continuity_status": "VERIFIED" if raw.get("continuity_verified") is True else "UNKNOWN",
                "durability": "IN_MEMORY",
                "event_type": "round.observed",
            }
            self._rows.append(row)
            # Collector observations are sequential. Sort defensively by their
            # provider/local order, then timestamp and receive sequence.
            self._rows.sort(key=lambda item: (
                int(item.get("round_index") or 0), item.get("observed_at") or "",
                int(item.get("stream_sequence") or 0)))
            self._seen[round_id] = fingerprint
            while len(self._rows) > self.capacity:
                removed = self._rows.pop(0)
                self._seen.pop(str(removed.get("round_id")), None)
            return {"inserted": True, "reset": reset, "round": deepcopy(row),
                    "snapshot": self.snapshot()}

    def snapshot(self, limit: int | None = None) -> dict[str, Any]:
        with self._lock:
            rows = self._rows[-max(1, int(limit)):] if limit is not None else self._rows
            return {"epoch_id": self._epoch_id, "collector_session_id": self._collector_session_id,
                    "sequence": self._sequence, "count": len(self._rows),
                    "capacity": self.capacity, "reset_count": self._reset_count,
                    "continuity_after_restart": False,
                    "rounds": deepcopy(rows),
                    "latest": deepcopy(self._rows[-1]) if self._rows else None}

