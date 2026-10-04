"""Nested, multi-evidence selective opportunity research.

V2 freezes the V1 test interval. It may use only round indices before that
interval for policy development; rows after the original V1 source snapshot
are reserved for prospective RESEARCH_SHADOW observations.
"""
from __future__ import annotations

import math
from statistics import NormalDist
import time
from typing import Any

import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression
from sklearn.neighbors import NearestNeighbors
from sklearn.preprocessing import StandardScaler

from app.ml.opportunity import (
    CalibratedBinaryModel, TARGET, WINDOWS, _factories, _interval_lift, build_opportunity_frame,
    validated_rounds, wilson,
)

# Captured from the PostgreSQL ordering used by the V1 research run. Do not
# move either boundary when new rounds arrive.
V1_DIAGNOSTIC_START_INDEX = 15310
V1_SOURCE_END_INDEX = 17739
V2_VERSION = "selective-opportunity-v2-nested"
TARGET_COVERAGES = (0.001, 0.0025, 0.005, 0.01, 0.02)
BASE_MODEL_NAMES = ("logistic_regression", "extra_trees", "random_forest",
                    "gradient_boosting", "hist_gradient_boosting")


def _feature_stability(X: np.ndarray, y: np.ndarray, names: list[str], *, blocks: int = 4) -> dict[str, Any]:
    """Select features only when association direction repeats in past blocks."""
    rows = []
    if len(y) < 120 or len(names) != X.shape[1]:
        return {"retained": [], "rows": [], "reason": "insufficient_training_blocks"}
    boundaries = np.linspace(0, len(y), blocks + 1, dtype=int)
    for j, name in enumerate(names):
        effects = []
        for left, right in zip(boundaries[:-1], boundaries[1:]):
            if right - left < 20 or np.std(X[left:right, j]) < 1e-10:
                continue
            corr = np.corrcoef(X[left:right, j], y[left:right])[0, 1]
            effects.append(float(corr) if np.isfinite(corr) else 0.0)
        nonzero = [value for value in effects if abs(value) >= 0.01]
        direction = 1 if sum(value > 0 for value in nonzero) >= sum(value < 0 for value in nonzero) else -1
        consistent = sum((value > 0) == (direction > 0) for value in nonzero)
        fraction = consistent / len(nonzero) if nonzero else 0.0
        pooled = float(np.corrcoef(X[:, j], y)[0, 1]) if np.std(X[:, j]) > 1e-10 else 0.0
        if not np.isfinite(pooled):
            pooled = 0.0
        zcrit = NormalDist().inv_cdf(1 - 0.05 / (2 * max(len(names), 1)))
        if len(y) > 3 and abs(pooled) < 1:
            fisher = math.atanh(pooled)
            radius = zcrit / math.sqrt(len(y) - 3)
            pooled_ci = (math.tanh(fisher - radius), math.tanh(fisher + radius))
        else:
            pooled_ci = (0.0, 0.0)
        significant = pooled_ci[0] > 0 or pooled_ci[1] < 0
        keep = len(nonzero) >= 3 and fraction >= 0.75 and significant
        rows.append({"feature": name, "block_effects": effects, "direction": direction,
                     "direction_consistency": fraction, "pooled_correlation": pooled,
                     "pooled_bonferroni_ci95": list(pooled_ci),
                     "retained": bool(keep),
                     "reason": "stable_training_association" if keep else "unstable_or_weak_training_association"})
    retained = [row["feature"] for row in rows if row["retained"]]
    return {"retained": retained, "rows": rows, "blocks": blocks,
            "criterion": "same direction in >=75% of >=3 chronological training blocks and Bonferroni-adjusted pooled Fisher-z 95% interval excludes zero"}


