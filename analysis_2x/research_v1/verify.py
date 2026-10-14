#!/usr/bin/env python3
import json
import math
from pathlib import Path

import numpy as np
import pandas as pd

import run_analysis as pipeline


HERE = Path(__file__).resolve().parent


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def main():
    plan, raw, clean_rows, model, segments, quality, hashes = pipeline.load_data()
    required_files = [
        HERE / "data_quality_report.md",
        HERE / "analysis_report.md",
        HERE / "candidate_rules.csv",
        HERE / "frozen_shortlist.json",
        HERE / "final_test_record.json",
        HERE / "selected_rounds.csv",
        HERE / "backtest_results.json",
        HERE / "prospective_validation_plan.md",
        HERE / "run_manifest.json",
        HERE / "search_reproducibility.json",
    ]
    for path in required_files:
        require(path.exists() and path.stat().st_size > 0, f"Missing or empty artifact: {path.name}")

    with open(HERE / "frozen_shortlist.json", encoding="utf-8") as handle:
        frozen = json.load(handle)
    with open(HERE / "final_test_record.json", encoding="utf-8") as handle:
        final_record = json.load(handle)
    with open(HERE / "search_reproducibility.json", encoding="utf-8") as handle:
        reproducibility = json.load(handle)
    with open(HERE / "backtest_results.json", encoding="utf-8") as handle:
        backtests = json.load(handle)
    with open(HERE / "run_manifest.json", encoding="utf-8") as handle:
        manifest = json.load(handle)

    require(frozen["plan_sha256"] == hashes["plan_sha256"], "Frozen shortlist plan hash mismatch")
    require(frozen["final_test_opened"] is False, "Shortlist record must preserve its pre-test state")
    require(final_record["final_test_opened"] is True, "Final-period evaluation was not recorded")
    require(final_record["evaluation_count"] == 1, "Final-period results were evaluated more than once")
    require(final_record["final_period_status"] == "CONTAMINATED_RETROSPECTIVE_ONLY", "Final period was mislabeled")
    require(final_record["selected_rule_set_sha256"] == frozen["selected_rule_set_sha256"], "Final test did not use the frozen shortlist")
    require(reproducibility["reproducible"] is True and all(reproducibility["comparisons"].values()), "Repeated discovery search was not deterministic")
    require(manifest["plan_sha256"] == hashes["plan_sha256"], "Run manifest plan hash mismatch")
    require(manifest["snapshot_sha256"] == plan["source"]["raw_sha256"], "Run manifest snapshot hash mismatch")

    platform_timestamps = pd.to_datetime(raw["platform_timestamp"], format="mixed", utc=True, errors="coerce")
    timestamp_plan = plan["timestamp_analysis"]
    require(int(raw["timestamp_utc"].notna().sum()) == len(raw), "Mixed-precision compatibility timestamps failed to parse")
    require(int(platform_timestamps.notna().sum()) == timestamp_plan["populated_rows"], "Platform timestamp parse count differs from the frozen audit")
    require(int((platform_timestamps.notna() & platform_timestamps.eq(raw["timestamp_utc"])).sum()) == timestamp_plan["equals_compatibility_timestamp_rows"], "Platform and compatibility timestamp comparison differs from the frozen audit")
    require(int(raw.loc[platform_timestamps.notna(), "continuity_verified_bool"].sum()) == timestamp_plan["continuity_verified_rows"], "Timestamp continuity provenance differs from the frozen plan")
    require(set(raw.loc[platform_timestamps.notna(), "source"].astype(str)) == {timestamp_plan["source"].split()[0]}, "Platform timestamp source differs from the frozen plan")
    require(set(raw.loc[platform_timestamps.notna(), "identity_confidence"].astype(str)) == {timestamp_plan["identity_confidence"]}, "Timestamp identity confidence differs from the frozen plan")

    candidates = pipeline.candidate_definitions(plan)
    candidate_frame = pd.read_csv(HERE / "candidate_rules.csv")
    require(len(candidate_frame) == len(candidates), "candidate_rules.csv does not cover the full frozen search family")
    require(pipeline.canonical_hash(candidates) == frozen["candidate_family_sha256"], "Candidate family hash differs from the frozen shortlist")
    require(len(frozen["shortlist"]) <= plan["shortlist_freeze"]["size"], "Shortlist exceeds the frozen size limit")
    shortlisted_ids = [item["candidate"]["candidate_id"] for item in frozen["shortlist"]]
    require(shortlisted_ids == final_record["evaluated_candidate_ids"], "Final test candidate IDs differ from the frozen shortlist")
    require(final_record["candidate_search_csv_sha256"] == reproducibility["second_run"]["candidate_rules_sha256"], "Final test used a different discovery/validation candidate file")

    required_config_fields = {
        "schema_version", "policy_id", "policy_version", "policy_status", "dataset_hash",
        "target_multiplier", "entry_condition", "feature_windows", "target_round_offset",
        "cooldown_rounds", "stake_mode", "stake_amount", "session_loss_limit",
        "consecutive_loss_limit", "profit_goal", "session_reset_rule", "missing_data_action",
        "expiry_rule", "reason_codes", "evidence",
    }
    configs = {}
    for candidate_id in shortlisted_ids:
        config_path = HERE / "rules" / f"{candidate_id}.json"
        require(config_path.exists(), f"Missing rule config {candidate_id}")
        with open(config_path, encoding="utf-8") as handle:
            config = json.load(handle)
        require(required_config_fields.issubset(config), f"Rule config schema incomplete for {candidate_id}")
        require(config["enabled"] is False, f"Rule {candidate_id} was enabled")
        require(config["policy_status"] in {"EDGE_UNVERIFIED", "INSUFFICIENT_DATA"}, f"Unexpected rule status for {candidate_id}")
        require(config["target_round_offset"] == 1 and config["target_multiplier"] == 2.0, f"Incorrect target contract for {candidate_id}")
        require((HERE / "rules" / f"{candidate_id}.md").exists(), f"Missing plain-language rule explanation for {candidate_id}")
        configs[candidate_id] = config

    selected = pd.read_csv(HERE / "selected_rounds.csv", dtype={"source_round_id": str, "target_round_id": str})
    require(set(selected["candidate_id"].unique()) == set(shortlisted_ids), "Selected-round output candidate set mismatch")
    require(len(selected) == len(model) * len(shortlisted_ids), "Selected-round output does not cover every modeled target per rule")
    position_by_id = {round_id: position for position, round_id in enumerate(model["round_id"].astype(str))}
    rule_masks = {}
    bank = pipeline.build_feature_bank(model, segments, len(model))
    for candidate_id in shortlisted_ids:
        candidate = configs[candidate_id]["entry_condition"]["definition"]
        mask, valid = pipeline.candidate_arrays(candidate, bank)
        rule_masks[candidate_id] = (mask, valid)

    profit_totals = {candidate_id: 0.0 for candidate_id in shortlisted_ids}
    bet_counts = {candidate_id: 0 for candidate_id in shortlisted_ids}
    for row in selected.itertuples(index=False):
        candidate_id = row.candidate_id
        target_position = position_by_id[str(row.target_round_id)]
        target = model.iloc[target_position]
        require(int(row.target_round_index) == int(target["round_index"]), "Target round index mismatch")
        expected_hit = int(float(row.observed_multiplier) >= 2.0)
        require(int(row.observed_hit_at_2x) == expected_hit, "Observed 2x label does not match multiplier")
        mask, valid = rule_masks[candidate_id]
        expected_trigger = bool(mask[target_position] and valid[target_position])
        require(pipeline.parse_bool(row.trigger) == expected_trigger, "Stored trigger differs from past-only feature evaluation")
        if not pd.isna(row.source_round_id) and str(row.source_round_id).strip():
            source_position = position_by_id[str(row.source_round_id)]
            source = model.iloc[source_position]
            require(source_position + 1 == target_position, "Entry did not target the next eligible row")
            require(int(row.source_segment_id) == int(row.target_segment_id), "Entry crossed a continuity segment")
            require(int(source["segment_id"]) == int(target["segment_id"]), "Source and target segment mismatch")
            require(int(row.source_round_index) < int(row.target_round_index), "Local round index inversion in source-target pair")
            if not pd.isna(row.feature_end_round_index):
                require(int(row.feature_end_round_index) < int(row.target_round_index), "Feature includes the target or a future round")
                require(int(row.feature_end_round_index) == int(row.source_round_index), "Feature end differs from the source round")
        else:
            require(not expected_trigger, "Trigger exists without an in-segment source round")
        if row.decision == "BET":
            require(expected_trigger, "BET was issued when the policy trigger was false")
            profit = 1.0 if expected_hit else -1.0
            require(math.isclose(float(row.idealized_profit_units_if_bet), profit, abs_tol=1e-12), "2.00x payout calculation is incorrect")
            profit_totals[candidate_id] += profit
            bet_counts[candidate_id] += 1
        if row.decision == "STOP":
            require(str(row.reason_code) in {"SESSION_LOSS_LIMIT", "CONSECUTIVE_LOSS_LIMIT", "PROFIT_GOAL"}, "STOP lacks a session-limit reason")

    for candidate_id in shortlisted_ids:
        trigger_rows = selected[(selected["candidate_id"] == candidate_id) & selected["trigger"].map(pipeline.parse_bool)]
        final_rows = trigger_rows[trigger_rows["split"] == "retrospective_final_contaminated"]
        saved = candidate_frame[candidate_frame["candidate_id"] == candidate_id].iloc[0]
        final_wins = int(final_rows["observed_hit_at_2x"].sum())
        require(int(saved["retrospective_final_selected"]) == len(final_rows), "Final selected-round count mismatch")
        require(int(saved["retrospective_final_wins"]) == final_wins, "Final win count mismatch")
        expected_rate = final_wins / len(final_rows) if len(final_rows) else np.nan
        require((math.isnan(expected_rate) and pd.isna(saved["retrospective_final_rate"])) or math.isclose(expected_rate, float(saved["retrospective_final_rate"]), abs_tol=1e-12), "Final rate mismatch")
        expected_roi = (2 * final_wins - len(final_rows)) / len(final_rows) if len(final_rows) else np.nan
        require((math.isnan(expected_roi) and pd.isna(saved["retrospective_final_ideal_roi"])) or math.isclose(expected_roi, float(saved["retrospective_final_ideal_roi"]), abs_tol=1e-12), "Ideal 2x return calculation mismatch")
        require(math.isclose(profit_totals[candidate_id], float(backtests[candidate_id]["net_profit_units"]), abs_tol=1e-12), "Backtest net P/L differs from BET rows")
        require(bet_counts[candidate_id] == int(backtests[candidate_id]["actual_bets"]), "Backtest bet count differs from BET rows")

    require(int(quality["cleaning"]["dropped_rows"]) == int(quality["cleaning"]["D1_exact_ts_same_mult_dropped"] + quality["cleaning"]["D2_overlap_reobserved_dropped"] + quality["cleaning"]["D3_near_ts_same_mult_dropped"]), "Overlap cleaning exclusions do not reconcile")
    require(int(raw["round_id"].duplicated().sum()) == 0, "Duplicate round IDs are present")
    require(all(bool(model.iloc[index]["continuity_verified"]) for index in range(len(model))), "Unverified row entered the pattern analysis set")
    for segment in segments:
        for left, right in zip(segment[:-1], segment[1:]):
            require(right == left + 1, "Analysis segment has a discontinuous output position")
            require(int(model.iloc[left]["segment_id"]) == int(model.iloc[right]["segment_id"]), "Segment ID changed inside a segment")
            require(not bool(model.iloc[right]["gap_before"]), "A declared gap was bridged")
    retained_equal_neighbors = sum(
        int(model.iloc[left]["multiplier"] == model.iloc[right]["multiplier"])
        for segment in segments
        for left, right in zip(segment[:-1], segment[1:])
    )
    require(retained_equal_neighbors > 0, "No legitimate adjacent equal-multiplier observations remain in the verified set")

    print(json.dumps({
        "verified": True,
        "raw_rows": int(len(raw)),
        "clean_rows": int(len(clean_rows)),
        "verified_order_rows": int(len(model)),
        "segments": int(len(segments)),
        "candidate_count": int(len(candidate_frame)),
        "shortlist": shortlisted_ids,
        "selected_round_rows": int(len(selected)),
        "final_test_evaluations": int(final_record["evaluation_count"]),
        "checks": [
            "input and plan hashes",
            "mixed-precision timestamp parse and legacy provenance",
            "deterministic discovery search",
            "full candidate coverage",
            "final shortlist freeze and contaminated-period label",
            "no future feature or gap crossing",
            "next-round offset",
            "2.00x profit arithmetic",
            "backtest P/L reconciliation",
            "rule schema and disabled status",
            "overlap cleaning counts",
            "adjacent equal multiplier preservation",
        ],
    }, indent=2))


if __name__ == "__main__":
    main()
