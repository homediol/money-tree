import json
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.database.repository import Repository
from app.rules_engine import (
    COMBINED_POLICY_ID,
    LEGACY_POLICY_ID,
    RESEARCH_RULES,
    STATE_KEY,
    START_CHECK_POLICY_ID,
    RulesEngine,
    classify_outcome,
    _fresh_engine,
)


FIXTURE_PATH = Path(__file__).parent / "fixtures" / "combined-recorded-50.json"
FIXTURE = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
SOURCE_ROOT = Path("/home/diolo/aviator-shadow-experiment-node")
NODE_BIN = Path("/home/diolo/.nvm/versions/node/v24.14.0/bin/node")
RESEARCH_IDS = [row["rule_id"] for row in RESEARCH_RULES]


def source_boundary():
    boundary = deepcopy(FIXTURE["boundary"])
    context_row = next(row for row in boundary["prestart_context"]
                       if row["round_index"] == boundary["round_index"])
    boundary.update(context_row)
    boundary.update({"stream_sequence": FIXTURE["boundary"]["stream_sequence"],
                     "event_type": "round.persisted", "durability": "COMMITTED",
                     "continuity_status": "VERIFIED"})
    return boundary


def observed(row):
    return {**deepcopy(row), "schema_version": "1.0", "event_type": "round.observed",
            "durability": "PENDING", "continuity_status": "VERIFIED", "delivery_mode": "LIVE",
            "stream_sequence": None}


def committed(row):
    return {**deepcopy(row), "schema_version": "1.0", "event_type": "round.persisted",
            "event_id": f"aviator-round:{row['round_id']}:persisted", "durability": "COMMITTED",
            "continuity_status": "VERIFIED", "delivery_mode": "LIVE",
            "stored_at": row["database_committed_at"]}


def new_session(mode, rule_ids, *, context=None, options=None, persistence=None):
    engine = RulesEngine(persistence=persistence)
    for rule_id in rule_ids:
        engine.configure(rule_id, enabled=True)
    session = engine.start_session(mode=mode, rule_ids=rule_ids, boundary=source_boundary(),
        prestart_context=FIXTURE["boundary"]["prestart_context"] if context is None else context,
        options=options or {}, now=FIXTURE["boundary"]["captured_at"])
    return engine, session


def feed(engine, row, *, now=None, late=False):
    result = engine.observe_round(observed(row), now=now or row["receiver_received_at"],
                                  target_already_completed=late)
    engine.observe_round(committed(row), now=row["database_committed_at"])
    return result


def evaluations_for(result, session_id):
    session_record = next((row for row in result.get("sessions", []) if row.get("session_id") == session_id), None)
    return session_record.get("rule_evaluations", []) if session_record else []


def test_complete_registry_preserves_all_source_paths_and_frozen_rule_semantics():
    engine = RulesEngine()
    registry = engine.registry()
    ids = [row["rule_id"] for row in registry["rules"]]
    assert ids == [START_CHECK_POLICY_ID, LEGACY_POLICY_ID, *RESEARCH_IDS, COMBINED_POLICY_ID]
    assert {row["id"] for row in registry["modes"]} == {
        "START_CHECK", "LEGACY_SEPARATE_TRACKER", "FIVE_RULE_TRACKER", "ALL_RULES_TOGETHER",
    }
    by_id = {row["rule_id"]: row for row in registry["rules"]}
    assert by_id["RECENT_5_H_GE_4"]["condition"] == {
        "window": 5, "outcome": "H", "operator": ">=", "count": 4,
        "includes_source": True, "latest_source_must_be_h": False,
    }
    assert by_id["SOURCE_H_MULT_2_3"]["condition"]["minimum_inclusive"] == 2.0
    assert by_id["SOURCE_H_MULT_2_3"]["condition"]["maximum_exclusive"] == 3.0
    assert by_id["AFTER_HH_CONTINUE_MAX_3"]["condition"]["maximum_selections"] == 3
    assert by_id[COMBINED_POLICY_ID]["initial_state"]["rule_state"] == "WAIT_LOW"
    assert by_id[COMBINED_POLICY_ID]["initial_state"]["mode"] == "STANDARD"
    assert classify_outcome(2.0) == "H"
    assert classify_outcome(1.999999) == "L"
    assert classify_outcome(None) is None


@pytest.mark.parametrize("mode,rule_id", [
    ("START_CHECK", START_CHECK_POLICY_ID),
    ("LEGACY_SEPARATE_TRACKER", LEGACY_POLICY_ID),
    *[("FIVE_RULE_TRACKER", rule_id) for rule_id in RESEARCH_IDS],
    ("ALL_RULES_TOGETHER", COMBINED_POLICY_ID),
])
def test_every_registered_policy_can_request_independently_on_saved_rounds(mode, rule_id):
    engine, session = new_session(mode, [rule_id])
    assert session["manifest"]["manifest_version"] == "WINNER_RULES_MANIFEST_V1"
    assert session["manifest"]["rules"][0]["rule_id"] == rule_id
    assert len(session["manifest"]["sha256"]) == 64
    found_true = False
    for row in FIXTURE["rounds"]:
        output = feed(engine, row)
        records = evaluations_for(output, session["session_id"])
        found_true |= any(item["rule_id"] == rule_id and item["eligible"] is True for item in records)
    assert found_true, rule_id
    assert engine.execution_authorized() is False


