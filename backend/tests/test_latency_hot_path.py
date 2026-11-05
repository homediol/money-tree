from __future__ import annotations

import asyncio
from concurrent.futures import Future
from copy import deepcopy
from threading import Event, Thread
import time

from app.api.signals import BetSignalHub
from app.database.repository import Repository
from app.rules_engine import RulesEngine, START_CHECK_POLICY_ID


class InlineExecutor:
    """Keep this SQLite contract test deterministic without spawning a thread."""

    def submit(self, function, /, *args, **kwargs):
        future = Future()
        try:
            future.set_result(function(*args, **kwargs))
        except BaseException as exc:
            future.set_exception(exc)
        return future


def test_live_round_context_is_newest_hundred_and_keeps_full_history(tmp_path):
    repository = Repository(tmp_path / "latency-history.sqlite3")
    repository.init()
    repository.upsert_rounds([
        {"round_id": f"round-{index}", "round_index": index,
         "multiplier": 1.25 if index % 2 else 2.5,
         "timestamp": f"2026-11-03T00:{index // 60:02d}:{index % 60:02d}+00:00",
         "target": int(index % 2 == 0)}
        for index in range(1, 251)
    ])

    recent = repository.load_recent_round_context(100)
    summary = repository.round_history_status()

    assert len(recent) == 100
    assert [row["round_index"] for row in recent] == list(range(151, 251))
    assert summary["count"] == 250
    assert summary["latest"]["round_id"] == "round-250"
    assert repository.load_recent_rounds(2)[-1]["round_index"] == 250
    assert repository.round_history_stats()["total"] == 250
    with repository.connect() as connection:
        indexes = {row[1] for row in connection.execute("PRAGMA index_list(bet_signals)").fetchall()}
    assert "bet_signals_target_round_created_idx" in indexes


def test_durable_signal_acceptance_is_atomic_idempotent_and_acknowledged_after_commit(tmp_path):
    repository = Repository(tmp_path / "latency-signals.sqlite3")
    repository.init()
    signal = {
        "signal_id": "signal-501", "session_id": "session-1", "round_id": "source-500",
        "target_round_id": "target-501", "action": "BET", "target_round_index": 501,
        "distribution_status": "ACCEPTED",
    }
    first_hub = BetSignalHub()
    accepted_at = []
    accepted, stored = asyncio.run(first_hub.accept(
        dict(signal), repository, executor=InlineExecutor(),
        on_durable_accept=accepted_at.append))

    assert accepted is True
    assert stored["signal_id"] == signal["signal_id"]
    assert len(accepted_at) == 1
    assert repository.bet_signal_by_id(signal["signal_id"])["signal_id"] == signal["signal_id"]

    restarted_hub = BetSignalHub()
    duplicate_callbacks = []
    accepted_again, stored_again = asyncio.run(restarted_hub.accept(
        {**signal, "signal_id": "retry-with-new-id"}, repository,
        executor=InlineExecutor(),
        on_durable_accept=duplicate_callbacks.append))

    assert accepted_again is False
    assert stored_again["signal_id"] == signal["signal_id"]
    assert duplicate_callbacks == []
    assert len(repository.list_bet_signals(10)) == 1


