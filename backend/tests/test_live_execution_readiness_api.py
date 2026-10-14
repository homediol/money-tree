from __future__ import annotations

import asyncio
from types import SimpleNamespace

from fastapi import FastAPI
from starlette.requests import Request

from app.api.live import readiness_read_only
from app.database.repository import Repository
from app.live import LiveActivationManager


def _request(app: FastAPI) -> Request:
    return Request({
        "type": "http", "method": "GET", "path": "/api/live/readiness",
        "headers": [], "query_string": b"", "server": ("test", 80),
        "client": ("test", 1), "scheme": "http", "app": app,
    })


def test_read_only_live_readiness_passes_fresh_balance_to_gate():
    observation = {
        "verified": False, "balance": None, "ui_ready": False,
        "browser_status": "UNAVAILABLE", "error": "login page is open",
    }

    class Modes:
        async def _read_platform_balance(self):
            return observation

    class Live:
        async def production_readiness_check(self, supplied):
            return {"ready": False, "state": "BLOCKED", "observation": supplied}

    app = FastAPI()
    app.state.betting_mode = Modes()
    app.state.live = Live()
    result = asyncio.run(readiness_read_only(_request(app)))

    assert result["ready"] is False
    assert result["observation"] == observation


def test_production_readiness_names_unwired_target_and_decision_contracts(tmp_path):
    repository = Repository(tmp_path / "live-readiness.sqlite3")
    repository.init()
    observation = {
        "verified": True, "balance": 1200.0, "ui_ready": True,
        "browser_status": "CONNECTED", "observed_at": "2026-10-14T00:00:00+00:00",
        "authentication_state": "AUTHENTICATED",
        "cashout_configuration": {
            "configuration_valid": True, "verified": False,
            "failures": ["PLATFORM_CASHOUT_ACKNOWLEDGEMENT_UNVERIFIED"],
        },
    }

    class Health:
        def record_browser_observation(self, _observation):
            pass

        def browser_observation(self):
            return observation

        def refresh(self):
            return {"state": "HEALTHY", "can_bet": {"reasons": []}}

    class Rules:
        def status(self):
            return {"rules_enabled": 1}

        def execution_authorized(self):
            return False

    history = SimpleNamespace(status=lambda: {
        "running": True, "status": "HEALTHY", "latest": {"round_id": "source-1"},
    })
    state = SimpleNamespace(
        wp=SimpleNamespace(repository=repository),
        system_health=Health(),
        betting=SimpleNamespace(
            settings=SimpleNamespace(allow_real_placement=True),
            status=lambda: {"automatic_enabled": False, "browser_status": "NOT_CONNECTED"},
        ),
        history_collector=history,
        risk=SimpleNamespace(emergency_latched=False),
        reconciliation=object(),
        orchestrator=object(),
        rules_engine=Rules(),
    )

    result = asyncio.run(LiveActivationManager(SimpleNamespace(state=state))
                         .production_readiness_check(observation))

    assert result["ready"] is False
    assert result["checks"]["decision_interface"] == "UNAVAILABLE"
    assert result["checks"]["target_identity"] == "UNVERIFIED"
    assert result["checks"]["platform_authentication"] == "VERIFIED"
    assert result["checks"]["auto_cashout"] == "CONFIGURED_UNACKNOWLEDGED"
    assert result["checks"]["platform_execution"] == "UNVERIFIED"
    assert result["ready"] is False
    assert "rule selections are not connected to target-bound, risk-approved execution intents" in result["reasons"]
    assert "the next target has no verified provider round identity before execution" in result["reasons"]
