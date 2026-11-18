from types import SimpleNamespace

import pytest

from app.rules_engine import RulesEngine


RULE_ID = "PATTERN_LH"


def _round(index, *, observed=False):
    return {
        "stream_id": "automatic-stream", "round_id": f"round-{index}",
        "observation_id": f"round-{index}", "round_index": index,
        "stream_sequence": index, "multiplier": 1.5,
        "event_type": "round.observed" if observed else "round.persisted",
        "durability": "PENDING" if observed else "COMMITTED",
        "continuity_status": "VERIFIED",
    }


def _observer(engine, rule_id=RULE_ID, *, wins, losses, latest_index=10, stream_id="automatic-stream"):
    engine._performance_observers[rule_id] = {
        "last_event": {**_round(latest_index), "stream_id": stream_id},
        "history": [{"stream_id": stream_id, "completed_window": True,
                     "settled": wins + losses, "wins": wins, "losses": losses,
                     "net": wins - losses, "last_round_index": latest_index,
                     "performance_configuration_key": engine._performance_configuration_key(rule_id),
                     "interruption_reason": None}],
        "engine": SimpleNamespace(sessions={}),
    }


def test_automatic_rule_choice_changes_with_settled_evidence_and_can_choose_none():
    engine = RulesEngine()
    engine.configure(RULE_ID, enabled=True)
    session = engine.start_session(mode="FIVE_RULE_TRACKER", rule_ids=[],
        boundary=_round(10), options={"automatic_rule_selection": True})
    assert session["active_rule_ids"] == []
    assert RULE_ID in session["rule_ids"]
    assert engine.status()["rules_enabled"] == 1

    _observer(engine, wins=3, losses=0)
    engine.observe_round(_round(11, observed=True))
    first = engine.session(session["session_id"])
    assert first["active_rule_ids"] == []
    pattern = next(row for row in first["automatic_rule_choice"]["candidates"]
                   if row["rule_id"] == RULE_ID)
    assert pattern["reason"] == "INSUFFICIENT_POSITIVE_EVIDENCE"
    assert first["event_records"]["pending:automatic-stream:round-11"]["decision"]["action"] == "NO_ACTION"

    _observer(engine, wins=9, losses=1, latest_index=11)
    engine.observe_round(_round(12, observed=True))
    qualified = engine.session(session["session_id"])
    assert qualified["active_rule_ids"] == [RULE_ID]
    assert qualified["automatic_rule_choice"]["reason"] == "POSITIVE_SETTLED_EDGE"

    _observer(engine, wins=4, losses=6, latest_index=12)
    engine.observe_round(_round(13, observed=True))
    losing = engine.session(session["session_id"])
    assert losing["active_rule_ids"] == []
    assert losing["automatic_rule_choice"]["reason"] == "NO_QUALIFIED_RULE"


def test_automatic_and_adaptive_previews_return_candidate_details_without_starting_sessions():
    engine = RulesEngine()
    source = _round(10)
    history = [_round(index) for index in range(6, 11)]

    automatic = engine.preview_rule_selection(mode="ALL_RULES_TOGETHER",
        selection_mode="automatic", source=source, history=history)
    adaptive = engine.preview_rule_selection(mode="ALL_RULES_TOGETHER",
        selection_mode="adaptive", source=source, history=history)

    assert automatic["status"] == "READY"
    assert automatic["source_round"]["round_index"] == 10
    assert automatic["candidates"]
    assert adaptive["status"] == "READY"
    assert adaptive["source_round"]["round_index"] == 10
    assert adaptive["selected_rule_ids"]
    assert adaptive["candidates"]
    assert engine.sessions == {}