def _dynamic_baselines(X: np.ndarray, names: list[str]) -> dict[str, np.ndarray]:
    positions = {name: index for index, name in enumerate(names)}
    result = {}
    for window in (100, 250, 500, 1000):
        name = f"rate_ge_2_1_{window}"
        result[f"last_{window}"] = X[:, positions[name]] if name in positions else np.full(len(X), np.nan)
    name = "past_target_rate"
    result["long_term"] = X[:, positions[name]] if name in positions else np.full(len(X), np.nan)
    # Primary baseline is the 1000-round estimate. It is a trailing feature,
    # never a full-dataset or future-informed rate.
    result["relevant"] = result["last_1000"]
    return result


def _analog_evidence(X_train: np.ndarray, y_train: np.ndarray, X_query: np.ndarray,
                     feature_names: list[str], *, baseline_override=None,
                     k: int = 100) -> dict[str, np.ndarray]:
    """Nearest historical states; labels are next outcomes from prior rows."""
    wanted = [name for name in feature_names if any(token in name for token in (
        "rate_ge_2_1_", "mean_", "std_", "entropy_", "recent_long_", "pattern_", "transition_rate"))]
    if not wanted or len(X_train) < 100:
        n = len(X_query)
        return {name: np.zeros(n) for name in ("count", "rate", "lift", "ci_lower", "ci_upper", "stable_blocks")}
    columns = [feature_names.index(name) for name in wanted]
    scaler = StandardScaler().fit(X_train[:, columns])
    past = scaler.transform(X_train[:, columns])
    query = scaler.transform(X_query[:, columns])
    neighbors = min(k, len(X_train))
    search = NearestNeighbors(n_neighbors=neighbors, algorithm="brute", metric="euclidean", n_jobs=1).fit(past)
    indices = search.kneighbors(query, return_distance=False)
    baselines = (np.asarray(baseline_override, dtype=float) if baseline_override is not None
                 else _dynamic_baselines(X_query, feature_names)["relevant"])
    output = {name: [] for name in ("count", "rate", "lift", "ci_lower", "ci_upper", "stable_blocks")}
    for row, ids in enumerate(indices):
        outcomes = y_train[ids]
        rate = float(outcomes.mean()) if len(outcomes) else 0.0
        ci = wilson(int(outcomes.sum()), len(outcomes))
        # Check the analog hit rate across four eras of the training history.
        eras = np.minimum(3, (ids * 4 / max(len(y_train), 1)).astype(int))
        era_rates = [float(outcomes[eras == era].mean()) for era in range(4) if (eras == era).sum() >= 10]
        stable = sum(value > baselines[row] for value in era_rates) / len(era_rates) if era_rates else 0.0
        values = (len(ids), rate, rate - float(baselines[row]), ci["lower"] or 0.0,
                  ci["upper"] or 1.0, stable)
        for name, value in zip(output, values):
            output[name].append(value)
    return {name: np.asarray(values, dtype=float) for name, values in output.items()}


def _evidence_matrix(X_train, y_train, X_part, p_models, evidence_feature_names, stability_fraction,
                     *, X_query_stable=None, analog_feature_names=None):
    baseline = _dynamic_baselines(X_part, evidence_feature_names)
    relevant = baseline["relevant"]
    p = np.column_stack(list(p_models.values()))
    mean_p = np.mean(p, axis=1)
    agreement = np.mean(p >= relevant[:, None], axis=1)
    pattern_names = [name for name in ("pattern_sequence_3_rate", "pattern_streak_rate", "transition_rate")
                     if name in evidence_feature_names]
    pattern_lift = (np.max(np.column_stack([X_part[:, evidence_feature_names.index(name)] for name in pattern_names]), axis=1)
                    - relevant) if pattern_names else np.zeros(len(X_part))
    regime_names = [name for name in ("recent_long_rate_shift_2_1", "recent_long_log_mean_shift",
                                       "recent_long_volatility_shift") if name in evidence_feature_names]
    regime_shift = (np.mean(np.abs(np.column_stack([X_part[:, evidence_feature_names.index(name)] for name in regime_names])), axis=1)
                    if regime_names else np.zeros(len(X_part)))
    analog = _analog_evidence(X_train, y_train, X_query_stable if X_query_stable is not None else X_part,
                              analog_feature_names or evidence_feature_names, baseline_override=relevant)
    uncertainty = np.std(p, axis=1) + np.maximum(0.0, analog["ci_upper"] - analog["ci_lower"])
    # Meta-weights are learned by a chronological Logistic Regression on the
    # dedicated score-fit partition, never hand-tuned on outer outcomes.
    components = np.column_stack([mean_p - relevant, agreement, pattern_lift,
                                  regime_shift, analog["lift"],
                                  np.full(len(X_part), stability_fraction), -uncertainty])
    component_names = ["probability_lift", "model_agreement", "pattern_lift", "regime_shift",
                       "historical_analog_lift", "feature_stability", "uncertainty_penalty"]
    metadata = {"baseline": baseline, "analogs": analog, "model_probabilities": p,
                "components": components, "component_names": component_names}
    return components, metadata


