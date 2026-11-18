from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import rules_engine as api
from app.rules_engine import RulesEngine


class _Connection:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, _query):
        return self

    def fetchone(self):
        return (1,)


class _Repository:
    database_url = "configured"

    def __init__(self, boundary):
        self.boundary = boundary
        self.snapshot_reads = 0

    def connect(self):
        return _Connection()

    def round_distribution_snapshot(self):
        self.snapshot_reads += 1
        return {"latest": self.boundary}

    def round_distribution_events(self, *_args):
        return []


def _boundary():
    return {
        "event_type": "round.persisted",
        "durability": "COMMITTED",
        "continuity_status": "VERIFIED",
        "stream_id": "stream-1",
        "stream_sequence": 10,
        "round_id": "round-10",
        "round_index": 10,
        "multiplier": 2.3,
        "observed_at": datetime.now(timezone.utc).isoformat(),
    }


def _request(repository, engine, payload=None):
    def history_status(*, persist=False):
        assert persist is False
        return {
            "status": "COLLECTING",
            "collector_process_status": "RUNNING",
            "heartbeat_state": "FRESH",
            "last_observed_round": "round-10",
            "last_observed_at": datetime.now(timezone.utc).isoformat(),
            "browser_connected": True,
            "browser_pages": {
                "browser_count": 1, "context_count": 1, "page_count": 1,
                "aviator_page_count": 1, "aviator_target_id": "shared-page",
                "aviator_page_state": "OPEN", "game_frame_count": 1,
                "game_frame_state": "READY", "history_target_id": "shared-page",
                "history_page_state": "OPEN",
            },
        }

    async def body():
        return payload or {"mode": "ALL_RULES_TOGETHER", "rule_ids": ["RECENT_5_H_GE_4"],
                           "options": {"fixed_stake_rwf": 100}}

    state = SimpleNamespace(
        wp=SimpleNamespace(repository=repository),
        history_collector=SimpleNamespace(health_status=history_status),
        rules_engine=engine,
    )
    return SimpleNamespace(app=SimpleNamespace(state=state), json=body)


def test_shadow_readiness_uses_feed_and_postgres_without_live_platform_requirements():
    engine = RulesEngine()
    engine.configure("RECENT_5_H_GE_4", enabled=True)
    request = _request(_Repository(_boundary()), engine)

    readiness = api._shadow_readiness(request)

    assert readiness["ready"] is True
    assert readiness["mode"] == "SHADOW"
    assert not any("BALANCE" in blocker or "CASHOUT" in blocker or "LIVE_AUTHORIZATION" in blocker
                   for blocker in readiness["blockers"])


def test_shadow_start_succeeds_when_optional_platform_balance_read_fails():
    engine = RulesEngine()
    engine.configure("RECENT_5_H_GE_4", enabled=True)
    request = _request(_Repository(_boundary()), engine)
    original_to_thread = asyncio.to_thread

    async def inline_to_thread(function, *args, **kwargs):
        return function(*args, **kwargs)

    # asyncio.run() shuts down its default executor after the test; running
    # these immediate repository fakes inline avoids waiting for that executor.
    asyncio.to_thread = inline_to_thread
    try:
        session = asyncio.run(api.start_session(request))
    finally:
        asyncio.to_thread = original_to_thread

    assert session["status"] == "RUNNING"
    assert engine.status()["active_session"]["session_id"] == session["session_id"]


def test_shadow_starts_from_fresh_watch_observation_instead_of_old_database_boundary():
    engine = RulesEngine()
    engine.configure("RECENT_5_H_GE_4", enabled=True)
    old = _boundary()
    old["observed_at"] = "2020-01-01T00:00:00+00:00"
    request = _request(_Repository(old), engine)
    now = datetime.now(timezone.utc).isoformat()
    event = dict(_boundary(), event_type="round.observed", durability="PENDING",
                 stream_sequence=None, round_id="watch-round", round_index=11,
                 observed_at=now, delivery_mode="LIVE",
                 source_metadata={"source": "ORBIT_DATA_WATCH_API"})
    request.app.state.latest_watch_observation = event
    request.app.state.history_collector = SimpleNamespace(health_status=lambda **_: {
        "data_source": "ORBIT_DATA_WATCH_API", "status": "COLLECTING",
        "collector_process_status": "RUNNING", "heartbeat_state": "FRESH",
        "orbit_api": {"status": "CONNECTED", "sourceStatus": "HEALTHY",
                      "lastSuccessfulSync": now, "latestReadingAt": now}})
    assert api._shadow_readiness(request)["ready"] is True
    assert request.app.state.wp.repository.snapshot_reads == 0
    original_to_thread = asyncio.to_thread
    async def inline_to_thread(function, *args, **kwargs):
        return function(*args, **kwargs)
    asyncio.to_thread = inline_to_thread
    try:
        session = asyncio.run(api.start_session(request))
    finally:
        asyncio.to_thread = original_to_thread
    assert session["start_boundary"]["roundId"] == "watch-round"
    assert session["context"] == []


