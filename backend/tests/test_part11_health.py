from __future__ import annotations

import asyncio
import time
from pathlib import Path

from app.database.repository import Repository
from app.services.system_health import SystemHealth


class FakeHistory:
    def __init__(self, status="HEALTHY", running=True, age=1):
        self._status, self._running, self._age = status, running, age

    def status(self):
        from datetime import datetime, timezone
        stamp = datetime.fromtimestamp(time.time() - self._age, timezone.utc).isoformat()
        return {"status": self._status, "running": self._running,
                "latest": {"timestamp": stamp}, "count": 10}


class FakeSession:
    mode = type("Mode", (), {"value": "REAL"})()
    state = type("State", (), {"is_active": True})()
    def request_stop(self, reason):
        self.reason = reason


class FakeBetting:
    def __init__(self):
        self.session = FakeSession()
    def status(self):
        return {"mode": "REAL", "browser_status": "CONNECTED",
                "last_ui_ready": True, "current_balance": 1000,
                "automatic_enabled": True, "state": "CONNECTED"}


def health(tmp_path: Path, history=None):
    repo = Repository(tmp_path / "health.sqlite3")
    repo.init()
    monitor = SystemHealth(betting_manager=FakeBetting(),
                           history_collector=history or FakeHistory(), repository=repo)
    for component in ("features", "patterns", "ml", "evidence", "decision", "risk", "api"):
        monitor.heartbeat(component, ok=True, stale_after_s=3600)
    return monitor


def test_can_bet_requires_real_verified_component_heartbeats(tmp_path):
    monitor = health(tmp_path)
    assert monitor.refresh()["state"] == "HEALTHY"
    assert monitor.can_bet_now(mode="REAL")["allowed"] is True
    monitor.heartbeat("browser", ok=False, error="CDP disconnected")
    gate = monitor.can_bet_now(mode="REAL")
    assert gate["allowed"] is False
    assert any("browser" in reason for reason in gate["reasons"])


def test_stale_history_pauses_without_fabricating_health(tmp_path):
    monitor = health(tmp_path, FakeHistory(age=1000))
    snapshot = monitor.refresh()
    assert snapshot["state"] == "PAUSED"
    assert snapshot["components"]["history"]["state"] == "ERROR"
    assert monitor.can_bet_now(mode="REAL")["allowed"] is False


def test_safety_enforcement_requests_stop_and_emits_alert(tmp_path):
    events = []

    async def broadcast(payload):
        events.append(payload)

    monitor = health(tmp_path, FakeHistory(status="DISCONNECTED"))
    monitor.broadcaster = broadcast
    result = asyncio.run(monitor.enforce_safety())
    assert result["state"] == "PAUSED"
    assert monitor.betting_manager.session.reason == "health_safety_pause"
    assert any(event["type"] == "health:alert" for event in events)


def test_unknown_component_is_not_reported_healthy(tmp_path):
    monitor = health(tmp_path)
    monitor.heartbeat("ml", ok=False, error="model unavailable")
    snapshot = monitor.refresh()
    assert snapshot["components"]["ml"]["state"] == "ERROR"
    assert snapshot["components"]["ml"]["last_error"] == "model unavailable"
