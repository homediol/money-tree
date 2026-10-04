from datetime import datetime, timedelta, timezone

import pandas as pd

import app.ml.opportunity_v4 as opportunity_v4
import app.api.opportunities as opportunities_api
from app.ml.opportunity_v4 import OpportunityStabilityV4


def _ranked(index, score, hit):
    item = {"target_round_index": index + 1}
    outcome = {"target_round_id": f"round-{index + 1}",
               "actual_multiplier": 2.5 if hit else 1.3}
    return score, hit, item, outcome


def test_alignment_status_requires_ten_consecutive_proof_backed_live_rounds():
    runtime = {
        "alignment_audit_version": opportunity_v4.REAL_DATA_ALIGNMENT_AUDIT_VERSION,
        "alignment_audit_rounds": [
            {"round_index": index, "collector_round_id": f"r{index}",
             "postgres_round_id": f"r{index}", "assessment_source_round_id": f"r{index}",
             "assessment_source_round_index": index, "pre_outcome_proof_verified": True,
             "configuration_hash": "frozen-hash"}
            for index in range(100, 110)
        ],
    }

    assert opportunity_v4.real_data_alignment_snapshot(runtime)["status"] == "VERIFIED_10_ROUND"

    runtime["alignment_audit_rounds"][5]["round_index"] = 106
    status = opportunity_v4.real_data_alignment_snapshot(runtime)
    assert status["status"] == "UNVERIFIED"
    assert status["consecutive_rounds"] == 4


def test_alignment_accepts_immutable_unscored_warmup_assessments_as_data_proof():
    runtime = {
        "alignment_audit_version": opportunity_v4.REAL_DATA_ALIGNMENT_AUDIT_VERSION,
        "alignment_audit_rounds": [
            {"round_index": index, "collector_round_id": f"r{index}",
             "postgres_round_id": f"r{index}", "assessment_source_round_id": f"r{index}",
             "assessment_source_round_index": index, "pre_outcome_proof_verified": True,
             "assessment_immutable": True, "scorable": False,
             "failed_gate": "CONTINUITY_WARMUP", "warmup_rounds": index - 99,
             "configuration_hash": "frozen-hash"}
            for index in range(100, 110)
        ],
    }

    result = opportunity_v4.real_data_alignment_snapshot(runtime)
    assert result["status"] == "VERIFIED_10_ROUND"
    assert result["consecutive_rounds"] == 10


def test_alignment_stays_verified_when_the_next_rolling_batch_starts():
    runtime = {
        "alignment_audit_version": opportunity_v4.REAL_DATA_ALIGNMENT_AUDIT_VERSION,
        "alignment_tracker_version": 1,
        "alignment_audit_rounds": [
            {"round_index": 210, "collector_round_id": "r210",
             "postgres_round_id": "r210", "assessment_source_round_id": "r210",
             "assessment_source_round_index": 210, "pre_outcome_proof_verified": True,
             "configuration_hash": "frozen-hash"}
        ],
        "alignment_batch_progress": 1,
        "alignment_completed_batches": 7,
        "alignment_passed_batches": 7,
        "alignment_failed_batches": 0,
        "alignment_had_verified_batch": True,
        "alignment_last_completed_batch": {
            "start_round_index": 200, "end_round_index": 209,
            "rounds": 10, "result": "PASS",
        },
        "alignment_last_pass_at": "2026-10-04T10:00:00+00:00",
    }

    result = opportunity_v4.real_data_alignment_snapshot(runtime)

    assert result["display_status"] == "VERIFIED"
    assert result["current_batch_progress"] == 1
    assert result["completed_batches"] == 7
    assert result["passed_batches"] == 7
    assert result["failed_batches"] == 0
    assert result["last_completed_batch"]["end_round_index"] == 209


