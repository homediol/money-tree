from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from starlette.requests import ClientDisconnect
from app.api import signals as signals_api
from app.round_distribution import RoundDistributionHub
from app.database.repository import Repository
from app.rules_engine import RulesEngine
from app.services.shadow_signal_bridge import ShadowSignalBridge
from main import collector_round_persisted


SESSION_ID = "shadow-session-actual"
SELECTION_ID = "selection-actual-7"
SOURCE_ID = "aviator-source-100"
TARGET_ID = "aviator-target-101"
STREAM_ID = "aviator-stream"
RULES = ["RULE_A", "RULE_B"]


class ContractRepository:
    """Use the real signal SQL and a test-only authoritative-round contract."""

    database_url = "postgresql://bridge-test-contract"

    def __init__(self, path):
        self.local = Repository(path)
        self.local.init()
        self.committed_rounds = {SOURCE_ID}
        self.committed_round_identities = {(STREAM_ID, SOURCE_ID, 100)}
        self.committed_indices = set()
        self.latest_event = None
        self.latest_marker = (100, "aviator-boundary-100")

    def has_committed_round_distribution_event(self, round_id):
        return str(round_id) in self.committed_rounds

    def committed_round_distribution_event_by_id(self, round_id):
        if self.latest_event and str(self.latest_event.get("round_id")) == str(round_id):
            return deepcopy(self.latest_event)
        return None

    def has_committed_round_distribution_identity(self, stream_id, round_id, round_index):
        return (str(stream_id), str(round_id), int(round_index)) in self.committed_round_identities

    def has_committed_round_distribution_index(self, stream_id, round_index):
        return (str(stream_id), int(round_index)) in self.committed_indices

    def round_distribution_stream_id(self):
        return STREAM_ID

    def bet_signal_for_target(self, session_id, target_round_id):
        return self.local.bet_signal_for_target(session_id, target_round_id)

    def insert_bet_signal(self, payload):
        return self.local.insert_bet_signal(payload)

    def bet_signal_by_id(self, signal_id):
        return self.local.bet_signal_by_id(signal_id)

    def list_bet_signals(self, limit=100, *, session_id=None):
        return self.local.list_bet_signals(limit, session_id=session_id)

    def bet_signals_for_target_round(self, target_round_id):
        return self.local.bet_signals_for_target_round(target_round_id)

    def update_bet_signal_settlement(self, signal_id, target_round_id, settlement):
        return self.local.update_bet_signal_settlement(signal_id, target_round_id, settlement)

    def behaviour_strategy_settlement_feedback(self, signal_id):
        return self.local.behaviour_strategy_settlement_feedback(signal_id)

    def pending_behaviour_strategy_settlements(self, limit=100):
        return self.local.pending_behaviour_strategy_settlements(limit)

    def mark_behaviour_strategy_settlement_delivered(self, signal_id, *, delivered, error=None):
        return self.local.mark_behaviour_strategy_settlement_delivered(
            signal_id, delivered=delivered, error=error)

    def round_distribution_snapshot(self):
        return {"latest": deepcopy(self.latest_event)}

    def persist_round_distribution_batch(self, observations, _retention, *, stream_id=None):
        events = []
        for observation in observations:
            round_id = observation["round_id"]
            event = source_event(round_id=round_id,
                observation_id=observation["observation_id"],
                round_index=observation["round_index"],
                stream_sequence=observation["round_index"],
                multiplier=observation["multiplier"],
                delivery_mode=observation["delivery_mode"])
            event.update({"event_id": f"aviator-round:{round_id}:persisted",
                "committed_sequence": event["stream_sequence"], "stage": "PERSISTED"})
            self.committed_rounds.add(round_id)
            self.committed_round_identities.add(
                (str(event["stream_id"]), str(round_id), int(event["round_index"])))
            self.committed_indices.add((str(event["stream_id"]), int(event["round_index"])))
            self.latest_marker = (int(event["round_index"]), round_id)
            self.latest_event = deepcopy(event)
            events.append(event)
        return events

    def latest_round_marker(self):
        return self.latest_marker


def source_event(**overrides):
    event = {
        "event_type": "round.persisted", "durability": "COMMITTED",
        "continuity_status": "VERIFIED", "delivery_mode": "LIVE",
        "stream_id": STREAM_ID, "stream_sequence": 100,
        "round_id": SOURCE_ID, "observation_id": SOURCE_ID,
        "round_index": 100, "multiplier": 2.5,
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "stored_at": datetime.now(timezone.utc).isoformat(),
    }
    event.update(overrides)
    return event


def source_ref():
    return {"streamId": STREAM_ID, "roundId": SOURCE_ID, "roundIndex": 100,
            "observationId": SOURCE_ID, "multiplier": 2.5, "outcome": "H"}


def expected_target():
    return {"streamId": STREAM_ID, "roundId": None, "roundIndex": 101,
            "expectedImmediateSuccessorOf": source_ref()}


def true_evaluation(*, persistence_status="COMMITTED"):
    return {"sessions": [{
        "session_id": SESSION_ID,
        "source_round": source_ref(), "target_round": expected_target(),
        "persistence_status": persistence_status,
        "decision": {"action": "SELECT", "reason": "ONE_OR_MORE_RULES_TRUE",
            "selection_id": SELECTION_ID, "true_rule_ids": [RULES[1]],
            "contributor_reasons": [{"rule_id": RULES[1], "reason": "test rule matched"}]},
    }]}


