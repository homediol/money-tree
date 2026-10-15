from __future__ import annotations

import asyncio
from types import SimpleNamespace
from datetime import datetime, timezone

from fastapi import FastAPI
from starlette.requests import Request
import pytest

from app.api.live import balance, readiness_read_only
from app.database.repository import Repository
from app.live import LiveActivationManager


def _request(app: FastAPI) -> Request:
    return Request({
        "type": "http", "method": "GET", "path": "/api/live/readiness",
        "headers": [], "query_string": b"", "server": ("test", 80),
        "client": ("test", 1), "scheme": "http", "app": app,
    })


def test_read_only_live_readiness_passes_only_operator_selected_panel_to_gate():
    observation = {
        "verified": False, "balance": None, "ui_ready": False,
        "browser_status": "UNAVAILABLE", "error": "login page is open",
    }

    class Modes:
        async def _read_platform_balance(self):
            return observation

        def live_configuration(self):
            return {"configuration_valid": True,
                    "execution_panel": 0,
                    "persistence_source": "REVIEWED"}

    class Live:
        async def production_readiness_check(self, supplied, configuration):
            return {"ready": False, "state": "BLOCKED", "observation": supplied,
                    "configuration": configuration}

    app = FastAPI()
    app.state.betting_mode = Modes()
    app.state.live = Live()
    result = asyncio.run(readiness_read_only(_request(app)))

    assert result["ready"] is False
    assert result["observation"] == observation
    assert result["configuration"] == {
        "execution_panel": 0,
        "configuration_source": "REVIEWED",
    }


