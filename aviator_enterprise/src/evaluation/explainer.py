"""
SHAP-based Explainer
====================

Produces per-prediction SHAP values, top contributing features and a
human-readable reasoning string.

Two backends:
  * `tree`   — fast exact SHAP for tree ensembles (XGB/LGB/CB/RF)
  * `kernel` — model-agnostic SHAP (slower, used for NN/LSTM/Transformer)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from src.core.helpers import safe_float
from src.core.logger import get_logger
from src.models.base import BaseModel

logger = get_logger(__name__)


@dataclass
class Explanation:
    predicted_class: int
    predicted_class_name: str
    confidence: float
    top_features: List[Dict[str, Any]]
    shap_values: Optional[np.ndarray] = None
    base_value: Optional[float] = None
    reasoning: str = ""
    risk_level: str = "MEDIUM"
    recommendation: str = "SKIP"
    extra: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return self.__dict__.copy()


class ShapExplainer:
    """Wrapper that auto-selects a SHAP explainer based on model type."""

    def __init__(self, model: BaseModel, background: Optional[pd.DataFrame] = None) -> None:
        self.model = model
        self.background = background
        self._explainer = None
        self._explainer_type: Optional[str] = None
        self._init_explainer()
        self.logger = get_logger(self.__class__.__name__)

    def _init_explainer(self) -> None:
        try:
            import shap
        except Exception as exc:
            self.logger.warning("SHAP not available: %s", exc)
            return
        name = self.model.family
        if name in ("tree", "gradient_boosting", "linear") and self.model.model is not None:
            try:
                self._explainer = shap.Explainer(self.model.model)
                self._explainer_type = "tree"
                return
            except Exception as exc:
                self.logger.debug("Tree SHAP failed: %s", exc)
        # fallback to permutation-style kernel explainer
        if self.background is not None and len(self.background) > 0:
            try:
                self._explainer = shap.KernelExplainer(self.model.predict_proba, self.background.values[:50])
                self._explainer_type = "kernel"
            except Exception as exc:
                self.logger.warning("Kernel SHAP failed: %s", exc)

    # ---------------------------------------------------------- explain
    def explain(self, X: pd.DataFrame, prediction: int, class_names: List[str]) -> Explanation:
        proba = self.model.predict_proba(X)
        confidence = float(proba[0, prediction])
        top_feats: List[Dict[str, Any]] = []
        shap_values: Optional[np.ndarray] = None
        base_value: Optional[float] = None

        if self._explainer is not None:
            try:
                import shap
                sv = self._explainer.shap_values(X)
                arr = np.array(sv)
                if arr.ndim == 3:
                    shap_values = arr[0, :, prediction] if arr.shape[-1] > prediction else arr[0, :, 0]
                    if hasattr(self._explainer, "expected_value"):
                        ev = self._explainer.expected_value
                        base_value = float(ev[prediction] if isinstance(ev, (list, np.ndarray)) else ev)
                elif arr.ndim == 2:
                    shap_values = arr[0]
                if shap_values is not None:
                    feats = list(X.columns)
                    pairs = sorted(zip(feats, shap_values), key=lambda p: abs(float(p[1])), reverse=True)
                    for name, val in pairs[:8]:
                        top_feats.append({
                            "feature": name,
                            "value": safe_float(X.iloc[0][name]),
                            "shap_value": safe_float(val),
                            "impact": "positive" if float(val) > 0 else "negative",
                        })
            except Exception as exc:
                self.logger.debug("SHAP compute failed: %s", exc)

        if not top_feats:
            # fall back to permutation-style importance via predict_proba perturbation
            try:
                base_proba = self.model.predict_proba(X)
                for col in X.columns:
                    perturbed = X.copy()
                    perturbed.iloc[0, perturbed.columns.get_loc(col)] = perturbed[col].median()
                    new_proba = self.model.predict_proba(perturbed)
                    delta = float(new_proba[0, prediction] - base_proba[0, prediction])
                    top_feats.append({
                        "feature": col,
                        "value": safe_float(X.iloc[0][col]),
                        "shap_value": delta,
                        "impact": "positive" if delta > 0 else "negative",
                    })
                top_feats.sort(key=lambda d: abs(d["shap_value"]), reverse=True)
                top_feats = top_feats[:8]
            except Exception:
                pass

        reasoning = self._build_reasoning(class_names[prediction], confidence, top_feats)
        risk_level = self._risk_level(class_names[prediction], confidence)
        recommendation = self._recommendation(class_names[prediction], confidence)

        return Explanation(
            predicted_class=prediction,
            predicted_class_name=class_names[prediction],
            confidence=confidence,
            top_features=top_feats,
            shap_values=shap_values,
            base_value=base_value,
            reasoning=reasoning,
            risk_level=risk_level,
            recommendation=recommendation,
        )

    # ---------------------------------------------------------- helpers
    @staticmethod
    def _build_reasoning(pred_class: str, confidence: float, top_feats: List[Dict[str, Any]]) -> str:
        if not top_feats:
            return f"Model predicts {pred_class} with {confidence*100:.1f}% confidence."
        bullets = ", ".join(f"{f['feature']} ({f['impact']})" for f in top_feats[:3])
        return (
            f"Predicted class: {pred_class} "
            f"(confidence {confidence*100:.1f}%). "
            f"Top drivers: {bullets}."
        )

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


__all__ = ["ShapExplainer", "Explanation"]