def _eligible_mask(components, evidence, uncertainty_limit, distribution_warning_limit):
    baseline = evidence["baseline"]["relevant"]
    model_p = evidence["model_probabilities"]
    analog = evidence["analogs"]
    pattern_lift = components[:, 2]
    uncertainty = -components[:, 6]
    regime_shift = components[:, 3]
    agreement = np.mean(model_p >= baseline[:, None], axis=1)
    return ((np.mean(model_p, axis=1) > baseline)
            & (agreement >= .60)
            & (pattern_lift > 0)
            & (analog["count"] >= 100)
            & (analog["lift"] > 0)
            & (analog["ci_lower"] > baseline)
            & (analog["stable_blocks"] >= .75)
            & (uncertainty <= uncertainty_limit)
            & (regime_shift <= distribution_warning_limit))


def _fit_score_policy(score_fit, y_score, threshold_rows, y_threshold, baseline_threshold,
                      threshold_components, threshold_evidence, uncertainty_limit,
                      distribution_warning_limit):
    model = LogisticRegression(C=0.1, max_iter=800, class_weight=None, random_state=42)
    if not np.isfinite(score_fit).all() or not np.isfinite(threshold_rows).all():
        return None, {"status": "NO_SIGNAL", "reason": "nonfinite_multi_evidence_components", "curves": []}
    if len(np.unique(y_score)) < 2:
        return None, {"status": "NO_SIGNAL", "reason": "score_fit_has_one_target_class", "curves": []}
    model.fit(score_fit, y_score)
    probability = model.predict_proba(threshold_rows)[:, 1]
    curves = []
    for requested in TARGET_COVERAGES:
        threshold = float(np.quantile(probability, 1 - requested, method="higher"))
        selected = ((probability >= threshold)
                    & _eligible_mask(threshold_components, threshold_evidence,
                                     uncertainty_limit, distribution_warning_limit))
        metrics = _interval_lift(y_threshold, selected, baseline_threshold)
        metrics.update({"requested_coverage": requested, "score_threshold": threshold,
                        "raw_score_signals": int(np.sum(probability >= threshold)),
                        "evidence_gate_passes": int(np.sum(_eligible_mask(threshold_components,
                            threshold_evidence, uncertainty_limit, distribution_warning_limit))),
                        "selection_qualified": bool(metrics["signals"] >= 10
                            and metrics["precision_ci95"]["lower"] is not None
                            and metrics["precision_ci95"]["lower"] > metrics["baseline_ci95"]["upper"])})
        curves.append(metrics)
    qualified = [row for row in curves if row["selection_qualified"]]
    if not qualified:
        return model, {"status": "NO_SIGNAL", "reason": "no_nested_validation_policy_passed_support_and_lift_gate",
                       "curves": curves, "selected_policy": None}
    chosen = max(qualified, key=lambda row: (row["precision_ci95"]["lower"], -row["requested_coverage"]))
    return model, {"status": "POLICY_SELECTED", "curves": curves, "selected_policy": chosen}


