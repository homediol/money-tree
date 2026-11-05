import json
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path

import pytest

from app.api.rules_engine import _without_retired_risk_data
from app.database.repository import Repository
from app.rules_engine import (
    COMBINED_POLICY_ID,
    LEGACY_POLICY_ID,
    RESEARCH_RULES,
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


def _special_skip_round(boundary, round_index, multiplier):
    offset = round_index - boundary["round_index"]
    observed_at = f"2026-10-16T00:00:{offset:02d}+00:00"
    return {
        "stream_id": boundary["stream_id"],
        "round_id": f"special-skip-round-{round_index}",
        "observation_id": f"special-skip-round-{round_index}",
        "round_index": round_index,
        "stream_sequence": boundary["stream_sequence"] + offset,
        "multiplier": multiplier,
        "receiver_received_at": observed_at,
        "database_committed_at": f"2026-10-16T00:00:{offset + 1:02d}+00:00",
        "observed_at": observed_at,
    }


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
    special_loss_stop = "One selected LOSS during the active special skip ends that policy cycle immediately."
    for rule_id in (START_CHECK_POLICY_ID, COMBINED_POLICY_ID):
        assert by_id[rule_id]["condition"]["h_loss_win_special_skip"] is True
        assert any(special_loss_stop in rule for rule in by_id[rule_id]["transition_rules"])
    assert by_id[LEGACY_POLICY_ID]["condition"]["h_loss_win_special_skip"] is False
    assert not any(special_loss_stop in rule
                   for rule in by_id[LEGACY_POLICY_ID]["transition_rules"])
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
        options={"window_size": 50, "fixed_stake_rwf": 100})
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


def test_all_rules_together_uses_only_explicitly_selected_enabled_rules():
    engine = RulesEngine()
    selected = [COMBINED_POLICY_ID, "SOURCE_H_MULT_2_3"]
    # Other policies may be enabled in the registry without joining this run.
    for rule_id in [*RESEARCH_IDS, COMBINED_POLICY_ID]:
        engine.configure(rule_id, enabled=True)
    session = engine.start_session(
        mode="ALL_RULES_TOGETHER", rule_ids=selected,
        boundary=source_boundary(), prestart_context=FIXTURE["boundary"]["prestart_context"],
        options={"fixed_stake_rwf": 100},
        now=FIXTURE["boundary"]["captured_at"],
    )

    assert session["rule_ids"] == selected
    assert list(session["engines"]) == selected
    assert [item["rule_id"] for item in session["manifest"]["rules"]] == selected

    feed(engine, FIXTURE["rounds"][0])
    detail = engine.session(session["session_id"])
    evaluation = detail["evaluations"][-1]
    assert [item["rule_id"] for item in evaluation["rule_evaluations"]] == selected
    assert evaluation["decision"]["true_rule_ids"] == ["SOURCE_H_MULT_2_3"]
    assert detail["selections"][-1]["contributing_rule_ids"] == ["SOURCE_H_MULT_2_3"]


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
    assert detail["evaluations"][-1]["rule_evaluations"][0]["status"] == "TARGET_ALREADY_COMPLETED"
    assert result["decision"] == "WAIT"


def test_pending_priority_delivery_skips_pre_session_source_and_commit_only_marks_it_committed():
    row = deepcopy(FIXTURE["rounds"][0])
    engine = RulesEngine()
    engine.configure("SOURCE_H_MULT_2_3", enabled=True)
    session = engine.start_session(mode="ALL_RULES_TOGETHER", rule_ids=["SOURCE_H_MULT_2_3"],
        boundary=source_boundary(), prestart_context=FIXTURE["boundary"]["prestart_context"],
        options={}, now="2026-10-11T13:30:40+00:00")

    pending = engine.observe_round(observed(row), now="2026-10-11T13:30:41+00:00")
    detail = engine.session(session["session_id"])
    assert detail["selections"] == []
    assert detail["evaluations"] == []
    assert detail["prestart_skipped_rounds"] == 1
    assert pending["sessions"][0]["decision"]["reason"] == "PRESTART_ROUND"

    committed_result = engine.observe_round(committed(row), now=row["database_committed_at"])
    committed_detail = engine.session(session["session_id"])
    assert committed_detail["selections"] == []
    assert committed_detail["prestart_skipped_rounds"] == 1
    assert committed_detail["last_stream_sequence"] == row["stream_sequence"]
    assert committed_result["sessions"][0]["persistence_status"] == "COMMITTED"


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


def test_observed_replay_after_committed_only_record_does_not_pause_or_re_evaluate():
    engine, session = new_session("ALL_RULES_TOGETHER", ["SOURCE_H_MULT_2_3"])
    row = FIXTURE["rounds"][0]
    committed_result = engine.observe_round(committed(row), now=row["database_committed_at"],
                                            target_already_completed=True)
    detail_before = engine.session(session["session_id"])
    replay_result = engine.observe_round(observed(row), now=row["receiver_received_at"])
    detail_after = engine.session(session["session_id"])

    assert committed_result["sessions"][0]["decision"]["action"] == "WAIT"
    assert len(detail_before["evaluations"]) == 1
    assert detail_after["evaluations"] == detail_before["evaluations"]
    assert detail_after["status"] == "RUNNING"
    assert replay_result["sessions"][0]["decision"]["action"] == "WAIT"


def test_win_cap_stops_selection_but_observer_finishes_window():
    options = {"window_size": 50, "fixed_stake_rwf": 100,
               "win_limit_enabled": True, "win_limit_count": 1}
    engine, session = new_session("ALL_RULES_TOGETHER", ["SOURCE_H_MULT_2_3"], options=options)
    for row in FIXTURE["rounds"]:
        feed(engine, row)
    detail = engine.session(session["session_id"])
    assert detail["selection_stop_reason"] == "WIN_LIMIT_REACHED"
    assert detail["net_outcome"] >= 1
    assert detail["total_wins"] >= 1
    assert detail["observed_rounds"] == 50
    assert detail["status"] == "COMPLETED"
    assert all(selection["expected_target"]["roundIndex"] <= source_boundary()["round_index"] + 50
               for selection in detail["selections"])


def _net_limit_engine(outcomes, *, win_limit=2, loss_limit=2, pending_count=1, stake_rwf=100,
                      win_enabled=True, loss_enabled=True):
    boundary = {"stream_id": "net-limit-stream", "round_id": "net-limit-boundary",
                "observation_id": "net-limit-boundary", "round_index": 1,
                "stream_sequence": 1, "multiplier": 1.5,
                "event_type": "round.persisted", "durability": "COMMITTED",
                "continuity_status": "VERIFIED"}
    engine = RulesEngine()
    session = engine.start_session(mode="ALL_RULES_TOGETHER", rule_ids=[], boundary=boundary,
        options={"window_size": len(outcomes) + 5, "fixed_stake_rwf": stake_rwf,
                 "win_limit_enabled": win_enabled, "win_limit_count": win_limit,
                 "loss_limit_enabled": loss_enabled, "loss_limit_count": loss_limit},
        now="2026-10-16T00:00:00+00:00")
    stored = engine.sessions[session["session_id"]]
    for offset, outcome in enumerate(outcomes, start=1):
        target_index = offset + 1
        stored["selections"].append({
            "selection_id": f"selection-{offset}", "session_id": session["session_id"],
            "source_round": {"streamId": boundary["stream_id"], "roundIndex": target_index - 1},
            "expected_target": {"streamId": boundary["stream_id"], "roundIndex": target_index},
            "contributing_rule_ids": [], "selected_at": "2026-10-16T00:00:01+00:00",
            "status": "PENDING", "result": "PENDING", "actual_multiplier": None,
            "actual_target": None, "resolved_at": None,
        })
    for offset in range(pending_count):
        target_index = len(outcomes) + offset + 2
        stored["selections"].append({
            "selection_id": f"pending-{offset}", "session_id": session["session_id"],
            "expected_target": {"streamId": boundary["stream_id"], "roundIndex": target_index},
            "contributing_rule_ids": [], "selected_at": "2026-10-16T00:00:01+00:00",
            "status": "PENDING", "result": "PENDING", "actual_multiplier": None,
            "actual_target": None, "resolved_at": None,
        })
    for offset, outcome in enumerate(outcomes, start=1):
        index = offset + 1
        event = {"stream_id": boundary["stream_id"], "round_id": f"net-limit-round-{index}",
                 "observation_id": f"net-limit-round-{index}", "round_index": index,
                 "stream_sequence": index, "multiplier": 2.1 if outcome == "WIN" else 1.5,
                 "event_type": "round.persisted", "durability": "COMMITTED",
                 "continuity_status": "VERIFIED", "observed_at": "2026-10-16T00:00:02+00:00"}
        engine.observe_round(event, now="2026-10-16T00:00:03+00:00")
    return engine, session


