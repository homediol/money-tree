"""
Evaluation Service
==================

Computes a comprehensive set of classification metrics, calibration
metrics and learning-curve diagnostics.  All methods are pure functions
of `(y_true, y_pred, proba)` and produce JSON-serialisable outputs.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
from sklearn.calibration import calibration_curve
from sklearn.metrics import (
    accuracy_score, balanced_accuracy_score, classification_report,
    confusion_matrix, cohen_kappa_score, f1_score, log_loss,
    matthews_corrcoef, precision_recall_curve, precision_score,
    recall_score, roc_auc_score, roc_curve, top_k_accuracy_score,
    average_precision_score, brier_score_loss,
)

from src.core.logger import get_logger

logger = get_logger(__name__)


@dataclass
class MetricsReport:
    accuracy: float
    balanced_accuracy: float
    precision_macro: float
    recall_macro: float
    f1_macro: float
    precision_weighted: float
    recall_weighted: float
    f1_weighted: float
    mcc: float
    cohen_kappa: float
    log_loss: float
    brier_score: float
    roc_auc: float
    pr_auc: float
    top_2_accuracy: float
    top_3_accuracy: float
    per_class: Dict[str, Dict[str, float]]
    n_samples: int
    n_classes: int
    extra: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {k: v for k, v in self.__dict__.items()}


class EvaluationService:
    """Computes every metric the dashboard exposes."""

    def compute(
        self,
        y_true: np.ndarray,
        y_pred: np.ndarray,
        proba: np.ndarray,
        class_names: Optional[List[str]] = None,
    ) -> MetricsReport:
        n_classes = proba.shape[1]
        if class_names is None:
            class_names = [f"class_{i}" for i in range(n_classes)]

        try:
            per_class_raw = classification_report(y_true, y_pred, output_dict=True, zero_division=0)
            per_class = {k: v for k, v in per_class_raw.items() if k not in ("accuracy", "macro avg", "weighted avg")}
            macro = per_class_raw.get("macro avg", {})
            weighted = per_class_raw.get("weighted avg", {})
        except Exception:
            per_class = {}
            macro, weighted = {}, {}

        try:
            mcc = float(matthews_corrcoef(y_true, y_pred))
        except Exception:
            mcc = 0.0
        try:
            kappa = float(cohen_kappa_score(y_true, y_pred))
        except Exception:
            kappa = 0.0
        try:
            ll = float(log_loss(y_true, proba, labels=list(range(n_classes))))
        except Exception:
            ll = float("nan")
        try:
            if n_classes == 2:
                roc = float(roc_auc_score(y_true, proba[:, 1]))
                pr = float(average_precision_score(y_true, proba[:, 1]))
                brier = float(brier_score_loss(y_true, proba[:, 1]))
            else:
                roc = float(roc_auc_score(y_true, proba, multi_class="ovr", labels=list(range(n_classes))))
                pr = float(average_precision_score(y_true, proba, average="macro"))
                brier = float(np.mean([
                    brier_score_loss((y_true == c).astype(int), proba[:, c]) for c in range(n_classes)
                ]))
        except Exception:
            roc = pr = brier = float("nan")

        top2 = top3 = float("nan")
        try:
            top2 = float(top_k_accuracy_score(y_true, proba, k=min(2, n_classes), labels=list(range(n_classes))))
            top3 = float(top_k_accuracy_score(y_true, proba, k=min(3, n_classes), labels=list(range(n_classes))))
        except Exception:
            pass

        return MetricsReport(
            accuracy=float(accuracy_score(y_true, y_pred)),
            balanced_accuracy=float(balanced_accuracy_score(y_true, y_pred)),
            precision_macro=float(macro.get("precision", precision_score(y_true, y_pred, average="macro", zero_division=0))),
            recall_macro=float(macro.get("recall", recall_score(y_true, y_pred, average="macro", zero_division=0))),
            f1_macro=float(macro.get("f1-score", f1_score(y_true, y_pred, average="macro", zero_division=0))),
            precision_weighted=float(weighted.get("precision", 0.0)),
            recall_weighted=float(weighted.get("recall", 0.0)),
            f1_weighted=float(weighted.get("f1-score", 0.0)),
            mcc=mcc,
            cohen_kappa=kappa,
            log_loss=ll,
            brier_score=brier,
            roc_auc=roc,
            pr_auc=pr,
            top_2_accuracy=top2,
            top_3_accuracy=top3,
            per_class=per_class,
            n_samples=len(y_true),
            n_classes=n_classes,
            extra={"class_names": class_names},
        )

    # ----------------------------------------------------------------
    def confusion_matrix(self, y_true: np.ndarray, y_pred: np.ndarray, class_names: Optional[List[str]] = None) -> Dict[str, Any]:
        cm = confusion_matrix(y_true, y_pred)
        if class_names is None:
            class_names = [f"class_{i}" for i in range(cm.shape[0])]
        return {
            "matrix": cm.tolist(),
            "labels": class_names,
            "normalized": (cm.astype(float) / np.maximum(cm.sum(axis=1, keepdims=True), 1)).tolist(),
        }

    def roc_curve(self, y_true: np.ndarray, proba: np.ndarray, n_classes: int) -> Dict[str, Any]:
        if n_classes == 2:
            fpr, tpr, _ = roc_curve(y_true, proba[:, 1])
            return {"fpr": fpr.tolist(), "tpr": tpr.tolist(), "auc": float(roc_auc_score(y_true, proba[:, 1]))}
        out = {"classes": [], "fpr": [], "tpr": [], "auc": []}
        for c in range(n_classes):
            fpr, tpr, _ = roc_curve((y_true == c).astype(int), proba[:, c])
            out["classes"].append(int(c))
            out["fpr"].append(fpr.tolist())
            out["tpr"].append(tpr.tolist())
            try:
                out["auc"].append(float(roc_auc_score((y_true == c).astype(int), proba[:, c])))
            except Exception:
                out["auc"].append(float("nan"))
        return out

    def pr_curve(self, y_true: np.ndarray, proba: np.ndarray, n_classes: int) -> Dict[str, Any]:
        if n_classes == 2:
            precision, recall, _ = precision_recall_curve(y_true, proba[:, 1])
            return {
                "precision": precision.tolist(),
                "recall": recall.tolist(),
                "ap": float(average_precision_score(y_true, proba[:, 1])),
            }
        out = {"classes": [], "precision": [], "recall": [], "ap": []}
        for c in range(n_classes):
            precision, recall, _ = precision_recall_curve((y_true == c).astype(int), proba[:, c])
            out["classes"].append(int(c))
            out["precision"].append(precision.tolist())
            out["recall"].append(recall.tolist())
            try:
                out["ap"].append(float(average_precision_score((y_true == c).astype(int), proba[:, c])))
            except Exception:
                out["ap"].append(float("nan"))
        return out

    def calibration_curve(self, y_true: np.ndarray, proba: np.ndarray, n_classes: int, n_bins: int = 10) -> Dict[str, Any]:
        if n_classes == 2:
            frac_pos, mean_pred = calibration_curve(y_true, proba[:, 1], n_bins=n_bins, strategy="uniform")
            return {"mean_predicted": mean_pred.tolist(), "fraction_positive": frac_pos.tolist()}
        out = {"classes": [], "mean_predicted": [], "fraction_positive": []}
        for c in range(n_classes):
            y_bin = (y_true == c).astype(int)
            if y_bin.sum() == 0:
                continue
            try:
                frac_pos, mean_pred = calibration_curve(y_bin, proba[:, c], n_bins=n_bins, strategy="uniform")
                out["classes"].append(int(c))
                out["mean_predicted"].append(mean_pred.tolist())
                out["fraction_positive"].append(frac_pos.tolist())
            except Exception:
                continue
        return out


__all__ = ["EvaluationService", "MetricsReport"]

