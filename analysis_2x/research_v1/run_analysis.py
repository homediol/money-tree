#!/usr/bin/env python3
import argparse
import hashlib
import json
import math
import os
import sys
from pathlib import Path

os.environ.setdefault("MPLCONFIGDIR", "/tmp/aviator-analysis-matplotlib")

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd


HERE = Path(__file__).resolve().parent
ANALYSIS_ROOT = HERE.parent
PROJECT_ROOT = ANALYSIS_ROOT.parent
ARTIFACTS = ANALYSIS_ROOT / "artifacts"
PLAN_PATH = HERE / "research_plan.json"
QUALITY_PATH = ARTIFACTS / "data_quality.json"
RAW_PATH = ARTIFACTS / "raw_rounds.csv"
CLEAN_PATH = ARTIFACTS / "rounds_clean.csv"
SHORTLIST_PATH = HERE / "frozen_shortlist.json"
CANDIDATE_PATH = HERE / "candidate_rules.csv"
NULL_PATH = HERE / "null_checks.json"
QUALITY_REPORT_PATH = HERE / "data_quality_report.md"
ANALYSIS_REPORT_PATH = HERE / "analysis_report.md"
BACKTEST_PATH = HERE / "backtest_results.json"
SELECTED_PATH = HERE / "selected_rounds.csv"
MANIFEST_PATH = HERE / "run_manifest.json"


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_hash(value):
    content = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def to_builtin(value):
    if isinstance(value, dict):
        return {str(key): to_builtin(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_builtin(item) for item in value]
    if isinstance(value, np.ndarray):
        return [to_builtin(item) for item in value.tolist()]
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating, float)):
        number = float(value)
        return number if math.isfinite(number) else None
    if isinstance(value, (np.bool_, bool)):
        return bool(value)
    if pd.isna(value):
        return None
    return value


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(to_builtin(value), handle, indent=2, sort_keys=True, allow_nan=False)
        handle.write("\n")


def read_plan():
    with open(PLAN_PATH, encoding="utf-8") as handle:
        plan = json.load(handle)
    if plan.get("status") != "FROZEN_BEFORE_NEW_CANDIDATE_SEARCH":
        raise RuntimeError("Research plan is not frozen")
    return plan


def parse_bool(value):
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"true", "t", "1", "yes"}


def load_data():
    plan = read_plan()
    raw_hash = sha256_file(RAW_PATH)
    clean_hash = sha256_file(CLEAN_PATH)
    quality_hash = sha256_file(QUALITY_PATH)
    if raw_hash != plan["source"]["raw_sha256"]:
        raise RuntimeError("Raw snapshot hash differs from the frozen plan")
    if clean_hash != plan["source"]["existing_clean_sha256"]:
        raise RuntimeError("Clean-set hash differs from the frozen plan")
    if quality_hash != plan["source"]["existing_quality_sha256"]:
        raise RuntimeError("Quality-audit hash differs from the frozen plan")

    raw = pd.read_csv(RAW_PATH, dtype={"round_id": str, "platform_round_id": str})
    clean = pd.read_csv(CLEAN_PATH, dtype={"round_id": str, "platform_round_id": str})
    with open(QUALITY_PATH, encoding="utf-8") as handle:
        existing_quality = json.load(handle)
    raw["round_index"] = pd.to_numeric(raw["round_index"], errors="raise").astype(int)
    raw["multiplier"] = pd.to_numeric(raw["multiplier"], errors="coerce")
    raw["timestamp_utc"] = pd.to_datetime(raw["timestamp"], format="mixed", utc=True, errors="coerce")
    raw["kept"] = raw["round_id"].map(clean.set_index("round_id")["kept"].map(parse_bool)).fillna(False)
    clean_lookup = clean.set_index("round_id")
    raw["drop_rule"] = raw["round_id"].map(clean_lookup["drop_rule"]).fillna("")
    raw["drop_detail"] = raw["round_id"].map(clean_lookup["drop_detail"]).fillna("")
    raw["round_index_source"] = raw["round_index_source"].fillna("UNKNOWN")
    raw["round_identity_type"] = raw["round_identity_type"].fillna("UNKNOWN")
    raw["continuity_proof"] = raw["continuity_proof"].fillna("UNKNOWN")
    raw["continuity_verified_bool"] = raw["continuity_verified"].map(parse_bool)
    raw["gap_before_bool"] = raw["gap_before"].map(parse_bool)
    raw = raw.sort_values(["round_index", "id"], kind="stable").reset_index(drop=True)
    clean_rows = raw[raw["kept"]].copy()
    model, segments = build_model_rows(raw)

    expected_model_n = int(plan["analysis_population"]["pattern_evaluation_rows"])
    if len(model) != expected_model_n:
        raise RuntimeError(f"Expected {expected_model_n} overlap-verified rows; found {len(model)}")
    return plan, raw, clean_rows, model, segments, existing_quality, {
        "raw_sha256": raw_hash,
        "clean_sha256": clean_hash,
        "quality_sha256": quality_hash,
        "plan_sha256": sha256_file(PLAN_PATH),
    }


def build_model_rows(raw):
    rows = []
    segments = []
    current = []
    previous_raw_index = None
    segment_id = -1
    segment_pos = 0
    barrier = True

    def close_segment():
        nonlocal current
        if current:
            segments.append(np.asarray([item["model_pos"] for item in current], dtype=int))
            current = []

    for record in raw.to_dict("records"):
        current_index = int(record["round_index"])
        if previous_raw_index is not None and current_index != previous_raw_index + 1:
            close_segment()
            barrier = True
        previous_raw_index = current_index
        if parse_bool(record.get("gap_before")):
            close_segment()
            barrier = True
        verified = (
            parse_bool(record.get("continuity_verified"))
            and record.get("continuity_proof") == "HISTORY_SNAPSHOT_OVERLAP"
            and record.get("round_identity_type") == "COLLECTOR_OBSERVATION_HASH"
            and record.get("round_index_source") == "LOCAL_SEQUENCE"
        )
        if not verified:
            close_segment()
            barrier = True
            continue
        if not record["kept"]:
            continue
        if barrier:
            close_segment()
            segment_id += 1
            segment_pos = 0
            barrier = False
        row = {
            "model_pos": len(rows),
            "segment_id": segment_id,
            "segment_pos": segment_pos,
            "id": str(record["id"]),
            "round_id": str(record["round_id"]),
            "round_index": current_index,
            "multiplier": float(record["multiplier"]),
            "timestamp": record.get("timestamp"),
            "timestamp_utc": record.get("timestamp_utc"),
            "source": str(record.get("source") or "UNKNOWN"),
            "continuity_verified": True,
            "gap_before": bool(record["gap_before_bool"]),
            "identity_confidence": str(record.get("identity_confidence") or "UNKNOWN"),
            "continuity_proof": str(record.get("continuity_proof") or "UNKNOWN"),
        }
        rows.append(row)
        current.append(row)
        segment_pos += 1
    close_segment()
    model = pd.DataFrame(rows)
    return model, segments


def wilson_interval(successes, observations, z=1.959963984540054):
    if observations <= 0:
        return None, None
    p = successes / observations
    denominator = 1 + z * z / observations
    center = (p + z * z / (2 * observations)) / denominator
    half = z * math.sqrt((p * (1 - p) + z * z / (4 * observations)) / observations) / denominator
    return max(0.0, center - half), min(1.0, center + half)


def candidate_definitions(plan):
    candidates = []

    def add(candidate_id, family, description, requirements, components=None, feature=None, op=None, threshold=None):
        item = {
            "candidate_id": candidate_id,
            "family": family,
            "description": description,
            "requirements": requirements,
        }
        if components is not None:
            item["components"] = components
        else:
            item.update({"feature": feature, "op": op, "threshold": threshold})
        candidates.append(item)

    for k in plan["feature_families"]["below_streak_at_least"]:
        add(f"below_streak_ge_{k}", "BELOW_STREAK", f"At least {k} consecutive previous rounds were below 2.00x.", {"low_streak": k}, feature="low_streak", op=">=", threshold=k)
    for k in plan["feature_families"]["above_streak_at_least"]:
        add(f"above_streak_ge_{k}", "ABOVE_STREAK", f"At least {k} consecutive previous rounds were at or above 2.00x.", {"high_streak": k}, feature="high_streak", op=">=", threshold=k)

    for length in plan["feature_families"]["exact_binary_sequences"]["lengths"]:
        for code in range(1 << length):
            bits = format(code, f"0{length}b")
            words = "-".join("H" if bit == "1" else "L" for bit in bits)
            add(
                f"binary_{length}_{bits}",
                "EXACT_BINARY_SEQUENCE",
                f"The previous {length} outcomes were {words} (H >=2.00x, L <2.00x).",
                {f"binary_sequence_{length}": length},
                feature=f"binary_sequence_{length}", op="==", threshold=code,
            )

    for window in plan["feature_families"]["rolling_hit_counts_and_rates"]["windows"]:
        for count in range(window):
            add(
                f"rate_{window}_le_{count}", "ROLLING_HIT_COUNT",
                f"At most {count} of the previous {window} rounds were >=2.00x.",
                {f"rolling_count_{window}": window},
                feature=f"rolling_count_{window}", op="<=", threshold=count,
            )
        for count in range(1, window + 1):
            add(
                f"rate_{window}_ge_{count}", "ROLLING_HIT_COUNT",
                f"At least {count} of the previous {window} rounds were >=2.00x.",
                {f"rolling_count_{window}": window},
                feature=f"rolling_count_{window}", op=">=", threshold=count,
            )

    for k in plan["feature_families"]["time_since_last_hit_at_least_rounds"]:
        add(
            f"time_since_hit_ge_{k}", "TIME_SINCE_LAST_HIT",
            f"No >=2.00x outcome appeared in the previous {k} verified rounds.",
            {"low_streak": k}, feature="low_streak", op=">=", threshold=k,
        )

    median_spec = plan["feature_families"]["rolling_median_multiplier"]
    for window in median_spec["windows"]:
        for cutoff in median_spec["cutoffs"]:
            for op in median_spec["directions"]:
                symbol = "le" if op == "<=" else "ge"
                add(
                    f"median_{window}_{symbol}_{str(cutoff).replace('.', '_')}", "ROLLING_MEDIAN",
                    f"Median multiplier of the previous {window} verified rounds {op} {cutoff:.2f}x.",
                    {f"rolling_median_{window}": window}, feature=f"rolling_median_{window}", op=op, threshold=cutoff,
                )

    quartile_spec = plan["feature_families"]["rolling_quartiles"]
    for window in quartile_spec["windows"]:
        for quantile in quartile_spec["quantiles"]:
            qname = int(quantile * 100)
            for cutoff in quartile_spec["cutoffs"]:
                for op in quartile_spec["directions"]:
                    symbol = "le" if op == "<=" else "ge"
                    add(
                        f"q{qname}_{window}_{symbol}_{str(cutoff).replace('.', '_')}", "ROLLING_QUANTILE",
                        f"Past {qname}th percentile over {window} verified rounds {op} {cutoff:.2f}x.",
                        {f"rolling_q{qname}_{window}": window}, feature=f"rolling_q{qname}_{window}", op=op, threshold=cutoff,
                    )

    volatility_spec = plan["feature_families"]["rolling_log_multiplier_volatility"]
    for window in volatility_spec["windows"]:
        for cutoff in volatility_spec["cutoffs"]:
            for op in volatility_spec["directions"]:
                symbol = "le" if op == "<=" else "ge"
                add(
                    f"logvol_{window}_{symbol}_{str(cutoff).replace('.', '_')}", "ROLLING_LOG_VOLATILITY",
                    f"Standard deviation of log multipliers over the previous {window} rounds {op} {cutoff:.2f}.",
                    {f"rolling_log_std_{window}": window}, feature=f"rolling_log_std_{window}", op=op, threshold=cutoff,
                )

    combo_spec = plan["feature_families"]["limited_combinations"]
    for streak in combo_spec["streak_lengths"]:
        for window in combo_spec["rate_windows"]:
            for cutoff in combo_spec["below_rate_cutoffs"]:
                add(
                    f"combo_low_{streak}_rate{window}_le_{str(cutoff).replace('.', '_')}", "TWO_COMPONENT_COMBINATION",
                    f"At least {streak} previous rounds below 2.00x and at most {cutoff:.0%} hits in the previous {window} rounds.",
                    {"low_streak": streak, f"rolling_count_{window}": window},
                    components=[
                        {"feature": "low_streak", "op": ">=", "threshold": streak, "required_history": streak},
                        {"feature": f"rolling_count_{window}", "op": "<=", "threshold": math.floor(cutoff * window + 1e-12), "required_history": window},
                    ],
                )
            for cutoff in combo_spec["above_rate_cutoffs"]:
                minimum = math.ceil(cutoff * window - 1e-12)
                add(
                    f"combo_high_{streak}_rate{window}_ge_{str(cutoff).replace('.', '_')}", "TWO_COMPONENT_COMBINATION",
                    f"At least {streak} previous rounds at or above 2.00x and at least {cutoff:.0%} hits in the previous {window} rounds.",
                    {"high_streak": streak, f"rolling_count_{window}": window},
                    components=[
                        {"feature": "high_streak", "op": ">=", "threshold": streak, "required_history": streak},
                        {"feature": f"rolling_count_{window}", "op": ">=", "threshold": minimum, "required_history": window},
                    ],
                )

    candidate_ids = [candidate["candidate_id"] for candidate in candidates]
    if len(candidate_ids) != len(set(candidate_ids)):
        raise RuntimeError("Candidate IDs are not unique")
    if len(candidates) > plan["feature_families"]["maximum_unique_candidates"]:
        raise RuntimeError(f"Candidate family size {len(candidates)} exceeds the frozen budget")
    return candidates