@pytest.mark.parametrize("outcomes,expected_reason", [
    (["WIN"], None),
    (["WIN", "WIN"], "WIN_LIMIT_REACHED"),
    (["WIN", "LOSS", "WIN"], None),
    (["LOSS", "WIN", "WIN", "WIN"], "WIN_LIMIT_REACHED"),
    (["LOSS", "WIN", "LOSS", "WIN", "WIN"], None),
    (["WIN", "LOSS", "WIN", "LOSS", "LOSS"], None),
    (["LOSS", "WIN", "LOSS", "LOSS"], "LOSS_LIMIT_REACHED"),
])
def test_combined_net_outcome_limits_match_all_examples_and_ignore_pending(outcomes, expected_reason):
    engine, session = _net_limit_engine(outcomes)
    detail = engine.session(session["session_id"])
    summary = engine.status()["active_session"]
    wins = outcomes.count("WIN")
    losses = outcomes.count("LOSS")
    assert detail["total_wins"] == summary["results"]["WIN"] == wins
    assert detail["total_losses"] == summary["results"]["LOSS"] == losses
    assert detail["net_outcome"] == summary["net_outcome"] == wins - losses
    assert detail["net_profit_rwf"] == summary["net_profit_rwf"] == (wins - losses) * 100
    assert summary["results"]["PENDING"] == 1
    assert detail["selection_stop_reason"] == expected_reason
    assert detail["selection_status"] == ("STOPPED" if expected_reason else "ACTIVE")
    assert detail["status"] == ("SETTLING" if expected_reason else "RUNNING")
    if expected_reason:
        assert detail["selection_stop_source"] == "NET_OUTCOME_LIMIT"
        assert detail["selection_stopped_at"]
        assert detail["selection_stopped_at_round_id"]
        assert detail["selection_stop_details"]["net_outcome"] == wins - losses


def test_disabled_net_limits_do_not_stop_and_terminal_summary_keeps_the_same_scope():
    engine, session = _net_limit_engine(["WIN", "WIN", "LOSS", "LOSS", "LOSS", "LOSS"],
        pending_count=2, win_enabled=False, loss_enabled=False)
    detail = engine.session(session["session_id"])
    assert detail["total_wins"] == detail["results"]["WIN"] == 2
    assert detail["total_losses"] == detail["results"]["LOSS"] == 4
    assert detail["results"]["PENDING"] == 2
    assert detail["net_outcome"] == 2 - 4 == -2
    assert detail["selection_stop_reason"] is None
    assert detail["selection_status"] == "ACTIVE"

    engine.session_action(session["session_id"], "stop", now="2026-10-16T00:01:00+00:00")
    status = engine.status()
    assert status["active_session"] is None
    assert status["latest_session"]["session_id"] == session["session_id"]
    assert status["reporting_session_id"] == session["session_id"]
    assert status["reporting_session_status"] == "MANUALLY_STOPPED"
    assert status["total_wins"] == status["latest_session"]["results"]["WIN"] == 2
    assert status["total_losses"] == status["latest_session"]["results"]["LOSS"] == 4
    assert status["net_outcome"] == status["latest_session"]["net_outcome"] == -2
    assert status["latest_session"]["stop_reason"] == "MANUAL_STOP"
    assert status["latest_session"]["stop_reason_source"] == "MANUAL_STOP_CONTROL"
    assert status["latest_session"]["stop_reason_at"] == "2026-10-16T00:01:00+00:00"


def test_balanced_two_wins_two_losses_with_both_limits_off_stays_running_without_stop_reason():
    engine, session = _net_limit_engine(["WIN", "WIN", "LOSS", "LOSS"], pending_count=0,
        win_enabled=False, loss_enabled=False)
    detail = engine.session(session["session_id"])
    summary = engine.status()["active_session"]

    assert detail["results"] == {"WIN": 2, "LOSS": 2, "PENDING": 0}
    assert detail["net_outcome"] == summary["net_outcome"] == 0
    assert detail["selection_stop_reason"] is None
    assert detail["stop_reason"] is None
    assert detail["status"] == summary["status"] == "RUNNING"
    assert detail["selection_status"] == "ACTIVE"


def test_reversed_settlements_clear_active_limit_stop_but_preserve_its_history():
    engine, session = _net_limit_engine(["WIN", "WIN", "LOSS", "LOSS"], pending_count=0,
        win_enabled=True, loss_enabled=False)
    detail = engine.session(session["session_id"])

    assert detail["net_outcome"] == 0
    assert detail["selection_stop_reason"] is None
    assert detail["stop_reason"] is None
    assert detail["selection_status"] == "ACTIVE"
    assert detail["status"] == "RUNNING"
    assert any(event["reason"] == "WIN_LIMIT_REACHED" for event in detail["stop_history"])


def test_balanced_results_and_pending_outcomes_do_not_change_net_or_limits():
    engine, session = _net_limit_engine(["WIN", "LOSS", "WIN", "LOSS", "WIN", "LOSS"],
        pending_count=3, win_enabled=False, loss_enabled=False)
    detail = engine.session(session["session_id"])
    assert detail["results"] == {"WIN": 3, "LOSS": 3, "PENDING": 3}
    assert detail["net_outcome"] == 0
    assert detail["selection_stop_reason"] is None
    assert detail["selection_status"] == "ACTIVE"


def test_shadow_profit_loss_uses_the_entered_fixed_stake():
    engine, session = _net_limit_engine(["WIN", "LOSS", "WIN"], stake_rwf=175)
    detail = engine.session(session["session_id"])
    summary = engine.status()["active_session"]
    assert detail["net_outcome"] == 1
    assert detail["net_profit_rwf"] == summary["net_profit_rwf"] == 175


def test_verified_target_settles_immediately_before_commit_and_keeps_exact_target_evidence():
    engine, session = new_session("ALL_RULES_TOGETHER", ["SOURCE_H_MULT_2_3"],
        options={"fixed_stake_rwf": 175})
    source, target = FIXTURE["rounds"][:2]
    engine.observe_round(observed(source), now=source["receiver_received_at"])
    pending = engine.session(session["session_id"])["selections"][0]
    assert pending["status"] == "PENDING"
    assert pending["decision_id"] == pending["selection_id"]
    assert pending["source_round_id"] == source["round_id"]
    assert pending["target_round_id"] is None
    assert pending["matched_rule_ids"] == ["SOURCE_H_MULT_2_3"]

    target_event = observed(target)
    settled = engine.observe_round(target_event, now=target["receiver_received_at"])
    detail = engine.session(session["session_id"])
    selection = detail["selections"][0]
    assert len(settled["target_settlements"]) == 1
    assert selection["status"] == selection["settlement_status"] == "LOSS"
    assert selection["target_round_id"] == target["round_id"]
    assert selection["actual_target"]["roundId"] == target["round_id"]
    assert selection["actual_multiplier"] == target["multiplier"]
    assert selection["target_persistence_status"] == "PENDING"
    assert selection["settlement_evidence"]["event_type"] == "round.observed"
    assert selection["settlement_evidence"]["continuity_status"] == "VERIFIED"
    assert selection["settled_at"] == target["receiver_received_at"]
    assert selection["stake_rwf"] == 175
    assert selection["payout_multiplier"] == 2.0
    assert selection["payout_rwf"] == 0
    assert selection["profit_loss_rwf"] == detail["net_profit_rwf"] == -175
    assert detail["last_received_round"]["round_id"] == target["round_id"]
    assert detail["last_settled_target"]["round_id"] == target["round_id"]

    engine.observe_round(committed(target), now=target["database_committed_at"])
    after_commit = engine.session(session["session_id"])
    assert after_commit["selections"][0]["status"] == "LOSS"
    assert after_commit["selections"][0]["target_persistence_status"] == "COMMITTED"
    assert after_commit["net_profit_rwf"] == -175
    assert after_commit["results"] == {"WIN": 0, "LOSS": 1, "PENDING": 0}


