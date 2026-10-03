from types import SimpleNamespace
import asyncio
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from app.live import LiveActivationManager
from app.services.system_health import SystemHealth


def test_live_activation_is_observing_and_requires_exact_confirmation():
    controller = LiveActivationManager()
    assert controller.mode == "OBSERVING"
    assert controller.live_active is False


def test_live_start_rejects_missing_confirmation_without_side_effect():
    controller = LiveActivationManager(SimpleNamespace(state=SimpleNamespace()))
    result = asyncio.run(controller.start({"confirmation": "enable live betting"}))
    assert result["ok"] is False
    assert result["error"] == "backend_mode_authorization_required"
    assert controller.mode == "OBSERVING"


def test_live_resume_requires_a_fresh_activation():
    controller = LiveActivationManager()
    result = asyncio.run(controller.resume("ENABLE LIVE BETTING"))
    assert result["ok"] is False
    assert result["error"] == "fresh_live_activation_required"
    assert controller.live_active is False


def test_production_readiness_blocks_unverified_platform_execution():
    class Database:
        @staticmethod
        def connect():
            return sqlite3.connect(":memory:")

    decision = SimpleNamespace(current=lambda: {"status": "READY_FOR_EXECUTION"})
    state = SimpleNamespace(
        wp=SimpleNamespace(repository=Database()),
        betting=SimpleNamespace(
            settings=SimpleNamespace(allow_real_placement=True),
            status=lambda: {"browser_status": "CONNECTED", "last_ui_ready": True},
        ),
        reconciliation=object(),
        history_collector=SimpleNamespace(status=lambda: {
            "running": True, "status": "HEALTHY", "latest": {"round_id": "101"},
        }),
        decision_engine=decision,
        risk=SimpleNamespace(emergency_latched=False),
        orchestrator=object(),
    )
    controller = LiveActivationManager(SimpleNamespace(state=state))

    result = asyncio.run(controller.production_readiness_check())

    assert result["ready"] is False
    assert result["checks"]["platform_execution"] == "UNVERIFIED"
    assert "platform acceptance, round and cashout controls are unverified" in result["reasons"]


def test_idle_readiness_uses_fresh_background_ui_balance(tmp_path):
    from app.database.repository import Repository
    repository = Repository(tmp_path / "health.sqlite3")
    repository.init()
    betting = SimpleNamespace(
        session=None, settings=SimpleNamespace(allow_real_placement=False),
        status=lambda: {"mode": "OFF", "browser_status": "NOT_CONNECTED",
                        "current_balance": None, "automatic_enabled": False},
    )
    history = SimpleNamespace(status=lambda: {
        "running": True, "status": "HEALTHY", "latest": {
            "round_id": "101", "timestamp": datetime.now(timezone.utc).isoformat()},
    })
    health = SystemHealth(betting_manager=betting, history_collector=history, repository=repository)
    health.record_browser_observation({
        "verified": True, "balance": 1006.0, "ui_ready": True,
        "browser_status": "CONNECTED", "observed_at": datetime.now(timezone.utc).isoformat(),
        "source": "existing_cdp_page_read_only",
    })
    state = SimpleNamespace(
        wp=SimpleNamespace(repository=repository), betting=betting,
        history_collector=history, system_health=health,
        reconciliation=object(), decision_engine=SimpleNamespace(current=lambda: None),
        risk=SimpleNamespace(emergency_latched=False), orchestrator=object(),
    )
    result = asyncio.run(LiveActivationManager(SimpleNamespace(state=state)).production_readiness_check())
    assert result["checks"]["betting_page"] == "HEALTHY"
    assert result["ready"] is False
    assert result["checks"]["platform_execution"] == "UNVERIFIED"
    assert health.refresh()["components"]["browser"]["metadata"]["balance_verified"] is True


def test_live_execution_uses_central_readiness_gate_and_fails_closed():
    repository = SimpleNamespace(execution_for=lambda *_args: None)
    state = SimpleNamespace(
        wp=SimpleNamespace(repository=repository),
        risk=SimpleNamespace(emergency_latched=False),
        betting=SimpleNamespace(status=lambda: {
            "mode": "REAL", "automatic_enabled": True,
        }),
        history_collector=SimpleNamespace(status=lambda: {
            "latest": {"round_id": "100"},
        }),
        readiness=SimpleNamespace(execution_gate=lambda **_kwargs: {
            "allowed": False,
            "reasons": ["history_warming_up:20/100", "model_not_deployable:NOT_VALIDATED"],
        }),
    )
    controller = LiveActivationManager(SimpleNamespace(state=state))
    controller.mode = "LIVE_ACTIVE"
    controller.live_active = True
    decision = {
        "decision_id": "d1", "target_round_id": "101", "source_round_id": "100",
        "status": "READY_FOR_EXECUTION",
        "expires_at": (datetime.now(timezone.utc) + timedelta(seconds=30)).isoformat(),
    }

    gate = controller.can_execute_live_bet(decision, state.betting.status())

    assert gate["allowed"] is False
    assert "history_warming_up:20/100" in gate["reasons"]
    assert "model_not_deployable:NOT_VALIDATED" in gate["reasons"]