def test_combined_true_and_false_proposals_create_one_shared_selection_and_result():
    enabled = ["RECENT_5_H_GE_4", "PATTERN_LH", "PATTERN_HH", "SOURCE_H_MULT_2_3",
               "AFTER_HH_CONTINUE_MAX_3", COMBINED_POLICY_ID]
    engine, session = new_session("ALL_RULES_TOGETHER", enabled,
        options={"window_size": 50})
    saw_mixed = False
    saw_multi_true = False
    for row in FIXTURE["rounds"]:
        output = feed(engine, row)
        current = engine.session(session["session_id"])
        entry = current["evaluations"][-1]
        proposal_values = [item["eligible"] for item in entry["rule_evaluations"]]
        if True in proposal_values and False in proposal_values:
            saw_mixed |= entry["decision"]["action"] == "SELECT"
        if sum(value is True for value in proposal_values) > 1:
            saw_multi_true |= entry["decision"]["action"] == "SELECT"
        assert len({(item["expected_target"]["streamId"], item["expected_target"]["roundIndex"])
                    for item in current["selections"]}) == len(current["selections"])
        assert len(current["selections"]) <= current["observed_rounds"]
    current = engine.session(session["session_id"])
    assert saw_mixed
    assert saw_multi_true
    assert all(item["status"] in {"PENDING", "WIN", "LOSS"} for item in current["selections"])
    for item in current["selections"]:
        assert item["expected_target"]["roundIndex"] == item["source_round"]["roundIndex"] + 1
        assert item["selected_at"]
        assert item["contributors"]
    assert current["total_wins"] + current["total_losses"] + sum(
        item["status"] == "PENDING" for item in current["selections"]) == len(current["selections"])


def test_unavailable_rule_does_not_veto_ready_true_and_wait_resumes_on_later_true():
    last_context = [deepcopy(FIXTURE["boundary"]["prestart_context"][-1])]
    engine, session = new_session("ALL_RULES_TOGETHER", ["RECENT_5_H_GE_4", "SOURCE_H_MULT_2_3"],
                                  context=last_context)
    first = feed(engine, FIXTURE["rounds"][0])
    current = engine.session(session["session_id"])["evaluations"][-1]
    by_id = {item["rule_id"]: item for item in current["rule_evaluations"]}
    assert by_id["RECENT_5_H_GE_4"]["eligible"] is None
    assert by_id["RECENT_5_H_GE_4"]["status"] == "UNAVAILABLE_HISTORY"
    assert by_id["SOURCE_H_MULT_2_3"]["eligible"] is True
    assert current["decision"]["action"] == "SELECT"

    pattern, session2 = new_session("ALL_RULES_TOGETHER", ["PATTERN_LH"])
    saw_wait = False
    resumed = False
    for row in FIXTURE["rounds"]:
        feed(pattern, row)
        record = pattern.session(session2["session_id"])["evaluations"][-1]
        if record["decision"]["action"] == "WAIT":
            saw_wait = True
        if saw_wait and record["decision"]["action"] == "SELECT":
            resumed = True
            break
    assert resumed


def test_aggregator_rejects_string_booleans_and_mismatched_targets():
    base = {"enabled": True, "eligible": True, "reason": "matched"}
    mismatch = RulesEngine.aggregate([
        {**base, "rule_id": "A", "expectedTargetRef": {"streamId": "s", "roundIndex": 5}},
        {**base, "rule_id": "B", "expectedTargetRef": {"streamId": "s", "roundIndex": 6}},
    ])
    string_boolean = RulesEngine.aggregate([
        {"rule_id": "A", "enabled": True, "eligible": "false", "reason": "not matched",
         "expectedTargetRef": {"streamId": "s", "roundIndex": 5}},
    ])
    assert mismatch["decision"] == "BLOCKED"
    assert mismatch["reason"] == "TARGET_MISMATCH"
    assert string_boolean["decision"] == "NO_ACTION"
    assert string_boolean["reason"] == "INVALID_RULE_EVALUATION"


def test_invalid_and_late_rounds_are_reported_without_backdated_selection():
    engine, session = new_session("ALL_RULES_TOGETHER", ["SOURCE_H_MULT_2_3"])
    invalid = engine.observe_round({**observed(FIXTURE["rounds"][0]), "continuity_status": "UNVERIFIED"})
    assert invalid["decision"] == "BLOCKED"
    evaluation = invalid["sessions"][0]["rule_evaluations"][0]
    assert evaluation["eligible"] is None
    assert evaluation["status"] == "UNAVAILABLE_CONTINUITY"

    late, late_session = new_session("ALL_RULES_TOGETHER", ["SOURCE_H_MULT_2_3"])
    result = late.observe_round(observed(FIXTURE["rounds"][0]), target_already_completed=True)
    detail = late.session(late_session["session_id"])
    assert detail["selections"] == []
    assert detail["evaluations"][-1]["decision"]["reason"] == "TARGET_ALREADY_COMPLETED"
    assert detail["evaluations"][-1]["rule_evaluations"][0]["status"] == "INVALID_LATE"
    assert result["decision"] == "WAIT"


