import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.api.betting import submit_decision
from app.api.rules_engine import reconfigure_session, status as rules_status
from app.betting.schemas import DecisionIntent
from app.rules_engine import RulesEngine
from starlette.requests import Request


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


def test_bounded_live_event_cache_preserves_ordered_rules_decisions_and_history():
    class UntrimmedRulesEngine(RulesEngine):
        @staticmethod
        def _trim_committed_event_records(_session, *, keep=100):
            return None

    boundary = {
        "stream_id": "bounded-cache-stream", "round_id": "cache-round-1",
        "observation_id": "cache-round-1", "round_index": 1,
        "stream_sequence": 1, "multiplier": 1.5,
        "event_type": "round.persisted", "durability": "COMMITTED",
        "continuity_status": "VERIFIED",
    }
    bounded = RulesEngine()
    reference = UntrimmedRulesEngine()
    cache_test_options = {"window_size": 500}
    bounded_session = bounded.start_session(mode="ALL_RULES_TOGETHER", rule_ids=[],
                                            boundary=boundary, options=cache_test_options)
    reference_session = reference.start_session(mode="ALL_RULES_TOGETHER", rule_ids=[],
                                                boundary=boundary, options=cache_test_options)

    observed_decisions = []
    for round_index in range(2, 152):
        event = {
            **boundary,
            "round_id": f"cache-round-{round_index}",
            "observation_id": f"cache-round-{round_index}",
            "round_index": round_index,
            "stream_sequence": round_index,
            "multiplier": 1.25 if round_index % 2 else 2.5,
        }
        bounded_result = bounded.observe_round(event)
        reference_result = reference.observe_round(event)
        observed_decisions.append((bounded_result["decision"], reference_result["decision"]))
        assert [row["decision"] for row in bounded_result["sessions"]] == [
            row["decision"] for row in reference_result["sessions"]]

    assert all(left == right for left, right in observed_decisions)
    bounded_state = bounded.session(bounded_session["session_id"])
    reference_state = reference.session(reference_session["session_id"])
    assert bounded_state["last_round_index"] == reference_state["last_round_index"] == 151
    assert bounded_state["observed_rounds"] == reference_state["observed_rounds"]
    assert bounded_state["engines"] == reference_state["engines"]
    assert [
        (row["source_round"], row["target_round"], row["decision"])
        for row in bounded_state["evaluations"]
    ] == [
        (row["source_round"], row["target_round"], row["decision"])
        for row in reference_state["evaluations"]
    ]
    assert len(bounded_state["event_records"]) == 100
    assert len(reference_state["event_records"]) == 150

    # The retained cursor still makes a replay older than the live cache a no-op.
    replay = bounded.observe_round({
        **boundary, "round_id": "cache-round-2", "observation_id": "cache-round-2",
        "round_index": 2, "stream_sequence": 2, "multiplier": 1.25,
    })
    assert replay["sessions"] == []
    assert bounded.session(bounded_session["session_id"])["last_round_index"] == 151


def test_session_reconfiguration_requires_exact_operator_confirmation_and_preserves_history():
    engine = RulesEngine()
    boundary = {
        "stream_id": "reconfigure-stream", "round_id": "boundary-10",
        "observation_id": "boundary-10", "round_index": 10,
        "stream_sequence": 10, "multiplier": 1.5,
        "event_type": "round.persisted", "durability": "COMMITTED",
        "continuity_status": "VERIFIED",
    }
    original = engine.start_session(mode="ALL_RULES_TOGETHER", rule_ids=[],
        boundary=boundary, options={"win_limit_enabled": True, "win_limit_count": 1,
                                    "loss_limit_enabled": False, "loss_limit_count": None})
    session_id = original["session_id"]

    try:
        engine.close_for_reconfiguration(session_id, "new-session", "CONFIRM")
    except ValueError as exc:
        assert str(exc) == "OPERATOR_CONFIRMATION_REQUIRED"
    else:
        raise AssertionError("a non-matching confirmation must not reconfigure the session")
    unchanged = engine.session(session_id)
    assert unchanged["status"] == "RUNNING"
    assert unchanged["configuration_snapshot"]["win_limit_enabled"] is True

    closed = engine.close_for_reconfiguration(
        session_id, "new-session", f"RECONFIGURE SESSION {session_id}")
    assert closed["status"] == "RECONFIGURED"
    assert closed["configuration_snapshot"]["win_limit_enabled"] is True
    assert closed["reconfigured_to_session_id"] == "new-session"
    assert closed["stop_history"][-1]["reason"] == "RECONFIGURED"
    assert closed["stop_history"][-1]["source"] == "OPERATOR_CONFIRMED_RECONFIGURATION"
    assert closed["stop_history"][-1]["details"]["operator_confirmation"] is True