def test_status_route_uses_in_memory_watch_boundary_and_reports_server_timing():
    engine = RulesEngine()
    repository = _Repository(_boundary())
    app = FastAPI()
    app.include_router(api.router)
    app.state.rules_engine = engine
    app.state.wp = SimpleNamespace(repository=repository)
    now = datetime.now(timezone.utc).isoformat()
    app.state.latest_watch_observation = dict(
        _boundary(), event_type="round.observed", durability="PENDING",
        stream_sequence=None, delivery_mode="LIVE", source_metadata={"source": "ORBIT_DATA_WATCH_API"})
    app.state.history_collector = SimpleNamespace(health_status=lambda **_: {
        "data_source": "ORBIT_DATA_WATCH_API", "status": "COLLECTING",
        "collector_process_status": "RUNNING", "heartbeat_state": "FRESH",
        "last_observed_round": "round-watch", "last_observed_at": now,
        "orbit_api": {"status": "CONNECTED", "sourceStatus": "HEALTHY",
                      "lastSuccessfulSync": now, "latestReadingAt": now},
    })
    app.state.shadow_signal_bridge_status = {"state": "RUNNING"}

    with TestClient(app) as client:
        response = client.get("/api/rules-engine/status")

    assert response.status_code == 200
    assert "total;dur=" in response.headers["server-timing"]
    assert response.json()["shadow_readiness"]["ready"] is True
    assert repository.snapshot_reads == 0


def test_shadow_start_route_replaces_explicit_integrity_stopped_session():
    engine = RulesEngine()
    engine.configure("RECENT_5_H_GE_4", enabled=True)
    boundary = _boundary()
    old = engine.start_session(
        mode="ALL_RULES_TOGETHER", rule_ids=["RECENT_5_H_GE_4"],
        boundary=boundary, options={"fixed_stake_rwf": 100},
    )
    engine.sessions[old["session_id"]].update({"status": "PAUSED", "stop_reason": "ROUND_INDEX_GAP"})
    request = _request(_Repository(boundary), engine, {
        "mode": "ALL_RULES_TOGETHER", "rule_ids": ["RECENT_5_H_GE_4"],
        "replace_session_id": old["session_id"], "options": {"fixed_stake_rwf": 100},
    })
    original_to_thread = asyncio.to_thread

    async def inline_to_thread(function, *args, **kwargs):
        return function(*args, **kwargs)

    asyncio.to_thread = inline_to_thread
    try:
        new = asyncio.run(api.start_session(request))
    finally:
        asyncio.to_thread = original_to_thread

    assert new["status"] == "RUNNING"
    assert new["replaced_session_id"] == old["session_id"]
    assert engine.session(old["session_id"])["status"] == "MANUALLY_STOPPED"
    assert engine.status()["active_session"]["session_id"] == new["session_id"]


def test_stopping_gap_paused_session_preserves_integrity_reason():
    engine = RulesEngine()
    engine.configure("RECENT_5_H_GE_4", enabled=True)
    session = engine.start_session(
        mode="ALL_RULES_TOGETHER", rule_ids=["RECENT_5_H_GE_4"],
        boundary=_boundary(), prestart_context=[], options={"fixed_stake_rwf": 100},
    )
    session_id = session["session_id"]
    engine.sessions[session_id].update({"status": "PAUSED", "stop_reason": "ROUND_INDEX_GAP"})

    stopped = engine.session_action(session_id, "stop")
    summary = next(row for row in engine.sessions_status() if row["session_id"] == session_id)

    assert stopped["stop_reason"] == "MANUAL_STOP"
    assert stopped["integrity_stop_reason"] == "ROUND_INDEX_GAP"
    assert summary["integrity_stop_reason"] == "ROUND_INDEX_GAP"


