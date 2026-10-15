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


def setup_rule_session(tmp_path, controls=None, *, currency="USD"):
    repo = Repository(tmp_path / "financial-stops.sqlite3")
    repo.init()
    session_id = "rule-session-financial"
    selection_id = "shared-selection-101"
    session = {
        "session_id": session_id,
        "mode": "ALL_RULES_TOGETHER",
        "status": "RUNNING",
        "selection_status": "ACTIVE",
        "started_at": now(),
        "ended_at": None,
        "selection_stop_reason": None,
        "stop_reason": None,
        "configuration_snapshot": {
            "fixed_stake_rwf": 100,
            "stake_currency": currency,
            **(controls or {}),
        },
        "selections": [{
            "selection_id": selection_id,
            "selected_at": now(),
            "source_round": {"streamId": "verified-stream", "roundId": "source-100",
                             "roundIndex": 100},
            "expected_target": {"streamId": "verified-stream", "roundId": "target-101",
                                "roundIndex": 101},
            "contributing_rule_ids": ["RULE_A", "RULE_B"],
            "execution_intent": {
                "target_round_id": "target-101",
                "requested_stake": 100,
                "requested_stake_currency": currency,
                "created_at": now(),
                "expires_at": now(),
            },
        }],
        "rule_selections": {},
    }
    engine = RulesEngine(repo)
    engine.sessions[session_id] = session
    engine._persist()
    return repo, engine, session_id, selection_id


def add_selection(repo, engine, session_id, index, *, currency="USD"):
    selection_id = f"shared-selection-{index}"
    session = engine.sessions[session_id]
    session["selections"].append({
        "selection_id": selection_id,
        "selected_at": now(),
        "source_round": {"streamId": "verified-stream", "roundId": f"source-{index - 1}",
                         "roundIndex": index - 1},
        "expected_target": {"streamId": "verified-stream", "roundId": f"target-{index}",
                            "roundIndex": index},
        "contributing_rule_ids": ["RULE_A", "RULE_B", "RULE_C"],
        "execution_intent": {"target_round_id": f"target-{index}", "requested_stake": 100,
                             "requested_stake_currency": currency, "created_at": now()},
    })
    engine._persist()
    return selection_id