def test_duplicate_observed_commit_and_restart_keep_one_selection_and_provenance(tmp_path):
    repository = Repository(tmp_path / "rules.sqlite3")
    repository.init()
    engine, session = new_session("FIVE_RULE_TRACKER", ["SOURCE_H_MULT_2_3"], persistence=repository)
    row = FIXTURE["rounds"][0]
    first = engine.observe_round(observed(row), now=row["receiver_received_at"])
    selection_count = len(engine.session(session["session_id"])["selections"])
    duplicate = engine.observe_round(observed(row), now=row["receiver_received_at"])
    assert len(engine.session(session["session_id"])["selections"]) == selection_count == 1
    assert duplicate["sessions"][0]["decision"]["action"] == first["sessions"][0]["decision"]["action"]

    restarted = RulesEngine(persistence=repository)
    persisted = restarted.observe_round(committed(row), now=row["database_committed_at"])
    recovered = restarted.session(session["session_id"])
    assert len(recovered["selections"]) == 1
    assert recovered["last_stream_sequence"] == row["stream_sequence"]
    assert recovered["manifest"]["sha256"] == session["manifest"]["sha256"]
    assert recovered["evaluations"][0]["evaluated_at"] == row["receiver_received_at"]
    assert recovered["evaluations"][0]["persistence_status"] == "COMMITTED"
    assert persisted["sessions"][0]["decision"]["action"] == first["sessions"][0]["decision"]["action"]
    with repository.connect() as conn:
        saved_evaluations = conn.execute("""SELECT rule_id,eligible,status FROM rules_engine_evaluations
            WHERE session_id=?""", (session["session_id"],)).fetchall()
        config_version = conn.execute("""SELECT configuration FROM rules_engine_configuration_versions
            WHERE configuration_version=?""", (session["configuration_version"],)).fetchone()
    assert len(saved_evaluations) == 1
    assert saved_evaluations[0]["rule_id"] == "SOURCE_H_MULT_2_3"
    assert bool(saved_evaluations[0]["eligible"]) is True
    assert config_version is not None


@pytest.mark.parametrize("limit_field,limit_value,expected_reason,expected_wins,expected_losses", [
    ("win_limit_count", 1, "WIN_LIMIT_REACHED", 1, 1),
    ("loss_limit_count", 2, "LOSS_LIMIT_REACHED", 2, 2),
])
def test_total_result_limits_are_not_consecutive_and_window_keeps_observing(
        limit_field, limit_value, expected_reason, expected_wins, expected_losses):
    enabled_key = "win_limit_enabled" if limit_field.startswith("win") else "loss_limit_enabled"
    options = {"window_size": 50,
               "win_limit_enabled": False, "loss_limit_enabled": False,
               enabled_key: True, limit_field: limit_value}
    engine, session = new_session("ALL_RULES_TOGETHER", ["SOURCE_H_MULT_2_3"], options=options)
    for row in FIXTURE["rounds"]:
        feed(engine, row)
    detail = engine.session(session["session_id"])
    assert detail["selection_stop_reason"] == expected_reason
    assert detail["total_wins"] == expected_wins
    assert detail["total_losses"] == expected_losses
    assert detail["observed_rounds"] == 50
    assert detail["status"] == "COMPLETED"
    assert all(selection["expected_target"]["roundIndex"] <= source_boundary()["round_index"] + 50
               for selection in detail["selections"])


def test_no_rule_is_no_action_and_round_50_never_creates_target_51():
    empty = RulesEngine()
    assert empty.evaluate()["decision"] == "NO_ACTION"
    engine, session = new_session("ALL_RULES_TOGETHER", ["SOURCE_H_MULT_2_3"])
    for row in FIXTURE["rounds"]:
        feed(engine, row)
    detail = engine.session(session["session_id"])
    assert detail["observed_rounds"] == 50
    assert all(selection["expected_target"]["roundIndex"] <= 27617
               for selection in detail["selections"])


def test_zero_rule_session_is_no_action_and_one_session_stop_does_not_affect_peers():
    empty, empty_session = new_session("ALL_RULES_TOGETHER", [])
    empty_result = feed(empty, FIXTURE["rounds"][0])
    assert empty_result["sessions"][0]["decision"]["action"] == "NO_ACTION"
    assert empty.session(empty_session["session_id"])["selections"] == []

    shared = RulesEngine()
    shared.configure("SOURCE_H_MULT_2_3", enabled=True)
    boundary = source_boundary()
    context = FIXTURE["boundary"]["prestart_context"]
    combined = shared.start_session(mode="ALL_RULES_TOGETHER", rule_ids=["SOURCE_H_MULT_2_3"],
        boundary=boundary, prestart_context=context, options={"window_size": 50})
    tracker = shared.start_session(mode="FIVE_RULE_TRACKER", rule_ids=["SOURCE_H_MULT_2_3"],
        boundary=boundary, prestart_context=context, options={"window_size": 50})
    initial = feed(shared, FIXTURE["rounds"][0])
    assert initial["decision"] == "SESSION_SCOPED"
    assert len(shared.session(combined["session_id"])["selections"]) == 1
    assert len(shared.session(tracker["session_id"])["selections"]) == 1
    with pytest.raises(ValueError, match="SESSION_ALREADY_ACTIVE_FOR_MODE"):
        shared.start_session(mode="ALL_RULES_TOGETHER", rule_ids=["SOURCE_H_MULT_2_3"],
            boundary=boundary, prestart_context=context, options={"window_size": 50})

    shared.session_action(combined["session_id"], "stop")
    resumed = feed(shared, FIXTURE["rounds"][1])
    assert resumed["decision"] == "SESSION_SCOPED"
    assert shared.session(combined["session_id"])["status"] == "MANUALLY_STOPPED"
    assert shared.session(tracker["session_id"])["status"] == "RUNNING"
    assert shared.session(tracker["session_id"])["observed_rounds"] == 2
    stopped_record = next(item for item in resumed["sessions"]
                          if item["session_id"] == combined["session_id"])
    assert stopped_record["decision"]["action"] == "WAIT"
    assert len(shared.session(combined["session_id"])["selections"]) == 1


