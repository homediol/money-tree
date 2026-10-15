import asyncio
import json
from types import SimpleNamespace

from app.api.betting import submit_decision
from app.api.rules_engine import status as rules_status
from app.betting.schemas import DecisionIntent
from app.rules_engine import RulesEngine


def test_empty_engine_returns_no_action_and_cannot_authorize_execution():
    engine = RulesEngine()

    result = engine.evaluate({"round_id": "r-1", "round_index": 1})

    assert result["result"] is False
    assert result["decision"] == "NO_ACTION"
    assert result["reason"] == "NOT_CONFIGURED"
    assert result["true_rule_ids"] == []
    assert result["rules_configured"] == 0
    assert result["rules_enabled"] == 0
    assert result["automatic_execution"] == "OFF"
    assert result["can_bet"] is False
    assert engine.execution_authorized() is False
    assert engine.status()["last_rule_evaluation"] != "NONE"
    assert engine.status()["last_evaluated_round"] == {"round_id": "r-1", "round_index": 1}


def test_empty_evaluation_updates_last_evaluation_without_creating_a_decision():
    engine = RulesEngine()
    result = engine.evaluate({"round_id": "r-5", "round_index": 5})
    status = engine.status()

    assert result["decision"] == "NO_ACTION"
    assert status["last_rule_evaluation"] != "NONE"
    assert status["last_evaluated_round"] == {"round_id": "r-5", "round_index": 5}


def test_history_epoch_reset_invalidates_only_old_prospective_state_and_preserves_financials():
    engine = RulesEngine()
    financial_stop = {"status": "ACTIVE", "currency": "BIF", "win_count": 2,
                      "loss_count": 1, "realized_betting_pl": 50.0}
    engine.sessions["old-session"] = {
        "session_id": "old-session", "mode": "ALL_RULES_TOGETHER", "status": "RUNNING",
        "selection_status": "ACTIVE", "started_at": "2026-10-14T10:00:00Z",
        "financial_stop": financial_stop.copy(), "selections": [
            {"selection_id": "pending-selection", "status": "PENDING", "result": "PENDING",
             "execution_intent": {"execution_status": "BLOCKED_READ_ONLY"}},
            {"selection_id": "settled-selection", "status": "WIN", "result": "WIN",
             "financial_result": {"status": "RECONCILED", "profit_loss": 50.0}},
        ], "rule_selections": {"RULE_A": []}, "history_epoch": 1,
        "auto_next_armed": True,
    }

    result = engine.reset_history_epoch(2)

    session = engine.session("old-session")
    assert result == {"history_epoch": 2, "invalidated_sessions": 1,
                      "expired_selections": 1, "already_current": False}
    assert session["status"] == "COMPLETED"
    assert session["selection_status"] == "STOPPED"
    assert session["auto_next_armed"] is False
    assert session["history_epoch"] == 2
    assert session["selections"][0]["status"] == "EXPIRED"
    assert session["selections"][0]["execution_intent"]["execution_status"] == "EXPIRED"
    assert session["selections"][1]["status"] == "WIN"
    assert session["selections"][1]["financial_result"]["profit_loss"] == 50.0
    assert session["financial_stop"] == financial_stop
    assert engine.last_evaluation["reason"] == "HISTORY_EPOCH_RESET"
    assert engine.last_evaluation["history_epoch"] == 2
    assert engine._snapshot()["history_epoch"] == 2


def test_unconfigured_rule_contract_has_no_evaluation_or_signal():
    assert RulesEngine.not_configured("RULE_001") == {
        "rule_id": "RULE_001",
        "enabled": True,
        "evaluated_round": None,
        "result": False,
        "reason": "NOT_CONFIGURED",
    }


def test_aggregator_preserves_independent_true_rules_and_ignores_disabled_true():
    rows = [
        {"rule_id": "RULE_A", "enabled": True, "evaluated_round": "r-2", "result": True, "reason": "MATCH"},
        {"rule_id": "RULE_B", "enabled": False, "evaluated_round": "r-2", "result": True, "reason": "DISABLED"},
        {"rule_id": "RULE_C", "enabled": True, "evaluated_round": "r-2", "result": True, "reason": "MATCH"},
    ]

    result = RulesEngine.aggregate(rows)

    assert result["result"] is True
    assert result["true_rule_ids"] == ["RULE_A", "RULE_C"]
    assert result["rule_evaluations"] == rows
    assert result["decision"] == "RULE_MATCH"
    assert result["can_bet"] is False
    assert result["automatic_execution"] == "OFF"