def test_delayed_out_of_order_target_round_settles_without_advancing_session_progress():
    engine, session = new_session("ALL_RULES_TOGETHER", ["SOURCE_H_MULT_2_3"])
    source, target, following = FIXTURE["rounds"][:3]
    engine.observe_round(observed(source), now=source["receiver_received_at"])
    engine.observe_round(observed(following), now=following["receiver_received_at"])
    paused = engine.session(session["session_id"])
    assert paused["status"] == "PAUSED"
    assert paused["stop_reason"] == "ROUND_INDEX_GAP"
    observed_count = paused["observed_rounds"]

    engine.observe_round(observed(target), now=target["receiver_received_at"])
    recovered = engine.session(session["session_id"])
    assert recovered["selections"][0]["status"] == "LOSS"
    assert recovered["selections"][0]["target_round_id"] == target["round_id"]
    assert recovered["observed_rounds"] == observed_count
    assert recovered["status"] == "PAUSED"
    assert recovered["stop_reason"] == "ROUND_INDEX_GAP"


def test_completed_session_still_settles_existing_target_but_does_not_observe_more_sources():
    engine, session = new_session("ALL_RULES_TOGETHER", ["SOURCE_H_MULT_2_3"])
    source, target, following = FIXTURE["rounds"][:3]
    engine.observe_round(observed(source), now=source["receiver_received_at"])
    stored = engine.sessions[session["session_id"]]
    stored["status"] = "COMPLETED"
    stored["observed_rounds"] = stored["window_size"]

    engine.observe_round(observed(target), now=target["receiver_received_at"])
    settled = engine.session(session["session_id"])
    assert settled["selections"][0]["status"] in {"WIN", "LOSS"}
    assert settled["selections"][0]["target_round_id"] == target["round_id"]
    assert settled["observed_rounds"] == settled["window_size"]

    last_received = settled["last_received_round"]["round_id"]
    engine.observe_round(observed(following), now=following["receiver_received_at"])
    after_following = engine.session(session["session_id"])
    assert after_following["last_received_round"]["round_id"] == last_received
    assert after_following["observed_rounds"] == after_following["window_size"]


def test_reset_session_does_not_accept_settlement_or_observation_events():
    engine, session = new_session("ALL_RULES_TOGETHER", ["SOURCE_H_MULT_2_3"])
    source, target = FIXTURE["rounds"][:2]
    engine.observe_round(observed(source), now=source["receiver_received_at"])
    engine.session_action(session["session_id"], "reset", now="2026-10-18T00:00:00+00:00")
    before = engine.session(session["session_id"])

    engine.observe_round(observed(target), now=target["receiver_received_at"])
    after = engine.session(session["session_id"])
    assert after["status"] == "RESET"
    assert after["selections"][0]["status"] == "PENDING"
    assert after["observed_rounds"] == before["observed_rounds"]
    assert after["last_received_round"] == before["last_received_round"]


def test_multiple_distinct_selections_for_one_target_settle_once_each_and_match_status_counts():
    boundary = {"stream_id": "multi-target", "round_id": "boundary", "round_index": 1,
                "stream_sequence": 1, "multiplier": 1.5,
                "event_type": "round.persisted", "durability": "COMMITTED",
                "continuity_status": "VERIFIED"}
    engine = RulesEngine()
    session = engine.start_session(mode="ALL_RULES_TOGETHER", rule_ids=[], boundary=boundary,
        options={"window_size": 10, "fixed_stake_rwf": 100}, now="2026-10-18T00:00:00+00:00")
    stored = engine.sessions[session["session_id"]]
    for decision_id, stake in (("decision-one", 100), ("decision-two", 175)):
        stored["selections"].append({
            "selection_id": decision_id, "decision_id": decision_id,
            "session_id": session["session_id"],
            "source_round": {"streamId": "multi-target", "roundId": "source", "roundIndex": 1},
            "source_round_id": "source",
            "expected_target": {"streamId": "multi-target", "roundIndex": 2, "roundId": None},
            "target_round_id": None, "contributing_rule_ids": ["SOURCE_H_MULT_2_3"],
            "matched_rule_ids": ["SOURCE_H_MULT_2_3"], "stake_rwf": stake,
            "payout_multiplier": 2.0, "status": "PENDING", "result": "PENDING",
            "settlement_status": "PENDING", "actual_multiplier": None,
            "actual_target": None, "resolved_at": None,
        })
    target = {"stream_id": "multi-target", "round_id": "target", "observation_id": "target",
              "round_index": 2, "stream_sequence": 2, "multiplier": 2.0,
              "event_type": "round.persisted", "durability": "COMMITTED",
              "continuity_status": "VERIFIED", "observed_at": "2026-10-18T00:00:01+00:00"}
    output = engine.observe_round(target, now="2026-10-18T00:00:02+00:00")
    detail = engine.session(session["session_id"])
    summary = engine.status()["active_session"]
    assert len(output["target_settlements"]) == 2
    assert [row["status"] for row in detail["selections"]] == ["WIN", "WIN"]
    assert [row["payout_rwf"] for row in detail["selections"]] == [200, 350]
    assert [row["profit_loss_rwf"] for row in detail["selections"]] == [100, 175]
    assert detail["net_profit_rwf"] == summary["net_profit_rwf"] == 275
    assert detail["results"] == summary["results"] == {"WIN": 2, "LOSS": 0, "PENDING": 0}
    assert detail["net_outcome"] == summary["net_outcome"] == 2
    assert summary["selection_count"] == 2
    assert detail["last_settled_target"]["settlement_count"] == 2
    assert detail["last_received_round"]["round_id"] == target["round_id"]
    assert detail["last_event_processing_latency_ms"] is not None
    assert detail["last_target_settlement_processing_latency_ms"] is not None
    metrics = engine.status()["event_latency_metrics"]["stages_ms"]
    assert metrics["rules_engine_processing_ms"]["samples"] == 1
    assert metrics["target_settlement_processing_ms"]["samples"] == 1
    assert metrics["target_settlement_durable_ms"]["samples"] == 1
    assert metrics["target_settlement_durable_ms"]["p50_ms"] >= 0

    duplicate = engine.observe_round(target, now="2026-10-18T00:00:03+00:00")
    after_duplicate = engine.session(session["session_id"])
    assert duplicate["target_settlements"] == []
    assert after_duplicate["net_profit_rwf"] == 275
    assert after_duplicate["results"] == {"WIN": 2, "LOSS": 0, "PENDING": 0}


def test_exact_2x_win_payout_uses_selection_stake_and_net_profit_is_stake():
    boundary = {"stream_id": "payout-stream", "round_id": "boundary", "round_index": 1,
                "stream_sequence": 1, "multiplier": 1.5,
                "event_type": "round.persisted", "durability": "COMMITTED",
                "continuity_status": "VERIFIED"}
    engine = RulesEngine()
    session = engine.start_session(mode="ALL_RULES_TOGETHER", rule_ids=[], boundary=boundary,
        options={"window_size": 5, "fixed_stake_rwf": 175}, now="2026-10-18T00:00:00+00:00")
    engine.sessions[session["session_id"]]["selections"].append({
        "selection_id": "exact-two-selection", "decision_id": "exact-two-selection",
        "source_round": {"roundId": "source"},
        "expected_target": {"streamId": "payout-stream", "roundIndex": 2},
        "stake_rwf": 175, "payout_multiplier": 2.0, "status": "PENDING",
        "result": "PENDING", "settlement_status": "PENDING", "contributing_rule_ids": [],
    })
    target = {"stream_id": "payout-stream", "round_id": "target", "observation_id": "target",
              "round_index": 2, "stream_sequence": 2, "multiplier": 2.0,
              "event_type": "round.persisted", "durability": "COMMITTED",
              "continuity_status": "VERIFIED", "observed_at": "2026-10-18T00:00:01+00:00"}
    engine.observe_round(target, now="2026-10-18T00:00:02+00:00")
    detail = engine.session(session["session_id"])
    selection = detail["selections"][0]
    assert selection["status"] == "WIN"
    assert selection["payout_rwf"] == 350
    assert selection["profit_loss_rwf"] == 175
    assert detail["net_profit_rwf"] == 175


