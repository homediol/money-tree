"""Backend-owned lifecycle and read model for the Playwright history collector."""
from __future__ import annotations

import asyncio
import json
import math
import os
import signal
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable

from app.core.logging import get_logger

log = get_logger("HISTORY")
Broadcaster = Callable[[dict], Awaitable[None]]
ROOT = Path(__file__).resolve().parents[2]


class HistoryCollectorManager:
    """Own one Node collector subprocess; the Node side owns BrowserManager."""

    STATES = {"HEALTHY", "WAITING", "CONNECTING", "DISCONNECTED", "STALE", "ERROR", "STOPPED"}

    def __init__(self, data_path: Path, broadcaster: Broadcaster | None = None):
        self.data_path = Path(data_path)
        self.status_path = ROOT / "data" / "bot" / "status.json"
        self.entrypoint = ROOT / "bot" / "roundhistory-collector.js"
        self.broadcaster = broadcaster
        self.process: asyncio.subprocess.Process | None = None
        self.state = "STOPPED"
        self.started_at: str | None = None
        self.last_error: str | None = None
        self._paused = False
        self._watch_task: asyncio.Task | None = None
        self._ensure_file()

    def _ensure_file(self) -> None:
        if self.data_path.exists():
            return
        self.data_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.data_path.with_suffix(self.data_path.suffix + ".tmp")
        tmp.write_text("[]\n", encoding="utf-8")
        os.replace(tmp, self.data_path)

    async def _emit(self, name: str, **payload) -> None:
        if self.broadcaster:
            await self.broadcaster({"type": name, **payload})

    async def start(self) -> dict:
        if self.process and self.process.returncode is None:
            if self._paused:
                await self.resume()
            return self.status()
        self.state = "CONNECTING"
        self.last_error = None
        self.started_at = datetime.now(timezone.utc).isoformat()
        try:
            self.process = await asyncio.create_subprocess_exec(
                "node", str(self.entrypoint), cwd=str(ROOT / "bot"),
                stdout=sys.stdout, stderr=sys.stderr,
            )
            self._watch_task = asyncio.create_task(self._watch())
            await self._emit("history:connecting", status=self.status())
        except Exception as exc:
            self.state = "ERROR"
            self.last_error = str(exc)
            log.exception("collector start failed")
            await self._emit("history:error", error=self.last_error)
        return self.status()

    async def _watch(self) -> None:
        assert self.process is not None
        code = await self.process.wait()
        if self.state != "STOPPED":
            self.state = "ERROR" if code else "STOPPED"
            self.last_error = None if code == 0 else f"collector exited with code {code}"
            await self._emit("history:error" if code else "history:stopped", status=self.status())

    async def stop(self) -> dict:
        proc = self.process
        self.state = "STOPPED"
        self._paused = False
        if proc and proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), 8)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
        self.process = None
        await self._emit("history:stopped", status=self.status())
        return self.status()

    async def pause(self) -> dict:
        if self.process and self.process.returncode is None and not self._paused:
            self.process.send_signal(signal.SIGSTOP)
            self._paused = True
            self.state = "WAITING"
            await self._emit("history:waiting", status=self.status())
        return self.status()

    async def resume(self) -> dict:
        if self.process and self.process.returncode is None and self._paused:
            self.process.send_signal(signal.SIGCONT)
            self._paused = False
            self.state = "CONNECTING"
            await self._emit("history:connecting", status=self.status())
        return self.status()

    def _node_status(self) -> dict:
        try:
            value = json.loads(self.status_path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def status(self) -> dict:
        node = self._node_status()
        running = bool(self.process and self.process.returncode is None)
        state = self.state
        status_is_current = False
        try:
            status_is_current = not self.started_at or self.status_path.stat().st_mtime >= datetime.fromisoformat(self.started_at).timestamp()
        except OSError:
            pass
        if running and not self._paused and status_is_current:
            candidate = str(node.get("health", state)).upper()
            state = candidate if candidate in self.STATES else state
        elif not running and state not in {"ERROR", "STOPPED"}:
            state = "STOPPED"
        rows = self.rows()
        latest = rows[-1] if rows else None
        previous = rows[-2] if len(rows) > 1 else None
        return {
            "status": state, "running": running, "paused": self._paused,
            "count": len(rows), "latest": latest, "previous": previous,
            "last_update": latest.get("timestamp") if latest else None,
            "started_at": self.started_at, "last_error": self.last_error,
            "browser_connected": bool(node.get("browserConnected", False)),
            "frame_connected": bool(node.get("frameConnected", False)),
            "recovery_count": int(node.get("recoveryCount", 0) or 0),
        }

    def rows(self) -> list[dict]:
        try:
            payload = json.loads(self.data_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        if isinstance(payload, dict):
            payload = payload.get("rounds", payload.get("history", []))
        if not isinstance(payload, list):
            return []
        result, seen = [], set()
        for pos, raw in enumerate(payload):
            if not isinstance(raw, dict):
                raw = {"multiplier": raw}
            try:
                multiplier = float(str(raw.get("multiplier", "")).lower().replace("x", "").strip())
            except (TypeError, ValueError):
                continue
            if not math.isfinite(multiplier) or multiplier <= 0:
                continue
            round_id = str(raw.get("round_id", raw.get("round_index", raw.get("id", pos + 1))))
            if round_id in seen:
                continue
            seen.add(round_id)
            result.append({
                "round_id": round_id,
                "round_index": int(raw.get("round_index", pos + 1)),
                "timestamp": raw.get("timestamp"),
                "multiplier": round(multiplier, 4),
            })
        return sorted(result, key=lambda row: row["round_index"])

    def stats(self) -> dict:
        values = [r["multiplier"] for r in self.rows()]
        return {
            "total": len(values),
            "min": min(values) if values else None,
            "max": max(values) if values else None,
            "mean": statistics.fmean(values) if values else None,
            "median": statistics.median(values) if values else None,
            "threshold_counts": {
                "below_1_5x": sum(v < 1.5 for v in values),
                "at_least_2x": sum(v >= 2 for v in values),
                "at_least_5x": sum(v >= 5 for v in values),
                "at_least_10x": sum(v >= 10 for v in values),
            },
        }
