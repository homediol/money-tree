from __future__ import annotations

import asyncio
from types import SimpleNamespace
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI, HTTPException
from starlette.requests import Request

from app.database.repository import Repository
from app.api import betting_mode as betting_mode_api
from app.api.live import start as live_start
from app.betting.schemas import DecisionIntent
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
            return {"ok": True, "balance": balance, "balance_currency": "BIF",
                    "balance_observed_at": datetime.now(timezone.utc).isoformat(),
                    "provider_identity": "winner.rw", "session_identity": "platform-session-1",
                    "ui_ready": True, "authentication_state": "AUTHENTICATED"}
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


def test_balance_is_not_verified_when_authenticated_account_evidence_is_missing(tmp_path, monkeypatch):
    repository = Repository(tmp_path / "auth-state.sqlite3")
    repository.init()
    class Browser:
        def __init__(self, _settings):
            pass
        async def snapshot(self):
            return {"ok": True, "balance": 1006.0, "ui_ready": True,
                    "authentication_state": "UNKNOWN"}
        async def close(self):
            pass
    monkeypatch.setattr("app.betting.browser_client.AviatorBrowserClient", Browser)
    app = SimpleNamespace(state=SimpleNamespace(
        wp=SimpleNamespace(repository=repository), betting=SimpleNamespace(settings=object()),
    ))
    result = asyncio.run(BettingModeManager(app)._read_platform_balance())

    assert result["verified"] is False
    assert result["balance"] is None
    assert result["displayed_amount"] == 1006.0
    assert result["authentication_state"] == "UNKNOWN"


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


def test_live_review_persists_currency_and_validates_stop_limit_counts(tmp_path):
    repository = Repository(tmp_path / "live-config.sqlite3")
    repository.init()
    profiles = {
        "PROFILE_A": {"cashout": 2.0, "minimum_bet": 100, "maximum_bet": 10000,
                      "maximum_session_loss": 500, "maximum_balance_percentage": 0.25},
        "PROFILE_B": {"cashout": 2.0, "minimum_bet": 100, "maximum_bet": 50000,
                      "maximum_session_loss": 1000, "maximum_balance_percentage": 0.5},
    }
    app = SimpleNamespace(state=SimpleNamespace(
        wp=SimpleNamespace(repository=repository),
        risk=SimpleNamespace(profiles=lambda: profiles),
        live=SimpleNamespace(production_readiness_check=lambda *_args, **_kwargs: asyncio.sleep(0, result={
            "ready": False, "state": "BLOCKED", "reasons": ["platform adapter incomplete"], "checks": {},
        })),
    ))
    modes = BettingModeManager(app)

    async def verified_balance():
        return {"verified": True, "fresh": True, "currency_verified": True,
                "balance": 528, "currency": "BIF",
                "observed_at": datetime.now(timezone.utc).isoformat(),
                "provider_identity": "winner.rw", "platform_session_identity": "session-1"}

    modes._read_platform_balance = verified_balance
    common = {
        "profile": "PROFILE_A", "win_limit_enabled": True, "win_limit": 250,
        "session_loss_limit_enabled": True, "session_loss_limit": 300,
        "reconciled_win_limit_enabled": True, "reconciled_win_limit_count": 2,
        "reconciled_loss_count_limit_enabled": True, "reconciled_loss_count_limit": 3,
        "execution_panel": 1, "stake_amount": 200, "currency": "BIF",
    }
    invalid = asyncio.run(modes.live_review(**{**common, "reconciled_win_limit_count": 0}))
    assert "enabled_win_count_limit_requires_a_positive_integer" in invalid["blockers"]

    reviewed = asyncio.run(modes.live_review(**common))
    assert reviewed["currency"] == "BIF"
    assert reviewed["execution_panel"] == 1
    assert reviewed["reconciled_win_limit_count"] == 2
    assert reviewed["reconciled_loss_count_limit"] == 3
    saved = repository.load_application_state("live_activation_configuration")
    assert saved["currency"] == "BIF"
    assert saved["stake_amount"] == 200
    assert saved["profit_target"] == 250
    assert saved["loss_limit"] == 300
    assert saved["win_count_limit"] == 2
    assert saved["loss_count_limit"] == 3
    assert saved["execution_panel"] == 1


