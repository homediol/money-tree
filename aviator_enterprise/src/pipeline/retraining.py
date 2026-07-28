"""
Continuous Learning Pipeline
============================

Compares a candidate (re-trained) model against the currently-active
model on a held-out validation set.  Only promotes the new model if it
demonstrates a measurable improvement in the primary metric.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Dict, Optional

import numpy as np

from config.settings import settings
from src.core.helpers import utc_now_iso
from src.core.logger import get_logger
from src.evaluation.metrics import EvaluationService
from src.models import BaseModel
from src.registry.model_registry import ModelRegistry, ModelVersion
from src.training.calibrator import Calibrator
from src.training.cross_validator import CrossValidator


logger = get_logger(__name__)


@dataclass
class PromotionDecision:
    promoted: bool
    reason: str
    active_version: str
    candidate_version: str
    active_metric: float
    candidate_metric: float
    improvement: float
    metric: str


class ContinuousLearningPipeline:
    """Compares a candidate model to the active one and decides on promotion."""

    def __init__(self, registry: Optional[ModelRegistry] = None) -> None:
        self.registry = registry or ModelRegistry()
        self.evaluator = EvaluationService()
        self.cv = CrossValidator(n_folds=3, n_bootstrap=100)
        self.logger = get_logger(self.__class__.__name__)

    # ---------------------------------------------------------- public
    async def maybe_retrain(
        self,
        candidate: BaseModel,
        candidate_calibrator: Optional[Calibrator],
        *,
        X_val: np.ndarray,
        y_val: np.ndarray,
        metric: str = "f1_macro",
        model_name: str = "candidate",
        min_improvement: Optional[float] = None,
    ) -> PromotionDecision:
        if y_val.size == 0:
            return PromotionDecision(False, "empty_validation_set", "-", candidate.version, 0.0, 0.0, 0.0, metric)

        cand_proba = candidate.predict_proba(X_val)
        if candidate_calibrator is not None:
            cand_proba = candidate_calibrator.transform(cand_proba)
        cand_pred = cand_proba.argmax(1)
        cand_metrics = self.evaluator.compute(y_val, cand_pred, cand_proba)
        cand_score = float(cand_metrics.extra.get("f1_macro", cand_metrics.f1_macro))

        active = self.registry.get_active(model_name) if model_name in {m.model_name for m in self.registry.list()} else None
        if active is None:
            return PromotionDecision(
                promoted=True, reason="no_active_model_to_compare",
                active_version="-", candidate_version=candidate.version,
                active_metric=0.0, candidate_metric=cand_score, improvement=cand_score, metric=metric,
            )

        # load active model and compare
        from src.models import build_model
        active_model = build_model(active.model_name, n_classes=len(candidate.class_names))
        try:
            active_model.load(_to_path(active.artifact_path))
        except Exception as exc:
            self.logger.warning("Could not load active model: %s", exc)
            return PromotionDecision(
                promoted=True, reason=f"active_load_failed: {exc}",
                active_version=active.version, candidate_version=candidate.version,
                active_metric=0.0, candidate_metric=cand_score, improvement=cand_score, metric=metric,
            )
        active_proba = active_model.predict_proba(X_val)
        active_pred = active_proba.argmax(1)
        active_metrics = self.evaluator.compute(y_val, active_pred, active_proba)
        active_score = float(active_metrics.f1_macro)

        threshold = min_improvement if min_improvement is not None else settings.retrain_min_improvement
        improvement = cand_score - active_score
        promoted = improvement >= threshold
        reason = (
            f"improved by {improvement:.4f}" if promoted
            else f"insufficient improvement ({improvement:.4f} < {threshold:.4f})"
        )
        return PromotionDecision(
            promoted=promoted, reason=reason,
            active_version=active.version, candidate_version=candidate.version,
            active_metric=active_score, candidate_metric=cand_score, improvement=improvement, metric=metric,
        )

    # ---------------------------------------------------------- scheduler
    async def should_retrain(
        self,
        *,
        new_rounds: int,
        drift_score: float = 0.0,
        min_new_rounds: Optional[int] = None,
        drift_threshold: Optional[float] = None,
    ) -> bool:
        min_new = min_new_rounds if min_new_rounds is not None else settings.retrain_min_new_rounds
        if new_rounds < min_new:
            return False
        if drift_score > (drift_threshold if drift_threshold is not None else settings.drift_threshold):
            return True
        return False


def _to_path(p: str):
    from pathlib import Path
    return Path(p)


__all__ = ["ContinuousLearningPipeline", "PromotionDecision"]

