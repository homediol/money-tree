"""Process lifecycle helpers for the Winner Predict API."""
from __future__ import annotations

import json
import os
import socket
import uuid
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

    OWNER_TYPE = "winner_predict_backend"

    def __init__(self, path: Path, *, port: int | None = None, instance_id: str | None = None):
        self.path = Path(path)
        self.port = port
        self.instance_id = instance_id or uuid.uuid4().hex
        self._handle = None
        self._metadata: dict | None = None

    @staticmethod
    def _process_start_time(pid: int) -> str | None:
        """Return a PID-reuse-resistant Linux process identity when available."""
        try:
            stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
            # comm is parenthesized and may itself contain spaces or ')'.
            fields_after_comm = stat[stat.rfind(")") + 2:].split()
            start_ticks = fields_after_comm[19]  # proc stat field 22
            boot_id = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
            return f"{boot_id}:{start_ticks}"
        except (OSError, IndexError, ValueError):
            return None

    @staticmethod
    def _process_snapshot(pid: int) -> dict | None:
        """Read PID-reuse-resistant local process details, if accessible."""
        proc = Path(f"/proc/{pid}")
        try:
            stat = (proc / "stat").read_text(encoding="utf-8")
            fields_after_comm = stat[stat.rfind(")") + 2:].split()
            boot_id = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
            command = (proc / "cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", "replace").strip()
            cwd = os.readlink(proc / "cwd")
            executable = os.readlink(proc / "exe")
            return {
                "process_start_time": f"{boot_id}:{fields_after_comm[19]}",
                "command_line": command,
                "working_directory": cwd,
                "executable": executable,
            }
        except FileNotFoundError:
            return None
        except PermissionError:
            return {"unverifiable": True}
        except (OSError, IndexError, ValueError):
            return {"unverifiable": True}

    @staticmethod
    def _looks_like_backend_process(snapshot: dict) -> bool:
        command = str(snapshot.get("command_line") or "")
        cwd = str(snapshot.get("working_directory") or "")
        return ("uvicorn" in command and "main:app" in command
                and Path(cwd).name == "backend")

    def _metadata_owner_state(self, metadata: dict | None) -> str:
        """Classify old metadata only after this process owns a free flock.

        A busy kernel lock is always authoritative and is never removed. When
        the flock is free, matching live backend identity still blocks takeover
        in case an older implementation accidentally dropped its descriptor.
        """
        if not isinstance(metadata, dict):
            return "stale"
        try:
            pid = int(metadata.get("pid"))
        except (TypeError, ValueError):
            return "stale"
        if pid <= 1:
            return "stale"
        recorded_host = metadata.get("hostname")
        if recorded_host and recorded_host != socket.gethostname():
            return "unverifiable_foreign_host"
        snapshot = self._process_snapshot(pid)
        if snapshot is None:
            return "stale"
        if snapshot.get("unverifiable"):
            return "unverifiable_process"
        recorded_start = metadata.get("process_start_time")
        if recorded_start and snapshot.get("process_start_time") != recorded_start:
            return "stale_pid_reused"
        if self._looks_like_backend_process(snapshot):
            return "live_backend_without_kernel_lock"
        return "stale"

    @staticmethod
    def _read_metadata(handle) -> dict | None:
        try:
            handle.seek(0)
            raw = handle.read().strip()
            value = json.loads(raw) if raw else None
            return value if isinstance(value, dict) else None
        except (OSError, json.JSONDecodeError):
            return None

    def status(self) -> dict:
        return {
            "owned": self._handle is not None,
            "lock_path": str(self.path),
            "fd": self._handle.fileno() if self._handle is not None else None,
            **(self._metadata or {}),
        }

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+", encoding="utf-8")
        if fcntl is not None:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                owner = self._read_metadata(handle)
                handle.close()
                raise SingleInstanceError(
                    "the backend kernel lock is held; recorded owner metadata "
                    f"(not proof of the kernel lock holder): {json.dumps(owner, sort_keys=True) if owner else 'unavailable'}"
                ) from exc
        prior_metadata = self._read_metadata(handle)
        prior_state = self._metadata_owner_state(prior_metadata)
        if prior_state not in {"stale", "stale_pid_reused"}:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()
            raise SingleInstanceError(
                f"kernel lock was free but recorded backend ownership is {prior_state}; "
                "refusing takeover without stale-owner proof"
            )
        pid = os.getpid()
        created_at = datetime.now(timezone.utc).isoformat()
        metadata = {
            "pid": pid,
            "process_start_time": self._process_start_time(pid),
            "created_at": created_at,
            "instance_id": self.instance_id,
            "port": self.port,
            "backend_port": self.port,
            "hostname": socket.gethostname(),
            "owner_type": self.OWNER_TYPE,
        }
        handle.seek(0)
        handle.truncate()
        handle.write(json.dumps(metadata, sort_keys=True))
        handle.flush()
        os.fsync(handle.fileno())
        self._handle = handle
        self._metadata = metadata

    def release(self) -> None:
        handle, self._handle = self._handle, None
        if handle is None:
            return
        try:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()
        self._metadata = None