def test_bounded_live_cache_preserves_enabled_start_check_ordered_decisions():
    class UntrimmedRulesEngine(RulesEngine):
        @staticmethod
        def _trim_committed_event_records(_session, *, keep=100):
            return None

    boundary = {
        "stream_id": "start-check-cache-stream", "round_id": "start-check-round-1",
        "observation_id": "start-check-round-1", "round_index": 1,
        "stream_sequence": 1, "multiplier": 1.5,
        "event_type": "round.persisted", "durability": "COMMITTED",
        "continuity_status": "VERIFIED",
    }
    bounded = RulesEngine()
    reference = UntrimmedRulesEngine()
    for engine in (bounded, reference):
        engine.configure(START_CHECK_POLICY_ID, enabled=True,
                         parameters={"window_size": 1000, "profit_target_enabled": False})
    bounded_session = bounded.start_session(
        mode="START_CHECK", rule_ids=[START_CHECK_POLICY_ID], boundary=boundary,
        options={"window_size": 1000, "profit_target_enabled": False})
    reference_session = reference.start_session(
        mode="START_CHECK", rule_ids=[START_CHECK_POLICY_ID], boundary=boundary,
        options={"window_size": 1000, "profit_target_enabled": False})

    def rule_semantics(rows):
        return [{key: row.get(key) for key in (
            "rule_id", "rule_version", "source_round", "target_round", "result",
            "eligible", "status", "reason", "state_snapshot")}
            for row in rows]

    def decision_semantics(decision):
        target = decision.get("expected_target") or decision.get("target_round") or {}
        result = {key: decision.get(key) for key in (
            "action", "reason", "true_rule_ids", "matched_rules")}
        result["target_round_index"] = (target.get("roundIndex")
                                        if isinstance(target, dict) else None)
        return result

    for round_index in range(2, 152):
        multiplier = (1.25, 2.5, 1.5, 3.2)[round_index % 4]
        identity = f"start-check-round-{round_index}"
        observed = {
            **boundary, "round_id": identity, "observation_id": identity,
            "round_index": round_index, "stream_sequence": None,
            "multiplier": multiplier, "event_type": "round.observed",
            "durability": "PENDING",
        }
        committed = {
            **observed, "stream_sequence": round_index,
            "event_type": "round.persisted", "durability": "COMMITTED",
        }
        for event in (observed, committed):
            bounded_result = bounded.observe_round(event)
            reference_result = reference.observe_round(event)
            assert bounded_result["decision"] == reference_result["decision"]
            bounded_record = bounded_result["sessions"][0]
            reference_record = reference_result["sessions"][0]
            assert decision_semantics(bounded_record["decision"]) == decision_semantics(reference_record["decision"])
            assert rule_semantics(bounded_record["rule_evaluations"]) == rule_semantics(reference_record["rule_evaluations"])

    bounded_state = bounded.session(bounded_session["session_id"])
    reference_state = reference.session(reference_session["session_id"])
    assert bounded_state["last_round_index"] == reference_state["last_round_index"] == 151
    assert bounded_state["engines"] == reference_state["engines"]
    assert [decision_semantics(row) for row in bounded_state["decisions"]] == [
        decision_semantics(row) for row in reference_state["decisions"]]
    assert len(bounded_state["event_records"]) == 100
    assert len(reference_state["event_records"]) == 150

    # A persisted replay older than the stream cursor is ignored even after its
    # cache record is trimmed, preserving full ordered processing semantics.
    replay = bounded.observe_round({
        **boundary, "round_id": "start-check-round-2", "observation_id": "start-check-round-2",
        "round_index": 2, "stream_sequence": 2, "multiplier": 1.5,
    })
    assert replay["sessions"] == []
    assert bounded.session(bounded_session["session_id"])["last_round_index"] == 151


