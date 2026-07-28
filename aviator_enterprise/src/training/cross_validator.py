"""
Cross-Validation Utilities
==========================

Provides stratified k-fold CV with bootstrap confidence intervals and
out-of-fold (OOF) predictions.  All metrics returned are model-agnostic
and plug into the evaluation service.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
from sklearn.metrics import (
    accuracy_score, balanced_accuracy_score, f1_score, log_loss,
    matthews_corrcoef, cohen_kappa_score, roc_auc_score,
    average_precision_score, brier_score_loss,
    precision_score, recall_score, top_k_accuracy_score,
)
from sklearn.model_selection import StratifiedKFold

from config.settings import settings
from src.core.logger import get_logger
from src.models.base import BaseModel

logger = get_logger(__name__)


@dataclass
class FoldResult:
    fold: int
    metrics: Dict[str, float]
    oof_predictions: np.ndarray
    oof_probabilities: np.ndarray
    oof_indices: np.ndarray


@dataclass
class CrossValidationReport:
    n_folds: int
    fold_metrics: List[Dict[str, float]]
    mean_metrics: Dict[str, float]
    std_metrics: Dict[str, float]
    oof_predictions: np.ndarray
    oof_probabilities: np.ndarray
    oof_indices: np.ndarray
    bootstrap_ci: Dict[str, Tuple[float, float]] = field(default_factory=dict)


class CrossValidator:
    """Stratified K-Fold with rich metrics + bootstrap CI."""

    def __init__(self, n_folds: int = 5, n_bootstrap: int = 200, random_state: int = 42) -> None:
        self.n_folds = n_folds
        self.n_bootstrap = n_bootstrap
        self.random_state = random_state
        self.logger = get_logger(self.__class__.__name__)

    def run(self, model: BaseModel, X: np.ndarray, y: np.ndarray) -> CrossValidationReport:
        skf = StratifiedKFold(n_splits=self.n_folds, shuffle=True, random_state=self.random_state)
        folds: List[FoldResult] = []
        all_oof_pred = np.zeros(len(y), dtype=np.int64)
        all_oof_proba = np.zeros((len(y), model.n_classes), dtype=np.float32)
        all_idx = np.zeros(len(y), dtype=np.int64)

        for i, (train_idx, val_idx) in enumerate(skf.split(np.zeros(len(y)), y)):
            # Reconstruct model preserving n_classes and class_names to avoid default mismatch
            init_kwargs = dict(model.hyperparams)
            init_kwargs.setdefault("n_classes", model.n_classes)
            init_kwargs.setdefault("class_names", model.class_names)
            m = type(model)(**init_kwargs)
            m.fit(X[train_idx], y[train_idx])
            proba = m.predict_proba(X[val_idx])
            pred = proba.argmax(1)
            metrics = self._compute_metrics(y[val_idx], pred, proba)
            folds.append(FoldResult(
                fold=i, metrics=metrics,
                oof_predictions=pred, oof_probabilities=proba, oof_indices=val_idx,
            ))
            all_oof_pred[val_idx] = pred
            all_oof_proba[val_idx] = proba
            all_idx[val_idx] = val_idx

        mean_metrics, std_metrics = self._aggregate(folds)
        ci = self._bootstrap_ci(all_oof_pred, y, all_oof_proba)

        return CrossValidationReport(
            n_folds=self.n_folds,
            fold_metrics=[f.metrics for f in folds],
            mean_metrics=mean_metrics,
            std_metrics=std_metrics,
            oof_predictions=all_oof_pred,
            oof_probabilities=all_oof_proba,
            oof_indices=all_idx,
            bootstrap_ci=ci,
        )

    # ---------------------------------------------------------- helpers
    def _compute_metrics(self, y_true: np.ndarray, y_pred: np.ndarray, proba: np.ndarray) -> Dict[str, float]:
        n_classes = proba.shape[1]
        out: Dict[str, float] = {}
        out["accuracy"] = float(accuracy_score(y_true, y_pred))
        out["balanced_accuracy"] = float(balanced_accuracy_score(y_true, y_pred))
        out["precision_macro"] = float(precision_score(y_true, y_pred, average="macro", zero_division=0))
        out["recall_macro"] = float(recall_score(y_true, y_pred, average="macro", zero_division=0))
        out["f1_macro"] = float(f1_score(y_true, y_pred, average="macro", zero_division=0))
        out["precision_weighted"] = float(precision_score(y_true, y_pred, average="weighted", zero_division=0))
        out["recall_weighted"] = float(recall_score(y_true, y_pred, average="weighted", zero_division=0))
        out["f1_weighted"] = float(f1_score(y_true, y_pred, average="weighted", zero_division=0))
        try:
            out["mcc"] = float(matthews_corrcoef(y_true, y_pred))
        except Exception:
            out["mcc"] = 0.0
        try:
            out["cohen_kappa"] = float(cohen_kappa_score(y_true, y_pred))
        except Exception:
            out["cohen_kappa"] = 0.0
        try:
            out["log_loss"] = float(log_loss(y_true, proba, labels=list(range(n_classes))))
        except Exception:
            out["log_loss"] = float("nan")
        try:
            if n_classes == 2:
                out["roc_auc"] = float(roc_auc_score(y_true, proba[:, 1]))
                out["pr_auc"] = float(average_precision_score(y_true, proba[:, 1]))
                out["brier_score"] = float(brier_score_loss(y_true, proba[:, 1]))
            else:
                out["roc_auc"] = float(roc_auc_score(y_true, proba, multi_class="ovr", labels=list(range(n_classes))))
                out["pr_auc"] = float(average_precision_score(y_true, proba, average="macro"))
                out["brier_score"] = float(np.mean([
                    brier_score_loss((y_true == c).astype(int), proba[:, c]) for c in range(n_classes)
                ]))
        except Exception:
            out["roc_auc"] = float("nan")
            out["pr_auc"] = float("nan")
            out["brier_score"] = float("nan")
        try:
            out["top_2_accuracy"] = float(top_k_accuracy_score(y_true, proba, k=2, labels=list(range(n_classes))))
        except Exception:
            out["top_2_accuracy"] = float("nan")
        return out

    @staticmethod
    def _aggregate(folds: List[FoldResult]) -> Tuple[Dict[str, float], Dict[str, float]]:
        if not folds:
            return {}, {}
        keys = list(folds[0].metrics.keys())
        mean = {k: float(np.mean([f.metrics[k] for f in folds])) for k in keys}
        std = {k: float(np.std([f.metrics[k] for f in folds])) for k in keys}
        return mean, std

    def _bootstrap_ci(self, y_pred: np.ndarray, y_true: np.ndarray, proba: np.ndarray, alpha: float = 0.05) -> Dict[str, Tuple[float, float]]:
        rng = np.random.default_rng(self.random_state)
        n = len(y_true)
        ci: Dict[str, Tuple[float, float]] = {}
        if n < 30:
            return ci
        keys = ["accuracy", "f1_macro", "log_loss", "roc_auc"]
        samples: Dict[str, List[float]] = {k: [] for k in keys}
        for _ in range(self.n_bootstrap):
            idx = rng.integers(0, n, size=n)
            if len(np.unique(y_true[idx])) < 2:
                continue
            yt, yp = y_true[idx], y_pred[idx]
            p = proba[idx]
            samples["accuracy"].append(float(accuracy_score(yt, yp)))
            samples["f1_macro"].append(float(f1_score(yt, yp, average="macro", zero_division=0)))
            try:
                samples["log_loss"].append(float(log_loss(yt, p, labels=list(range(p.shape[1])))))
            except Exception:
                pass
            try:
                samples["roc_auc"].append(float(roc_auc_score(yt, p, multi_class="ovr", labels=list(range(p.shape[1])))))
            except Exception:
                pass
        for k, vals in samples.items():
            if not vals:
                continue
            arr = np.array(vals)
            ci[k] = (float(np.quantile(arr, alpha / 2)), float(np.quantile(arr, 1 - alpha / 2)))
        return ci


__all__ = ["CrossValidator", "CrossValidationReport", "FoldResult"]