def test_auto_next_cannot_create_a_second_active_session_for_same_mode():
    engine = RulesEngine()
    engine.configure(START_CHECK_POLICY_ID, enabled=True)
    boundary = source_boundary()
    context = FIXTURE["boundary"]["prestart_context"]
    completed = engine.start_session(mode="START_CHECK", rule_ids=[START_CHECK_POLICY_ID],
        boundary=boundary, prestart_context=context,
        options={"window_size": 1, "auto_next_check_enabled": True})
    prior = engine.sessions[completed["session_id"]]
    prior.update({"status": "COMPLETED", "auto_next_armed": True,
                  "last_round_index": context[-1]["round_index"]})

    manual = engine.start_session(mode="START_CHECK", rule_ids=[START_CHECK_POLICY_ID],
        boundary=boundary, prestart_context=context,
        options={"window_size": 50, "auto_next_check_enabled": False})
    # Simulate a persisted/recovered stale arm racing the manual start.
    prior["auto_next_armed"] = True
    feed(engine, FIXTURE["rounds"][0])

    assert prior["auto_next_armed"] is False
    assert prior["auto_next_block_reason"] == "SESSION_ALREADY_ACTIVE_FOR_MODE"
    running = [row for row in engine.sessions.values()
               if row["mode"] == "START_CHECK" and row["status"] == "RUNNING"]
    assert [row["session_id"] for row in running] == [manual["session_id"]]


def test_max_selection_limit_stops_new_selections_but_observes_full_window():
    engine, session = new_session("ALL_RULES_TOGETHER", ["SOURCE_H_MULT_2_3"],
                                  options={"window_size": 50, "max_selections": 1})
    for row in FIXTURE["rounds"]:
        feed(engine, row)
    detail = engine.session(session["session_id"])
    assert len(detail["selections"]) == 1
    assert detail["selection_stop_reason"] == "MAX_SELECTIONS_REACHED"
    assert detail["observed_rounds"] == 50
    assert detail["status"] == "COMPLETED"


def test_manual_stop_event_is_durably_recorded_with_its_stop_timestamp(tmp_path):
    repository = Repository(tmp_path / "rules-stop-event.sqlite3")
    repository.init()
    engine, session = new_session("ALL_RULES_TOGETHER", ["SOURCE_H_MULT_2_3"],
        options={"window_size": 50}, persistence=repository)
    stopped_at = "2026-10-14T00:05:00+00:00"
    engine.session_action(session["session_id"], "stop", now=stopped_at)
    engine._persist()

    with repository.connect() as conn:
        events = conn.execute("""SELECT reason,stopped_at FROM rule_session_stop_events
            WHERE session_id=?""", (session["session_id"],)).fetchall()
    assert len(events) == 1
    assert events[0]["reason"] == "MANUAL_STOP"
    assert events[0]["stopped_at"] == stopped_at


def test_concurrent_duplicate_observations_and_config_versions_are_isolated(tmp_path):
    repository = Repository(tmp_path / "concurrent-rules.sqlite3")
    repository.init()
    engine, session = new_session("ALL_RULES_TOGETHER", ["SOURCE_H_MULT_2_3"], persistence=repository)
    row = FIXTURE["rounds"][0]
    event = observed(row)
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda _: engine.observe_round(event, now=row["receiver_received_at"]), range(24)))
    detail = engine.session(session["session_id"])
    assert len(detail["selections"]) == 1
    assert detail["observed_rounds"] == 1
    old_version = detail["configuration_version"]
    engine.configure("SOURCE_H_MULT_2_3", enabled=False)
    assert engine.session(session["session_id"])["configuration_version"] == old_version
    assert engine.session(session["session_id"])["rule_ids"] == ["SOURCE_H_MULT_2_3"]
    with pytest.raises(ValueError, match="OPTIONAL_BANKROLL_MANAGEMENT_REMOVED"):
        engine.save_preset("invalid-one", "ALL_RULES_TOGETHER", ["SOURCE_H_MULT_2_3"],
                           {"fixed_stake_rwf": 77, "max_observed_rounds": 50})
    engine.save_preset("saved-one", "ALL_RULES_TOGETHER", ["SOURCE_H_MULT_2_3"],
                       {"max_observed_rounds": 50})
    restored = RulesEngine(persistence=repository)
    assert restored.status()["presets"]["saved-one"]["options"] == {"max_observed_rounds": 50}
    assert restored.session(session["session_id"])["configuration_version"] == old_version


def test_start_check_and_combined_policy_have_distinct_exact_private_transitions():
    engine = RulesEngine()
    start_state = _fresh_engine(START_CHECK_POLICY_ID)
    start_state.update({"rule_state": "PLAYING", "mode": "STANDARD", "previous_outcome": "H"})
    engine._advance_selected_state({}, START_CHECK_POLICY_ID, start_state, {}, "WIN", {}, "t1")
    assert start_state["rule_state"] == "WAIT_LOW"  # old reducer counts trigger H in sequence

    policy_state = _fresh_engine(COMBINED_POLICY_ID)
    policy_state.update({"rule_state": "PLAYING", "mode": "TWO_BET_WINS", "previous_outcome": None})
    for outcome in ("WIN", "LOSS", "WIN"):
        engine._advance_selected_state({}, COMBINED_POLICY_ID, policy_state, {}, outcome, {}, "t2")
        assert policy_state["mode"] == "TWO_BET_WINS"
    engine._advance_selected_state({}, COMBINED_POLICY_ID, policy_state, {}, "WIN", {}, "t3")
    assert policy_state["mode"] == "STANDARD"
    assert policy_state["rule_state"] == "WAIT_LOW"