def test_live_configuration_is_never_null_and_draft_does_not_authorize(tmp_path):
    repository = Repository(tmp_path / "live-draft.sqlite3")
    repository.init()
    profiles = {
        "PROFILE_A": {"cashout": 2.0, "minimum_bet": 100, "maximum_bet": 10000,
                      "maximum_session_loss": 500, "maximum_balance_percentage": 0.25},
        "PROFILE_B": {"cashout": 2.0, "minimum_bet": 100, "maximum_bet": 50000,
                      "maximum_session_loss": 1000, "maximum_balance_percentage": 0.5},
    }
    app = SimpleNamespace(state=SimpleNamespace(
        wp=SimpleNamespace(repository=repository),
        risk=SimpleNamespace(profiles=lambda: profiles),
    ))
    modes = BettingModeManager(app)

    initial = modes.live_configuration()
    assert initial is not None
    assert initial["configuration_status"] == "INCOMPLETE"
    assert initial["persistence_source"] == "DEFAULTS"
    assert initial["win_count_limit"] == 2
    assert initial["loss_count_limit"] == 200

    saved = modes.save_live_configuration_draft({
        "profile": "PROFILE_A", "execution_panel": 1, "stake_amount": 200,
        "currency": "BIF", "profit_target_enabled": True, "profit_target": 400,
        "loss_limit_enabled": True, "loss_limit": 400,
        "win_count_limit_enabled": True, "win_count_limit": 2,
        "loss_count_limit_enabled": True, "loss_count_limit": 200,
        "cashout_target": 2.0,
    })

    assert saved["configuration_status"] == "COMPLETE"
    assert saved["persistence_source"] == "DRAFT"
    assert repository.load_application_state("live_activation_configuration") is None
    assert repository.load_application_state("live_activation_configuration_draft")["execution_panel"] == 1
    assert modes.live_authorized is False
    assert modes.mode == SHADOW_REALISTIC

    with pytest.raises(ValueError, match="LIVE_CONFIGURATION_REQUIRES_BIF_WITHOUT_CONVERSION"):
        modes.save_live_configuration_draft({"currency": "RWF"})


