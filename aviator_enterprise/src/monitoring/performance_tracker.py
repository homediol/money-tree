"""
Performance Tracker (Enterprise v3)
====================================

Tracks every prediction and its outcome, computing rolling performance
metrics, confidence histograms, accuracy trends, and comprehensive
aggregation statistics exposed to the dashboard.

Improvements over v2:
  - Richer rolling metrics (precision, recall, F1, MCC, kappa, log-loss)
  - Accuracy trend with timestamps and windowed statistics
  - Confidence histogram with per-bucket accuracy (reliability diagram data)
  - Class-wise performance breakdown
  - Persistent JSONL log with NDJSON streaming
  - Thread-safe deque with O(1) append / O(n) resolve
"""
from __future__ import annotations

import json
from collections import deque
from dataclasses import asdict, dataclass, field
from typing import Any, Deque, Dict, List, Optional

import numpy as np
from sklearn.metrics import (
    accuracy_score, balanced_accuracy_score, cohen_kappa_score,
    f1_score, log_loss, matthews_corrcoef, precision_score, recall_score,
)

from config.settings import settings
from src.core.helpers import ensure_dir, utc_now_iso
from src.core.logger import get_logger

logger = get_logger(__name__)


@dataclass
class PredictionLog:
    prediction_id: str
    timestamp: str
    predicted_class: int
    predicted_class_name: str
    probabilities: Dict[str, float]
    confidence: float
    uncertainty: float
    model_name: str
    model_version: str
    features_used: List[str]
    actual_class: Optional[int] = None
    actual_class_name: Optional[str] = None
    actual_multiplier: Optional[float] = None
    resolved: bool = False
    correct: Optional[bool] = None
    risk_level: Optional[str] = None
    recommendation: Optional[str] = None
    latency_ms: Optional[float] = None
    shap_top_features: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class PerformanceTracker:
    """
    Real-time performance & log tracking for the dashboard.

    Thread characteristics:
      - Reads are always safe (deque slices are atomic-ish in CPython).
      - Writes use deque which is O(1) append with maxlen eviction.
    """

    def __init__(self, max_size: int = 5000) -> None:
        self.predictions: Deque[PredictionLog] = deque(maxlen=max_size)
        self._persist_path = settings.reports_dir / "prediction_log.jsonl"  # type: ignore[operator]
        self.logger = get_logger(self.__class__.__name__)

    # ---------------------------------------------------------------- logging
    def log(self, entry: PredictionLog) -> None:
        self.predictions.append(entry)
        try:
            ensure_dir(self._persist_path.parent)
            with open(self._persist_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry.to_dict(), default=str) + "\n")
        except Exception as exc:
            self.logger.debug("Prediction log persistence failed: %s", exc)

    def resolve(
        self,
        prediction_id: str,
        actual_class: int,
        actual_multiplier: Optional[float] = None,
        class_names: Optional[List[str]] = None,
    ) -> bool:
        for p in self.predictions:
            if p.prediction_id == prediction_id and not p.resolved:
                p.actual_class = actual_class
                p.actual_class_name = (
                    class_names[actual_class]
                    if class_names and 0 <= actual_class < len(class_names)
                    else str(actual_class)
                )
                p.actual_multiplier = actual_multiplier
                p.resolved = True
                p.correct = p.predicted_class == actual_class
                return True
        return False

    # ---------------------------------------------------------------- aggregates
    def rolling_metrics(self, window: int = 100) -> Dict[str, float]:
        recent = [p for p in list(self.predictions)[-window:] if p.resolved]
        if not recent:
            return self._empty_metrics()
        y_true = np.array([p.actual_class for p in recent], dtype=np.int64)
        y_pred = np.array([p.predicted_class for p in recent], dtype=np.int64)
        proba_list = [list(p.probabilities.values()) for p in recent]
        result: Dict[str, float] = {
            "accuracy": float(accuracy_score(y_true, y_pred)),
            "balanced_accuracy": float(balanced_accuracy_score(y_true, y_pred)),
            "precision_macro": float(
                precision_score(y_true, y_pred, average="macro", zero_division=0)
            ),
            "recall_macro": float(
                recall_score(y_true, y_pred, average="macro", zero_division=0)
            ),
            "precision_weighted": float(
                precision_score(y_true, y_pred, average="weighted", zero_division=0)
            ),
            "recall_weighted": float(
                recall_score(y_true, y_pred, average="weighted", zero_division=0)
            ),
            "f1_macro": float(
                f1_score(y_true, y_pred, average="macro", zero_division=0)
            ),
            "f1_weighted": float(
                f1_score(y_true, y_pred, average="weighted", zero_division=0)
            ),
            "count": float(len(recent)),
        }
        try:
            result["mcc"] = float(matthews_corrcoef(y_true, y_pred))
        except Exception:
            result["mcc"] = 0.0
        try:
            result["cohen_kappa"] = float(cohen_kappa_score(y_true, y_pred))
        except Exception:
            result["cohen_kappa"] = 0.0
        if proba_list and all(len(p) == len(proba_list[0]) for p in proba_list):
            try:
                proba_arr = np.array(proba_list)
                n_classes = proba_arr.shape[1]
                result["log_loss"] = float(
                    log_loss(y_true, proba_arr, labels=list(range(n_classes)))
                )
                result["brier_score"] = float(np.mean([
                    np.mean(((y_true == c).astype(float) - proba_arr[:, c]) ** 2)
                    for c in range(n_classes)
                ]))
            except Exception:
                pass
        return result

    def confidence_histogram(self, bins: int = 20) -> Dict[str, Any]:
        conf = np.array([p.confidence for p in self.predictions])
        if len(conf) == 0:
            return {"bins": [], "counts": [], "accuracies": []}
        hist, edges = np.histogram(conf, bins=bins, range=(0.0, 1.0))

        # Per-bucket accuracy for reliability diagram
        resolved = [p for p in self.predictions if p.resolved]
        accuracies = []
        for i in range(bins):
            lo, hi = edges[i], edges[i + 1]
            bucket = [
                p for p in resolved
                if lo <= p.confidence < hi
            ]
            if bucket:
                acc = float(sum(1 for p in bucket if p.correct) / len(bucket))
            else:
                acc = float("nan")
            accuracies.append(acc)

        return {
            "bins": [(float(edges[i]), float(edges[i + 1])) for i in range(bins)],
            "counts": hist.tolist(),
            "accuracies": accuracies,
        }

    def class_distribution(self) -> Dict[str, int]:
        out: Dict[str, int] = {}
        for p in self.predictions:
            key = p.predicted_class_name or str(p.predicted_class)
            out[key] = out.get(key, 0) + 1
        return out

    def accuracy_trend(self, window: int = 50) -> List[Dict[str, Any]]:
        resolved = [p for p in self.predictions if p.resolved]
        if len(resolved) < 5:
            return []
        out: List[Dict[str, Any]] = []
        step = max(window // 8, 1)
        for i in range(window, len(resolved) + 1, step):
            chunk = resolved[max(0, i - window): i]
            y_t = [p.actual_class for p in chunk]
            y_p = [p.predicted_class for p in chunk]
            out.append({
                "timestamp": chunk[-1].timestamp,
                "accuracy": float(accuracy_score(y_t, y_p)),
                "f1_macro": float(
                    f1_score(y_t, y_p, average="macro", zero_division=0)
                ),
                "precision": float(
                    precision_score(y_t, y_p, average="macro", zero_division=0)
                ),
                "recall": float(
                    recall_score(y_t, y_p, average="macro", zero_division=0)
                ),
                "n": len(chunk),
            })
        return out

    def recent(self, n: int = 50) -> List[Dict[str, Any]]:
        items = list(self.predictions)[-n:]
        items.reverse()
        return [p.to_dict() for p in items]

    def counts(self) -> Dict[str, int]:
        total = len(self.predictions)
        resolved = sum(1 for p in self.predictions if p.resolved)
        correct = sum(1 for p in self.predictions if p.resolved and p.correct)
        return {
            "total": total,
            "resolved": resolved,
            "pending": total - resolved,
            "correct": correct,
            "incorrect": resolved - correct,
        }

    # ---------------------------------------------------------------- helpers
    @staticmethod
    def _empty_metrics() -> Dict[str, float]:
        return {
            "accuracy": 0.0,
            "balanced_accuracy": 0.0,
            "precision_macro": 0.0,
            "recall_macro": 0.0,
            "precision_weighted": 0.0,
            "recall_weighted": 0.0,
            "f1_macro": 0.0,
            "f1_weighted": 0.0,
            "mcc": 0.0,
            "cohen_kappa": 0.0,
            "log_loss": float("nan"),
            "brier_score": float("nan"),
            "count": 0.0,
        }


__all__ = ["PerformanceTracker", "PredictionLog"]
