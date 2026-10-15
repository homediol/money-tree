from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from app.database.repository import Repository
from app.reconciliation.service import ReconciliationService
from app.rules_engine import RulesEngine


def run(coro):
    return asyncio.run(coro)


def now():
    return datetime.now(timezone.utc).isoformat()


def setup_rule_session(tmp_path):
    repo = Repository(tmp_path / "settlement-integrity.sqlite3")
    repo.init()
    session_id = "rule-session-integrity"
    selection_id = "shared-selection-101"
    session = {
        "session_id": session_id,
        "mode": "ALL_RULES_TOGETHER",
        "status": "RUNNING",
        "selection_status": "ACTIVE",
        "configuration_schema_version": 2,
        "started_at": now(),
        "ended_at": None,
        "selection_stop_reason": None,
        "stop_reason": None,
        "configuration_snapshot": {"window_size": 50},
        "settlement_integrity": {"status": "CLEAR", "reason": None,
                                  "reconciled_bets": 0, "reconciled_execution_ids": []},
        "selections": [{
            "selection_id": selection_id,
            "selected_at": now(),
            "source_round": {"streamId": "verified-stream", "roundId": "source-100",
                             "roundIndex": 100},
            "expected_target": {"streamId": "verified-stream", "roundId": "target-101",
                                "roundIndex": 101},
            "contributing_rule_ids": ["RULE_A", "RULE_B"],
            "execution_intent": {
                "target_round_id": "target-101", "created_at": now(),
                "expires_at": now(), "execution_status": "BLOCKED_READ_ONLY",
            },
        }],
        "rule_selections": {},
    }
    engine = RulesEngine(repo)
    engine.sessions[session_id] = session
    engine._persist()
    return repo, engine, session_id, selection_id


def execution(session_id, decision_id, target_round_id, *, currency="USD", balance_before=1000):
    stamp = now()
    # A pre-existing ledger row is test evidence for reconciliation only.
    return {
        "execution_id": f"execution-{decision_id}",
        "decision_id": decision_id,
        "target_round_id": target_round_id,
        "profile": "LEGACY_HISTORICAL",
        "bet_amount": 100,
        "cashout_target": 2.0,
        "status": "BET_PLACED",
        "lifecycle_state": "PLACED",
        "simulated": False,
        "created_at": stamp,
        "updated_at": stamp,
        "placed_at": stamp,
        "resolved_at": None,
        "result_multiplier": None,
        "balance_before": balance_before,
        "balance_after": None,
        "profit_loss": None,
        "error": None,
        "balance_currency": currency,
        "platform_session_identity": "platform-session-1",
        "session_id": session_id,
    }


def receipt(outcome, observed_balance, *, currency="USD", settled=True,
            decision_id="shared-selection-101", target_round_id="target-101"):
    evidence = {
        "platform_status": "ACCEPTED",
        "platform_evidence": True,
        "platform_receipt_id": f"acceptance-{decision_id}",
        "target_round_id": target_round_id,
        "decision_id": decision_id,
        "accepted_stake": 100,
        "accepted_currency": currency,
        "session_identity": "platform-session-1",
        "platform_observed_at": now(),
        "balance_before_verified": True,
        "balance_provider_identity": "provider-1",
        "balance_session_identity": "platform-session-1",
        "balance_observed_at": now(),
        "balance_currency": currency,
        "platform_observed_balance": observed_balance,
        "bet_result": outcome,
        "payout": 200 if outcome == "WIN" else 0,
    }
    if settled:
        evidence.update({"settlement_receipt_id": f"settlement-{decision_id}", "settled_at": now()})
    return evidence


def reconcile(repo, engine, row, *, outcome, balance, multiplier, currency="USD", settled=True):
    assert repo.create_execution(row)
    service = ReconciliationService(repo)
    service.rules_engine = engine
    return run(service.reconcile(row, round_id=row["target_round_id"], multiplier=multiplier,
        evidence=receipt(outcome, balance, currency=currency, settled=settled,
                         decision_id=row["decision_id"], target_round_id=row["target_round_id"])))


