from types import SimpleNamespace
import asyncio
import sqlite3

import pytest

from app.live import LiveActivationManager


def test_live_activation_is_observing_and_requires_exact_confirmation():
    controller = LiveActivationManager()
    assert controller.mode == "OBSERVING"
    assert controller.live_active is False


def test_live_start_rejects_missing_confirmation_without_side_effect():
    controller = LiveActivationManager(SimpleNamespace(state=SimpleNamespace()))
    result = asyncio.run(controller.start({"confirmation": "enable live betting"}))
    assert result["ok"] is False
    assert result["error"] == "explicit_confirmation_required"
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