def sliding_stats(values, window):
    if len(values) <= window:
        return np.asarray([]), np.asarray([]), np.asarray([]), np.asarray([])
    windows = np.lib.stride_tricks.sliding_window_view(values, window)[:len(values) - window]
    return (
        windows.sum(axis=1),
        np.median(windows, axis=1),
        np.quantile(windows, 0.25, axis=1),
        np.quantile(windows, 0.75, axis=1),
    )


def build_feature_bank(model, segments, length):
    feature_values = {}
    feature_valid = {}
    segment_positions = np.zeros(length, dtype=int)
    outcomes = (model["multiplier"].to_numpy(dtype=float) >= 2.0).astype(np.int8)
    multipliers = model["multiplier"].to_numpy(dtype=float)
    for key in (
        "low_streak", "high_streak",
        *[f"binary_sequence_{window}" for window in (2, 3, 4)],
        *[f"rolling_count_{window}" for window in (5, 10, 20, 50, 100)],
        *[f"rolling_median_{window}" for window in (10, 25, 50, 100)],
        *[f"rolling_q25_{window}" for window in (25, 50, 100)],
        *[f"rolling_q75_{window}" for window in (25, 50, 100)],
        *[f"rolling_log_std_{window}" for window in (10, 25, 50, 100)],
    ):
        feature_values[key] = np.zeros(length, dtype=float)
        feature_valid[key] = np.zeros(length, dtype=bool)

    for indices in segments:
        indices = indices[indices < length]
        if len(indices) == 0:
            continue
        y = outcomes[indices]
        values = multipliers[indices]
        n = len(indices)
        segment_positions[indices] = np.arange(n)
        low = np.zeros(n, dtype=float)
        high = np.zeros(n, dtype=float)
        low_count = 0
        high_count = 0
        for pos, outcome in enumerate(y):
            low[pos] = low_count
            high[pos] = high_count
            low_count = low_count + 1 if outcome == 0 else 0
            high_count = high_count + 1 if outcome == 1 else 0
        if n > 1:
            feature_values["low_streak"][indices[1:]] = low[1:]
            feature_values["high_streak"][indices[1:]] = high[1:]
            feature_valid["low_streak"][indices[1:]] = True
            feature_valid["high_streak"][indices[1:]] = True

        for window in (2, 3, 4):
            if n <= window:
                continue
            bits = np.lib.stride_tricks.sliding_window_view(y, window)[:n - window]
            weights = (1 << np.arange(window - 1, -1, -1, dtype=np.int64))
            codes = bits.astype(np.int64) @ weights
            targets = indices[window:]
            feature_values[f"binary_sequence_{window}"][targets] = codes
            feature_valid[f"binary_sequence_{window}"][targets] = True

        for window in (5, 10, 20, 50, 100):
            if n <= window:
                continue
            windows = np.lib.stride_tricks.sliding_window_view(y, window)[:n - window]
            targets = indices[window:]
            feature_values[f"rolling_count_{window}"][targets] = windows.sum(axis=1)
            feature_valid[f"rolling_count_{window}"][targets] = True

        logs = np.log(values)
        for window in (10, 25, 50, 100):
            if n <= window:
                continue
            raw_windows = np.lib.stride_tricks.sliding_window_view(values, window)[:n - window]
            log_windows = np.lib.stride_tricks.sliding_window_view(logs, window)[:n - window]
            targets = indices[window:]
            feature_values[f"rolling_median_{window}"][targets] = np.median(raw_windows, axis=1)
            feature_values[f"rolling_log_std_{window}"][targets] = np.std(log_windows, axis=1)
            feature_valid[f"rolling_median_{window}"][targets] = True
            feature_valid[f"rolling_log_std_{window}"][targets] = True
            if window in (25, 50, 100):
                feature_values[f"rolling_q25_{window}"][targets] = np.quantile(raw_windows, 0.25, axis=1)
                feature_values[f"rolling_q75_{window}"][targets] = np.quantile(raw_windows, 0.75, axis=1)
                feature_valid[f"rolling_q25_{window}"][targets] = True
                feature_valid[f"rolling_q75_{window}"][targets] = True
    return {
        "values": feature_values,
        "valid": feature_valid,
        "segment_positions": segment_positions,
        "outcomes": outcomes[:length],
        "multipliers": multipliers[:length],
        "length": length,
    }


def compare(values, op, threshold):
    if op == "<=":
        return values <= threshold
    if op == ">=":
        return values >= threshold
    if op == "==":
        return values == threshold
    raise ValueError(f"Unsupported operator {op}")


def candidate_arrays(candidate, bank):
    if "components" in candidate:
        masks = []
        valid = []
        for component in candidate["components"]:
            feature = component["feature"]
            masks.append(compare(bank["values"][feature], component["op"], component["threshold"]))
            valid.append(bank["valid"][feature] & (bank["segment_positions"] >= component["required_history"]))
        return np.logical_and.reduce(masks), np.logical_and.reduce(valid)
    feature = candidate["feature"]
    required = max(candidate["requirements"].values())
    mask = compare(bank["values"][feature], candidate["op"], candidate["threshold"])
    valid = bank["valid"][feature] & (bank["segment_positions"] >= required)
    return mask, valid


def build_candidate_matrices(candidates, bank):
    masks = np.zeros((len(candidates), bank["length"]), dtype=bool)
    valid = np.zeros_like(masks)
    for index, candidate in enumerate(candidates):
        masks[index], valid[index] = candidate_arrays(candidate, bank)
    return masks, valid


def metric_summary(mask, available, outcomes, period):
    eligible = available & period
    selected = mask & eligible
    eligible_count = int(eligible.sum())
    selected_count = int(selected.sum())
    wins = int(outcomes[selected].sum()) if selected_count else 0
    losses = selected_count - wins
    baseline_wins = int(outcomes[eligible].sum()) if eligible_count else 0
    baseline_rate = baseline_wins / eligible_count if eligible_count else None
    rate = wins / selected_count if selected_count else None
    lower, upper = wilson_interval(wins, selected_count)
    return {
        "eligible": eligible_count,
        "selected": selected_count,
        "wins": wins,
        "losses": losses,
        "coverage": selected_count / eligible_count if eligible_count else None,
        "rate": rate,
        "wilson_95": [lower, upper],
        "baseline_rate": baseline_rate,
        "lift_vs_baseline": rate - baseline_rate if rate is not None and baseline_rate is not None else None,
        "above_break_even": rate - 0.5 if rate is not None else None,
        "ideal_net_per_unit_staked": 2 * rate - 1 if rate is not None else None,
        "baseline_ideal_net_per_unit_staked": 2 * baseline_rate - 1 if baseline_rate is not None else None,
    }


def chronological_block_rates(mask, available, outcomes, period, blocks=5):
    eligible_indices = np.flatnonzero(available & period)
    result = []
    for block_index, block in enumerate(np.array_split(eligible_indices, blocks), start=1):
        selected = block[mask[block]] if len(block) else np.asarray([], dtype=int)
        count = len(selected)
        wins = int(outcomes[selected].sum()) if count else 0
        result.append({
            "block": block_index,
            "eligible": int(len(block)),
            "selected": int(count),
            "wins": wins,
            "losses": int(count - wins),
            "rate": wins / count if count else None,
        })
    return result


def walk_forward_summary(mask, available, outcomes, discovery_end, fractions):
    folds = []
    n = discovery_end
    for fold_index, train_fraction in enumerate(fractions, start=1):
        eval_start = min(n, int(math.floor(n * train_fraction)))
        eval_end = min(n, int(math.floor(n * (train_fraction + 0.15))))
        train = np.arange(n) < eval_start
        evaluate = (np.arange(n) >= eval_start) & (np.arange(n) < eval_end)
        train_mask = available[:n] & train
        eval_mask = available[:n] & evaluate
        train_base = float(outcomes[:n][train_mask].mean()) if train_mask.any() else None
        selected = mask[:n] & eval_mask
        selected_n = int(selected.sum())
        wins = int(outcomes[:n][selected].sum()) if selected_n else 0
        rate = wins / selected_n if selected_n else None
        eval_base = float(outcomes[:n][eval_mask].mean()) if eval_mask.any() else None
        folds.append({
            "fold": fold_index,
            "train_end_position": eval_start,
            "evaluation_start_position": eval_start,
            "evaluation_end_position": eval_end,
            "train_baseline_rate": train_base,
            "evaluation_baseline_rate": eval_base,
            "selected": selected_n,
            "wins": wins,
            "losses": selected_n - wins,
            "rate": rate,
            "lift_vs_evaluation_baseline": rate - eval_base if rate is not None and eval_base is not None else None,
            "lift_vs_training_baseline": rate - train_base if rate is not None and train_base is not None else None,
        })
    return folds


def holm_adjust(p_values):
    p_values = np.asarray(p_values, dtype=float)
    order = np.argsort(p_values, kind="stable")
    adjusted = np.empty(len(p_values), dtype=float)
    running = 0.0
    total = len(p_values)
    for rank, index in enumerate(order):
        running = max(running, (total - rank) * p_values[index])
        adjusted[index] = min(1.0, running)
    return adjusted


def circular_shift_p_values(masks, valid, outcomes, discovery_end, segments, replicates, seed):
    candidate_n = masks.shape[0]
    y = outcomes[:discovery_end]
    selection = (masks[:, :discovery_end] & valid[:, :discovery_end]).astype(np.float32)
    availability = valid[:, :discovery_end].astype(np.float32)
    selected_counts = selection.sum(axis=1)
    available_counts = availability.sum(axis=1)
    observed_selected = selection @ y.astype(np.float32)
    observed_available = availability @ y.astype(np.float32)
    observed_lift = np.divide(observed_selected, selected_counts, out=np.zeros(candidate_n), where=selected_counts > 0)
    observed_lift -= np.divide(observed_available, available_counts, out=np.zeros(candidate_n), where=available_counts > 0)
    rng = np.random.default_rng(seed)
    permutation = np.zeros((replicates, discovery_end), dtype=np.float32)
    segment_slices = []
    for segment in segments:
        indices = segment[(segment < discovery_end)]
        if len(indices):
            segment_slices.append(indices)
    for replicate in range(replicates):
        for indices in segment_slices:
            shift = int(rng.integers(1, len(indices))) if len(indices) > 1 else 0
            permutation[replicate, indices] = np.roll(y[indices], shift)
    selected_hits = permutation @ selection.T
    available_hits = permutation @ availability.T
    permuted_lift = np.divide(selected_hits, selected_counts, out=np.zeros_like(selected_hits), where=selected_counts[None, :] > 0)
    permuted_lift -= np.divide(available_hits, available_counts, out=np.zeros_like(available_hits), where=available_counts[None, :] > 0)
    p_values = (1 + np.sum(permuted_lift >= observed_lift[None, :], axis=0)) / (replicates + 1)
    p_values[(selected_counts == 0) | (available_counts == 0)] = 1.0
    return p_values, observed_lift, selected_counts.astype(int), available_counts.astype(int)


