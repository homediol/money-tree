"""Central health model and final betting safety gate.

Health is evidence based: a component is never reported healthy until it has
produced a successful heartbeat.  The monitor is deliberately independent of
the history collector so stopping betting cannot stop observation.
"""
from __future__ import annotations

import asyncio
import json
import math
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
        "browser", "history", "database", "features", "patterns", "ml",
        "evidence", "decision", "risk", "executor", "reconciliation", "api",
    )

    def __init__(self, *, broadcaster: Callable[[dict], Awaitable[None]] | None = None,
                 betting_manager=None, history_collector=None, repository=None):
        self.broadcaster = broadcaster
        self.betting_manager = betting_manager
        self.history_collector = history_collector
        self.repository = repository
        self.started_at = time.time()
        self.state = "STARTING"
        self.reason = "health checks have not completed"
        self._components = {name: ComponentHealth(name=name) for name in self.COMPONENTS}
        self._last_snapshot: dict[str, Any] | None = None
        self._last_alert_key: str | None = None
        self._lock = asyncio.Lock()
        self._browser_observation: dict[str, Any] | None = None

    def record_browser_observation(self, observation: dict[str, Any]) -> None:
        """Retain current read-only UI evidence, independently of a bet session."""
        self._browser_observation = dict(observation)

    def browser_observation(self) -> dict[str, Any]:
        observation = dict(self._browser_observation or {})
        balance = observation.get("balance")
        try:
            age = time.time() - datetime.fromisoformat(observation["observed_at"]).timestamp()
        except (KeyError, TypeError, ValueError):
            age = None
        valid_balance = (isinstance(balance, (int, float)) and not isinstance(balance, bool)
                         and math.isfinite(balance) and balance >= 0)
        fresh = age is not None and 0 <= age <= 20
        observation.update(age_s=age, fresh=fresh,
                           verified=bool(observation.get("verified") and valid_balance and fresh))
        if not observation["verified"]:
            observation["balance"] = None
            observation["error"] = observation.get("error") or (
                "browser balance observation stale" if age is not None and not fresh
                else "execution browser idle; no verified UI balance")
        return observation

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
            c.last_error = error or "health check failed"

    def _history_ok(self, now: float) -> tuple[bool, str | None, dict]:
        if self.history_collector is None:
            return False, "history collector unavailable", {}
        try:
            # Execution safety still verifies the canonical persisted history,
            # not only the collector's lightweight sidecar health signal.
            status = self.history_collector.status()
            latest = status.get("latest") or {}
            stamp = latest.get("timestamp")
            age = None
            if stamp:
                age = max(0.0, now - datetime.fromisoformat(str(stamp).replace("Z", "+00:00")).timestamp())
            running = bool(status.get("running"))
            collector_state = str(status.get("status", "UNKNOWN")).upper()
            healthy = collector_state in {"HEALTHY", "COLLECTING"}
            fresh = age is not None and age <= 120
            ok = running and healthy and fresh
            if ok:
                reason = None
            elif not running or collector_state == "STOPPED":
                reason = status.get("last_error") or "history collector process is stopped"
            elif collector_state in {"CONNECTING", "WAITING", "WAITING_FOR_NETWORK", "RECOVERING", "STARTING"}:
                reason = status.get("last_error") or f"history collector is {collector_state.lower().replace('_', ' ')}"
            elif collector_state == "STALE" or (running and healthy and not fresh):
                reason = ("history collector has not saved a new round yet" if age is None
                          else f"latest saved round is {int(round(age))} seconds old; waiting for a fresh round")
            else:
                reason = status.get("last_error") or f"history collector status is {collector_state.lower().replace('_', ' ')}"
            waiting_state = collector_state in {
                "CONNECTING", "WAITING", "WAITING_FOR_NETWORK", "RECOVERING", "STARTING", "STALE",
            } or (running and healthy and not fresh)
            metadata = {"status": status.get("status"), "age_s": age,
                        "count": status.get("count", 0)}
            if not ok and waiting_state:
                metadata["waiting"] = True
            return ok, reason, metadata
        except Exception as exc:
            return False, f"history check failed: {exc}", {}

    def refresh(self) -> dict[str, Any]:
        now = time.time()
        reasons: list[str] = []
        # Database is authoritative for execution safety.
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

        if self.betting_manager is not None:
            try:
                bs = self.betting_manager.status()
                mode = bs.get("mode")
                browser_ok = mode == "SIMULATION" or (
                    bs.get("browser_status") in {"CONNECTED", "READY"} and bool(bs.get("last_ui_ready"))
                    and bs.get("current_balance") is not None
                )
                observation = self.browser_observation()
                # A preflight UI read may establish browser health while idle.
                # An active REAL executor must still verify its own session.
                use_observation = not bs.get("automatic_enabled") and mode != "SIMULATION"
                if use_observation:
                    browser_ok = bool(observation.get("verified") and observation.get("ui_ready")
                                      and observation.get("browser_status") in {"CONNECTED", "READY"})
                browser_waiting = (not browser_ok and not bs.get("automatic_enabled")
                                   and not bs.get("last_error"))
                browser_reason = None if browser_ok else (
                    (observation.get("error") or "betting panel is not UI-ready") if browser_waiting
                    else bs.get("last_error") or "browser not connected and UI-ready with verified balance"
                )
                browser_metadata = {"mode": mode, "browser_status": bs.get("browser_status"),
                                    "ui_ready": bs.get("last_ui_ready"),
                                    "balance": bs.get("current_balance"),
                                    "balance_verified": browser_ok and mode != "SIMULATION"}
                if use_observation:
                    browser_metadata.update({key: observation.get(key) for key in (
                        "browser_status", "ui_ready", "balance", "balance_text", "observed_at", "source")})
                    browser_metadata["balance_verified"] = observation.get("verified", False)
                self.heartbeat("browser", ok=browser_ok,
                               waiting=browser_waiting, error=browser_reason,
                               metadata=browser_metadata, stale_after_s=20)
                if not browser_ok and mode == "REAL" and bs.get("automatic_enabled"):
                    reasons.append("browser unsafe")
                # An active execution is expected while a round is in flight;
                # unresolved UNKNOWN/RECONCILIATION_PENDING is unsafe.
                open_rows = self.repository.open_executions() if self.repository else []
                recon_ok = not any(x.get("status") in {"UNKNOWN", "RECONCILIATION_PENDING"} for x in open_rows)
                self.heartbeat("reconciliation", ok=recon_ok,
                               error=None if recon_ok else "unresolved execution requires reconciliation",
                               metadata={"open_executions": len(open_rows) if self.repository else None},
                               stale_after_s=30)
                if not recon_ok:
                    reasons.append("reconciliation unresolved")
                self.heartbeat("executor", ok=True, metadata={"state": bs.get("state")}, stale_after_s=30)
                self.heartbeat("risk", ok=not bool(bs.get("emergency_stop")),
                               error="risk emergency stop latched" if bs.get("emergency_stop") else None,
                               stale_after_s=30)
            except Exception as exc:
                self.heartbeat("executor", ok=False, error=str(exc))
                reasons.append("executor status unavailable")

        for name in ("features", "patterns", "ml", "evidence", "decision", "api"):
            c = self._components[name]
            if c.last_success is None and c.state == "UNKNOWN":
                c.state = "UNKNOWN"

        if reasons:
            self.state = "PAUSED" if self.betting_manager and self.betting_manager.session else "DEGRADED"
            self.reason = "; ".join(dict.fromkeys(reasons))
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
            "can_bet": self.can_bet_now(),
        }

    def can_bet_now(self, *, mode: str | None = None) -> dict[str, Any]:
        mode = mode or (self.betting_manager.status().get("mode") if self.betting_manager else "REAL")
        required = ["database", "history", "reconciliation"]
        if mode != "SIMULATION":
            required.append("browser")
        blocked = []
        for name in required:
            c = self._components[name]
            if (name == "browser" and (c.metadata or {}).get("source") == "existing_cdp_page_read_only"
                    and not self.browser_observation().get("verified")):
                blocked.append("browser:" + self.browser_observation().get("error", "balance unverified"))
                continue
            if c.last_success is None or c.state != "HEALTHY":
                blocked.append(f"{name}:{c.last_error or c.state.lower()}")
            elif c.last_heartbeat is None or time.time() - c.last_heartbeat > c.stale_after_s:
                blocked.append(f"{name}:stale")
        allowed = not blocked and self.state == "HEALTHY"
        return {"allowed": allowed, "state": self.state, "reasons": blocked,
                "checked_at": datetime.now(timezone.utc).isoformat()}

    async def enforce_safety(self) -> dict[str, Any]:
        async with self._lock:
            result = await asyncio.to_thread(self.refresh)
            if self.betting_manager and self.betting_manager.session:
                session = self.betting_manager.session
                if session.mode.value == "REAL" and not self.can_bet_now(mode="REAL").get("allowed"):
                    if session.state.is_active:
                        session.request_stop("health_safety_pause")
                        key = f"{self.state}:{self.reason}"
                        if key != self._last_alert_key:
                            self._last_alert_key = key
                            event = {"type": "health:alert", "severity": "CRITICAL",
                                     "state": self.state, "reason": self.reason}
                            if self.repository:
                                try:
                                    with self.repository.connect() as conn:
                                        conn.execute(
                                            "INSERT INTO system_audit_log(event_type,entity_id,recorded_at,payload) VALUES(?,?,datetime('now'),?)",
                                            ("HEALTH_ALERT", None, json.dumps(event)),
                                        )
                                except Exception:
                                    pass
                            await self.emit(event)
            await self.emit({"type": "health:updated", "health": result})
            return result