def test_verified_settlement_is_recorded_once_and_survives_restart(tmp_path):
    repo, engine, session_id, selection_id = setup_rule_session(tmp_path)
    row = execution(session_id, selection_id, "target-101")

    result = reconcile(repo, engine, row, outcome="WIN", balance=1100, multiplier=2.5)
    current = engine.session(session_id)
    integrity = current["settlement_integrity"]
    financials = repo.sync_rule_session_financials(session_id)
    assert result["status"] == "RECONCILED"
    assert financials["realized_betting_pl"] == 100
    assert financials["starting_balance"] is None
    assert financials["balance_change"] is None
    assert "non_betting_balance_movement" not in financials
    assert integrity["status"] == "CLEAR"
    assert integrity["reconciled_bets"] == 1
    assert integrity["reconciled_execution_ids"] == [row["execution_id"]]
    assert current["selections"][0]["financial_result"]["status"] == "RECONCILED"
    assert current["selections"][0]["contributing_rule_ids"] == ["RULE_A", "RULE_B"]

    duplicate_service = ReconciliationService(repo)
    duplicate_service.rules_engine = engine
    duplicate = run(duplicate_service.reconcile(repo.execution_by_id(row["execution_id"]),
        round_id="target-101", multiplier=2.5,
        evidence=receipt("WIN", 1100, decision_id=selection_id, target_round_id="target-101")))
    assert duplicate["reconciliation_id"] == result["reconciliation_id"]
    assert engine.session(session_id)["settlement_integrity"]["reconciled_bets"] == 1

    recovered = RulesEngine(repo).session(session_id)
    assert recovered["settlement_integrity"]["status"] == "CLEAR"
    assert recovered["settlement_integrity"]["reconciled_execution_ids"] == [row["execution_id"]]


def test_unknown_settlement_pauses_and_requires_receipt_before_resume(tmp_path):
    repo, engine, session_id, selection_id = setup_rule_session(tmp_path)
    row = execution(session_id, selection_id, "target-101")
    pending = reconcile(repo, engine, row, outcome="WIN", balance=1100,
                        multiplier=2.5, settled=False)

    assert pending["status"] == "RECONCILIATION_PENDING"
    paused = engine.session(session_id)
    assert paused["status"] == "PAUSED"
    assert paused["selection_status"] == "STOPPED"
    assert paused["settlement_integrity"]["status"] == "RECONCILIATION_REQUIRED"
    assert paused["settlement_integrity"]["reconciled_bets"] == 0
    assert paused["settlement_integrity"]["reconciled_execution_ids"] == []
    with pytest.raises(ValueError, match="UNRECONCILED_EXECUTION_REMAINS"):
        engine.session_action(session_id, "resume")

    recovered_engine = RulesEngine(repo)
    recovered = recovered_engine.session(session_id)
    assert recovered["settlement_integrity"]["status"] == "RECONCILIATION_REQUIRED"
    service = ReconciliationService(repo)
    service.rules_engine = recovered_engine
    final = run(service.reconcile(repo.execution_by_id(row["execution_id"]),
        round_id="target-101", multiplier=2.5,
        evidence=receipt("WIN", 1100, settled=True, decision_id=selection_id,
                         target_round_id="target-101")))
    assert final["status"] == "RECONCILED"
    assert recovered_engine.session(session_id)["settlement_integrity"]["status"] == "CLEAR"
    resumed = recovered_engine.session_action(session_id, "resume")
    assert resumed["status"] == "RUNNING"
    assert resumed["selection_status"] == "ACTIVE"


def test_optional_live_bankroll_stops_are_retired_without_changing_session(tmp_path):
    _repo, engine, session_id, _selection_id = setup_rule_session(tmp_path)
    before = engine.session(session_id)
    result = engine.install_live_execution_stops({
        "profit_target_enabled": True, "profit_target": 100,
        "loss_limit_enabled": True, "loss_limit": 100,
        "maximum_exposure_percentage": 5,
    })

    assert result == {"ok": False, "reason": "OPTIONAL_BANKROLL_MANAGEMENT_REMOVED"}
    after = engine.session(session_id)
    assert after["configuration_snapshot"] == before["configuration_snapshot"]
    assert after["selection_status"] == "ACTIVE"
    assert after["settlement_integrity"] == before["settlement_integrity"]