def test_target_settlement_survives_persistence_failure_and_restart_retry(tmp_path, monkeypatch):
    repository = Repository(tmp_path / "target-settlement.sqlite3")
    repository.init()
    engine, session = new_session("ALL_RULES_TOGETHER", ["SOURCE_H_MULT_2_3"],
                                  persistence=repository)
    source, target = FIXTURE["rounds"][:2]
    engine.observe_round(observed(source), now=source["receiver_received_at"])
    save_snapshot = repository.save_rules_engine_snapshot
    attempts = {"count": 0}

    def fail_once(snapshot, saved_at):
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise RuntimeError("temporary snapshot write failure")
        return save_snapshot(snapshot, saved_at)

    monkeypatch.setattr(repository, "save_rules_engine_snapshot", fail_once)
    with pytest.raises(RuntimeError, match="temporary snapshot write failure"):
        engine.observe_round(observed(target), now=target["receiver_received_at"])

    recovered = RulesEngine(persistence=repository)
    recovered.observe_round(observed(target), now=target["receiver_received_at"])
    detail = recovered.session(session["session_id"])
    assert detail["selections"][0]["status"] == "LOSS"
    assert detail["net_profit_rwf"] == -100
    restarted = RulesEngine(persistence=repository)
    final = restarted.session(session["session_id"])
    assert final["selections"][0]["status"] == "LOSS"
    assert final["net_profit_rwf"] == -100
    assert final["results"] == {"WIN": 0, "LOSS": 1, "PENDING": 0}


@pytest.mark.parametrize("status", ["COMPLETED", "SETTLING", "PAUSED"])
def test_saved_shadow_session_delete_removes_results_and_keeps_audit(tmp_path, status):
    repository = Repository(tmp_path / f"delete-shadow-{status.lower()}.sqlite3")
    repository.init()
    engine, session = new_session("ALL_RULES_TOGETHER", ["SOURCE_H_MULT_2_3"],
                                  persistence=repository)
    source = FIXTURE["rounds"][0]
    engine.observe_round(observed(source), now=source["receiver_received_at"])
    # A saved session cannot be removed while its observed event is still
    # awaiting the matching durable commit confirmation.
    engine.observe_round(committed(source), now=source["database_committed_at"])
    stored = engine.sessions[session["session_id"]]
    stored.update({"status": status, "selection_status": "STOPPED", "ended_at": source["receiver_received_at"]})
    engine._persist()
    assert engine.session(session["session_id"])["results"]["PENDING"] == 1

    deleted = engine.delete_session(session["session_id"], now="2026-10-18T12:00:00+00:00")
    assert deleted["ok"] is True
    assert deleted["summary"]["results"]["PENDING"] == 1
    assert engine.session(session["session_id"]) is None
    assert RulesEngine(persistence=repository).session(session["session_id"]) is None

    with repository.connect() as conn:
        assert conn.execute("SELECT 1 FROM rule_sessions WHERE session_id=?",
                            (session["session_id"],)).fetchone() is None
        audit = conn.execute("SELECT payload FROM system_audit_log WHERE entity_id=? "
                             "AND event_type='RULE_SHADOW_SESSION_DELETED'",
                             (session["session_id"],)).fetchone()
        retained_intent = conn.execute("SELECT 1 FROM rule_execution_intents WHERE session_id=?",
                                       (session["session_id"],)).fetchone()
    assert audit is not None
    audit_payload = json.loads(audit["payload"])
    assert audit_payload["deleted_shadow_results"]["PENDING"] == 1
    assert audit_payload["settling_observer_stopped"] is (status == "SETTLING")
    assert retained_intent is not None


@pytest.mark.parametrize("wins,losses,expected_reason", [
    (1, 0, None), (2, 0, "WIN_LIMIT_REACHED"), (2, 1, None),
    (3, 1, "WIN_LIMIT_REACHED"), (3, 2, None), (2, 3, None),
    (1, 3, "LOSS_LIMIT_REACHED"),
])
def test_net_outcome_stop_decision_unit_examples(wins, losses, expected_reason):
    configuration = {"win_limit_enabled": True, "win_limit_count": 2,
                      "loss_limit_enabled": True, "loss_limit_count": 2}
    assert RulesEngine._net_outcome_stop_reason(wins, losses, configuration) == expected_reason


def test_disabled_or_non_boolean_limit_flags_cannot_generate_limit_stop_reasons():
    assert RulesEngine._net_outcome_stop_reason(2, 4, {
        "win_limit_enabled": False, "win_limit_count": 2,
        "loss_limit_enabled": False, "loss_limit_count": 2,
    }) is None
    assert RulesEngine._net_outcome_stop_reason(2, 0, {
        "win_limit_enabled": "false", "win_limit_count": 2,
        "loss_limit_enabled": False, "loss_limit_count": 2,
    }) is None


def test_win_and_loss_limits_can_be_enabled_independently_and_counter_dedupes():
    assert RulesEngine._net_outcome_stop_reason(
        2, 3, {"win_limit_enabled": True, "win_limit_count": 2,
               "loss_limit_enabled": False}) is None
    assert RulesEngine._net_outcome_stop_reason(
        3, 1, {"win_limit_enabled": True, "win_limit_count": 2,
               "loss_limit_enabled": False}) == "WIN_LIMIT_REACHED"
    assert RulesEngine._net_outcome_stop_reason(
        3, 1, {"win_limit_enabled": False,
               "loss_limit_enabled": True, "loss_limit_count": 2}) is None
    assert RulesEngine._net_outcome_stop_reason(
        1, 3, {"win_limit_enabled": False,
               "loss_limit_enabled": True, "loss_limit_count": 2}) == "LOSS_LIMIT_REACHED"

    selections = [
        {"selection_id": "same-selection", "status": "WIN",
         "expected_target": {"streamId": "stream", "roundIndex": 10}},
        {"selection_id": "duplicate-target", "status": "LOSS",
         "expected_target": {"streamId": "stream", "roundIndex": 10}},
        {"selection_id": "same-selection", "status": "LOSS",
         "expected_target": {"streamId": "stream", "roundIndex": 11}},
        {"selection_id": "pending", "status": "PENDING",
         "expected_target": {"streamId": "stream", "roundIndex": 12}},
    ]
    assert RulesEngine._settled_shared_result_counts({"selections": selections}) == (1, 1)


def test_duplicate_settlement_and_duplicate_target_are_counted_once():
    engine, session = _net_limit_engine(["WIN"], pending_count=0)
    stored = engine.sessions[session["session_id"]]
    duplicate = deepcopy(stored["selections"][0])
    duplicate["selection_id"] = "duplicate-selection-same-target"
    duplicate.update({"status": "PENDING", "result": "PENDING", "actual_multiplier": None,
                      "actual_target": None, "resolved_at": None})
    stored["selections"].append(duplicate)
    # Replay the same committed source: the event record prevents a second settlement.
    event = {"stream_id": "net-limit-stream", "round_id": "net-limit-round-2",
             "observation_id": "net-limit-round-2", "round_index": 2,
             "stream_sequence": 2, "multiplier": 2.1,
             "event_type": "round.persisted", "durability": "COMMITTED",
             "continuity_status": "VERIFIED", "observed_at": "2026-10-16T00:00:02+00:00"}
    engine.observe_round(event, now="2026-10-16T00:00:04+00:00")
    detail = engine.session(session["session_id"])
    assert detail["total_wins"] == 1
    assert detail["total_losses"] == 0
    assert detail["net_outcome"] == 1
    assert detail["net_profit_rwf"] == 100
    assert detail["selections"][1]["status"] == "PENDING"


