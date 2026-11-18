"""Central health model and SHADOW readiness gate.

Health is evidence based: a component is never reported healthy until it has
produced a successful heartbeat. The monitor observes the history collector
without controlling it.
"""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable


GLOBAL_STATES = {"STARTING", "HEALTHY", "DEGRADED", "PAUSED", "CRITICAL", "STOPPING"}


@dataclass
class ComponentHealth:
    name: str
    state: str = "UNKNOWN"
    last_heartbeat: float | None = None
    last_success: float | None = None
    latency_ms: float | None = None
    error_count: int = 0
    last_error: str | None = None
    stale_after_s: float = 30.0
    metadata: dict[str, Any] | None = None

    def public(self, now: float) -> dict[str, Any]:
        row = asdict(self)
        row["age_s"] = None if self.last_heartbeat is None else round(max(0.0, now - self.last_heartbeat), 3)
        row["success_age_s"] = None if self.last_success is None else round(max(0.0, now - self.last_success), 3)
        return row


class SystemHealth:
    COMPONENTS = (
        "browser", "history", "database", "rules_engine", "api",
    )

    def __init__(self, *, broadcaster: Callable[[dict], Awaitable[None]] | None = None,
                 history_collector=None, repository=None):
        self.broadcaster = broadcaster
        self.history_collector = history_collector
        self.repository = repository
        self.started_at = time.time()
        self.state = "STARTING"
        self.reason = "health checks have not completed"
        self._components = {name: ComponentHealth(name=name) for name in self.COMPONENTS}
        self._last_snapshot: dict[str, Any] | None = None
        self._last_alert_key: str | None = None
        self._lock = asyncio.Lock()

    @staticmethod
    def _reason_text(reason: Any) -> str:
        if isinstance(reason, str):
            return reason
        try:
            return json.dumps(reason, sort_keys=True, default=str)
        except (TypeError, ValueError):
            return str(reason)

    async def emit(self, payload: dict) -> None:
        if self.broadcaster:
            try:
                await self.broadcaster(payload)
            except Exception:
                pass

    def heartbeat(self, name: str, *, ok: bool, latency_ms: float | None = None,
                  error: str | None = None, metadata: dict[str, Any] | None = None,
                  stale_after_s: float | None = None, waiting: bool = False) -> None:
        if name not in self._components:
            self._components[name] = ComponentHealth(name=name)
        c = self._components[name]
        now = time.time()
        c.last_heartbeat = now
        c.latency_ms = latency_ms
        c.metadata = metadata or c.metadata or {}
        if stale_after_s is not None:
            c.stale_after_s = stale_after_s
        if ok:
            c.state = "HEALTHY"
            c.last_success = now
            c.last_error = None
        else:
            c.state = "WAITING" if waiting else "ERROR"
            if not waiting:
                c.error_count += 1
            c.last_error = self._reason_text(error or "health check failed")

    def _history_ok(self, now: float) -> tuple[bool, str | None, dict]:
        if self.history_collector is None:
            return False, "history collector unavailable", {}
        try:
            # SHADOW decisions require recent canonical history.
            status = self.history_collector.status()
            latest = status.get("latest") or {}
            stamp = latest.get("timestamp")
            age = None
            if stamp:
                age = max(0.0, now - datetime.fromisoformat(str(stamp).replace("Z", "+00:00")).timestamp())
            running = bool(status.get("running"))
            collector_state = str(status.get("status", "UNKNOWN")).upper()
            process_state = str(status.get("collector_process_status", "UNKNOWN")).upper()
            healthy = collector_state in {"HEALTHY", "COLLECTING"}
            fresh = age is not None and age <= 120
            ok = running and healthy and fresh
            if ok:
                reason = None
            elif process_state == "STOPPED" or collector_state == "STOPPED":
                reason = status.get("last_error") or "history collector process is stopped"
            elif process_state == "UNKNOWN" or collector_state == "UNKNOWN":
                reason = (status.get("process_reason") or status.get("reason") or status.get("last_error")
                          or "history collector process status could not be verified")
            elif not running:
                reason = status.get("last_error") or status.get("reason") or "history collector is not running"
            elif collector_state in {"CONNECTING", "WAITING", "WAITING_FOR_NETWORK", "RECOVERING", "STARTING"}:
                reason = status.get("last_error") or f"history collector is {collector_state.lower().replace('_', ' ')}"
            elif collector_state == "STALE" or (running and healthy and not fresh):
                reason = ("history collector has not saved a new round yet" if age is None
                          else f"latest saved round is {int(round(age))} seconds old; waiting for a fresh round")
            else:
                reason = status.get("last_error") or f"history collector status is {collector_state.lower().replace('_', ' ')}"
            waiting_state = process_state == "UNKNOWN" or collector_state in {
                "CONNECTING", "WAITING", "WAITING_FOR_NETWORK", "RECOVERING", "STARTING", "STALE",
            } or (running and healthy and not fresh)
            metadata = {"status": status.get("status"), "age_s": age,
                        "count": status.get("count", 0),
                        "process_status": process_state,
                        "heartbeat_state": status.get("heartbeat_state", "UNKNOWN")}
            if not ok and waiting_state:
                metadata["waiting"] = True
            return ok, reason, metadata
        except Exception as exc:
            return False, f"history check failed: {exc}", {}

    def refresh(self) -> dict[str, Any]:
        now = time.time()
        reasons: list[str] = []
        # The database must be reachable before SHADOW results can be trusted.
        db_started = time.perf_counter()
        try:
            if self.repository is None:
                raise RuntimeError("repository unavailable")
            with self.repository.connect() as conn:
                conn.execute("SELECT 1").fetchone()
            self.heartbeat("database", ok=True,
                           latency_ms=round((time.perf_counter() - db_started) * 1000, 3),
                           stale_after_s=60)
        except Exception as exc:
            self.heartbeat("database", ok=False, latency_ms=round((time.perf_counter() - db_started) * 1000, 3), error=str(exc))
            reasons.append("database unavailable")

        history_started = time.perf_counter()
        history_ok, history_reason, history_meta = self._history_ok(now)
        self.heartbeat("history", ok=history_ok, error=history_reason,
                       metadata=history_meta,
                       latency_ms=round((time.perf_counter() - history_started) * 1000, 3),
                       stale_after_s=120,
                       waiting=bool(history_meta.get("waiting")))
        if not history_ok:
            reasons.append(history_reason or "history unavailable")

        try:
            collector_state = self.history_collector.status() if self.history_collector else {}
            no_browser_source = collector_state.get("data_source") == "ORBIT_DATA_WATCH_API"
            browser_connected = collector_state.get("browser_connected")
            browser_ok = no_browser_source or browser_connected is True
            self.heartbeat("browser", ok=browser_ok,
                waiting=not browser_ok and browser_connected is None,
                error=None if browser_ok else "collector browser is unavailable",
                metadata={"browser_connected": browser_connected,
                          "not_required": no_browser_source}, stale_after_s=120)
        except Exception as exc:
            self.heartbeat("browser", ok=False, error=str(exc), waiting=True)

        if reasons:
            self.state = "DEGRADED"
            self.reason = "; ".join(dict.fromkeys(self._reason_text(reason) for reason in reasons))
        elif all(c.last_success is not None for c in self._components.values() if c.name in {"database", "history"}):
            self.state, self.reason = "HEALTHY", None
        else:
            self.state, self.reason = "DEGRADED", "component health has not been established"
        snap = self.snapshot()
        self._last_snapshot = snap
        return snap

    def snapshot(self) -> dict[str, Any]:
        now = time.time()
        return {
            "state": self.state, "reason": self.reason,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "uptime_s": round(now - self.started_at, 3),
            "components": {name: c.public(now) for name, c in self._components.items()},
            "can_shadow": self.can_shadow(),
        }

    def can_shadow(self) -> dict[str, Any]:
        required = ["database", "history"]
        blocked = []
        for name in required:
            c = self._components[name]
            if c.last_success is None or c.state != "HEALTHY":
                blocked.append(f"{name}:{c.last_error or c.state.lower()}")
            elif c.last_heartbeat is None or time.time() - c.last_heartbeat > c.stale_after_s:
                blocked.append(f"{name}:stale")
        allowed = not blocked and self.state == "HEALTHY"
        return {"allowed": allowed, "state": self.state, "reasons": blocked,
                "checked_at": datetime.now(timezone.utc).isoformat()}

    async def refresh_and_emit(self) -> dict[str, Any]:
        async with self._lock:
            result = await asyncio.to_thread(self.refresh)
            await self.emit({"type": "health:updated", "health": result})
            return result