def test_alignment_degrades_when_a_new_failure_follows_the_last_pass():
    runtime = {
        "alignment_audit_version": opportunity_v4.REAL_DATA_ALIGNMENT_AUDIT_VERSION,
        "alignment_tracker_version": 1,
        "alignment_audit_rounds": [],
        "alignment_batch_progress": 0,
        "alignment_had_verified_batch": True,
        "alignment_last_pass_at": "2026-10-04T10:00:00+00:00",
        "alignment_last_failure": {
            "failed_round_index": 211, "reason": "MISSING_ASSESSMENT",
            "detected_at": "2026-10-04T10:01:00+00:00",
        },
        "alignment_last_reset": {
            "previous_progress": 10, "current_progress": 0,
            "round_index": 211, "reason": "MISSING_ASSESSMENT",
        },
    }

    result = opportunity_v4.real_data_alignment_snapshot(runtime)

    assert result["display_status"] == "DEGRADED"
    assert result["last_failure"]["reason"] == "MISSING_ASSESSMENT"
    assert result["reset"]["previous_progress"] == 10


def test_scoring_warmup_reports_real_progress_without_making_rounds_scorable():
    result = opportunity_v4.scoring_warmup_snapshot([{
        "round_index": 19419, "scorable": False, "assessment_immutable": True,
        "failed_gate": "CONTINUITY_WARMUP",
        "feature_snapshot": {"verified_segment_rounds": 11, "required_rounds": 102},
    }])

    assert result == {
        "status": "WARMING_UP", "verified_rounds": 11, "required_rounds": 102,
        "remaining_rounds": 91, "source_round_index": 19419,
        "reason": "11/102 verified contiguous rounds; 91 more are required before frozen scoring.",
    }


def test_scoring_warmup_ready_requires_pre_outcome_order_proof():
    assessment = {
        "round_index": 200, "target_round_index": 201, "round_id": "round-200",
        "scorable": True, "assessment_immutable": True,
        "model_configuration_hash": "frozen-hash",
        "feature_snapshot": {"verified_segment_rounds": 102},
    }
    result = opportunity_v4.scoring_warmup_snapshot([assessment], "frozen-hash")
    assert result["status"] == "WAITING"

    assessment["round_order_proof"] = {
        "assessment_source_round_index": 200,
        "assessment_source_round_id": "round-200",
        "latest_round_at_assessment_commit_index": 200,
        "latest_round_at_assessment_commit_id": "round-200",
        "target_outcome_round_index": 201,
        "source_was_latest_at_assessment_commit": True,
        "target_absent_at_assessment_commit": True,
        "proof_method": "postgres_advisory_transaction_lock",
    }
    result = opportunity_v4.scoring_warmup_snapshot([assessment], "frozen-hash")
    assert result["status"] == "READY"


def test_live_response_reports_authentication_pause_instead_of_catchup(monkeypatch):
    class Engine:
        @staticmethod
        def live_dashboard():
            return {"observer_active": True, "observer_state": "CATCHING_UP",
                    "observer_reason": "processing persisted real rounds"}

    monkeypatch.setattr(opportunities_api, "_collector_runtime", lambda _live: {
        "status": "STALE", "reason": "authentication is not verified"})
    result = opportunities_api._frozen_live_response(None, Engine())

    assert result["observer_active"] is False
    assert result["observer_state"] == "PAUSED_REQUIRES_OPERATOR"
    assert result["observer_reason"] == "authentication is not verified"
    assert result["pipeline_state"] == "LIVE_OBSERVATION_PAUSED"


def test_top4_experiment_selects_four_frozen_scores_and_reports_all_windows():
    rows = [_ranked(i, 1.0 if i < 4 else 0.0, i in {0, 2, 50}) for i in range(100)]
    result = OpportunityStabilityV4._top4_experiment(rows, permutations=100)
    window = result["window_sizes"]["100"]

    assert result["status"] == "READY"
    assert window["windows_completed"] == 1
    assert window["selected"] == 4
    assert [row["target_round_index"] for row in window["windows"][0]["selected"]] == [1, 2, 3, 4]
    assert [row["result"] for row in window["windows"][0]["selected"]] == ["TRUE", "FALSE", "TRUE", "FALSE"]
    assert window["true"] == 2
    assert window["false"] == 2
    assert window["window_hit_distribution"] == {
        "4/4 windows": 0, "3/4 windows": 0, "2/4 windows": 1,
        "1/4 windows": 0, "0/4 windows": 0,
    }
    assert window["rank_performance"]["rank_1"]["hit_rate"] == 1.0
    assert window["random_selection"]["selection"].startswith("uniform random 4")