def independent_null_checks(masks, valid, outcomes, discovery_end, segments, min_support, replicates, seed):
    discovery_valid = np.arange(discovery_end)
    common = np.zeros(discovery_end, dtype=bool)
    for segment in segments:
        indices = segment[segment < discovery_end]
        if len(indices) > 100:
            common[indices[100:]] = True
    if not common.any():
        common = np.ones(discovery_end, dtype=bool)
    p0 = float(outcomes[:discovery_end][common].mean())
    selected_masks = masks[:, :discovery_end]
    valid_masks = valid[:, :discovery_end]
    eligible_candidates = (selected_masks & valid_masks).sum(axis=1) >= min_support
    selected_counts = (selected_masks & valid_masks).sum(axis=1).astype(float)
    valid_counts = valid_masks.sum(axis=1).astype(float)
    observed_hits = (selected_masks & valid_masks).astype(np.float32) @ outcomes[:discovery_end].astype(np.float32)
    observed_base_hits = valid_masks.astype(np.float32) @ outcomes[:discovery_end].astype(np.float32)
    observed_lifts = np.divide(observed_hits, selected_counts, out=np.zeros_like(observed_hits), where=selected_counts > 0)
    observed_lifts -= np.divide(observed_base_hits, valid_counts, out=np.zeros_like(observed_base_hits), where=valid_counts > 0)
    observed_best = float(np.max(observed_lifts[eligible_candidates])) if eligible_candidates.any() else None

    rng = np.random.default_rng(seed)
    simulated = rng.binomial(1, p0, size=(replicates, discovery_end)).astype(np.float32)
    sim_selected_hits = simulated @ (selected_masks & valid_masks).astype(np.float32).T
    sim_valid_hits = simulated @ valid_masks.astype(np.float32).T
    sim_lifts = np.divide(sim_selected_hits, selected_counts[None, :], out=np.zeros_like(sim_selected_hits), where=selected_counts[None, :] > 0)
    sim_lifts -= np.divide(sim_valid_hits, valid_counts[None, :], out=np.zeros_like(sim_valid_hits), where=valid_counts[None, :] > 0)
    maxima = np.max(sim_lifts[:, eligible_candidates], axis=1) if eligible_candidates.any() else np.zeros(replicates)
    max_p = (1 + int(np.sum(maxima >= observed_best))) / (replicates + 1) if observed_best is not None else None

    def longest(values, target):
        maximum = 0
        for segment in segments:
            indices = segment[segment < discovery_end]
            if not len(indices):
                continue
            count = 0
            for position in indices:
                if values[position] == target:
                    count += 1
                    maximum = max(maximum, count)
                else:
                    count = 0
        return maximum

    observed_longest_below = longest(outcomes[:discovery_end], 0)
    observed_longest_above = longest(outcomes[:discovery_end], 1)
    simulated_below = np.zeros(replicates, dtype=int)
    simulated_above = np.zeros(replicates, dtype=int)
    for replicate in range(replicates):
        for segment in segments:
            indices = segment[segment < discovery_end]
            if not len(indices):
                continue
            draws = rng.binomial(1, p0, size=len(indices))
            for target, result in ((0, simulated_below), (1, simulated_above)):
                count = 0
                for value in draws:
                    if value == target:
                        count += 1
                        result[replicate] = max(result[replicate], count)
                    else:
                        count = 0

    streak_summary = {}
    for name, observed, distribution in (
        ("below_2x", observed_longest_below, simulated_below),
        ("at_least_2x", observed_longest_above, simulated_above),
    ):
        streak_summary[name] = {
            "observed_longest": int(observed),
            "iid_null_quantiles": {
                "p50": float(np.quantile(distribution, 0.50)),
                "p95": float(np.quantile(distribution, 0.95)),
                "p99": float(np.quantile(distribution, 0.99)),
            },
            "iid_null_tail_probability": float((1 + np.sum(distribution >= observed)) / (replicates + 1)),
        }
    return {
        "method": "independent Bernoulli target outcomes conditional on the frozen observed candidate triggers; streak simulations are generated independently within discovery segment lengths",
        "replicates": int(replicates),
        "seed": int(seed),
        "discovery_baseline_probability": p0,
        "candidate_support_gate": int(min_support),
        "observed_best_candidate_lift_vs_period_baseline": observed_best,
        "iid_null_best_lift_quantiles": {
            "p50": float(np.quantile(maxima, 0.50)),
            "p95": float(np.quantile(maxima, 0.95)),
            "p99": float(np.quantile(maxima, 0.99)),
        },
        "iid_null_probability_max_lift_at_least_observed": max_p,
        "longest_streaks": streak_summary,
        "assumption": "Conditional independent outcomes with a fixed discovery-period baseline. Candidate trigger times are held fixed; this complements, but does not replace, segment-wise circular-shift tests and Holm correction."
    }


def summarize_candidate(candidate, mask, valid, outcomes, period):
    result = metric_summary(mask, valid, outcomes, period)
    result["blocks"] = chronological_block_rates(mask, valid, outcomes, period, blocks=5)
    return result


def search_candidates():
    plan, raw, clean_rows, full_model, full_segments, quality, hashes = load_data()
    if (HERE / "final_test_record.json").exists():
        raise RuntimeError("The retrospective final period has already been evaluated; candidate search is locked")
    discovery_end = int(plan["chronological_split"]["discovery_rows"][1])
    validation_end = int(plan["chronological_split"]["validation_rows"][1])
    prefix = full_model.iloc[:validation_end].reset_index(drop=True)
    prefix_segments = [segment[segment < validation_end] for segment in full_segments if np.any(segment < validation_end)]
    candidates = candidate_definitions(plan)
    bank = build_feature_bank(prefix, prefix_segments, len(prefix))
    masks, valid = build_candidate_matrices(candidates, bank)
    outcomes = bank["outcomes"]
    positions = np.arange(len(prefix))
    discovery_period = positions < discovery_end
    validation_period = (positions >= discovery_end) & (positions < validation_end)

    permutation_reps = int(plan["false_discovery_control"]["dependence_test"]["replicates"])
    seed = int(plan["support_and_scoring"]["block_bootstrap"]["seed"])
    raw_p, observed_lifts, selected_counts, eligible_counts = circular_shift_p_values(
        masks, valid, outcomes, discovery_end, prefix_segments, permutation_reps, seed + 1,
    )
    adjusted = holm_adjust(raw_p)
    minimum_discovery = int(plan["support_and_scoring"]["minimum_discovery_selected"])
    iid_checks = independent_null_checks(
        masks, valid, outcomes, discovery_end, prefix_segments, minimum_discovery,
        int(plan["false_discovery_control"]["independent_null_simulation"]["replicates"]),
        int(plan["false_discovery_control"]["independent_null_simulation"]["seed"]),
    )

    records = []
    for index, candidate in enumerate(candidates):
        discovery = summarize_candidate(candidate, masks[index], valid[index], outcomes, discovery_period)
        validation = summarize_candidate(candidate, masks[index], valid[index], outcomes, validation_period)
        folds = walk_forward_summary(
            masks[index], valid[index], outcomes, discovery_end,
            plan["support_and_scoring"]["walk_forward"]["expanding_training_fractions_of_discovery"],
        )
        records.append({
            "candidate_id": candidate["candidate_id"],
            "family": candidate["family"],
            "trigger": candidate["description"],
            "feature_requirements_json": json.dumps(candidate["requirements"], sort_keys=True),
            "candidate_definition_json": json.dumps(candidate, sort_keys=True),
            "discovery_eligible": discovery["eligible"],
            "discovery_selected": discovery["selected"],
            "discovery_wins": discovery["wins"],
            "discovery_losses": discovery["losses"],
            "discovery_coverage": discovery["coverage"],
            "discovery_rate": discovery["rate"],
            "discovery_wilson95": json.dumps(discovery["wilson_95"]),
            "discovery_baseline": discovery["baseline_rate"],
            "discovery_lift": discovery["lift_vs_baseline"],
            "discovery_above_break_even": discovery["above_break_even"],
            "discovery_ideal_roi": discovery["ideal_net_per_unit_staked"],
            "discovery_blocks_json": json.dumps(discovery["blocks"], sort_keys=True),
            "discovery_circular_shift_p": float(raw_p[index]),
            "discovery_holm_p": float(adjusted[index]),
            "discovery_selected_count_check": int(selected_counts[index]),
            "walk_forward_folds_json": json.dumps(folds, sort_keys=True),
            "walk_forward_positive_lift_folds": sum(
                1 for fold in folds if fold["lift_vs_evaluation_baseline"] is not None and fold["lift_vs_evaluation_baseline"] > 0
            ),
            "validation_eligible": validation["eligible"],
            "validation_selected": validation["selected"],
            "validation_wins": validation["wins"],
            "validation_losses": validation["losses"],
            "validation_coverage": validation["coverage"],
            "validation_rate": validation["rate"],
            "validation_wilson95": json.dumps(validation["wilson_95"]),
            "validation_baseline": validation["baseline_rate"],
            "validation_lift": validation["lift_vs_baseline"],
            "validation_above_break_even": validation["above_break_even"],
            "validation_ideal_roi": validation["ideal_net_per_unit_staked"],
            "validation_blocks_json": json.dumps(validation["blocks"], sort_keys=True),
            "retrospective_final_selected": None,
            "retrospective_final_rate": None,
            "retrospective_final_baseline": None,
            "retrospective_final_ideal_roi": None,
            "status": "INSUFFICIENT_DATA" if discovery["selected"] < minimum_discovery or validation["selected"] < plan["support_and_scoring"]["minimum_validation_selected"] else "REJECTED",
        })

    minimum_validation = int(plan["support_and_scoring"]["minimum_validation_selected"])
    eligible_shortlist = [
        record for record in records
        if record["discovery_selected"] >= minimum_discovery
        and record["validation_selected"] >= minimum_validation
    ]
    eligible_shortlist.sort(key=lambda item: (
        -(item["validation_ideal_roi"] if item["validation_ideal_roi"] is not None else -1e9),
        -(item["discovery_ideal_roi"] if item["discovery_ideal_roi"] is not None else -1e9),
        item["candidate_id"],
    ))
    shortlist_size = int(plan["shortlist_freeze"]["size"])
    chosen = eligible_shortlist[:shortlist_size]
    shortlist_items = []
    record_lookup = {record["candidate_id"]: record for record in records}
    candidate_lookup = {candidate["candidate_id"]: candidate for candidate in candidates}
    for rank, record in enumerate(chosen, start=1):
        candidate = candidate_lookup[record["candidate_id"]]
        record["status"] = "EDGE_UNVERIFIED"
        shortlist_items.append({
            "rank": rank,
            "candidate": candidate,
            "discovery_selected": record["discovery_selected"],
            "validation_selected": record["validation_selected"],
            "discovery_rate": record["discovery_rate"],
            "validation_rate": record["validation_rate"],
            "discovery_ideal_roi": record["discovery_ideal_roi"],
            "validation_ideal_roi": record["validation_ideal_roi"],
        })
    frozen = {
        "schema_version": 1,
        "plan_id": plan["plan_id"],
        "plan_sha256": hashes["plan_sha256"],
        "raw_dataset_sha256": hashes["raw_sha256"],
        "clean_dataset_sha256": hashes["clean_sha256"],
        "candidate_family_sha256": canonical_hash(candidates),
        "selected_rule_set_sha256": canonical_hash([item["candidate"] for item in shortlist_items]),
        "shortlist_size": len(shortlist_items),
        "shortlist": shortlist_items,
        "selection_rule": plan["shortlist_freeze"]["ranking"],
        "frozen_at_utc": plan["frozen_at_utc"],
        "final_test_opened": False,
        "final_period_status": plan["chronological_split"]["final_period_status"],
        "no_candidate_passes_historical_edge_gate": True,
    }
    write_json(SHORTLIST_PATH, frozen)
    pd.DataFrame(records).to_csv(CANDIDATE_PATH, index=False, float_format="%.12g")
    write_json(NULL_PATH, iid_checks)
    write_json(HERE / "search_summary.json", {
        "candidate_count": len(candidates),
        "shortlist_count": len(shortlist_items),
        "discovery_positions": [0, discovery_end],
        "validation_positions": [discovery_end, validation_end],
        "final_test_opened": False,
        "familywise_alpha": plan["false_discovery_control"]["family_wise_alpha"],
        "holms_corrected_candidates": len(adjusted),
        "iid_null": iid_checks,
        "shortlist": shortlist_items,
    })
    search_fingerprint = {
        "plan_sha256": hashes["plan_sha256"],
        "candidate_family_sha256": canonical_hash(candidates),
        "candidate_rules_sha256": sha256_file(CANDIDATE_PATH),
        "shortlist_sha256": sha256_file(SHORTLIST_PATH),
        "null_checks_sha256": sha256_file(NULL_PATH),
    }
    first_pass_path = HERE / "search_first_pass.json"
    reproducibility_path = HERE / "search_reproducibility.json"
    if not first_pass_path.exists():
        write_json(first_pass_path, search_fingerprint)
    else:
        with open(first_pass_path, encoding="utf-8") as handle:
            first_pass = json.load(handle)
        comparisons = {key: first_pass.get(key) == value for key, value in search_fingerprint.items()}
        write_json(reproducibility_path, {
            "reproducible": all(comparisons.values()),
            "first_run": first_pass,
            "second_run": search_fingerprint,
            "comparisons": comparisons,
        })
        if not all(comparisons.values()):
            raise RuntimeError("Repeated discovery/validation search did not reproduce identical outputs")
    print(json.dumps({
        "candidate_count": len(candidates),
        "shortlist_count": len(shortlist_items),
        "shortlist": [item["candidate"]["candidate_id"] for item in shortlist_items],
        "final_test_opened": False,
        "null_max_lift_p": iid_checks["iid_null_probability_max_lift_at_least_observed"],
    }, indent=2))


