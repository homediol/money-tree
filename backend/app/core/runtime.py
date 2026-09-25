"""Process lifecycle helpers for the Winner Predict API."""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

try:  # POSIX production/development hosts
    import fcntl
except ImportError:  # pragma: no cover - Windows falls back to port ownership
    fcntl = None


class SingleInstanceError(RuntimeError):
    """Raised when another Winner Predict backend already owns the lock."""


class InstanceLock:
    """Advisory process lock released automatically when the process exits."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._handle = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+", encoding="utf-8")
        if fcntl is not None:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                handle.seek(0)
                owner = handle.read().strip() or "unknown owner"
                handle.close()
                raise SingleInstanceError(
                    f"another Winner Predict backend is already running ({owner})"
                ) from exc
        handle.seek(0)
        handle.truncate()
        handle.write(json.dumps({
            "pid": os.getpid(),
            "started_at": datetime.now(timezone.utc).isoformat(),
        }))
        handle.flush()
        os.fsync(handle.fileno())
        self._handle = handle

    def release(self) -> None:
        handle, self._handle = self._handle, None
        if handle is None:
            return
        try:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()

