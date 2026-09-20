from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from app.betting.config import BettingSettings
from app.betting.schemas import DecisionIntent, SessionStartRequest
from app.betting.session import BettingManager, DuplicateDecision
from app.database.repository import Repository
from app.risk.engine import RiskManager


def run(coro):
    return asyncio.run(coro)


def approved_decision(**changes):
    now = datetime.now(timezone.utc)
    row = {
        "decision_id": "part9-d1", "status": "READY_FOR_EXECUTION",
        "source_round_id": "100", "target_round_id": "101",
        "profile": "PROFILE_A", "expires_at": (now + timedelta(seconds=30)).isoformat(),
        "risk_evaluation": {"approved": True, "status": "APPROVED",
                            "approved_bet": 250, "approved_bet_amount": 250},
    }
    row.update(changes)
    return row


def session_status(**changes):
    row = {"automatic_enabled": True, "state": "SIMULATION", "mode": "SIMULATION",
           "profile": "PROFILE_A", "starting_balance": 10_000,
           "current_balance": 10_000, "goal_balance": 20_000,
           "consecutive_losses": 0, "current_bet": None,
           "browser_status": "SIMULATION", "last_ui_ready": True}
    row.update(changes)
    return row


def test_final_risk_validation_blocks_changed_conditions():
    risk = RiskManager()
    assert risk.validate_execution(approved_decision(), session_status()) == []
    assert "automatic_mode_off" in risk.validate_execution(
        approved_decision(), session_status(automatic_enabled=False))
    assert "goal_reached" in risk.validate_execution(
        approved_decision(), session_status(current_balance=20_000))
    risk.emergency_latched = True
    assert "emergency_stop" in risk.validate_execution(approved_decision(), session_status())


def test_profile_change_does_not_mutate_existing_decision():
    decision = approved_decision()
    risk = RiskManager()
    run(risk.select_profile("PROFILE_B"))
    assert "profile_changed" in risk.validate_execution(decision, session_status())
    assert decision["profile"] == "PROFILE_A"
    assert decision["risk_evaluation"]["approved_bet"] == 250


def test_persistent_duplicate_protection_and_execution_record(tmp_path):
    repo = Repository(tmp_path / "part9.sqlite3"); repo.init()
    settings = BettingSettings(sim_round_every_s=5, poll_interval_s=.01)
    manager = BettingManager(settings, repository=repo)

    async def scenario():
        await manager.start_session(SessionStartRequest(
            action="start", mode="SIMULATION", profile="PROFILE_A",
            starting_balance=10_000, goal_balance=20_000))
        intent = DecisionIntent(decision_id="persist-d", round_id="101", profile="PROFILE_A",
                                cashout=2.0, bet_amount=250,
                                expires_at=datetime.now(timezone.utc) + timedelta(seconds=30))
        entry = await manager.submit_decision(intent)
        assert entry["status"] == "placed"
        with pytest.raises(DuplicateDecision):
            await manager.submit_decision(intent)
        await manager.stop_session()
        await manager.shutdown()
    run(scenario())
    records = repo.list_executions()
    assert len(records) == 1
    assert records[0]["status"] == "BET_PLACED"
    assert records[0]["simulated"] is True


def test_restart_reconciliation_never_repeats_placed_transaction(tmp_path):
    repo = Repository(tmp_path / "restart.sqlite3"); repo.init()
    now = datetime.now(timezone.utc).isoformat()
    row = {"execution_id": "e1", "decision_id": "d1", "target_round_id": "r1",
           "profile": "PROFILE_A", "bet_amount": 100, "cashout_target": 2.0,
           "status": "BET_PLACED", "simulated": False, "created_at": now,
           "updated_at": now, "placed_at": now, "resolved_at": None,
           "result_multiplier": None, "balance_before": 5000,
           "balance_after": None, "profit_loss": None, "error": None}
    assert repo.create_execution(row)
    assert repo.reconcile_incomplete_executions() == 1
    saved = repo.execution_for("d1", "r1")
    assert saved["status"] == "UNKNOWN"
    assert saved["error"] == "restart_reconciliation_required"
    assert not repo.create_execution({**row, "execution_id": "e2"})


def test_profiles_have_required_cashout_targets():
    from app.profiles import RISK_PROFILES
    assert RISK_PROFILES["PROFILE_A"].cashout == 2.0
    assert RISK_PROFILES["PROFILE_B"].cashout == 1.5