def test_start_can_replace_only_an_explicitly_confirmed_integrity_paused_session():
    engine = RulesEngine()
    engine.configure("RECENT_5_H_GE_4", enabled=True)
    original_boundary = _boundary()
    old = engine.start_session(
        mode="ALL_RULES_TOGETHER", rule_ids=["RECENT_5_H_GE_4"],
        boundary=original_boundary, options={"fixed_stake_rwf": 100},
    )
    prior = engine.sessions[old["session_id"]]
    prior.update({"status": "PAUSED", "stop_reason": "ROUND_INDEX_GAP"})
    next_boundary = {**original_boundary, "stream_sequence": 11,
                     "round_id": "round-11", "round_index": 11}

    try:
        engine.start_session(
            mode="ALL_RULES_TOGETHER", rule_ids=["RECENT_5_H_GE_4"],
            boundary=next_boundary, options={"fixed_stake_rwf": 100},
        )
    except ValueError as exc:
        assert str(exc) == "SESSION_ALREADY_ACTIVE_FOR_MODE"
    else:
        raise AssertionError("an integrity-stopped session must require explicit replacement")

    new = engine.start_session(
        mode="ALL_RULES_TOGETHER", rule_ids=["RECENT_5_H_GE_4"],
        boundary=next_boundary, options={"fixed_stake_rwf": 100},
        replace_session_id=old["session_id"],
    )
    archived = engine.session(old["session_id"])

    assert new["status"] == "RUNNING"
    assert new["replaced_session_id"] == old["session_id"]
    assert archived["status"] == "MANUALLY_STOPPED"
    assert archived["selection_status"] == "STOPPED"
    assert archived["integrity_stop_reason"] == "ROUND_INDEX_GAP"
    assert archived["stop_history"][-1]["reason"] == "REPLACED_AFTER_INTEGRITY_STOP"
    assert engine.status()["active_session"]["session_id"] == new["session_id"]


def test_integrity_paused_session_preserves_pending_shadow_outcomes_on_replacement():
    engine = RulesEngine()
    engine.configure("RECENT_5_H_GE_4", enabled=True)
    original_boundary = _boundary()
    old = engine.start_session(
        mode="ALL_RULES_TOGETHER", rule_ids=["RECENT_5_H_GE_4"],
        boundary=original_boundary, options={"fixed_stake_rwf": 100},
    )
    prior = engine.sessions[old["session_id"]]
    prior.update({"status": "PAUSED", "stop_reason": "ROUND_INDEX_GAP"})
    prior["selections"].append({"status": "PENDING"})
    next_boundary = {**original_boundary, "stream_sequence": 11,
                     "round_id": "round-11", "round_index": 11}

    new = engine.start_session(
        mode="ALL_RULES_TOGETHER", rule_ids=["RECENT_5_H_GE_4"],
        boundary=next_boundary, options={"fixed_stake_rwf": 100},
        replace_session_id=old["session_id"],
    )

    archived = engine.session(old["session_id"])
    assert new["status"] == "RUNNING"
    assert archived["status"] == "MANUALLY_STOPPED"
    assert archived["selections"][0]["status"] == "PENDING"
    assert archived["stop_history"][-1]["details"]["preserved_pending_shadow_outcomes"] == 1


def test_gap_paused_old_session_does_not_pin_new_shadow_stream_cursor():
    engine = RulesEngine()
    engine.configure("RECENT_5_H_GE_4", enabled=True)
    boundary = _boundary()
    old = engine.start_session(
        mode="ALL_RULES_TOGETHER", rule_ids=["RECENT_5_H_GE_4"],
        boundary=boundary, prestart_context=[], options={"fixed_stake_rwf": 100},
    )
    engine.session_action(old["session_id"], "stop")
    current = engine.start_session(
        mode="ALL_RULES_TOGETHER", rule_ids=["RECENT_5_H_GE_4"],
        boundary=boundary, prestart_context=[], options={"fixed_stake_rwf": 100},
    )
    prior = engine.sessions[old["session_id"]]
    prior.update({"status": "PAUSED", "stop_reason": "ROUND_INDEX_GAP",
                  "integrity_stop_reason": "ROUND_INDEX_GAP"})
    prior["last_stream_sequence"] = int(boundary["stream_sequence"]) - 10

    assert engine.stream_cursor(boundary["stream_id"], int(boundary["stream_sequence"]) + 5) \
        == int(boundary["stream_sequence"])
    assert engine.status()["active_session"]["session_id"] == current["session_id"]
    assert engine.status()["status"] == "RUNNING"
    assert engine.needs_round_observation() is True
    prior.setdefault("event_records", {})["pending:stream-1:old"] = {"persistence_status": "PENDING"}
    assert engine.stream_cursor(boundary["stream_id"], int(boundary["stream_sequence"]) + 5) \
        == int(boundary["stream_sequence"]) - 10


