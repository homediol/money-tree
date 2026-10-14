"""Cross-process lock shared with the Node collector for short betting UI writes."""
from __future__ import annotations

import asyncio
import json
import os
import socket
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
LOCK_DIR = Path(os.environ.get(
    "WINNER_SHARED_AVIATOR_UI_LOCK",
    ROOT / "data" / "bot" / "shared-aviator-ui.lock",
))


def _process_exists(pid: int) -> bool | None:
    if pid <= 1:
        return None
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _process_start_ticks(pid: int) -> int | None:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        return int(stat[stat.rfind(")") + 2:].split()[19])
    except (OSError, ValueError, IndexError):
        return None


async def _acquire(timeout_s: float, poll_s: float) -> dict:
    owner = {
        "pid": os.getpid(),
        "host": socket.gethostname(),
        "token": uuid.uuid4().hex,
        "process_start_ticks": _process_start_ticks(os.getpid()),
        "acquired_at": datetime.now(timezone.utc).isoformat(),
    }
    LOCK_DIR.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            LOCK_DIR.mkdir(mode=0o700)
            try:
                owner_path = LOCK_DIR / "owner.json"
                fd = os.open(owner_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as stream:
                    json.dump(owner, stream)
                    stream.flush()
                    os.fsync(stream.fileno())
                return owner
            except Exception:
                try:
                    LOCK_DIR.rmdir()
                except OSError:
                    pass
                raise
        except FileExistsError:
            try:
                owner_path = LOCK_DIR / "owner.json"
                existing = json.loads(owner_path.read_text(encoding="utf-8"))
                existing_pid = int(existing.get("pid") or 0)
                current_start_ticks = _process_start_ticks(existing_pid)
                owner_was_reused = (
                    existing.get("process_start_ticks") is not None
                    and current_start_ticks is not None
                    and int(existing["process_start_ticks"]) != current_start_ticks
                )
                if (existing.get("host") == owner["host"]
                        and (_process_exists(existing_pid) is False or owner_was_reused)):
                    owner_path.unlink()
                    LOCK_DIR.rmdir()
                    continue
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                # Missing or malformed ownership is ambiguous; wait and fail closed.
                pass
            await asyncio.sleep(poll_s)
    raise TimeoutError("SHARED_AVIATOR_UI_LOCK_TIMEOUT")


def _release(owner: dict) -> None:
    owner_path = LOCK_DIR / "owner.json"
    try:
        current = json.loads(owner_path.read_text(encoding="utf-8"))
        if current.get("token") != owner.get("token") or int(current.get("pid") or 0) != os.getpid():
            return
        owner_path.unlink()
        LOCK_DIR.rmdir()
    except FileNotFoundError:
        return


@asynccontextmanager
async def shared_aviator_ui_lock(timeout_s: float = 10.0):
    owner = await _acquire(timeout_s, 0.025)
    try:
        yield
    finally:
        _release(owner)
