"""
Probability Calibration
=======================

Wraps a trained `BaseModel` with a calibrator that maps raw probabilities
to well-calibrated ones.  Three methods are supported:
  * `platt`     — Platt Scaling (multinomial logistic regression on logits)
  * `temperature` — Temperature Scaling (single scalar, logit division)
  * `isotonic`  — Isotonic Regression (per-class)

The calibrator is fit on a held-out validation split (default 20%) and
stored alongside the model so it can be re-applied at inference time.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional, Union

import joblib
import numpy as np
from sklearn.isotonic import IsotonicRegression
from sklearn.linear_model import LogisticRegression

from config.settings import settings
from src.core.logger import get_logger

logger = get_logger(__name__)


class Calibrator:
    """Wraps a model + optional calibrator."""

    def __init__(self, method: str = "temperature") -> None:
        if method not in ("platt", "temperature", "isotonic", "none"):
            raise ValueError(f"Unknown calibration method: {method}")
        self.method = method
        self.T: float = 1.0
        self.platt_model: Optional[LogisticRegression] = None
        self.isotonic_models: list = []
        self._fitted = False

    # ---------------------------------------------------------- fit
    def fit(self, proba: np.ndarray, y: np.ndarray) -> "Calibrator":
        if self.method == "none":
            self._fitted = True
            return self
        if self.method == "temperature":
            self.T = self._fit_temperature(proba, y)
        elif self.method == "platt":
            self.platt_model = self._fit_platt(proba, y)
        elif self.method == "isotonic":
            self.isotonic_models = self._fit_isotonic(proba, y)
        self._fitted = True
        return self

    # ---------------------------------------------------------- apply
    def transform(self, proba: np.ndarray) -> np.ndarray:
        if not self._fitted or self.method == "none":
            return self._normalize(proba)
        if self.method == "temperature":
            return self._apply_temperature(proba, self.T)
        if self.method == "platt" and self.platt_model is not None:
            logit = np.log(np.clip(proba, 1e-9, 1.0))
            return self.platt_model.predict_proba(logit)
        if self.method == "isotonic" and self.isotonic_models:
            out = np.zeros_like(proba)
            for c, model in enumerate(self.isotonic_models):
                if model is not None:
                    out[:, c] = model.predict(proba[:, c])
                else:
                    out[:, c] = proba[:, c]
            return self._normalize(out)
        return self._normalize(proba)

    # ---------------------------------------------------------- I/O
    def save(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        joblib.dump({
            "method": self.method,
            "T": self.T,
            "platt_model": self.platt_model,
            "isotonic_models": self.isotonic_models,
            "_fitted": self._fitted,
        }, path, compress=3)
        return path

    @classmethod
    def load(cls, path: Path) -> "Calibrator":
        data = joblib.load(path)
        c = cls(method=data["method"])
        c.T = data["T"]
        c.platt_model = data["platt_model"]
        c.isotonic_models = data["isotonic_models"]
        c._fitted = data["_fitted"]
        return c

    # ---------------------------------------------------------- internals
    @staticmethod
    def _normalize(p: np.ndarray) -> np.ndarray:
        p = np.clip(p, 1e-9, 1.0)
        return p / p.sum(axis=-1, keepdims=True)

    def _fit_temperature(self, proba: np.ndarray, y: np.ndarray) -> float:
        # binary search for T that minimises NLL on logits
        logit = np.log(np.clip(proba, 1e-9, 1.0))
        best_T, best_nll = 1.0, float("inf")
        for T in np.linspace(0.5, 5.0, 46):
            scaled = self._softmax(logit / T)
            nll = self._nll(scaled, y)
            if nll < best_nll:
                best_nll, best_T = nll, T
        return float(best_T)

    def _apply_temperature(self, proba: np.ndarray, T: float) -> np.ndarray:
        logit = np.log(np.clip(proba, 1e-9, 1.0))
        return self._softmax(logit / T)

    def _fit_platt(self, proba: np.ndarray, y: np.ndarray) -> LogisticRegression:
        logit = np.log(np.clip(proba, 1e-9, 1.0))
        clf = LogisticRegression(max_iter=2000, multi_class="multinomial", C=1.0)
        clf.fit(logit, y)
        return clf

    def _fit_isotonic(self, proba: np.ndarray, y: np.ndarray) -> list:
        n_classes = proba.shape[1]
        models = []
        for c in range(n_classes):
            y_bin = (y == c).astype(int)
            if y_bin.sum() < 5 or (1 - y_bin).sum() < 5:
                models.append(None)
                continue
            iso = IsotonicRegression(out_of_bounds="clip", y_min=0.0, y_max=1.0)
            iso.fit(proba[:, c], y_bin)
            models.append(iso)
        return models

    @staticmethod
    def _softmax(x: np.ndarray) -> np.ndarray:
        e = np.exp(x - x.max(axis=-1, keepdims=True))
        return e / e.sum(axis=-1, keepdims=True)

    @staticmethod
    def _nll(proba: np.ndarray, y: np.ndarray) -> float:
        eps = 1e-12
        return float(-np.mean(np.log(np.clip(proba[np.arange(len(y)), y], eps, 1.0))))


__all__ = ["Calibrator"]

