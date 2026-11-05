from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

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

    def list_bet_signals(self, limit=100, *, session_id=None):
        return self.local.list_bet_signals(limit, session_id=session_id)

    def bet_signals_for_target_round(self, target_round_id):
        return self.local.bet_signals_for_target_round(target_round_id)

    def update_bet_signal_settlement(self, signal_id, target_round_id, settlement):
        return self.local.update_bet_signal_settlement(signal_id, target_round_id, settlement)

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

    def observe_round(self, _event, **_kwargs):
        return deepcopy(self.evaluation)


class FakeMode:
    mode = "SHADOW_REALISTIC"
    transitioning = False


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
        betting_mode=FakeMode(), latest_provider_phase_observation=phase,
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
        betting_mode=FakeMode(), latest_provider_phase_observation=phase,
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
        betting_mode=FakeMode(), latest_provider_phase_observation=phase,
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
        shadow_signal_bridge=ShadowSignalBridge(), betting_mode=FakeMode(),
        latest_provider_phase_observation=phase,
        round_stream_id=STREAM_ID,
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
        return response

    response = asyncio.run(send_commit())

    assert response["ok"] is True
    [signal] = repository.list_bet_signals()
    assert signal["session_id"] == session["session_id"]
    assert signal["round_id"] == source_id
    assert signal["target_round_id"] == target_id
    assert signal["matched_rules"] == [rule_id]
    assert len(dispatches) == 1
    assert dispatches[0][1] == {
        "delivery_mode": "LIVE", "target_already_completed": False,
    }


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