def test_shared_select_creates_one_durable_blocked_intent_per_target_and_panel(tmp_path):
    repository = Repository(tmp_path / "rules-intents.sqlite3")
    repository.init()
    rule_ids = [*RESEARCH_IDS, COMBINED_POLICY_ID]
    engine, session = new_session("ALL_RULES_TOGETHER", rule_ids,
        options={"window_size": 50}, persistence=repository)
    observed_multiple_true = False
    # This contract is about merging concurrent TRUE rule results into one
    # durable target intent; the dedicated source-parity test below replays
    # the full recorded window. Keep persistence focused on the first target.
    for row in FIXTURE["rounds"][:3]:
        output = feed(engine, row)
        current = output["sessions"][0]
        if sum(item.get("eligible") is True for item in current["rule_evaluations"]) > 1:
            observed_multiple_true = True
            assert current["decision"]["action"] == "SELECT"
    selections = engine.sessions[session["session_id"]]["selections"]
    assert len({(item["expected_target"]["streamId"], item["expected_target"]["roundIndex"])
                for item in selections}) == len(selections)

    intents = repository.list_rule_execution_intents(100)
    assert observed_multiple_true
    assert intents
    assert len({(item["target_stream_id"], item["target_round_index"])
                for item in intents}) == len(intents)
    assert all(item["execution_status"] in {"BLOCKED_READ_ONLY", "EXPIRED"} for item in intents)
    assert all(item["selected_panel"] is None and item["panel"] == -1 for item in intents)
    assert all(not ({"requested_stake", "requested_stake_currency", "cashout_target", "stake", "currency"}
                    & set(item)) for item in intents)
    expired = [item for item in intents if item["execution_status"] == "EXPIRED"]
    assert expired
    assert all(item["target_round_id"] and item["expires_at"] and
               item["execution_eligibility"] == "EXPIRED" and
               item["provider_target_round_id"] is None and
               item["target_identity_status"] == "UNVERIFIED" and
               "TARGET_ALREADY_COMPLETED" in item["blockers"] for item in expired)
    assert all(item["expires_at"] is None for item in intents if item["execution_status"] != "EXPIRED")
    assert all("BETTING_DEADLINE_UNVERIFIED" in item["blockers"] for item in intents)
    assert all(item["selected_rule_ids"] for item in intents)
    original_ids = {item["intent_id"] for item in intents}


    # Recreate the legacy state where a completed target has no persisted ID
    # or expiry in its intent. A normal engine restart must repair both the
    # in-memory selection and the durable row from its committed target event.
    completed_selection = next(item for item in selections if item.get("actual_target"))
    legacy_snapshot = deepcopy(engine._snapshot())
    legacy_session = legacy_snapshot["sessions"][session["session_id"]]
    legacy_selection = next(item for item in legacy_session["selections"]
                            if item["selection_id"] == completed_selection["selection_id"])
    legacy_intent = legacy_selection["execution_intent"]
    legacy_intent.update({"target_round_id": None, "provider_target_round_id": None,
                          "expires_at": None, "execution_status": "BLOCKED_READ_ONLY",
                          "selected_panel": None, "panel": 0})
    legacy_intent.pop("execution_eligibility", None)
    legacy_intent.pop("target_completed_at", None)
    legacy_intent.pop("target_identity_status", None)
    legacy_intent["blockers"] = [value for value in legacy_intent.get("blockers", [])
                                  if value != "TARGET_ALREADY_COMPLETED"]
    serialized_intent = json.dumps(legacy_intent, sort_keys=True, separators=(",", ":"))
    serialized_selection = json.dumps({**legacy_selection, "selection_kind": "SHARED"},
                                      sort_keys=True, separators=(",", ":"))
    with repository.connect() as conn:
        conn.execute("UPDATE application_state SET payload=? WHERE state_key=?",
                     (json.dumps(legacy_snapshot, sort_keys=True, separators=(",", ":")), STATE_KEY))
        conn.execute("""UPDATE rule_execution_intents SET target_round_id=NULL,expires_at=NULL,
            execution_status='BLOCKED_READ_ONLY',panel=0,payload=? WHERE decision_id=?""",
                     (serialized_intent, completed_selection["selection_id"]))
        conn.execute("UPDATE rule_selections SET payload=? WHERE selection_id=?",
                     (serialized_selection, completed_selection["selection_id"]))

    restarted = RulesEngine(persistence=repository)
    repaired_intent = repository.rule_execution_intent(completed_selection["selection_id"])
    assert repaired_intent["execution_status"] == "EXPIRED"
    assert repaired_intent["target_round_id"] == completed_selection["actual_target"]["roundId"]
    assert repaired_intent["provider_target_round_id"] is None
    assert repaired_intent["target_identity_status"] == "UNVERIFIED"
    assert repaired_intent["expires_at"] == completed_selection["resolved_at"]
    restarted_selection = next(item for item in restarted.session(session["session_id"])["selections"]
                               if item["selection_id"] == completed_selection["selection_id"])
    assert restarted_selection["execution_intent"]["execution_status"] == "EXPIRED"
    assert restarted_selection["execution_intent"]["selected_panel"] is None
    assert restarted_selection["execution_intent"]["panel"] == -1
    assert {item["intent_id"] for item in repository.list_rule_execution_intents(100)} == original_ids


