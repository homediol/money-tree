"""
Uncertainty Estimation
======================

Provides two complementary uncertainty signals:
  * Predictive entropy (information-theoretic)
  * MC-Dropout variance (epistemic, for deep models)

A prediction is considered "actionable" only if its confidence exceeds
`min_confidence` AND its uncertainty is below `max_uncertainty`.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import numpy as np


@dataclass
class UncertaintyEstimate:
    predictive_entropy: float
    normalised_entropy: float
    mc_dropout_variance: Optional[float] = None
    confidence: float = 0.0
    is_actionable: bool = False
    reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return self.__dict__.copy()


class UncertaintyEstimator:
    def __init__(self, *, min_confidence: float = 0.40, max_uncertainty: float = 0.55) -> None:
        self.min_confidence = min_confidence
        self.max_uncertainty = max_uncertainty

    def estimate(self, proba: np.ndarray, mc_proba: Optional[np.ndarray] = None) -> UncertaintyEstimate:
        p = proba / max(proba.sum(), 1e-9)
        ent = float(-np.sum(p * np.log2(p + 1e-12)))
        norm = ent / max(np.log2(len(p)), 1e-9)
        conf = float(np.max(p))
        var = None
        if mc_proba is not None:
            var = float(np.mean(np.var(mc_proba, axis=0)))
        is_ok = (conf >= self.min_confidence) and (norm <= self.max_uncertainty)
        reason = "ok" if is_ok else (
            "low_confidence" if conf < self.min_confidence else "high_uncertainty"
        )
        return UncertaintyEstimate(
            predictive_entropy=ent,
            normalised_entropy=norm,
            mc_dropout_variance=var,
            confidence=conf,
            is_actionable=is_ok,
            reason=reason,
        )


__all__ = ["UncertaintyEstimator", "UncertaintyEstimate"]

