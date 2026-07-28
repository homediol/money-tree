"""
Training Orchestrator
=====================

End-to-end training:
  1. Collect & validate data
  2. Engineer features
  3. Build model (optionally optimised via Optuna)
  4. Fit on training split
  5. Cross-validate
  6. Calibrate probabilities
  7. Persist model + metadata to registry

Returns a `TrainingResult` that the API surfaces to the dashboard.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
from sklearn.model_selection import train_test_split
from sklearn.utils.class_weight import compute_class_weight, compute_sample_weight

from config.settings import settings
from src.core.exceptions import (
    InsufficientDataError, ModelTrainingError, FeatureEngineeringError
)
from src.core.helpers import utc_now_iso
from src.core.logger import get_logger
from src.data import DataCollector, Preprocessor, RoundValidator, FeatureStore
from src.features.engineering import FeatureEngineeringPipeline
from src.models import build_model, BaseModel
from src.models.ensemble import EnsembleModel
from src.registry.model_registry import ModelRegistry, ModelVersion
from src.training.calibrator import Calibrator
from src.training.cross_validator import CrossValidator, CrossValidationReport
from src.training.optimizer import HyperparameterOptimizer, OptimizationResult


logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------
@dataclass
class TrainingResult:
    model_name: str
    model_version: str
    metrics: Dict[str, float]
    cv_report: Optional[CrossValidationReport] = None
    optimization: Optional[OptimizationResult] = None
    feature_names: List[str] = field(default_factory=list)
    calibration: Optional[Dict[str, Any]] = None
    duration_seconds: float = 0.0
    n_samples: int = 0
    n_features: int = 0
    class_distribution: Dict[str, int] = field(default_factory=dict)
    training_history: Dict[str, List[float]] = field(default_factory=dict)
    artifact_path: Optional[str] = None


# ---------------------------------------------------------------------------
# Trainer
# ---------------------------------------------------------------------------
class Trainer:
    """Coordinates a full training run for a single model."""

    def __init__(
        self,
        *,
        collector: Optional[DataCollector] = None,
        preprocessor: Optional[Preprocessor] = None,
        feature_pipeline: Optional[FeatureEngineeringPipeline] = None,
        feature_store: Optional[FeatureStore] = None,
        model_registry: Optional[ModelRegistry] = None,
    ) -> None:
        self.collector = collector or DataCollector()
        self.preprocessor = preprocessor or Preprocessor()
        self.pipeline = feature_pipeline or FeatureEngineeringPipeline()
        self.feature_store = feature_store or FeatureStore()
        self.model_registry = model_registry or ModelRegistry()
        self.logger = get_logger(self.__class__.__name__)

    # ---------------------------------------------------------- public
    async def train(
        self,
        model_name: str,
        *,
        hyperparameters: Optional[Dict[str, Any]] = None,
        optimize: bool = False,
        n_optuna_trials: Optional[int] = None,
        cross_validate: bool = True,
        calibrate: bool = True,
        class_imbalance: str = "class_weight",
        persist: bool = True,
    ) -> TrainingResult:
        t0 = time.perf_counter()
        records = await self.collector.collect()
        RoundValidator().validate_and_clean(records)
        if len(records) < 100:
            raise InsufficientDataError(f"Need >=100 rounds, got {len(records)}")

        df = self.preprocessor.transform(records)
        result = self.pipeline.fit_transform(df, target_col="category")
        if result.features.empty or result.target is None:
            raise FeatureEngineeringError("Feature pipeline produced no usable data")

        try:
            await self.feature_store.write(
                "aviator_features",
                result.features.assign(target=result.target.values),
                source_hash=str(hash(tuple(r.round_id for r in records[-500:]))),
                extra={
                    "target_col": "target",
                    "pipeline": self.pipeline.export_metadata(),
                    "round_count": len(records),
                },
            )
        except Exception as exc:
            self.logger.warning("Feature-store write failed: %s", exc)

        X = result.features.values
        y = result.target.values.astype(np.int64)
        n_classes = int(y.max() + 1)
        class_names = settings.category_name_list()[:n_classes]
        boundaries = settings.category_boundary_list()[:n_classes - 1]
        self.logger.info("Features ready", extra={"n_samples": len(y), "n_features": X.shape[1]})

        # ---- class imbalance ----
        sample_weight = None
        if class_imbalance == "class_weight":
            sample_weight = compute_sample_weight(class_weight="balanced", y=y)
        elif class_imbalance == "smote":
            try:
                from imblearn.over_sampling import SMOTE
                sm = SMOTE(random_state=42)
                X, y = sm.fit_resample(X, y)
                self.logger.info("Applied SMOTE", extra={"new_n": len(y)})
            except Exception as exc:
                self.logger.warning(f"SMOTE failed, falling back to class_weight: {exc}")
                sample_weight = compute_sample_weight(class_weight="balanced", y=y)

        # ---- optuna ----
        optimization: Optional[OptimizationResult] = None
        params: Dict[str, Any] = hyperparameters or {}
        if optimize:
            optimizer = HyperparameterOptimizer(
                model_name=model_name,
                n_trials=n_optuna_trials or settings.optuna_trials,
                timeout=settings.optuna_timeout,
                cv_folds=settings.cv_folds,
            )
            optimization = optimizer.optimize(X, y)
            params = optimization.best_params

        # ---- train / val split ----
        X_train, X_val, y_train, y_val, sw_train, sw_val = train_test_split(
            X, y, sample_weight, test_size=settings.test_size, random_state=42, stratify=y
        )

        # ---- fit ----
        if model_name == "ensemble":
            model = self._build_ensemble(X_train, y_train, n_classes, class_names, params)
        else:
            model = build_model(model_name, n_classes=n_classes, class_names=class_names, **params)
        try:
            if model_name == "ensemble":
                model.fit(X_train, y_train)
            else:
                model.fit(X_train, y_train, sample_weight=sw_train,
                          eval_set=[(X_val, y_val)] if X_val.size else None)
        except TypeError:
            model.fit(X_train, y_train, sample_weight=sw_train)

        # ---- cross validation ----
        cv_report: Optional[CrossValidationReport] = None
        if cross_validate:
            cv_report = CrossValidator(
                n_folds=settings.cv_folds,
                n_bootstrap=settings.n_bootstrap,
            ).run(model, X, y)

        # ---- calibration ----
        calibrator = None
        calibration_meta: Optional[Dict[str, Any]] = None
        if calibrate and X_val.size:
            calibrator = Calibrator(method=settings.calibration_method)
            calibrator.fit(model.predict_proba(X_val), y_val)

        # ---- metrics on val ----
        val_proba = model.predict_proba(X_val)
        if calibrator is not None:
            val_proba_cal = calibrator.transform(val_proba)
        else:
            val_proba_cal = val_proba
        val_pred = val_proba_cal.argmax(1)
        cv_temp = CrossValidator(n_folds=2, n_bootstrap=50)
        val_metrics = cv_temp._compute_metrics(y_val, val_pred, val_proba_cal)

        # ---- class distribution ----
        unique, counts = np.unique(y, return_counts=True)
        class_dist = {class_names[i]: int(c) for i, c in zip(unique, counts)}

        # ---- persist + register ----
        artifact_path: Optional[str] = None
        if persist and settings.models_dir is not None:
            artifact_path = self._persist(model, calibrator, result.feature_names, val_metrics, model_name)
            # Register in the model registry so prediction service can find it
            try:
                mv = ModelVersion(
                    model_name=model_name,
                    version=model.version,
                    artifact_path=artifact_path,
                    calibrator_path=str(Path(settings.models_dir) / model_name / f"{model.version}_calibrator.joblib") if calibrator is not None else None,
                    metrics={k: float(v) for k, v in val_metrics.items() if isinstance(v, (int, float))},
                    feature_names=result.feature_names,
                    is_active=True,  # newly trained model becomes active
                )
                self.model_registry.register(mv)
                self.logger.info("Model registered", extra={"name": model_name, "version": model.version})
            except Exception as reg_exc:
                self.logger.warning("Registry registration failed: %s", reg_exc)

        dt = time.perf_counter() - t0
        return TrainingResult(
            model_name=model_name,
            model_version=model.version,
            metrics=val_metrics,
            cv_report=cv_report,
            optimization=optimization,
            feature_names=result.feature_names,
            calibration=calibration_meta,
            duration_seconds=dt,
            n_samples=len(y),
            n_features=len(result.feature_names),
            class_distribution=class_dist,
            training_history=model.training_history,
            artifact_path=artifact_path,
        )

    # ---------------------------------------------------------- helpers
    def _persist(
        self,
        model: BaseModel,
        calibrator: Optional[Calibrator],
        feature_names: List[str],
        metrics: Dict[str, float],
        model_name: str,
    ) -> str:
        assert settings.models_dir is not None
        model_dir = Path(settings.models_dir) / model_name
        model_dir.mkdir(parents=True, exist_ok=True)
        model_path = model_dir / f"{model.version}.joblib"
        model.save(model_path)
        if calibrator is not None:
            calibrator.save(model_dir / f"{model.version}_calibrator.joblib")
        meta = {
            "model_name": model_name,
            "version": model.version,
            "feature_names": feature_names,
            "metrics": metrics,
            "created_at": utc_now_iso(),
            "artifact_path": str(model_path),
            "calibrator_path": str(model_dir / f"{model.version}_calibrator.joblib") if calibrator else None,
        }
        (model_dir / f"{model.version}_meta.json").write_text(
            __import__("json").dumps(meta, indent=2, default=str), encoding="utf-8"
        )
        return str(model_path)

    def _build_ensemble(
        self,
        X_train: np.ndarray,
        y_train: np.ndarray,
        n_classes: int,
        class_names: List[str],
        params: Dict[str, Any],
    ) -> EnsembleModel:
        """Train base learners and return a fitted-ready ensemble wrapper."""
        member_names = params.pop(
            "members",
            ["random_forest", "xgboost", "lightgbm", "logistic_regression"],
        )
        members: List[BaseModel] = []
        for member_name in member_names:
            try:
                member = build_model(member_name, n_classes=n_classes, class_names=class_names)
                member.fit(X_train, y_train)
                members.append(member)
            except Exception as exc:
                self.logger.warning("Skipping ensemble member %s: %s", member_name, exc)
        if not members:
            raise ModelTrainingError("Ensemble training failed: no member model trained successfully")
        return EnsembleModel(members=members, n_classes=n_classes, class_names=class_names, **params)


__all__ = ["Trainer", "TrainingResult"]
