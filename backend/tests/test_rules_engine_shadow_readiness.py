from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

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

    def connect(self):
        return _Connection()

    def round_distribution_snapshot(self):
        return {"latest": self.boundary}

    def round_distribution_events(self, *_args):
        return []

    def latest_platform_balance_observation(self):
        raise RuntimeError("optional balance observation is unavailable")


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


def _request(repository, engine):
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
                "history_page_state": "OPEN", "betting_target_id": "shared-page",
                "betting_page_state": "OPEN",
            },
            "betting_ui_configuration": {
                "status": "READY", "updated_at": datetime.now(timezone.utc).isoformat(),
            },
            "cashout_initialization": {
                "status": "CASHOUT_VERIFIED", "updated_at": datetime.now(timezone.utc).isoformat(),
                "panel_1_cashout": "CASHOUT_VERIFIED", "panel_2_cashout": "CASHOUT_VERIFIED",
                "panels": [
                    {"status": "CASHOUT_VERIFIED", "auto_bet": "OFF", "auto_cashout": "ON",
                     "multiplier": "2.00", "auto_tab_selected": True},
                    {"status": "CASHOUT_VERIFIED", "auto_bet": "OFF", "auto_cashout": "ON",
                     "multiplier": "2.00", "auto_tab_selected": True},
                ],
            },
        }

    async def body():
        return {"mode": "ALL_RULES_TOGETHER", "rule_ids": ["RECENT_5_H_GE_4"],
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
    assert readiness["automatic_execution"] == "OFF"
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
    assert session["automatic_execution"] == "OFF"
    assert engine.status()["active_session"]["session_id"] == session["session_id"]


def test_fresh_verified_frame_is_not_downgraded_by_unknown_cached_state():
    engine = RulesEngine()
    engine.configure("RECENT_5_H_GE_4", enabled=True)
    request = _request(_Repository(_boundary()), engine)
    original = request.app.state.history_collector.health_status

    def fresh_frame_with_unknown_aggregate(*, persist=False):
        result = original(persist=persist)
        result["status"] = "UNKNOWN"
        return result

    request.app.state.history_collector.health_status = fresh_frame_with_unknown_aggregate

    readiness = api._shared_aviator_readiness(request)
    shadow = api._shadow_readiness(request, engine)

    assert readiness["aviator_page_ready"] == "READY"
    assert readiness["game_iframe_ready"] == "READY"
    assert readiness["history_observer_ready"] == "READY"
    assert readiness["cashout_configured"] == "READY"
    assert readiness["automatic_execution"] == "OFF"
    assert readiness["platform_cashout_acknowledged"] is None
    assert shadow["ready"] is True


def test_stale_cashout_readback_remains_stale_even_with_fresh_frame():
    engine = RulesEngine()
    request = _request(_Repository(_boundary()), engine)
    original = request.app.state.history_collector.health_status

    def stale_cashout(*, persist=False):
        result = original(persist=persist)
        result["cashout_initialization"]["updated_at"] = "2020-01-01T00:00:00+00:00"
        return result

    request.app.state.history_collector.health_status = stale_cashout

    readiness = api._shared_aviator_readiness(request)

    assert readiness["game_iframe_ready"] == "READY"
    assert readiness["cashout_configured"] == "STALE"


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
    assert detail["evaluations"][-1]["rule_evaluations"][0]["status"] == "INVALID_LATE"
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
    assert detail["evaluations"][-1]["rule_evaluations"][0]["status"] == "INVALID_LATE"
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
