from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
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
    assert snapshot["components"]["history"]["state"] == "WAITING"
    assert "1000 seconds old" in snapshot["components"]["history"]["last_error"]
    assert monitor.can_bet_now(mode="REAL")["allowed"] is False


def test_stopped_history_reports_process_state_and_stays_blocked(tmp_path):
    monitor = health(tmp_path, FakeHistory(status="STOPPED", running=False))
    snapshot = monitor.refresh()
    history = snapshot["components"]["history"]
    assert history["state"] == "ERROR"
    assert history["last_error"] == "history collector process is stopped"
    assert monitor.can_bet_now(mode="REAL")["allowed"] is False


def test_safety_enforcement_requests_stop_and_emits_alert(tmp_path, monkeypatch):
    async def direct(function, *args, **kwargs):
        return function(*args, **kwargs)
    monkeypatch.setattr(asyncio, "to_thread", direct)
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


def test_idle_execution_browser_waits_without_counting_failure_or_opening_gate(tmp_path):
    monitor = health(tmp_path)

    class IdleBetting:
        session = None

        def status(self):
            return {"mode": "OFF", "browser_status": "NOT_CONNECTED",
                    "automatic_enabled": False, "current_balance": None}

    monitor.betting_manager = IdleBetting()
    browser = monitor.refresh()["components"]["browser"]
    assert browser["state"] == "WAITING"
    assert browser["error_count"] == 0
    assert "idle" in browser["last_error"]
    assert monitor.can_bet_now(mode="REAL")["allowed"] is False


def test_active_real_session_with_unverified_browser_remains_error(tmp_path):
    monitor = health(tmp_path)
    monitor.betting_manager.status = lambda: {
        "mode": "REAL", "browser_status": "WAITING_FOR_BROWSER",
        "automatic_enabled": True, "current_balance": None,
    }
    browser = monitor.refresh()["components"]["browser"]
    assert browser["state"] == "ERROR"
    assert browser["error_count"] == 1
    assert monitor.can_bet_now(mode="REAL")["allowed"] is False


def idle_browser_health(tmp_path):
    monitor = health(tmp_path)
    monitor.betting_manager.session = None
    monitor.betting_manager.status = lambda: {
        "mode": "OFF", "browser_status": "NOT_CONNECTED", "automatic_enabled": False,
        "current_balance": None,
    }
    return monitor


def ui_balance_observation():
    return {"verified": True, "balance": 1006.0, "balance_text": "1,006BIF",
            "ui_ready": True, "browser_status": "CONNECTED",
            "observed_at": datetime.now(timezone.utc).isoformat(),
            "source": "existing_cdp_page_read_only"}


def test_read_only_balance_survives_idle_health_refresh(tmp_path):
    monitor = idle_browser_health(tmp_path)
    monitor.record_browser_observation(ui_balance_observation())
    for _ in range(3):
        browser = monitor.refresh()["components"]["browser"]
        assert browser["state"] == "HEALTHY"
        assert browser["metadata"]["balance_verified"] is True
        assert browser["metadata"]["balance"] == 1006
        assert browser["last_error"] is None
    assert monitor.betting_manager.status()["current_balance"] is None


def test_read_only_balance_expires_even_without_health_refresh(tmp_path, monkeypatch):
    monitor = idle_browser_health(tmp_path)
    monitor.record_browser_observation(ui_balance_observation())
    monitor.refresh()
    observed = time.time()
    monkeypatch.setattr(time, "time", lambda: observed + 21)
    assert monitor.can_bet_now(mode="REAL")["allowed"] is False
    browser = monitor.refresh()["components"]["browser"]
    assert browser["state"] == "WAITING"
    assert browser["metadata"]["balance"] is None
    assert browser["metadata"]["balance_verified"] is False
    assert "stale" in browser["last_error"]


def test_failed_balance_read_clears_previous_verification(tmp_path):
    monitor = idle_browser_health(tmp_path)
    monitor.record_browser_observation(ui_balance_observation())
    monitor.refresh()
    monitor.record_browser_observation({**ui_balance_observation(), "verified": False,
                                        "balance": None, "error": "CDP disconnected"})
    assert monitor.can_bet_now(mode="REAL")["allowed"] is False
    browser = monitor.refresh()["components"]["browser"]
    assert browser["metadata"]["balance_verified"] is False
    assert browser["last_error"] == "CDP disconnected"


def test_read_only_balance_does_not_mask_failed_active_real_executor(tmp_path):
    monitor = health(tmp_path)
    monitor.record_browser_observation(ui_balance_observation())
    monitor.betting_manager.status = lambda: {
        "mode": "REAL", "browser_status": "NOT_CONNECTED", "automatic_enabled": True,
        "current_balance": None,
    }
    assert monitor.refresh()["components"]["browser"]["state"] == "ERROR"
    assert monitor.can_bet_now(mode="REAL")["allowed"] is False