def audit_report():
    plan, raw, clean_rows, model, segments, existing_quality, hashes = load_data()
    raw_count = len(raw)
    source_counts = raw["source"].value_counts(dropna=False).to_dict()
    timestamp_valid = int(raw["timestamp_utc"].notna().sum())
    platform_ts_utc = pd.to_datetime(raw["platform_timestamp"], format="mixed", utc=True, errors="coerce")
    platform_timestamp_populated = int(raw["platform_timestamp"].fillna("").astype(str).str.strip().ne("").sum())
    platform_timestamp_valid = int(platform_ts_utc.notna().sum())
    platform_timestamp_source_counts = raw.loc[platform_ts_utc.notna(), "source"].value_counts(dropna=False).to_dict()
    platform_timestamp_equals_timestamp = int((platform_ts_utc.notna() & platform_ts_utc.eq(raw["timestamp_utc"])).sum())
    platform_timestamp_verified = int(raw.loc[platform_ts_utc.notna(), "continuity_verified_bool"].sum())
    platform_timestamp_confidence_counts = raw.loc[platform_ts_utc.notna(), "identity_confidence"].value_counts(dropna=False).to_dict()
    platform_ts_range = [platform_ts_utc.min().isoformat(), platform_ts_utc.max().isoformat()] if platform_timestamp_valid else []
    platform_id_count = int(raw["platform_round_id"].fillna("").astype(str).str.strip().ne("").sum())
    platform_index_count = int(raw["platform_round_index"].notna().sum())
    duplicates = {
        "round_id": int(raw["round_id"].duplicated().sum()),
        "round_index": int(raw["round_index"].duplicated().sum()),
        "primary_id": int(raw["id"].duplicated().sum()),
    }
    multipliers = raw["multiplier"].to_numpy(dtype=float)
    precision_bad = int(np.sum(np.abs(multipliers * 100 - np.round(multipliers * 100)) > 1e-6))
    below_one = int(np.sum(multipliers < 1.0))
    invalid_mult = int(np.sum(~np.isfinite(multipliers)))
    indices = raw["round_index"].to_numpy(dtype=int)
    missing_index_count = int(sum(max(0, int(right - left - 1)) for left, right in zip(indices[:-1], indices[1:])))
    timestamp_values = raw["timestamp_utc"].tolist()
    inversions = 0
    for previous, current in zip(timestamp_values[:-1], timestamp_values[1:]):
        if pd.notna(previous) and pd.notna(current) and current < previous:
            inversions += 1
    clean_count = int(len(clean_rows))
    dropped = int(raw_count - clean_count)
    cleaning = existing_quality.get("cleaning", {})
    verified_count = int(raw["continuity_verified_bool"].sum())
    gap_count = int(raw["gap_before_bool"].sum())
    proof_counts = raw["continuity_proof"].value_counts(dropna=False).to_dict()
    identity_counts = raw["round_identity_type"].value_counts(dropna=False).to_dict()
    index_source_counts = raw["round_index_source"].value_counts(dropna=False).to_dict()
    raw_ts_utc = raw["timestamp_utc"].dropna()
    start_ts = raw_ts_utc.min().isoformat() if len(raw_ts_utc) else None
    end_ts = raw_ts_utc.max().isoformat() if len(raw_ts_utc) else None
    model_range = [int(model["round_index"].min()), int(model["round_index"].max())] if len(model) else []
    segment_sizes = [int(len(segment)) for segment in segments]
    report = f"""# Data quality report

## Source and snapshot

- Source: PostgreSQL table `public.aviator_rounds`; the recorded extraction is an all-source snapshot taken {plan['source']['snapshot_time_utc']}.
- Extracted rows: **{raw_count:,}**, matching the extraction manifest table count. Snapshot SHA-256: `{hashes['raw_sha256']}`.
- The local PostgreSQL service did not answer `pg_isready` during this run, so the live table count could not be refreshed. Reported count is the verified historical extraction boundary, not a claim about the current live table.
- Source mix: `{json.dumps({str(k): int(v) for k, v in source_counts.items()}, sort_keys=True)}`.

## Identity, order, and continuity

- `round_id`, primary `id`, and local `round_index` duplicates: `{duplicates}`.
- All `{len(raw):,}` entries use `round_identity_type=COLLECTOR_OBSERVATION_HASH` and `round_index_source=LOCAL_SEQUENCE`; platform round IDs present: **{platform_id_count:,}**, platform indices present: **{platform_index_count:,}**.
- `continuity_verified=true`: **{verified_count:,}** rows; proof counts: `{json.dumps({str(k): int(v) for k, v in proof_counts.items()}, sort_keys=True)}`. These flags establish overlap-verified relative order only, not platform identity.
- Declared `gap_before` rows: **{gap_count:,}**. Missing local index values in the raw snapshot: **{missing_index_count:,}**. Timestamp inversions in local-index order: **{inversions:,}**.
- Compatibility `timestamp` values parse in UTC on **{timestamp_valid:,}/{raw_count:,}** rows, spanning `{start_ts}` to `{end_ts}`. `platform_timestamp` is populated on **{platform_timestamp_populated:,}** rows and parses on **{platform_timestamp_valid:,}** (unparseable populated values: {platform_timestamp_populated - platform_timestamp_valid:,}; range `{platform_ts_range[0] if platform_ts_range else None}` to `{platform_ts_range[1] if platform_ts_range else None}`); source counts are `{json.dumps({str(k): int(v) for k, v in platform_timestamp_source_counts.items()}, sort_keys=True)}`. It equals the compatibility timestamp on **{platform_timestamp_equals_timestamp:,}** rows, has **{platform_timestamp_verified:,}** continuity-verified rows, and identity-confidence counts `{json.dumps({str(k): int(v) for k, v in platform_timestamp_confidence_counts.items()}, sort_keys=True)}`. Provenance is unverified; do not treat these values as confirmed platform event times.
- Predictive pattern evaluation is restricted to **{len(model):,}** kept rows with `HISTORY_SNAPSHOT_OVERLAP` order proof, split into **{len(segments):,}** segments (largest {max(segment_sizes) if segment_sizes else 0:,}). Features reset at each declared gap, unverified row, or missing raw index.

## Multiplier validity and overlap cleaning

- Minimum multiplier: **{float(np.nanmin(multipliers)):.2f}x**; maximum: **{float(np.nanmax(multipliers)):.2f}x**.
- Multipliers below 1.00x: **{below_one:,}**; non-finite values: **{invalid_mult:,}**; values not representable to two decimals: **{precision_bad:,}**.
- Existing audited clean set retains **{clean_count:,}** rows and excludes **{dropped:,}** rows: exact same-timestamp same-multiplier **{int(cleaning.get('D1_exact_ts_same_mult_dropped', 0)):,}**, repeated catch-up overlap **{int(cleaning.get('D2_overlap_reobserved_dropped', 0)):,}**, and near-timestamp same-multiplier captures **{int(cleaning.get('D3_near_ts_same_mult_dropped', 0)):,}**.
- Equal multipliers alone are never a deduplication key. Legitimate adjacent equal outcomes remain. Exclusion decisions rely on timestamp/source/sequence overlap evidence and are listed in `../artifacts/dropped_rows.csv`.

## Coverage and limits

- The table combines `legacy_import`, live collector, and collector catch-up observations. It is a convenience archive, not a random sample of all platform rounds; gaps, catch-up batches, and changing collection coverage can bias frequency and chronology summaries.
- No rows contain a platform round ID or index. Although `{platform_timestamp_valid:,}` rows have an explicit `platform_timestamp`, all are from legacy imports and none are continuity-verified; daily/UTC-hour summaries are reported only as unverified legacy-field diagnostics, not trusted game-time effects. These values do not enter candidate features, ordering, continuity, or prospective claims.
- Historical reports in the repository already examined earlier portions of this archive. The current predictive order subset remains order-only and cannot establish a repeatable or live betting edge.
- Raw snapshot hash: `{hashes['raw_sha256']}`. Clean-set hash: `{hashes['clean_sha256']}`. Existing quality-audit hash: `{hashes['quality_sha256']}`.
"""
    QUALITY_REPORT_PATH.write_text(report, encoding="utf-8")
    print(json.dumps({
        "raw_rows": raw_count,
        "clean_rows": clean_count,
        "verified_order_rows": len(model),
        "segments": len(segments),
        "platform_round_ids": platform_id_count,
        "platform_round_indices": platform_index_count,
        "data_quality_report": str(QUALITY_REPORT_PATH),
    }, indent=2))


def run_lengths_by_outcome(segments, outcomes, value):
    lengths = []
    for segment in segments:
        current = 0
        for position in segment:
            if outcomes[position] == value:
                current += 1
            elif current:
                lengths.append(current)
                current = 0
        if current:
            lengths.append(current)
    return lengths


def summarize_lengths(lengths):
    if not lengths:
        return {"count": 0, "longest": 0, "median": None, "p90": None, "distribution": {}}
    counts = pd.Series(lengths).value_counts().sort_index()
    return {
        "count": int(len(lengths)),
        "longest": int(max(lengths)),
        "median": float(np.median(lengths)),
        "p90": float(np.quantile(lengths, 0.90)),
        "distribution": {str(int(key)): int(value) for key, value in counts.items()},
    }


def descriptive_statistics(clean_rows, model, segments):
    values = clean_rows["multiplier"].to_numpy(dtype=float)
    hits = values >= 2.0
    hit_count = int(hits.sum())
    low, high = wilson_interval(hit_count, len(values))
    bands = [
        ("<1.20", lambda x: x < 1.2),
        ("1.20-<1.50", lambda x: (x >= 1.2) & (x < 1.5)),
        ("1.50-<2.00", lambda x: (x >= 1.5) & (x < 2.0)),
        ("2.00-<3.00", lambda x: (x >= 2.0) & (x < 3.0)),
        ("3.00-<5.00", lambda x: (x >= 3.0) & (x < 5.0)),
        ("5.00-<10.00", lambda x: (x >= 5.0) & (x < 10.0)),
        (">=10.00", lambda x: x >= 10.0),
    ]
    band_counts = {label: int(np.sum(predicate(values))) for label, predicate in bands}
    quantile_levels = [0.0, 0.01, 0.05, 0.10, 0.25, 0.50, 0.75, 0.90, 0.95, 0.99, 1.0]
    quantiles = {str(level): float(np.quantile(values, level)) for level in quantile_levels}
    y = (model["multiplier"].to_numpy(dtype=float) >= 2.0).astype(np.int8)
    model_segments = [segment for segment in segments if len(segment)]
    below_lengths = run_lengths_by_outcome(model_segments, y, 0)
    above_lengths = run_lengths_by_outcome(model_segments, y, 1)
    waiting_times = []
    for segment in model_segments:
        local_hits = np.flatnonzero(y[segment] == 1)
        if len(local_hits) > 1:
            waiting_times.extend(np.diff(local_hits).astype(int).tolist())
    wait_summary = {
        "completed_hit_to_hit_intervals": int(len(waiting_times)),
        "mean_rounds": float(np.mean(waiting_times)) if waiting_times else None,
        "median_rounds": float(np.median(waiting_times)) if waiting_times else None,
        "p90_rounds": float(np.quantile(waiting_times, 0.90)) if waiting_times else None,
        "longest_rounds": int(max(waiting_times)) if waiting_times else None,
    }
    rolling = {}
    rolling_points = {}
    for window in (25, 50, 100, 250, 500):
        rates = []
        points = []
        for segment in model_segments:
            if len(segment) <= window:
                continue
            target_indices = segment[window:]
            window_values = np.lib.stride_tricks.sliding_window_view(y[segment], window)[:len(segment) - window]
            local_rates = window_values.mean(axis=1)
            rates.extend(local_rates.astype(float).tolist())
            points.extend(zip(target_indices.astype(int).tolist(), local_rates.astype(float).tolist()))
        rolling[str(window)] = {
            "valid_windows": int(len(rates)),
            "mean_rate": float(np.mean(rates)) if rates else None,
            "min_rate": float(np.min(rates)) if rates else None,
            "p05_rate": float(np.quantile(rates, 0.05)) if rates else None,
            "median_rate": float(np.median(rates)) if rates else None,
            "p95_rate": float(np.quantile(rates, 0.95)) if rates else None,
            "max_rate": float(np.max(rates)) if rates else None,
        }
        rolling_points[str(window)] = points

    temporal = clean_rows.sort_values(["timestamp_utc", "round_index"], kind="stable").reset_index(drop=True)
    time_blocks = []
    for block_number, block in enumerate(np.array_split(np.arange(len(temporal)), 10), start=1):
        if not len(block):
            continue
        subset = temporal.iloc[block]
        block_values = subset["multiplier"].to_numpy(dtype=float)
        block_hits = int(np.sum(block_values >= 2.0))
        time_blocks.append({
            "block": block_number,
            "n": int(len(subset)),
            "hits": block_hits,
            "rate": block_hits / len(subset),
            "capture_start_utc": subset["timestamp_utc"].min().isoformat(),
            "capture_end_utc": subset["timestamp_utc"].max().isoformat(),
        })
    clean_platform_ts = pd.to_datetime(clean_rows["platform_timestamp"], format="mixed", utc=True, errors="coerce")
    actual = clean_rows.loc[clean_platform_ts.notna()].copy()
    actual["platform_ts_utc"] = clean_platform_ts[clean_platform_ts.notna()]
    actual["target_hit"] = (actual["multiplier"].to_numpy(dtype=float) >= 2.0).astype(int)
    daily_groups = actual.groupby(actual["platform_ts_utc"].dt.strftime("%Y-%m-%d"), sort=True)
    platform_daily = [
        {"utc_date": str(day), "rows": int(group["target_hit"].count()), "hits": int(group["target_hit"].sum()), "rate": float(group["target_hit"].mean())}
        for day, group in daily_groups
    ]
    hourly_groups = actual.groupby(actual["platform_ts_utc"].dt.hour, sort=True)
    platform_hourly = [
        {"utc_hour": int(hour), "rows": int(group["target_hit"].count()), "hits": int(group["target_hit"].sum()), "rate": float(group["target_hit"].mean())}
        for hour, group in hourly_groups
    ]
    platform_time_count = int(len(actual))
    platform_timestamp_source_counts = actual["source"].value_counts(dropna=False).to_dict()
    platform_timestamp_confidence_counts = actual["identity_confidence"].value_counts(dropna=False).to_dict()

    return {
        "clean_rows": int(len(clean_rows)),
        "target_hits": hit_count,
        "target_rate": hit_count / len(values) if len(values) else None,
        "target_wilson_95": [low, high],
        "multiplier_quantiles": quantiles,
        "multiplier_bands": band_counts,
        "verified_order_rows": int(len(model)),
        "verified_segments": int(len(model_segments)),
        "below_2x_streaks": summarize_lengths(below_lengths),
        "at_least_2x_streaks": summarize_lengths(above_lengths),
        "hit_waiting_times": wait_summary,
        "rolling_rates": rolling,
        "rolling_points": rolling_points,
        "capture_time_chronological_blocks": time_blocks,
        "platform_timestamp_rows": platform_time_count,
        "platform_daily_counts": platform_daily,
        "platform_hourly_rates": platform_hourly,
        "platform_timestamp_provenance": {
            "field": "platform_timestamp",
            "retained_rows": platform_time_count,
            "source_counts": {str(k): int(v) for k, v in platform_timestamp_source_counts.items()},
            "identity_confidence_counts": {str(k): int(v) for k, v in platform_timestamp_confidence_counts.items()},
            "continuity_verified_rows": int(actual["continuity_verified_bool"].sum()) if platform_time_count else 0,
            "interpretation": "Unverified legacy-field diagnostics only; not used for event ordering, candidate features, continuity, or prospective claims.",
        },
    }