class FakeEngine:
    def __init__(self, evaluation=None):
        self.rule_configuration = {
            rule_id: {"configured": True, "enabled": True} for rule_id in RULES
        }
        self.selection = {
            "selection_id": SELECTION_ID, "decision_id": SELECTION_ID,
            "status": "PENDING", "source_round_id": SOURCE_ID,
            "source_round": source_ref(), "expected_target": expected_target(),
            "contributing_rule_ids": [RULES[1]], "matched_rule_ids": [RULES[1]],
        }
        self.detail = {"session_id": SESSION_ID, "status": "RUNNING",
            "selection_status": "ACTIVE", "rule_ids": list(RULES),
            "selections": [self.selection], "rule_selections": {}}
        self.evaluation = evaluation or true_evaluation()

    def status(self):
        return {"active_session": dict(self.detail)}

    def session(self, session_id):
        return deepcopy(self.detail) if session_id == SESSION_ID else None

    def signal_selection_context(self, session_id, selection_id):
        if session_id != SESSION_ID:
            return None
        selections = list(self.detail.get("adaptive_signal_selections") or [])
        selections.extend(self.detail.get("selections") or [])
        selections.extend(item for rows in (self.detail.get("rule_selections") or {}).values()
                         for item in rows)
        selection = next((item for item in selections
                          if str(item.get("selection_id") or item.get("decision_id") or "")
                          == selection_id), None)
        if selection is None:
            return None
        return {"session_id": SESSION_ID, "mode": self.detail.get("mode", "FIVE_RULE_TRACKER"),
            "status": self.detail.get("status", "RUNNING"),
            "selection_status": self.detail.get("selection_status", "ACTIVE"),
            "last_round_index": 100, "rule_ids": list(self.detail.get("rule_ids") or []),
            "selection": deepcopy(selection)}

    def observe_round(self, _event, **_kwargs):
        return deepcopy(self.evaluation)


def make_app(tmp_path, *, evaluation=None):
    repository = ContractRepository(tmp_path / "shadow-signals.sqlite")
    current = datetime.now(timezone.utc)
    phase = {
        "phase": "BETTING_OPEN", "source_round_id": SOURCE_ID,
        "source_round_index": 100, "canonical_target_round_id": TARGET_ID,
        "target_round_index": 101, "target_identity_verified": True,
        "target_started": False, "observed_at": current.isoformat(),
        "betting_deadline": (current + timedelta(seconds=30)).isoformat(),
    }
    engine = FakeEngine(evaluation)
    app = SimpleNamespace(state=SimpleNamespace(
        wp=SimpleNamespace(repository=repository), rules_engine=engine,
        latest_provider_phase_observation=phase,
        bet_signal_hub=signals_api.BetSignalHub(),
        round_distribution=RoundDistributionHub(),
    ))
    return app, repository, engine


class ActionRequest:
    def __init__(self, app):
        self.app = app
        self.headers = {}
        self.client = SimpleNamespace(host="testclient")
        self.url = SimpleNamespace(scheme="http", hostname="testserver")


def test_bridge_commit_is_read_by_action_endpoint_and_returns_ok(tmp_path):
    app, repository, _engine = make_app(tmp_path)

    async def publish_then_read():
        result = await ShadowSignalBridge().process_committed_round(
            app, source_event(), true_evaluation())
        response = await signals_api.latest_signal_action(ActionRequest(app))
        return result, response

    bridge_result, response = asyncio.run(publish_then_read())

    [persisted] = repository.list_bet_signals(10, session_id=SESSION_ID)
    assert bridge_result["status"] == "PUBLISHED"
    assert bridge_result["published"] == 1
    assert persisted["distribution_status"] == "ACCEPTED"
    assert persisted["decision_id"] == SELECTION_ID
    assert persisted["signal_id"] == f"shadow-{SELECTION_ID}"
    assert response.model_dump() == {"action": "OK", "index": "#101"}
    # The built-in path succeeds without an external Node publisher heartbeat.
    assert not hasattr(app.state, "rules_publisher_heartbeat")


def test_index_only_bridge_action_projects_the_published_signal_decision(tmp_path):
    app, repository, _engine = make_app(tmp_path)
    app.state.latest_provider_phase_observation["target_identity_verified"] = False

    async def publish_then_read():
        result = await ShadowSignalBridge().process_committed_round(
            app, source_event(), true_evaluation())
        response = await signals_api.latest_signal_action(ActionRequest(app))
        return result, response

    bridge_result, response = asyncio.run(publish_then_read())

    [persisted] = repository.list_bet_signals(10, session_id=SESSION_ID)
    assert bridge_result["status"] == "PUBLISHED"
    assert persisted["target_identity_status"] == "INDEX_ONLY_SHADOW"
    assert persisted["target_identity_verified"] is False
    assert persisted["timing_status"] == "UNVERIFIED"
    assert persisted["distribution_status"] == "ACCEPTED"
    assert response.model_dump() == {"action": "OK", "index": "#101"}


def test_committed_true_selection_publishes_exact_signal_payload(tmp_path):
    app, repository, _engine = make_app(tmp_path)
    result = asyncio.run(ShadowSignalBridge().process_committed_round(
        app, source_event(), true_evaluation()))

    assert result["published"] == 1
    [signal] = repository.list_bet_signals()
    assert signal["action"] == "BET"
    assert signal["session_id"] == SESSION_ID
    assert signal["round_id"] == SOURCE_ID
    assert signal["target_round_id"] == TARGET_ID
    assert signal["selected_rules"] == RULES
    assert signal["matched_rules"] == [RULES[1]]
    assert signal["decision_id"] == SELECTION_ID
    assert signal["settlement_status"] == "PENDING"


def test_adaptive_independent_predictions_publish_one_signal_with_all_true_rules(tmp_path):
    app, repository, engine = make_app(tmp_path)
    signal_selection_id = "adaptive-signal-envelope"
    engine.detail["adaptive_rule_selection"] = True
    engine.detail["adaptive_signal_selections"] = [{
        "selection_id": signal_selection_id, "decision_id": signal_selection_id,
        "status": "PENDING", "source_round": source_ref(),
        "expected_target": expected_target(),
        "contributing_rule_ids": list(RULES), "matched_rule_ids": list(RULES),
    }]
    evaluation = {"sessions": [{
        "session_id": SESSION_ID, "source_round": source_ref(),
        "target_round": expected_target(), "persistence_status": "COMMITTED",
        "decision": {"action": "SELECT", "reason": "INDEPENDENT_TRACKER_SELECTIONS",
            "selection_id": signal_selection_id,
            "selection_ids": ["rule-selection-a", "rule-selection-b"],
            "true_rule_ids": list(RULES)},
    }]}

    result = asyncio.run(ShadowSignalBridge().process_committed_round(
        app, source_event(), evaluation))

    [signal] = repository.list_bet_signals()
    assert result["published"] == 1
    assert signal["decision_id"] == signal_selection_id
    assert signal["selected_rules"] == RULES
    assert signal["matched_rules"] == RULES


