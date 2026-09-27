"""Chronological, leakage-safe binary model training on Part 4 features."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

import numpy as np
import pandas as pd
from sklearn.ensemble import ExtraTreesClassifier, HistGradientBoostingClassifier, RandomForestClassifier
from sklearn.isotonic import IsotonicRegression
from sklearn.inspection import permutation_importance
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import (accuracy_score, average_precision_score, brier_score_loss,
                             confusion_matrix, f1_score, log_loss, precision_score,
                             recall_score, roc_auc_score)
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler
from app.services.dataset_service import DatasetService

TARGET_COLUMN = "target_2x"
IDENTITY_COLUMNS = {"round_id", "round_index", "timestamp", TARGET_COLUMN, "sequence_last_10"}
FEATURE_VERSION_PREFIX = "part4"
SEED = 42
MAX_HISTORY_GAP_S = 120


class ProbabilityCalibratedModel:
    """Serializable wrapper applying a validation-fitted sigmoid mapping."""
    def __init__(self, model, calibrator):
        self.model, self.calibrator = model, calibrator

    def predict_proba(self, features):
        raw = self.model.predict_proba(features)[:, 1]
        calibrated = (self.calibrator.predict_proba(raw.reshape(-1, 1))[:, 1]
                      if hasattr(self.calibrator, "predict_proba") else self.calibrator.predict(raw))
        return np.column_stack([1 - calibrated, calibrated])


@dataclass
class TrainingResult:
    validated: bool
    status: str
    message: str
    dataset_size: int
    models: dict[str, Any]
    ensemble: dict[str, Any]
    baselines: dict[str, Any]
    calibration: dict[str, Any]
    trained_at: str
    model_version: str | None = None
    feature_version: str | None = None
    feature_names: list[str] | None = None
    feature_schema_hash: str | None = None
    target_definition: str = "next_round_ge_2x"
    algorithm: str | None = None
    hyperparameters: dict[str, Any] | None = None
    class_distribution: dict[str, Any] | None = None
    splits: dict[str, int] | None = None
    training_date_range: dict[str, str | None] | None = None
    validation_metrics: dict[str, Any] | None = None
    test_metrics: dict[str, Any] | None = None
    walk_forward: list[dict[str, Any]] | None = None
    overfitting_checks: dict[str, Any] | None = None
    dataset_hash: str | None = None
    feature_importance: list[dict[str, Any]] | None = None
    data_quality: dict[str, Any] | None = None
    selection_reason: str | None = None
    source_sha256: str | None = None
    last_training_round_index: int | None = None

    def model_dump(self) -> dict[str, Any]:
        return {key: value for key, value in self.__dict__.items() if not key.startswith("_")}


def feature_schema(dataset: pd.DataFrame) -> tuple[list[str], str, str]:
    names = [name for name in dataset.columns if name not in IDENTITY_COLUMNS and pd.api.types.is_numeric_dtype(dataset[name])]
    payload = json.dumps([(name, str(dataset[name].dtype)) for name in names], separators=(",", ":"))
    digest = hashlib.sha256(payload.encode()).hexdigest()
    return names, digest, f"{FEATURE_VERSION_PREFIX}-{digest[:12]}"


def causal_frequency(y: np.ndarray, start: int, stop: int, window: int | None = None) -> np.ndarray:
    """At index i, the estimate sees y[:i], never y[i] or later."""
    return np.asarray([float(np.mean(y[max(0, i - window):i] if window else y[:i]))
                       for i in range(start, stop)], dtype=float)


def block_bootstrap_brier_advantage(y: np.ndarray, model_p: np.ndarray, baseline_p: np.ndarray,
                                    *, repeats: int = 400, block: int = 25,
                                    baseline_name: str = "frozen_training_frequency") -> dict[str, Any]:
    """Positive values favor the model; contiguous blocks retain local dependence."""
    losses = (np.asarray(y) - np.asarray(baseline_p)) ** 2 - (np.asarray(y) - np.asarray(model_p)) ** 2
    n = len(losses)
    if n < 2:
        return {"point": None, "ci95": [None, None], "n": n}
    block = min(block, n)
    rng = np.random.default_rng(SEED)
    estimates = []
    for _ in range(repeats):
        starts = rng.integers(0, n - block + 1, size=int(np.ceil(n / block)))
        indices = np.concatenate([np.arange(start, start + block) for start in starts])[:n]
        estimates.append(float(losses[indices].mean()))
    return {"point": float(losses.mean()), "ci95": [float(x) for x in np.quantile(estimates, [.025, .975])],
            "n": n, "block_size": block, "repeats": repeats, "baseline": baseline_name}


def evaluate_probabilities(y_true: np.ndarray, probabilities: np.ndarray) -> dict[str, Any]:
    probabilities = np.clip(np.asarray(probabilities, dtype=float), 1e-8, 1 - 1e-8)
    y_true = np.asarray(y_true, dtype=int)
    predicted = (probabilities >= 0.5).astype(int)
    tn, fp, fn, tp = confusion_matrix(y_true, predicted, labels=[0, 1]).ravel()
    both = len(np.unique(y_true)) == 2
    calibration = calibration_diagnostics(y_true, probabilities)
    return {
        "accuracy": float(accuracy_score(y_true, predicted)),
        "precision": float(precision_score(y_true, predicted, zero_division=0)),
        "recall": float(recall_score(y_true, predicted, zero_division=0)),
        "f1": float(f1_score(y_true, predicted, zero_division=0)),
        "roc_auc": float(roc_auc_score(y_true, probabilities)) if both else None,
        "pr_auc": float(average_precision_score(y_true, probabilities)) if both else None,
        "log_loss": float(log_loss(y_true, probabilities, labels=[0, 1])),
        "brier_score": float(brier_score_loss(y_true, probabilities)),
        "calibration_error": calibration["expected_calibration_error"],
        "prediction_distribution": {"min": float(np.min(probabilities)),
                                    "p10": float(np.quantile(probabilities, .1)),
                                    "median": float(np.median(probabilities)),
                                    "p90": float(np.quantile(probabilities, .9)),
                                    "max": float(np.max(probabilities)),
                                    "mean": float(np.mean(probabilities)),
                                    "std": float(np.std(probabilities))},
        "confusion_matrix": {"tn": int(tn), "fp": int(fp), "fn": int(fn), "tp": int(tp)},
        # Compatibility fields used by the existing model table/tests.
        "tn": int(tn), "fp": int(fp), "fn": int(fn), "tp": int(tp),
    }


def calibration_diagnostics(y_true: np.ndarray, probabilities: np.ndarray, bins: int = 10) -> dict[str, Any]:
    edges = np.linspace(0, 1, bins + 1)
    rows, ece, total = [], 0.0, len(y_true)
    for index in range(bins):
        mask = (probabilities >= edges[index]) & (probabilities < edges[index + 1] if index < bins - 1 else probabilities <= edges[index + 1])
        n = int(mask.sum())
        if not n:
            continue
        predicted, observed = float(probabilities[mask].mean()), float(y_true[mask].mean())
        ece += n / max(total, 1) * abs(predicted - observed)
        rows.append({"lower": float(edges[index]), "upper": float(edges[index + 1]), "n": n,
                     "mean_probability": predicted, "observed_rate": observed})
    return {"method": "none", "bins": rows, "expected_calibration_error": float(ece),
            "brier_score": float(brier_score_loss(y_true, probabilities)),
            "decision_thresholds": {
                str(threshold): {
                    "near_count": int(((probabilities >= threshold - .05) & (probabilities < threshold + .05)).sum()),
                    "near_observed_rate": (float(y_true[(probabilities >= threshold - .05) & (probabilities < threshold + .05)].mean())
                                           if ((probabilities >= threshold - .05) & (probabilities < threshold + .05)).any() else None),
                    "above_count": int((probabilities >= threshold).sum()),
                    "above_observed_rate": (float(y_true[probabilities >= threshold].mean())
                                            if (probabilities >= threshold).any() else None),
                } for threshold in (.55, .60, .68)},
            "note": "No calibrator is applied unless held-out validation demonstrates material improvement."}


def _baseline(y_train: np.ndarray, y_eval: np.ndarray) -> dict[str, Any]:
    rate = float(y_train.mean())
    majority = int(rate >= 0.5)
    return {"training_base_rate": rate,
            "base_rate_probability": evaluate_probabilities(y_eval, np.full(len(y_eval), rate)),
            "majority_class": {"class": majority, **evaluate_probabilities(y_eval, np.full(len(y_eval), majority, dtype=float))}}


class ModelTrainer:
    def __init__(self, target: float = 2.0, min_samples: int = 300):
        self.target, self.min_samples = target, min_samples
        self.latest: TrainingResult | None = None

    @staticmethod
    def factories(class_weight: str | None = None) -> dict[str, Callable[[], Any]]:
        factories: dict[str, Callable[[], Any]] = {
            "logistic_regression": lambda: make_pipeline(StandardScaler(), LogisticRegression(
                C=.1, max_iter=800, class_weight=class_weight, random_state=SEED)),
            "random_forest": lambda: RandomForestClassifier(
                n_estimators=100, max_depth=6, min_samples_leaf=35,
                max_features=.3, class_weight=class_weight, random_state=SEED, n_jobs=2),
            "extra_trees": lambda: ExtraTreesClassifier(
                n_estimators=120, max_depth=6, min_samples_leaf=35,
                max_features=.3, class_weight=class_weight, random_state=SEED, n_jobs=2),
            "gradient_boosting": lambda: HistGradientBoostingClassifier(
                max_iter=80, max_leaf_nodes=7, min_samples_leaf=40,
                learning_rate=.04, l2_regularization=10, random_state=SEED),
        }
        try:
            from xgboost import XGBClassifier
            factories["xgboost"] = lambda: XGBClassifier(n_estimators=90, max_depth=2, learning_rate=.04,
                subsample=.85, colsample_bytree=.7, reg_lambda=10, random_state=SEED, n_jobs=2, eval_metric="logloss")
        except ImportError:
            pass
        try:
            from lightgbm import LGBMClassifier
            factories["lightgbm"] = lambda: LGBMClassifier(n_estimators=90, max_depth=3, num_leaves=7,
                min_child_samples=40, learning_rate=.04, reg_lambda=10, random_state=SEED, n_jobs=2, verbosity=-1)
        except ImportError:
            pass
        return factories

    def train_validate(self, dataset: pd.DataFrame, progress: Callable[[str], None] | None = None) -> TrainingResult:
        if TARGET_COLUMN not in dataset or len(dataset) < self.min_samples:
            return self._empty("INSUFFICIENT_DATA", f"At least {self.min_samples} Part 4 rows with target_2x are required.", len(dataset))
        if not dataset["round_index"].is_monotonic_increasing:
            return self._empty("ERROR", "Dataset is not chronologically ordered.", len(dataset))
        feature_names, schema_hash, feature_version = feature_schema(dataset)
        prohibited = {"multiplier", "target_multiplier", "next_multiplier", "actual_multiplier", "result"}
        allowed = {item.name for item in DatasetService._build_metadata() if item.type != "categorical_sequence"}
        if (not feature_names or set(feature_names) != allowed or
                any(name in prohibited or name.startswith(("future_", "next_", "outcome_")) for name in feature_names)):
            return self._empty("INCOMPATIBLE", "Feature schema differs from the approved past-only feature definitions.", len(dataset))
        ordered = dataset.dropna(subset=feature_names + [TARGET_COLUMN]).reset_index(drop=True)
        # Do not pretend the first observed round after a collection gap was
        # immediately next. Timestamp defects are also excluded as labels.
        timestamps = pd.to_datetime(ordered["timestamp"], utc=True, errors="coerce")
        seconds = timestamps.diff().dt.total_seconds()
        index_delta = ordered["round_index"].diff()
        bad_boundary = ((seconds > MAX_HISTORY_GAP_S) | (seconds < 0) | (index_delta != 1)).fillna(False)
        bad_boundary.iloc[0] = False
        clean = ordered.loc[~bad_boundary].reset_index(drop=True)
        excluded = int(bad_boundary.sum())
        n = len(clean); train_end, selection_end, validation_end = int(n * .70), int(n * .80), int(n * .85)
        train = clean.iloc[:train_end]; selection = clean.iloc[train_end:selection_end]
        calibration_holdout = clean.iloc[selection_end:validation_end]
        validation = clean.iloc[train_end:validation_end]; test = clean.iloc[validation_end:]
        if n < self.min_samples or min(len(train), len(selection), len(calibration_holdout), len(test)) < 20:
            return self._empty("INSUFFICIENT_DATA", "Chronological splits are empty.", n)
        X_train, y_train = train[feature_names].to_numpy(float), train[TARGET_COLUMN].to_numpy(int)
        X_selection, y_selection = selection[feature_names].to_numpy(float), selection[TARGET_COLUMN].to_numpy(int)
        X_calibration, y_calibration = calibration_holdout[feature_names].to_numpy(float), calibration_holdout[TARGET_COLUMN].to_numpy(int)
        X_validation, y_validation = validation[feature_names].to_numpy(float), validation[TARGET_COLUMN].to_numpy(int)
        X_test, y_test = test[feature_names].to_numpy(float), test[TARGET_COLUMN].to_numpy(int)
        if len(np.unique(y_train)) < 2:
            return self._empty("INSUFFICIENT_DATA", "Training split contains only one class.", n)
        if not np.isfinite(clean[feature_names].to_numpy(float)).all():
            return self._empty("INCOMPATIBLE", "Features contain non-finite values.", n)
        all_y = clean[TARGET_COLUMN].to_numpy(int)
        selection_baseline = _baseline(y_train, y_selection)
        validation_baseline = _baseline(y_train, y_validation)
        test_baseline = _baseline(y_train, y_test)
        for label, start, stop, frame in (("selection", train_end, selection_end, selection_baseline),
                                          ("validation", train_end, validation_end, validation_baseline),
                                          ("test", validation_end, n, test_baseline)):
            ys = all_y[start:stop]
            frame["causal_frequency"] = evaluate_probabilities(ys, causal_frequency(all_y, start, stop))
            frame["rolling_250"] = evaluate_probabilities(ys, causal_frequency(all_y, start, stop, 250))
        class_weight = "balanced" if min(float(y_train.mean()), 1-float(y_train.mean())) < .20 else None
        if progress:
            progress("EVALUATING")
        factories = self.factories(class_weight)
        models, fitted, candidate_errors = {}, {}, {}
        for name, factory in factories.items():
            try:
                model = factory(); model.fit(X_train, y_train)
                selection_metrics = evaluate_probabilities(y_selection, model.predict_proba(X_selection)[:, 1])
                folds = self._walk_forward(clean.iloc[:train_end], feature_names, name, factories)
                if len(folds) < 3:
                    raise ValueError("fewer than three walk-forward folds completed")
                fold_scores = [-f["brier_advantage"] for f in folds]
                best_selection_baseline = min(v["brier_score"] for v in selection_baseline.values()
                                              if isinstance(v, dict) and "brier_score" in v)
                selection_advantage = best_selection_baseline - selection_metrics["brier_score"]
                fold_wins = sum(score <= -.001 for score in fold_scores)
                models[name] = {"selection": selection_metrics,
                                "validation": evaluate_probabilities(y_validation, model.predict_proba(X_validation)[:, 1]),
                                "train": evaluate_probabilities(y_train, model.predict_proba(X_train)[:, 1]),
                                "walk_forward": folds,
                                "selection_score": selection_metrics["brier_score"] + .5 * float(np.mean([f["metrics"]["brier_score"] for f in folds])) + .25 * float(np.std(fold_scores)),
                                "selection_brier_advantage": selection_advantage,
                                "folds_beating_baseline": fold_wins,
                                "passes_selection_gate": selection_advantage >= .001 and fold_wins >= 2}
                fitted[name] = model
            except Exception as exc:
                candidate_errors[name] = f"{type(exc).__name__}: {exc}"
        if not models:
            return self._empty("ERROR", f"Every candidate failed: {candidate_errors}", n)
        qualified = [name for name in models if models[name]["passes_selection_gate"]]
        # Never discard a candidate that passes the earlier validation gate
        # merely because an unqualified candidate has a lower composite score.
        pool = qualified or list(models)
        selected = min(pool, key=lambda name: (models[name]["selection_score"], models[name]["selection"]["log_loss"]))
        selected_train_model = fitted[selected]
        validation_raw = selected_train_model.predict_proba(X_selection)[:, 1]
        calibration_choice = {"method": "none", "applied": False, "material_improvement_threshold": .001}
        calibrator = None
        if len(y_selection) >= 100 and len(np.unique(y_selection)) == 2:
            raw_holdout_values = selected_train_model.predict_proba(X_calibration)[:, 1]
            raw_metrics = evaluate_probabilities(y_calibration, raw_holdout_values)
            calibration_choice["validation_holdout_raw_brier"] = raw_metrics["brier_score"]
            calibration_choice["methods"] = {"none": raw_metrics}
            candidates = {}
            sigmoid = LogisticRegression(random_state=SEED)
            sigmoid.fit(validation_raw.reshape(-1, 1), y_selection)
            candidates["sigmoid"] = (sigmoid, sigmoid.predict_proba(raw_holdout_values.reshape(-1, 1))[:, 1])
            isotonic = IsotonicRegression(out_of_bounds="clip")
            isotonic.fit(validation_raw, y_selection)
            candidates["isotonic"] = (isotonic, isotonic.predict(raw_holdout_values))
            eligible = []
            for method, (candidate, probabilities) in candidates.items():
                metrics = evaluate_probabilities(y_calibration, probabilities)
                calibration_choice["methods"][method] = metrics
                if (raw_metrics["brier_score"] - metrics["brier_score"] >= .001
                        and metrics["log_loss"] <= raw_metrics["log_loss"]
                        and metrics["calibration_error"] <= raw_metrics["calibration_error"]):
                    eligible.append((metrics["brier_score"], method, candidate))
            if eligible:
                _, method, calibrator = min(eligible)
                chosen_metrics = calibration_choice["methods"][method]
                calibration_choice |= {"method": method, "applied": True,
                                       "validation_holdout_calibrated_brier": chosen_metrics["brier_score"],
                                       "validation_holdout_improvement": raw_metrics["brier_score"] - chosen_metrics["brier_score"]}
            else:
                calibration_choice |= {"validation_holdout_calibrated_brier": raw_metrics["brier_score"],
                                       "validation_holdout_improvement": 0.0}
        # Keep the fitted estimator used to select the calibrator. Refitting on
        # validation would change its probability distribution after calibration.
        final_model = selected_train_model
        if calibrator is not None:
            final_model = ProbabilityCalibratedModel(final_model, calibrator)
        test_probability = final_model.predict_proba(X_test)[:, 1]
        test_metrics = evaluate_probabilities(y_test, test_probability)
        validation_metrics = models[selected]["validation"]
        best_selection_baseline = min(x["brier_score"] for k,x in selection_baseline.items() if isinstance(x,dict) and "brier_score" in x)
        best_test_baseline = min(x["brier_score"] for k,x in test_baseline.items() if isinstance(x,dict) and "brier_score" in x)
        selection_advantage = best_selection_baseline - models[selected]["selection"]["brier_score"]
        test_advantage = best_test_baseline - test_metrics["brier_score"]
        best_test_name = min((key for key, value in test_baseline.items() if isinstance(value, dict) and "brier_score" in value),
                             key=lambda name: test_baseline[name]["brier_score"])
        frozen_p = np.full(len(y_test), float(y_train.mean()))
        baseline_probabilities = {"base_rate_probability": frozen_p,
                                  "majority_class": np.full(len(y_test), int(float(y_train.mean()) >= .5)),
                                  "causal_frequency": causal_frequency(all_y, validation_end, n),
                                  "rolling_250": causal_frequency(all_y, validation_end, n, 250)}
        uncertainty = block_bootstrap_brier_advantage(y_test, test_probability, frozen_p)
        best_uncertainty = block_bootstrap_brier_advantage(
            y_test, test_probability, baseline_probabilities[best_test_name], baseline_name=best_test_name)
        fold_count = len(models[selected]["walk_forward"])
        fold_wins = models[selected]["folds_beating_baseline"]
        robust = bool(qualified) and models[selected]["passes_selection_gate"] and fold_count >= 3
        # A sealed chronological test is a final *veto*, not a ranking set.
        deployable = (robust and test_advantage >= .001 and uncertainty["ci95"][0] > 0
                      and best_uncertainty["ci95"][0] > 0)
        rejection_reasons = []
        if selection_advantage < .001:
            rejection_reasons.append("selection_brier_advantage_below_0.001")
        if fold_wins < 2:
            rejection_reasons.append("fewer_than_2_of_3_walk_forward_folds_beat_base_rate_by_0.001")
        if test_advantage < .001:
            rejection_reasons.append("test_brier_advantage_below_0.001")
        if uncertainty["ci95"][0] <= 0:
            rejection_reasons.append("base_rate_test_bootstrap_lower_bound_not_positive")
        if best_uncertainty["ci95"][0] <= 0:
            rejection_reasons.append("best_baseline_test_bootstrap_lower_bound_not_positive")
        walk_forward = models[selected]["walk_forward"]
        test_periods = []
        for start, stop in zip(np.linspace(0, len(test), 4, dtype=int)[:-1],
                               np.linspace(0, len(test), 4, dtype=int)[1:]):
            segment = test.iloc[start:stop]
            test_periods.append({"start": str(segment.iloc[0]["timestamp"]),
                                 "end": str(segment.iloc[-1]["timestamp"]), "n": len(segment),
                                 "model": evaluate_probabilities(y_test[start:stop], test_probability[start:stop]),
                                 "baseline": evaluate_probabilities(y_test[start:stop],
                                                                    baseline_probabilities[best_test_name][start:stop])})
        version_seed = f"{datetime.now(timezone.utc).isoformat()}|{schema_hash}|{selected}"
        version = f"ml-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{hashlib.sha256(version_seed.encode()).hexdigest()[:8]}"
        distribution = {"total": n, "positive": int(clean[TARGET_COLUMN].sum()), "negative": int(n-clean[TARGET_COLUMN].sum()),
                        "positive_rate": float(clean[TARGET_COLUMN].mean()), "negative_rate": float(1-clean[TARGET_COLUMN].mean()),
                        "splits": {label: {"n": len(frame), "positive_rate": float(frame[TARGET_COLUMN].mean())}
                                   for label, frame in (("train", train), ("selection", selection),
                                                        ("calibration", calibration_holdout), ("test", test))}}
        gap = validation_metrics["brier_score"] - models[selected]["train"]["brier_score"]
        calibration = calibration_diagnostics(y_test, test_probability)
        calibration.update(calibration_choice)
        calibration["selection_data"] = "70-80% selection; 80-85% calibration holdout; 85-100% untouched test"
        dataset_hash = hashlib.sha256(pd.util.hash_pandas_object(clean[["round_id", "timestamp", TARGET_COLUMN] + feature_names], index=False).values.tobytes()).hexdigest()
        importance = self._feature_importance(selected_train_model, feature_names, X_selection, y_selection)
        selection_reason = (f"{selected} had the lowest selection/walk-forward score among "
                            f"{len(qualified)} pre-test qualified candidates" if qualified else
                            f"No candidate qualified before the test; {selected} is reported for diagnostics only")
        selection_reason += ("; "
                            f"selection Brier advantage {selection_advantage:+.6f}, "
                            f"{fold_wins}/{fold_count} earlier folds beat frequency, "
                            f"untouched test Brier advantage {test_advantage:+.6f} "
                            f"versus {best_test_name}; its block-bootstrap lower bound was "
                            f"{best_uncertainty['ci95'][0]:+.6f}.")
        if rejection_reasons:
            selection_reason += " Rejection gates: " + ", ".join(rejection_reasons) + "."
        drift = {}
        for label, frame in (("selection", selection), ("calibration", calibration_holdout), ("test", test)):
            shift = ((frame[feature_names].mean() - train[feature_names].mean()).abs()
                     / train[feature_names].std().replace(0, np.nan)).replace([np.inf, -np.inf], np.nan).dropna()
            drift[label] = {"base_rate_shift": float(frame[TARGET_COLUMN].mean() - train[TARGET_COLUMN].mean()),
                            "largest_standardized_feature_shifts": {key: float(value) for key, value in shift.nlargest(10).items()}}
        result = TrainingResult(
            deployable, "READY" if deployable else "NOT_VALIDATED", selection_reason +
            (" Validated for inference." if deployable else " Live ML inference disabled; historical frequency remains an informational fallback."),
            n, models, {"validated": deployable, "weights": {selected: 1.0}, "feature_columns": feature_names},
            {"selection": selection_baseline, "validation": validation_baseline, "test": test_baseline, "historical_base_rate": distribution["positive_rate"],
             "base_rate": test_baseline["base_rate_probability"]},
            calibration, datetime.now(timezone.utc).isoformat(),
            version, feature_version, feature_names, schema_hash, "next_round_ge_2x", selected,
            {key: value if isinstance(value, (str, int, float, bool, type(None))) else repr(value)
             for key, value in (final_model.model if isinstance(final_model, ProbabilityCalibratedModel) else final_model).get_params(deep=False).items()}, distribution,
            {"train": len(train), "validation": len(validation), "test": len(test)},
            {"start": str(clean.iloc[0]["timestamp"]), "end": str(clean.iloc[-1]["timestamp"])},
            validation_metrics, test_metrics, walk_forward,
            {"train_validation_brier_gap": gap,
             "validation_test_brier_degradation": test_metrics["brier_score"] - validation_metrics["brier_score"],
             "suspicious_perfect_metrics": any(metric["validation"]["accuracy"] >= .999 for metric in models.values()),
             "outperforms_validation_baseline": selection_advantage >= .001,
             "outperforms_test_baseline": test_advantage >= .001,
             "folds_beating_baseline": fold_wins, "fold_count": fold_count,
             "candidate_errors": candidate_errors,
             "test_brier_advantage_uncertainty": uncertainty,
             "best_baseline_test_uncertainty": best_uncertainty,
             "best_test_baseline": best_test_name,
             "qualified_candidates_before_test": qualified,
             "rejection_reasons": rejection_reasons,
             "test_periods": test_periods,
             "deployable": deployable,
             "walk_forward_brier_std": float(np.std([fold["metrics"]["brier_score"] for fold in walk_forward])) if walk_forward else None},
            dataset_hash, importance, {"excluded_gap_or_clock_rows": excluded, "class_weight": class_weight or "none",
                                       "max_history_gap_s": MAX_HISTORY_GAP_S, "drift": drift,
                                       "removed_deterministic_features": ["rolling_count_from_rate", "variance_from_std", "range_from_extrema"]}, selection_reason)
        result._model = final_model  # type: ignore[attr-defined]
        result._models = {selected: final_model}  # type: ignore[attr-defined]
        self.latest = result
        return result

    @staticmethod
    def _feature_importance(model, names: list[str], X_selection: np.ndarray,
                            y_selection: np.ndarray) -> list[dict[str, Any]]:
        if hasattr(model, "feature_importances_"):
            values = model.feature_importances_
            method = "training_impurity_importance"
        elif hasattr(model, "named_steps") and hasattr(model[-1], "coef_"):
            values = np.abs(model[-1].coef_[0])
            method = "absolute_scaled_training_coefficient"
        else:
            # HistGradientBoosting has no native importance. Only the earlier
            # selection period may be used here, never the sealed final test.
            values = permutation_importance(model, X_selection, y_selection,
                                            scoring="neg_brier_score", n_repeats=2,
                                            max_samples=min(500, len(y_selection)),
                                            random_state=SEED, n_jobs=1).importances_mean
            method = "selection_holdout_permutation_brier"
        return [{"feature": names[i], "importance": float(values[i]), "method": method}
                for i in np.argsort(values)[::-1][:20]]

    def _walk_forward(self, data: pd.DataFrame, feature_names: list[str], algorithm: str,
                      factories: dict[str, Callable[[], Any]] | None = None) -> list[dict]:
        results, n = [], len(data)
        for end in (int(n * .55), int(n * .70), int(n * .85)):
            stop = min(end + int(n * .15), n)
            train, evaluate = data.iloc[:end], data.iloc[end:stop]
            if len(evaluate) == 0 or train[TARGET_COLUMN].nunique() < 2:
                continue
            model = (factories or self.factories())[algorithm](); model.fit(train[feature_names], train[TARGET_COLUMN])
            metrics = evaluate_probabilities(evaluate[TARGET_COLUMN].to_numpy(int), model.predict_proba(evaluate[feature_names])[:, 1])
            baseline = evaluate_probabilities(evaluate[TARGET_COLUMN].to_numpy(int), np.full(len(evaluate), float(train[TARGET_COLUMN].mean())))
            probabilities = model.predict_proba(evaluate[feature_names])[:, 1]
            frozen = np.full(len(evaluate), float(train[TARGET_COLUMN].mean()))
            results.append({"train_end": end, "evaluation_start": end, "evaluation_end": stop,
                            "metrics": metrics, "baseline": baseline, "baseline_name": "base_rate_probability",
                            "brier_advantage": baseline["brier_score"] - metrics["brier_score"],
                            "brier_advantage_uncertainty": block_bootstrap_brier_advantage(
                                evaluate[TARGET_COLUMN].to_numpy(int), probabilities, frozen,
                                baseline_name="base_rate_probability")})
        return results

    def _empty(self, status: str, message: str, size: int) -> TrainingResult:
        return TrainingResult(False, status, message, size, {}, {}, {}, {}, datetime.now(timezone.utc).isoformat())
