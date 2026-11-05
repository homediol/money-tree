from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from app.betting.config import BettingSettings
from app.betting.schemas import DecisionIntent, SessionStartRequest
from app.betting.session import BettingManager, DuplicateDecision
from app.database.repository import Repository


def run(coro):
    return asyncio.run(coro)


def test_decision_contract_uses_fixed_cashout_and_requires_expiry():
    with pytest.raises(ValidationError):
        DecisionIntent(decision_id="missing-expiry", round_id="target-1")
    with pytest.raises(ValidationError):
        DecisionIntent(decision_id="wrong-cashout", round_id="target-1",
                       expires_at=datetime.now(timezone.utc) + timedelta(seconds=30),
                       cashout=1.5)
    intent = DecisionIntent(decision_id="fixed", round_id="target-1",
                            expires_at=datetime.now(timezone.utc) + timedelta(seconds=30))
    assert intent.effective_amount() == 100
    assert intent.cashout == 2.0
    assert "profile" not in intent.as_dict()


def test_existing_executor_persists_simulated_decision_and_rejects_duplicate(tmp_path):
    repo = Repository(tmp_path / "part9.sqlite3")
    repo.init()
    settings = BettingSettings(sim_round_every_s=5, poll_interval_s=.01)
    manager = BettingManager(settings, repository=repo)

    async def scenario():
        await manager.start_session(SessionStartRequest(
            action="start", mode="SIMULATION", starting_balance=10_000,
            goal_balance=20_000))
        intent = DecisionIntent(
            decision_id="persist-d", round_id="101", cashout=2.0,
            bet_amount=250,
            expires_at=datetime.now(timezone.utc) + timedelta(seconds=30),
        )
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
    assert "profile" not in records[0]


def test_restart_reconciliation_never_repeats_placed_transaction(tmp_path):
    repo = Repository(tmp_path / "restart.sqlite3")
    repo.init()
    now = datetime.now(timezone.utc).isoformat()
    row = {"execution_id": "e1", "decision_id": "d1", "target_round_id": "r1",
           "bet_amount": 100, "cashout_target": 2.0,
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