def test_top4_experiment_omits_incomplete_or_noncontiguous_windows():
    rows = [_ranked(i, float(i), i % 2 == 0) for i in range(99)]
    result = OpportunityStabilityV4._top4_experiment(rows, permutations=10)
    assert result["window_sizes"]["100"]["windows_completed"] == 0

    rows = [_ranked(i, float(i), i % 2 == 0) for i in range(100)]
    rows[50] = _ranked(150, 50.0, True)
    result = OpportunityStabilityV4._top4_experiment(rows, permutations=10)
    assert result["window_sizes"]["100"]["windows_completed"] == 0


def test_top4_picks_need_not_be_consecutive_inside_a_complete_window():
    selected_positions = {7, 29, 64, 93}
    rows = [_ranked(i, 1.0 if i in selected_positions else 0.0, i % 2 == 1)
            for i in range(100)]
    result = OpportunityStabilityV4._top4_experiment(rows, permutations=10)
    window = result["window_sizes"]["100"]["windows"][0]

    assert window["start_target_round_index"] == 1
    assert window["end_target_round_index"] == 100
    assert [row["target_round_index"] for row in window["selected"]] == [8, 30, 65, 94]


def test_resolved_outcome_requires_immutable_assessment_commit_order_proof():
    assessment = {
        "assessment_immutable": True,
        "round_id": "source-10",
        "round_index": 10,
        "model_configuration_hash": "frozen-hash",
        "round_order_proof": {
            "assessment_source_round_index": 10,
            "assessment_source_round_id": "source-10",
            "latest_round_at_assessment_commit_index": 10,
            "latest_round_at_assessment_commit_id": "source-10",
            "target_outcome_round_index": 11,
            "source_was_latest_at_assessment_commit": True,
            "target_absent_at_assessment_commit": True,
            "proof_method": "postgres_advisory_transaction_lock",
        },
    }
    outcome = {
        "target_round_id": "target-11",
        "target_round_index": 11,
        "round_order_proof": {
            "assessment_source_round_index": 10,
            "target_outcome_round_index": 11,
            "consecutive_indices": True,
            "assessment_inserted_while_source_latest": True,
            "target_absent_at_assessment_commit": True,
        },
    }

    assert OpportunityStabilityV4._order_proof_verified(assessment, outcome, "frozen-hash")
    assessment["round_order_proof"]["target_absent_at_assessment_commit"] = False
    assert not OpportunityStabilityV4._order_proof_verified(assessment, outcome, "frozen-hash")


def test_continuity_reports_warmup_as_not_scorable_and_keeps_unscored_slot_distinct():
    class Repository:
        database_url = "postgresql://redacted"

        @staticmethod
        def load_application_state(_key):
            return {"configuration_hash": "frozen-hash"}

        @staticmethod
        def round_continuity_range(first, last):
            return {index: {"continuity_verified": True, "gap_before": False,
                            "identity_confidence": "OVERLAP_VERIFIED_ORDER_ONLY"}
                    for index in range(first, last + 1)}

    def assessment(assessment_id, source_index, *, scorable=True, failed_gate=None):
        return {"assessment_id": assessment_id, "round_id": f"round-{source_index}",
                "round_index": source_index, "target_round_index": source_index + 1,
                "assessment_immutable": True, "model_configuration_hash": "frozen-hash",
                "scorable": scorable, "failed_gate": failed_gate,
                "opportunity_score": 0.7 if scorable else None}

    def outcome(assessment_id, source_index):
        return {"assessment_id": assessment_id, "target_round_id": f"round-{source_index + 1}",
                "target_round_index": source_index + 1,
                "round_order_proof": {
                    "assessment_source_round_index": source_index,
                    "target_outcome_round_index": source_index + 1,
                    "consecutive_indices": True,
                    "assessment_inserted_while_source_latest": True,
                    "target_absent_at_assessment_commit": True,
                }}

    assessments = [assessment("resolved-a", 1),
                   assessment("warmup", 2, scorable=False, failed_gate="CONTINUITY_WARMUP"),
                   assessment("resolved-b", 4)]
    report = OpportunityStabilityV4._resolved_continuity(
        assessments, [outcome("resolved-a", 1), outcome("resolved-b", 4)], Repository())

    assert report["break_reason_counts"] == {"OTHER": 1, "NO_ASSESSMENT": 1}
    assert report["breaks"][0]["reasons"] == [
        {"reason": "OTHER", "first_target_index": 3, "last_target_index": 3, "rounds": 1,
         "detail": "ASSESSMENT_NOT_SCORABLE:CONTINUITY_WARMUP"},
        {"reason": "NO_ASSESSMENT", "first_target_index": 4, "last_target_index": 4, "rounds": 1},
    ]