def segment_period_indices(segments, period):
    return [segment[period[segment]] for segment in segments if len(segment[period[segment]])]


def block_bootstrap_interval(mask, available, outcomes, period, segments, replicates, block_length, seed):
    rng = np.random.default_rng(seed)
    period_segments = segment_period_indices(segments, period)
    roi_values = []
    lift_values = []
    for _ in range(replicates):
        sampled = []
        for indices in period_segments:
            size = len(indices)
            if not size:
                continue
            draws = []
            while len(draws) < size:
                start = int(rng.integers(0, size))
                take = min(block_length, size - len(draws))
                draws.extend(indices[(start + np.arange(take)) % size].tolist())
            sampled.extend(draws)
        if not sampled:
            continue
        sampled = np.asarray(sampled, dtype=int)
        selected = mask[sampled] & available[sampled]
        eligible = available[sampled]
        if not selected.any() or not eligible.any():
            continue
        rule_rate = float(outcomes[sampled][selected].mean())
        base_rate = float(outcomes[sampled][eligible].mean())
        roi_values.append(2 * rule_rate - 1)
        lift_values.append(rule_rate - base_rate)
    if not roi_values:
        return {"replicates": 0, "roi_95": [None, None], "lift_95": [None, None], "probability_positive_roi": None}
    return {
        "replicates": int(len(roi_values)),
        "block_length_rounds": int(block_length),
        "roi_95": [float(np.quantile(roi_values, 0.025)), float(np.quantile(roi_values, 0.975))],
        "lift_95": [float(np.quantile(lift_values, 0.025)), float(np.quantile(lift_values, 0.975))],
        "probability_positive_roi": float(np.mean(np.asarray(roi_values) > 0)),
    }


def random_entry_comparison(mask, available, outcomes, period, segments, replicates, seed):
    rng = np.random.default_rng(seed)
    target_total = 0
    win_total = 0
    strata = []
    for segment in segments:
        indices = segment[period[segment] & available[segment]]
        if not len(indices):
            continue
        selected = mask[indices]
        count = int(selected.sum())
        target_total += count
        win_total += int(outcomes[indices][selected].sum())
        strata.append((indices, count))
    if target_total == 0:
        return {"matched_entries": 0, "rate_mean": None, "rate_95": [None, None], "p_random_at_least_observed": None}
    observed_rate = win_total / target_total
    random_rates = []
    for _ in range(replicates):
        wins = 0
        count_total = 0
        for indices, count in strata:
            if count == 0:
                continue
            choice = rng.choice(indices, size=count, replace=False)
            wins += int(outcomes[choice].sum())
            count_total += count
        random_rates.append(wins / count_total if count_total else 0.0)
    return {
        "matched_entries": target_total,
        "rate_mean": float(np.mean(random_rates)),
        "rate_95": [float(np.quantile(random_rates, 0.025)), float(np.quantile(random_rates, 0.975))],
        "p_random_at_least_observed": float((1 + np.sum(np.asarray(random_rates) >= observed_rate)) / (replicates + 1)),
    }


def candidate_feature_context(candidate, bank, model, position):
    if "components" in candidate:
        values = []
        for component in candidate["components"]:
            feature = component["feature"]
            values.append({
                "feature": feature,
                "value": float(bank["values"][feature][position]),
                "operator": component["op"],
                "threshold": component["threshold"],
            })
    else:
        feature = candidate["feature"]
        values = [{
            "feature": feature,
            "value": float(bank["values"][feature][position]),
            "operator": candidate["op"],
            "threshold": candidate["threshold"],
        }]
    history = max(candidate["requirements"].values())
    start = position - history
    segment_id = int(model.iloc[position]["segment_id"])
    segment_pos = int(model.iloc[position]["segment_pos"])
    start_index = None
    if start >= 0 and segment_pos >= history and int(model.iloc[start]["segment_id"]) == segment_id:
        start_index = int(model.iloc[start]["round_index"])
    return {
        "features": values,
        "feature_history_rounds": history,
        "feature_start_round_index": start_index,
        "feature_end_round_index": int(model.iloc[position - 1]["round_index"]) if segment_pos > 0 else None,
        "feature_end_position": position - 1 if segment_pos > 0 else None,
    }


