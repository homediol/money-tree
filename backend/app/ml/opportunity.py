"""Selective, research-only detector for next-round outcomes >= 2.10x.

This module is deliberately separate from the deployed 2.00x model and from
Decision/Risk/Execution. It abstains unless a frozen validation procedure and
an untouched chronological test both support a selective rule.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import math
import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any

import joblib
import numpy as np
import pandas as pd
from sklearn.ensemble import (ExtraTreesClassifier, GradientBoostingClassifier,
                              HistGradientBoostingClassifier, RandomForestClassifier)
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from app.ml.trainer import calibration_diagnostics, evaluate_probabilities

TARGET = 2.10
WINDOWS = (5, 10, 20, 50, 100, 250, 500, 1000)
MIN_PRIOR = 100
MAX_GAP_SECONDS = 120
ENGINE_VERSION = "selective-opportunity-v1"


class CalibratedBinaryModel:
    """Apply a sigmoid calibration fitted only on the calibration partition."""
    def __init__(self, estimator, calibrator=None):
        self.estimator = estimator
        self.calibrator = calibrator

    def predict_proba(self, features):
        raw = self.estimator.predict_proba(features)[:, 1]
        if self.calibrator is None:
            calibrated = raw
        else:
            calibrated = self.calibrator.predict_proba(raw.reshape(-1, 1))[:, 1]
        return np.column_stack([1 - calibrated, calibrated])


def wilson(successes: int, n: int, z: float = 1.959963984540054) -> dict[str, Any]:
    if n <= 0:
        return {"lower": None, "upper": None, "n": 0, "confidence": 0.95}
    p = successes / n
    d = 1 + z * z / n
    center = (p + z * z / (2 * n)) / d
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return {"lower": max(0.0, center - margin), "upper": min(1.0, center + margin),
            "n": n, "confidence": 0.95}


def _entropy(values: np.ndarray) -> float:
    if not len(values):
        return 0.0
    _, counts = np.unique(values, return_counts=True)
    probabilities = counts / counts.sum()
    return float(-(probabilities * np.log2(probabilities)).sum())


def _bucket(values: np.ndarray) -> np.ndarray:
    return np.where(values < 1.5, 0, np.where(values < 4.0, 1, 2))


def validated_rounds(rounds: pd.DataFrame) -> tuple[pd.DataFrame, dict[str, int]]:
    """Fail closed on repeated identities/order, invalid time, and suspect values."""
    counters = {"input": len(rounds), "invalid": 0, "duplicate_id": 0,
                "duplicate_index": 0, "sequence_replay": 0, "valid": 0}
    required = {"round_id", "round_index", "timestamp", "multiplier"}
    if rounds.empty or not required.issubset(rounds.columns):
        counters["invalid"] = len(rounds)
        return pd.DataFrame(columns=sorted(required)), counters
    frame = rounds[list(sorted(required))].copy()
    frame["round_id"] = frame["round_id"].astype(str)
    frame["round_index"] = pd.to_numeric(frame["round_index"], errors="coerce")
    frame["multiplier"] = pd.to_numeric(frame["multiplier"], errors="coerce")
    frame["timestamp_dt"] = pd.to_datetime(frame["timestamp"], format="mixed", utc=True, errors="coerce")
    invalid = (frame["round_id"].str.strip().eq("") | frame["round_index"].isna()
               | frame["multiplier"].isna() | ~np.isfinite(frame["multiplier"])
               | (frame["multiplier"] < 1) | frame["timestamp_dt"].isna())
    counters["invalid"] = int(invalid.sum())
    frame = frame.loc[~invalid].sort_values("round_index", kind="stable")
    duplicate_id = frame["round_id"].duplicated(keep="first")
    counters["duplicate_id"] = int(duplicate_id.sum())
    frame = frame.loc[~duplicate_id]
    duplicate_index = frame["round_index"].duplicated(keep="first")
    counters["duplicate_index"] = int(duplicate_index.sum())
    frame = frame.loc[~duplicate_index].copy()
    # Do not infer a replay from equal multipliers/timestamps: distinct real
    # rounds can share both after a collector catch-up batch. Duplicate IDs or
    # sequence indices above are the verifiable replay signals.
    frame["round_index"] = frame["round_index"].astype("int64")
    counters["valid"] = len(frame)
    return frame.reset_index(drop=True), counters


def build_opportunity_frame(rounds: pd.DataFrame, target: float = TARGET) -> tuple[pd.DataFrame, dict[str, int]]:
    """Build one past-only feature row per eligible next-round target."""
    clean, quality = validated_rounds(rounds)
    if clean.empty:
        return pd.DataFrame(), quality
    values = clean["multiplier"].to_numpy(float)
    indices = clean["round_index"].to_numpy(int)
    stamps = clean["timestamp_dt"].tolist()
    segments: list[tuple[int, int]] = []
    start = 0
    for i in range(1, len(clean)):
        seconds = (stamps[i] - stamps[i - 1]).total_seconds()
        if indices[i] != indices[i - 1] + 1 or not 0 <= seconds <= MAX_GAP_SECONDS:
            segments.append((start, i))
            start = i
    segments.append((start, len(clean)))
    output: list[dict[str, Any]] = []
    for segment_start, segment_end in segments:
        # Only use target rows with 100 contiguous known prior rounds.
        pattern_counts: dict[tuple[str, str], list[int]] = {}
        transition_counts: dict[int, list[int]] = {0: [0, 0], 1: [0, 0]}
        prior_hits = 0
        for target_pos in range(segment_start + MIN_PRIOR, segment_end):
            history = values[segment_start:target_pos]
            label = int(values[target_pos] >= target)
            row = _feature_row(history, target, prior_hits, target_pos - segment_start - MIN_PRIOR,
                               pattern_counts, transition_counts)
            row.update({
                "source_round_id": str(clean.iloc[target_pos - 1]["round_id"]),
                "source_round_index": int(indices[target_pos - 1]),
                "target_round_id": str(clean.iloc[target_pos]["round_id"]),
                "target_round_index": int(indices[target_pos]),
                "target_timestamp": str(clean.iloc[target_pos]["timestamp"]),
                "target_multiplier_value": float(values[target_pos]),
                "target_hit": label,
            })
            output.append(row)

            # Reveal this target only after its feature row has been frozen.
            buckets = _bucket(history)
            streak_key = str(min(row["streak_below_2_1"], 12))
            for length in (2, 3, 5):
                key = "|".join(map(str, buckets[-length:]))
                pair = pattern_counts.setdefault((f"sequence_{length}", key), [0, 0])
                pair[0] += 1
                pair[1] += label
            pair = pattern_counts.setdefault(("streak", streak_key), [0, 0])
            pair[0] += 1
            pair[1] += label
            last_state = int(history[-1] >= target)
            transition_counts[last_state][0] += 1
            transition_counts[last_state][1] += label
            prior_hits += label
    frame = pd.DataFrame(output)
    quality["eligible_targets"] = len(frame)
    quality["excluded_for_history_or_gaps"] = max(0, len(clean) - len(frame))
    quality["segments"] = len(segments)
    return frame, quality


def _feature_row(history: np.ndarray, target: float, prior_hits: int, prior_count: int,
                 pattern_counts: dict, transition_counts: dict) -> dict[str, float | int]:
    buckets = _bucket(history)
    result: dict[str, float | int] = {"past_target_rate": (prior_hits + 10) / (prior_count + 20)}
    for window in WINDOWS:
        sample = history[-window:]
        n = len(sample)
        logs = np.log1p(np.minimum(sample, 100))
        result[f"observed_prior_{window}"] = n
        result[f"mean_{window}"] = float(np.mean(logs))
        result[f"median_{window}"] = float(np.median(logs))
        result[f"std_{window}"] = float(np.std(logs))
        result[f"q25_{window}"] = float(np.quantile(logs, .25))
        result[f"q75_{window}"] = float(np.quantile(logs, .75))
        result[f"rate_ge_2_1_{window}"] = float(np.mean(sample >= target))
        result[f"rate_ge_2_{window}"] = float(np.mean(sample >= 2.0))
        result[f"rate_ge_5_{window}"] = float(np.mean(sample >= 5.0))
        result[f"rate_below_1_5_{window}"] = float(np.mean(sample < 1.5))
        result[f"entropy_{window}"] = _entropy(_bucket(sample))
    for threshold, name in ((target, "2_1"), (5.0, "5"), (10.0, "10")):
        below = 0
        for value in history[-1000:][::-1]:
            if value < threshold:
                below += 1
            else:
                break
        result[f"rounds_since_ge_{name}"] = below
        result[f"streak_below_{name}"] = below
    recent, long = history[-10:], history[-100:]
    result["recent_long_rate_shift_2_1"] = float(np.mean(recent >= target) - np.mean(long >= target))
    result["recent_long_log_mean_shift"] = float(np.mean(np.log1p(np.minimum(recent, 100)))
                                                   - np.mean(np.log1p(np.minimum(long, 100))))
    result["recent_long_volatility_shift"] = float(np.std(np.log1p(np.minimum(recent, 100)))
                                                     - np.std(np.log1p(np.minimum(long, 100))))
    result["alternation_100"] = float(np.mean(buckets[-100:][1:] != buckets[-100:][:-1]))
    for length in (2, 3, 5):
        key = "|".join(map(str, buckets[-length:]))
        count, hits = pattern_counts.get((f"sequence_{length}", key), [0, 0])
        result[f"pattern_sequence_{length}_count"] = count
        result[f"pattern_sequence_{length}_successes"] = hits
        result[f"pattern_sequence_{length}_rate"] = (hits + 10 * result["past_target_rate"]) / (count + 10)
    streak_key = str(min(result["streak_below_2_1"], 12))
    count, hits = pattern_counts.get(("streak", streak_key), [0, 0])
    result["pattern_streak_count"] = count
    result["pattern_streak_successes"] = hits
    result["pattern_streak_rate"] = (hits + 10 * result["past_target_rate"]) / (count + 10)
    last_state = int(history[-1] >= target)
    count, hits = transition_counts[last_state]
    result["transition_count"] = count
    result["transition_rate"] = (hits + 10 * result["past_target_rate"]) / (count + 20)
    return result


def _interval_lift(y: np.ndarray, selected: np.ndarray, baseline: float) -> dict[str, Any]:
    n = int(selected.sum())
    successes = int(np.asarray(y)[selected].sum()) if n else 0
    ci = wilson(successes, n)
    base_ci = wilson(int(round(baseline * len(y))), len(y))
    return {"signals": n, "true_signals": successes, "false_signals": n - successes,
            "precision": successes / n if n else None, "precision_ci95": ci,
            "baseline_rate": baseline, "baseline_ci95": base_ci,
            "lift": (successes / n - baseline) if n else None,
            "lift_ci95": ([ci["lower"] - base_ci["upper"], ci["upper"] - base_ci["lower"]]
                          if n and ci["lower"] is not None and base_ci["lower"] is not None else [None, None]),
            "lift_ratio": (successes / n / baseline) if n and baseline else None,
            "coverage": n / len(y) if len(y) else 0.0,
            "abstention_rate": 1 - (n / len(y)) if len(y) else 1.0,
            "precision_lcb_above_baseline_ucb": bool(n and ci["lower"] > (base_ci["upper"] or 1.0))}


def _factories() -> dict[str, Any]:
    rows = {
        "logistic_regression": lambda: make_pipeline(StandardScaler(), LogisticRegression(C=.1, max_iter=800, random_state=42)),
        "random_forest": lambda: RandomForestClassifier(n_estimators=100, max_depth=6, min_samples_leaf=40,
                                                          max_features=.5, random_state=42, n_jobs=1),
        "extra_trees": lambda: ExtraTreesClassifier(n_estimators=120, max_depth=6, min_samples_leaf=40,
                                                      max_features=.5, random_state=42, n_jobs=1),
        "gradient_boosting": lambda: GradientBoostingClassifier(n_estimators=80, max_depth=2, min_samples_leaf=40,
                                                                 learning_rate=.04, random_state=42),
        "hist_gradient_boosting": lambda: HistGradientBoostingClassifier(max_iter=80, max_leaf_nodes=7,
                                                                           min_samples_leaf=40, learning_rate=.04,
                                                                           l2_regularization=10, random_state=42,
                                                                           early_stopping=False),
    }
    try:
        from xgboost import XGBClassifier
        rows["xgboost"] = lambda: XGBClassifier(n_estimators=80, max_depth=2, learning_rate=.04,
                                                 subsample=.85, colsample_bytree=.7, reg_lambda=10,
                                                 random_state=42, n_jobs=1, eval_metric="logloss")
    except ImportError:
        pass
    try:
        from lightgbm import LGBMClassifier
        rows["lightgbm"] = lambda: LGBMClassifier(n_estimators=80, max_depth=3, num_leaves=7,
                                                    min_child_samples=40, learning_rate=.04,
                                                    reg_lambda=10, random_state=42, n_jobs=1, verbosity=-1)
    except ImportError:
        pass
    return rows


def _thresholds(probabilities: np.ndarray) -> list[float]:
    values = np.asarray(probabilities, dtype=float)
    if not len(values):
        return []
    # Validation quantiles only; no fixed hit-rate quota and no test access.
    return sorted({float(np.quantile(values, q)) for q in (.90, .95, .975, .99, .995)})


def _best_validation_threshold(y: np.ndarray, p: np.ndarray, baseline: float) -> dict[str, Any]:
    curves = []
    minimum = max(10, int(math.ceil(len(y) * .005)))
    for threshold in _thresholds(p):
        selected = p >= threshold
        metrics = _interval_lift(y, selected, baseline)
        metrics["threshold"] = threshold
        metrics["minimum_signal_count"] = minimum
        metrics["selection_qualified"] = bool(
            metrics["signals"] >= minimum
            and metrics["precision_ci95"]["lower"] is not None
            and metrics["precision_ci95"]["lower"] > (metrics["baseline_ci95"]["upper"] or 1.0)
        )
        curves.append(metrics)
    if not curves:
        return {"threshold": None, "curves": [], "selection_qualified": False}
    # Pick on validation only. Prioritize conservative evidence, then selectivity.
    selected = max(curves, key=lambda row: (
        row["precision_ci95"]["lower"] if row["precision_ci95"]["lower"] is not None else -1,
        row["lift"] if row["lift"] is not None else -1,
        row["threshold"],
    ))
    return {**selected, "curves": curves}


def _walk_forward(factory, X: np.ndarray, y: np.ndarray, *, seed: int = 42) -> list[dict[str, Any]]:
    n = len(y)
    folds = []
    for fraction in (.50, .60, .70):
        train_end = int(n * fraction)
        tune_end = min(n, train_end + max(50, int(n * .08)))
        eval_end = min(n, tune_end + max(50, int(n * .08)))
        if train_end < 200 or tune_end - train_end < 30 or eval_end - tune_end < 30:
            continue
        if len(np.unique(y[:train_end])) < 2:
            continue
        model = factory()
        model.fit(X[:train_end], y[:train_end])
        p_tune = model.predict_proba(X[train_end:tune_end])[:, 1]
        threshold = _best_validation_threshold(y[train_end:tune_end], p_tune,
                                               float(y[train_end:tune_end].mean()))["threshold"]
        p_eval = model.predict_proba(X[tune_end:eval_end])[:, 1]
        selected = p_eval >= threshold if threshold is not None else np.zeros(len(p_eval), dtype=bool)
        metrics = _interval_lift(y[tune_end:eval_end], selected, float(y[tune_end:eval_end].mean()))
        metrics.update({"train_n": train_end, "threshold_n": tune_end - train_end,
                        "evaluation_n": eval_end - tune_end, "threshold": threshold,
                        "block_start": tune_end, "block_end": eval_end})
        folds.append(metrics)
    return folds


class OpportunityResearchEngine:
    """On-demand research plus frozen SHADOW-only observation stream."""

    def __init__(self, repository, artifact_dir: Path, *, max_feature_age_s: int = 600):
        self.repository = repository
        self.artifact_dir = Path(artifact_dir) / "selective_opportunity"
        self.max_feature_age_s = max_feature_age_s
        self.report: dict[str, Any] = repository.load_application_state("selective_opportunity_2_1x") or {
            "status": "NOT_EVALUATED", "target": TARGET, "target_definition": "next_round_ge_2_1x",
            "best_selective_model": None, "shadow_ready": False, "reason": "Research has not been run."}
        self.bundle: dict[str, Any] | None = None
        self.last_skip_reason: str | None = None
        self._run_lock = Lock()
        self._running = False
        self._load_artifact()

    def _load_artifact(self) -> None:
        path = self.report.get("artifact_path")
        if not self.report.get("shadow_ready") or not path:
            return
        try:
            self.bundle = joblib.load(path)
            if self.bundle.get("engine_version") != ENGINE_VERSION or self.bundle.get("target") != TARGET:
                self.bundle = None
        except (OSError, ValueError, EOFError):
            self.bundle = None

    def run(self, dataset_service) -> dict[str, Any]:
        if not self._run_lock.acquire(blocking=False):
            return self.current(dataset_service.clean_rounds.copy()) | {"status": "RESEARCH_RUNNING"}
        self._running = True
        try:
            return self._run(dataset_service)
        finally:
            self._running = False
            self._run_lock.release()

    def _run(self, dataset_service) -> dict[str, Any]:
        started = time.perf_counter()
        frame, quality = build_opportunity_frame(dataset_service.clean_rounds.copy(), TARGET)
        if frame.empty:
            report = self._empty(quality, "No targets have 100 contiguous, validated prior rounds.")
            return self._save(report)
        n = len(frame)
        train_end, calibration_end = int(n * .60), int(n * .70)
        validation_end, = (int(n * .85),)
        train, calibration = frame.iloc[:train_end], frame.iloc[train_end:calibration_end]
        validation, test = frame.iloc[calibration_end:validation_end], frame.iloc[validation_end:]
        clean_source, _ = validated_rounds(dataset_service.clean_rounds.copy())
        feature_names = [name for name in frame.columns if name not in {
            "source_round_id", "source_round_index", "target_round_id", "target_round_index",
            "target_timestamp", "target_multiplier_value", "target_hit"}]
        report_base = {"status": "INSUFFICIENT_DATA", "target": TARGET,
                       "target_definition": "next_round_ge_2_1x", "clean_rounds": quality["valid"],
                       "excluded": quality, "rounds_evaluated": n,
                       "baseline_rate": float(frame["target_hit"].mean()),
                       "baseline_count": int(frame["target_hit"].sum()),
                       "feature_windows": list(WINDOWS), "feature_count": len(feature_names),
                       "algorithms_tested": [], "best_selective_model": None,
                       "optional_algorithms": {name: ("AVAILABLE" if importlib.util.find_spec(name) else "NOT_INSTALLED")
                                               for name in ("xgboost", "lightgbm")},
                       "walk_forward": [], "block_stability": {}, "calibration": None,
                       "untouched_test": None, "threshold_curves": [], "shadow_ready": False,
                       "comparison_baselines": {
                           f">={threshold:.2f}x": {
                               "successes": int((clean_source["multiplier"].astype(float) >= threshold).sum()),
                               "rounds": int(len(clean_source)),
                               "rate": float((clean_source["multiplier"].astype(float) >= threshold).mean())
                                       if len(clean_source) else None,
                           } for threshold in (2.0, TARGET, 3.0, 5.0)
                       },
                       "source": ("PostgreSQL:aviator_rounds" if getattr(dataset_service, "source_loader", None)
                                  else "validated local history"), "mode": "HISTORICAL_BACKTEST"}
        if min(len(train), len(calibration), len(validation), len(test)) < 100:
            report = report_base | {"reason": "Chronological train/calibration/validation/test splits need at least 100 targets each."}
            return self._save(report)
        X = frame[feature_names].to_numpy(float)
        y = frame["target_hit"].to_numpy(int)
        if not np.isfinite(X).all() or len(np.unique(y[:train_end])) < 2:
            report = report_base | {"reason": "Feature matrix is nonfinite or training split has one target class."}
            return self._save(report)
        candidates: list[dict[str, Any]] = []
        fitted: dict[str, Any] = {}
        validation_probabilities: dict[str, np.ndarray] = {}
        factory_map = _factories()
        for name, factory in factory_map.items():
            if len(np.unique(y[:train_end])) < 2:
                continue
            try:
                estimator = factory()
                estimator.fit(X[:train_end], y[:train_end])
                raw_calibration = estimator.predict_proba(X[train_end:calibration_end])[:, 1]
                calibrator = None
                if (len(raw_calibration) >= 100
                        and len(np.unique(y[train_end:calibration_end])) == 2):
                    calibrator = LogisticRegression(C=1.0, max_iter=500, random_state=42)
                    calibrator.fit(raw_calibration.reshape(-1, 1), y[train_end:calibration_end])
                model = CalibratedBinaryModel(estimator, calibrator)
                p_validation = model.predict_proba(X[calibration_end:validation_end])[:, 1]
                validation_probabilities[name] = p_validation
                p_calibration = model.predict_proba(X[train_end:calibration_end])[:, 1]
                threshold_row = _best_validation_threshold(y[calibration_end:validation_end], p_validation,
                                                            float(y[calibration_end:validation_end].mean()))
                folds = _walk_forward(factory, X[:train_end], y[:train_end])
                fold_wins = sum((fold.get("lift") or 0) > 0 and fold["signals"] > 0 for fold in folds)
                threshold_row.update({"model": name, "walk_forward": folds,
                                      "walk_forward_positive_blocks": fold_wins,
                                      "walk_forward_blocks": len(folds),
                                      "passes_walk_forward": len(folds) >= 3 and fold_wins >= 2,
                                      "validation_brier": float(np.mean((y[calibration_end:validation_end] - p_validation) ** 2)),
                                      "calibration_brier": float(np.mean((y[train_end:calibration_end] - p_calibration) ** 2)),
                                      "calibration_diagnostics": {
                                          **calibration_diagnostics(y[calibration_end:validation_end], p_validation),
                                          "method": "platt_sigmoid" if calibrator is not None else "none",
                                          "fit_partition": "chronological_calibration_only" if calibrator is not None else None,
                                      },
                                      "selection_qualified": bool(threshold_row.get("selection_qualified")
                                          and len(folds) >= 3 and fold_wins >= 2)})
                candidates.append(threshold_row)
                fitted[name] = model
            except Exception as exc:
                candidates.append({"model": name, "selection_qualified": False,
                                   "error": f"{type(exc).__name__}: {exc}"})
        if not candidates:
            return self._save(report_base | {"reason": "No supported model completed evaluation."})
        # Lock the candidate and threshold using validation only. Open the final
        # 15% once, for this sole preselected candidate.
        chosen = max(candidates, key=lambda row: (
            row.get("selection_qualified", False),
            (row.get("precision_ci95") or {}).get("lower") or -1,
            row.get("lift") or -1,
            row.get("model", "")))
        selected_model = fitted.get(chosen.get("model"))
        validation_agreement = None
        if chosen.get("threshold") is not None and chosen.get("signals"):
            agreement_counts = np.column_stack([
                values >= chosen["threshold"] for values in validation_probabilities.values()
            ]).sum(axis=1)
            selected_mask = validation_probabilities[chosen["model"]] >= chosen["threshold"]
            selected_agreement = agreement_counts[selected_mask]
            validation_agreement = {
                "families_tested": len(validation_probabilities),
                "selected_signals_with_two_or_more_models": int((selected_agreement >= 2).sum()),
                "selected_signal_agreement_rate": float(np.mean(selected_agreement >= 2)),
                "mean_agreeing_families_on_signal": float(np.mean(selected_agreement)),
            }
        test_report = None
        test_predictions = None
        if selected_model is not None and chosen.get("threshold") is not None:
            p_test = selected_model.predict_proba(X[validation_end:])[:, 1]
            test_predictions = p_test
            y_test = y[validation_end:]
            selected = p_test >= chosen["threshold"]
            baseline_test = float(y_test.mean())
            selective = _interval_lift(y_test, selected, baseline_test)
            signals_idx = np.flatnonzero(selected)
            gaps = np.diff(signals_idx)
            test_report = {**selective,
                           "rounds_evaluated": len(y_test), "threshold": chosen["threshold"],
                           "base_rate_observed": float(y_test.mean()),
                           "brier_score": float(np.mean((y_test - p_test) ** 2)),
                           "log_loss": float(-np.mean(y_test * np.log(np.clip(p_test, 1e-8, 1))
                                                       + (1 - y_test) * np.log(np.clip(1 - p_test, 1e-8, 1)))),
                           "calibration": calibration_diagnostics(y_test, p_test),
                           "max_signal_gap": int(gaps.max() + 1) if len(gaps) else None,
                           "signals_per_100": selective["signals"] / max(len(y_test), 1) * 100,
                           "signals_per_500": selective["signals"] / max(len(y_test), 1) * 500,
                           "target": TARGET, "untouched": True}
        blocks = self._block_stability(y[validation_end:], test_predictions, chosen.get("threshold"),
                                       test["target_timestamp"].astype(str).tolist()) if test_predictions is not None else {}
        validation_pass = bool(chosen.get("selection_qualified"))
        test_pass = bool(test_report and test_report["signals"] >= max(10, int(math.ceil(len(test) * .005)))
                         and test_report["precision_ci95"]["lower"] is not None
                         and test_report["precision_ci95"]["lower"] > test_report["baseline_ci95"]["upper"])
        stability_pass = bool(blocks.get("independent_blocks", 0) >= 3
                              and blocks.get("positive_lift_blocks", 0) >= math.ceil(blocks.get("independent_blocks", 0) * .67))
        shadow_ready = validation_pass and chosen.get("passes_walk_forward") and test_pass and stability_pass
        version = f"opportunity-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:8]}"
        artifact_path = None
        if shadow_ready and selected_model is not None:
            self.artifact_dir.mkdir(parents=True, exist_ok=True)
            artifact = {"engine_version": ENGINE_VERSION, "target": TARGET,
                        "model_version": version, "model": selected_model,
                        "agreement_models": fitted,
                        "feature_names": feature_names, "threshold": float(chosen["threshold"]),
                        "validation_evidence": {key: chosen.get(key) for key in
                            ("signals", "precision", "precision_ci95", "baseline_rate", "baseline_ci95", "lift", "walk_forward")}}
            artifact_path = self.artifact_dir / f"{version}.joblib"
            temp_path = artifact_path.with_suffix(".joblib.tmp")
            joblib.dump(artifact, temp_path)
            os.replace(temp_path, artifact_path)
            self.bundle = artifact
        else:
            self.bundle = None
        report = report_base | {
            "status": "SHADOW_READY" if shadow_ready else "NO_RELIABLE_SELECTIVE_SIGNAL",
            "best_selective_model": chosen.get("model"),
            "selected_threshold": chosen.get("threshold"),
            "validation": {key: value for key, value in chosen.items() if key != "curves"},
            "model_agreement": validation_agreement,
            "threshold_curves": chosen.get("curves", []),
            "algorithms_tested": [{"model": row.get("model"), "validation_brier": row.get("validation_brier"),
                                   "signals": row.get("signals"), "precision": row.get("precision"),
                                   "precision_ci95": row.get("precision_ci95"), "lift": row.get("lift"),
                                   "selection_qualified": row.get("selection_qualified"),
                                   "walk_forward_positive_blocks": row.get("walk_forward_positive_blocks"),
                                   "walk_forward_blocks": row.get("walk_forward_blocks"),
                                   "error": row.get("error")} for row in candidates],
            "walk_forward": chosen.get("walk_forward", []), "block_stability": blocks,
            "calibration": chosen.get("calibration_diagnostics"),
            "untouched_test": test_report, "shadow_ready": shadow_ready,
            "artifact_path": str(artifact_path) if artifact_path else None,
            "model_version": version if shadow_ready else None,
            "reasons": self._reasons(validation_pass, chosen, test_pass, stability_pass, test_report),
            "duration_seconds": time.perf_counter() - started,
        }
        return self._save(report)

    @staticmethod
    def _block_stability(y, probabilities, threshold, timestamps=None) -> dict[str, Any]:
        if probabilities is None or threshold is None or len(y) < 500:
            return {"status": "INSUFFICIENT_BLOCKS", "independent_blocks": 0,
                    "positive_lift_blocks": 0, "block_size": 500, "blocks": [],
                    "1000_round": {"independent_blocks": 0, "positive_lift_blocks": 0, "blocks": []},
                    "daily": {"periods": 0, "positive_lift_periods": 0, "blocks": []}}
        def fixed_blocks(size):
            result = []
            for start in range(0, len(y), size):
                stop = min(start + size, len(y))
                if stop - start < size:
                    continue
                measured = _interval_lift(y[start:stop], probabilities[start:stop] >= threshold,
                                          float(y[start:stop].mean()))
                result.append({"start": start, "end": stop, **measured})
            wins = sum(row["lift"] is not None and row["lift"] > 0 for row in result)
            return {"block_size": size, "independent_blocks": len(result),
                    "positive_lift_blocks": wins,
                    "retained_advantage_fraction": wins / len(result) if result else None,
                    "blocks": result}
        by_day: dict[str, list[int]] = {}
        for index, value in enumerate(timestamps or []):
            day = str(value)[:10]
            by_day.setdefault(day, []).append(index)
        daily = []
        for day, positions in by_day.items():
            if len(positions) < 20:
                continue
            selection = probabilities[positions] >= threshold
            measured = _interval_lift(y[positions], selection, float(y[positions].mean()))
            daily.append({"date": day, **measured})
        daily_wins = sum(row["lift"] is not None and row["lift"] > 0 for row in daily)
        blocks_500 = fixed_blocks(500)
        return {"status": "MEASURED" if blocks_500["blocks"] else "INSUFFICIENT_BLOCKS",
                **blocks_500, "blocks": blocks_500["blocks"],
                "1000_round": fixed_blocks(1000),
                "daily": {"periods": len(daily), "positive_lift_periods": daily_wins,
                          "retained_advantage_fraction": daily_wins / len(daily) if daily else None,
                          "blocks": daily}}

    @staticmethod
    def _reasons(validation_pass, chosen, test_pass, stability_pass, test_report):
        reasons = []
        if not validation_pass:
            reasons.append("validation_threshold_did_not_show_defensible_selective_lift")
        if not chosen.get("passes_walk_forward"):
            reasons.append("walk_forward_advantage_not_stable")
        if not test_pass:
            reasons.append("untouched_test_did_not_confirm_precision_lift_with_minimum_support")
        if not stability_pass:
            reasons.append("fewer_than_three_500_round_test_blocks_or_advantage_not_stable")
        if test_report and not test_report.get("signals"):
            reasons.append("no_signals_on_untouched_test")
        return reasons or ["all predeclared historical gates passed; begin SHADOW observation"]

    def _empty(self, quality, reason):
        return {"status": "INSUFFICIENT_DATA", "target": TARGET,
                "target_definition": "next_round_ge_2_1x", "clean_rounds": quality.get("valid", 0),
                "excluded": quality, "rounds_evaluated": 0, "baseline_rate": None,
                "algorithms_tested": [], "best_selective_model": None,
                "walk_forward": [], "block_stability": {}, "calibration": None,
                "untouched_test": None, "threshold_curves": [], "shadow_ready": False,
                "mode": "HISTORICAL_BACKTEST", "reason": reason}

    def _save(self, report):
        report["generated_at"] = datetime.now(timezone.utc).isoformat()
        self.report = report
        if not report.get("shadow_ready"):
            self.bundle = None
        self.repository.save_application_state("selective_opportunity_2_1x", report, report["generated_at"])
        return report

    def current(self, rounds: pd.DataFrame | None = None) -> dict[str, Any]:
        payload = dict(self.report)
        payload["current_opportunity"] = self._score_current(rounds) if rounds is not None else {
            "status": "NO_SIGNAL", "reason_codes": ["current_history_unavailable"]}
        payload["mode"] = "SHADOW" if self.bundle else "HISTORICAL_BACKTEST"
        latest_index = int(rounds.iloc[-1]["round_index"]) if rounds is not None and len(rounds) else None
        shadow = self._shadow_metrics(latest_index)
        payload["shadow"] = shadow
        return payload

    def _score_current(self, rounds: pd.DataFrame | None) -> dict[str, Any]:
        if self.bundle is None or not self.report.get("shadow_ready"):
            return {"status": "NO_SIGNAL", "probability_2_1x": None, "confidence": None,
                    "uncertainty": None, "evidence_strength": "NONE", "opportunity_score": 0,
                    "abstain": True, "reason_codes": ["no_validated_selective_model"]}
        try:
            features = self._latest_features(rounds)
        except ValueError:
            return {"status": "NO_SIGNAL", "probability_2_1x": None, "confidence": None,
                    "uncertainty": None, "evidence_strength": "NONE", "opportunity_score": 0,
                    "abstain": True, "reason_codes": ["insufficient_contiguous_history"]}
        latest_time = pd.to_datetime(rounds.iloc[-1]["timestamp"], format="mixed", utc=True, errors="coerce")
        latest_age = ((datetime.now(timezone.utc) - latest_time.to_pydatetime()).total_seconds()
                      if not pd.isna(latest_time) else float("inf"))
        if not 0 <= latest_age <= self.max_feature_age_s:
            return {"status": "NO_SIGNAL", "probability_2_1x": None, "confidence": None,
                    "uncertainty": None, "evidence_strength": "NONE", "opportunity_score": 0,
                    "abstain": True, "reason_codes": ["source_round_stale_or_future"]}
        names = self.bundle["feature_names"]
        p = float(self.bundle["model"].predict_proba(np.array([[features[name] for name in names]], dtype=float))[0, 1])
        threshold = float(self.bundle["threshold"])
        validation = self.bundle["validation_evidence"]
        ci = validation.get("precision_ci95") or {}
        elevated = p >= threshold
        score = max(0.0, min(100.0, (p - threshold) / max(1 - threshold, 1e-8) * 100)) if elevated else 0.0
        pattern_n = int(features.get("pattern_sequence_3_count", 0))
        pattern_hits = int(features.get("pattern_sequence_3_successes", 0))
        pattern_ci = wilson(pattern_hits, pattern_n)
        baseline_ci = validation.get("baseline_ci95") or {}
        pattern_agrees = bool(pattern_n >= 50 and pattern_ci["lower"] is not None
                              and pattern_ci["lower"] > (baseline_ci.get("upper") or 1.0))
        agreement_models = self.bundle.get("agreement_models") or {self.bundle.get("model_version", "selected"): self.bundle["model"]}
        agreeing = [name for name, model in agreement_models.items()
                    if float(model.predict_proba(np.array([[features[key] for key in names]], dtype=float))[0, 1]) >= threshold]
        status = ("HIGH_CONFIDENCE_CANDIDATE" if elevated and pattern_agrees and len(agreeing) >= 2 else
                  "CANDIDATE" if elevated else "WATCH" if p >= threshold * .8 else "NO_SIGNAL")
        return {"status": status, "probability_2_1x": p,
                "confidence": "HIGH" if status == "HIGH_CONFIDENCE_CANDIDATE" else "MEDIUM" if elevated else "LOW",
                "uncertainty": {"validation_signal_precision_ci95": ci, "pattern_precision_ci95": pattern_ci},
                "evidence_strength": "MODEL_AND_PATTERN" if pattern_agrees else "MODEL_ONLY",
                "model_agreement": {"families_tested": len(agreement_models),
                                    "families_at_threshold": len(agreeing), "agreeing_models": agreeing,
                                    "pattern_support": pattern_n,
                                    "pattern_agrees_with_baseline": pattern_agrees},
                "opportunity_score": score, "threshold": threshold,
                "abstain": status not in {"CANDIDATE", "HIGH_CONFIDENCE_CANDIDATE"},
                "reason_codes": (["pattern_evidence_not_independently_strong"] if elevated and not pattern_agrees
                                 else [] if elevated else ["below_validation_selected_threshold"]),
                "model_version": self.bundle["model_version"]}

    @staticmethod
    def _latest_features(rounds: pd.DataFrame) -> dict[str, float]:
        clean, _ = validated_rounds(rounds)
        if len(clean) < MIN_PRIOR:
            raise ValueError("no eligible source features")
        # Use only the latest contiguous episode; never bridge a history gap.
        start = len(clean) - 1
        stamps = clean["timestamp_dt"].tolist()
        indices = clean["round_index"].to_numpy(int)
        while start > 0:
            seconds = (stamps[start] - stamps[start - 1]).total_seconds()
            if indices[start] != indices[start - 1] + 1 or not 0 <= seconds <= MAX_GAP_SECONDS:
                break
            start -= 1
        values = clean["multiplier"].to_numpy(float)
        if len(clean) - start < MIN_PRIOR:
            raise ValueError("latest contiguous episode is too short")
        patterns: dict[tuple[str, str], list[int]] = {}
        transitions = {0: [0, 0], 1: [0, 0]}
        prior_hits = 0
        prior_count = 0
        for target_pos in range(start + MIN_PRIOR, len(clean)):
            history = values[start:target_pos]
            buckets = _bucket(history)
            streak = 0
            for value in history[-1000:][::-1]:
                if value < TARGET:
                    streak += 1
                else:
                    break
            streak_key = str(min(streak, 12))
            label = int(values[target_pos] >= TARGET)
            for length in (2, 3, 5):
                key = "|".join(map(str, buckets[-length:]))
                pair = patterns.setdefault((f"sequence_{length}", key), [0, 0])
                pair[0] += 1
                pair[1] += label
            pair = patterns.setdefault(("streak", streak_key), [0, 0])
            pair[0] += 1
            pair[1] += label
            transitions[int(history[-1] >= TARGET)][0] += 1
            transitions[int(history[-1] >= TARGET)][1] += label
            prior_hits += label
            prior_count += 1
        result = _feature_row(values[start:], TARGET, prior_hits, prior_count, patterns, transitions)
        return {key: float(value) for key, value in result.items()}

    def _shadow_metrics(self, latest_round_index: int | None = None) -> dict[str, Any]:
        observations = self.repository.list_opportunity_observations(limit=100000)
        scored = [row for row in observations if row.get("status") == "SCORED"]
        signals = [row for row in scored if row.get("signal")]
        true = sum(int(row.get("outcome_2_1x", 0)) for row in signals)
        false = len(signals) - true
        baseline_count = sum(int(row.get("outcome_2_1x", 0)) for row in scored)
        baseline = baseline_count / len(scored) if scored else None
        precision = true / len(signals) if signals else None
        last_signal = next((row for row in observations if row.get("signal")), None)
        last_signal_index = int(last_signal["source_round_index"]) if last_signal else None
        last_gap = (latest_round_index - last_signal_index
                    if latest_round_index is not None and last_signal_index is not None else None)
        return {"status": "OBSERVING" if self.bundle else "NOT_STARTED",
                "rounds_evaluated": len(scored), "signals_generated": len(signals),
                "rounds_since_last_signal": last_gap,
                "true_signals": true, "false_signals": false,
                "precision": precision, "coverage": len(signals) / len(scored) if scored else None,
                "abstention_rate": 1 - len(signals) / len(scored) if scored else None,
                "signals_per_100": len(signals) / len(scored) * 100 if scored else None,
                "signals_per_500": len(signals) / len(scored) * 500 if scored else None,
                "baseline_rate": baseline, "signal_rate": precision,
                "lift": precision - baseline if precision is not None and baseline is not None else None,
                "precision_ci95": wilson(true, len(signals)),
                "pending": sum(row.get("status") == "PENDING" for row in observations),
                "unknown": sum(row.get("status") == "UNKNOWN" for row in observations)}

    def reconcile(self, rounds: pd.DataFrame) -> int:
        by_index = {int(row.round_index): row for row in rounds.itertuples(index=False)}
        latest = max(by_index, default=0)
        count = 0
        for item in self.repository.list_opportunity_observations(status="PENDING", limit=10000):
            target = by_index.get(int(item["source_round_index"]) + 1)
            if target is None:
                if latest > int(item["source_round_index"]) + 1:
                    item.update(status="UNKNOWN", reason_codes=["target_round_missing"],
                                resolved_at=datetime.now(timezone.utc).isoformat())
                    self.repository.resolve_opportunity_observation(item)
                    count += 1
                continue
            source = by_index.get(int(item["source_round_index"]))
            try:
                target_time = pd.to_datetime(target.timestamp, format="mixed", utc=True, errors="raise")
                created = pd.to_datetime(item["created_at"], format="mixed", utc=True, errors="raise")
                source_time = pd.to_datetime(source.timestamp, format="mixed", utc=True, errors="raise") if source is not None else pd.NaT
                timely = source is not None and str(source.round_id) == item["source_round_id"] and target_time > created and 0 <= (target_time - source_time).total_seconds() <= MAX_GAP_SECONDS
            except (ValueError, TypeError):
                timely = False
            if timely:
                outcome = int(float(target.multiplier) >= TARGET)
                item.update(status="SCORED", target_round_id=str(target.round_id),
                            actual_multiplier=float(target.multiplier), outcome_2_1x=outcome,
                            resolved_at=datetime.now(timezone.utc).isoformat())
            else:
                item.update(status="UNKNOWN", target_round_id=str(target.round_id),
                            reason_codes=["target_not_proven_future_or_round_gap"],
                            resolved_at=datetime.now(timezone.utc).isoformat())
            self.repository.resolve_opportunity_observation(item)
            count += 1
        return count

    def observe(self, dataset_service, *, collector_latest_round_id: str | None = None) -> dict[str, Any] | None:
        if self.bundle is None or not self.report.get("shadow_ready"):
            self.last_skip_reason = "no_validated_selective_model"
            return None
        rounds = dataset_service.clean_rounds
        if rounds.empty:
            self.last_skip_reason = "history_unavailable"
            return None
        source = rounds.iloc[-1]
        if collector_latest_round_id is not None and str(collector_latest_round_id) != str(source.round_id):
            self.last_skip_reason = "collector_and_dataset_latest_round_differ"
            return None
        stamp = pd.to_datetime(source.timestamp, format="mixed", utc=True, errors="coerce")
        age = (datetime.now(timezone.utc) - stamp.to_pydatetime()).total_seconds() if not pd.isna(stamp) else float("inf")
        if not 0 <= age <= self.max_feature_age_s:
            self.last_skip_reason = "source_round_stale_or_future"
            return None
        assessment = self._score_current(rounds)
        features = self._latest_features(rounds)
        signal = assessment["status"] in {"CANDIDATE", "HIGH_CONFIDENCE_CANDIDATE"}
        item = {"observation_id": hashlib.sha256(f"{self.bundle['model_version']}|{source.round_id}".encode()).hexdigest(),
                "model_version": self.bundle["model_version"], "source_round_id": str(source.round_id),
                "source_round_index": int(source.round_index), "source_round_timestamp": str(source.timestamp),
                "target_threshold": TARGET, "created_at": datetime.now(timezone.utc).isoformat(),
                "probability_2_1x": assessment["probability_2_1x"], "confidence": assessment["confidence"],
                "uncertainty": assessment["uncertainty"], "evidence_strength": assessment["evidence_strength"],
                "opportunity_score": assessment["opportunity_score"], "signal_threshold": assessment.get("threshold"),
                "status": "PENDING", "signal": signal, "abstain": assessment["abstain"],
                "state": assessment["status"], "reason_codes": assessment["reason_codes"],
                "evidence": {"features": features, "validation": self.bundle["validation_evidence"]},
                "target_round_id": None, "outcome_2_1x": None, "actual_multiplier": None,
                "usable": False, "mode": "SHADOW"}
        self.last_skip_reason = None
        return self.repository.save_opportunity_observation(item)