def _collapse_diagnosis(prior_report: dict[str, Any]) -> dict[str, Any]:
    validation = prior_report.get("validation") or {}
    test = prior_report.get("untouched_test") or {}
    validation_n = int(validation.get("signals") or 0)
    test_n = int(test.get("signals") or 0)
    base_v = validation.get("baseline_rate")
    base_t = test.get("base_rate_observed")
    return {
        "validation_signal_count": validation_n,
        "validation_true_false": [validation.get("true_signals"), validation.get("false_signals")],
        "untouched_signal_count": test_n,
        "untouched_true_false": [test.get("true_signals"), test.get("false_signals")],
        "threshold_overfit": {
            "assessment": "LIKELY_CONTRIBUTOR",
            "evidence": "V1 selected both the winning model and one threshold from the same outer validation block after searching five quantiles per available model.",
            "searches": len(prior_report.get("algorithms_tested") or []) * 5,
        },
        "small_signal_sample": {
            "assessment": "CONFIRMED",
            "evidence": "17 validation and 16 untouched-test signals produce wide overlapping confidence intervals; 8/16 test hits are compatible with baseline.",
            "validation_ci95": validation.get("precision_ci95"),
            "untouched_ci95": test.get("precision_ci95"),
        },
        "baseline_change": {"validation": base_v, "untouched": base_t,
                            "absolute_change": (base_t - base_v) if base_v is not None and base_t is not None else None,
                            "assessment": "SMALL_BASE_RATE_SHIFT"},
        "calibration_drift": {
            "validation_ece": (validation.get("calibration_diagnostics") or {}).get("expected_calibration_error"),
            "untouched_ece": (test.get("calibration") or {}).get("expected_calibration_error"),
            "assessment": "MEASURED_DIFFERENCE_REQUIRES_CAUTION",
        },
        "feature_instability": "NOT_MEASURED_BY_V1; V2 reports training-only feature direction stability per chronological fold.",
        "regime_or_distribution_change": "NOT_ESTABLISHED_BY_V1; global baseline changed little, but V1 did not preserve per-round predictions needed for a decisive regime attribution.",
        "pattern_instability": "NOT_MEASURED_BY_V1; V2 only uses past-conditioned pattern evidence and reports outer-fold results.",
        "data_quality": {"excluded": prior_report.get("excluded"),
                         "assessment": "No evidence in the saved V1 report attributes the collapse to duplicate/invalid data; its quality counts are retained for review."},
        "conclusion": "Selection on a small signal sample is the confirmed weakness. The same-block model/threshold search is a likely winner's-curse contributor. V1 evidence cannot isolate feature, pattern, or regime drift because per-round predictions and corresponding feature snapshots were not persisted.",
    }