def test_real_rules_engine_select_automatically_reaches_signal_service(tmp_path):
    repository = ContractRepository(tmp_path / "real-engine.sqlite")
    engine = RulesEngine()
    rule_id = "SOURCE_H_MULT_2_3"
    engine.configure(rule_id, enabled=True)
    now = datetime.now(timezone.utc)
    boundary = {
        "event_type": "round.persisted", "durability": "COMMITTED",
        "continuity_status": "VERIFIED", "stream_id": STREAM_ID,
        "stream_sequence": 100, "round_id": "aviator-boundary-100",
        "observation_id": "aviator-boundary-100", "round_index": 100,
        "multiplier": 1.5, "observed_at": (now - timedelta(seconds=3)).isoformat(),
    }
    session = engine.start_session(mode="FIVE_RULE_TRACKER", rule_ids=[rule_id],
        boundary=boundary, now=(now - timedelta(seconds=2)).isoformat())
    source_id = "aviator-live-source-101"
    target_id = "aviator-live-target-102"
    event = source_event(round_id=source_id, observation_id=source_id,
        round_index=101, stream_sequence=101, multiplier=2.5,
        observed_at=(now - timedelta(seconds=1)).isoformat())
    repository.committed_rounds.add(source_id)
    repository.latest_event = event
    phase = {
        "phase": "BETTING_OPEN", "source_round_id": source_id,
        "source_round_index": 101, "canonical_target_round_id": target_id,
        "target_round_index": 102, "target_identity_verified": True,
        "target_started": False, "observed_at": now.isoformat(),
        "betting_deadline": (now + timedelta(seconds=30)).isoformat(),
    }
    app = SimpleNamespace(state=SimpleNamespace(
        wp=SimpleNamespace(repository=repository), rules_engine=engine,
        latest_provider_phase_observation=phase,
        bet_signal_hub=signals_api.BetSignalHub(),
        round_distribution=RoundDistributionHub(),
    ))

    evaluation = engine.observe_round(event)
    assert evaluation["sessions"][0]["session_id"] == session["session_id"]
    assert evaluation["sessions"][0]["decision"]["action"] == "SELECT"
    async def publish_and_read_action():
        result = await ShadowSignalBridge().process_committed_round(app, event, evaluation)
        action = await signals_api.latest_signal_action(ActionRequest(app))
        return result, action

    result, action = asyncio.run(publish_and_read_action())

    assert result["published"] == 1
    [signal] = repository.list_bet_signals()
    assert signal["session_id"] == session["session_id"]
    assert signal["round_id"] == source_id
    assert signal["target_round_id"] == target_id
    assert signal["matched_rules"] == [rule_id]
    assert signal["distribution_status"] == "ACCEPTED"
    assert action.model_dump() == {"action": "OK", "index": "#102"}


def test_behaviour_signal_becomes_durable_future_only_shadow_selection():
    engine = RulesEngine()
    now = datetime.now(timezone.utc)
    boundary = {"event_type": "round.persisted", "durability": "COMMITTED",
        "continuity_status": "VERIFIED", "stream_id": STREAM_ID, "stream_sequence": 100,
        "round_id": "behaviour-source-100", "observation_id": "behaviour-source-100",
        "round_index": 100, "multiplier": 2.5, "observed_at": now.isoformat()}
    started = engine.start_session(mode="FIVE_RULE_TRACKER", rule_ids=[], boundary=boundary,
        options={"behaviour_detection_enabled": True}, now=now.isoformat())
    signal = {"signal_id": "behaviour-high-test-1", "strategy_id": "ROLLING_HIGH",
        "series_id": "series-test", "part": "PART1", "batch_id": "PART1-001",
        "direction": "HIGH", "trigger_horizon": 5, "policy_version": "high-series-v1",
        "trigger_round_identity": "source-99", "decision_round_identity": "source-100",
        "decision_timestamp": now.isoformat(), "target_stream_id": STREAM_ID,
        "target_round_index": 101}
    source = {"round_id": boundary["round_id"], "round_index": 100,
        "stream_id": STREAM_ID, "observation_id": boundary["observation_id"],
        "multiplier": 2.5, "outcome": "H"}
    first = engine.register_strategy_selection(started["session_id"], signal, source,
                                                     now=now.isoformat())
    duplicate = engine.register_strategy_selection(started["session_id"], signal, source,
                                                         now=now.isoformat())
    assert first["selection_id"] == duplicate["selection_id"]
    assert first["status"] == "PENDING"
    assert first["expected_target"]["roundIndex"] == 101
    conflicting = {**signal, "signal_id": "behaviour-high-test-conflict",
                   "decision_round_identity": "source-100-other"}
    with pytest.raises(ValueError, match="SHADOW_TARGET_CONFLICT"):
        engine.register_strategy_selection(started["session_id"], conflicting, source,
                                           now=now.isoformat())
    result = engine.observe_round({"event_type": "round.persisted", "durability": "COMMITTED",
        "continuity_status": "VERIFIED", "stream_id": STREAM_ID, "stream_sequence": 101,
        "round_id": "behaviour-target-101", "observation_id": "behaviour-target-101",
        "round_index": 101, "multiplier": 2.5, "observed_at": now.isoformat()})
    session = engine.session(started["session_id"])
    settled = next(item for item in session["selections"]
                   if item["selection_id"] == signal["signal_id"])
    assert settled["status"] == "WIN"
    assert settled["actual_target"]["roundId"] == "behaviour-target-101"
    assert session["source_performance"]["behaviour_detection"]["wins"] == 1
    assert session["source_performance"]["behaviour_detection"]["net_profit_rwf"] == 100


