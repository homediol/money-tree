"""
Data Collector
==============

Loads round history from the local JSON file at
``settings.round_history_path``.  This is the only supported data source —
no live game feed, no WebSocket stream, no HTTP polling.

The collector caches results in memory after the first read and supports
incremental appending of new rounds resolved via the dashboard.
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import aiofiles

from config.settings import settings
from src.core.exceptions import DataNotFoundError, DataCorruptionError
from src.core.logger import get_logger
from src.core.helpers import parse_multiplier, utc_now_iso, safe_float


logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class RoundRecord:
    """Canonical representation of a single round from roundhistory.json."""
    round_id: int
    multiplier: float
    timestamp: Optional[float] = None
    source: str = "roundhistory"
    ingested_at: str = field(default_factory=utc_now_iso)

    def category_index(self, boundaries: List[float]) -> int:
        for i, b in enumerate(boundaries):
            if self.multiplier < b:
                return i
        return len(boundaries)

    def category_name(self, names: List[str], boundaries: List[float]) -> str:
        return names[self.category_index(boundaries)]

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_raw(cls, raw: Dict[str, Any]) -> Optional["RoundRecord"]:
        mult = parse_multiplier(
            raw.get("multiplier") or raw.get("crashPoint") or raw.get("value")
        )
        if mult is None:
            return None
        rid = raw.get("round_id") or raw.get("round_index") or raw.get("id")
        if rid is None:
            return None
        ts_raw = raw.get("timestamp")
        ts: Optional[float] = None
        if ts_raw:
            if isinstance(ts_raw, (int, float)):
                ts = float(ts_raw)
            elif isinstance(ts_raw, str):
                try:
                    ts = datetime.fromisoformat(
                        ts_raw.replace("Z", "+00:00")
                    ).timestamp()
                except ValueError:
                    ts = safe_float(ts_raw)
        return cls(
            round_id=int(rid),
            multiplier=float(mult),
            timestamp=ts,
            source="roundhistory",
        )


# ---------------------------------------------------------------------------
# Collector
# ---------------------------------------------------------------------------
class DataCollector:
    """
    Reads all rounds from ``roundhistory.json`` and caches them.

    Call ``collect()`` to get the full list.  New resolved rounds from the
    dashboard can be appended via ``append_round()`` so they are available
    for the next prediction without reloading from disk.
    """

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path: Path = path or settings.round_history_path
        self._cache: List[RoundRecord] = []
        self._cache_loaded: bool = False
        self._lock = asyncio.Lock()
        self.logger = get_logger(self.__class__.__name__)

    # ------------------------------------------------------- public
    async def collect(self, *, use_cache: bool = True) -> List[RoundRecord]:
        """Return all rounds, reading from disk on first call."""
        if use_cache and self._cache_loaded:
            return self._cache
        async with self._lock:
            # Double-checked locking
            if use_cache and self._cache_loaded:
                return self._cache
            records = await self._load()
            records = self._deduplicate(records)
            records.sort(key=lambda r: (r.round_id, r.timestamp or 0.0))
            self._cache = records
            self._cache_loaded = True
            self.logger.info(
                "Round history loaded",
                extra={"path": str(self.path), "count": len(records)},
            )
        return self._cache

    def append_round(self, record: RoundRecord) -> None:
        """Append a resolved round so future predictions include it."""
        if any(r.round_id == record.round_id for r in self._cache):
            return
        self._cache.append(record)
        self._cache.sort(key=lambda r: r.round_id)

    def append_many(self, records: List[RoundRecord]) -> int:
        before = len(self._cache)
        for r in records:
            self.append_round(r)
        return len(self._cache) - before

    def clear_cache(self) -> None:
        self._cache.clear()
        self._cache_loaded = False

    # ------------------------------------------------------- private
    async def _load(self) -> List[RoundRecord]:
        if not self.path.exists():
            raise DataNotFoundError(
                f"Round history file not found: {self.path}\n"
                "Place your roundhistory.json at that path and restart."
            )
        try:
            async with aiofiles.open(self.path, "r", encoding="utf-8") as f:
                raw = json.loads(await f.read())
        except (json.JSONDecodeError, OSError) as exc:
            raise DataCorruptionError(
                f"Cannot read round history: {exc}"
            ) from exc

        return self._parse(raw)

    @staticmethod
    def _parse(raw: Any) -> List[RoundRecord]:
        # Support { "rounds": [...] } or { "history": [...] } wrappers
        if isinstance(raw, dict):
            raw = raw.get("rounds") or raw.get("history") or raw.get("data") or []
        if not isinstance(raw, list):
            raise DataCorruptionError(
                "roundhistory.json must be a JSON array (or object wrapping one)"
            )

        records: List[RoundRecord] = []
        for index, item in enumerate(raw):
            # Plain number list  →  [1.23, 4.56, ...]
            if isinstance(item, (int, float)) and item >= 1.0:
                records.append(RoundRecord(
                    round_id=index + 1,
                    multiplier=float(item),
                    timestamp=None,
                ))
            elif isinstance(item, dict):
                rec = RoundRecord.from_raw(item)
                if rec is not None:
                    records.append(rec)
        return records

    @staticmethod
    def _deduplicate(records: List[RoundRecord]) -> List[RoundRecord]:
        seen: Dict[int, RoundRecord] = {}
        for r in records:
            existing = seen.get(r.round_id)
            if existing is None or (r.timestamp or 0) > (existing.timestamp or 0):
                seen[r.round_id] = r
        return list(seen.values())


# ---------------------------------------------------------------------------
# Singleton accessor
# ---------------------------------------------------------------------------
_default_collector: Optional[DataCollector] = None


def get_collector() -> DataCollector:
    global _default_collector
    if _default_collector is None:
        _default_collector = DataCollector()
    return _default_collector


__all__ = ["RoundRecord", "DataCollector", "get_collector"]