def test_combined_net_limit_settings_validate_independently_and_old_risk_values_stay_inert():
    engine = RulesEngine()
    boundary = source_boundary()
    session = engine.start_session(mode="ALL_RULES_TOGETHER", rule_ids=[], boundary=boundary,
        options={"fixed_stake_rwf": 100, "win_limit_enabled": True, "win_limit_count": 2,
                 "loss_limit_enabled": True, "loss_limit_count": 3, "loss_limit_rwf": 25},
        now=FIXTURE["boundary"]["captured_at"])
    assert session["configuration_snapshot"]["win_limit_count"] == 2
    assert session["configuration_snapshot"]["loss_limit_count"] == 3
    assert session["configuration_snapshot"]["net_outcome_limit_version"] == 1
    assert "loss_limit_rwf" not in session["configuration_snapshot"]
    with pytest.raises(ValueError, match="INVALID_RESULT_COUNT_LIMIT"):
        engine.start_session(mode="ALL_RULES_TOGETHER", rule_ids=[], boundary=boundary,
            options={"loss_limit_enabled": True, "loss_limit_count": 0},
            now=FIXTURE["boundary"]["captured_at"])


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
        boundary=boundary, prestart_context=context, options={"window_size": 50, "fixed_stake_rwf": 100},
        now=FIXTURE["boundary"]["captured_at"])
    tracker = shared.start_session(mode="FIVE_RULE_TRACKER", rule_ids=["SOURCE_H_MULT_2_3"],
        boundary=boundary, prestart_context=context, options={"window_size": 50},
        now=FIXTURE["boundary"]["captured_at"])
    initial = feed(shared, FIXTURE["rounds"][0])
    assert initial["decision"] == "SESSION_SCOPED"
    assert len(shared.session(combined["session_id"])["selections"]) == 1
    assert len(shared.session(tracker["session_id"])["selections"]) == 1
    with pytest.raises(ValueError, match="SESSION_ALREADY_ACTIVE_FOR_MODE"):
        shared.start_session(mode="ALL_RULES_TOGETHER", rule_ids=["SOURCE_H_MULT_2_3"],
            boundary=boundary, prestart_context=context, options={"window_size": 50, "fixed_stake_rwf": 100})

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


def _auto_next_round(boundary, round_index, multiplier):
    return {
        "stream_id": boundary["stream_id"],
        "round_id": f"auto-next-round-{round_index}",
        "observation_id": f"auto-next-round-{round_index}",
        "round_index": round_index,
        "stream_sequence": boundary["stream_sequence"] + round_index - boundary["round_index"],
        "multiplier": multiplier,
        "receiver_received_at": "2026-10-16T00:01:00+00:00",
        "database_committed_at": "2026-10-16T00:01:01+00:00",
        "observed_at": "2026-10-16T00:01:00+00:00",
    }


def test_auto_next_after_full_window_starts_on_next_committed_round():
    engine = RulesEngine()
    engine.configure(START_CHECK_POLICY_ID, enabled=True)
    boundary = source_boundary()
    prior = engine.start_session(mode="START_CHECK", rule_ids=[START_CHECK_POLICY_ID],
        boundary=boundary, prestart_context=[],
        options={"window_size": 1, "auto_next_check_enabled": True},
        now="2026-10-16T00:00:00+00:00")
    previous = engine.sessions[prior["session_id"]]

    first = _auto_next_round(boundary, boundary["round_index"] + 1, 1.5)
    feed(engine, first)
    assert previous["status"] == "COMPLETED"
    assert previous["auto_next_armed"] is True
    assert previous.get("auto_next_session_id") is None

    next_round = _auto_next_round(boundary, boundary["round_index"] + 2, 2.1)
    feed(engine, next_round)
    clone = engine.sessions[previous["auto_next_session_id"]]
    assert clone["start_boundary"]["roundIndex"] == first["round_index"]
    assert clone["observed_rounds"] == 1
    assert clone["last_round_id"] == next_round["round_id"]
    assert clone["prestart_skipped_rounds"] == 0
    assert clone["auto_next_start_round_id"] is None


@pytest.mark.parametrize("limit_kind,multiplier,expected_reason", [
    ("win", 2.1, "WIN_LIMIT_REACHED"),
    ("loss", 1.5, "LOSS_LIMIT_REACHED"),
    ("selections", 2.1, "MAX_SELECTIONS_REACHED"),
])
def test_auto_next_on_configured_limit_starts_next_round(limit_kind, multiplier, expected_reason):
    engine = RulesEngine()
    boundary = source_boundary()
    options = {"window_size": 50, "fixed_stake_rwf": 100,
               "auto_next_on_limit_enabled": True}
    if limit_kind == "win":
        options.update({"win_limit_enabled": True, "win_limit_count": 1})
    elif limit_kind == "loss":
        options.update({"loss_limit_enabled": True, "loss_limit_count": 1})
    else:
        options["max_selections"] = 1
    session = engine.start_session(mode="ALL_RULES_TOGETHER", rule_ids=[], boundary=boundary,
        prestart_context=[],
        options=options,
        now="2026-10-16T00:00:00+00:00")
    previous = engine.sessions[session["session_id"]]
    target_index = boundary["round_index"] + 1
    previous["selections"].append({
        "selection_id": "auto-next-limit-selection", "session_id": session["session_id"],
        "source_round": {"streamId": boundary["stream_id"],
                         "roundIndex": boundary["round_index"]},
        "expected_target": {"streamId": boundary["stream_id"], "roundIndex": target_index},
        "contributing_rule_ids": [], "selected_at": "2026-10-16T00:00:01+00:00",
        "status": "PENDING", "result": "PENDING", "actual_multiplier": None,
        "actual_target": None, "resolved_at": None,
    })

    limit_round = _auto_next_round(boundary, target_index, multiplier)
    feed(engine, limit_round)
    assert previous["selection_stop_reason"] == expected_reason
    assert previous["auto_next_armed"] is True
    assert previous["auto_next_trigger"] == "LIMIT_REACHED"
    assert previous.get("auto_next_session_id") is None

    next_round = _auto_next_round(boundary, target_index + 1, 1.5)
    feed(engine, next_round)
    clone = engine.sessions[previous["auto_next_session_id"]]
    assert clone["start_boundary"]["roundIndex"] == target_index
    assert clone["observed_rounds"] == 1
    assert clone["last_round_id"] == next_round["round_id"]
    assert clone["prestart_skipped_rounds"] == 0


def test_auto_next_restore_recovers_boundary_only_from_matching_commit_evidence():
    session = {
        "auto_next_armed": True,
        "auto_next_after_round_id": "saved-boundary",
        "context": [{"stream_id": "saved-stream", "round_id": "saved-boundary",
                     "round_index": 20, "stream_sequence": 0, "multiplier": 2.1,
                     "outcome": "H"}],
        "event_records": {"saved-stream:42": {
            "persistence_status": "COMMITTED",
            "source_round": {"streamId": "saved-stream", "roundId": "saved-boundary"},
            "commit_evidence": {"event_type": "round.persisted", "durability": "COMMITTED",
                                "continuity_status": "VERIFIED", "stream_sequence": 42},
        }},
    }
    RulesEngine._recover_auto_next_boundary(session)
    assert session["auto_next_boundary"]["stream_sequence"] == 42
    assert session["auto_next_boundary"]["event_type"] == "round.persisted"
    assert session["auto_next_context"][-1]["round_id"] == "saved-boundary"

    uncommitted = deepcopy(session)
    uncommitted["auto_next_boundary"] = None
    uncommitted["event_records"]["saved-stream:42"]["persistence_status"] = "PENDING"
    RulesEngine._recover_auto_next_boundary(uncommitted)
    assert uncommitted.get("auto_next_boundary") is None


def test_max_selection_limit_stops_new_selections_but_observes_full_window():
    engine, session = new_session("ALL_RULES_TOGETHER", ["SOURCE_H_MULT_2_3"],
                                  options={"window_size": 50, "fixed_stake_rwf": 100, "max_selections": 1})
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
        options={"window_size": 50, "fixed_stake_rwf": 100}, persistence=repository)
    stopped_at = "2026-10-14T00:05:00+00:00"
    engine.session_action(session["session_id"], "stop", now=stopped_at)
    engine._persist()

    with repository.connect() as conn:
        events = conn.execute("""SELECT reason,stopped_at,payload FROM rule_session_stop_events
            WHERE session_id=?""", (session["session_id"],)).fetchall()
    assert len(events) == 1
    assert events[0]["reason"] == "MANUAL_STOP"
    assert events[0]["stopped_at"] == stopped_at
    event = json.loads(events[0]["payload"])
    assert event["source"] == "MANUAL_STOP_CONTROL"
    assert event["scope"] == "SESSION_AND_SELECTION"
    assert event["source_round"]["round_id"] == session["last_round_id"]


