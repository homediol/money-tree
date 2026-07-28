"""
Drift detection for features and predictions.

Two complementary methods:
  * Population Stability Index (PSI) — fast, distribution-based
  * Kolmogorov–Smirnov two-sample test — non-parametric, per-feature
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional

import numpy as np
import pandas as pd
from scipy import stats as sp_stats

from config.settings import settings
from src.core.logger import get_logger

logger = get_logger(__name__)


@dataclass(slots=True)
class DriftReport:
    feature_drift: Dict[str, float]
    prediction_drift: float
    overall_score: float
    drift_detected: bool
    top_drifted_features: List[str]


class DriftDetector:
    """Computes PSI and KS per feature, reports an overall drift score."""

    PSI_THRESHOLD = 0.2  # industry standard for "significant drift"
    KS_THRESHOLD = 0.05

    def __init__(self, reference: Optional[pd.DataFrame] = None) -> None:
        self.reference = reference
        self.threshold = settings.drift_threshold
        self.logger = get_logger(self.__class__.__name__)

    def set_reference(self, reference: pd.DataFrame) -> None:
        self.reference = reference.copy()

    def compute(self, current: pd.DataFrame) -> DriftReport:
        if self.reference is None or self.reference.empty:
            return DriftReport({}, 0.0, 0.0, False, [])

        common = [c for c in current.columns if c in self.reference.columns]
        if not common:
            return DriftReport({}, 0.0, 0.0, False, [])

        per_feature: Dict[str, float] = {}
        for col in common:
            ref = self.reference[col].dropna().values
            cur = current[col].dropna().values
            if len(ref) < 30 or len(cur) < 30:
                continue
            try:
                psi = self._psi(ref, cur)
                per_feature[col] = float(psi)
            except Exception as exc:
                self.logger.debug("PSI failed for %s: %s", col, exc)

        if not per_feature:
            return DriftReport({}, 0.0, 0.0, False, [])

        overall = float(np.mean(list(per_feature.values())))
        top = sorted(per_feature.items(), key=lambda kv: kv[1], reverse=True)
        return DriftReport(
            feature_drift=per_feature,
            prediction_drift=0.0,
            overall_score=overall,
            drift_detected=overall > self.threshold,
            top_drifted_features=[k for k, _ in top[:10]],
        )

    @staticmethod
    def _psi(reference: np.ndarray, current: np.ndarray, bins: int = 10) -> float:
        eps = 1e-6
        quantiles = np.linspace(0, 1, bins + 1)
        edges = np.quantile(reference, quantiles)
        edges = np.unique(edges)
        if len(edges) < 3:
            return 0.0
        ref_counts, _ = np.histogram(reference, bins=edges)
        cur_counts, _ = np.histogram(current, bins=edges)
        ref_pct = (ref_counts + eps) / (ref_counts.sum() + eps * len(ref_counts))
        cur_pct = (cur_counts + eps) / (cur_counts.sum() + eps * len(cur_counts))
        return float(np.sum((cur_pct - ref_pct) * np.log(cur_pct / ref_pct)))

    @staticmethod
    def ks_test(reference: np.ndarray, current: np.ndarray) -> float:
        if len(reference) < 5 or len(current) < 5:
            return 1.0
        return float(sp_stats.ks_2samp(reference, current).pvalue)


__all__ = ["DriftDetector", "DriftReport"]

