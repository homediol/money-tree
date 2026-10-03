"""Part 2 risk engine tests. No browser, prediction, or real betting is used."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
import httpx
from fastapi import FastAPI

from app.api import betting as betting_api, risk as risk_api
from app.betting.config import BettingSettings
from app.betting.schemas import DecisionIntent
from app.betting.session import BettingManager
from app.risk.config import get_risk_profile
from app.risk.engine import RiskManager


class FakeBetting:
    def __init__(self, **overrides):
        self.stops = []
        self.data = {
            "state": "SIMULATION", "mode": "SIMULATION",
            "profile": "PROFILE_A", "starting_balance": 10_000,
            "current_balance": 10_000, "goal_balance": 20_000,
            "current_bet": None, "consecutive_losses": 0,
            "browser_status": "SIMULATION", "last_ui_ready": True,
        }
        self.data.update(overrides)

    def status(self):
        return dict(self.data)

    async def stop_session(self, *, emergency=False, reason=None):
        self.stops.append(reason or ("emergency" if emergency else "manual"))
        self.data["state"] = "STOPPING"
        return self.status()


def intent(key="d1", **overrides):
    data = {
        "decision_id": key, "round_id": f"r-{key}", "execute": True,
        "profile": "PROFILE_A", "cashout": 2.0, "bet_amount": 250,
        "expires_at": datetime.now(timezone.utc) + timedelta(minutes=1),
    }
    data.update(overrides)
    return DecisionIntent(**data)


def run(coro):
    return asyncio.run(coro)


def manager(fake=None, events=None):
    fake = fake or FakeBetting()
    events = events if events is not None else []

    async def broadcast(payload):
        events.append(payload)

    return RiskManager(broadcaster=broadcast, betting_manager=fake), fake, events


def test_profiles_load_with_central_cashouts():
    assert get_risk_profile("profile_a").cashout == 2.0
    assert get_risk_profile("PROFILE_B").cashout == 1.5


def test_invalid_profile_rejected():
    with pytest.raises(ValueError, match="unknown risk profile"):
        get_risk_profile("PROFILE_X")


def test_valid_decision_is_approved_and_audited():
    risk, fake, events = manager()
    result = run(risk.evaluate(intent(), fake.status()))
    assert result.approved is True
    assert result.approved_bet == 250
    assert risk.audit_log[-1]["round_id"] == "r-d1"
    assert {e["type"] for e in events} >= {"risk:evaluated", "risk:approved"}


def test_manual_panels_use_per_panel_limits_and_one_combined_bankroll_limit():
    fake = FakeBetting(current_balance=40_000, goal_balance=60_000)
    fake.data["mode"] = "SHADOW"
    fake.data["automatic_enabled"] = True
    fake.data["automatic_enabled"] = True
    fake.data["session_configuration"] = {"mode": "MANUAL", "panels": [
        {"enabled": True, "stake": 1000, "cashout": 2.0},
        {"enabled": True, "stake": 1000, "cashout": 2.0},
    ]}
    risk, _, _ = manager(fake)
    evaluated = run(risk.evaluate(intent(bet_amount=2000), fake.status()))
    assert evaluated.approved is True
    assert evaluated.approved_bet == 2000
    decision = {"status": "READY_FOR_EXECUTION", "profile": "PROFILE_A",
        "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat(),
        "risk_evaluation": {"approved": True, "status": "APPROVED", "approved_bet": 2000}}
    assert risk.validate_execution(decision, fake.status()) == []


def test_manual_combined_exposure_over_bankroll_percentage_is_rejected():
    fake = FakeBetting(current_balance=10_000)
    fake.data["session_configuration"] = {"mode": "MANUAL", "panels": [
        {"enabled": True, "stake": 300, "cashout": 2.0},
        {"enabled": True, "stake": 300, "cashout": 2.0},
    ]}
    risk, _, _ = manager(fake)
    evaluated = run(risk.evaluate(intent(bet_amount=600), fake.status()))
    assert evaluated.approved is False
    assert evaluated.reason == "Combined manual exposure exceeds bankroll limits"


def test_manual_panels_use_per_panel_limits_and_one_combined_bankroll_limit():
    fake = FakeBetting(current_balance=40_000, goal_balance=60_000)
    fake.data["mode"] = "SHADOW"
    fake.data["automatic_enabled"] = True
    fake.data["session_configuration"] = {"mode": "MANUAL", "panels": [
        {"enabled": True, "stake": 1000, "cashout": 2.0},
        {"enabled": True, "stake": 1000, "cashout": 2.0},
    ]}
    risk, _, _ = manager(fake)
    evaluated = run(risk.evaluate(intent(bet_amount=2000), fake.status()))
    assert evaluated.approved is True
    assert evaluated.approved_bet == 2000
    decision = {"status": "READY_FOR_EXECUTION", "profile": "PROFILE_A",
        "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat(),
        "risk_evaluation": {"approved": True, "status": "APPROVED", "approved_bet": 2000}}
    assert risk.validate_execution(decision, fake.status()) == []


def test_manual_combined_exposure_over_bankroll_percentage_is_rejected():
    fake = FakeBetting(current_balance=10_000)
    fake.data["session_configuration"] = {"mode": "MANUAL", "panels": [
        {"enabled": True, "stake": 300, "cashout": 2.0},
        {"enabled": True, "stake": 300, "cashout": 2.0},
    ]}
    risk, _, _ = manager(fake)
    evaluated = run(risk.evaluate(intent(bet_amount=600), fake.status()))
    assert evaluated.approved is False
    assert evaluated.reason == "Combined manual exposure exceeds bankroll limits"


@pytest.mark.parametrize("amount,reason", [
    (1100, "configured maximum"),
    (600, "balance percentage"),
])
def test_bet_limits_reject(amount, reason):
    risk, fake, _ = manager()
    result = run(risk.evaluate(intent(bet_amount=amount), fake.status()))
    assert not result.approved
    assert reason in result.reason


def test_invalid_or_expired_decision_rejected():
    risk, fake, _ = manager()
    invalid = run(risk.evaluate(intent(execute=False), fake.status()))
    assert not invalid.approved
    risk2, _, _ = manager(fake)
    expired = run(risk2.evaluate(intent(expires_at=datetime.now(timezone.utc) - timedelta(seconds=1)), fake.status()))
    assert not expired.approved


def test_duplicate_decision_rejected():
    risk, fake, _ = manager()
    assert run(risk.evaluate(intent(), fake.status())).approved
    duplicate = run(risk.evaluate(intent(), fake.status()))
    assert not duplicate.approved
    assert duplicate.reason == "Duplicate decision"


def test_goal_protection_stops_session_and_emits():
    fake = FakeBetting(current_balance=20_000, goal_balance=20_000)
    risk, _, events = manager(fake)
    result = run(risk.evaluate(intent(), fake.status()))
    assert not result.approved
    assert fake.stops == ["goal_reached"]
    assert "risk:goal_protection" in {e["type"] for e in events}


def test_session_loss_limit_stops_session():
    fake = FakeBetting(current_balance=8_500)
    risk, _, events = manager(fake)
    result = run(risk.evaluate(intent(), fake.status()))
    assert not result.approved
    assert fake.stops == ["risk_loss_limit"]
    assert "risk:loss_limit_reached" in {e["type"] for e in events}


def test_consecutive_loss_limit_stops_session():
    fake = FakeBetting(consecutive_losses=3)
    risk, _, events = manager(fake)
    result = run(risk.evaluate(intent(), fake.status()))
    assert not result.approved
    assert fake.stops == ["risk_consecutive_losses"]
    assert "risk:consecutive_loss_limit" in {e["type"] for e in events}


def test_emergency_stop_latches_until_reset():
    risk, fake, events = manager()
    run(risk.emergency_stop())
    assert not run(risk.evaluate(intent(), fake.status())).approved
    assert risk.emergency_latched
    assert "risk:emergency_stop" in {e["type"] for e in events}
    run(risk.reset_emergency())
    assert not risk.emergency_latched


def test_profile_b_uses_its_own_limits():
    fake = FakeBetting(profile="PROFILE_B")
    risk, _, _ = manager(fake)
    run(risk.select_profile("PROFILE_B"))
    result = run(risk.evaluate(intent(profile="PROFILE_B", cashout=1.5,
                                      bet_amount=250), fake.status()))
    assert result.approved
    assert result.cashout == 1.5


def test_browser_health_blocks_real_mode():
    fake = FakeBetting(mode="REAL", browser_status="WAITING_FOR_BROWSER",
                       last_ui_ready=False)
    risk, _, _ = manager(fake)
    result = run(risk.evaluate(intent(), fake.status()))
    assert not result.approved
    assert "Browser" in result.reason


def test_risk_api_and_frontend_state_payload():
    async def go():
        fake = FakeBetting(state="STOPPED")
        risk, _, _ = manager(fake)
        app = FastAPI()
        app.state.betting = fake
        app.state.risk = risk
        app.include_router(risk_api.router)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test",
        ) as client:
            profiles = (await client.get("/api/risk/profiles")).json()
            changed = (await client.post("/api/risk/profile", json={"profile": "PROFILE_B"})).json()
            payload = (await client.get("/api/risk/status")).json()["status"]
        assert profiles["profiles"]["PROFILE_A"]["cashout"] == 2.0
        assert changed["profile"]["cashout"] == 1.5
        assert payload["risk_status"] == "WAITING"
        assert payload["risk_level"] == "NOT_EVALUATED"
        assert "maximum_session_loss" in payload
    run(go())


def test_risk_status_distinguishes_waiting_from_emergency_block():
    risk, fake, _ = manager()
    waiting = risk.status(fake.status())
    assert waiting["risk_status"] == "WAITING"
    assert waiting["reason"].startswith("Waiting for a fresh")

    run(risk.emergency_stop())
    blocked = risk.status(fake.status())
    assert blocked["risk_status"] == "BLOCKED"
    assert blocked["risk_level"] == "BLOCKED"
    assert blocked["reason"] == "Emergency stop is active"


def test_decision_api_routes_risk_approval_into_executor():
    async def go():
        settings = BettingSettings(
            cdp_http="http://127.0.0.1:1", allow_real_placement=False,
            poll_interval_s=0.02, browser_recheck_s=0.02,
            sim_round_every_s=1.0,
        )
        betting = BettingManager(settings)
        risk = RiskManager(betting_manager=betting)
        app = FastAPI()
        app.state.betting = betting
        app.state.risk = risk
        ready = {"status": "READY_FOR_EXECUTION", "decision_id": "pipeline-1",
                 "target_round_id": "round-1", "profile": "PROFILE_A",
                 "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat(),
                 "risk_evaluation": {"approved": True, "approved_bet": 250}}
        app.state.decision_engine = SimpleNamespace(current=lambda: ready)
        app.include_router(betting_api.router)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test",
        ) as client:
            started = await client.post("/api/betting/session", json={
                "action": "start", "mode": "SIMULATION",
                "profile": "PROFILE_A", "starting_balance": 10_000,
                "goal_balance": 20_000,
            })
            assert started.status_code == 200
            response = await client.post("/api/betting/decisions", json={
                "decision_id": "pipeline-1", "round_id": "round-1",
                "execute": True, "profile": "PROFILE_A", "cashout": 2.0,
                "bet_amount": 250,
                "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat(),
            })
            payload = response.json()
            assert payload["risk"]["approved"] is True
            assert payload["entry"]["status"] == "placed"
            assert payload["entry"]["amount_bif"] == 250
        await betting.shutdown()
    run(go())