def test_emergency_stop_keeps_selection_stopped_and_records_its_origin():
    engine, session = _net_limit_engine([], pending_count=0)
    stopped_at = "2026-10-16T00:02:00+00:00"
    stopped = engine.session_action(session["session_id"], "emergency_stop", now=stopped_at)
    detail = engine.session(session["session_id"])
    assert stopped["status"] == "MANUALLY_STOPPED"
    assert stopped["selection_status"] == "STOPPED"
    assert detail["stop_reason"] == "EMERGENCY_STOP"
    assert detail["stop_reason_source"] == "EMERGENCY_STOP_CONTROL"
    assert detail["stop_reason_at"] == stopped_at
    assert engine.execution_authorized() is False


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
    engine.save_preset("saved-one", "ALL_RULES_TOGETHER", ["SOURCE_H_MULT_2_3"],
                       {"fixed_stake_rwf": 77, "max_observed_rounds": 50})
    restored = RulesEngine(persistence=repository)
    assert restored.status()["presets"]["saved-one"]["options"]["fixed_stake_rwf"] == 77
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


@pytest.mark.parametrize("mode,rule_id", [
    ("START_CHECK", START_CHECK_POLICY_ID),
    ("ALL_RULES_TOGETHER", COMBINED_POLICY_ID),
])
def test_h_loss_win_runs_repeated_special_skip_cycles_and_completes_on_second_win(mode, rule_id):
    engine, session = new_session(mode, [rule_id], context=[], options={"window_size": 50})
    boundary = source_boundary()
    session_id = session["session_id"]

    # L,H triggers the policy; the H target loses, and the immediate next bet wins.
    for offset, multiplier in enumerate((1.5, 2.5, 1.5, 2.5), start=1):
        feed(engine, _special_skip_round(boundary, boundary["round_index"] + offset, multiplier))
    state = engine.session(session_id)["engines"][rule_id]
    assert state["special_skip_active"] is True
    assert state["special_skip_after_round_index"] == boundary["round_index"] + 4
    assert len(engine.session(session_id)["selections"]) == 2

    # First skipped L starts the next bet. Its WIN arms the next single-round skip.
    first_skip = _special_skip_round(boundary, boundary["round_index"] + 5, 1.5)
    feed(engine, first_skip)
    assert len(engine.session(session_id)["selections"]) == 3
    feed(engine, _special_skip_round(boundary, boundary["round_index"] + 6, 2.5))
    state = engine.session(session_id)["engines"][rule_id]
    assert state["special_skip_active"] is True
    assert state["special_skip_after_round_index"] == boundary["round_index"] + 6

    # Exactly 2.00 is L only for this skip comparison, so it starts another bet.
    second_skip = _special_skip_round(boundary, boundary["round_index"] + 7, 2.0)
    feed(engine, second_skip)
    assert len(engine.session(session_id)["selections"]) == 4
    feed(engine, _special_skip_round(boundary, boundary["round_index"] + 8, 2.5))

    detail = engine.session(session_id)
    state = detail["engines"][rule_id]
    assert state["rule_state"] == "WAIT_LOW"
    assert state["special_skip_active"] is False
    assert state["selected_losses"] == 1
    assert state["selected_wins"] == 3
    assert detail["total_losses"] == 1
    assert detail["total_wins"] == 3
    selections = detail["selections"]
    assert len(selections) == 4
    assert len({row["selection_id"] for row in selections}) == 4
    assert [row["expected_target"]["roundIndex"] for row in selections] == [
        boundary["round_index"] + offset for offset in (3, 4, 6, 8)
    ]
    assert all(row["contributing_rule_ids"] == [rule_id] for row in selections)

    # Replaying the second skipped source cannot create a duplicate bet or settlement.
    feed(engine, second_skip)
    replayed = engine.session(session_id)
    assert len(replayed["selections"]) == 4
    assert replayed["total_wins"] == 3
    assert replayed["total_losses"] == 1


@pytest.mark.parametrize("mode,rule_id", [
    ("START_CHECK", START_CHECK_POLICY_ID),
    ("ALL_RULES_TOGETHER", COMBINED_POLICY_ID),
])
def test_special_skipped_h_ends_cycle_without_bet_or_counter_change(mode, rule_id):
    engine, session = new_session(mode, [rule_id], context=[], options={"window_size": 50})
    boundary = source_boundary()
    for offset, multiplier in enumerate((1.5, 2.5, 1.5, 2.5), start=1):
        feed(engine, _special_skip_round(boundary, boundary["round_index"] + offset, multiplier))

    feed(engine, _special_skip_round(boundary, boundary["round_index"] + 5, 2.01))
    detail = engine.session(session["session_id"])
    state = detail["engines"][rule_id]
    assert state["rule_state"] == "WAIT_LOW"
    assert state["special_skip_active"] is False
    assert state["selected_losses"] == 1
    assert state["selected_wins"] == 1
    assert detail["total_losses"] == 1
    assert detail["total_wins"] == 1
    assert len(detail["selections"]) == 2


@pytest.mark.parametrize("mode,rule_id", [
    ("START_CHECK", START_CHECK_POLICY_ID),
    ("ALL_RULES_TOGETHER", COMBINED_POLICY_ID),
])
def test_special_bet_loss_terminates_cycle_after_one_loss(mode, rule_id):
    engine, session = new_session(mode, [rule_id], context=[], options={"window_size": 50})
    assert session["rule_ids"] == [rule_id]
    assert engine.rule_configuration[rule_id]["enabled"] is True
    assert session["manifest"]["rules"][0]["condition"]["h_loss_win_special_skip"] is True
    boundary = source_boundary()
    # Activate H -> selected LOSS -> selected WIN -> SKIP L -> selected bet.
    multipliers = (1.5, 2.5, 1.5, 2.5, 1.5, 1.5)
    for offset, multiplier in enumerate(multipliers, start=1):
        feed(engine, _special_skip_round(boundary, boundary["round_index"] + offset, multiplier))

    detail = engine.session(session["session_id"])
    state = detail["engines"][rule_id]
    assert state["special_skip_active"] is False
    assert state["rule_state"] == "WAIT_LOW"
    assert state["special_skip_terminal_round_index"] is None
    assert state["selected_losses"] == 2
    assert state["selected_wins"] == 1
    assert detail["total_losses"] == 2
    assert detail["total_wins"] == 1
    assert len(detail["selections"]) == 3
    assert [row["expected_target"]["roundIndex"] for row in detail["selections"]] == [
        boundary["round_index"] + offset for offset in (3, 4, 6)
    ]

    # The LOSS source itself is not reprocessed after terminating; the next
    # verified L source starts the next WAIT_LOW cycle normally.
    feed(engine, _special_skip_round(boundary, boundary["round_index"] + 7, 1.5))
    state = engine.session(session["session_id"])["engines"][rule_id]
    assert state["rule_state"] == "WAIT_HIGH"
    assert len(engine.session(session["session_id"])["selections"]) == 3


@pytest.mark.parametrize("mode,rule_id,seed", [
    ("START_CHECK", START_CHECK_POLICY_ID, "H"),
    ("ALL_RULES_TOGETHER", COMBINED_POLICY_ID, None),
])
def test_normal_standard_first_win_still_completes_cycle(mode, rule_id, seed):
    engine = RulesEngine()
    state = _fresh_engine(rule_id)
    state.update({"rule_state": "PLAYING", "mode": "STANDARD", "previous_outcome": seed})

    engine._advance_selected_state({}, rule_id, state,
        {"round_index": 100}, "WIN", {}, "test-normal-win")

    assert state["rule_state"] == "WAIT_LOW"
    assert state["mode"] == "STANDARD"
    assert state["special_skip_active"] is False
    assert state["selected_wins"] == 1