def test_behaviour_shadow_settlement_is_durably_outboxed_and_delivered(tmp_path, monkeypatch):
    import json
    import app.services.shadow_signal_bridge as bridge_module
    from app.round_observation_inbox import RoundObservationInbox

    repository = ContractRepository(tmp_path / "behaviour-settlement.sqlite")
    inbox = RoundObservationInbox(tmp_path / "behaviour-shared-inbox.sqlite")
    signal = {"signal_id": "behaviour-settlement-1", "session_id": SESSION_ID,
        "round_id": SOURCE_ID, "target_round_id": f"shadow-index:{STREAM_ID}:101",
        "target_stream_id": STREAM_ID, "target_round_index": 101,
        "source_id": "BEHAVIOUR_DETECTION", "mode": "SHADOW",
        "strategy_id": "ROLLING_HIGH", "series_id": "series-1", "part": "PART1",
        "batch_id": "PART1-001", "configuration_version": 7,
        "settlement_status": "PENDING"}
    assert inbox.enqueue_fast_signal(signal)[0] is True
    assert repository.insert_bet_signal(signal)[0] is True
    settlement = {"status": "COMMITTED", "target_round_id": "aviator-target-101",
        "target_stream_id": STREAM_ID, "target_round_index": 101,
        "actual_multiplier": 2.5, "result": "WIN", "profit_loss_rwf": 100,
        "evidence": {"event_type": "round.persisted", "durability": "COMMITTED",
            "continuity_status": "VERIFIED", "stream_id": STREAM_ID}}
    repository.update_bet_signal_settlement(signal["signal_id"],
        signal["target_round_id"], settlement)
    feedback = repository.behaviour_strategy_settlement_feedback(signal["signal_id"])
    assert feedback["signal_lock_status"] == "DURABLY_LOCKED"
    assert feedback["configuration_version"] == 7
    assert feedback["target_round_identity"] == "winner-predict:aviator-target-101"
    assert feedback["result"] == "WIN"
    # Duplicate SHADOW settlement processing keeps one durable feedback item.
    repository.update_bet_signal_settlement(signal["signal_id"],
        signal["target_round_id"], settlement)
    assert len(repository.pending_behaviour_strategy_settlements()) == 1

    class Response:
        def __enter__(self):
            return self
        def __exit__(self, *_args):
            return False
        def read(self):
            return b'{"status":"RECORDED"}'

    def fake_urlopen(request, timeout):
        assert request.full_url.endswith("/api/v1/behaviour/internal/shadow-settlement")
        assert timeout == 2.0
        assert json.loads(request.data)["signal_id"] == signal["signal_id"]
        return Response()

    monkeypatch.setattr(bridge_module, "urlopen", fake_urlopen)
    monkeypatch.setenv("BEHAVIOUR_LOCAL_API_URL", "http://127.0.0.1:8100")
    async def run_inline(function, *args, **kwargs):
        return function(*args, **kwargs)
    # This test process cannot complete asyncio.to_thread handoffs; keep the
    # delivery contract deterministic while still exercising both callbacks.
    monkeypatch.setattr(asyncio, "to_thread", run_inline)
    app = SimpleNamespace(state=SimpleNamespace(
        wp=SimpleNamespace(repository=repository), round_observation_inbox=inbox))
    assert asyncio.run(bridge_module._deliver_behaviour_settlement(app, feedback)) is True
    assert repository.pending_behaviour_strategy_settlements() == []
    mirrored = inbox.pending_fast_signals()[0]
    assert mirrored["settlement_status"] == "COMMITTED"
    assert mirrored["settlement"]["result"] == "WIN"
    inbox.close()


def test_dashboard_pending_count_excludes_cancelled_behaviour_selections():
    rows = ([{"selection_id": f"win-{i}", "status": "WIN"} for i in range(2)]
        + [{"selection_id": "loss-1", "status": "LOSS"}]
        + [{"selection_id": f"cancel-{i}", "status": "CANCELLED"} for i in range(8)]
        + [{"selection_id": "still-pending", "status": "PENDING"}])
    session = {"session_id": "summary-cancel-test", "mode": "SHADOW",
        "status": "RUNNING", "selection_status": "ACTIVE", "selections": rows,
        "total_wins": 2, "total_losses": 1, "total_cancelled": 8}
    summary = RulesEngine._session_status_summary(session)
    assert summary["results"] == {"WIN": 2, "LOSS": 1, "PENDING": 1}