def test_automatic_rule_choice_requires_current_stream_and_enabled_rule():
    engine = RulesEngine()
    engine.configure(RULE_ID, enabled=True)
    session = engine.start_session(mode="FIVE_RULE_TRACKER", rule_ids=[],
        boundary=_round(10), options={"automatic_rule_selection": True})
    _observer(engine, wins=9, losses=1, stream_id="old-stream")
    engine.observe_round(_round(11, observed=True))
    assert engine.session(session["session_id"])["active_rule_ids"] == []
    engine.configure(RULE_ID, enabled=False)
    _observer(engine, wins=9, losses=1, latest_index=11)
    engine.observe_round(_round(12, observed=True))
    assert engine.session(session["session_id"])["active_rule_ids"] == []


def test_automatic_rule_choice_expires_old_positive_windows():
    engine = RulesEngine()
    engine.configure(RULE_ID, enabled=True)
    session = engine.start_session(mode="FIVE_RULE_TRACKER", rule_ids=[],
        boundary=_round(100), options={"automatic_rule_selection": True})
    _observer(engine, wins=9, losses=1, latest_index=100)
    engine._performance_observers[RULE_ID]["history"][0]["last_round_index"] = 40
    engine.observe_round(_round(101, observed=True))
    choice = engine.session(session["session_id"])["automatic_rule_choice"]
    assert choice["rule_ids"] == []
    assert next(row for row in choice["candidates"] if row["rule_id"] == RULE_ID)["reason"] == "NO_RECENT_SETTLED_RESULTS"


def test_automatic_rule_choice_does_not_reuse_pre_gap_wins():
    engine = RulesEngine()
    engine.configure(RULE_ID, enabled=True)
    session = engine.start_session(mode="FIVE_RULE_TRACKER", rule_ids=[],
        boundary=_round(100), options={"automatic_rule_selection": True})
    _observer(engine, wins=9, losses=1, latest_index=100)
    engine._performance_observers[RULE_ID]["segment_start_round_index"] = 101
    engine.observe_round(_round(101, observed=True))
    choice = engine.session(session["session_id"])["automatic_rule_choice"]
    assert choice["rule_ids"] == []


def test_automatic_rule_choice_and_segment_survive_state_restore():
    engine = RulesEngine()
    engine.configure(RULE_ID, enabled=True)
    session = engine.start_session(mode="FIVE_RULE_TRACKER", rule_ids=[],
        boundary=_round(10), options={"automatic_rule_selection": True})
    observer = engine._performance_observer(RULE_ID)
    observer["last_event"] = _round(10)
    observer["history"] = [{"stream_id": "automatic-stream", "completed_window": True,
                            "settled": 10, "wins": 9, "losses": 1, "net": 8,
                            "performance_configuration_key": engine._performance_configuration_key(RULE_ID),
                            "last_round_index": 10, "interruption_reason": None}]
    observer["segment_start_round_index"] = 8
    engine.observe_round(_round(11, observed=True))
    restored = RulesEngine()
    restored._restore(engine._snapshot())
    current = restored.session(session["session_id"])
    assert current["automatic_rule_selection"] is True
    assert current["active_rule_ids"] == [RULE_ID]
    assert restored._performance_observers[RULE_ID]["segment_start_round_index"] == 8


def test_performance_observer_moves_segment_boundary_on_verified_gap():
    engine = RulesEngine()
    assert engine.observe_rule_performance_round(_round(10)) is True
    assert engine.observe_rule_performance_round(_round(12)) is True
    assert engine._performance_observers[RULE_ID]["segment_start_round_index"] == 12