@pytest.mark.parametrize("mode,rule_id,seed", [
    ("START_CHECK", START_CHECK_POLICY_ID, "H"),
    ("ALL_RULES_TOGETHER", COMBINED_POLICY_ID, None),
])
def test_normal_two_losses_still_close_cycle_without_special_skip(mode, rule_id, seed):
    engine = RulesEngine()
    state = _fresh_engine(rule_id)
    state.update({"rule_state": "PLAYING", "mode": "STANDARD", "previous_outcome": seed})
    engine._advance_selected_state({}, rule_id, state,
        {"round_index": 100}, "LOSS", {}, "test-loss-one")
    engine._advance_selected_state({}, rule_id, state,
        {"round_index": 101}, "LOSS", {}, "test-loss-two")

    assert state["rule_state"] == "WAIT_HIGH"
    assert state["mode"] == "STANDARD"
    assert state["failed_cycle_count"] == 1
    assert state["special_skip_active"] is False
    assert state["selected_losses"] == 2


@pytest.mark.parametrize("mode,rule_id,seed", [
    ("START_CHECK", START_CHECK_POLICY_ID, "H"),
    ("ALL_RULES_TOGETHER", COMBINED_POLICY_ID, None),
])
def test_two_bet_wins_does_not_enter_standard_special_skip(mode, rule_id, seed):
    engine = RulesEngine()
    state = _fresh_engine(rule_id)
    state.update({"rule_state": "PLAYING", "mode": "TWO_BET_WINS", "previous_outcome": seed})
    engine._advance_selected_state({}, rule_id, state,
        {"round_index": 100}, "LOSS", {}, "test-two-bet-loss")
    engine._advance_selected_state({}, rule_id, state,
        {"round_index": 101}, "WIN", {}, "test-two-bet-win")

    assert state["mode"] == "TWO_BET_WINS"
    assert state["special_skip_active"] is False
    assert state["rule_state"] == "PLAYING"


def test_shared_select_creates_one_durable_blocked_intent_per_target_and_panel(tmp_path):
    repository = Repository(tmp_path / "rules-intents.sqlite3")
    repository.init()
    rule_ids = [*RESEARCH_IDS, COMBINED_POLICY_ID]
    engine, session = new_session("ALL_RULES_TOGETHER", rule_ids,
        options={"window_size": 50, "fixed_stake_rwf": 100}, persistence=repository)
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
    assert len({(item["target_stream_id"], item["target_round_index"], item["panel"])
                for item in intents}) == len(intents)
    assert all(item["cashout_target"] == 2.0 for item in intents)
    assert all(item["execution_status"] == "BLOCKED_READ_ONLY" for item in intents)
    assert all(item["expires_at"] is None for item in intents)
    assert all("BETTING_DEADLINE_UNVERIFIED" in item["blockers"] for item in intents)
    assert all(item["selected_rule_ids"] for item in intents)
    original_ids = {item["intent_id"] for item in intents}

    restarted = RulesEngine(persistence=repository)
    restarted._persist()
    assert {item["intent_id"] for item in repository.list_rule_execution_intents(100)} == original_ids


def test_five_independent_true_rules_share_one_select_and_one_intent(tmp_path):
    repository = Repository(tmp_path / "five-rule-shared-intent.sqlite3")
    repository.init()
    engine, session = new_session("FIVE_RULE_TRACKER", RESEARCH_IDS,
        options={"window_size": 50, "fixed_stake_rwf": 100}, persistence=repository)

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
    assert intents[0]["cashout_target"] == 2.0
    assert intents[0]["target_round_id"] is None
    assert intents[0]["execution_status"] == "BLOCKED_READ_ONLY"
    assert "TARGET_ROUND_ID_UNVERIFIED" in intents[0]["blockers"]
    assert output["sessions"][0]["decision"]["action"] == "SELECT"


def test_balance_change_and_realized_pl_require_platform_currency_and_reconciled_transactions(tmp_path):
    repository = Repository(tmp_path / "rules-financials.sqlite3")
    repository.init()
    # A status bit and a multiplier-derived P/L are not settlement evidence.
    unverified_execution = {
        "execution_id": "execution-unverified", "decision_id": "decision-unverified",
        "target_round_id": "target-unverified", "profile": "PROFILE_A", "bet_amount": 100,
        "cashout_target": 2.0, "status": "RECONCILED", "simulated": False,
        "created_at": "2026-10-14T00:00:00+00:00", "updated_at": "2026-10-14T00:00:01+00:00",
        "session_id": "platform-session-one", "profit_loss": 999.0, "result": "WIN",
    }
    assert repository.create_execution(unverified_execution) is True
    execution = {
        "execution_id": "execution-one", "decision_id": "decision-one",
        "target_round_id": "target-one", "profile": "PROFILE_A", "bet_amount": 100,
        "cashout_target": 2.0, "status": "RECONCILED", "simulated": False,
        "created_at": "2026-10-14T00:00:00+00:00", "updated_at": "2026-10-14T00:00:01+00:00",
        "session_id": "platform-session-one", "profit_loss": 100.0, "result": "WIN",
        "balance_after": 1100.0, "balance_status": "VERIFIED",
        "platform_evidence": {
            "platform_status": "ACCEPTED", "platform_evidence": True,
            "platform_receipt_id": "accept-one", "settlement_receipt_id": "settle-one",
            "target_round_id": "target-one", "decision_id": "decision-one",
            "session_identity": "platform-session-one", "accepted_stake": 100.0,
            "accepted_currency": "USD", "platform_observed_at": "2026-10-14T00:00:00Z",
            "settled_at": "2026-10-14T00:00:01Z", "bet_result": "WIN", "payout": 200.0,
            "balance_before_verified": True, "balance_provider_identity": "provider-one",
            "balance_session_identity": "platform-session-one",
            "balance_observed_at": "2026-10-14T00:00:02Z", "balance_currency": "USD",
            "platform_observed_balance": 1100.0,
        },
    }
    assert repository.create_execution(execution) is True
    first = repository.record_platform_balance_observation({
        "balance": 1000.0, "displayed_amount": 1000.0, "currency": "USD",
        "verified": True, "session_identity": "platform-session-one",
        "observed_at": "2026-10-14T00:00:02+00:00",
    })
    second = repository.record_platform_balance_observation({
        "balance": 1050.0, "displayed_amount": 1050.0, "currency": "USD",
        "verified": True, "session_identity": "platform-session-one",
        "observed_at": "2026-10-14T00:00:03+00:00",
    })
    financials = repository.rules_session_financials("platform-session-one")

    assert first and second
    assert financials["starting_balance"] == 1000.0
    assert financials["current_balance"] == 1050.0
    assert financials["balance_change"] == 50.0
    assert financials["realized_betting_pl"] == 100.0
    assert financials["win_count"] == 1
    assert financials["loss_count"] == 0
    assert financials["reconciled_bets"] == 1


def test_balance_change_is_unknown_when_currency_changes_or_was_unknown_at_start(tmp_path):
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
    assert repository.rules_session_financials("currency-session")["balance_change"] is None

    unknown_start = "unknown-currency-session"
    repository.record_platform_balance_observation({
        "balance": 1000.0, "currency": None, "verified": True,
        "application_session_id": unknown_start, "observed_at": "2026-10-14T00:00:02Z",
    })
    repository.record_platform_balance_observation({
        "balance": 1100.0, "currency": "BIF", "verified": True,
        "application_session_id": unknown_start, "observed_at": "2026-10-14T00:00:03Z",
    })
    financials = repository.rules_session_financials(unknown_start)
    assert financials["starting_balance_currency"] is None
    assert financials["balance_change"] is None


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
    special_skip_seen = False
    for index, row in enumerate(FIXTURE["rounds"]):
        feed(combined_engine, row)
        feed(five_engine, row)
        combined_evaluations = combined_engine.session(combined_session["session_id"])["evaluations"][-1]["rule_evaluations"]
        combined_actual = [{"rule_id": item["rule_id"], "eligible": item["eligible"], "status": item["status"]}
                           for item in combined_evaluations]
        policy_evaluation = next(item for item in combined_evaluations
                                 if item["rule_id"] == COMBINED_POLICY_ID)
        transition = policy_evaluation["proposal"].get("relevantInputs", {}).get("transition") or {}
        if (policy_evaluation["state_snapshot"]["after"].get("special_skip_active")
                or transition.get("type", "").startswith("SPECIAL_SKIP_")):
            special_skip_seen = True
        if special_skip_seen:
            # This policy intentionally diverges from the frozen source only
            # after its new H -> LOSS -> WIN special-skip transition begins.
            combined_actual = [item for item in combined_actual if item["rule_id"] != COMBINED_POLICY_ID]
            combined_expected = [item for item in oracle[index]["combined"]
                                 if item["rule_id"] != COMBINED_POLICY_ID]
        else:
            combined_expected = oracle[index]["combined"]
        assert combined_actual == combined_expected, f"combined source mismatch at #{row['round_index']}"
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
    special_skip_seen = False
    for index, row in enumerate(FIXTURE["rounds"]):
        feed(start_engine, row)
        feed(legacy_engine, row)
        start_record = start_engine.session(start_session["session_id"])["evaluations"][-1]
        legacy_record = legacy_engine.session(legacy_session["session_id"])["evaluations"][-1]
        start_policy_evaluation = next(item for item in start_record["rule_evaluations"]
                                       if item["rule_id"] == START_CHECK_POLICY_ID)
        transition = start_policy_evaluation["proposal"].get("relevantInputs", {}).get("transition") or {}
        if (start_policy_evaluation["state_snapshot"]["after"].get("special_skip_active")
                or transition.get("type", "").startswith("SPECIAL_SKIP_")):
            special_skip_seen = True
        if not special_skip_seen:
            assert (start_record["decision"]["action"] == "SELECT") == oracle[index]["start_check"], \
                f"Start Check source mismatch at #{row['round_index']}"
        assert (legacy_record["decision"]["action"] == "SELECT") == oracle[index]["legacy_tracker"], \
            f"Legacy tracker source mismatch at #{row['round_index']}"