def test_deferred_persistence_keeps_start_check_decisions_and_state_unchanged(tmp_path):
    inline = Repository(tmp_path / "immediate-rules.sqlite3")
    deferred = Repository(tmp_path / "deferred-rules.sqlite3")
    inline.init()
    deferred.init()
    engines = [RulesEngine(persistence=inline), RulesEngine(persistence=deferred)]
    boundary = {
        "stream_id": "deferred-persist-stream", "round_id": "deferred-persist-1",
        "observation_id": "deferred-persist-1", "round_index": 1,
        "stream_sequence": 1, "multiplier": 1.5,
        "event_type": "round.persisted", "durability": "COMMITTED",
        "continuity_status": "VERIFIED",
    }
    sessions = []
    for engine in engines:
        engine.configure(START_CHECK_POLICY_ID, enabled=True,
                         parameters={"window_size": 1000, "profit_target_enabled": False})
        sessions.append(engine.start_session(mode="START_CHECK", rule_ids=[START_CHECK_POLICY_ID],
            boundary=boundary, options={"window_size": 1000, "profit_target_enabled": False}))

    def semantic(result):
        rows = result.get("sessions") or []
        return [{
            "decision": {key: (row.get("decision") or {}).get(key) for key in (
                "action", "reason", "true_rule_ids")},
            "rule_evaluations": [{key: rule.get(key) for key in (
                "rule_id", "status", "eligible", "reason", "state_snapshot", "target_round")}
                for rule in row.get("rule_evaluations", [])],
        } for row in rows]

    multipliers = [1.3, 2.6, 1.8, 3.1, 1.4, 1.1, 2.4, 3.0]
    for round_index, multiplier in enumerate(multipliers, start=2):
        identity = f"deferred-persist-{round_index}"
        event = {**boundary, "round_id": identity, "observation_id": identity,
                 "round_index": round_index, "stream_sequence": None,
                 "multiplier": multiplier, "event_type": "round.observed", "durability": "PENDING"}
        expected = engines[0].observe_round(event)
        actual = engines[1].observe_round(event, defer_persistence=True)
        session_ids = set(actual.pop("_deferred_persistence_session_ids"))
        assert semantic(actual) == semantic(expected)
        assert session_ids
        timings = engines[1].persist_current_snapshot(session_ids=session_ids)
        assert timings["database_persistence_ms"] >= 0

    expected_session = engines[0].session(sessions[0]["session_id"])
    actual_session = engines[1].session(sessions[1]["session_id"])
    assert actual_session["engines"] == expected_session["engines"]
    assert actual_session["observed_rounds"] == expected_session["observed_rounds"]
    selection_projection = lambda rows: [{key: row.get(key) for key in (
        "source_round", "expected_target", "actual_target", "status", "result",
        "settlement_status", "multiplier", "outcome", "persistence_status")}
        for row in rows]
    assert selection_projection(actual_session["selections"]) == selection_projection(
        expected_session["selections"])
    restored = RulesEngine(persistence=deferred)
    restored_session = restored.session(sessions[1]["session_id"])
    assert restored_session["engines"] == expected_session["engines"]
    assert selection_projection(restored_session["selections"]) == selection_projection(
        expected_session["selections"])
    inline.close()
    deferred.close()


def test_priority_observation_delivery_matches_ordered_two_stage_rules_results():
    boundary = {
        "stream_id": "priority-order-stream", "round_id": "priority-order-1",
        "observation_id": "priority-order-1", "round_index": 1,
        "stream_sequence": 1, "multiplier": 1.5,
        "event_type": "round.persisted", "durability": "COMMITTED",
        "continuity_status": "VERIFIED",
    }
    ordered = RulesEngine()
    priority = RulesEngine()
    for engine in (ordered, priority):
        engine.configure(START_CHECK_POLICY_ID, enabled=True,
                         parameters={"window_size": 1000, "profit_target_enabled": False})
        engine.start_session(mode="START_CHECK", rule_ids=[START_CHECK_POLICY_ID],
                             boundary=boundary,
                             options={"window_size": 1000, "profit_target_enabled": False})

    observations = {}
    for index, multiplier in ((2, 1.2), (3, 2.7), (4, 3.4)):
        identity = f"priority-order-{index}"
        observed = {**boundary, "round_id": identity, "observation_id": identity,
                    "round_index": index, "stream_sequence": None,
                    "multiplier": multiplier, "event_type": "round.observed",
                    "durability": "PENDING"}
        committed = {**observed, "stream_sequence": index,
                     "event_type": "round.persisted", "durability": "COMMITTED"}
        observations[index] = (observed, committed)

    expected_decisions = {}
    for index in (2, 3, 4):
        expected_decisions[index] = ordered.observe_round(observations[index][0])["decision"]
        ordered.observe_round(observations[index][1])

    actual_decisions = {}
    for index in (2, 3, 4):
        actual_decisions[index] = priority.observe_round(observations[index][0])["decision"]
    for index in (2, 3, 4):
        priority.observe_round(observations[index][1])

    assert actual_decisions == expected_decisions
    expected_session = next(iter(ordered.sessions.values()))
    actual_session = next(iter(priority.sessions.values()))
    for key in ("engines", "observed_rounds", "status", "selection_status",
                "last_round_index", "last_round_id", "last_stream_sequence"):
        assert actual_session[key] == expected_session[key]
    def selection_projection(rows):
        return [{key: row.get(key) for key in (
            "source_round", "expected_target", "actual_target", "status", "result",
            "settlement_status", "multiplier", "outcome", "persistence_status")}
            for row in rows]
    assert selection_projection(actual_session["selections"]) == selection_projection(
        expected_session["selections"])


