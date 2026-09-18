"""Chronological, leakage-safe binary model training on Part 4 features."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

import numpy as np
import pandas as pd
from sklearn.ensemble import GradientBoostingClassifier, RandomForestClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import (accuracy_score, average_precision_score, brier_score_loss,
                             confusion_matrix, f1_score, log_loss, precision_score,
                             recall_score, roc_auc_score)
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

TARGET_COLUMN = "target_2x"
IDENTITY_COLUMNS = {"round_id", "round_index", "timestamp", TARGET_COLUMN, "sequence_last_10"}
FEATURE_VERSION_PREFIX = "part4"
SEED = 42


class ProbabilityCalibratedModel:
    """Serializable wrapper applying a validation-fitted sigmoid mapping."""
    def __init__(self, model, calibrator):
        self.model, self.calibrator = model, calibrator

    def predict_proba(self, features):
        raw = self.model.predict_proba(features)[:, 1]
        calibrated = self.calibrator.predict_proba(raw.reshape(-1, 1))[:, 1]
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

    def model_dump(self) -> dict[str, Any]:
        return {key: value for key, value in self.__dict__.items() if not key.startswith("_")}


def feature_schema(dataset: pd.DataFrame) -> tuple[list[str], str, str]:
    names = [name for name in dataset.columns if name not in IDENTITY_COLUMNS and pd.api.types.is_numeric_dtype(dataset[name])]
    payload = json.dumps([(name, str(dataset[name].dtype)) for name in names], separators=(",", ":"))
    digest = hashlib.sha256(payload.encode()).hexdigest()
    return names, digest, f"{FEATURE_VERSION_PREFIX}-{digest[:12]}"


def evaluate_probabilities(y_true: np.ndarray, probabilities: np.ndarray) -> dict[str, Any]:
    probabilities = np.clip(np.asarray(probabilities, dtype=float), 1e-8, 1 - 1e-8)
    y_true = np.asarray(y_true, dtype=int)
    predicted = (probabilities >= 0.5).astype(int)
    tn, fp, fn, tp = confusion_matrix(y_true, predicted, labels=[0, 1]).ravel()
    both = len(np.unique(y_true)) == 2
    return {
        "accuracy": float(accuracy_score(y_true, predicted)),
        "precision": float(precision_score(y_true, predicted, zero_division=0)),
        "recall": float(recall_score(y_true, predicted, zero_division=0)),
        "f1": float(f1_score(y_true, predicted, zero_division=0)),
        "roc_auc": float(roc_auc_score(y_true, probabilities)) if both else None,
        "pr_auc": float(average_precision_score(y_true, probabilities)) if both else None,
        "log_loss": float(log_loss(y_true, probabilities, labels=[0, 1])),
        "brier_score": float(brier_score_loss(y_true, probabilities)),
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
    def factories() -> dict[str, Callable[[], Any]]:
        return {
            "logistic_regression": lambda: make_pipeline(StandardScaler(), LogisticRegression(max_iter=1500, random_state=SEED)),
            "random_forest": lambda: RandomForestClassifier(
                n_estimators=180, max_depth=6, min_samples_leaf=30,
                max_features=.30, random_state=SEED, n_jobs=-1,
            ),
            "gradient_boosting": lambda: GradientBoostingClassifier(n_estimators=100, learning_rate=.05, max_depth=2, random_state=SEED),
        }

    def train_validate(self, dataset: pd.DataFrame) -> TrainingResult:
        if TARGET_COLUMN not in dataset or len(dataset) < self.min_samples:
            return self._empty("INSUFFICIENT_DATA", f"At least {self.min_samples} Part 4 rows with target_2x are required.", len(dataset))
        if not dataset["round_index"].is_monotonic_increasing:
            return self._empty("ERROR", "Dataset is not chronologically ordered.", len(dataset))
        feature_names, schema_hash, feature_version = feature_schema(dataset)
        if not feature_names or any(name in {"multiplier", "target_multiplier", "next_multiplier"} for name in feature_names):
            return self._empty("INCOMPATIBLE", "Feature schema is empty or contains a prohibited target/outcome field.", len(dataset))
        clean = dataset.dropna(subset=feature_names + [TARGET_COLUMN]).reset_index(drop=True)
        n = len(clean); train_end, validation_end = int(n * .70), int(n * .85)
        train, validation, test = clean.iloc[:train_end], clean.iloc[train_end:validation_end], clean.iloc[validation_end:]
        if min(len(train), len(validation), len(test)) == 0:
            return self._empty("INSUFFICIENT_DATA", "Chronological splits are empty.", n)
        X_train, y_train = train[feature_names].to_numpy(float), train[TARGET_COLUMN].to_numpy(int)
        X_validation, y_validation = validation[feature_names].to_numpy(float), validation[TARGET_COLUMN].to_numpy(int)
        X_test, y_test = test[feature_names].to_numpy(float), test[TARGET_COLUMN].to_numpy(int)
        if len(np.unique(y_train)) < 2:
            return self._empty("INSUFFICIENT_DATA", "Training split contains only one class.", n)

        models, fitted = {}, {}
        for name, factory in self.factories().items():
            model = factory(); model.fit(X_train, y_train)
            models[name] = {"validation": evaluate_probabilities(y_validation, model.predict_proba(X_validation)[:, 1]),
                            "train": evaluate_probabilities(y_train, model.predict_proba(X_train)[:, 1])}
            fitted[name] = model
        selected = min(models, key=lambda name: (models[name]["validation"]["brier_score"], models[name]["validation"]["log_loss"]))
        # Calibration selection uses validation only. The final test remains
        # untouched until algorithm and calibration choices are fixed.
        selected_train_model = fitted[selected]
        validation_raw = selected_train_model.predict_proba(X_validation)[:, 1]
        calibration_choice = {"method": "none", "applied": False, "material_improvement_threshold": .001}
        calibrator = None
        midpoint = len(y_validation) // 2
        if midpoint >= 30 and len(np.unique(y_validation[:midpoint])) == 2:
            candidate = LogisticRegression(random_state=SEED)
            candidate.fit(validation_raw[:midpoint].reshape(-1, 1), y_validation[:midpoint])
            raw_holdout = float(brier_score_loss(y_validation[midpoint:], validation_raw[midpoint:]))
            calibrated_holdout_values = candidate.predict_proba(validation_raw[midpoint:].reshape(-1, 1))[:, 1]
            calibrated_holdout = float(brier_score_loss(y_validation[midpoint:], calibrated_holdout_values))
            improvement = raw_holdout - calibrated_holdout
            calibration_choice |= {"validation_holdout_raw_brier": raw_holdout,
                                   "validation_holdout_calibrated_brier": calibrated_holdout,
                                   "validation_holdout_improvement": improvement}
            if improvement >= calibration_choice["material_improvement_threshold"]:
                calibrator = candidate
                calibration_choice |= {"method": "sigmoid", "applied": True}
        final_model = self.factories()[selected](); final_model.fit(np.vstack([X_train, X_validation]), np.concatenate([y_train, y_validation]))
        if calibrator is not None:
            final_model = ProbabilityCalibratedModel(final_model, calibrator)
        test_probability = final_model.predict_proba(X_test)[:, 1]
        test_metrics = evaluate_probabilities(y_test, test_probability)
        validation_metrics = models[selected]["validation"]
        validation_baseline = _baseline(y_train, y_validation)
        test_baseline = _baseline(np.concatenate([y_train, y_validation]), y_test)
        beats_baseline = validation_metrics["brier_score"] < validation_baseline["base_rate_probability"]["brier_score"]
        walk_forward = self._walk_forward(clean, feature_names, selected)
        version_seed = f"{datetime.now(timezone.utc).isoformat()}|{schema_hash}|{selected}"
        version = f"ml-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{hashlib.sha256(version_seed.encode()).hexdigest()[:8]}"
        distribution = {"total": n, "positive": int(clean[TARGET_COLUMN].sum()), "negative": int(n-clean[TARGET_COLUMN].sum()),
                        "positive_rate": float(clean[TARGET_COLUMN].mean()), "negative_rate": float(1-clean[TARGET_COLUMN].mean())}
        gap = models[selected]["train"]["brier_score"] - validation_metrics["brier_score"]
        calibration = calibration_diagnostics(y_test, test_probability)
        calibration.update(calibration_choice)
        calibration["selection_data"] = "validation_only"
        result = TrainingResult(
            True, "READY", "Selected by lowest chronological validation Brier score. " +
            ("It beat the base-rate probability baseline." if beats_baseline else "It did not beat the base-rate probability baseline; estimates must be treated as weak."),
            n, models, {"validated": beats_baseline, "weights": {selected: 1.0}, "feature_columns": feature_names},
            {"validation": validation_baseline, "test": test_baseline, "historical_base_rate": distribution["positive_rate"],
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
             "outperforms_validation_baseline": beats_baseline,
             "deployable": beats_baseline,
             "walk_forward_brier_std": float(np.std([fold["metrics"]["brier_score"] for fold in walk_forward])) if walk_forward else None})
        result._model = final_model  # type: ignore[attr-defined]
        result._models = {selected: final_model}  # type: ignore[attr-defined]
        self.latest = result
        return result

    def _walk_forward(self, data: pd.DataFrame, feature_names: list[str], algorithm: str) -> list[dict]:
        results, n = [], len(data)
        for end in (int(n * .60), int(n * .70), int(n * .80)):
            stop = min(end + int(n * .10), n)
            train, evaluate = data.iloc[:end], data.iloc[end:stop]
            if len(evaluate) == 0 or train[TARGET_COLUMN].nunique() < 2:
                continue
            model = self.factories()[algorithm](); model.fit(train[feature_names], train[TARGET_COLUMN])
            metrics = evaluate_probabilities(evaluate[TARGET_COLUMN].to_numpy(int), model.predict_proba(evaluate[feature_names])[:, 1])
            results.append({"train_end": end, "evaluation_start": end, "evaluation_end": stop, "metrics": metrics})
        return results

    def _empty(self, status: str, message: str, size: int) -> TrainingResult:
        return TrainingResult(False, status, message, size, {}, {}, {}, {}, datetime.now(timezone.utc).isoformat())