def execution(session_id, decision_id, target_round_id, *, currency="USD", balance_before=1000):
    stamp = now()
    return {
        "execution_id": f"execution-{decision_id}",
        "decision_id": decision_id,
        "target_round_id": target_round_id,
        "profile": "PROFILE_A",
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


def test_confirmed_shared_win_stops_once_and_survives_restart(tmp_path):
    repo, engine, session_id, selection_id = setup_rule_session(tmp_path,
        {"reconciled_win_limit_enabled": True, "reconciled_win_limit_count": 1})
    row = execution(session_id, selection_id, "target-101")

    result = reconcile(repo, engine, row, outcome="WIN", balance=1100, multiplier=2.5)
    current = engine.session(session_id)
    assert result["status"] == "RECONCILED"
    assert current["financial_stop"]["status"] == "STOPPED"
    assert current["financial_stop"]["reason"] == "WIN_LIMIT_REACHED"
    assert current["financial_stop"]["win_count"] == 1
    assert current["financial_stop"]["reconciled_bets"] == 1
    assert current["financial_stop"]["realized_betting_pl"] == 100
    assert current["selections"][0]["contributing_rule_ids"] == ["RULE_A", "RULE_B"]

    duplicate_service = ReconciliationService(repo)
    duplicate_service.rules_engine = engine
    duplicate = run(duplicate_service.reconcile(repo.execution_by_id(row["execution_id"]),
        round_id="target-101", multiplier=2.5,
        evidence=receipt("WIN", 1100, decision_id=selection_id, target_round_id="target-101")))
    assert duplicate["reconciliation_id"] == result["reconciliation_id"]
    assert engine.session(session_id)["financial_stop"]["win_count"] == 1
    assert engine.session(session_id)["financial_stop"]["reconciled_bets"] == 1

    recovered = RulesEngine(repo).session(session_id)
    assert recovered["financial_stop"]["status"] == "STOPPED"
    assert recovered["financial_stop"]["reason"] == "WIN_LIMIT_REACHED"
    assert recovered["financial_stop"]["win_count"] == 1
    assert recovered["financial_stop"]["reconciled_bets"] == 1


def test_loss_count_limit_waits_for_two_confirmed_losses(tmp_path):
    repo, engine, session_id, selection_id = setup_rule_session(tmp_path,
        {"reconciled_loss_count_limit_enabled": True, "reconciled_loss_count_limit": 2})
    first = execution(session_id, selection_id, "target-101")
    first_result = reconcile(repo, engine, first, outcome="LOSS", balance=900, multiplier=1.1)
    assert first_result["status"] == "RECONCILED"
    assert engine.session(session_id)["financial_stop"]["status"] == "ACTIVE"
    assert engine.session(session_id)["financial_stop"]["loss_count"] == 1

    second_id = add_selection(repo, engine, session_id, 102)
    second = execution(session_id, second_id, "target-102", balance_before=900)
    second_result = reconcile(repo, engine, second, outcome="LOSS", balance=800, multiplier=1.2)
    assert second_result["status"] == "RECONCILED"
    stop = engine.session(session_id)["financial_stop"]
    assert stop["status"] == "STOPPED"
    assert stop["reason"] == "LOSS_LIMIT_REACHED"
    assert stop["loss_count"] == 2
    assert stop["reconciled_bets"] == 2


def test_live_activation_limits_count_only_receipt_reconciled_outcomes(tmp_path):
    repo, engine, session_id, selection_id = setup_rule_session(tmp_path, currency="BIF")
    started = now()
    installed = engine.install_live_execution_stops({
        "live_session_id": "LIVE-test-session", "started_at": started, "currency": "BIF",
        "profit_target_enabled": True, "profit_target": 100,
        "loss_limit_enabled": True, "loss_limit": 100,
        "win_count_limit_enabled": True, "win_count_limit": 1,
        "loss_count_limit_enabled": True, "loss_count_limit": 2,
    })
    assert installed["ok"] is True
    assert installed["rules_session_id"] == session_id

    row = execution(session_id, selection_id, "target-101", currency="BIF")
    result = reconcile(repo, engine, row, outcome="WIN", balance=1100, multiplier=2.5,
                       currency="BIF")
    stop = engine.session(session_id)["live_execution_stop"]
    assert result["status"] == "RECONCILED"
    assert stop["status"] == "STOPPED"
    assert stop["reason"] == "WIN_LIMIT_REACHED"
    assert stop["realized_profit_loss"] == 100
    assert stop["win_count"] == 1
    assert stop["settled_execution_ids"] == [row["execution_id"]]
    assert engine.session(session_id)["selection_status"] == "STOPPED"

    recovered = RulesEngine(repo).session(session_id)["live_execution_stop"]
    assert recovered["status"] == "STOPPED"
    assert recovered["win_count"] == 1
    assert recovered["realized_profit_loss"] == 100


@pytest.mark.parametrize(("controls", "outcome", "balance", "expected_reason"), [
    ({"reconciled_profit_target_enabled": True, "reconciled_profit_target_rwf": 100},
     "WIN", 1100, "PROFIT_TARGET_REACHED"),
    ({"reconciled_loss_limit_enabled": True, "reconciled_loss_limit_rwf": 100},
     "LOSS", 900, "LOSS_LIMIT_REACHED"),
])
def test_receipt_backed_money_targets_stop_only_with_verified_matching_currency(
        tmp_path, controls, outcome, balance, expected_reason):
    repo, engine, session_id, selection_id = setup_rule_session(tmp_path, controls, currency="RWF")
    row = execution(session_id, selection_id, "target-101", currency="RWF")

    result = reconcile(repo, engine, row, outcome=outcome, balance=balance,
                       multiplier=2.5 if outcome == "WIN" else 1.1, currency="RWF")
    assert result["status"] == "RECONCILED"
    assert engine.session(session_id)["financial_stop"]["reason"] == expected_reason


def test_unknown_settlement_blocks_and_restart_recovers_without_counting(tmp_path):
    repo, engine, session_id, selection_id = setup_rule_session(tmp_path,
        {"reconciled_win_limit_enabled": True, "reconciled_win_limit_count": 1})
    engine.install_live_execution_stops({
        "live_session_id": "LIVE-recovery", "started_at": now(), "currency": "USD",
        "profit_target_enabled": False, "loss_limit_enabled": False,
        "win_count_limit_enabled": True, "win_count_limit": 1,
        "loss_count_limit_enabled": False,
    })
    row = execution(session_id, selection_id, "target-101")
    pending = reconcile(repo, engine, row, outcome="WIN", balance=1100,
                        multiplier=2.5, settled=False)
    assert pending["status"] == "RECONCILIATION_PENDING"
    stop = engine.session(session_id)["financial_stop"]
    assert stop["status"] == "RECONCILIATION_REQUIRED"
    assert stop["win_count"] == 0
    assert stop["loss_count"] == 0
    assert stop["reconciled_bets"] == 0
    live_stop = engine.session(session_id)["live_execution_stop"]
    assert live_stop["status"] == "RECONCILIATION_REQUIRED"
    assert live_stop["win_count"] == 0

    recovered_engine = RulesEngine(repo)
    recovered = recovered_engine.session(session_id)
    assert recovered["financial_stop"]["status"] == "RECONCILIATION_REQUIRED"
    assert recovered["financial_stop"]["win_count"] == 0
    service = ReconciliationService(repo)
    service.rules_engine = recovered_engine
    final = run(service.reconcile(row, round_id="target-101", multiplier=2.5,
        evidence=receipt("WIN", 1100, settled=True, decision_id=selection_id,
                         target_round_id="target-101")))
    assert final["status"] == "RECONCILED"
    resolved = recovered_engine.session(session_id)["financial_stop"]
    assert resolved["status"] == "STOPPED"
    assert resolved["reason"] == "WIN_LIMIT_REACHED"
    assert resolved["win_count"] == 1
    assert resolved["reconciled_bets"] == 1
    recovered_live_stop = recovered_engine.session(session_id)["live_execution_stop"]
    assert recovered_live_stop["status"] == "STOPPED"
    assert recovered_live_stop["win_count"] == 1
    assert recovered_live_stop["realized_profit_loss"] == 100


def test_money_limits_require_valid_configuration():
    engine = RulesEngine()
    boundary = {"stream_id": "stream-1", "round_id": "round-1", "observation_id": "round-1",
                "round_index": 1, "stream_sequence": 1, "multiplier": 2.0,
                "event_type": "round.persisted", "durability": "COMMITTED",
                "continuity_status": "VERIFIED"}
    with pytest.raises(ValueError, match="INVALID_NET_LIMIT"):
        engine.start_session(mode="ALL_RULES_TOGETHER", rule_ids=[], boundary=boundary, options={
            "reconciled_profit_target_enabled": True,
            "reconciled_profit_target_rwf": 0,
        })