def test_behaviour_internal_endpoint_accepts_and_retries_same_future_signal(tmp_path):
    repository = ContractRepository(tmp_path / "behaviour-internal.sqlite")
    engine = RulesEngine()
    now = datetime.now(timezone.utc)
    boundary = source_event(round_id="behaviour-boundary-100", observation_id="behaviour-boundary-100",
                           round_index=100, stream_sequence=100, multiplier=1.5)
    session = engine.start_session(mode="FIVE_RULE_TRACKER", rule_ids=[], boundary=boundary,
        options={"behaviour_detection_enabled": True}, now=now.isoformat())
    source = source_event(round_id="behaviour-source-101", observation_id="behaviour-source-101",
                          round_index=101, stream_sequence=101, multiplier=3.0)
    repository.committed_rounds.add(source["round_id"])
    repository.committed_round_identities.add((STREAM_ID, source["round_id"], 101))
    repository.latest_event = deepcopy(source)
    app = FastAPI()
    app.include_router(signals_api.router)
    app.state.wp = SimpleNamespace(repository=repository)
    app.state.rules_engine = engine
    app.state.round_stream_id = STREAM_ID
    app.state.round_observation_inbox = None
    app.state.bet_signal_hub = signals_api.BetSignalHub()
    app.state.round_distribution = RoundDistributionHub()
    body = {"eligibility_id": "behaviour-eligibility-endpoint-test", "strategy_id": "ROLLING_HIGH",
        "series_id": "series-endpoint", "part": "PART1", "batch_id": "PART1-001",
        "trigger_round_identity": "source-100", "decision_round_identity": "source-101",
        "source_round_id": "behaviour-source-101", "direction": "HIGH",
        "decision_timestamp": now.isoformat(), "trigger_horizon": 5,
        "policy_version": "high-series-v1", "verified_data_freshness": {"verified": True},
        "eligibility": "ELIGIBLE", "current_dominance_state": "HIGH_DOMINANT"}
    with TestClient(app, client=("127.0.0.1", 8001)) as client:
        response = client.post("/api/v1/internal/behaviour/strategy-eligibility", json=body)
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "ACCEPTED"
        assert response.json()["signal_lock_status"] == "DURABLY_LOCKED"
        assert response.json()["persistence"] == "COMMITTED"
        assert response.json()["lock_acknowledged_at"]
        assert response.json()["rules_engine_acceptance_to_durable_lock_ms"] >= 0
        stored = repository.list_bet_signals()[0]
        assert stored["source_label"] == "Behaviour Detection / Rolling HIGH"
        assert stored["target_round_index"] == 102
        # Lost acknowledgements retry idempotently even after latest moves on.
        repository.latest_event = source_event(round_id="behaviour-target-102",
            observation_id="behaviour-target-102", round_index=102,
            stream_sequence=102, multiplier=1.2)
        repository.committed_rounds.add("behaviour-target-102")
        repository.committed_indices.add((STREAM_ID, 102))
        retried = client.post("/api/v1/internal/behaviour/strategy-eligibility", json=body)
        assert retried.status_code == 200
        assert retried.json()["status"] == "ALREADY_ACCEPTED"
        assert len(repository.list_bet_signals()) == 1
        latest_low = source_event(round_id="behaviour-low-103",
            observation_id="behaviour-low-103", round_index=103,
            stream_sequence=103, multiplier=1.3)
        repository.committed_rounds.add("behaviour-low-103")
        repository.committed_round_identities.add((STREAM_ID, "behaviour-low-103", 103))
        repository.latest_event = deepcopy(latest_low)
        ineligible = {"eligibility_id": "behaviour-eligibility-low-103",
            "strategy_id": "ROLLING_HIGH", "decision_round_identity": "source-103",
            "source_round_id": "behaviour-low-103", "direction": "HIGH",
            "decision_timestamp": now.isoformat(), "current_dominance_state": "LOW_DOMINANT",
            "eligibility": "INELIGIBLE", "eligibility_reason": "PART2_PAUSED_LOW_DOMINANT",
            "policy_version": "high-series-v1", "verified_data_freshness": {"verified": True}}
        recorded = client.post("/api/v1/internal/behaviour/strategy-eligibility", json=ineligible)
        assert recorded.status_code == 200, recorded.text
        assert recorded.json()["status"] == "ELIGIBILITY_RECORDED"
        assert len(repository.list_bet_signals()) == 1
        status = engine.status(include_sessions=False)["active_session"]["behaviour_strategy"]
        assert status["eligible"] is False
        assert status["reason"] == "PART2_PAUSED_LOW_DOMINANT"


def test_behaviour_part2_high_result_is_accepted_when_rolling_dominance_is_low(tmp_path):
    repository = ContractRepository(tmp_path / "behaviour-part2-low-dominance.sqlite")
    engine = RulesEngine()
    now = datetime.now(timezone.utc)
    boundary = source_event(round_id="part2-boundary-200", observation_id="part2-boundary-200",
                           round_index=200, stream_sequence=200, multiplier=1.5)
    engine.start_session(mode="FIVE_RULE_TRACKER", rule_ids=[], boundary=boundary,
        options={"behaviour_detection_enabled": True}, now=now.isoformat())
    # A verified individual HIGH result resumed Part 2. The rolling context can
    # still be LOW_DOMINANT; it must not veto the next-round eligibility.
    source = source_event(round_id="part2-resume-high-201", observation_id="part2-resume-high-201",
                          round_index=201, stream_sequence=201, multiplier=2.1)
    repository.committed_rounds.add(source["round_id"])
    repository.committed_round_identities.add((STREAM_ID, source["round_id"], 201))
    repository.latest_event = deepcopy(source)
    app = FastAPI()
    app.include_router(signals_api.router)
    app.state.wp = SimpleNamespace(repository=repository)
    app.state.rules_engine = engine
    app.state.round_stream_id = STREAM_ID
    app.state.round_observation_inbox = None
    app.state.bet_signal_hub = signals_api.BetSignalHub()
    app.state.round_distribution = RoundDistributionHub()
    body = {"eligibility_id": "behaviour-part2-low-dominance-201", "strategy_id": "ROLLING_HIGH",
        "series_id": "series-part2", "part": "PART2", "batch_id": "PART2-003",
        "trigger_round_identity": "part1-r5", "decision_round_identity": "source-201",
        "source_round_id": source["round_id"], "direction": "HIGH",
        "decision_timestamp": now.isoformat(), "trigger_horizon": 5,
        "policy_version": "high-series-v1", "verified_data_freshness": {"verified": True},
        "eligibility": "ELIGIBLE", "eligibility_reason": "PART2_LAST_VERIFIED_RESULT_HIGH",
        "current_dominance_state": "LOW_DOMINANT"}
    with TestClient(app, client=("127.0.0.1", 8001)) as client:
        response = client.post("/api/v1/internal/behaviour/strategy-eligibility", json=body)
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "ACCEPTED"
    signal = repository.list_bet_signals()[0]
    assert signal["target_round_index"] == 202
    assert signal["round_id"] == source["round_id"]


