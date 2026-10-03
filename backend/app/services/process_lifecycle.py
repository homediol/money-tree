"""Persistent process lifecycle diagnostics.

The supervisor and the API append one JSON object per lifecycle event.  This
file is deliberately append-only so the next process can explain what the
previous process did before it exited.
"""
from __future__ import annotations

import json
import os
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ProcessLifecycle:
    def __init__(self, path: Path, *, service: str = "winner-predict-backend"):
        self.path = Path(path)
        self.service = service
        self.started_at = _now()
        self.started_monotonic = time.monotonic()
        self._lock = Lock()
        self._signals: list[str] = []
        self.repository = None

    def bind_repository(self, repository) -> None:
        """Switch lifecycle history to PostgreSQL and migrate legacy file rows once."""
        self.repository = repository
        self.repository.save_process_lifecycle_events(self._file_events())

    def _file_events(self) -> list[dict]:
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        result = []
        for line in lines[-5000:]:
            try:
                value = json.loads(line)
            except (TypeError, json.JSONDecodeError):
                continue
            if isinstance(value, dict):
                result.append(value)
        return result

    def record(self, event: str, **fields) -> dict:
        payload = {
            "event": event,
            "service": self.service,
            "timestamp": _now(),
            "pid": os.getpid(),
            "ppid": os.getppid(),
            **fields,
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            line = json.dumps(payload, sort_keys=True, default=str) + "\n"
            with self._lock, self.path.open("a", encoding="utf-8") as handle:
                handle.write(line)
                handle.flush()
                os.fsync(handle.fileno())
        except Exception:
            # Diagnostics must never be able to terminate the API.
            pass
        if self.repository is not None:
            try:
                self.repository.save_process_lifecycle_event(payload)
            except Exception:
                # File diagnostics remain a recovery trail if PostgreSQL itself
                # is unavailable, but database backed APIs never claim the
                # failed event was persisted.
                pass
        return payload

    def start(self, *, startup_reason: str, previous_pid: int | None = None,
              restart_count: int = 0) -> dict:
        return self.record(
            "backend_start", startup_reason=startup_reason,
            previous_pid=previous_pid, restart_count=restart_count,
        )

    def exception(self, error: BaseException, *, phase: str) -> dict:
        return self.record(
            "backend_exception", phase=phase, error_type=type(error).__name__,
            reason=str(error), traceback="".join(
                traceback.format_exception(type(error), error, error.__traceback__)
            ),
        )

    def signal(self, signal_name: str) -> dict:
        self._signals.append(signal_name)
        return self.record("backend_signal", signal=signal_name)

    def shutdown(self, *, reason: str, exit_code: int | None = None) -> dict:
        return self.record(
            "backend_shutdown", shutdown_reason=reason,
            received_signals=list(self._signals), exit_code=exit_code,
            uptime_seconds=round(time.monotonic() - self.started_monotonic, 3),
        )

    def events(self) -> list[dict]:
        if self.repository is not None:
            try:
                return self.repository.list_process_lifecycle_events(5000)
            except Exception:
                return []
        return self._file_events()

    def status(self) -> dict:
        events = self.events()
        starts = [e for e in events if e.get("event") == "backend_start"]
        # A rejected competing launch never owned the backend lock. Keep it
        # in events, but do not count it as an authoritative backend restart.
        authoritative_pids = {e.get("pid") for e in starts}
        exits = [e for e in events if e.get("event") == "supervisor_exit"
                 and e.get("pid") in authoritative_pids]
        unexpected = [e for e in exits if e.get("unexpected")]
        previous = starts[-2] if len(starts) > 1 else None
        shutdowns = [e for e in events if e.get("event") in {"supervisor_exit", "backend_shutdown"}
                     and e.get("pid") in authoritative_pids]
        last_exit = shutdowns[-1] if shutdowns else None
        return {
            "status": "RUNNING",
            "pid": os.getpid(),
            "ppid": os.getppid(),
            "started_at": self.started_at,
            "uptime_seconds": round(time.monotonic() - self.started_monotonic, 3),
            "restart_count": max(0, len(starts) - 1),
            "unexpected_restart_count": len(unexpected),
            "rejected_duplicate_launch_count": sum(
                e.get("event") == "backend_exception" and e.get("error_type") == "SingleInstanceError"
                for e in events
            ),
            "previous_pid": previous.get("pid") if previous else None,
            "last_restart": starts[-1].get("timestamp") if len(starts) > 1 else None,
            "last_shutdown_reason": (last_exit or {}).get("reason") or
                                    (last_exit or {}).get("shutdown_reason"),
            "startup_reason": (starts[-1] if starts else {}).get("startup_reason"),
            "received_signals": list(self._signals),
            "events": events[-50:],
        }