def test_selected_panel_intent_carries_read_only_state_timing_and_unverified_receipt(monkeypatch):
    monkeypatch.setattr("app.rules_engine._current_runtime_panel_observation",
        lambda panel, at: {"panel_id": f"PANEL_{panel + 1}", "state": "UNKNOWN",
                           "observed_at": None, "pending_bet_state": "UNKNOWN"})
    engine, session = new_session("ALL_RULES_TOGETHER", ["SOURCE_H_MULT_2_3"])
    selected = None
    selected_event = None
    for fixture_row in FIXTURE["rounds"]:
        row = deepcopy(fixture_row)
        observed_at = datetime.fromisoformat(row["observed_at"].replace("Z", "+00:00"))
        deadline_at = (observed_at + timedelta(seconds=6)).isoformat()
        panels = [
            {"panel_id": "PANEL_1", "state": "BET_AVAILABLE", "button_text": "Bet 200 BIF",
             "displayed_stake": 200, "currency": "BIF", "observed_at": row["observed_at"],
             "source": "AVIATOR_BETTING_PANEL_MUTATION_OBSERVER", "confidence": 0.98,
             "frame_identity": "https://aviator-next.spribegaming.com/", "pending_bet_state": "CLEAR",
             "evidence": {"reason": "BET_ACTION_VISIBLE_ENABLED"}},
            {"panel_id": "PANEL_2", "state": "BET_ACTIVE", "button_text": "Cash Out 121 BIF",
             "displayed_stake": None, "cashout_amount": 121, "currency": "BIF",
             "observed_at": row["observed_at"], "source": "AVIATOR_BETTING_PANEL_MUTATION_OBSERVER",
             "confidence": 0.98, "frame_identity": "https://aviator-next.spribegaming.com/",
             "pending_bet_state": "ACTIVE", "evidence": {"reason": "CASHOUT_ACTION_VISIBLE_ENABLED"}},
        ]
        row["game_phase"] = {"phase": "BETTING_OPEN", "observed_at": row["observed_at"],
            "evidence": {"source": "AVIATOR_EXPLICIT_BETTING_PHASE_DOM",
                "explicit_window_state": "BETTING_OPEN", "deadline_status": "VERIFIED",
                "countdown_seconds": 6, "deadline_at": deadline_at},
            "panel_observations": panels}
        observed_event = observed(row)
        committed_event = committed(row)
        result = engine.observe_round(observed_event, now=row["receiver_received_at"])
        result = engine.observe_round(committed_event, now=row["database_committed_at"])
        session_result = next((item for item in result.get("sessions", [])
                               if item["session_id"] == session["session_id"]), None)
        if session_result and session_result.get("decision", {}).get("action") == "SELECT":
            selected_event = committed_event
            selected = next(item for item in engine.sessions[session["session_id"]]["selections"]
                            if item["selection_id"] == session_result["decision"]["selection_id"])
            break
    assert selected is not None
    intent = selected["execution_intent"]
    assert intent["session_id"] == session["session_id"]
    assert intent["decision_id"] == selected["selection_id"]
    assert intent["source_round_id"] == selected["source_round"]["roundId"]
    assert intent["target_round_id"] == selected["expected_target"]["roundId"]
    assert intent.get("selected_panel") is None
    assert not ({"stake", "currency", "cashout_target"} & set(intent))
    assert intent["panel_state_before"]["state"] == "UNKNOWN"
    assert intent["panel_state_before"]["evidence"]["reason"] == "EXECUTION_PANEL_NOT_SELECTED"
    assert intent["panel_state_after"]["state"] == "UNKNOWN"
    assert intent["deadline_evidence"]["status"] == "VERIFIED"
    assert intent["decision_latency_ms"] is not None
    assert intent["platform_evidence"] is None
    assert intent["platform_acceptance"] == "UNVERIFIED"
    assert intent["settlement"] == "UNVERIFIED"
    assert intent["execution_status"] == "BLOCKED_READ_ONLY"

    engine.observe_round(selected_event, now=selected_event["database_committed_at"])
    assert len(engine.sessions[session["session_id"]]["selections"]) == 1


def test_five_independent_true_rules_share_one_select_and_one_intent(tmp_path):
    repository = Repository(tmp_path / "five-rule-shared-intent.sqlite3")
    repository.init()
    engine, session = new_session("FIVE_RULE_TRACKER", RESEARCH_IDS,
        options={"window_size": 50}, persistence=repository)

    output = feed(engine, FIXTURE["rounds"][0])
    detail = engine.session(session["session_id"])
    record = detail["evaluations"][-1]
    true_rules = [row["rule_id"] for row in record["rule_evaluations"] if row["eligible"] is True]
    assert len(true_rules) > 1
    assert record["decision"]["action"] == "SELECT"
    assert record["decision"]["true_rule_ids"] == true_rules
    assert len(detail["selections"]) == 1
    assert detail["selections"][0]["contributing_rule_ids"] == true_rules
    assert all(not rows for rows in detail["rule_selections"].values())

    intents = repository.list_rule_execution_intents(10)
    assert len(intents) == 1
    assert intents[0]["selected_rule_ids"] == true_rules
    assert intents[0]["decision_id"] == detail["selections"][0]["selection_id"]
    assert "cashout_target" not in intents[0]
    assert intents[0]["target_round_id"] is None
    assert intents[0]["selected_panel"] is None and intents[0]["panel"] == -1
    assert intents[0]["execution_status"] == "BLOCKED_READ_ONLY"
    assert "TARGET_ROUND_ID_UNVERIFIED" in intents[0]["blockers"]
    assert output["sessions"][0]["decision"]["action"] == "SELECT"


def test_rule_intents_are_not_bound_to_bankroll_or_platform_panel(tmp_path):
    repository = Repository(tmp_path / "missing-panel-intent.sqlite3")
    repository.init()
    engine, _session = new_session("ALL_RULES_TOGETHER", [*RESEARCH_IDS, COMBINED_POLICY_ID],
                                  options={"window_size": 50}, persistence=repository)
    for row in FIXTURE["rounds"][:3]:
        feed(engine, row)
    intents = repository.list_rule_execution_intents(100)
    assert intents
    assert all(intent["selected_panel"] is None and intent["panel"] == -1 for intent in intents)
    assert all(not ({"requested_stake", "requested_stake_currency", "cashout_target", "stake", "currency"}
                    & set(intent)) for intent in intents)


