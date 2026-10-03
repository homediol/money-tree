from __future__ import annotations

import asyncio
from types import SimpleNamespace
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest
from fastapi import FastAPI, HTTPException
from starlette.requests import Request

from app.database.repository import Repository
from app.api import betting_mode as betting_mode_api
from app.api.live import start as live_start
from app.live import LiveActivationManager
from app.services.betting_mode import (
    LIVE_MODE_AUTHORIZATION,
    LIVE_REAL,
    SHADOW_REALISTIC,
    BettingModeManager,
)


@pytest.mark.parametrize("balance,verified", [(1006.0, True), (0.0, True),
    (None, False), ("1006", False), (True, False), (-1.0, False),
    (float("nan"), False), (float("inf"), False)])
def test_platform_balance_requires_finite_numeric_ui_evidence(tmp_path, monkeypatch, balance, verified):
    repository = Repository(tmp_path / "modes.sqlite3")
    repository.init()
    observations = []
    class Browser:
        def __init__(self, _settings):
            pass
        async def snapshot(self):
            return {"ok": True, "balance": balance, "ui_ready": True}
        async def close(self):
            pass
    monkeypatch.setattr("app.betting.browser_client.AviatorBrowserClient", Browser)
    app = SimpleNamespace(state=SimpleNamespace(
        wp=SimpleNamespace(repository=repository),
        betting=SimpleNamespace(settings=object()),
        system_health=SimpleNamespace(record_browser_observation=observations.append),
    ))
    modes = BettingModeManager(app)
    result = asyncio.run(modes._read_platform_balance())
    assert result["verified"] is verified
    assert result["balance"] == balance if verified else result["balance"] is None
    assert observations == [result]


def test_restart_never_restores_live_authorization(tmp_path):
    repository = Repository(tmp_path / "modes.sqlite3")
    repository.init()
    repository.save_betting_mode(LIVE_REAL, {
        "mode": LIVE_REAL, "updated_at": "persisted", "live_authorized": True,
    })
    app = SimpleNamespace(state=SimpleNamespace(wp=SimpleNamespace(repository=repository)))

    modes = BettingModeManager(app)

    assert modes.mode == SHADOW_REALISTIC
    assert modes.live_authorized is False
    assert modes.recovered_live is True
    assert modes.can_execute_live() is False
    assert repository.load_betting_mode()["mode"] == SHADOW_REALISTIC


def test_live_manager_rejects_direct_or_forged_start():
    controller = LiveActivationManager(SimpleNamespace(state=SimpleNamespace()))

    direct = asyncio.run(controller.start({"confirmation": "ENABLE LIVE BETTING"}))
    forged = asyncio.run(controller.start({
        "confirmation": "ENABLE LIVE BETTING",
        "_mode_authorization": "LIVE_MODE_AUTHORIZATION",
    }))

    assert direct["error"] == "backend_mode_authorization_required"
    assert forged["error"] == "backend_mode_authorization_required"
    assert controller.live_active is False
    assert controller.explicit_live_authorization is False


def test_live_manager_requires_confirmation_even_with_backend_capability():
    controller = LiveActivationManager(SimpleNamespace(state=SimpleNamespace()))

    result = asyncio.run(controller.start({}, _mode_authorization=LIVE_MODE_AUTHORIZATION))

    assert result["error"] == "explicit_confirmation_required"
    assert controller.live_active is False


def test_forged_and_reused_review_cannot_switch_to_live(tmp_path):
    repository = Repository(tmp_path / "modes.sqlite3")
    repository.init()
    app = SimpleNamespace(state=SimpleNamespace(wp=SimpleNamespace(repository=repository)))
    modes = BettingModeManager(app)

    result = asyncio.run(modes.switch_live({
        "confirmation": "ENABLE LIVE BETTING", "review_id": "forged",
    }))

    assert result["ok"] is False
    assert result["error"] == "fresh_backend_live_review_required"
    assert modes.mode == SHADOW_REALISTIC
    assert modes.live_authorized is False