def test_no_match_and_invalid_or_duplicate_rule_results_fail_closed():
    no_match = RulesEngine.aggregate([
        {"rule_id": "RULE_A", "enabled": True, "evaluated_round": "r-3", "result": False, "reason": "NO_MATCH"},
    ])
    duplicate = RulesEngine.aggregate([
        {"rule_id": "RULE_A", "enabled": True, "evaluated_round": None, "result": True, "reason": "MATCH"},
        {"rule_id": "RULE_A", "enabled": True, "evaluated_round": None, "result": True, "reason": "MATCH"},
    ])
    malformed = RulesEngine.aggregate([
        {"rule_id": "RULE_A", "enabled": True, "result": True, "reason": "MATCH"},
    ])

    assert no_match["decision"] == "NO_ACTION"
    assert no_match["reason"] == "NO_RULE_MATCHED"
    assert duplicate["result"] is False
    assert duplicate["decision"] == "NO_ACTION"
    assert duplicate["reason"] == "INVALID_RULE_ID"
    assert malformed["result"] is False
    assert malformed["reason"] == "INVALID_RULE_EVALUATION"


def test_rules_status_includes_real_history_snapshot_and_zero_rule_counts():
    latest = {"round_id": "canonical-12", "round_index": 12, "multiplier": 2.4}
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
        rules_engine=RulesEngine(),
        history_collector=SimpleNamespace(status=lambda: {
            "status": "STALE", "running": False, "count": 12, "latest": latest,
        }),
    )))

    result = rules_status(request)

    assert result["collector_status"]["status"] == "STALE"
    assert result["latest_real_round"] == latest
    assert result["total_history"] == 12
    assert result["rules_configured"] == 0
    assert result["rules_enabled"] == 0
    assert result["decision"] == "NO_ACTION"
    assert result["automatic_execution"] == "OFF"


def test_rules_status_counts_registry_separately_from_active_session_lifecycle():
    engine = RulesEngine()
    rule_ids = [row["rule_id"] for row in engine.registry()["rules"]]
    research_ids = set(engine.registry()["modes"][-2]["rule_ids"])
    participating = next(rule_id for rule_id in rule_ids if rule_id in research_ids)
    for rule_id in rule_ids[:6]:
        engine.configure(rule_id, enabled=(rule_id == participating))
    boundary = {"event_type": "round.persisted", "durability": "COMMITTED",
                "continuity_status": "VERIFIED", "stream_id": "stream", "stream_sequence": 5,
                "round_id": "round-5", "round_index": 5, "multiplier": 1.5}
    engine.start_session(mode="FIVE_RULE_TRACKER", rule_ids=[participating], boundary=boundary)

    status = engine.status()

    assert status["rules_configured"] == 6
    assert status["rules_enabled"] == 1
    assert status["active_session_status"] == "RUNNING"
    assert status["session_running"] is True
    assert status["session_rule_count"] == 1
    assert status["rules_execution_enabled"] is False
    assert status["live_execution_authorized"] is False


def test_decision_submission_fails_closed_with_no_rules():
    engine = RulesEngine()
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(rules_engine=engine)))
    intent = DecisionIntent(decision_id="attempt-1", round_id="round-2", cashout=2.0)

    response = asyncio.run(submit_decision(request, intent))
    body = json.loads(response.body)

    assert response.status_code == 409
    assert body["decision"] == "NO_ACTION"
    assert body["error"] == "rules_engine_not_configured"
    assert body["automatic_execution"] == "OFF"
    assert body["rule_evaluation"]["true_rule_ids"] == []


def test_true_rule_selection_does_not_bypass_missing_intent_risk_or_platform_contract():
    class SelectedRules(RulesEngine):
        def __init__(self):
            super().__init__()
            self.last_evaluation = {
                "result": True,
                "decision": "RULE_MATCH",
                "true_rule_ids": ["RULE_A", "RULE_B"],
                "selection_id": "one-canonical-selection",
            }

        def status(self):
            return {"rules_enabled": 2}

    engine = SelectedRules()
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(rules_engine=engine)))
    intent = DecisionIntent(decision_id="attempt-true", round_id="provider-round-2", cashout=2.0)

    response = asyncio.run(submit_decision(request, intent))
    body = json.loads(response.body)

    assert response.status_code == 409
    assert body["decision"] == "NO_ACTION"
    assert body["error"] == "decision_interface_not_connected"
    assert body["rule_evaluation"]["true_rule_ids"] == ["RULE_A", "RULE_B"]
    assert body["rule_evaluation"]["selection_id"] == "one-canonical-selection"
    assert body["automatic_execution"] == "OFF"
    assert any("Durable Shared SELECT intent persistence is unavailable" in reason
               for reason in body["reasons"])
    assert any("Risk Engine approval" in reason for reason in body["reasons"])
    assert any("provider round ID" in reason for reason in body["reasons"])
    assert any("settlement receipts" in reason for reason in body["reasons"])