def test_persisted_target_identity_blocks_a_late_true_rule_selection():
    engine = RulesEngine()
    engine.configure("USER_PLAYING_POLICY", enabled=True)
    session = engine.start_session(
        mode="ALL_RULES_TOGETHER", rule_ids=["USER_PLAYING_POLICY"],
        boundary=_boundary(), prestart_context=[], options={"fixed_stake_rwf": 100},
    )
    engine._persistence = SimpleNamespace(
        round_continuity_for_indices=lambda indices: {
            13: {"round_index": 13} if 13 in indices else None,
        } if 13 in indices else {},
        save_application_state=lambda *_args: None,
    )

    def committed(index: int, sequence: int, multiplier: float):
        return {
            "event_type": "round.persisted", "durability": "COMMITTED",
            "continuity_status": "VERIFIED", "stream_id": "stream-1",
            "stream_sequence": sequence, "round_id": f"round-{index}",
            "round_index": index, "multiplier": multiplier,
        }

    engine.observe_round(committed(11, 11, 1.5))
    result = engine.observe_round(committed(12, 12, 2.5))
    detail = engine.session(session["session_id"])

    assert result["decision"] == "WAIT"
    assert detail["selections"] == []
    assert detail["evaluations"][-1]["target_identity_guard"] == "TARGET_ALREADY_COMPLETED"
    assert detail["evaluations"][-1]["rule_evaluations"][0]["status"] == "TARGET_ALREADY_COMPLETED"
    assert detail["decisions"][-1]["action"] == "NO_SELECT"


def test_pending_collector_target_blocks_a_late_true_rule_selection():
    engine = RulesEngine()
    engine.configure("USER_PLAYING_POLICY", enabled=True)
    session = engine.start_session(
        mode="ALL_RULES_TOGETHER", rule_ids=["USER_PLAYING_POLICY"],
        boundary=_boundary(), prestart_context=[], options={"fixed_stake_rwf": 100},
    )
    engine.observation_inbox = SimpleNamespace(has_pending_round_index=lambda index: index == 13)
    engine._persistence = SimpleNamespace(
        round_continuity_for_indices=lambda _indices: {},
        save_application_state=lambda *_args: None,
    )

    def committed(index: int, sequence: int, multiplier: float):
        return {
            "event_type": "round.persisted", "durability": "COMMITTED",
            "continuity_status": "VERIFIED", "stream_id": "stream-1",
            "stream_sequence": sequence, "round_id": f"round-{index}",
            "round_index": index, "multiplier": multiplier,
        }

    engine.observe_round(committed(11, 11, 1.5))
    result = engine.observe_round(committed(12, 12, 2.5))
    detail = engine.session(session["session_id"])

    assert result["decision"] == "WAIT"
    assert detail["selections"] == []
    assert detail["evaluations"][-1]["target_identity_guard"] == "TARGET_ALREADY_OBSERVED"
    assert detail["evaluations"][-1]["rule_evaluations"][0]["status"] == "TARGET_ALREADY_COMPLETED"
    assert detail["decisions"][-1]["action"] == "NO_SELECT"


def test_committed_event_observed_before_session_start_is_skipped():
    engine = RulesEngine()
    engine.configure("USER_PLAYING_POLICY", enabled=True)
    session = engine.start_session(
        mode="ALL_RULES_TOGETHER", rule_ids=["USER_PLAYING_POLICY"],
        boundary=_boundary(), prestart_context=[], options={"fixed_stake_rwf": 100},
        now="2026-10-14T19:00:10+00:00",
    )
    result = engine.observe_round({
        "event_type": "round.persisted", "durability": "COMMITTED",
        "continuity_status": "VERIFIED", "stream_id": "stream-1",
        "stream_sequence": 11, "round_id": "round-11", "round_index": 11,
        "multiplier": 1.5, "observed_at": "2026-10-14T19:00:09+00:00",
    })
    detail = engine.session(session["session_id"])

    assert result["decision"] == "NO_ACTION"
    assert detail["status"] == "RUNNING"
    assert detail["observed_rounds"] == 0
    assert detail["prestart_skipped_rounds"] == 1
    assert detail["last_stream_sequence"] == 11
    assert detail["last_round_index"] == 11
    assert detail["evaluations"] == []
    assert detail["selections"] == []


def test_shared_no_select_is_persisted_when_rules_evaluate_false():
    engine = RulesEngine()
    engine.configure("USER_PLAYING_POLICY", enabled=True)
    session = engine.start_session(
        mode="ALL_RULES_TOGETHER", rule_ids=["USER_PLAYING_POLICY"],
        boundary=_boundary(), prestart_context=[], options={"fixed_stake_rwf": 100},
    )
    result = engine.observe_round({
        "event_type": "round.persisted", "durability": "COMMITTED",
        "continuity_status": "VERIFIED", "stream_id": "stream-1",
        "stream_sequence": 11, "round_id": "round-11", "round_index": 11,
        "multiplier": 1.5,
    })
    detail = engine.session(session["session_id"])

    assert result["decision"] == "WAIT"
    assert detail["evaluations"][-1]["rule_evaluations"][0]["eligible"] is False
    assert detail["decisions"][-1]["action"] == "NO_SELECT"
    assert detail["decisions"][-1]["source_round"]["roundIndex"] == 11
