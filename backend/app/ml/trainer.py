"""Chronological, leakage-safe binary model training on Part 4 features."""
from __future__ import annotations

import hashlib
import json
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
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
from threadpoolctl import threadpool_limits
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
    cycle_id: str | None = None
    ranking: list[str] | None = None
    model_concurrency: int = 1
    training_time_seconds: float | None = None
    cycle_time_seconds: float | None = None

    def model_dump(self) -> dict[str, Any]:
        return {key: value for key, value in self.__dict__.items() if not key.startswith("_")}


def eligible_round_ids(rounds: pd.DataFrame, required_prior_rounds: int = 100) -> set[str]:
    """Admit targets only after a complete observed prior window."""
    if rounds.empty:
        return set()
    ordered = rounds.sort_values("round_index", kind="stable").reset_index(drop=True)
    times = pd.to_datetime(ordered["timestamp"], format="mixed", utc=True, errors="coerce")
    seconds = times.diff().dt.total_seconds()
    adjacent = (ordered["round_index"].diff().eq(1) & seconds.between(0, MAX_HISTORY_GAP_S)).fillna(False)
    episode = (~adjacent).cumsum()
    prior_observed = ordered.groupby(episode, sort=False).cumcount()
    return set(ordered.loc[prior_observed >= required_prior_rounds, "round_id"].astype(str))


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
    probabilities = np.asarray(probabilities, dtype=float)
    if (probabilities.ndim != 1 or len(probabilities) != len(y_true)
            or not np.isfinite(probabilities).all() or ((probabilities < 0) | (probabilities > 1)).any()):
        raise ValueError("Model probabilities must be finite, aligned and within [0, 1]")
    probabilities = np.clip(probabilities, 1e-8, 1 - 1e-8)
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
    def __init__(self, target: float = 2.0, min_samples: int = 300, model_concurrency: int = 1):
        self.target, self.min_samples = target, min_samples
        if not 1 <= model_concurrency <= 4:
            raise ValueError("model_concurrency must be between 1 and 4")
        self.model_concurrency = model_concurrency
        self.latest: TrainingResult | None = None

    @staticmethod
    def factories(class_weight: str | None = None) -> dict[str, Callable[[], Any]]:
        factories: dict[str, Callable[[], Any]] = {
            "logistic_regression": lambda: make_pipeline(StandardScaler(), LogisticRegression(
                C=.1, max_iter=800, class_weight=class_weight, random_state=SEED)),
            "random_forest": lambda: RandomForestClassifier(
                n_estimators=100, max_depth=6, min_samples_leaf=35,
                max_features=.3, class_weight=class_weight, random_state=SEED, n_jobs=1),
            "extra_trees": lambda: ExtraTreesClassifier(
                n_estimators=120, max_depth=6, min_samples_leaf=35,
                max_features=.3, class_weight=class_weight, random_state=SEED, n_jobs=1),
            "gradient_boosting": lambda: HistGradientBoostingClassifier(
                max_iter=80, max_leaf_nodes=7, min_samples_leaf=40,
                learning_rate=.04, l2_regularization=10, random_state=SEED, early_stopping=False),
        }
        try:
            from xgboost import XGBClassifier
            factories["xgboost"] = lambda: XGBClassifier(n_estimators=90, max_depth=2, learning_rate=.04,
                subsample=.85, colsample_bytree=.7, reg_lambda=10, random_state=SEED, n_jobs=1, eval_metric="logloss")
        except ImportError:
            factories["xgboost"] = lambda: ModelTrainer._unavailable("xgboost")
        try:
            from lightgbm import LGBMClassifier
            factories["lightgbm"] = lambda: LGBMClassifier(n_estimators=90, max_depth=3, num_leaves=7,
                min_child_samples=40, learning_rate=.04, reg_lambda=10, random_state=SEED, n_jobs=1, verbosity=-1)
        except ImportError:
            factories["lightgbm"] = lambda: ModelTrainer._unavailable("lightgbm")
        return factories

    @staticmethod
    def _unavailable(dependency):
        raise ImportError(f"optional model dependency unavailable: {dependency}")

    @staticmethod
    def rank_candidates(models):
        """Freeze ranking before final verification; never read test metrics."""
        return sorted((name for name, row in models.items() if "selection_score" in row),
                      key=lambda name: (not models[name]["passes_selection_gate"],
                                        models[name]["selection_score"],
                                        models[name]["selection"]["log_loss"], name))

    @threadpool_limits.wrap(limits=2)
    def train_validate(self, dataset: pd.DataFrame, progress: Callable[[str], None] | None = None,
                       *, rounds: pd.DataFrame | None = None) -> TrainingResult:
        cycle_started = time.perf_counter()
        cycle_id = f"cycle-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:8]}"
        if TARGET_COLUMN not in dataset or len(dataset) < self.min_samples:
            return self._empty("INSUFFICIENT_DATA", f"At least {self.min_samples} Part 4 rows with target_2x are required.", len(dataset))
        if (not dataset["round_index"].is_monotonic_increasing or dataset["round_index"].duplicated().any()
                or dataset["round_id"].duplicated().any()):
            return self._empty("ERROR", "Dataset is not chronologically ordered.", len(dataset))
        if not dataset[TARGET_COLUMN].isin([0, 1]).all():
            return self._empty("INCOMPATIBLE", "Only binary target_2x labels are allowed.", len(dataset))
        feature_names, schema_hash, feature_version = feature_schema(dataset)
        prohibited = {"multiplier", "target_multiplier", "next_multiplier", "actual_multiplier", "result"}
        allowed = {item.name for item in DatasetService._build_metadata() if item.type != "categorical_sequence"}
        if (not feature_names or set(feature_names) != allowed or
                any(name in prohibited or name.startswith(("future_", "next_", "outcome_")) for name in feature_names)):
            return self._empty("INCOMPATIBLE", "Feature schema differs from the approved past-only feature definitions.", len(dataset))
        ordered = dataset.dropna(subset=feature_names + [TARGET_COLUMN]).reset_index(drop=True)
        if rounds is not None:
            admitted = eligible_round_ids(rounds)
            clean = ordered.loc[ordered["round_id"].astype(str).isin(admitted)].reset_index(drop=True)
            excluded = len(ordered) - len(clean)
        else:
            # Without the full round history, at least exclude broken target
            # boundaries. Production passes the full PostgreSQL snapshot above.
            timestamps = pd.to_datetime(ordered["timestamp"], format="mixed", utc=True, errors="coerce")
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
        if len(np.unique(y_train)) < 2:
            return self._empty("INSUFFICIENT_DATA", "Training split contains only one class.", n)
        if not np.isfinite(clean[feature_names].to_numpy(float)).all():
            return self._empty("INCOMPATIBLE", "Features contain non-finite values.", n)
        all_y = clean[TARGET_COLUMN].to_numpy(int)
        selection_baseline = _baseline(y_train, y_selection)
        validation_baseline = _baseline(y_train, y_validation)
        for label, start, stop, frame in (("selection", train_end, selection_end, selection_baseline),
                                          ("validation", train_end, validation_end, validation_baseline)):
            ys = all_y[start:stop]
            frame["causal_frequency"] = evaluate_probabilities(ys, causal_frequency(all_y, start, stop))
            frame["rolling_250"] = evaluate_probabilities(ys, causal_frequency(all_y, start, stop, 250))
        class_weight = "balanced" if min(float(y_train.mean()), 1-float(y_train.mean())) < .20 else None
        if progress:
            progress("EVALUATING")
        factories = self.factories(class_weight)
        models, fitted, candidate_errors = {}, {}, {}
        # Each worker sees exactly the same immutable snapshot and boundaries.
        def evaluate_candidate(item):
            name, factory = item
            started = time.perf_counter()
            model_id = f"ml-{cycle_id.removeprefix('cycle-')}-{name}"
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
                row = {"model_id": model_id, "algorithm": name, "status": "EVALUATED",
                                "hyperparameters": {key: value if isinstance(value, (str, int, float, bool, type(None))) else repr(value)
                                                    for key, value in model.get_params(deep=False).items()},
                                "selection": selection_metrics,
                                "validation": evaluate_probabilities(y_validation, model.predict_proba(X_validation)[:, 1]),
                                "train": evaluate_probabilities(y_train, model.predict_proba(X_train)[:, 1]),
                                "walk_forward": folds,
                                "selection_score": selection_metrics["brier_score"] + .5 * float(np.mean([f["metrics"]["brier_score"] for f in folds])) + .25 * float(np.std(fold_scores)),
                                "selection_brier_advantage": selection_advantage,
                                "folds_beating_baseline": fold_wins,
                                "passes_selection_gate": selection_advantage >= .001 and fold_wins >= 2,
                                "training_time_seconds": time.perf_counter() - started}
                return name, row, model
            except Exception as exc:
                reason = f"{type(exc).__name__}: {exc}"
                return name, {"model_id": model_id, "algorithm": name,
                              "status": "UNAVAILABLE" if isinstance(exc, ImportError) else "FAILED",
                              "rank": None, "training_time_seconds": time.perf_counter() - started,
                              "passes_selection_gate": False, "deployable": False,
                              "walk_forward": [], "test": None, "rejection_reasons": [reason]}, None
        # OpenMP's limit is thread-local; initialize each worker as well as the
        # surrounding process-wide BLAS context, restored after workers join.
        with ThreadPoolExecutor(max_workers=self.model_concurrency, thread_name_prefix="ml-candidate",
                                initializer=lambda: threadpool_limits(limits=2)) as pool:
            for name, row, model in pool.map(evaluate_candidate, factories.items()):
                models[name] = row
                if model is not None:
                    fitted[name] = model
                else:
                    candidate_errors[name] = row["rejection_reasons"][0]
        ranking = self.rank_candidates(models)
        if not ranking:
            result = self._empty("NOT_DEPLOYABLE", f"Every candidate failed: {candidate_errors}", n)
            result.models, result.cycle_id, result.ranking = models, cycle_id, []
            result.model_concurrency = self.model_concurrency
            result.cycle_time_seconds = time.perf_counter() - cycle_started
            self.latest = result
            return result
        qualified = [name for name in ranking if models[name]["passes_selection_gate"]]
        # Never discard a candidate that passes the earlier validation gate
        # merely because an unqualified candidate has a lower composite score.
        selected = ranking[0]
        # Seal the order before any final-test probability is requested. A veto
        # of rank 1 must not turn this test set into a search over runners-up.
        for rank, name in enumerate(ranking, 1):
            models[name]["rank"] = rank
        if progress:
            progress("VERIFYING")
        X_test, y_test = test[feature_names].to_numpy(float), test[TARGET_COLUMN].to_numpy(int)
        test_baseline = _baseline(y_train, y_test)
        test_baseline["causal_frequency"] = evaluate_probabilities(y_test, causal_frequency(all_y, validation_end, n))
        test_baseline["rolling_250"] = evaluate_probabilities(y_test, causal_frequency(all_y, validation_end, n, 250))
        verified = {}
        for name in ranking:
            started = time.perf_counter()
            try:
                model, report = self._verify_candidate(
                    fitted[name], models[name], X_selection, y_selection, X_calibration, y_calibration,
                    X_test, y_test, test, y_train, all_y, validation_end, test_baseline)
                models[name].update(report)
                verified[name] = model
            except Exception as exc:
                models[name].update(status="FAILED", deployable=False, test=None,
                                    rejection_reasons=[f"final_verification_failed: {type(exc).__name__}: {exc}"])
            models[name]["training_time_seconds"] += time.perf_counter() - started
            models[name]["selected"] = name == selected
        winner = models[selected]
        if selected not in verified:
            result = self._empty("NOT_DEPLOYABLE", "; ".join(winner["rejection_reasons"]), n)
            result.models, result.cycle_id, result.ranking = models, cycle_id, ranking
            result.algorithm, result.model_version = selected, winner["model_id"]
            result.feature_names, result.feature_schema_hash, result.feature_version = feature_names, schema_hash, feature_version
            result.model_concurrency = self.model_concurrency
            result.cycle_time_seconds = time.perf_counter() - cycle_started
            result._models = verified
            self.latest = result
            return result
        selected_train_model = fitted[selected]
        final_model = verified[selected]
        test_metrics = winner["test"]
        validation_metrics = winner["validation"]
        selection_advantage = winner["selection_brier_advantage"]
        test_advantage = winner["test_brier_advantage"]
        best_test_name = winner["overfitting_checks"]["best_test_baseline"]
        best_uncertainty = winner["overfitting_checks"]["best_baseline_test_uncertainty"]
        fold_count, fold_wins = len(winner["walk_forward"]), winner["folds_beating_baseline"]
        deployable, rejection_reasons = winner["deployable"], winner["rejection_reasons"]
        walk_forward = winner["walk_forward"]
        version = winner["model_id"]
        distribution = {"total": n, "positive": int(clean[TARGET_COLUMN].sum()), "negative": int(n-clean[TARGET_COLUMN].sum()),
                        "positive_rate": float(clean[TARGET_COLUMN].mean()), "negative_rate": float(1-clean[TARGET_COLUMN].mean()),
                        "splits": {label: {"n": len(frame), "positive_rate": float(frame[TARGET_COLUMN].mean())}
                                   for label, frame in (("train", train), ("selection", selection),
                                                        ("calibration", calibration_holdout), ("test", test))}}
        dataset_hash = hashlib.sha256(pd.util.hash_pandas_object(clean[["round_id", "timestamp", TARGET_COLUMN] + feature_names], index=False).values.tobytes()).hexdigest()
        importance = self._feature_importance(selected_train_model, feature_names, X_selection, y_selection)
        selection_reason = (f"{selected} ranked first using selection/walk-forward validation only among "
                            f"{len(qualified)} pre-test qualified candidates; "
                            f"selection Brier advantage {selection_advantage:+.6f}, "
                            f"{fold_wins}/{fold_count} earlier folds beat frequency, "
                            f"untouched test Brier advantage {test_advantage:+.6f} "
                            f"versus {best_test_name}; bootstrap lower bound {best_uncertainty['ci95'][0]:+.6f}.")
        if rejection_reasons:
            selection_reason += " Rejection gates: " + ", ".join(rejection_reasons) + "."
        drift = {}
        for label, frame in (("selection", selection), ("calibration", calibration_holdout), ("test", test)):
            shift = ((frame[feature_names].mean() - train[feature_names].mean()).abs()
                     / train[feature_names].std().replace(0, np.nan)).replace([np.inf, -np.inf], np.nan).dropna()
            drift[label] = {"base_rate_shift": float(frame[TARGET_COLUMN].mean() - train[TARGET_COLUMN].mean()),
                            "largest_standardized_feature_shifts": {key: float(value) for key, value in shift.nlargest(10).items()}}
        result = TrainingResult(
            deployable, "READY" if deployable else "NOT_DEPLOYABLE", selection_reason,
            n, models, {"validated": deployable, "weights": {selected: 1.0}, "feature_columns": feature_names},
            {"selection": selection_baseline, "validation": validation_baseline, "test": test_baseline,
             "historical_base_rate": distribution["positive_rate"], "base_rate": test_baseline["base_rate_probability"]},
            winner["calibration"], datetime.now(timezone.utc).isoformat(),
            version, feature_version, feature_names, schema_hash, "next_round_ge_2x", selected,
            {key: value if isinstance(value, (str, int, float, bool, type(None))) else repr(value)
             for key, value in selected_train_model.get_params(deep=False).items()}, distribution,
            {"train": len(train), "validation": len(validation), "test": len(test)},
            {"start": str(clean.iloc[0]["timestamp"]), "end": str(clean.iloc[-1]["timestamp"])},
            validation_metrics, test_metrics, walk_forward,
            {**winner["overfitting_checks"], "candidate_errors": candidate_errors,
             "qualified_candidates_before_test": qualified},
            dataset_hash, importance, {"excluded_gap_or_clock_rows": excluded,
                                       "required_prior_contiguous_rounds": 100 if rounds is not None else None,
                                       "class_weight": class_weight or "none", "max_history_gap_s": MAX_HISTORY_GAP_S,
                                       "drift": drift,
                                       "removed_deterministic_features": ["rolling_count_from_rate", "variance_from_std", "range_from_extrema"]}, selection_reason)
        result.cycle_id, result.ranking, result.model_concurrency = cycle_id, ranking, self.model_concurrency
        result.training_time_seconds = winner["training_time_seconds"]
        result.cycle_time_seconds = time.perf_counter() - cycle_started
        result._model, result._models = final_model, verified
        self.latest = result
        return result

    @staticmethod
    def _verify_candidate(selected_train_model, row, X_selection, y_selection, X_calibration, y_calibration,
                          X_test, y_test, test, y_train, all_y, validation_end, test_baseline):
        """Fit calibration using validation only, then open this candidate's test once."""
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
        validation_metrics = row["validation"]
        best_test_baseline = min(x["brier_score"] for k,x in test_baseline.items() if isinstance(x,dict) and "brier_score" in x)
        selection_advantage = row["selection_brier_advantage"]
        test_advantage = best_test_baseline - test_metrics["brier_score"]
        best_test_name = min((key for key, value in test_baseline.items() if isinstance(value, dict) and "brier_score" in value),
                             key=lambda name: test_baseline[name]["brier_score"])
        frozen_p = np.full(len(y_test), float(y_train.mean()))
        baseline_probabilities = {"base_rate_probability": frozen_p,
                                  "majority_class": np.full(len(y_test), int(float(y_train.mean()) >= .5)),
                                  "causal_frequency": causal_frequency(all_y, validation_end, len(all_y)),
                                  "rolling_250": causal_frequency(all_y, validation_end, len(all_y), 250)}
        uncertainty = block_bootstrap_brier_advantage(y_test, test_probability, frozen_p)
        best_uncertainty = block_bootstrap_brier_advantage(
            y_test, test_probability, baseline_probabilities[best_test_name], baseline_name=best_test_name)
        fold_count = len(row["walk_forward"])
        fold_wins = row["folds_beating_baseline"]
        robust = row["passes_selection_gate"] and fold_count >= 3
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
        walk_forward = row["walk_forward"]
        test_periods = []
        for start, stop in zip(np.linspace(0, len(test), 4, dtype=int)[:-1],
                               np.linspace(0, len(test), 4, dtype=int)[1:]):
            segment = test.iloc[start:stop]
            test_periods.append({"start": str(segment.iloc[0]["timestamp"]),
                                 "end": str(segment.iloc[-1]["timestamp"]), "n": len(segment),
                                 "model": evaluate_probabilities(y_test[start:stop], test_probability[start:stop]),
                                 "baseline": evaluate_probabilities(y_test[start:stop],
                                                                    baseline_probabilities[best_test_name][start:stop])})
        gap = validation_metrics["brier_score"] - row["train"]["brier_score"]
        calibration = calibration_diagnostics(y_test, test_probability)
        calibration.update(calibration_choice)
        calibration["selection_data"] = "70-80% selection; 80-85% calibration holdout; 85-100% untouched test"
        return final_model, {"status": "PASSED" if deployable else "REJECTED",
            "test": test_metrics, "test_brier_advantage": test_advantage,
            "calibration": calibration, "deployable": deployable, "rejection_reasons": rejection_reasons,
            "overfitting_checks": {"train_validation_brier_gap": gap,
             "validation_test_brier_degradation": test_metrics["brier_score"] - validation_metrics["brier_score"],
             "suspicious_perfect_metrics": validation_metrics["accuracy"] >= .999,
             "outperforms_validation_baseline": selection_advantage >= .001,
             "outperforms_test_baseline": test_advantage >= .001,
             "folds_beating_baseline": fold_wins, "fold_count": fold_count,
             "test_brier_advantage_uncertainty": uncertainty,
             "best_baseline_test_uncertainty": best_uncertainty,
             "best_test_baseline": best_test_name,
             "rejection_reasons": rejection_reasons,
             "test_periods": test_periods,
             "deployable": deployable,
             "walk_forward_brier_std": float(np.std([fold["metrics"]["brier_score"] for fold in walk_forward]))}}

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
            model = (factories or self.factories())[algorithm]()
            # Imbalance decisions for a fold must not see later training labels.
            weight = "balanced" if min(float(train[TARGET_COLUMN].mean()), 1 - float(train[TARGET_COLUMN].mean())) < .20 else None
            parameters = model.get_params(deep=True)
            weights = {key: weight for key in parameters if key == "class_weight" or key.endswith("__class_weight")}
            if weights:
                model.set_params(**weights)
            model.fit(train[feature_names], train[TARGET_COLUMN])
            probabilities = model.predict_proba(evaluate[feature_names])[:, 1]
            metrics = evaluate_probabilities(evaluate[TARGET_COLUMN].to_numpy(int), probabilities)
            baseline = evaluate_probabilities(evaluate[TARGET_COLUMN].to_numpy(int), np.full(len(evaluate), float(train[TARGET_COLUMN].mean())))
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