def test_preset_saves_net_outcome_limits_and_drops_retired_financial_loss_controls():
    engine = RulesEngine()
    saved = engine.save_preset(
        "fixed-stake",
        "ALL_RULES_TOGETHER",
        [],
        {"fixed_stake_rwf": 150, "win_limit_enabled": True, "win_limit_count": 2,
         "loss_limit_enabled": True, "loss_limit_count": 3,
         "loss_limit_rwf": 25, "reconciled_loss_count_limit": 2},
    )

    assert saved["options"] == {"fixed_stake_rwf": 150, "win_limit_enabled": True,
        "win_limit_count": 2, "loss_limit_enabled": True, "loss_limit_count": 3,
        "net_outcome_limit_version": 1, "auto_next_check_enabled": False,
        "auto_next_on_limit_enabled": False}
    assert engine.status()["presets"]["fixed-stake"]["options"] == saved["options"]


def test_public_rules_payload_hides_legacy_risk_gates_without_mutating_history():
    historical = {
        "execution_status": "RISK_WAITING",
        "profile": "PROFILE_A",
        "requested_stake": 100,
        "blockers": ["VERIFIED_BALANCE_AND_SUFFICIENT_FUNDS_REQUIRED",
                     "CASHOUT_PLATFORM_ACKNOWLEDGEMENT_UNVERIFIED"],
        "risk_profile": {"max_drawdown": 5},
    }

    public = _without_retired_risk_data(historical)

    assert public == {
        "execution_status": "BLOCKED_READ_ONLY",
        "requested_stake": 100,
        "blockers": ["CASHOUT_PLATFORM_ACKNOWLEDGEMENT_UNVERIFIED"],
    }
    assert historical["execution_status"] == "RISK_WAITING"
    assert "VERIFIED_BALANCE_AND_SUFFICIENT_FUNDS_REQUIRED" in historical["blockers"]


def test_public_rules_payload_exposes_only_versioned_net_loss_limits():
    old = _without_retired_risk_data({"loss_limit_enabled": True, "loss_limit_count": 2})
    current = _without_retired_risk_data({"net_outcome_limit_version": 1,
        "loss_limit_enabled": True, "loss_limit_count": 2, "loss_limit_rwf": 25})
    assert old == {}
    assert current == {"net_outcome_limit_version": 1,
                       "loss_limit_enabled": True, "loss_limit_count": 2}


def test_restore_does_not_reactivate_legacy_loss_risk_settings():
    engine = RulesEngine()
    engine._restore({"sessions": {"legacy": {"session_id": "legacy",
        "configuration_snapshot": {"loss_limit_enabled": True, "loss_limit_count": 1}}},
        "presets": {"legacy": {"options": {"loss_limit_enabled": True, "loss_limit_count": 1}}}})
    assert "loss_limit_enabled" not in engine.sessions["legacy"]["configuration_snapshot"]
    assert "loss_limit_enabled" not in engine.presets["legacy"]["options"]


def test_restore_clears_stale_disabled_limit_stop_but_retains_historical_event():
    engine, session = _net_limit_engine(["WIN", "WIN", "LOSS", "LOSS", "LOSS", "LOSS"],
        pending_count=0, win_enabled=False, loss_enabled=False)
    stored = engine.sessions[session["session_id"]]
    stored.update({"selection_status": "STOPPED", "selection_stop_reason": "WIN_LIMIT_REACHED",
                   "selection_stopped_at": "2026-10-16T00:00:03+00:00"})
    restored = RulesEngine()
    restored._restore(engine._snapshot())

    detail = restored.session(session["session_id"])
    assert detail["selection_status"] == "ACTIVE"
    assert detail["status"] == "RUNNING"
    assert detail["selection_stop_reason"] is None
    assert detail["stop_reason"] is None
    assert detail["legacy_stop_reason"] == "WIN_LIMIT_REACHED"
    assert detail["legacy_stop_reason_status"] == "UNVERIFIED_DISABLED_OR_UNMET_LIMIT"
    assert any(event["reason"] == "WIN_LIMIT_REACHED" for event in detail["stop_history"])


def test_restore_does_not_surface_an_invalid_session_limit_as_current_stop():
    engine, session = _net_limit_engine(["WIN", "LOSS"], pending_count=0,
        win_enabled=False, loss_enabled=False)
    stored = engine.sessions[session["session_id"]]
    stored.update({"stop_reason": "WIN_LIMIT_REACHED", "stop_source": "NET_OUTCOME_LIMIT",
                   "stop_at": "2026-10-16T00:00:03+00:00", "status": "RUNNING"})

    restored = RulesEngine()
    restored._restore(engine._snapshot())
    detail = restored.session(session["session_id"])

    assert detail["status"] == "RUNNING"
    assert detail["stop_reason"] is None
    assert detail["legacy_stop_reason"] == "WIN_LIMIT_REACHED"
    assert any(event["reason"] == "WIN_LIMIT_REACHED" for event in detail["stop_history"])


def test_restore_persists_cleared_limit_stop_without_deleting_its_history():
    engine, session = _net_limit_engine(["WIN", "LOSS"], pending_count=0,
        win_enabled=False, loss_enabled=False)
    stored = engine.sessions[session["session_id"]]
    stored.update({"selection_status": "STOPPED", "selection_stop_reason": "WIN_LIMIT_REACHED",
                   "selection_stopped_at": "2026-10-16T00:00:03+00:00"})
    snapshot = engine._snapshot()

    class Persistence:
        def load_application_state(self, _key):
            return deepcopy(snapshot)

        def save_rules_engine_snapshot(self, payload, _updated_at):
            self.saved = deepcopy(payload)

    repository = Persistence()
    RulesEngine(persistence=repository)
    restored_session = repository.saved["sessions"][session["session_id"]]

    assert restored_session["status"] == "RUNNING"
    assert restored_session["selection_status"] == "ACTIVE"
    assert restored_session["selection_stop_reason"] is None
    assert any(event["reason"] == "WIN_LIMIT_REACHED"
               for event in restored_session["stop_history"])


def test_restore_marks_legacy_stops_without_timestamps_as_unverified():
    engine, session = _net_limit_engine([], pending_count=0)
    stored = engine.sessions[session["session_id"]]
    stored.update({"selection_status": "STOPPED", "selection_stop_reason": "MANUAL_STOP"})
    restored = RulesEngine()
    restored._restore(engine._snapshot())

    detail = restored.session(session["session_id"])
    assert detail["stop_reason"] == "MANUAL_STOP"
    assert detail["stop_reason_source"] == "LEGACY_UNVERIFIED"
    assert detail["stop_reason_at"] is None
    assert detail["legacy_stop_timestamp_status"] == "UNAVAILABLE"
