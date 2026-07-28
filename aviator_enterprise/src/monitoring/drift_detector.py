"""
Drift Detection Service
=======================

Detects data and prediction drift in real time.  Persists a rolling
window of features/predictions and compares against a reference window.
"""
from __future__ import annotations

import json
from collections import deque
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional

import numpy as np
import pandas as pd

from config.settings import settings
from src.core.helpers import ensure_dir, utc_now_iso
from src.core.logger import get_logger
from src.features.drift import DriftDetector, DriftReport

logger = get_logger(__name__)


@dataclass
class DriftSnapshot:
    timestamp: str
    overall_drift: float
    prediction_drift: float
    drift_detected: bool
    top_drifted_features: List[str]
    feature_drift: Dict[str, float] = field(default_factory=dict)


class DriftDetectionService:
    """Maintains a rolling reference and emits drift reports."""

    def __init__(self, window: int = 200) -> None:
        self.window = window
        self.detector = DriftDetector()
        self.feature_history: Deque[Dict[str, float]] = deque(maxlen=window)
        self.prediction_history: Deque[float] = deque(maxlen=window)
        self.snapshots: List[DriftSnapshot] = []
        self.logger = get_logger(self.__class__.__name__)
        self.storage_path = (settings.reports_dir / "drift_snapshots.jsonl")  # type: ignore[operator]

    # ---------------------------------------------------------- ingest
    def add_features(self, features: Dict[str, float]) -> None:
        self.feature_history.append(features)

    def add_prediction(self, predicted_class: int) -> None:
        self.prediction_history.append(float(predicted_class))

    def set_reference(self, features_df: pd.DataFrame) -> None:
        self.detector.set_reference(features_df)

    # ---------------------------------------------------------- compute
    def compute(self) -> DriftReport:
        if len(self.feature_history) < 30:
            return DriftReport({}, 0.0, 0.0, False, [])
        current = pd.DataFrame(list(self.feature_history))
        report = self.detector.compute(current)
        # prediction drift: PSI on class frequencies
        pred_drift = self._prediction_drift()
        report.prediction_drift = pred_drift
        snap = DriftSnapshot(
            timestamp=utc_now_iso(),
            overall_drift=report.overall_score,
            prediction_drift=pred_drift,
            drift_detected=report.drift_detected or pred_drift > 0.1,
            top_drifted_features=report.top_drifted_features,
            feature_drift=report.feature_drift,
        )
        self.snapshots.append(snap)
        self._persist(snap)
        return report

    def history(self, limit: int = 100) -> List[Dict[str, Any]]:
        return [asdict(s) for s in self.snapshots[-limit:]]

    # ---------------------------------------------------------- internals
    def _prediction_drift(self) -> float:
        if len(self.prediction_history) < 30:
            return 0.0
        arr = np.array(self.prediction_history)
        n_classes = 5  # default
        if self.detector.reference is not None and hasattr(self.detector.reference, "shape"):
            n_classes = max(int(arr.max()) + 1, 5)
        uniform = np.full(n_classes, 1.0 / n_classes)
        hist = np.bincount(arr.astype(int), minlength=n_classes)[:n_classes]
        p = (hist + 1e-6) / (hist.sum() + 1e-6 * len(hist))
        return float(np.sum((p - uniform) * np.log(p / uniform)))

    def _persist(self, snap: DriftSnapshot) -> None:
        try:
            ensure_dir(self.storage_path.parent)
            with open(self.storage_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(asdict(snap), default=str) + "\n")
        except Exception as exc:
            self.logger.debug("Drift persistence failed: %s", exc)


__all__ = ["DriftDetectionService", "DriftSnapshot"]