def backtest_rule(candidate, mask, available, model, plan):
    session_length = int(plan["session_backtest"]["session_length_eligible_rounds_example"])
    loss_limit = float(plan["session_backtest"]["session_loss_limit_units_example"])
    consecutive_limit = int(plan["session_backtest"]["consecutive_loss_limit_example"])
    profit_goal = float(plan["session_backtest"]["profit_goal_units_example"])
    cooldown_config = int(plan["session_backtest"]["cooldown_rounds_example"])
    outcomes = (model["multiplier"].to_numpy(dtype=float) >= 2.0).astype(int)
    decisions = [None] * len(model)
    profits = np.zeros(len(model), dtype=float)
    session_keys = set()
    stopped_sessions = set()
    opportunities = int(np.sum(mask & available))
    current_session = None
    session_net = 0.0
    session_loss_streak = 0
    session_stopped_reason = None
    cooldown_remaining = 0
    current_global_losing_streak = 0
    longest_losing_streak = 0

    for position, row in model.iterrows():
        session_key = (int(row["segment_id"]), int(row["segment_pos"]) // session_length)
        session_keys.add(session_key)
        if session_key != current_session:
            current_session = session_key
            session_net = 0.0
            session_loss_streak = 0
            session_stopped_reason = None
            cooldown_remaining = 0
            current_global_losing_streak = 0
        signal = bool(mask[position] and available[position])
        if session_stopped_reason:
            action, reason = "STOP", session_stopped_reason
        elif cooldown_remaining > 0:
            action, reason = "WAIT", "COOLDOWN"
            cooldown_remaining -= 1
        elif not available[position]:
            action, reason = "WAIT", "INSUFFICIENT_HISTORY"
        elif not signal:
            action, reason = "WAIT", "CONDITION_FALSE"
        else:
            action, reason = "BET", "RULE_TRIGGERED"
            profit = 1.0 if outcomes[position] else -1.0
            profits[position] = profit
            session_net += profit
            if outcomes[position]:
                session_loss_streak = 0
                current_global_losing_streak = 0
            else:
                session_loss_streak += 1
                current_global_losing_streak += 1
                longest_losing_streak = max(longest_losing_streak, current_global_losing_streak)
            if cooldown_config:
                cooldown_remaining = cooldown_config
            if session_net <= -loss_limit:
                session_stopped_reason = "SESSION_LOSS_LIMIT"
                stopped_sessions.add(session_key)
            elif session_loss_streak >= consecutive_limit:
                session_stopped_reason = "CONSECUTIVE_LOSS_LIMIT"
                stopped_sessions.add(session_key)
            elif session_net >= profit_goal:
                session_stopped_reason = "PROFIT_GOAL"
                stopped_sessions.add(session_key)
        decisions[position] = {
            "decision": action,
            "reason_code": reason,
            "trigger": signal,
            "profit_units": float(profits[position]),
            "session_id": f"segment-{session_key[0]}-session-{session_key[1]}",
        }

    bets = np.asarray([item["decision"] == "BET" for item in decisions])
    bet_count = int(bets.sum())
    wins = int(outcomes[bets].sum()) if bet_count else 0
    losses = bet_count - wins
    net = float(profits.sum())
    equity = np.cumsum(profits)
    peaks = np.maximum.accumulate(np.concatenate(([0.0], equity)))
    drawdown = peaks[1:] - equity
    periods = {
        "discovery": np.arange(len(model)) < int(plan["chronological_split"]["discovery_rows"][1]),
        "validation": (np.arange(len(model)) >= int(plan["chronological_split"]["discovery_rows"][1])) & (np.arange(len(model)) < int(plan["chronological_split"]["validation_rows"][1])),
        "retrospective_final": np.arange(len(model)) >= int(plan["chronological_split"]["validation_rows"][1]),
    }
    period_results = {}
    for name, period in periods.items():
        local_bets = bets & period
        count = int(local_bets.sum())
        local_wins = int(outcomes[local_bets].sum()) if count else 0
        period_results[name] = {
            "eligible_targets": int(np.sum(available & period)),
            "trigger_opportunities": int(np.sum(mask & available & period)),
            "actual_bets": count,
            "wins": local_wins,
            "losses": count - local_wins,
            "net_profit_units": float(profits[period].sum()),
            "return_per_unit_staked": float(profits[period].sum() / count) if count else None,
        }
    return {
        "candidate_id": candidate["candidate_id"],
        "ideal_settlement_assumption": True,
        "target_multiplier": 2.0,
        "stake_per_bet_units": 1.0,
        "eligible_targets": int(available.sum()),
        "trigger_opportunities": opportunities,
        "actual_bets": bet_count,
        "wins": wins,
        "losses": losses,
        "net_profit_units": net,
        "return_per_unit_staked": net / bet_count if bet_count else None,
        "maximum_drawdown_units": float(np.max(drawdown)) if len(drawdown) else 0.0,
        "longest_losing_sequence": int(longest_losing_streak),
        "session_count": int(len(session_keys)),
        "sessions_stopped": int(len(stopped_sessions)),
        "session_stop_frequency": len(stopped_sessions) / len(session_keys) if session_keys else None,
        "periods": period_results,
        "decisions": decisions,
        "profits": profits,
    }


def execution_sensitivity(backtest, model, split_start, replicates, seed):
    decisions = backtest["decisions"]
    outcomes = (model["multiplier"].to_numpy(dtype=float) >= 2.0).astype(int)
    test_positions = np.arange(len(model)) >= split_start
    bet_positions = np.asarray([item["decision"] == "BET" for item in decisions]) & test_positions
    entries = np.flatnonzero(bet_positions)
    if not len(entries):
        return {"eligible_bets": 0, "scenarios": []}
    rng = np.random.default_rng(seed)
    scenario_rows = []
    for failure_rate in (0.05, 0.10):
        profits = []
        bet_counts = []
        for _ in range(replicates):
            placed = rng.random(len(entries)) >= failure_rate
            selected = entries[placed]
            selected_outcomes = outcomes[selected]
            unit_profit = np.where(selected_outcomes == 1, 1.0, -1.0)
            profits.append(float(unit_profit.sum()))
            bet_counts.append(int(len(selected)))
        scenario_rows.append({
            "scenario": f"{int(failure_rate * 100)}% entry failures; missed entries not staked",
            "mean_actual_bets": float(np.mean(bet_counts)),
            "mean_net_profit_units": float(np.mean(profits)),
            "net_profit_95_simulation_interval": [float(np.quantile(profits, 0.025)), float(np.quantile(profits, 0.975))],
        })
    loss_settlement_profits = []
    payout_haircut_profit = 0.0
    test_outcomes = outcomes[entries]
    payout_haircut_profit = float(np.where(test_outcomes == 1, 0.98, -1.0).sum())
    for _ in range(replicates):
        failed_wins = (test_outcomes == 1) & (rng.random(len(entries)) < 0.01)
        unit_profit = np.where(test_outcomes == 1, 1.0, -1.0)
        unit_profit[failed_wins] = -1.0
        loss_settlement_profits.append(float(unit_profit.sum()))
    scenario_rows.extend([
        {
            "scenario": "1% of winning settlements fail and are treated as losses",
            "mean_actual_bets": int(len(entries)),
            "mean_net_profit_units": float(np.mean(loss_settlement_profits)),
            "net_profit_95_simulation_interval": [float(np.quantile(loss_settlement_profits, 0.025)), float(np.quantile(loss_settlement_profits, 0.975))],
        },
        {
            "scenario": "Winning payout reduced to 1.98x; losses remain -1 unit",
            "mean_actual_bets": int(len(entries)),
            "mean_net_profit_units": payout_haircut_profit,
            "net_profit_95_simulation_interval": [payout_haircut_profit, payout_haircut_profit],
        },
    ])
    return {"eligible_bets": int(len(entries)), "simulation_replicates": int(replicates), "scenarios": scenario_rows}


def format_percent(value, digits=2):
    return "n/a" if value is None or pd.isna(value) else f"{100 * value:.{digits}f}%"


def format_number(value, digits=3):
    return "n/a" if value is None or pd.isna(value) else f"{value:.{digits}f}"


def generate_plots(clean_rows, model, segments, descriptive, candidate_results, backtests):
    plot_dir = HERE / "plots"
    plot_dir.mkdir(parents=True, exist_ok=True)
    values = clean_rows["multiplier"].to_numpy(dtype=float)
    fig, axis = plt.subplots(figsize=(9, 5))
    axis.hist(np.log10(values), bins=55, color="#3b82f6", alpha=0.85)
    axis.set_xlabel("log10(multiplier)")
    axis.set_ylabel("round records")
    axis.set_title("Aviator multiplier distribution, retained archive rows")
    fig.tight_layout()
    fig.savefig(plot_dir / "multiplier_distribution.png", dpi=160)
    plt.close(fig)

    fig, axis = plt.subplots(figsize=(10, 5))
    for window, points in descriptive["rolling_points"].items():
        if not points:
            continue
        x_values = [point[0] for point in points]
        y_values = [point[1] for point in points]
        axis.plot(x_values, y_values, linewidth=0.8, alpha=0.7, label=f"{window} rounds")
    axis.axhline(0.5, color="#dc2626", linestyle="--", linewidth=1, label="50% break-even")
    axis.set_ylim(0, 1)
    axis.set_xlabel("overlap-verified row position")
    axis.set_ylabel("past-window >=2x rate")
    axis.set_title("Past-only rolling rates inside verified-order segments")
    axis.legend(ncol=3)
    fig.tight_layout()
    fig.savefig(plot_dir / "rolling_2x_rates.png", dpi=160)
    plt.close(fig)

    if candidate_results:
        rows = []
        labels = []
        for result in candidate_results:
            labels.append(result["candidate_id"])
            rows.append([result.get("discovery_rate"), result.get("validation_rate"), result.get("retrospective_final_rate")])
        matrix = np.asarray([[np.nan if value is None else value for value in row] for row in rows], dtype=float)
        fig, axis = plt.subplots(figsize=(10, 5))
        x = np.arange(len(labels))
        width = 0.24
        for offset, (name, column, color) in enumerate((
            ("Discovery", 0, "#2563eb"),
            ("Validation", 1, "#16a34a"),
            ("Retrospective final", 2, "#f97316"),
        )):
            axis.bar(x + (offset - 1) * width, matrix[:, column], width, label=name, color=color)
        axis.axhline(0.5, color="#dc2626", linestyle="--", linewidth=1, label="50% break-even")
        axis.set_xticks(x, labels, rotation=30, ha="right")
        axis.set_ylim(0, 1)
        axis.set_ylabel("observed >=2x rate")
        axis.set_title("Frozen shortlist: retrospective hit rates")
        axis.legend()
        fig.tight_layout()
        fig.savefig(plot_dir / "shortlist_comparison.png", dpi=160)
        plt.close(fig)

    for candidate_id, result in backtests.items():
        profits = result["profits"]
        equity = np.cumsum(profits)
        fig, axis = plt.subplots(figsize=(10, 4.5))
        axis.plot(np.arange(len(equity)), equity, color="#7c3aed", linewidth=1)
        axis.axhline(0, color="#6b7280", linewidth=0.8)
        axis.set_xlabel("overlap-verified target position")
        axis.set_ylabel("ideal net units")
        axis.set_title(f"Chronological ideal-settlement replay: {candidate_id}")
        fig.tight_layout()
        fig.savefig(plot_dir / f"{candidate_id}_equity.png", dpi=160)
        plt.close(fig)


def rule_config(candidate, result, backtest, random_compare, bootstrap, plan, hashes):
    status = "INSUFFICIENT_DATA" if (
        result["validation"]["metrics"]["selected"] < plan["support_and_scoring"]["minimum_validation_selected"]
        or result["retrospective_final"]["metrics"]["selected"] < plan["support_and_scoring"]["minimum_retrospective_final_selected"]
    ) else "EDGE_UNVERIFIED"
    reason_codes = [
        "HISTORICAL_FINAL_PERIOD_CONTAMINATED",
        "PLATFORM_IDENTITY_UNAVAILABLE_ORDER_ONLY",
        "PROSPECTIVE_CONFIRMATION_REQUIRED",
        "NO_POLICY_AUTO_ENABLE",
    ]
    if result["discovery"]["holm_p"] >= plan["false_discovery_control"]["family_wise_alpha"]:
        reason_codes.append("DISCOVERY_MULTIPLE_TEST_GATE_NOT_MET")
    if result["discovery"]["metrics"]["rate"] is None or result["discovery"]["metrics"]["rate"] <= 0.5:
        reason_codes.append("DISCOVERY_NOT_ABOVE_BREAK_EVEN")
    if result["validation"]["metrics"]["rate"] is None or result["validation"]["metrics"]["rate"] <= 0.5:
        reason_codes.append("VALIDATION_NOT_ABOVE_BREAK_EVEN")
    if result["retrospective_final"]["metrics"]["rate"] is None or result["retrospective_final"]["metrics"]["rate"] <= 0.5:
        reason_codes.append("RETROSPECTIVE_FINAL_NOT_ABOVE_BREAK_EVEN")
    config = {
        "schema_version": 1,
        "policy_id": candidate["candidate_id"],
        "policy_version": "1.0.0",
        "policy_status": status,
        "enabled": False,
        "dataset_hash": hashes["raw_sha256"],
        "target_multiplier": 2.0,
        "entry_condition": {
            "family": candidate["family"],
            "description": candidate["description"],
            "definition": candidate,
            "operator_semantics": "Evaluate only from source-round history before the target. The target is the next eligible verified-order row.",
        },
        "feature_windows": candidate["requirements"],
        "target_round_offset": 1,
        "cooldown_rounds": int(plan["session_backtest"]["cooldown_rounds_example"]),
        "stake_mode": "FIXED_NORMALIZED_UNIT_USER_CONFIGURED",
        "stake_amount": float(plan["session_backtest"]["stake_amount_example"]),
        "stake_unit_note": "Illustrative normalized unit only; user must explicitly choose a real stake amount.",
        "session_loss_limit": float(plan["session_backtest"]["session_loss_limit_units_example"]),
        "consecutive_loss_limit": int(plan["session_backtest"]["consecutive_loss_limit_example"]),
        "profit_goal": float(plan["session_backtest"]["profit_goal_units_example"]),
        "session_reset_rule": "RESET_AFTER_100_VERIFIED_ORDER_ROUNDS_OR_ON_SEGMENT_BREAK",
        "missing_data_action": "STOP",
        "expiry_rule": "EXPIRES_AFTER_NEXT_TARGET_ROUND",
        "reason_codes": reason_codes,
        "evidence": {
            "candidate_search_count": int(result["candidate_search_count"]),
            "discovery": result["discovery"],
            "validation": result["validation"],
            "retrospective_final": result["retrospective_final"],
            "walk_forward": result["walk_forward"],
            "matched_random_entry": random_compare,
            "block_bootstrap": bootstrap,
            "backtest": {key: value for key, value in backtest.items() if key not in {"decisions", "profits"}},
            "profitability_assumptions": {
                "ideal_cashout": 2.0,
                "net_profit_on_hit_per_unit": 1.0,
                "net_profit_on_loss_per_unit": -1.0,
                "break_even_hit_rate_before_costs": 0.5,
                "execution_costs_in_historical_results": "not observed; ideal settlement only",
            },
            "known_limitations": [
                "No platform round IDs or platform order indices exist in this snapshot.",
                "The historical final period overlaps prior model candidate evaluation and is not an untouched test.",
                "Historical multiplier replay does not verify live placement or cashout execution.",
                "Candidate search is exploratory and uses a predeclared finite family with many overlapping predicates.",
                "Bankroll and loss/profit limits shown here are illustrative parameters, not individualized recommendations.",
            ],
        },
        "integration": {
            "decision_values": ["BET", "WAIT", "STOP"],
            "required_fields": [
                "policy_id", "policy_version", "source_round_id", "target_round_id",
                "decision_timestamp", "expiry_timestamp", "trigger_evidence", "reason_codes", "evidence_status",
            ],
            "eligibility_is_separate_from_operational_safety": True,
            "operational_checks": plan["policy_output"]["operational_gates_required"],
            "live_activation": "not authorized by this analysis; existing risk engine remains mandatory",
        },
    }
    return config


def make_selected_rows(candidate, mask, available, decisions, bank, model, plan):
    split_discovery = int(plan["chronological_split"]["discovery_rows"][1])
    split_validation = int(plan["chronological_split"]["validation_rows"][1])
    outcomes = bank["outcomes"]
    rows = []
    for position in range(len(model)):
        row = model.iloc[position]
        segment_pos = int(row["segment_pos"])
        source_row = model.iloc[position - 1] if segment_pos > 0 else None
        if position < split_discovery:
            split = "discovery"
        elif position < split_validation:
            split = "validation"
        else:
            split = "retrospective_final_contaminated"
        context = candidate_feature_context(candidate, bank, model, position)
        decision_info = decisions[position]
        rows.append({
            "candidate_id": candidate["candidate_id"],
            "policy_version": "1.0.0",
            "split": split,
            "source_round_id": str(source_row["round_id"]) if source_row is not None else "",
            "source_round_index": int(source_row["round_index"]) if source_row is not None else None,
            "source_observed_timestamp": str(source_row["timestamp"]) if source_row is not None else "",
            "target_round_id": str(row["round_id"]),
            "target_round_index": int(row["round_index"]),
            "target_observed_timestamp": str(row["timestamp"]),
            "source_segment_id": int(source_row["segment_id"]) if source_row is not None else None,
            "target_segment_id": int(row["segment_id"]),
            "feature_start_round_index": context["feature_start_round_index"],
            "feature_end_round_index": context["feature_end_round_index"],
            "feature_history_rounds": context["feature_history_rounds"],
            "feature_context_json": json.dumps(context["features"], sort_keys=True),
            "trigger": bool(mask[position] and available[position]),
            "decision": decision_info["decision"],
            "reason_code": decision_info["reason_code"],
            "session_id": decision_info["session_id"],
            "observed_multiplier": float(row["multiplier"]),
            "observed_hit_at_2x": int(outcomes[position]),
            "idealized_profit_units_if_bet": float(1 if outcomes[position] else -1),
            "evidence_status": "EDGE_UNVERIFIED_ORDER_ONLY_HISTORICAL",
        })
    return rows


def generate_policy_explanation(config, result):
    c = config["entry_condition"]["description"]
    discovery = result["discovery"]["metrics"]
    validation = result["validation"]["metrics"]
    final = result["retrospective_final"]["metrics"]
    content = f"""# {config['policy_id']}

**Status:** `{config['policy_status']}` · **Enabled:** no · **Target:** 2.00x

## Entry rule

{c} If it is true after a source round, the historical replay considers only the next eligible round. The rule uses no target-round information.

## Historical evidence

- Discovery: {discovery['selected']:,} selections, {discovery['wins']:,} hits, {discovery['losses']:,} losses, hit rate {format_percent(discovery['rate'])}, baseline {format_percent(discovery['baseline_rate'])}, ideal return {format_percent(discovery['ideal_net_per_unit_staked'])} per unit staked.
- Validation: {validation['selected']:,} selections, {validation['wins']:,} hits, {validation['losses']:,} losses, hit rate {format_percent(validation['rate'])}, baseline {format_percent(validation['baseline_rate'])}, ideal return {format_percent(validation['ideal_net_per_unit_staked'])} per unit staked.
- Retrospective final period: {final['selected']:,} selections, {final['wins']:,} hits, {final['losses']:,} losses, hit rate {format_percent(final['rate'])}, baseline {format_percent(final['baseline_rate'])}, ideal return {format_percent(final['ideal_net_per_unit_staked'])} per unit staked. This period is contaminated by prior model evaluation and is not confirmatory.
- At 2.00x, the idealized break-even hit rate is 50% before execution costs. Historical cashout settlement is assumed, not verified.

## Use conditions

This is a research configuration only. It is disabled and must not be auto-selected. Before any future use, a user must explicitly choose it and set stake, cooldown, session loss limit, consecutive-loss limit, and profit goal. Runtime eligibility remains separate from safety: authentication, browser health, verified continuity, target validity, balance, duplicate protection, session limits, and the existing risk engine must all pass. Missing or stale data produces `STOP`.
"""
    return content


def generate_prospective_plan(shortlist):
    ids = ", ".join(item["candidate"]["candidate_id"] for item in shortlist) or "none"
    return f"""# Prospective validation plan

The frozen exploratory shortlist is: {ids}. Historical final-period results are contaminated by prior candidate/model evaluation and do not qualify as confirmation.

## Before collection

1. The user must explicitly select one `EDGE_UNVERIFIED` policy and set stake, cooldown, session loss limit, consecutive-loss limit, profit goal, and reset schedule. No rule is selected or enabled automatically.
2. Add stable platform round identity or platform order evidence to each prospective record. The historical `COLLECTOR_OBSERVATION_HASH` plus `LOCAL_SEQUENCE` metadata supports relative order only.
3. Freeze the selected policy JSON, source code version, dataset boundary, target definition, 2.00x cashout, decision expiry, and operational risk configuration. Record their hashes before observing any prospective outcome.
4. Keep the existing risk engine and all operational checks active. A failed login, browser/page health, continuity, target identity, balance, session limit, duplicate check, or risk check emits `STOP` and no bet.

## Prospective event ledger

Before each target round, persist exactly one decision with `policy_id`, `policy_version`, source and target round IDs, actual UTC decision timestamp, expiry after the next target only, trigger evidence, reason codes, and evidence status. Store the target result only after settlement. Never infer a missing round or join a feature window across a continuity gap.

## Decision record contract

The following is a ledger shape, not a betting instruction. The selected policy may emit `BET` only after the user explicitly selects it, its trigger is true for the next eligible target, and every independent operational safety gate passes. A false trigger, cooldown, or insufficient warm-up emits `WAIT`. Any stale/missing data, failed continuity, invalid target, authentication/browser/balance/session/risk/duplicate gate emits `STOP`; safety can never be overridden by policy eligibility.

```json
{{
  "policy_id": "<explicitly selected policy ID>",
  "policy_version": "<frozen version>",
  "source_round_id": "<verified source round ID>",
  "target_round_id": "<next eligible verified round ID>",
  "decision": "BET | WAIT | STOP",
  "decision_timestamp_utc": "<actual ISO-8601 UTC decision time>",
  "expires_after_target_round_id": "<same target round ID>",
  "trigger_evidence": {{
    "condition_id": "<candidate ID>",
    "satisfied": true,
    "feature_values": {{}},
    "feature_end_round_id": "<source round ID>"
  }},
  "reason_codes": ["TRIGGER_MET", "ALL_OPERATIONAL_GATES_PASSED"],
  "evidence_status": "EDGE_UNVERIFIED",
  "operational_safety": {{
    "authentication": "PASS",
    "browser_health": "PASS",
    "continuity": "PASS",
    "target_validity": "PASS",
    "balance": "PASS",
    "session_limits": "PASS",
    "duplicate_protection": "PASS",
    "risk_engine": "PASS"
  }}
}}
```

Use the policy evidence status (`HISTORICAL_EDGE_SUPPORTED`, `EDGE_UNVERIFIED`, `INSUFFICIENT_DATA`, or `REJECTED`) as `evidence_status`; this project produced only disabled `EDGE_UNVERIFIED` shortlist configs. Historical collector observation hashes are not substitutes for the stable platform round IDs required for prospective records.

## Sample and stopping rule

Use the next **2,000 eligible prospective targets** after the frozen policy selection. Make one confirmatory analysis at 2,000; do not peek or retune during collection. If fewer than 200 policy entries occur, or if any platform identity/continuity requirement fails, report `INSUFFICIENT_DATA` and continue collecting only under a separately frozen extension plan. Use Wilson 95% hit-rate intervals and a 20-round moving-block bootstrap for net return. Compare against 50% break-even and the contemporaneous unconditional baseline. A confidence interval crossing zero return is inconclusive; positive historical outcomes do not change stakes or bypass risk limits.

## Decision contract

- `BET`: the user-selected rule triggers for the next eligible target and every operational safety gate passes.
- `WAIT`: no trigger, insufficient warm-up history, or configured cooldown remains.
- `STOP`: any continuity, authentication, browser, balance, target validity, duplicate, session-limit, risk, or expiry gate fails.

Policy eligibility never overrides operational safety. Historical 2.00x settlement assumptions are not evidence of live placement or cashout execution.
"""


def write_analysis_report(plan, descriptive, null_checks, shortlist_results, backtests, selected_count, final_record):
    interval = descriptive["target_wilson_95"]
    lines = [
        "# Historical 2.00x analysis",
        "",
        "This is an exploratory retrospective analysis. It does not establish a profitable or live-verified strategy.",
        "",
        "## Descriptive history",
        "",
        f"- Retained archive records: {descriptive['clean_rows']:,}; >=2.00x: {descriptive['target_hits']:,}/{descriptive['clean_rows']:,} ({format_percent(descriptive['target_rate'])}, Wilson 95% {format_percent(interval[0])}–{format_percent(interval[1])}).",
        f"- Verified-order analysis records: {descriptive['verified_order_rows']:,} across {descriptive['verified_segments']:,} segments. The evidence is overlap-verified order only; no platform IDs or platform order indices are present.",
        f"- Multiplier quantiles: {json.dumps(descriptive['multiplier_quantiles'], sort_keys=True)}.",
        f"- Bands: {json.dumps(descriptive['multiplier_bands'], sort_keys=True)}.",
        f"- Below-2x streaks: {descriptive['below_2x_streaks']['count']:,}; longest {descriptive['below_2x_streaks']['longest']:,}. At-least-2x streaks: {descriptive['at_least_2x_streaks']['count']:,}; longest {descriptive['at_least_2x_streaks']['longest']:,}.",
        f"- Completed hit-to-hit waiting intervals: {descriptive['hit_waiting_times']['completed_hit_to_hit_intervals']:,}; median {format_number(descriptive['hit_waiting_times']['median_rounds'])} verified rounds, longest {descriptive['hit_waiting_times']['longest_rounds']}.",
        "- Rolling verified-order rates:",
    ]
    for window, result in descriptive["rolling_rates"].items():
        lines.append(f"  - {window} rounds: {result['valid_windows']:,} windows, mean {format_percent(result['mean_rate'])}, median {format_percent(result['median_rate'])}, 5th–95th percentile {format_percent(result['p05_rate'])}–{format_percent(result['p95_rate'])}.")
    lines.extend([
        "- Capture-time chronological blocks are included below as coverage diagnostics; they are not platform event-time analyses.",
        "",
        "| Capture-time block | Rows | >=2x rate | UTC span |",
        "|---:|---:|---:|---|",
    ])
    for block in descriptive["capture_time_chronological_blocks"]:
        lines.append(f"| {block['block']} | {block['n']:,} | {format_percent(block['rate'])} | {block['capture_start_utc']} to {block['capture_end_utc']} |")
    lines.extend([
        "",
        "Unverified legacy `platform_timestamp` diagnostics only (not trusted game-event time): **{:,} retained rows**, across **{:,} UTC dates**. They are excluded from predictive features and chronology.".format(descriptive["platform_timestamp_rows"], len(descriptive["platform_daily_counts"])),
        "",
        "| Legacy timestamp UTC date | Rows | >=2x hits | >=2x rate |",
        "|---|---:|---:|---:|",
    ])
    for item in descriptive["platform_daily_counts"]:
        lines.append(f"| {item['utc_date']} | {item['rows']:,} | {item['hits']:,} | {format_percent(item['rate'])} |")
    lines.extend([
        "",
        "| Legacy timestamp UTC hour | Rows | >=2x hits | >=2x rate |",
        "|---:|---:|---:|---:|",
    ])
    for item in descriptive["platform_hourly_rates"]:
        lines.append(f"| {item['utc_hour']:02d}:00 | {item['rows']:,} | {item['hits']:,} | {format_percent(item['rate'])} |")
    lines.extend([
        "",
        "## Search and validation",
        "",
        f"The frozen search evaluated {final_record['candidate_count']:,} predeclared candidates. Holm adjustment covered the complete family. The independent null simulation estimated its baseline from discovery only. Best-search null-tail probability: {format_percent(null_checks['iid_null_probability_max_lift_at_least_observed']) if null_checks['iid_null_probability_max_lift_at_least_observed'] is not None else 'n/a'}.",
        "",
        "The short list was frozen after discovery and validation. The latest period was opened once afterward, but is contaminated by prior model candidate evaluation and cannot be called an untouched final test.",
        "",
        "| Rule | Discovery n | Discovery rate (95% CI) | Baseline | Ideal ROI | Validation n | Validation rate (95% CI) | Baseline | Ideal ROI | Final n | Final rate (95% CI) | Baseline | Ideal ROI | Matched random | Status |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|",
    ])
    for result in shortlist_results:
        d = result["discovery"]["metrics"]
        v = result["validation"]["metrics"]
        f = result["retrospective_final"]["metrics"]
        dci = d["wilson_95"]
        vci = v["wilson_95"]
        fci = f["wilson_95"]
        random_rate = result.get("matched_random_final", {}).get("rate_mean")
        lines.append(
            f"| {result['candidate_id']} | {d['selected']:,} | {format_percent(d['rate'])} [{format_percent(dci[0])}–{format_percent(dci[1])}] | {format_percent(d['baseline_rate'])} | {format_percent(d['ideal_net_per_unit_staked'])} | {v['selected']:,} | {format_percent(v['rate'])} [{format_percent(vci[0])}–{format_percent(vci[1])}] | {format_percent(v['baseline_rate'])} | {format_percent(v['ideal_net_per_unit_staked'])} | {f['selected']:,} | {format_percent(f['rate'])} [{format_percent(fci[0])}–{format_percent(fci[1])}] | {format_percent(f['baseline_rate'])} | {format_percent(f['ideal_net_per_unit_staked'])} | {format_percent(random_rate)} | {result['policy_status']} |"
        )
    lines.extend([
        "",
        "At 2.00x, idealized break-even is 50% before costs. A positive lift versus a sub-50% historical baseline is not enough to establish positive return.",
        "",
        "## Chronological replay",
        "",
        "Historical outcomes assume a fixed one-unit stake, +1 unit for a 2.00x hit, -1 unit for a miss, one active round at a time, 100-round sessions, and stop limits of -3 units, three consecutive losses, or +3 units. These are parameterized examples, not user preferences or verified execution.",
        "",
        "| Rule | Period | Opportunities | Bets | Wins | Losses | Net units | Return/staked | Max drawdown | Longest losing sequence | Session stop frequency |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ])
    for candidate_id, backtest in backtests.items():
        for period, stats in backtest["periods"].items():
            lines.append(
                f"| {candidate_id} | {period} | {stats['trigger_opportunities']:,} | {stats['actual_bets']:,} | {stats['wins']:,} | {stats['losses']:,} | {stats['net_profit_units']:.2f} | {format_percent(stats['return_per_unit_staked'])} | {backtest['maximum_drawdown_units']:.2f} | {backtest['longest_losing_sequence']} | {format_percent(backtest['session_stop_frequency'])} |"
            )
    lines.extend([
        "",
        "The `NO_BET` reference has zero exposure and zero net loss/profit; return per unit staked is undefined with no stake. Rule baselines are unconditional rates on each eligible period, with matched-coverage random-entry rates shown above.",
        "",
        "Execution sensitivity scenarios are in `backtest_results.json`; they are hypothetical and do not estimate platform failure rates.",
        "",
        "## Conclusion",
        "",
        "**NO_VALIDATED_EDGE.** No rule receives `HISTORICAL_EDGE_SUPPORTED`: the archive has order-only identities and the retrospective final period overlaps prior model search. The generated rules are disabled and labeled `EDGE_UNVERIFIED` or `INSUFFICIENT_DATA`; they are options for explicit user selection, not automatic recommendations.",
        "",
        f"**Mu Kinyarwanda:** Ibi bisubizo bisobanura amateka gusa; ntibyemeza ko amateka ateganya neza uruziga rukurikira cyangwa ko gutega byunguka. Ku ntego ya 2.00x, nibura 50% by'intsinzi birakenewe mbere y'ibiciro n'amakosa yo gushyira igitego. Amakuru afite urutonde rwemejwe binyuze mu kugereranya snapshots gusa, kandi igihe cya nyuma cyari cyararebwe mu isuzuma ry'icyitegererezo. Bityo nta `HISTORICAL_EDGE_SUPPORTED` yabonetse. Amategeko yakozwe aracyafunze; hitamo rimwe gusa nyuma yo gusuzuma igihombo n'imbibi zaryo.",
        "",
        "## Reproduction",
        "",
        "Run from the Winner Predict repository root. To preserve this completed one-shot evaluation, reproduce into a fresh sibling output folder:",
        "",
        "```bash",
        "mkdir -p analysis_2x/research_v1_repro",
        "cp analysis_2x/research_v1/research_plan.json analysis_2x/research_v1/run_analysis.py analysis_2x/research_v1/verify.py analysis_2x/research_v1/requirements.txt analysis_2x/research_v1_repro/",
        "python3 analysis_2x/research_v1_repro/run_analysis.py audit",
        "python3 analysis_2x/research_v1_repro/run_analysis.py search",
        "python3 analysis_2x/research_v1_repro/run_analysis.py search",
        "python3 analysis_2x/research_v1_repro/run_analysis.py finalize",
        "python3 analysis_2x/research_v1_repro/verify.py",
        "```",
    ])
    ANALYSIS_REPORT_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")


def finalize_analysis():
    plan, raw, clean_rows, model, segments, quality, hashes = load_data()
    if not SHORTLIST_PATH.exists() or not CANDIDATE_PATH.exists() or not NULL_PATH.exists():
        raise RuntimeError("Run the candidate search and freeze the shortlist before final evaluation")
    with open(SHORTLIST_PATH, encoding="utf-8") as handle:
        frozen = json.load(handle)
    if frozen["plan_sha256"] != hashes["plan_sha256"]:
        raise RuntimeError("Shortlist was frozen against a different research plan")
    if frozen["raw_dataset_sha256"] != hashes["raw_sha256"] or frozen["clean_dataset_sha256"] != hashes["clean_sha256"]:
        raise RuntimeError("Shortlist input hashes differ from current data")
    shortlist = frozen["shortlist"]
    if not shortlist:
        raise RuntimeError("No candidate met the predeclared discovery/validation support gates")
    frozen_ids = [item["candidate"]["candidate_id"] for item in shortlist]
    if canonical_hash([item["candidate"] for item in shortlist]) != frozen["selected_rule_set_sha256"]:
        raise RuntimeError("Frozen shortlist fingerprint does not match")

    final_record_path = HERE / "final_test_record.json"
    if final_record_path.exists():
        with open(final_record_path, encoding="utf-8") as handle:
            final_record = json.load(handle)
        if final_record.get("selected_rule_set_sha256") != frozen["selected_rule_set_sha256"]:
            raise RuntimeError("A different shortlist has already been evaluated on the retrospective final period")
        results_by_id = final_record["results"]
        random_by_id = final_record["random_entry"]
        bootstrap_by_id = final_record["bootstrap"]
    else:
        bank = build_feature_bank(model, segments, len(model))
        outcomes = bank["outcomes"]
        candidates = [item["candidate"] for item in shortlist]
        masks, valid = build_candidate_matrices(candidates, bank)
        discovery_end = int(plan["chronological_split"]["discovery_rows"][1])
        validation_end = int(plan["chronological_split"]["validation_rows"][1])
        periods = {
            "discovery": np.arange(len(model)) < discovery_end,
            "validation": (np.arange(len(model)) >= discovery_end) & (np.arange(len(model)) < validation_end),
            "retrospective_final": np.arange(len(model)) >= validation_end,
        }
        reps = int(plan["support_and_scoring"]["block_bootstrap"]["replicates"])
        block_length = int(plan["support_and_scoring"]["block_bootstrap"]["block_length_rounds"])
        random_reps = int(plan["support_and_scoring"]["random_entry_replicates"])
        results_by_id = {}
        random_by_id = {}
        bootstrap_by_id = {}
        for index, item in enumerate(shortlist):
            candidate = item["candidate"]
            mask = masks[index]
            available = valid[index]
            period_summaries = {}
            random_summaries = {}
            bootstrap_summaries = {}
            for period_name, period in periods.items():
                stats = summarize_candidate(candidate, mask, available, outcomes, period)
                period_summaries[period_name] = {
                    "metrics": stats,
                    "walk_forward_placeholder": None,
                }
                random_summaries[period_name] = random_entry_comparison(
                    mask, available, outcomes, period, segments, random_reps,
                    int(plan["support_and_scoring"]["block_bootstrap"]["seed"]) + 100 + index * 7 + len(period_name),
                )
                bootstrap_summaries[period_name] = block_bootstrap_interval(
                    mask, available, outcomes, period, segments, reps, block_length,
                    int(plan["support_and_scoring"]["block_bootstrap"]["seed"]) + 200 + index * 11 + len(period_name),
                )
            candidate_csv = pd.read_csv(CANDIDATE_PATH).set_index("candidate_id")
            row = candidate_csv.loc[candidate["candidate_id"]]
            folds = json.loads(row["walk_forward_folds_json"])
            results_by_id[candidate["candidate_id"]] = {
                "candidate_id": candidate["candidate_id"],
                "candidate_search_count": int(len(candidate_csv)),
                "candidate": candidate,
                "discovery": {
                    "metrics": {
                        "eligible": int(row["discovery_eligible"]),
                        "selected": int(row["discovery_selected"]),
                        "wins": int(row["discovery_wins"]),
                        "losses": int(row["discovery_losses"]),
                        "coverage": row["discovery_coverage"],
                        "rate": row["discovery_rate"],
                        "baseline_rate": row["discovery_baseline"],
                        "lift_vs_baseline": row["discovery_lift"],
                        "above_break_even": row["discovery_above_break_even"],
                        "ideal_net_per_unit_staked": row["discovery_ideal_roi"],
                        "wilson_95": json.loads(row["discovery_wilson95"]),
                    },
                    "wilson_95": json.loads(row["discovery_wilson95"]),
                    "blocks": json.loads(row["discovery_blocks_json"]),
                    "holm_p": float(row["discovery_holm_p"]),
                },
                "validation": {
                    "metrics": {
                        "eligible": int(row["validation_eligible"]),
                        "selected": int(row["validation_selected"]),
                        "wins": int(row["validation_wins"]),
                        "losses": int(row["validation_losses"]),
                        "coverage": row["validation_coverage"],
                        "rate": row["validation_rate"],
                        "baseline_rate": row["validation_baseline"],
                        "lift_vs_baseline": row["validation_lift"],
                        "above_break_even": row["validation_above_break_even"],
                        "ideal_net_per_unit_staked": row["validation_ideal_roi"],
                        "wilson_95": json.loads(row["validation_wilson95"]),
                    },
                    "wilson_95": json.loads(row["validation_wilson95"]),
                    "blocks": json.loads(row["validation_blocks_json"]),
                },
                "retrospective_final": period_summaries["retrospective_final"],
                "walk_forward": folds,
            }
            random_by_id[candidate["candidate_id"]] = random_summaries
            bootstrap_by_id[candidate["candidate_id"]] = bootstrap_summaries
        final_record = {
            "plan_id": plan["plan_id"],
            "plan_sha256": hashes["plan_sha256"],
            "raw_dataset_sha256": hashes["raw_sha256"],
            "selected_rule_set_sha256": frozen["selected_rule_set_sha256"],
            "candidate_search_csv_sha256": sha256_file(CANDIDATE_PATH),
            "search_reproducibility_sha256": sha256_file(HERE / "search_reproducibility.json") if (HERE / "search_reproducibility.json").exists() else None,
            "final_period_status": plan["chronological_split"]["final_period_status"],
            "evaluation_count": 1,
            "final_test_opened": True,
            "candidate_count": len(candidate_csv),
            "evaluated_candidate_ids": frozen_ids,
            "results": results_by_id,
            "random_entry": random_by_id,
            "bootstrap": bootstrap_by_id,
            "note": "Only the frozen shortlist was evaluated. This retrospective final period is contaminated by prior model search and is not confirmatory.",
        }
        write_json(final_record_path, final_record)

    bank = build_feature_bank(model, segments, len(model))
    outcomes = bank["outcomes"]
    candidate_by_id = {item["candidate"]["candidate_id"]: item["candidate"] for item in shortlist}
    candidate_list = [candidate_by_id[candidate_id] for candidate_id in frozen_ids]
    masks, valid = build_candidate_matrices(candidate_list, bank)
    backtests = {}
    selected_rows = []
    rule_output = HERE / "rules"
    rule_output.mkdir(parents=True, exist_ok=True)
    for index, candidate_id in enumerate(frozen_ids):
        candidate = candidate_list[index]
        result = results_by_id[candidate_id]
        result["discovery"]["metrics"] = {key: to_builtin(value) for key, value in result["discovery"]["metrics"].items()}
        result["validation"]["metrics"] = {key: to_builtin(value) for key, value in result["validation"]["metrics"].items()}
        result["retrospective_final"]["metrics"]["wilson_95"] = result["retrospective_final"]["metrics"]["wilson_95"]
        bootstrap = bootstrap_by_id[candidate_id]
        random_compare = random_by_id[candidate_id]["retrospective_final"]
        backtest = backtest_rule(candidate, masks[index], valid[index], model, plan)
        backtests[candidate_id] = backtest
        config = rule_config(candidate, result, backtest, random_compare, bootstrap, plan, hashes)
        write_json(rule_output / f"{candidate_id}.json", config)
        (rule_output / f"{candidate_id}.md").write_text(generate_policy_explanation(config, result), encoding="utf-8")
        selected_rows.extend(make_selected_rows(candidate, masks[index], valid[index], backtest["decisions"], bank, model, plan))

    selected_frame = pd.DataFrame(selected_rows)
    selected_frame.to_csv(SELECTED_PATH, index=False, float_format="%.12g")
    backtest_report = {}
    final_start = int(plan["chronological_split"]["validation_rows"][1])
    for candidate_id, backtest in backtests.items():
        sensitivity = execution_sensitivity(backtest, model, final_start, 1000, int(plan["support_and_scoring"]["block_bootstrap"]["seed"]) + 1000)
        backtest_report[candidate_id] = {
            key: value for key, value in backtest.items() if key not in {"decisions", "profits"}
        }
        backtest_report[candidate_id]["execution_sensitivity_retrospective_final"] = sensitivity
    discovery_end = int(plan["chronological_split"]["discovery_rows"][1])
    validation_end = int(plan["chronological_split"]["validation_rows"][1])
    model_positions = np.arange(len(model))
    no_bet_periods = {
        "discovery": model_positions < discovery_end,
        "validation": (model_positions >= discovery_end) & (model_positions < validation_end),
        "retrospective_final": model_positions >= validation_end,
    }
    backtest_report["NO_BET"] = {
        "candidate_id": "NO_BET",
        "actual_bets": 0,
        "wins": 0,
        "losses": 0,
        "net_profit_units": 0.0,
        "return_per_unit_staked": None,
        "maximum_drawdown_units": 0.0,
        "longest_losing_sequence": 0,
        "session_stop_frequency": 0.0,
        "stake_per_bet_units": 0.0,
        "ideal_settlement_assumption": False,
        "periods": {
            name: {
                "eligible_targets": int(period.sum()),
                "trigger_opportunities": 0,
                "actual_bets": 0,
                "wins": 0,
                "losses": 0,
                "net_profit_units": 0.0,
                "return_per_unit_staked": None,
            }
            for name, period in no_bet_periods.items()
        },
    }
    write_json(BACKTEST_PATH, backtest_report)

    candidate_frame = pd.read_csv(CANDIDATE_PATH)
    for candidate_id in frozen_ids:
        result = results_by_id[candidate_id]
        metrics = result["retrospective_final"]["metrics"]
        bootstrap = bootstrap_by_id[candidate_id]["retrospective_final"]
        random_compare = random_by_id[candidate_id]["retrospective_final"]
        row_mask = candidate_frame["candidate_id"] == candidate_id
        for column, value in {
            "retrospective_final_eligible": metrics["eligible"],
            "retrospective_final_selected": metrics["selected"],
            "retrospective_final_wins": metrics["wins"],
            "retrospective_final_losses": metrics["losses"],
            "retrospective_final_rate": metrics["rate"],
            "retrospective_final_wilson95": json.dumps(metrics["wilson_95"]),
            "retrospective_final_baseline": metrics["baseline_rate"],
            "retrospective_final_lift": metrics["lift_vs_baseline"],
            "retrospective_final_above_break_even": metrics["above_break_even"],
            "retrospective_final_ideal_roi": metrics["ideal_net_per_unit_staked"],
            "retrospective_final_blocks_json": json.dumps(metrics["blocks"], sort_keys=True),
            "retrospective_final_block_bootstrap_json": json.dumps(bootstrap, sort_keys=True),
            "retrospective_final_matched_random_json": json.dumps(random_compare, sort_keys=True),
            "status": "INSUFFICIENT_DATA" if metrics["selected"] < plan["support_and_scoring"]["minimum_retrospective_final_selected"] else "EDGE_UNVERIFIED",
        }.items():
            candidate_frame.loc[row_mask, column] = value
    candidate_frame.to_csv(CANDIDATE_PATH, index=False, float_format="%.12g")

    descriptive = descriptive_statistics(clean_rows, model, segments)
    descriptive.pop("rolling_points")
    write_json(HERE / "descriptive_statistics.json", descriptive)
    generate_plots(clean_rows, model, segments, descriptive_statistics(clean_rows, model, segments), [
        {
            "candidate_id": candidate_id,
            "discovery_rate": results_by_id[candidate_id]["discovery"]["metrics"]["rate"],
            "validation_rate": results_by_id[candidate_id]["validation"]["metrics"]["rate"],
            "retrospective_final_rate": results_by_id[candidate_id]["retrospective_final"]["metrics"]["rate"],
        } for candidate_id in frozen_ids
    ], backtests)
    with open(NULL_PATH, encoding="utf-8") as handle:
        null_checks = json.load(handle)
    write_analysis_report(plan, descriptive, null_checks, [
        {
            "candidate_id": candidate_id,
            "policy_status": json.load(open(rule_output / f"{candidate_id}.json", encoding="utf-8"))["policy_status"],
            "matched_random_final": random_by_id[candidate_id]["retrospective_final"],
            **results_by_id[candidate_id],
        } for candidate_id in frozen_ids
    ], backtest_report, len(selected_frame), {"candidate_count": final_record["candidate_count"]})
    (HERE / "prospective_validation_plan.md").write_text(generate_prospective_plan(shortlist), encoding="utf-8")
    write_json(MANIFEST_PATH, {
        "plan_id": plan["plan_id"],
        "plan_sha256": hashes["plan_sha256"],
        "snapshot_sha256": hashes["raw_sha256"],
        "clean_sha256": hashes["clean_sha256"],
        "shortlist_sha256": sha256_file(SHORTLIST_PATH),
        "final_test_record_sha256": sha256_file(final_record_path),
        "outputs": {
            path.name: sha256_file(path)
            for path in [QUALITY_REPORT_PATH, ANALYSIS_REPORT_PATH, CANDIDATE_PATH, SELECTED_PATH, BACKTEST_PATH, HERE / "prospective_validation_plan.md"]
        },
        "python": sys.version,
        "numpy": np.__version__,
        "pandas": pd.__version__,
        "matplotlib": matplotlib.__version__,
    })
    print(json.dumps({
        "final_test_evaluation_count": final_record["evaluation_count"],
        "shortlisted_rules": frozen_ids,
        "selected_round_rows": len(selected_frame),
        "candidate_csv": str(CANDIDATE_PATH),
        "analysis_report": str(ANALYSIS_REPORT_PATH),
        "status": "NO_VALIDATED_EDGE",
    }, indent=2))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("phase", choices=["audit", "search", "finalize"])
    args = parser.parse_args()
    if args.phase == "audit":
        audit_report()
    elif args.phase == "search":
        search_candidates()
    else:
        finalize_analysis()


if __name__ == "__main__":
    main()