def test_new_live_draft_overrides_reviewed_readback_and_invalidates_review(tmp_path):
    repository = Repository(tmp_path / "live-draft-readback.sqlite3")
    repository.init()
    profiles = {
        "PROFILE_A": {"cashout": 2.0, "minimum_bet": 100, "maximum_bet": 10000,
                      "maximum_session_loss": 500, "maximum_balance_percentage": 0.25},
        "PROFILE_B": {"cashout": 2.0, "minimum_bet": 100, "maximum_bet": 50000,
                      "maximum_session_loss": 1000, "maximum_balance_percentage": 0.5},
    }
    app = SimpleNamespace(state=SimpleNamespace(
        wp=SimpleNamespace(repository=repository),
        risk=SimpleNamespace(profiles=lambda: profiles),
    ))
    modes = BettingModeManager(app)
    reviewed = {
        "profile": "PROFILE_A", "execution_panel": 0, "stake_amount": 100,
        "currency": "BIF", "profit_target_enabled": False, "profit_target": None,
        "loss_limit_enabled": False, "loss_limit": None,
        "win_count_limit_enabled": True, "win_count_limit": 2,
        "loss_count_limit_enabled": True, "loss_count_limit": 200,
        "cashout_target": 2.0, "reviewed_at": "2030-10-14T10:00:00+00:00",
        "saved_at": "2030-10-14T10:00:00+00:00",
    }
    repository.save_application_state("live_activation_configuration", reviewed,
                                      reviewed["reviewed_at"])
    repository.save_application_state("live_activation_configuration_draft", reviewed,
                                      reviewed["saved_at"])
    modes._reviews["old-review"] = {"expires_monotonic": 9999999999}

    saved = modes.save_live_configuration_draft({"execution_panel": 1, "stake_amount": 200})

    assert saved["execution_panel"] == 1
    assert saved["stake_amount"] == 200
    assert saved["persistence_source"] == "DRAFT"
    assert saved["reviewed_at"] is None
    assert repository.load_application_state("live_activation_configuration")["stake_amount"] == 100
    assert repository.load_application_state("live_activation_configuration_draft")["stake_amount"] == 200
    assert modes._reviews == {}
    assert modes.live_authorized is False

    modes.mode = LIVE_REAL
    with pytest.raises(ValueError, match="LIVE_ACTIVE_CONFIGURATION_IMMUTABLE"):
        modes.save_live_configuration_draft({"stake_amount": 300})


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
                        lambda: SimpleNamespace(api_key="server-secret", public_access=False))
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
        latest_round_marker=lambda: (100, "round-100"),
    )
    risk = SimpleNamespace(emergency_latched=False)
    state = SimpleNamespace(
        wp=SimpleNamespace(repository=repository),
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
        "target_round_index": 101,
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
    wrong_round = controller.can_execute_live_bet({**decision, "target_round_index": 102}, status)

    assert expired["allowed"] is False
    assert "decision expired or expiry missing" in expired["reasons"]
    assert wrong_round["allowed"] is False
    assert "target round does not match the next canonical history index" in wrong_round["reasons"]


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


def test_final_gate_excludes_only_the_current_queued_execution_reservation(tmp_path):
    repository = Repository(tmp_path / "current-execution.sqlite3")
    repository.init()
    stamp = datetime.now(timezone.utc).isoformat()
    intent = DecisionIntent(decision_id="d-current", round_id="target-current", cashout=2.0)
    assert repository.create_execution({
        "execution_id": "current-execution", "decision_id": intent.decision_id,
        "target_round_id": intent.round_id, "profile": "PROFILE_A",
        "bet_amount": 100, "cashout_target": 2.0, "status": "VALIDATING",
        "simulated": False, "created_at": stamp, "updated_at": stamp,
        "balance_before": 1000, "lifecycle_state": "QUEUED",
    })

    class Risk:
        emergency_latched = False

        def validate_execution(self, _decision, _status):
            return []

    class Rules:
        last_evaluation = {"result": True, "true_rule_ids": ["RULE_A"]}

        def status(self):
            return {"rules_enabled": 1}

        def execution_authorized(self):
            return False

    app = SimpleNamespace(state=SimpleNamespace(
        wp=SimpleNamespace(repository=repository), risk=Risk(), rules_engine=Rules(),
        betting=SimpleNamespace(
            settings=SimpleNamespace(allow_real_placement=True),
            status=lambda: {
                "mode": "REAL", "automatic_enabled": True,
                "current_balance": 1000, "last_balance_observed_at": stamp,
                "current_bet": None, "profile": "PROFILE_A",
                "session_configuration": {"mode": "AUTOMATIC"},
            },
            backend=SimpleNamespace(verified_capabilities=lambda: {}),
        ),
    ))
    modes = BettingModeManager(app)
    modes.mode = LIVE_REAL
    modes.live_authorized = True

    result = asyncio.run(modes.final_execution_gate(
        intent, execution_id="current-execution"))

    assert result["checks"]["unresolved_executions"] == "CLEAR"
    assert result["checks"]["duplicate_protection"] == "CURRENT_RESERVATION"
    assert result["checks"]["rule_to_intent_interface"] == "UNAVAILABLE"
    assert result["allowed"] is False
    assert "RULE_EXECUTION_INTENT_STORE_UNAVAILABLE" in result["reasons"]
