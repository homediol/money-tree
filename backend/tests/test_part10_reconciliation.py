from __future__ import annotations

import asyncio
import copy
from datetime import datetime, timezone

import pytest

from app.database.repository import Repository
from app.reconciliation.service import InvalidTransition, ReconciliationService


def run(coro): return asyncio.run(coro)
def now(): return datetime.now(timezone.utc).isoformat()


@pytest.fixture
def setup(tmp_path):
    repo = Repository(tmp_path / "part10.sqlite3"); repo.init()
    return repo, ReconciliationService(repo)


def execution(**changes):
    row = {"execution_id": "x1", "decision_id": "d1", "target_round_id": "101",
           "profile": "PROFILE_A", "bet_amount": 100, "cashout_target": 2.0,
           "status": "BET_PLACED", "lifecycle_state": "PLACED", "simulated": False,
           "created_at": now(), "updated_at": now(), "placed_at": now(),
           "resolved_at": None, "result_multiplier": None, "balance_before": 1000,
           "balance_after": None, "profit_loss": None, "error": None,
           "session_id": "S-1"}
    row.update(changes); return row


def store(repo, row): assert repo.create_execution(row)


@pytest.mark.parametrize(("multiplier","outcome","pnl","observed"), [
    (2.5, "WIN", 100, 1100),
    (1.2, "LOSS", -100, 900),
])
def test_valid_win_loss_requires_matching_platform_balance(
        setup, multiplier, outcome, pnl, observed):
    repo, service = setup; row = execution(); store(repo, row)
    result = run(service.reconcile(row, round_id="101", multiplier=multiplier, evidence={
        "placement_confirmed": True, "cashout_requested": True,
        "cashout_executed": outcome == "WIN", "cashout_confirmed": outcome == "WIN",
        "platform_observed_balance": observed,
    }))
    assert result["outcome"] == outcome
    assert result["balance"]["reconciled_balance"] == observed
    saved = repo.execution_by_id("x1")
    assert saved["profit_loss"] == pnl
    assert saved["balance_after"] == observed


def test_platform_balance_mismatch_stays_unknown_and_pauses(setup):
    repo, service = setup; row = execution(); store(repo, row)
    result = run(service.reconcile(row, round_id="101", multiplier=2.5, evidence={
        "placement_confirmed": True, "cashout_requested": True,
        "cashout_executed": True, "cashout_confirmed": True,
        "platform_observed_balance": 1234,
    }))
    assert result["status"] == "UNKNOWN"
    assert result["outcome"] == "UNKNOWN"
    assert result["balance"]["balance_status"] == "MISMATCH"
    assert result["balance"]["internal_expected_balance"] == 1100
    assert result["risk_action"] == "PAUSE"
    saved = repo.execution_by_id("x1")
    assert saved["profit_loss"] is None


def test_unknown_never_becomes_loss_and_cashout_states_are_distinct(setup):
    repo, service = setup; row = execution(); store(repo, row)
    result = run(service.reconcile(row, round_id="101", multiplier=3.0, evidence={
        "placement_confirmed": True, "cashout_requested": True,
        "cashout_executed": False, "cashout_confirmed": False,
        "platform_observed_balance": 1000,
    }))
    assert result["outcome"] == "UNKNOWN"
    assert result["verification"] == {"placement_confirmed": True,
        "cashout_requested": True, "cashout_executed": False, "cashout_confirmed": False}
    assert repo.execution_by_id("x1")["profit_loss"] is None


def test_unverified_balance_is_unknown_not_estimated(setup):
    repo, service = setup; row = execution(); store(repo, row)
    result = run(service.reconcile(row, round_id="101", multiplier=1.1, evidence={
        "placement_confirmed": True}))
    assert result["outcome"] == "UNKNOWN"
    assert result["status"] == "UNKNOWN"
    assert result["balance"]["internal_expected_balance"] == 900
    assert result["balance"]["reconciled_balance"] is None


def test_unknown_reconciliation_does_not_evaluate_prediction(setup):
    repo, service = setup
    prediction = {"prediction_id": "p-unknown", "source_round_id": "100",
                  "probability_2x": .8, "created_at": now(), "features": {"x": 7}}
    repo.save_ml_prediction(prediction)
    repo.save_decision({"decision_id": "d-unknown", "idempotency_key": "k-unknown",
                        "created_at": now(), "updated_at": now(),
                        "status": "READY_FOR_EXECUTION", "prediction_id": "p-unknown"})
    store(repo, execution(decision_id="d-unknown", execution_id="x-unknown"))
    result = run(service.reconcile(execution(decision_id="d-unknown", execution_id="x-unknown"),
                                   round_id="101", multiplier=3.0,
                                   evidence={"placement_confirmed": True,
                                             "cashout_confirmed": False,
                                             "platform_observed_balance": 1000}))
    assert result["status"] == "UNKNOWN"
    assert repo.list_prediction_evaluations() == []


def test_immutable_ledger_and_duplicate_reconciliation(setup):
    repo, service = setup; row = execution(); store(repo, row)
    evidence = {"placement_confirmed": True, "platform_observed_balance": 900}
    first = run(service.reconcile(row, round_id="101", multiplier=1.1, evidence=evidence))
    before = copy.deepcopy(repo.list_ledger())
    second = run(service.reconcile(row, round_id="101", multiplier=1.1, evidence=evidence))
    assert second["reconciliation_id"] == first["reconciliation_id"]
    assert repo.list_ledger() == before


