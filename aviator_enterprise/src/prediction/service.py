"""
Prediction Service
==================

Single entry point for live and batch predictions.  Responsibilities:
  * Loads the active model + calibrator from the registry
  * Engineers features from the latest history
  * Optionally applies the SHAP explainer
  * Logs the prediction to the performance tracker
  * Updates the drift detector with the new feature vector
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

from config.settings import settings
from src.core.exceptions import ModelNotTrainedError, PredictionError
from src.core.helpers import deterministic_hash, short_uuid, utc_now_iso
from src.core.logger import get_logger
from src.data import DataCollector, Preprocessor
from src.evaluation.explainer import Explanation, ShapExplainer
from src.features.engineering import FeatureEngineeringPipeline
from src.monitoring.drift_detector import DriftDetectionService
from src.monitoring.health import HealthMonitor
from src.monitoring.performance_tracker import PerformanceTracker, PredictionLog
from src.models import build_model, BaseModel
from src.prediction.uncertainty import UncertaintyEstimator
from src.registry.model_registry import ModelRegistry, ModelVersion
from src.training.calibrator import Calibrator


logger = get_logger(__name__)


@dataclass
class PredictionResponse:
    prediction_id: str
    prediction: int
    predicted_class: str
    probabilities: Dict[str, float]
    confidence: float
    uncertainty: float
    risk_level: str
    recommendation: str
    reasoning: str
    model_name: str
    model_version: str
    top_features: List[Dict[str, Any]] = field(default_factory=list)
    feature_importance: List[Dict[str, Any]] = field(default_factory=list)
    timestamp: str = field(default_factory=utc_now_iso)
    latency_ms: float = 0.0
    is_actionable: bool = True
    extra: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return self.__dict__.copy()


class PredictionService:
    """Coordinates the full live-prediction pipeline."""

    def __init__(
        self,
        *,
        collector: Optional[DataCollector] = None,
        preprocessor: Optional[Preprocessor] = None,
        feature_pipeline: Optional[FeatureEngineeringPipeline] = None,
        registry: Optional[ModelRegistry] = None,
        tracker: Optional[PerformanceTracker] = None,
        drift: Optional[DriftDetectionService] = None,
        health: Optional[HealthMonitor] = None,
    ) -> None:
        self.collector = collector or DataCollector()
        self.preprocessor = preprocessor or Preprocessor()
        self.feature_pipeline = feature_pipeline or FeatureEngineeringPipeline()
        self.registry = registry or ModelRegistry()
        self.tracker = tracker or PerformanceTracker()
        self.drift = drift or DriftDetectionService()
        self.health = health or HealthMonitor()
        self.uncertainty_estimator = UncertaintyEstimator(
            min_confidence=settings.min_confidence,
            max_uncertainty=settings.max_uncertainty,
        )
        self._model: Optional[BaseModel] = None
        self._calibrator: Optional[Calibrator] = None
        self._active_version: Optional[ModelVersion] = None
        self._explainer: Optional[ShapExplainer] = None
        self._lock = asyncio.Lock()
        self.logger = get_logger(self.__class__.__name__)

    # ---------------------------------------------------------- model
    async def load_active(self, model_name: str = "xgboost") -> ModelVersion:
        async with self._lock:
            version = self.registry.get_active(model_name)
            self._model = build_model(model_name, n_classes=len(settings.category_name_list()))
            self._model.load(Path(version.artifact_path))
            if version.calibrator_path and Path(version.calibrator_path).exists():
                self._calibrator = Calibrator.load(Path(version.calibrator_path))
            else:
                self._calibrator = None
            self._active_version = version
            self._explainer = None
            self.logger.info("Active model loaded", extra={"version": version.version})
            return version

    def set_model(self, model: BaseModel, calibrator: Optional[Calibrator] = None, version: Optional[ModelVersion] = None) -> None:
        self._model = model
        self._calibrator = calibrator
        self._active_version = version or ModelVersion(
            model_name=model.name, version=model.version, artifact_path="(in-memory)"
        )
        self._explainer = None

    # ---------------------------------------------------------- predict
    async def predict(self, model_name: str = "xgboost", *, explain: bool = True) -> PredictionResponse:
        t0 = time.perf_counter()
        try:
            if self._model is None or (self._active_version and self._active_version.model_name != model_name):
                await self.load_active(model_name)
            assert self._model is not None
            assert self._active_version is not None

            records = await self.collector.collect()
            df = self.preprocessor.transform(records)
            if df.empty:
                raise PredictionError("No data available for prediction")

            # Fit feature pipeline on first use if not fitted
            if not self.feature_pipeline.feature_names_:
                self.feature_pipeline.fit_transform(df, target_col="category")
            result = self.feature_pipeline.transform(df, target_col="category")

            if result.features.empty:
                raise PredictionError("No engineered features available")

            # Set drift reference from historical data on first use
            if len(self.drift.feature_history) == 0 and not result.features.empty:
                ref = result.features.tail(min(200, len(result.features)))
                self.drift.set_reference(ref)

            x = result.features.tail(1)
            proba = self._model.predict_proba(x)
            if self._calibrator is not None:
                proba = self._calibrator.transform(proba)
            proba = self._normalize(proba)
            pred = int(np.argmax(proba[0]))
            confidence = float(proba[0, pred])

            # uncertainty
            unc = self.uncertainty_estimator.estimate(proba[0])

            # explanation
            explanation: Optional[Explanation] = None
            if explain:
                try:
                    if self._explainer is None:
                        self._explainer = ShapExplainer(self._model, background=result.features.tail(50))
                    explanation = self._explainer.explain(x, pred, self._model.class_names)
                except Exception as exc:
                    self.logger.debug("SHAP explain failed: %s", exc)

            # importance
            importance = self._feature_importance()
            top_features = explanation.top_features if explanation else []
            risk_level = explanation.risk_level if explanation else self._risk_level(self._model.class_names[pred], confidence)
            recommendation = explanation.recommendation if explanation else self._recommendation(self._model.class_names[pred], confidence)
            reasoning = explanation.reasoning if explanation else f"Predicted {self._model.class_names[pred]} with {confidence*100:.1f}% confidence."

            # log + drift
            entry = PredictionLog(
                prediction_id=short_uuid(12),
                timestamp=utc_now_iso(),
                predicted_class=pred,
                predicted_class_name=self._model.class_names[pred],
                probabilities={c: float(p) for c, p in zip(self._model.class_names, proba[0])},
                confidence=confidence,
                uncertainty=unc.normalised_entropy,
                model_name=self._model.name,
                model_version=self._active_version.version,
                features_used=list(x.columns),
                risk_level=risk_level,
                recommendation=recommendation,
            )
            self.tracker.log(entry)
            self.drift.add_features({c: float(x.iloc[0][c]) for c in x.columns})
            self.drift.add_prediction(pred)

            latency_ms = (time.perf_counter() - t0) * 1000
            self.health.record_latency(latency_ms)
            self.health.record_error(False)

            return PredictionResponse(
                prediction_id=entry.prediction_id,
                prediction=pred,
                predicted_class=self._model.class_names[pred],
                probabilities={c: float(p) for c, p in zip(self._model.class_names, proba[0])},
                confidence=confidence,
                uncertainty=unc.normalised_entropy,
                risk_level=risk_level,
                recommendation=recommendation,
                reasoning=reasoning,
                model_name=self._model.name,
                model_version=self._active_version.version,
                top_features=top_features,
                feature_importance=importance,
                latency_ms=latency_ms,
                is_actionable=unc.is_actionable,
                extra={"uncertainty_reason": unc.reason},
            )
        except Exception as exc:
            self.health.record_error(True)
            self.logger.exception("Prediction failed")
            raise PredictionError(f"Prediction failed: {exc}") from exc

    # ---------------------------------------------------------- internals
    @staticmethod
    def _normalize(p: np.ndarray) -> np.ndarray:
        p = np.clip(p, 1e-9, 1.0)
        return p / p.sum(axis=-1, keepdims=True)

    def _feature_importance(self) -> List[Dict[str, Any]]:
        if self._model is None or not self.feature_pipeline.feature_names_:
            return []
        imp = self._model.get_feature_importance()
        if imp is None:
            return []
        feats = self.feature_pipeline.feature_names_
        pairs = sorted(zip(feats, imp.tolist()), key=lambda p: abs(float(p[1])), reverse=True)
        return [{"feature": f, "importance": float(v)} for f, v in pairs[:20]]

    @staticmethod
    def _risk_level(pred_class: str, confidence: float) -> str:
        if pred_class in ("HIGH", "VERY_HIGH"):
            return "EXTREME" if confidence < 0.55 else "HIGH"
        if pred_class == "MEDIUM":
            return "MEDIUM" if confidence >= 0.55 else "HIGH"
        return "LOW" if confidence >= 0.55 else "MEDIUM"

    @staticmethod
    def _recommendation(pred_class: str, confidence: float) -> str:
        if confidence >= 0.70 and pred_class in ("LOW", "MEDIUM"):
            return "BET"
        if confidence >= 0.85 and pred_class in ("HIGH", "VERY_HIGH"):
            return "BET"
        if confidence < 0.40:
            return "SKIP"
        return "WEAK_BET"

    @property
    def active_model_version(self) -> Optional[ModelVersion]:
        return self._active_version


__all__ = ["PredictionService", "PredictionResponse"]

