from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import numpy as np
import pandas as pd

from app.services.feature_engineering import build_supervised_frame


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

    def model_dump(self) -> dict[str, Any]:
        # Private runtime attributes (e.g. fitted sklearn estimators on `_models`)
        # are not JSON-serializable and must never be exposed or persisted.
        return {k: v for k, v in self.__dict__.items() if not k.startswith("_")}


def _metrics(y_true: np.ndarray, proba: np.ndarray) -> dict[str, float]:
    pred = (proba >= 0.5).astype(int)
    tp = int(((pred == 1) & (y_true == 1)).sum())
    fp = int(((pred == 1) & (y_true == 0)).sum())
    fn = int(((pred == 0) & (y_true == 1)).sum())
    tn = int(((pred == 0) & (y_true == 0)).sum())
    accuracy = float((tp + tn) / max(len(y_true), 1))
    precision = float(tp / max(tp + fp, 1))
    recall = float(tp / max(tp + fn, 1))
    brier = float(np.mean((proba - y_true) ** 2))
    try:
        from sklearn.metrics import roc_auc_score

        roc_auc = float(roc_auc_score(y_true, proba)) if len(set(y_true.tolist())) > 1 else 0.5
    except Exception:
        roc_auc = 0.5
    return {"accuracy": accuracy, "precision": precision, "recall": recall, "roc_auc": roc_auc, "brier_score": brier, "tp": tp, "fp": fp, "fn": fn, "tn": tn}


def _baseline_metrics(y_true: np.ndarray) -> dict[str, Any]:
    base_rate = float(np.mean(y_true)) if len(y_true) else 0.0
    always_high = _metrics(y_true, np.ones_like(y_true, dtype=float))
    always_low = _metrics(y_true, np.zeros_like(y_true, dtype=float))
    base_proba = _metrics(y_true, np.full_like(y_true, base_rate, dtype=float))
    random_proba = _metrics(y_true, np.full_like(y_true, 0.5, dtype=float))
    return {"historical_base_rate": base_rate, "base_rate": base_proba, "always_ge_2x": always_high, "always_below_2x": always_low, "random_50_50": random_proba}


class ModelTrainer:
    def __init__(self, target: float = 2.0):
        self.target = target
        self.latest: TrainingResult | None = None

    def train_validate(self, rounds: pd.DataFrame) -> TrainingResult:
        supervised = build_supervised_frame(rounds, self.target)
        if len(supervised) < 120:
            return self._result(False, "INSUFFICIENT DATA", "At least 120 supervised examples are required for time-series validation.", len(supervised), {}, {}, {}, {})
        try:
            from sklearn.ensemble import GradientBoostingClassifier, RandomForestClassifier
            from sklearn.linear_model import LogisticRegression
            from sklearn.pipeline import make_pipeline
            from sklearn.preprocessing import StandardScaler
        except Exception as exc:
            return self._result(False, "MODEL NOT VALIDATED", f"scikit-learn is required for ML validation: {exc}", len(supervised), {}, {}, {}, {})

        feature_cols = [c for c in supervised.columns if c not in ("next_target", "round_index")]
        X = supervised[feature_cols].to_numpy(dtype=float)
        y = supervised["next_target"].to_numpy(dtype=int)
        split_points = [int(len(y) * r) for r in (0.55, 0.70, 0.85)]
        model_factories = {
            "logistic_regression": lambda: make_pipeline(StandardScaler(), LogisticRegression(max_iter=1000, class_weight="balanced")),
            "random_forest": lambda: RandomForestClassifier(n_estimators=160, min_samples_leaf=8, random_state=42, class_weight="balanced_subsample"),
            "gradient_boosting": lambda: GradientBoostingClassifier(random_state=42),
        }
        predictions: dict[str, list[float]] = {name: [] for name in model_factories}
        truth: list[int] = []
        final_models = {}

        for split in split_points:
            if split <= 60 or split >= len(y) - 10:
                continue
            X_train, y_train = X[:split], y[:split]
            X_test, y_test = X[split : min(split + max(20, len(y) // 10), len(y))], y[split : min(split + max(20, len(y) // 10), len(y))]
            if len(set(y_train.tolist())) < 2 or len(y_test) == 0:
                continue
            truth.extend(y_test.tolist())
            for name, factory in model_factories.items():
                model = factory()
                model.fit(X_train, y_train)
                proba = model.predict_proba(X_test)[:, 1]
                predictions[name].extend(proba.tolist())

        y_eval = np.array(truth, dtype=int)
        baselines = _baseline_metrics(y_eval) if len(y_eval) else {}
        models = {}
        for name, probs in predictions.items():
            if not probs or len(probs) != len(y_eval):
                continue
            models[name] = _metrics(y_eval, np.array(probs, dtype=float))

        for name, factory in model_factories.items():
            model = factory()
            model.fit(X, y)
            final_models[name] = model

        if not models:
            return self._result(False, "MODEL NOT VALIDATED", "Walk-forward validation did not produce valid folds.", len(supervised), {}, {}, baselines, {})

        base_brier = baselines.get("base_rate", {}).get("brier_score", 1.0)
        weights = {}
        for name, metric in models.items():
            edge = max(base_brier - metric["brier_score"], 0.0)
            weights[name] = edge + max(metric["roc_auc"] - 0.5, 0.0)
        total_weight = sum(weights.values())
        if total_weight <= 0:
            weights = {name: 1 / len(models) for name in models}
            validated = False
            status = "MODEL NOT VALIDATED"
            message = "Models did not beat the historical base-rate baseline on validation."
        else:
            weights = {name: weight / total_weight for name, weight in weights.items()}
            best_brier = min(m["brier_score"] for m in models.values())
            validated = best_brier < base_brier
            status = "VALIDATED" if validated else "MODEL NOT VALIDATED"
            message = "Walk-forward validation completed without future leakage."

        ensemble = {"weights": weights, "validated": validated, "feature_columns": feature_cols}
        calibration = {"method": "validation_brier_comparison", "base_brier_score": base_brier, "supported": validated}
        result = self._result(validated, status, message, len(supervised), models, ensemble, baselines, calibration)
        result._models = final_models  # type: ignore[attr-defined]
        self.latest = result
        return result

    def _result(self, validated: bool, status: str, message: str, dataset_size: int, models: dict, ensemble: dict, baselines: dict, calibration: dict) -> TrainingResult:
        return TrainingResult(validated, status, message, dataset_size, models, ensemble, baselines, calibration, datetime.now(timezone.utc).isoformat())



