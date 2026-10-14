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
           "balance_currency": "USD", "platform_session_identity": "platform-s1",
           "session_id": "S-1"}
    row.update(changes); return row


def store(repo, row): assert repo.create_execution(row)


def platform_receipt(outcome="WIN", observed_balance=1100, *, settlement=True):
    evidence = {
        "platform_status": "ACCEPTED", "platform_evidence": True,
        "platform_receipt_id": "accept-1", "target_round_id": "101",
        "decision_id": "d1", "accepted_stake": 100, "accepted_currency": "USD",
        "session_identity": "platform-s1", "platform_observed_at": now(),
        "balance_before_verified": True, "balance_provider_identity": "provider-1",
        "balance_session_identity": "platform-s1", "balance_observed_at": now(),
        "balance_currency": "USD", "platform_observed_balance": observed_balance,
        "bet_result": outcome, "payout": 200 if outcome == "WIN" else 0,
    }
    if settlement:
        evidence.update({"settlement_receipt_id": "settle-1", "settled_at": now()})
    return evidence


@pytest.mark.parametrize(("multiplier","outcome","pnl","observed"), [
    (2.5, "WIN", 100, 1100),
    (1.2, "LOSS", -100, 900),
])
def test_valid_win_loss_requires_matching_platform_balance(
        setup, multiplier, outcome, pnl, observed):
    repo, service = setup; row = execution(); store(repo, row)
    result = run(service.reconcile(row, round_id="101", multiplier=multiplier,
                                   evidence=platform_receipt(outcome, observed)))
    assert result["outcome"] == outcome
    assert result["balance"]["reconciled_balance"] == observed
    saved = repo.execution_by_id("x1")
    assert saved["profit_loss"] == pnl
    assert saved["balance_after"] == observed


def test_platform_balance_mismatch_stays_unknown_and_pauses(setup):
    repo, service = setup; row = execution(); store(repo, row)
    result = run(service.reconcile(row, round_id="101", multiplier=2.5,
                                   evidence=platform_receipt("WIN", 1234)))
    assert result["status"] == "RECONCILIATION_PENDING"
    assert result["outcome"] == "UNKNOWN"
    assert result["balance"]["balance_status"] == "MISMATCH"
    assert result["balance"]["internal_expected_balance"] == 1100
    assert result["risk_action"] == "PAUSE"
    saved = repo.execution_by_id("x1")
    assert saved["profit_loss"] is None


def test_unknown_never_becomes_loss_and_cashout_states_are_distinct(setup):
    repo, service = setup; row = execution(); store(repo, row)
    result = run(service.reconcile(row, round_id="101", multiplier=3.0,
        evidence={**platform_receipt("WIN", 1100, settlement=False),
                  "cashout_requested": True, "cashout_executed": False,
                  "cashout_confirmed": False}))
    assert result["outcome"] == "UNKNOWN"
    assert result["status"] == "RECONCILIATION_PENDING"
    assert result["verification"]["platform_bet_state"] == "ACCEPTED"
    assert result["verification"]["settlement_status"] == "UNKNOWN"
    assert result["verification"]["cashout_requested"] is True
    assert result["verification"]["cashout_executed"] is False
    assert repo.execution_by_id("x1")["profit_loss"] is None


def test_unverified_balance_is_unknown_not_estimated(setup):
    repo, service = setup; row = execution(); store(repo, row)
    result = run(service.reconcile(row, round_id="101", multiplier=2.5,
                                   evidence=platform_receipt("WIN", None)))
    assert result["outcome"] == "UNKNOWN"
    assert result["status"] == "RECONCILIATION_PENDING"
    assert result["balance"]["internal_expected_balance"] is None
    assert result["balance"]["reconciled_balance"] is None


def test_immutable_ledger_and_duplicate_reconciliation(setup):
    repo, service = setup; row = execution(); store(repo, row)
    evidence = platform_receipt("LOSS", 900)
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


@pytest.mark.parametrize("multiplier", [1.1, 3.0])
def test_multiplier_without_receipts_never_creates_win_or_loss(setup, multiplier):
    repo, service = setup; row = execution(); store(repo, row)
    result = run(service.reconcile(row, round_id="101", multiplier=multiplier,
                                   evidence={"placement_confirmed": True,
                                             "cashout_confirmed": multiplier >= 2.0,
                                             "platform_observed_balance": 900}))
    assert result["status"] == "RECONCILIATION_PENDING"
    assert result["outcome"] == "UNKNOWN"
    assert result["risk_action"] == "PAUSE"
    assert repo.list_reconciliations() == []
    assert repo.execution_by_id("x1")["profit_loss"] is None


def test_pending_execution_can_reconcile_when_later_receipt_arrives(setup):
    repo, service = setup; row = execution(); store(repo, row)
    pending = run(service.reconcile(row, round_id="101", multiplier=2.5, evidence={}))
    assert pending["status"] == "RECONCILIATION_PENDING"
    assert repo.list_reconciliations() == []

    saved = repo.execution_by_id("x1")
    resolved = run(service.reconcile(saved, round_id="101", multiplier=2.5,
                                     evidence=platform_receipt("WIN", 1100)))
    assert resolved["status"] == "RECONCILED"
    assert resolved["outcome"] == "WIN"
    assert repo.execution_by_id("x1")["profit_loss"] == 100
    assert len(repo.list_reconciliations()) == 1


def test_restart_recovery_marks_unknown_and_never_duplicates(setup):
    repo, _ = setup; row = execution(); store(repo, row)
    assert repo.reconcile_incomplete_executions() == 1
    saved = repo.execution_by_id("x1")
    assert saved["status"] == "UNKNOWN"
    assert saved["lifecycle_state"] == "UNKNOWN"
    assert saved["error"] == "restart_reconciliation_required"
    assert not repo.create_execution(execution(execution_id="x2"))


def test_exact_round_matching_only(setup):
    repo, service = setup; store(repo, execution())
    assert run(service.reconcile_round({"round_id": "102", "multiplier": 1.1})) == []
    assert len(run(service.reconcile_round({"round_id": "101", "multiplier": 1.1}))) == 1


def test_exact_round_reconciliation_uses_durable_browser_evidence(setup):
    repo, service = setup
    row = execution(platform_evidence=platform_receipt("WIN", 1100))
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