def test_rule_interface_reports_persistence_without_authorizing_live(tmp_path):
    repository = Repository(tmp_path / "decision-interface.sqlite3")
    repository.init()
    engine = RulesEngine(persistence=repository)

    interface = engine.decision_interface_status()

    assert interface["available"] is True
    assert interface["persistence"] == "DURABLE_REPOSITORY"
    assert interface["live_authorized"] is False
    assert engine.execution_authorized() is False


def test_read_only_balance_snapshots_do_not_calculate_bankroll_growth(tmp_path):
    repository = Repository(tmp_path / "rules-financials.sqlite3")
    repository.init()
    first = repository.record_platform_balance_observation({
        "balance": 1000.0, "displayed_amount": 1000.0, "currency": "USD",
        "verified": True, "session_identity": "platform-session-one",
        "application_session_id": "platform-session-one",
        "observed_at": "2026-10-14T00:00:02+00:00",
    })
    second = repository.record_platform_balance_observation({
        "balance": 1050.0, "displayed_amount": 1050.0, "currency": "USD",
        "verified": True, "session_identity": "platform-session-one",
        "application_session_id": "platform-session-one",
        "observed_at": "2026-10-14T00:00:03+00:00",
    })

    assert first and second
    assert repository.latest_platform_balance_observation()["balance"] == 1050.0
    assert repository.rules_session_financials("platform-session-one") is None


def test_balance_snapshots_do_not_create_currency_or_session_bankroll_totals(tmp_path):
    repository = Repository(tmp_path / "rules-balance-currency.sqlite3")
    repository.init()
    repository.record_platform_balance_observation({
        "balance": 1000.0, "currency": "BIF", "verified": True,
        "application_session_id": "currency-session", "observed_at": "2026-10-14T00:00:00Z",
    })
    repository.record_platform_balance_observation({
        "balance": 500.0, "currency": "USD", "verified": True,
        "application_session_id": "currency-session", "observed_at": "2026-10-14T00:00:01Z",
    })
    assert repository.rules_session_financials("currency-session") is None

    unknown_start = "unknown-currency-session"
    repository.record_platform_balance_observation({
        "balance": 1000.0, "currency": None, "verified": True,
        "application_session_id": unknown_start, "observed_at": "2026-10-14T00:00:02Z",
    })
    repository.record_platform_balance_observation({
        "balance": 1100.0, "currency": "BIF", "verified": True,
        "application_session_id": unknown_start, "observed_at": "2026-10-14T00:00:03Z",
    })
    assert repository.rules_session_financials(unknown_start) is None


def test_source_parity_for_combined_and_independent_tracker_on_same_saved_rounds():
    if not SOURCE_ROOT.exists():
        pytest.skip("original source checkout is not available")
    source_program = r'''
      import fs from 'node:fs';
      import { createCombinedRulesTest, observeCombinedRulesRound, DEFAULT_COMBINED_RUN_SETTINGS } from './src/combined-rules-test.js';
      import { createFiveRuleExperiment, observeFiveRuleRound } from './src/five-rule-experiment.js';
      const fixture=JSON.parse(fs.readFileSync('./test/fixtures/combined-recorded-50.json','utf8'));
      const source={connection_state:'CONNECTED',collector_status:'HEALTHY',continuity_status:'VERIFIED'};
      const opts={boundary:{...fixture.boundary,sequence:fixture.boundary.stream_sequence},
        synchronizationRoundIndex:fixture.boundary.synchronization_round_index,
        pendingBoundary:fixture.boundary.pending_cursor_sequence,
        prestartExcludedObservations:fixture.boundary.prestart_excluded_observation_ids.map(observation_id=>({observation_id})),
        prestartContext:fixture.boundary.prestart_context,runSettings:{...DEFAULT_COMBINED_RUN_SETTINGS},now:fixture.boundary.captured_at};
      const observed=row=>({schema_version:1,event_type:'round.observed',stage:'OBSERVED',durability:'PENDING',
        continuity_status:'VERIFIED',delivery_mode:'LIVE',...row,stream_sequence:null});
      const persisted=row=>({schema_version:1,event_type:'round.persisted',stage:'PERSISTED',durability:'COMMITTED',
        continuity_status:'VERIFIED',delivery_mode:'LIVE',...row,event_id:`aviator-round:${row.round_id}:persisted`,stored_at:row.database_committed_at});
      const combined=createCombinedRulesTest(opts), five=createFiveRuleExperiment(opts), output=[];
      for(const recorded of fixture.rounds){
        const c=observeCombinedRulesRound(combined,observed(recorded),{source,now:recorded.receiver_received_at});
        observeFiveRuleRound(five,observed(recorded),{source,now:recorded.receiver_received_at});
        const f=five.ledger.at(-1);
        output.push({round_index:recorded.round_index,
          combined:(c?.rule_proposals||[]).map(p=>({rule_id:p.ruleId,eligible:p.eligible,status:p.status})),
          five:Object.fromEntries(Object.entries(f?.rule_results||{}).map(([id,value])=>[id,Boolean(value.source_selection)]))});
        observeCombinedRulesRound(combined,persisted(recorded),{source,stage:'COMMITTED',now:recorded.database_committed_at});
        observeFiveRuleRound(five,persisted(recorded),{source,stage:'COMMITTED',now:recorded.database_committed_at});
      }
      process.stdout.write(JSON.stringify(output));
    '''
    env = {**os.environ, "SOURCE_PROJECT": str(SOURCE_ROOT),
           "PATH": f"{NODE_BIN.parent}:{os.environ.get('PATH', '')}"}
    completed = subprocess.run([str(NODE_BIN), "--input-type=module", "-e", source_program], cwd=SOURCE_ROOT,
                               env=env, check=True, capture_output=True, text=True, timeout=30)
    oracle = json.loads(completed.stdout)
    combined_engine, combined_session = new_session("ALL_RULES_TOGETHER",
        [*RESEARCH_IDS, COMBINED_POLICY_ID])
    five_engine, five_session = new_session("FIVE_RULE_TRACKER", RESEARCH_IDS)
    for index, row in enumerate(FIXTURE["rounds"]):
        feed(combined_engine, row)
        feed(five_engine, row)
        combined_evaluations = combined_engine.session(combined_session["session_id"])["evaluations"][-1]["rule_evaluations"]
        combined_actual = [{"rule_id": item["rule_id"], "eligible": item["eligible"], "status": item["status"]}
                           for item in combined_evaluations]
        assert combined_actual == oracle[index]["combined"], f"combined source mismatch at #{row['round_index']}"
        five_record = five_engine.session(five_session["session_id"])["evaluations"][-1]
        five_actual = {rule_id: any(item["rule_id"] == rule_id and item["eligible"] is True
                                    for item in five_record["rule_evaluations"]) for rule_id in RESEARCH_IDS}
        assert five_actual == oracle[index]["five"], f"independent tracker source mismatch at #{row['round_index']}"