def test_round_mismatch_and_invalid_transition(setup):
    repo, service = setup; row = execution(); store(repo, row)
    with pytest.raises(ValueError, match="round_mismatch"):
        run(service.reconcile(row, round_id="102", multiplier=2.1, evidence={}))
    with pytest.raises(InvalidTransition):
        service.transition(row, "CLOSED")


def test_restart_recovery_marks_unknown_and_never_duplicates(setup):
    repo, _ = setup; row = execution(); store(repo, row)
    assert repo.reconcile_incomplete_executions() == 1
    saved = repo.execution_by_id("x1")
    assert saved["status"] == "UNKNOWN"
    assert saved["lifecycle_state"] == "UNKNOWN"
    assert saved["error"] == "restart_reconciliation_required"
    assert not repo.create_execution(execution(execution_id="x2"))


def test_prediction_snapshot_immutable_and_no_future_leakage(setup):
    repo, service = setup
    prediction = {"prediction_id": "p1", "source_round_id": "100",
                  "probability_2x": .8, "created_at": now(), "features": {"x": 7}}
    repo.save_ml_prediction(prediction)
    repo.save_decision({"decision_id": "d1", "idempotency_key": "k1",
                        "created_at": now(), "updated_at": now(), "status": "READY_FOR_EXECUTION",
                        "prediction_id": "p1"})
    row = execution(); store(repo, row)
    run(service.reconcile(row, round_id="101", multiplier=2.5, evidence={
        "placement_confirmed": True, "cashout_confirmed": True,
        "platform_observed_balance": 1100}))
    evaluation = repo.list_prediction_evaluations()[0]
    assert evaluation["prediction_snapshot"] == prediction
    assert repo.latest_ml_prediction() == prediction

    row2 = execution(execution_id="x2", decision_id="d2", target_round_id="99")
    store(repo, row2)
    repo.save_decision({"decision_id": "d2", "idempotency_key": "k2",
                        "created_at": now(), "updated_at": now(), "status": "READY_FOR_EXECUTION",
                        "prediction_id": "p1"})
    run(service.reconcile(row2, round_id="99", multiplier=2.5, evidence={
        "placement_confirmed": True, "cashout_confirmed": True,
        "platform_observed_balance": 1100}))
    assert len(repo.list_prediction_evaluations()) == 1


def test_exact_round_matching_only(setup):
    repo, service = setup; store(repo, execution())
    assert run(service.reconcile_round({"round_id": "102", "multiplier": 1.1})) == []
    assert len(run(service.reconcile_round({"round_id": "101", "multiplier": 1.1}))) == 1


def test_exact_round_reconciliation_uses_durable_browser_evidence(setup):
    repo, service = setup
    row = execution(platform_evidence={
        "placement_confirmed": True,
        "cashout_requested": True,
        "cashout_executed": True,
        "cashout_confirmed": True,
        "platform_observed_balance": 1100,
        "observation_complete": True,
    })
    store(repo, row)

    result = run(service.reconcile_round({"round_id": "101", "multiplier": 2.5}))[0]

    assert result["status"] == "RECONCILED"
    assert result["outcome"] == "WIN"
    assert result["balance"]["reconciled_balance"] == 1100
    assert repo.execution_by_id("x1")["status"] == "RECONCILED"


def test_exact_round_waits_for_browser_evidence_without_freezing_unknown(setup):
    repo, service = setup
    store(repo, execution())

    pending = run(service.reconcile_round({"round_id": "101", "multiplier": 2.5}))[0]

    assert pending["status"] == "RECONCILIATION_PENDING"
    assert repo.list_reconciliations() == []
    saved = repo.execution_by_id("x1")
    assert saved["exact_round_result"] == {"round_id": "101", "multiplier": 2.5}


def test_simulated_session_keeps_reconciled_terminal_status(tmp_path):
    from app.betting.config import BettingSettings
    from app.betting.schemas import DecisionIntent, SessionStartRequest
    from app.betting.session import BettingManager

    repo = Repository(tmp_path / "sim-terminal.sqlite3"); repo.init()
    manager = BettingManager(BettingSettings(sim_round_every_s=.05, poll_interval_s=.01),
                             repository=repo)
    service = ReconciliationService(repo, betting_manager=manager)
    manager.reconciler = service

    async def scenario():
        await manager.start_session(SessionStartRequest(
            action="start", mode="SIMULATION", profile="PROFILE_A",
            starting_balance=1000, goal_balance=2000))
        await manager.submit_decision(DecisionIntent(
            decision_id="sim-terminal", round_id="101", profile="PROFILE_A",
            cashout=2.0, bet_amount=100))
        for _ in range(50):
            saved = repo.execution_for("sim-terminal", "101")
            if saved and saved["status"] == "RECONCILED":
                break
            await asyncio.sleep(.01)
        assert repo.execution_for("sim-terminal", "101")["status"] == "RECONCILED"
        await manager.stop_session()
        await manager.shutdown()

    run(scenario())