def test_live_board_uses_only_frozen_pre_outcome_rows_and_keeps_pending_results_hidden():
    configuration_hash = "frozen-hash"
    heartbeat_age_seconds = [20]

    def assessment(target_index, score):
        source_index = target_index - 1
        source_id = f"round-{source_index}"
        return {
            "assessment_id": f"assessment-{target_index}", "model_version": "V3_FROZEN_2026-10-03",
            "round_id": source_id, "round_index": source_index, "target_round_index": target_index,
            "model_configuration_hash": configuration_hash, "assessment_immutable": True,
            "scorable": True, "opportunity_score": score,
            "signal": False, "candidate_is_prediction": False,
            "candidate_state": "ABSTAIN_STABILITY_LOW",
            "rank_at_observation": target_index - 100,
            "model_scores": {"logistic_regression_probability": score}, "pattern_score": "ABC",
            "analog_score": None, "stability": {"state": "STABILITY_LOW"},
            "feature_snapshot": {"sequence5_state": "ABCDE"},
            "seq5_evidence": {"support": 12, "successes": 4, "state": "PAST_ONLY"},
            "created_at": f"2026-10-04T09:00:{target_index % 60:02d}+00:00",
            "round_order_proof": {
                "assessment_source_round_index": source_index, "assessment_source_round_id": source_id,
                "latest_round_at_assessment_commit_index": source_index,
                "latest_round_at_assessment_commit_id": source_id,
                "target_outcome_round_index": target_index,
                "source_was_latest_at_assessment_commit": True,
                "target_absent_at_assessment_commit": True,
                "proof_method": "postgres_advisory_transaction_lock",
            },
        }

    assessments = [assessment(101, .1), assessment(102, .2), assessment(103, .3),
                   assessment(104, .4), assessment(105, .99)]
    warmup_assessment = {
        "assessment_id": "assessment-warmup-106", "model_version": "V3_FROZEN_2026-10-03",
        "round_id": "round-105", "round_index": 105, "target_round_index": 106,
        "model_configuration_hash": configuration_hash, "assessment_immutable": True,
        "scorable": False, "failed_gate": "CONTINUITY_WARMUP",
        "feature_snapshot": {"verified_segment_rounds": 53, "required_rounds": 102},
        "created_at": "2026-10-04T09:01:06+00:00",
    }
    assessments.append(warmup_assessment)
    outcomes = []
    for target_index, multiplier in ((101, 3.2), (102, 1.2), (103, 2.1), (104, 1.1)):
        source_index = target_index - 1
        outcomes.append({
            "assessment_id": f"assessment-{target_index}", "target_round_id": f"round-{target_index}",
            "target_round_index": target_index, "actual_multiplier": multiplier,
            "resolved_at": "2026-10-04T09:01:00+00:00",
            "round_order_proof": {
                "assessment_source_round_index": source_index,
                "target_outcome_round_index": target_index, "consecutive_indices": True,
                "assessment_inserted_while_source_latest": True,
                "target_absent_at_assessment_commit": True,
            },
        })

    class Repository:
        def list_v4_assessments(self):
            return assessments

        def list_v4_outcomes(self):
            return outcomes

        def load_application_state(self, key):
            if key == "opportunity_v3_frozen_config":
                return {"status": "FROZEN", "version": "V3_FROZEN_2026-10-03",
                        "configuration_hash": configuration_hash}
            if key == "opportunity_v4_observer_runtime":
                heartbeat = datetime.now(timezone.utc) - timedelta(seconds=heartbeat_age_seconds[0])
                return {"state": "RUNNING", "heartbeat_at": heartbeat.isoformat(),
                        "last_processed_round_index": 105, "last_processed_round_id": "round-105",
                        "last_assessment_round_id": "round-105"}
            return {}

        @staticmethod
        def latest_round_marker():
            return (105, "round-105", None, 1.1)

        @staticmethod
        def latest_round_live_details():
            return {"round_index": 105, "round_id": "round-105", "stored_at": "2026-10-04T09:01:06+00:00"}

        @staticmethod
        def count_rounds_after(_index):
            return 5

    snapshot = OpportunityStabilityV4(Repository()).live_dashboard()

    assert snapshot["scored"] == 5
    assert snapshot["resolved"] == 4
    assert snapshot["pending"] == 1
    assert snapshot["selection_gate_status"] == "SELECTIVE_GATE_NOT_DEFINED"
    assert snapshot["selection_counts"] == {"no_signal": 5, "watch": 0, "selected": 0, "gate_undefined": 0}
    assert snapshot["current_top4"] == []
    assert snapshot["selected_opportunities"] == []
    assert snapshot["available_selected_slots"] == 4
    assert [row["target_round_index"] for row in snapshot["live_internal_scoring"]] == [101, 102, 103, 104, 105]
    assert all(row["selection_state"] == "NO_SIGNAL" for row in snapshot["live_internal_scoring"])
    assert snapshot["live_internal_scoring"][0]["result"] == "TRUE"
    assert snapshot["live_internal_scoring"][1]["result"] == "FALSE"
    assert snapshot["live_internal_scoring"][1]["is_true_2_10x"] is False
    assert snapshot["live_internal_scoring"][2]["is_true_2_10x"] is True
    assert snapshot["observer_active"] is True
    assert snapshot["observer_caught_up_to_latest_round"] is True
    assert snapshot["last_assessment_round_index"] == 105
    assert snapshot["last_assessment_target_round_index"] == 106
    assert snapshot["last_assessment_scorable"] is False
    assert snapshot["last_scored_assessment_round_index"] == 104
    assert snapshot["scoring_warmup"]["verified_rounds"] == 53
    assert snapshot["rolling_windows"]["100"]["status"] == "WAITING"
    assert snapshot["rolling_windows"]["100"]["progress"] == 4

    heartbeat_age_seconds[0] = 60
    stale_snapshot = OpportunityStabilityV4(Repository()).live_dashboard()
    assert stale_snapshot["observer_active"] is False
    assert stale_snapshot["observer_state"] == "PAUSED_REQUIRES_OPERATOR"