def run_v2(dataset_service, prior_report: dict[str, Any]) -> dict[str, Any]:
    """Nested development on the frozen pre-V1-test history only.

    The V1 test metrics are copied verbatim as diagnostic context. They are
    never read by V2 fitting, policy selection, threshold selection, or gates.
    """
    started = time.perf_counter()
    clean, quality = validated_rounds(dataset_service.clean_rounds.copy())
    development = clean.loc[clean["round_index"] < V1_DIAGNOSTIC_START_INDEX]
    # A fresh prospective sample is explicitly later than the complete V1
    # source snapshot, and starts as unavailable until its own 100 prior rounds.
    prospective_rows = clean.loc[clean["round_index"] > V1_SOURCE_END_INDEX]
    prospective_contiguous = 0
    if len(prospective_rows):
        p_indices = prospective_rows["round_index"].to_numpy(int)
        verified = prospective_rows["continuity_verified"].astype(bool).to_numpy()
        gap_before = prospective_rows["gap_before"].astype(bool).to_numpy()
        prospective_contiguous = int(bool(verified[-1]))
        for i in range(len(prospective_rows) - 1, 0, -1):
            if (not verified[i] or not verified[i - 1] or gap_before[i]
                    or p_indices[i] != p_indices[i - 1] + 1
                    or str(prospective_rows.iloc[i].round_id) == str(prospective_rows.iloc[i - 1].round_id)):
                break
            prospective_contiguous += 1
    if development.empty:
        return {"version": V2_VERSION, "status": "INSUFFICIENT_DATA", "target": TARGET,
                "clean_rounds": quality["valid"], "reason": "frozen_pre_v1_test_development_history_unavailable",
                "old_untouched_test": {"label": "DIAGNOSTIC ONLY", "result": prior_report.get("untouched_test")}}
    frame, frame_quality = build_opportunity_frame(development, TARGET)
    if frame.empty:
        return {"version": V2_VERSION, "status": "INSUFFICIENT_DATA", "target": TARGET,
                "clean_rounds": len(development), "reason": "no_eligible_pre_v1_test_targets",
                "old_untouched_test": {"label": "DIAGNOSTIC ONLY", "result": prior_report.get("untouched_test")}}
    excluded = {"source_round_id", "source_round_index", "target_round_id", "target_round_index",
                "target_timestamp", "target_multiplier_value", "target_hit"}
    all_features = [name for name in frame.columns if name not in excluded]
    X_all = frame[all_features].to_numpy(float)
    y_all = frame["target_hit"].to_numpy(int)
    n = len(y_all)
    # Four expanding chronological outer folds. Every outer outcome is used
    # only for reporting stability, never to revise that fold's policy.
    folds_spec = ((.40, .45, .55, .65, .72), (.47, .52, .62, .72, .79),
                  (.54, .59, .69, .79, .85))
    fold_reports = []
    strategy_rows: dict[float, list[dict[str, Any]]] = {value: [] for value in TARGET_COVERAGES}
    for train_f, cal_f, score_f, threshold_f, test_f in folds_spec:
        train_end, cal_end, score_end, threshold_end, test_end = [int(n * value) for value in
                                                                   (train_f, cal_f, score_f, threshold_f, test_f)]
        y_train = y_all[:train_end]
        if train_end < 300 or min(cal_end - train_end, score_end - cal_end,
                                  threshold_end - score_end, test_end - threshold_end) < 100:
            continue
        X_train_all = X_all[:train_end]
        stability = _feature_stability(X_train_all, y_train, all_features)
        retained = stability["retained"]
        if not retained:
            # Refuse an unstable feature set; there is no fallback to all
            # features because that would hide the stability failure.
            fold_reports.append({"status": "ABSTAIN", "reason": "no_stable_training_features",
                                 "feature_stability": stability, "fold": len(fold_reports) + 1})
            continue
        positions = [all_features.index(name) for name in retained]
        X = X_all[:, positions]
        factories = _factories()
        probability_parts: dict[str, np.ndarray] = {}
        model_objects = {}
        errors = {}
        for name in BASE_MODEL_NAMES:
            factory = factories.get(name)
            if factory is None:
                errors[name] = "not_installed"
                continue
            try:
                estimator = factory()
                estimator.fit(X[:train_end], y_all[:train_end])
                raw_cal = estimator.predict_proba(X[train_end:cal_end])[:, 1]
                cal = LogisticRegression(C=1.0, max_iter=500, random_state=42)
                if len(np.unique(y_all[train_end:cal_end])) == 2:
                    cal.fit(raw_cal.reshape(-1, 1), y_all[train_end:cal_end])
                else:
                    cal = None
                fitted = CalibratedBinaryModel(estimator, cal)
                model_objects[name] = fitted
                # Components for score fit, threshold selection and outer test
                # use only models fitted on the fold's past training prefix.
                probability_parts[name] = fitted.predict_proba(X[cal_end:test_end])[:, 1]
            except Exception as exc:
                errors[name] = f"{type(exc).__name__}: {exc}"
        if len(probability_parts) < 3:
            fold_reports.append({"status": "ABSTAIN", "reason": "fewer_than_three_independent_models",
                                 "model_errors": errors, "fold": len(fold_reports) + 1})
            continue
        # One model probability slice is partitioned by the same time cuts.
        score_l, score_r = cal_end - cal_end, score_end - cal_end
        threshold_l, threshold_r = score_end - cal_end, threshold_end - cal_end
        test_l, test_r = threshold_end - cal_end, test_end - cal_end
        X_source = X[cal_end:test_end]
        X_evidence_source = X_all[cal_end:test_end]
        y_source = y_all[cal_end:test_end]
        # Neighbors are limited to training outcomes; the cut labels below
        # preserve chronological partition boundaries.
        train_X_selected, train_y = X[:train_end], y_all[:train_end]
        stable_fraction = len(retained) / max(len(all_features), 1)
        components, evidence = _evidence_matrix(train_X_selected, train_y, X_evidence_source,
                                                 probability_parts, all_features, stable_fraction,
                                                 X_query_stable=X_source, analog_feature_names=retained)
        # The model probability dictionary spans [cal_end:test_end]; split
        # each row range after every feature/evidence component is frozen.
        uncertainty_limit = float(np.quantile(-components[score_l:score_r, 6], .95))
        distribution_warning_limit = float(np.quantile(components[score_l:score_r, 3], .95))
        score_model, policy = _fit_score_policy(
            components[score_l:score_r], y_source[score_l:score_r],
            components[threshold_l:threshold_r], y_source[threshold_l:threshold_r],
            float(y_source[threshold_l:threshold_r].mean()), components[threshold_l:threshold_r],
            {key: (value[threshold_l:threshold_r] if isinstance(value, np.ndarray) and len(value) == len(X_source)
                   else {sub: val[threshold_l:threshold_r] for sub, val in value.items()}
                   if isinstance(value, dict) else value) for key, value in evidence.items()},
            uncertainty_limit, distribution_warning_limit)
        if score_model is None:
            fold_reports.append({"status": "ABSTAIN", "reason": policy["reason"],
                                 "feature_stability": stability, "fold": len(fold_reports) + 1})
            continue
        probabilities = score_model.predict_proba(components[test_l:test_r])[:, 1]
        y_test = y_source[test_l:test_r]
        test_curves = []
        chosen_validation = policy.get("selected_policy")
        for curve in policy["curves"]:
            cutoff = float(curve["score_threshold"])
            test_evidence = {key: (value[test_l:test_r] if isinstance(value, np.ndarray) and len(value) == len(X_source)
                                   else {sub: val[test_l:test_r] for sub, val in value.items()}
                                   if isinstance(value, dict) else value) for key, value in evidence.items()}
            eligible = _eligible_mask(components[test_l:test_r], test_evidence,
                                      uncertainty_limit, distribution_warning_limit)
            metrics = _interval_lift(y_test, (probabilities >= cutoff) & eligible, float(y_test.mean()))
            metrics.update({"requested_coverage": curve["requested_coverage"], "threshold": cutoff})
            test_curves.append(metrics)
            strategy_rows[curve["requested_coverage"]].append(metrics)
        selected = None
        if chosen_validation:
            candidate = next(row for row in test_curves if row["requested_coverage"] == chosen_validation["requested_coverage"])
            selected = {"validation": chosen_validation, "outer_test": candidate}
        fold_reports.append({"status": "EVALUATED", "fold": len(fold_reports) + 1,
                             "train_rounds": train_end, "calibration_rounds": cal_end - train_end,
                             "score_fit_rounds": score_end - cal_end,
                             "threshold_validation_rounds": threshold_end - score_end,
                             "outer_test_rounds": len(y_test), "stable_feature_count": len(retained),
                             "feature_stability_fraction": stable_fraction,
                             "feature_stability": stability, "model_errors": errors,
                             "models_agreeable": list(probability_parts),
                             "model_uncertainty_mean": float(np.mean(np.std(evidence["model_probabilities"][test_l:test_r], axis=1))),
                             "analog_count_median": float(np.median(evidence["analogs"]["count"][test_l:test_r])),
                             "analog_lift_mean": float(np.mean(evidence["analogs"]["lift"][test_l:test_r])),
                             "component_weights": dict(zip(evidence["component_names"],
                                 score_model.coef_[0].tolist())),
                             "threshold_validation": policy, "selected_policy_test": selected,
                             "coverage_curves_test": test_curves})
    def aggregate(rows, coverage):
        signals = sum(row["signals"] for row in rows)
        true = sum(row["true_signals"] for row in rows)
        rounds = sum(row["baseline_ci95"]["n"] for row in rows)
        baseline_success = sum(round(row["baseline_rate"] * row["baseline_ci95"]["n"]) for row in rows)
        baseline = baseline_success / rounds if rounds else None
        precision_ci = wilson(true, signals)
        baseline_ci = wilson(baseline_success, rounds)
        lift_ci = ([precision_ci["lower"] - baseline_ci["upper"],
                    precision_ci["upper"] - baseline_ci["lower"]]
                   if signals and rounds else [None, None])
        wins = sum((row["lift"] or 0) > 0 for row in rows if row["signals"] > 0)
        return {"target_coverage": coverage, "blocks": len(rows),
                           "positive_lift_blocks": wins, "rounds": rounds, "signals": signals,
                           "true_signals": true, "false_signals": signals - true,
                           "precision": true / signals if signals else None,
                           "precision_ci95": precision_ci, "baseline": baseline,
                           "baseline_ci95": baseline_ci,
                           "lift": (true / signals - baseline) if signals and baseline is not None else None,
                           "lift_ci95": lift_ci,
                           "coverage": signals / rounds if rounds else None,
                           "abstention_rate": 1 - signals / rounds if rounds else None,
                           "signals_per_100": signals / rounds * 100 if rounds else None,
                           "signals_per_500": signals / rounds * 500 if rounds else None,
                           "selection_stable": bool(len(rows) >= 3 and wins >= math.ceil(len(rows) * .75))}

    # Policy choice is made solely from the nested threshold-validation
    # partitions. Outer-test metrics are used only once for the selected
    # coverage and can veto research-shadow readiness; they never choose it.
    validation_rows: dict[float, list[dict[str, Any]]] = {value: [] for value in TARGET_COVERAGES}
    for fold in fold_reports:
        for curve in (fold.get("threshold_validation") or {}).get("curves", []):
            validation_rows[curve["requested_coverage"]].append(curve)
    validation_policies = [aggregate(rows, coverage) | {"selection_stable":
        bool(len(rows) >= 3 and sum(bool(row.get("selection_qualified")) for row in rows) >= math.ceil(len(rows) * .75))}
        for coverage, rows in validation_rows.items()]
    qualified = [row for row in validation_policies if row["signals"] >= 30
                 and row["selection_stable"] and row["precision_ci95"]["lower"] is not None
                 and row["baseline_ci95"]["upper"] is not None
                 and row["precision_ci95"]["lower"] > row["baseline_ci95"]["upper"]]
    selected_validation_policy = max(qualified,
        key=lambda row: (row["precision_ci95"]["lower"], -row["target_coverage"])) if qualified else None
    selected_coverage = selected_validation_policy["target_coverage"] if selected_validation_policy else None
    selected_outer_rows = strategy_rows[selected_coverage] if selected_coverage is not None else []
    outer_policy_result = aggregate(selected_outer_rows, selected_coverage) if selected_coverage is not None else None
    outer_pass = bool(outer_policy_result and outer_policy_result["signals"] >= 30
                      and outer_policy_result["selection_stable"]
                      and outer_policy_result["precision_ci95"]["lower"] is not None
                      and outer_policy_result["baseline_ci95"]["upper"] is not None
                      and outer_policy_result["precision_ci95"]["lower"] > outer_policy_result["baseline_ci95"]["upper"])
    strategy = ({"validation": selected_validation_policy, "outer_validation": outer_policy_result}
                if selected_validation_policy and outer_pass else None)
    outer_diagnostics = [aggregate(rows, coverage) for coverage, rows in strategy_rows.items()]
    report = {"version": V2_VERSION,
              "status": "RESEARCH_SHADOW_READY" if strategy else "NO_SIGNAL",
              "research_shadow_ready": bool(strategy),
              "target": TARGET, "development_source": "PostgreSQL validated rounds before frozen V1 test",
              "frozen_boundaries": {"v1_diagnostic_start_round_index": V1_DIAGNOSTIC_START_INDEX,
                                    "v1_source_end_round_index": V1_SOURCE_END_INDEX,
                                    "prospective_rounds_observed": len(prospective_rows),
                                    "prospective_contiguous_suffix": prospective_contiguous,
                                    "prospective_latest_index": int(prospective_rows["round_index"].max())
                                    if len(prospective_rows) else None},
              "clean_rounds": len(development), "eligible_targets": frame_quality.get("eligible_targets", 0),
              "baseline_rate": float(y_all.mean()), "feature_windows": list(WINDOWS),
              "models": list(BASE_MODEL_NAMES), "model_search": "fixed diverse ensemble; score weights learned on forward score-fit partition",
              "policy_search": {"nested_chronological": True, "coverage_targets": list(TARGET_COVERAGES),
                                "threshold_partition_separate_from_score_fit": True,
                                "old_test_accessed": False},
              "coverage_strategies_validation": validation_policies,
              "coverage_strategies_outer_diagnostic": outer_diagnostics,
              "best_opportunity_policy": strategy,
              "outer_blocks": fold_reports,
              "feature_stability": {"method": "fold-specific, training-only chronological direction consistency",
                                    "retained_by_fold": [row.get("feature_stability", {}).get("retained", [])
                                                         for row in fold_reports]},
              "analog_method": {"method": "nearest neighbors over past-only distribution/pattern features",
                                "neighbors": 100, "minimum_count": 100,
                                "stable_era_rule": "positive lift in >=75% of adequately supported training eras"},
              "old_untouched_test": {"label": "DIAGNOSTIC ONLY", "used_for_selection": False,
                                     "result": prior_report.get("untouched_test")},
              "validation_collapse": _collapse_diagnosis(prior_report),
              "prospective": {"mode": "RESEARCH_SHADOW" if strategy else "NOT_STARTED",
                              "status": "WAITING_FOR_100_CONTIGUOUS_FUTURE_ROUNDS" if prospective_contiguous < 100 else "READY_TO_OBSERVE"}
                              if strategy else {"mode": "NOT_STARTED", "status": "NO_VALIDATED_INTERNAL_POLICY"},
              "production_shadow_ready": False,
              "duration_seconds": time.perf_counter() - started}
    if strategy and score_model is not None and selected_coverage is not None:
        threshold_row = next(row for row in policy["curves"]
                             if row["requested_coverage"] == selected_coverage)
        score_train_end = score_end - cal_end
        uncertainty_history = -components[:score_train_end, 6]
        regime_history = components[:score_train_end, 3]
        report["_runtime_bundle"] = {
            "engine_version": "selective-opportunity-v2", "target": TARGET,
            "model_version": f"opportunity-v2-{int(time.time())}",
            "base_models": model_objects, "score_model": score_model,
            "feature_names": retained, "evidence_feature_names": all_features,
            "analog_X": train_X_selected,
            "analog_y": train_y, "feature_stability_fraction": stable_fraction,
            "score_threshold": threshold_row["score_threshold"],
            "uncertainty_limit": uncertainty_limit,
            "distribution_warning_limit": distribution_warning_limit,
            "coverage_policy": selected_coverage,
        }
    return report