def test_changed_policy_parameters_invalidate_old_performance_and_session_choice():
    from app.rules_engine import START_CHECK_POLICY_ID

    engine = RulesEngine()
    engine.configure(START_CHECK_POLICY_ID, enabled=True,
                     parameters={"profit_target_rwf": 400})
    session = engine.start_session(mode="START_CHECK", rule_ids=[],
        boundary=_round(10), options={"automatic_rule_selection": True})
    rule_id = START_CHECK_POLICY_ID
    old_key = engine._performance_configuration_key(rule_id)
    engine.configure(rule_id, enabled=True, parameters={"profit_target_rwf": 800})
    assert engine._performance_configuration_key(rule_id) != old_key
    engine._performance_observers[rule_id] = {
        "last_event": _round(10),
        "history": [{"stream_id": "automatic-stream", "completed_window": True,
                     "settled": 10, "wins": 9, "losses": 1, "net": 8,
                     "last_round_index": 10, "performance_configuration_key": old_key}],
        "engine": SimpleNamespace(sessions={}),
    }
    engine.observe_round(_round(11, observed=True))
    choice = engine.session(session["session_id"])["automatic_rule_choice"]
    assert choice["rule_ids"] == []
    assert choice["candidates"][0]["reason"] == "SESSION_RULE_CONFIGURATION_CHANGED"


def test_performance_observer_restarts_window_after_parameter_change():
    from app.rules_engine import START_CHECK_POLICY_ID

    engine = RulesEngine()
    engine.configure(START_CHECK_POLICY_ID, enabled=True,
                     parameters={"profit_target_rwf": 400})
    engine.observe_rule_performance_round(_round(10))
    old_key = engine._performance_configuration_key(START_CHECK_POLICY_ID)
    engine.configure(START_CHECK_POLICY_ID, enabled=True,
                     parameters={"profit_target_rwf": 800})
    engine.observe_rule_performance_round(_round(11))
    observer = engine._performance_observers[START_CHECK_POLICY_ID]
    assert observer["history"][-1]["interruption_reason"] == "CONFIGURATION_CHANGED"
    assert observer["history"][-1]["performance_configuration_key"] == old_key
    assert all(row["performance_configuration_key"] == engine._performance_configuration_key(START_CHECK_POLICY_ID)
               for row in observer["engine"].sessions.values())


def test_performance_observer_live_ledger_is_bounded_for_reports():
    from app.rules_engine import RulesEngine

    session = {
        "configuration_snapshot": {"performance_window_size": 100},
        "evaluations": [{"source_round": {"roundIndex": index},
                         "evaluated_at": str(index), "state_snapshot": {"large": True},
                         "rule_evaluations": [{"rule_id": "RULE", "eligible": True}]}
                        for index in range(1000)],
        "decisions": list(range(1000)),
        "selections": list(range(1000)),
        "rule_selections": {"RULE": list(range(1000))},
        "event_records": {str(index): {"persistence_status": "COMMITTED"}
                          for index in range(300)},
    }

    assert RulesEngine._trim_performance_observer_ledger(session) is True
    assert len(session["evaluations"]) == 105
    assert session["evaluations"][-1]["source_round"]["roundIndex"] == 999
    assert "state_snapshot" not in session["evaluations"][-1]
    assert session["decisions"] == []
    assert session["selections"] == list(range(800, 1000))
    assert session["rule_selections"]["RULE"] == list(range(800, 1000))
    assert session["event_records"] == {}


def test_automatic_rule_selection_rejects_fixed_participants():
    engine = RulesEngine()
    with pytest.raises(ValueError, match="AUTOMATIC_RULE_SELECTION_REQUIRES_NO_FIXED_RULES"):
        engine.start_session(mode="FIVE_RULE_TRACKER", rule_ids=[RULE_ID],
            boundary=_round(10), options={"automatic_rule_selection": True})




def test_manual_rule_selection_keeps_operator_participants():
    engine = RulesEngine()
    engine.configure(RULE_ID, enabled=True)
    session = engine.start_session(mode="FIVE_RULE_TRACKER", rule_ids=[RULE_ID],
        boundary=_round(10), options={"automatic_rule_selection": False})
    assert session["automatic_rule_selection"] is False
    assert session["rule_ids"] == [RULE_ID]
    engine.observe_round(_round(11, observed=True))
    current = engine.session(session["session_id"])
    assert current["rule_ids"] == [RULE_ID]
    assert current["automatic_rule_choice"] is None