def test_selected_opportunity_requires_explicit_frozen_gate_and_pre_outcome_proof():
    gate = {"status": "FROZEN", "policy_version": "frozen-policy-v1",
            "configuration_hash": "frozen-hash", "policy": {"kind": "existing"}}
    row = {
        "assessment_immutable": True,
        "round_id": "source-1", "round_index": 1, "target_round_index": 2,
        "model_configuration_hash": "frozen-hash",
        "selection_state": "SELECTED_OPPORTUNITY", "selected": True,
        "selection_gate_hash": "frozen-hash", "selection_frozen_with_assessment": True,
        "round_order_proof": {
            "assessment_source_round_index": 1, "assessment_source_round_id": "source-1",
            "latest_round_at_assessment_commit_index": 1,
            "latest_round_at_assessment_commit_id": "source-1",
            "target_outcome_round_index": 2,
            "source_was_latest_at_assessment_commit": True,
            "target_absent_at_assessment_commit": True,
            "proof_method": "postgres_advisory_transaction_lock",
        },
    }
    assert OpportunityStabilityV4._selected_before_outcome(row, "frozen-hash", gate)
    assert not OpportunityStabilityV4._selected_before_outcome(row, "frozen-hash", None)
    row["round_order_proof"]["target_absent_at_assessment_commit"] = False
    assert not OpportunityStabilityV4._selected_before_outcome(row, "frozen-hash", gate)