def test_behaviour_eligibility_uses_latest_pending_api_round_without_database_lookup(tmp_path):
    repository = ContractRepository(tmp_path / "behaviour-api-observation.sqlite")
    engine = RulesEngine()
    now = datetime.now(timezone.utc)
    boundary = source_event(round_id="behaviour-api-boundary-100",
        observation_id="behaviour-api-boundary-100", round_index=100,
        stream_sequence=100, multiplier=1.5)
    engine.start_session(mode="FIVE_RULE_TRACKER", rule_ids=[], boundary=boundary,
        options={"behaviour_detection_enabled": True}, now=now.isoformat())
    class SlowPersistence:
        def save_rules_engine_snapshot(self, *_args):
            raise AssertionError("ineligible live readouts must not block on Rules Engine snapshots")
    engine._persistence = SlowPersistence()
    observed = source_event(round_id="behaviour-api-source-101",
        observation_id="behaviour-api-source-101", round_index=101,
        stream_sequence=101, multiplier=1.3)
    observed.update({"event_type": "round.observed", "durability": "PENDING",
                    "delivery_mode": "LIVE"})
    repository.committed_round_distribution_event_by_id = lambda *_: (_ for _ in ()).throw(
        AssertionError("fresh API source must not wait for the shared database"))
    repository.round_distribution_snapshot = lambda *_: (_ for _ in ()).throw(
        AssertionError("fresh API source must not read the committed snapshot"))

    class PendingInbox:
        def latest_pending_verified_round(self):
            return deepcopy(observed)

        def latest_verified_round_event(self):
            return deepcopy(observed)

    app = FastAPI()
    app.include_router(signals_api.router)
    app.state.wp = SimpleNamespace(repository=repository)
    app.state.rules_engine = engine
    app.state.round_stream_id = STREAM_ID
    app.state.round_observation_inbox = PendingInbox()
    app.state.bet_signal_hub = signals_api.BetSignalHub()
    app.state.round_distribution = RoundDistributionHub()
    body = {"eligibility_id": "behaviour-api-ineligible-test", "strategy_id": "ROLLING_HIGH",
        "decision_round_identity": "winner-predict:behaviour-api-source-101",
        "source_round_id": "behaviour-api-source-101", "direction": "HIGH",
        "decision_timestamp": now.isoformat(), "current_dominance_state": "LOW_DOMINANT",
        "eligibility": "INELIGIBLE", "eligibility_reason": "PART2_PAUSED_LOW_DOMINANT",
        "policy_version": "high-series-v1", "verified_data_freshness": {"verified": True}}
    with TestClient(app, client=("127.0.0.1", 8001)) as client:
        response = client.post("/api/v1/internal/behaviour/strategy-eligibility", json=body)
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "ELIGIBILITY_RECORDED"


def test_behaviour_fresh_watch_signal_uses_fast_outbox_and_one_target(tmp_path):
    repository = ContractRepository(tmp_path / "behaviour-fast-signal.sqlite")
    engine = RulesEngine()
    now = datetime.now(timezone.utc)
    boundary = source_event(round_id="behaviour-fast-boundary-100",
        observation_id="behaviour-fast-boundary-100", round_index=100,
        stream_sequence=100, multiplier=1.5)
    session = engine.start_session(mode="FIVE_RULE_TRACKER", rule_ids=[], boundary=boundary,
        options={"behaviour_detection_enabled": True}, now=now.isoformat())
    observed = source_event(round_id="behaviour-fast-source-101",
        observation_id="behaviour-fast-source-101", round_index=101,
        stream_sequence=101, multiplier=3.0)
    observed.update({"event_type": "round.observed", "durability": "PENDING",
        "continuity_status": "VERIFIED", "delivery_mode": "LIVE",
        "source_metadata": {"source": "ORBIT_DATA_WATCH_API"}})

    class FastInbox:
        def __init__(self):
            self.saved = {}

        def latest_pending_verified_round(self):
            return deepcopy(observed)

        def latest_verified_round_event(self):
            return deepcopy(observed)

        def has_pending_round_index(self, index):
            return False

        def enqueue_fast_signal(self, signal):
            key = (signal["session_id"], signal["target_round_id"])
            if key in self.saved:
                return False, deepcopy(self.saved[key])
            self.saved[key] = deepcopy(signal)
            return True, deepcopy(signal)

    inbox = FastInbox()
    app = FastAPI()
    app.include_router(signals_api.router)
    app.state.wp = SimpleNamespace(repository=repository)
    app.state.rules_engine = engine
    app.state.round_stream_id = STREAM_ID
    committed = {**observed, "event_type": "round.persisted", "durability": "COMMITTED"}
    app.state.latest_verified_live_observation = deepcopy(committed)
    app.state.latest_live_observed_envelope = deepcopy(observed)
    app.state.latest_watch_observation = deepcopy(observed)
    app.state.round_observation_inbox = inbox
    app.state.bet_signal_hub = signals_api.BetSignalHub()
    app.state.round_distribution = RoundDistributionHub()
    body = {"eligibility_id": "behaviour-fast-eligibility-101", "strategy_id": "ROLLING_HIGH",
        "series_id": "series-fast", "part": "PART1", "batch_id": "PART1-001",
        "trigger_round_identity": "source-100", "decision_round_identity": "source-101",
        "source_round_id": observed["round_id"], "direction": "HIGH",
        "decision_timestamp": now.isoformat(), "trigger_horizon": 5,
        "policy_version": "high-series-v1", "verified_data_freshness": {"verified": True},
        "eligibility": "ELIGIBLE", "current_dominance_state": "HIGH_DOMINANT"}
    with TestClient(app, client=("127.0.0.1", 8001)) as client:
        response = client.post("/api/v1/internal/behaviour/strategy-eligibility", json=body)
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "ACCEPTED"
        assert response.json()["signal_lock_status"] == "DURABLY_LOCKED"
        assert response.json()["persistence"] == "PENDING_POSTGRES"
        assert response.json()["signal_id"]
        assert response.json()["lock_acknowledged_at"]
        retry = client.post("/api/v1/internal/behaviour/strategy-eligibility", json=body)
        assert retry.status_code == 200, retry.text
        assert retry.json()["status"] == "ALREADY_ACCEPTED"
    assert len(inbox.saved) == 1
    saved = next(iter(inbox.saved.values()))
    assert saved["target_round_index"] == 102
    assert saved["target_round_id"] == f"shadow-index:{STREAM_ID}:102"