def test_replayed_pending_selection_keeps_stable_decision_identity():
    boundary = {
        "stream_id": "stable-signal-stream", "round_id": "stable-signal-1",
        "observation_id": "stable-signal-1", "round_index": 1,
        "stream_sequence": 1, "multiplier": 1.5,
        "event_type": "round.persisted", "durability": "COMMITTED",
        "continuity_status": "VERIFIED",
    }
    original = RulesEngine()
    original.configure(START_CHECK_POLICY_ID, enabled=True,
                       parameters={"window_size": 1000, "profit_target_enabled": False})
    original.start_session(mode="START_CHECK", rule_ids=[START_CHECK_POLICY_ID],
                           boundary=boundary,
                           options={"window_size": 1000, "profit_target_enabled": False})
    before_event = deepcopy(original._snapshot())
    replayed = RulesEngine()
    replayed._restore(before_event)

    for index, multiplier in ((2, 1.2), (3, 2.7)):
        event = {**boundary, "round_id": f"stable-signal-{index}",
                 "observation_id": f"stable-signal-{index}", "round_index": index,
                 "stream_sequence": None, "multiplier": multiplier,
                 "event_type": "round.observed", "durability": "PENDING"}
        first = original.observe_round(event, defer_persistence=True)
        second = replayed.observe_round(event, defer_persistence=True)
        assert first["sessions"][0]["decision"].get("selection_id") == (
            second["sessions"][0]["decision"].get("selection_id"))


def test_slow_snapshot_commit_does_not_hold_rules_lock(tmp_path):
    started = Event()
    release = Event()

    class SlowPersistence:
        def save_rules_engine_snapshot(self, _payload, _updated_at):
            started.set()
            assert release.wait(timeout=2)

    engine = RulesEngine()
    engine._persistence = SlowPersistence()
    worker = Thread(target=engine.persist_current_snapshot, daemon=True)
    worker.start()
    assert started.wait(timeout=1)
    check_started = time.perf_counter()
    engine.signal_validation_session()
    elapsed = time.perf_counter() - check_started
    release.set()
    worker.join(timeout=2)
    assert not worker.is_alive()
    assert elapsed < 0.1


def test_stale_deferred_snapshot_cannot_overwrite_newer_rule_configuration(tmp_path):
    repository = Repository(tmp_path / "snapshot-generation.sqlite3")
    repository.init()
    engine = RulesEngine(persistence=repository)
    engine.configure(START_CHECK_POLICY_ID, enabled=True,
                     parameters={"window_size": 1000, "profit_target_enabled": False})
    stale_snapshot = deepcopy(engine._snapshot())
    stale_generation = stale_snapshot["_rules_engine_generation"]
    engine.configure(START_CHECK_POLICY_ID, enabled=False,
                     parameters={"window_size": 1000, "profit_target_enabled": False})

    repository.save_rules_engine_snapshot(stale_snapshot, "2026-11-03T00:00:00+00:00")
    restored = RulesEngine(persistence=repository)

    assert restored._persistence_generation > stale_generation
    assert restored.rule_configuration[START_CHECK_POLICY_ID]["enabled"] is False
    repository.close()