def test_source_parity_for_start_check_and_legacy_tracker_on_saved_rounds():
    if not SOURCE_ROOT.exists():
        pytest.skip("original source checkout is not available")
    source_program = r'''
      import fs from 'node:fs';
      import { createRun, reservePendingRound, applyObservedRound } from './src/rule-engine.js';
      import { createLiveShadowTest, applyLiveShadowObservation } from './src/live-shadow-test.js';
      const fixture=JSON.parse(fs.readFileSync('./test/fixtures/combined-recorded-50.json','utf8'));
      const b={...fixture.boundary,sequence:fixture.boundary.stream_sequence};
      const excluded=fixture.boundary.prestart_context
        .filter(row=>row.round_index>fixture.boundary.round_index)
        .map(row=>({observation_id:row.observation_id,round_index:row.round_index}));
      const common={boundary:b,prestartRoundIndex:fixture.boundary.synchronization_round_index,
        prestartExcludedObservations:excluded,now:fixture.boundary.captured_at,windowSize:50};
      const run=createRun(common);
      const live=createLiveShadowTest({boundary:b,synchronizationRoundIndex:fixture.boundary.synchronization_round_index,
        pendingBoundary:fixture.boundary.pending_cursor_sequence,
        prestartExcludedObservations:fixture.boundary.prestart_excluded_observation_ids.map(observation_id=>({observation_id})),
        now:fixture.boundary.captured_at,windowSize:50});
      const source={connection_state:'CONNECTED',collector_status:'HEALTHY',continuity_status:'VERIFIED'};
      const outputs=[];
      for(const row of fixture.rounds){
        const observed={...row,event_type:'round.observed',durability:'PENDING',continuity_status:'VERIFIED',
          delivery_mode:'LIVE',stream_sequence:null};
        const reserved=reservePendingRound(run,observed,{receiverReceivedAt:row.receiver_received_at});
        applyObservedRound(run,observed,{existingRow:reserved,now:row.receiver_received_at,
          receiverReceivedAt:row.receiver_received_at,source});
        const committed={...row,event_type:'round.persisted',durability:'COMMITTED',continuity_status:'VERIFIED',delivery_mode:'LIVE'};
        applyLiveShadowObservation(live,committed,{source,stage:'COMMITTED',now:row.database_committed_at});
        outputs.push({round_index:row.round_index,
          start_check:Boolean(run.pending_intent&&run.pending_intent.source_round_id===row.round_id),
          legacy_tracker:Boolean(live.pending_intent&&live.pending_intent.source_round_id===row.round_id)});
      }
      process.stdout.write(JSON.stringify(outputs));
    '''
    env = {**os.environ, "SOURCE_PROJECT": str(SOURCE_ROOT),
           "PATH": f"{NODE_BIN.parent}:{os.environ.get('PATH', '')}"}
    completed = subprocess.run([str(NODE_BIN), "--input-type=module", "-e", source_program], cwd=SOURCE_ROOT,
                               env=env, check=True, capture_output=True, text=True, timeout=30)
    oracle = json.loads(completed.stdout)
    start_engine, start_session = new_session("START_CHECK", [START_CHECK_POLICY_ID])
    legacy_engine, legacy_session = new_session("LEGACY_SEPARATE_TRACKER", [LEGACY_POLICY_ID])
    for index, row in enumerate(FIXTURE["rounds"]):
        feed(start_engine, row)
        feed(legacy_engine, row)
        start_record = start_engine.session(start_session["session_id"])["evaluations"][-1]
        legacy_record = legacy_engine.session(legacy_session["session_id"])["evaluations"][-1]
        assert (start_record["decision"]["action"] == "SELECT") == oracle[index]["start_check"], \
            f"Start Check source mismatch at #{row['round_index']}"
        assert (legacy_record["decision"]["action"] == "SELECT") == oracle[index]["legacy_tracker"], \
            f"Legacy tracker source mismatch at #{row['round_index']}"