def test_behaviour_eligibility_client_disconnect_is_not_reported_as_server_crash(monkeypatch):
    monkeypatch.delenv("BEHAVIOUR_SHADOW_SHARED_SECRET", raising=False)

    class DisconnectingRequest:
        client = SimpleNamespace(host="127.0.0.1")
        headers = {}

        async def json(self):
            raise ClientDisconnect()

    response = asyncio.run(signals_api.accept_behaviour_strategy_eligibility(DisconnectingRequest()))
    assert response.status_code == 499


def test_recovered_commit_retries_saved_select_only_when_target_is_still_verified_open(tmp_path):
    repository = ContractRepository(tmp_path / "recovered-selection.sqlite")
    engine = RulesEngine()
    rule_id = "SOURCE_H_MULT_2_3"
    engine.configure(rule_id, enabled=True)
    now = datetime.now(timezone.utc)
    boundary = {
        "event_type": "round.persisted", "durability": "COMMITTED",
        "continuity_status": "VERIFIED", "stream_id": STREAM_ID,
        "stream_sequence": 100, "round_id": "aviator-recovery-boundary-100",
        "observation_id": "aviator-recovery-boundary-100", "round_index": 100,
        "multiplier": 1.5, "observed_at": (now - timedelta(seconds=3)).isoformat(),
    }
    session = engine.start_session(mode="FIVE_RULE_TRACKER", rule_ids=[rule_id],
        boundary=boundary, now=(now - timedelta(seconds=2)).isoformat())
    source_id = "aviator-recovered-source-101"
    target_id = "aviator-recovered-target-102"
    pending_event = source_event(event_type="round.observed", durability="PENDING",
        stream_sequence=None, committed_sequence=None, round_id=source_id,
        observation_id=source_id, round_index=101, multiplier=2.5,
        observed_at=(now - timedelta(seconds=1)).isoformat())
    pending_evaluation = engine.observe_round(pending_event)
    assert pending_evaluation["sessions"][0]["decision"]["action"] == "SELECT"

    committed_event = source_event(delivery_mode="RECOVERED", round_id=source_id,
        observation_id=source_id, round_index=101, stream_sequence=101, multiplier=2.5,
        observed_at=pending_event["observed_at"])
    repository.committed_rounds.add(source_id)
    phase = {"phase": "BETTING_OPEN", "source_round_id": source_id,
        "source_round_index": 101, "canonical_target_round_id": target_id,
        "target_round_index": 102, "target_identity_verified": True,
        "target_started": False, "observed_at": now.isoformat(),
        "betting_deadline": (now + timedelta(seconds=30)).isoformat()}
    app = SimpleNamespace(state=SimpleNamespace(
        wp=SimpleNamespace(repository=repository), rules_engine=engine,
        latest_provider_phase_observation=phase,
        bet_signal_hub=signals_api.BetSignalHub(),
    ))

    committed_evaluation = engine.observe_round(committed_event, target_already_completed=True)
    assert committed_evaluation["sessions"][0]["decision"]["action"] == "SELECT"
    result = asyncio.run(ShadowSignalBridge().process_committed_round(
        app, committed_event, committed_evaluation))

    assert result["published"] == 1
    [signal] = repository.list_bet_signals()
    assert signal["session_id"] == session["session_id"]
    assert signal["round_id"] == source_id
    assert signal["target_round_id"] == target_id
    assert signal["matched_rules"] == [rule_id]


def test_committed_collector_handoff_invokes_the_bridge(tmp_path):
    repository = ContractRepository(tmp_path / "collector-bridge.sqlite")
    engine = RulesEngine()
    rule_id = "SOURCE_H_MULT_2_3"
    engine.configure(rule_id, enabled=True)
    now = datetime.now(timezone.utc)
    boundary = {
        "event_type": "round.persisted", "durability": "COMMITTED",
        "continuity_status": "VERIFIED", "stream_id": STREAM_ID,
        "stream_sequence": 100, "round_id": "aviator-boundary-100",
        "observation_id": "aviator-boundary-100", "round_index": 100,
        "multiplier": 1.5, "observed_at": (now - timedelta(seconds=3)).isoformat(),
    }
    session = engine.start_session(mode="FIVE_RULE_TRACKER", rule_ids=[rule_id],
        boundary=boundary, now=(now - timedelta(seconds=2)).isoformat())
    source_id = "aviator-handoff-source-101"
    target_id = "aviator-handoff-target-102"
    phase = {"phase": "BETTING_OPEN", "source_round_id": source_id,
        "source_round_index": 101, "canonical_target_round_id": target_id,
        "target_round_index": 102, "target_identity_verified": True,
        "target_started": False, "observed_at": now.isoformat(),
        "betting_deadline": (now + timedelta(seconds=30)).isoformat()}
    app = SimpleNamespace(state=SimpleNamespace(
        wp=SimpleNamespace(repository=repository), rules_engine=engine,
        shadow_signal_bridge=ShadowSignalBridge(),
        latest_provider_phase_observation=phase,
        round_stream_id=STREAM_ID,
        round_event_commit_lock=asyncio.Lock(),
        round_distribution=RoundDistributionHub(), round_observation_inbox=None,
    ))
    dispatches = []

    async def dispatch_evaluation(evaluation, **kwargs):
        dispatches.append((evaluation, kwargs))
        return {"status": "NO_SELECTION", "dispatched": []}

    app.state.decision_dispatcher = SimpleNamespace(
        dispatch_evaluation=dispatch_evaluation)
    body = {"source": "WINNER_CANONICAL_POSTGRESQL_COMMIT", "rounds": [{
        "round_id": source_id, "observation_id": source_id,
        "multiplier": 2.5, "observed_at": (now - timedelta(seconds=1)).isoformat(),
        "round_index": 101, "continuity_verified": True,
        "delivery_mode": "LIVE",
    }]}

    async def send_commit():
        async def json_body():
            return body
        request = SimpleNamespace(app=app, json=json_body)
        response = await collector_round_persisted(request)
        await app.state.round_event_queue.join()
        await app.state.committed_bridge_queue.join()
        return response

    response = asyncio.run(send_commit())

    assert response["ok"] is True
    [signal] = repository.list_bet_signals()
    assert signal["session_id"] == session["session_id"]
    assert signal["round_id"] == source_id
    assert signal["target_round_id"] == target_id
    assert signal["matched_rules"] == [rule_id]
    assert dispatches == [], "the committed bridge publishes through the durable signal API, not a second direct dispatch"