def test_reconfigure_api_missing_confirmation_does_not_mutate_session():
    engine = RulesEngine()
    boundary = {
        "stream_id": "api-reconfigure-stream", "round_id": "api-boundary-10",
        "observation_id": "api-boundary-10", "round_index": 10,
        "stream_sequence": 10, "multiplier": 1.5,
        "event_type": "round.persisted", "durability": "COMMITTED",
        "continuity_status": "VERIFIED",
    }
    session = engine.start_session(mode="ALL_RULES_TOGETHER", rule_ids=[],
        boundary=boundary, options={"win_limit_enabled": True, "win_limit_count": 1,
                                    "loss_limit_enabled": False, "loss_limit_count": None})
    app = SimpleNamespace(state=SimpleNamespace(rules_engine=engine))
    payload = b"{}"
    sent = False

    async def receive():
        nonlocal sent
        if sent:
            return {"type": "http.request", "body": b"", "more_body": False}
        sent = True
        return {"type": "http.request", "body": payload, "more_body": False}

    request = Request({"type": "http", "method": "POST",
        "path": f"/api/rules-engine/sessions/{session['session_id']}/reconfigure",
        "headers": [], "query_string": b"", "server": ("test", 80),
        "client": ("test", 1), "scheme": "http", "app": app}, receive)

    try:
        asyncio.run(reconfigure_session(request, session["session_id"]))
    except Exception as exc:
        assert getattr(exc, "status_code", None) == 409
        assert exc.detail["error"] == "operator_confirmation_required"
    else:
        raise AssertionError("the API must require a session-specific confirmation")
    assert engine.session(session["session_id"])["status"] == "RUNNING"


def test_empty_evaluation_updates_last_evaluation_without_creating_a_decision():
    engine = RulesEngine()
    result = engine.evaluate({"round_id": "r-5", "round_index": 5})
    status = engine.status()

    assert result["decision"] == "NO_ACTION"
    assert status["last_rule_evaluation"] != "NONE"
    assert status["last_evaluated_round"] == {"round_id": "r-5", "round_index": 5}


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


def test_decision_submission_fails_closed_with_no_rules():
    engine = RulesEngine()
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(rules_engine=engine)))
    intent = DecisionIntent(decision_id="attempt-1", round_id="round-2", cashout=2.0,
                            expires_at=datetime.now(timezone.utc) + timedelta(seconds=30))

    response = asyncio.run(submit_decision(request, intent))
    body = json.loads(response.body)

    assert response.status_code == 409
    assert body["decision"] == "NO_ACTION"
    assert body["error"] == "eligible_rule_decision_not_found"
    assert body["automatic_execution"] == "OFF"
    assert body["reasons"] == ["decision_id_does_not_match_a_pending_selection_from_the_active_rules_session"]


def test_rule_status_without_a_persisted_selection_is_not_executable():
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
    intent = DecisionIntent(decision_id="attempt-true", round_id="provider-round-2", cashout=2.0,
                            expires_at=datetime.now(timezone.utc) + timedelta(seconds=30))

    response = asyncio.run(submit_decision(request, intent))
    body = json.loads(response.body)

    assert response.status_code == 409
    assert body["decision"] == "NO_ACTION"
    assert body["error"] == "eligible_rule_decision_not_found"
    assert body["automatic_execution"] == "OFF"
    assert body["reasons"] == ["decision_id_does_not_match_a_pending_selection_from_the_active_rules_session"]
