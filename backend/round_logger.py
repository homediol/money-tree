"""
round_logger.py
Persistent JSON-backed round store.

Thread-safe via a re-entrant lock.  Keeps the latest *MAX_ROUNDS* rounds
in ``data/roundhistory.json`` so the file never grows unbounded.
"""

import json
import threading
from pathlib import Path
from typing import List, Optional


# ── Constants ──────────────────────────────────────────────────────────────

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DATA_FILE = DATA_DIR / "roundhistory.json"
MAX_ROUNDS = 10_000


# ── Thread-safe store ─────────────────────────────────────────────────────

class RoundStore:
    """
    JSON-backed append-only store that keeps at most ``MAX_ROUNDS`` rounds.
    """

    def __init__(self, path: Path = DATA_FILE, max_rounds: int = MAX_ROUNDS):
        self._path = path
        self._max_rounds = max_rounds
        self._lock = threading.RLock()
        self._rounds: List[dict] = []
        self._init_storage()
        self._load()

    def _init_storage(self) -> None:
        """Create the data directory and seed an empty file if needed."""
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            if not self._path.exists():
                self._path.write_text("[]", encoding="utf-8")
        except OSError as exc:
            import logging
            logging.getLogger(__name__).error(
                "Cannot initialise round store at %s: %s", self._path, exc
            )

    # ── Internal helpers ───────────────────────────────────────────────

    def _load(self) -> None:
        """Load rounds from disk (up to ``MAX_ROUNDS``)."""
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            if not isinstance(data, list):
                data = []
        except (json.JSONDecodeError, OSError):
            data = []
        # Normalise: alias round_index -> round_id so last_round_id() works
        for rec in data:
            if "round_index" in rec and "round_id" not in rec:
                rec["round_id"] = rec["round_index"]
        self._rounds = data[-self._max_rounds:]

    def _flush(self) -> None:
        """Write in-memory list to disk atomically.

        Uses a sibling .tmp file then os.replace() which is atomic on POSIX.
        The parent directory and the target file are guaranteed to exist before
        os.replace() is called, avoiding FileNotFoundError on first write.
        """
        import os
        # Always ensure directory exists (handles the case where data/ was deleted)
        self._path.parent.mkdir(parents=True, exist_ok=True)

        # Ensure target exists so os.replace() never raises FileNotFoundError
        if not self._path.exists():
            self._path.write_text("[]", encoding="utf-8")

        tmp = self._path.with_suffix(".tmp")
        try:
            tmp.write_text(
                json.dumps(self._rounds, ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8",
            )
            os.replace(str(tmp), str(self._path))
        except OSError:
            # Clean up orphaned tmp file if something went wrong
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            raise

    # ── Public API ─────────────────────────────────────────────────────

    def append(self, round_data: dict) -> None:
        """Append a single round.  Trims if exceeding ``MAX_ROUNDS``."""
        with self._lock:
            self._rounds.append(round_data)
            if len(self._rounds) > self._max_rounds:
                self._rounds = self._rounds[-self._max_rounds:]
            self._flush()

    def append_batch(self, rounds: List[dict]) -> None:
        """Append several rounds at once (more efficient for bulk loads)."""
        with self._lock:
            self._rounds.extend(rounds)
            if len(self._rounds) > self._max_rounds:
                self._rounds = self._rounds[-self._max_rounds:]
            self._flush()

    def all(self) -> List[dict]:
        """Return a shallow copy of all stored rounds."""
        with self._lock:
            return list(self._rounds)

    def latest(self, n: int = 1) -> List[dict]:
        """Return the latest *n* rounds (or as many as exist)."""
        with self._lock:
            return list(self._rounds[-n:])

    def last_round_id(self) -> Optional[int]:
        """Return the ``round_id`` of the most recent round, or ``None``."""
        with self._lock:
            if not self._rounds:
                return None
            return self._rounds[-1]["round_id"]

    def count(self) -> int:
        """Return the number of rounds currently stored."""
        with self._lock:
            return len(self._rounds)

    def clear(self) -> None:
        """Remove all rounds (testing / reset)."""
        with self._lock:
            self._rounds.clear()
            self._flush()


# ── Module-level singleton ────────────────────────────────────────────────

_store: Optional[RoundStore] = None


def get_store() -> RoundStore:
    """Return the module-level RoundStore singleton."""
    global _store
    if _store is None:
        _store = RoundStore()
    return _store


def append_round(round_data: dict) -> None:
    """Convenience: append a round to the global store."""
    get_store().append(round_data)


def get_all_rounds() -> List[dict]:
    """Convenience: return all rounds."""
    return get_store().all()


def get_latest(n: int = 1) -> List[dict]:
    """Convenience: return the latest *n* rounds."""
    return get_store().latest(n)


def get_last_round_id() -> Optional[int]:
    """Convenience: return the last round id."""
    return get_store().last_round_id()


def round_count() -> int:
    """Convenience: return the number of stored rounds."""
    return get_store().count()