def test_false_or_uncommitted_decision_does_not_publish(tmp_path):
    app, repository, _engine = make_app(tmp_path)
    false_result = {"sessions": [{"session_id": SESSION_ID,
        "source_round": source_ref(), "target_round": expected_target(),
        "persistence_status": "COMMITTED",
        "decision": {"action": "WAIT", "true_rule_ids": []}}]}
    result = asyncio.run(ShadowSignalBridge().process_committed_round(
        app, source_event(), false_result))
    assert result["published"] == 0
    assert repository.list_bet_signals() == []

    pending = asyncio.run(ShadowSignalBridge().process_committed_round(
        app, source_event(event_type="round.observed", durability="PENDING"), true_evaluation()))
    assert pending["status"] == "IGNORED_UNCOMMITTED_EVENT"
    assert repository.list_bet_signals() == []


def test_invalid_or_pending_select_is_rejected(tmp_path):
    app, repository, _engine = make_app(tmp_path)
    pending_result = asyncio.run(ShadowSignalBridge().process_committed_round(
        app, source_event(), true_evaluation(persistence_status="PENDING")))
    assert pending_result["published"] == 0
    assert repository.list_bet_signals() == []

    # A true rule omitted from the active selected rules fails the same
    # validator used by POST /api/v1/signals.
    invalid = true_evaluation()
    invalid["sessions"][0]["decision"]["true_rule_ids"] = ["RULE_NOT_SELECTED"]
    result = asyncio.run(ShadowSignalBridge().process_committed_round(
        app, source_event(), invalid))
    assert result["published"] == 0
    assert repository.list_bet_signals() == []
    assert app.state.shadow_signal_bridge_status["state"] == "REJECTED_INVALID_DECISION"


def test_duplicate_decision_and_restart_replay_are_idempotent(tmp_path):
    app, repository, engine = make_app(tmp_path)
    event = source_event()
    repository.latest_event = event
    bridge = ShadowSignalBridge()
    first = asyncio.run(bridge.process_committed_round(app, event, true_evaluation()))
    assert first["published"] == 1

    # A fresh app hub replays the saved engine decision after restart.
    restarted = SimpleNamespace(state=SimpleNamespace(**dict(app.state.__dict__)))
    restarted.state.bet_signal_hub = signals_api.BetSignalHub()
    restarted.state.rules_engine = engine
    second_bridge = ShadowSignalBridge()
    second = asyncio.run(second_bridge.recover_for_open_phase(
        restarted, restarted.state.latest_provider_phase_observation))
    assert second["status"] == "ALREADY_PUBLISHED"
    assert second["duplicates"] == 1
    assert len(repository.list_bet_signals()) == 1


def test_transient_bridge_persistence_failure_can_retry_safely(tmp_path):
    app, repository, _engine = make_app(tmp_path)
    insert = repository.insert_bet_signal
    attempts = 0

    def fail_twice(payload):
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise OSError("temporary database interruption")
        return insert(payload)

    repository.insert_bet_signal = fail_twice
    result = asyncio.run(ShadowSignalBridge().process_committed_round(
        app, source_event(), true_evaluation()))
    assert result["published"] == 1
    assert attempts == 3
    assert len(repository.list_bet_signals()) == 1


def test_signal_waits_for_exact_committed_target_before_recording_result(tmp_path):
    app, repository, engine = make_app(tmp_path)
    bridge = ShadowSignalBridge()
    asyncio.run(bridge.process_committed_round(app, source_event(), true_evaluation()))

    target = {"streamId": STREAM_ID, "roundId": TARGET_ID, "roundIndex": 101,
              "observationId": TARGET_ID, "multiplier": 4.25, "outcome": "H"}
    engine.selection.update({"status": "WIN", "result": "WIN", "target_round_id": TARGET_ID,
        "target_persistence_status": "PENDING", "actual_target": target,
        "settlement_evidence": {"event_type": "round.observed", "durability": "PENDING",
            "continuity_status": "VERIFIED", "stream_id": STREAM_ID}, "settled_at": "pending-time"})
    observed_result = asyncio.run(bridge.process_committed_round(app,
        source_event(event_type="round.observed", durability="PENDING", round_id=TARGET_ID,
                     round_index=101), {"sessions": []}))
    assert observed_result["status"] == "IGNORED_UNCOMMITTED_EVENT"
    [pending_signal] = repository.list_bet_signals()
    assert pending_signal["settlement_status"] == "PENDING"

    engine.selection.update({"target_persistence_status": "COMMITTED",
        "settlement_evidence": {"event_type": "round.persisted", "durability": "COMMITTED",
            "continuity_status": "VERIFIED", "stream_id": STREAM_ID,
            "target_round_id": TARGET_ID, "target_round_index": 101,
            "target_multiplier": 4.25, "settled_at": "committed-time"}})
    repository.committed_rounds.add(TARGET_ID)
    committed_result = asyncio.run(bridge.process_committed_round(app,
        source_event(round_id=TARGET_ID, round_index=101, stream_sequence=101, multiplier=4.25),
        {"sessions": []}))
    assert committed_result["settled"] == 1
    [settled_signal] = repository.list_bet_signals()
    assert settled_signal["settlement_status"] == "COMMITTED"
    assert settled_signal["settlement"]["target_round_id"] == TARGET_ID
    assert settled_signal["settlement"]["actual_multiplier"] == 4.25
    assert settled_signal["settlement"]["result"] == "WIN"