def test_assessment_commit_proof_is_required_for_live_board_eligibility():
    row = {"assessment_immutable": True, "round_id": "source-1", "round_index": 1,
           "target_round_index": 2, "model_configuration_hash": "frozen-hash",
           "round_order_proof": {
               "assessment_source_round_index": 1, "assessment_source_round_id": "source-1",
               "latest_round_at_assessment_commit_index": 1,
               "latest_round_at_assessment_commit_id": "source-1", "target_outcome_round_index": 2,
               "source_was_latest_at_assessment_commit": True,
               "target_absent_at_assessment_commit": True,
               "proof_method": "postgres_advisory_transaction_lock",
           }}
    assert OpportunityStabilityV4._assessment_order_proof_verified(row, "frozen-hash")
    row["round_order_proof"]["target_absent_at_assessment_commit"] = False
    assert not OpportunityStabilityV4._assessment_order_proof_verified(row, "frozen-hash")


def test_reconcile_resolves_pending_target_when_collector_has_advanced_past_its_marker(monkeypatch):
    assessment = {
        "assessment_id": "assessment-2", "model_version": "V3_FROZEN_2026-10-03",
        "round_id": "round-1", "round_index": 1, "target_round_index": 2,
        "observed_at": "2026-10-04T09:00:00+00:00", "created_at": "2026-10-04T09:00:00+00:00",
        "assessment_immutable": True, "scorable": True, "opportunity_score": .7,
        "model_configuration_hash": "frozen-hash",
        "round_order_proof": {
            "assessment_source_round_index": 1, "assessment_source_round_id": "round-1",
            "latest_round_at_assessment_commit_index": 1,
            "latest_round_at_assessment_commit_id": "round-1", "target_outcome_round_index": 2,
            "source_was_latest_at_assessment_commit": True,
            "target_absent_at_assessment_commit": True,
            "proof_method": "postgres_advisory_transaction_lock",
        },
    }
    saved = []
    queried_ranges = []

    class Repository:
        database_url = "postgresql://redacted"

        @staticmethod
        def list_unresolved_v4_assessments(first_source_round_index, last_source_round_index):
            queried_ranges.append((first_source_round_index, last_source_round_index))
            return [assessment] if first_source_round_index <= 1 <= last_source_round_index else []

        @staticmethod
        def save_v4_outcome_once(payload):
            saved.append(payload)

    frame = pd.DataFrame([
        {"round_id": "round-1", "round_index": 1, "multiplier": 1.5,
         "continuity_verified": True, "gap_before": False,
         "stored_at": "2026-10-04T08:59:00+00:00", "stored_at_dt": pd.Timestamp("2026-10-04T08:59:00Z"),
         "observed_at": "2026-10-04T08:59:00+00:00"},
        {"round_id": "round-2", "round_index": 2, "multiplier": 2.1,
         "continuity_verified": True, "gap_before": False,
         "stored_at": "2026-10-04T09:00:05+00:00", "stored_at_dt": pd.Timestamp("2026-10-04T09:00:05Z"),
         "observed_at": "2026-10-04T09:00:05+00:00"},
        {"round_id": "round-3", "round_index": 3, "multiplier": 1.2,
         "continuity_verified": True, "gap_before": False,
         "stored_at": "2026-10-04T09:00:10+00:00", "stored_at_dt": pd.Timestamp("2026-10-04T09:00:10Z"),
         "observed_at": "2026-10-04T09:00:10+00:00"},
    ])
    monkeypatch.setattr(opportunity_v4, "validated_rounds", lambda value: (value, {}))

    added = OpportunityStabilityV4(Repository()).reconcile(frame, target_round_index=3)

    assert added == 1
    assert queried_ranges == [(1, 2)]
    assert saved[0]["target_round_index"] == 2
    assert saved[0]["actual_multiplier"] == 2.1
    assert saved[0]["status"] == "TRUE"