def test_live_api_requires_server_credential_and_rejects_legacy_start(monkeypatch):
    class Modes:
        mode = SHADOW_REALISTIC

        def __init__(self):
            self.switch_calls = 0

        async def switch_live(self, _body):
            self.switch_calls += 1
            return {"ok": False, "error": "fresh_backend_live_review_required"}

    modes = Modes()
    app = FastAPI()
    app.state.betting_mode = modes
    monkeypatch.setattr(betting_mode_api, "get_settings",
                        lambda: SimpleNamespace(api_key="server-secret"))
    request = Request({
        "type": "http", "method": "POST", "path": "/api/betting-mode",
        "headers": [], "query_string": b"", "server": ("test", 80),
        "client": ("test", 1), "scheme": "http", "app": app,
    })

    async def scenario():
        try:
            await betting_mode_api.switch_mode(request, {
                "mode": LIVE_REAL, "confirmation": "ENABLE LIVE BETTING",
                "review_id": "forged-by-frontend",
            })
        except HTTPException as exc:
            unauthorized_status = exc.status_code
        else:
            unauthorized_status = 200
        legacy = await live_start(request, {"confirmation": "ENABLE LIVE BETTING"})
        return unauthorized_status, legacy

    unauthorized_status, legacy = asyncio.run(scenario())

    assert unauthorized_status == 401
    assert modes.switch_calls == 0
    assert legacy["error"] == "backend_mode_switch_required"


def test_modes_are_mutually_exclusive_and_emergency_safe(tmp_path):
    repository = Repository(tmp_path / "modes.sqlite3")
    repository.init()
    live = LiveActivationManager()
    state = SimpleNamespace(
        wp=SimpleNamespace(repository=repository),
        live=live,
        betting=SimpleNamespace(session=None),
    )
    modes = BettingModeManager(SimpleNamespace(state=state))
    state.betting_mode = modes

    blocked_new_config = asyncio.run(modes.switch_live({"configuration": {"mode": "MANUAL"}}))
    assert blocked_new_config["error"] == "two_panel_live_configuration_not_qualified"

    modes.mode = LIVE_REAL
    modes.live_authorized = True
    live.explicit_live_authorization = True
    live.live_active = True
    assert modes.can_execute_live() is True

    result = asyncio.run(modes.force_safe("emergency_stop"))

    assert result["mode"] == SHADOW_REALISTIC
    assert modes.live_authorized is False
    assert live.explicit_live_authorization is False
    assert live.live_active is False
    assert modes.can_execute_live() is False


def _live_gate_fixture():
    repository = SimpleNamespace(
        execution_for=lambda *_args: None,
        open_executions=lambda: [],
    )
    risk = SimpleNamespace(emergency_latched=False)
    state = SimpleNamespace(
        wp=SimpleNamespace(
            repository=repository,
            dataset_service=SimpleNamespace(clean_rounds=pd.DataFrame([{"round_index": 100}])),
        ),
        risk=risk,
        betting=SimpleNamespace(
            settings=SimpleNamespace(allow_real_placement=True),
            status=lambda: {},
        ),
        history_collector=SimpleNamespace(status=lambda: {"latest": {"round_id": "100"}}),
        readiness=SimpleNamespace(execution_gate=lambda **_kwargs: {"allowed": True, "reasons": []}),
        betting_mode=SimpleNamespace(can_execute_live=lambda: True),
    )
    controller = LiveActivationManager(SimpleNamespace(state=state))
    controller.mode = "LIVE_ACTIVE"
    controller.live_active = True
    controller.explicit_live_authorization = True
    now = datetime.now(timezone.utc)
    status = {
        "mode": "REAL", "automatic_enabled": True, "current_balance": 5000,
        "last_balance_observed_at": now.isoformat(),
    }
    decision = {
        "decision_id": "live-d1", "target_round_id": "101", "source_round_id": "100",
        "profile": "PROFILE_A", "status": "READY_FOR_EXECUTION",
        "expires_at": (now + timedelta(seconds=30)).isoformat(),
        "risk_evaluation": {"status": "APPROVED", "approved": True,
                             "approved_bet": 100, "approved_bet_amount": 100},
    }
    return controller, state, status, decision


def test_live_gate_blocks_expired_and_wrong_round_decisions():
    controller, _state, status, decision = _live_gate_fixture()

    expired = controller.can_execute_live_bet({
        **decision, "expires_at": (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),
    }, status)
    wrong_round = controller.can_execute_live_bet({**decision, "target_round_id": "102"}, status)

    assert expired["allowed"] is False
    assert "decision expired or expiry missing" in expired["reasons"]
    assert wrong_round["allowed"] is False
    assert "target round does not match the next live round" in wrong_round["reasons"]


def test_live_gate_blocks_duplicate_and_emergency_stop():
    controller, state, status, decision = _live_gate_fixture()
    state.wp.repository.execution_for = lambda *_args: {"execution_id": "already-placed"}
    duplicate = controller.can_execute_live_bet(decision, status)
    assert duplicate["allowed"] is False
    assert "DUPLICATE_BLOCKED" in duplicate["reasons"]

    state.wp.repository.execution_for = lambda *_args: None
    state.risk.emergency_latched = True
    emergency = controller.can_execute_live_bet(decision, status)
    assert emergency["allowed"] is False
    assert "emergency stop is active" in emergency["reasons"]