def test_readiness_does_not_apply_optional_balance_exposure_policy(tmp_path):
    repository = Repository(tmp_path / "live-exposure-readiness.sqlite3")
    repository.init()
    observation = {
        "verified": True, "balance": 414.0, "currency": "BIF",
        "currency_verified": True, "fresh": True, "ui_ready": True,
        "browser_status": "CONNECTED", "observed_at": datetime.now(timezone.utc).isoformat(),
        "authentication_state": "AUTHENTICATED", "provider_identity": "winner.rw",
        "platform_session_identity": "platform-session",
        "cashout_panels": [], "betting_controls": {},
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

        def decision_interface_status(self):
            return {"available": True, "missing_capabilities": []}

    state = SimpleNamespace(
        wp=SimpleNamespace(repository=repository), system_health=Health(),
        betting=SimpleNamespace(settings=SimpleNamespace(allow_real_placement=True),
                                status=lambda: {"automatic_enabled": False}),
        risk=SimpleNamespace(emergency_latched=False, profiles=lambda: {
            "PROFILE_A": {"maximum_balance_percentage": 0.05}}),
        history_collector=SimpleNamespace(status=lambda: {
            "running": True, "status": "HEALTHY", "latest": {"round_id": "round-1"}}),
        reconciliation=object(), orchestrator=object(), rules_engine=Rules(),
    )
    result = asyncio.run(LiveActivationManager(SimpleNamespace(state=state))
        .production_readiness_check(observation, {
            "profile": "PROFILE_A", "execution_panel": 0,
            "stake_amount": 100, "currency": "BIF",
            "maximum_balance_percentage": 0.05,
        }))

    assert result["optional_bankroll_policy"] == "NOT_APPLIED"
    assert "optional_bankroll_policy" not in result["checks"]
    assert result["checks"]["real_money_execution"] == "DISABLED_FOR_REFACTORING"
    assert result["checks"]["execution_integrity"] == "BLOCKED"
    assert result["checks"]["emergency_stop"] == "CLEAR"
    assert result["checks"]["selected_panel"] == "VERIFIED"
    assert result["checks"]["betting_controls"] == "UNAVAILABLE"
    assert "selected execution panel is not currently verified" not in result["reasons"]
    assert result["provider_capabilities"] == {
        "bet_submission": False, "target_identity": False,
        "betting_deadline": False, "cashout_acknowledgement": False,
        "acceptance_receipt": False, "settlement_receipt": False,
        "currency_validation": False,
    }
    assert "maximum_balance_exposure" not in result
    assert not any("EXPOSURE" in reason or "BALANCE_PERCENTAGE" in reason
                   for reason in result["reasons"])


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

        def decision_interface_status(self):
            return {"available": True, "state": "AVAILABLE", "missing_capabilities": []}

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
    assert result["checks"]["decision_interface"] == "AVAILABLE"
    assert result["checks"]["live_execution_authorization"] == "BLOCKED_UNTIL_ACTIVATED"
    assert result["checks"]["target_identity"] == "UNVERIFIED"
    assert result["checks"]["platform_authentication"] == "VERIFIED"
    assert result["checks"]["auto_cashout"] == "CONFIGURED_UNACKNOWLEDGED"
    assert result["checks"]["platform_execution"] == "UNVERIFIED"
    assert result["ready"] is False
    assert "the next target has no verified provider round identity before execution" in result["reasons"]

    reviewed = asyncio.run(LiveActivationManager(SimpleNamespace(state=state))
        .production_readiness_check(observation, {
            "execution_panel": 0, "stake_amount": 100, "currency": "BIF",
            "real_funds_can_be_lost": True,
            "_authorization_context": {
                "operator_identity": "api-key:test-operator", "review_id": "one-use-review",
                "confirmation": True, "real_funds_can_be_lost": True,
            },
        }))
    assert reviewed["checks"]["operator_authorization"] == "CONFIRMED_FOR_ACTIVATION"
    assert reviewed["checks"]["live_execution_authorization"] == "BLOCKED_UNTIL_ACTIVATED"
    assert "EXPLICIT_LIVE_SESSION_AUTHORIZATION_REQUIRED" not in reviewed["reasons"]
    assert reviewed["ready"] is False  # provider evidence remains independently required

    before_confirmation = asyncio.run(LiveActivationManager(SimpleNamespace(state=state))
        .production_readiness_check(observation, {"_authorization_review": True}))
    assert before_confirmation["checks"]["operator_authorization"] == "PENDING_EXPLICIT_CONFIRMATION"
    assert "EXPLICIT_LIVE_SESSION_AUTHORIZATION_REQUIRED" not in before_confirmation["reasons"]


def test_balance_endpoint_returns_unverified_transport_failure_as_503_without_fake_value():
    class Modes:
        async def _read_platform_balance(self):
            return {"verified": False, "balance": None, "currency": None,
                    "error": "browser_snapshot_timeout"}

    app = FastAPI()
    app.state.betting_mode = Modes()
    response = asyncio.run(balance(_request(app)))

    assert response.status_code == 503
    assert response.body
    assert b"browser_snapshot_timeout" in response.body
    assert b"balance\":null" in response.body


@pytest.mark.parametrize("shown_stake", [100, 200])
def test_live_review_reports_manual_selected_panel_stake_without_app_stake_configuration(
        tmp_path, shown_stake):
    repository = Repository(tmp_path / f"stake-readback-{shown_stake}.sqlite3")
    repository.init()
    now = datetime.now(timezone.utc).isoformat()
    panel_rows = [
        {"slot": slot, "panel_id": f"PANEL_{slot + 1}", "state": "BET_AVAILABLE",
         "pending_bet_state": "CLEAR", "evidence": {"button_visible": True},
         "stake_input_visible": True, "displayed_stake": shown_stake,
         "currency": "BIF", "button_text": f"Bet {shown_stake} BIF"}
        for slot in (0, 1)
    ]
    observation = {
        "verified": True, "balance": 5000, "currency": "BIF",
        "currency_verified": True, "fresh": True, "ui_ready": True,
        "browser_status": "CONNECTED", "observed_at": now,
        "authentication_state": "AUTHENTICATED", "provider_identity": "winner.rw",
        "platform_session_identity": "platform-session",
        "cashout_configuration": {"configuration_valid": True, "verified": False,
                                   "failures": ["PLATFORM_CASHOUT_ACKNOWLEDGEMENT_UNVERIFIED"]},
        "cashout_panels": [{"configuration_valid": True, "auto_bet_enabled": False,
                            "auto_tab_selected": True, "platform_acknowledged": None}
                           for _ in (0, 1)],
        "betting_controls": {
            "snapshot_ok": True, "panels": panel_rows,
            "stake_inputs": [{"slot": slot, "visible": True, "value": str(shown_stake)}
                             for slot in (0, 1)],
            "bet_buttons": [{"slot": slot, "visible": True, "disabled": False,
                             "text": f"Bet {shown_stake} BIF"} for slot in (0, 1)],
            "betting_window": {"state": "OPEN"}, "round_phase": "BETTING_OPEN",
            "deadline_evidence": {"status": "VERIFIED"},
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
            return {"rules_enabled": 1, "active_session": {
                "status": "RUNNING", "selection_status": "ACTIVE",
                "live_execution_stop": {"status": "UNCONFIGURED"}}}

        def decision_interface_status(self):
            return {"available": True, "missing_capabilities": []}

    history = SimpleNamespace(status=lambda: {
        "running": True, "status": "HEALTHY", "latest": {"round_id": "source-1"},
    })
    state = SimpleNamespace(
        wp=SimpleNamespace(repository=repository), system_health=Health(),
        betting=SimpleNamespace(settings=SimpleNamespace(allow_real_placement=True),
                                status=lambda: {"automatic_enabled": False}),
        history_collector=history, risk=SimpleNamespace(emergency_latched=False),
        reconciliation=object(), orchestrator=object(), rules_engine=Rules(),
    )
    result = asyncio.run(LiveActivationManager(SimpleNamespace(state=state))
        .production_readiness_check(observation, {
            "execution_panel": 0, "stake_amount": 100, "currency": "BIF",
        }))

    assert result["checks"]["displayed_stake"] == "MATCHED_SELECTED_PANEL_READBACK"
    assert result["checks"]["betting_controls"] == "READY"
    assert result["checks"]["stake"] == "MATCHED_SELECTED_PANEL_READBACK"
    assert "selected panel displayed stake does not match the configured BIF stake" not in result["reasons"]
