"""
Model abstractions and contracts
================================

All models in the platform implement the same `BaseModel` interface so the
training, evaluation, registry and prediction services can treat them
uniformly.
"""
from __future__ import annotations

import abc
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

import joblib
import numpy as np
import pandas as pd

from config.settings import settings
from src.core.exceptions import ModelNotTrainedError, ModelLoadError
from src.core.logger import get_logger
from src.core.helpers import utc_now_iso

ArrayLike = Union[np.ndarray, pd.DataFrame, pd.Series, List]


@dataclass
class PredictionResult:
    """Standardised output of `BaseModel.predict`."""
    prediction: int
    probabilities: np.ndarray
    confidence: float
    class_names: List[str]
    model_name: str
    model_version: str
    uncertainty: float = 0.0
    extra: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "prediction": int(self.prediction),
            "prediction_class": self.class_names[int(self.prediction)] if 0 <= int(self.prediction) < len(self.class_names) else str(self.prediction),
            "probabilities": {c: float(p) for c, p in zip(self.class_names, self.probabilities)},
            "confidence": float(self.confidence),
            "uncertainty": float(self.uncertainty),
            "model_name": self.model_name,
            "model_version": self.model_version,
            **self.extra,
        }


# ---------------------------------------------------------------------------
# Base class
# ---------------------------------------------------------------------------
class BaseModel(abc.ABC):
    """Abstract base for all models in the platform."""

    name: str = "base"
    family: str = "base"
    supports_proba: bool = True
    needs_sequence: bool = False  # if True, expects 3-D input (samples, timesteps, features)

    def __init__(
        self,
        *,
        n_classes: int = 5,
        class_names: Optional[List[str]] = None,
        random_state: int = 42,
        **hyperparams: Any,
    ) -> None:
        self.n_classes = n_classes
        self.class_names = class_names or settings.category_name_list()
        self.random_state = random_state
        self.hyperparams: Dict[str, Any] = hyperparams
        self.model: Any = None
        self.is_trained: bool = False
        self.version: str = "v1"
        self.created_at: str = utc_now_iso()
        self.training_history: Dict[str, List[float]] = {}
        self.logger = get_logger(f"{self.__class__.__name__}")

    # -------------------------------------------------------------- ABC
    @abc.abstractmethod
    def _build(self) -> Any:
        ...

    @abc.abstractmethod
    def _fit(self, X: np.ndarray, y: np.ndarray, **kwargs: Any) -> Dict[str, List[float]]:
        ...

    @abc.abstractmethod
    def _predict_proba(self, X: np.ndarray) -> np.ndarray:
        ...

    # -------------------------------------------------------------- public
    def fit(self, X: ArrayLike, y: ArrayLike, **kwargs: Any) -> "BaseModel":
        X_arr = self._coerce_X(X)
        y_arr = self._coerce_y(y)
        if self.model is None:
            self.model = self._build()
        history = self._fit(X_arr, y_arr, **kwargs)
        self.training_history = history
        self.is_trained = True
        self.version = self._bump_version()
        return self

    def predict(self, X: ArrayLike) -> PredictionResult:
        if not self.is_trained:
            raise ModelNotTrainedError(f"{self.name} has not been trained yet")
        X_arr = self._coerce_X(X)
        proba = self._predict_proba(X_arr)
        if proba.ndim == 1:
            proba = np.vstack([1 - proba, proba]).T
        proba = self._normalize_proba(proba)
        pred = int(np.argmax(proba[0]))
        confidence = float(proba[0, pred])
        uncertainty = self._compute_uncertainty(proba[0])
        return PredictionResult(
            prediction=pred,
            probabilities=proba[0],
            confidence=confidence,
            class_names=self.class_names,
            model_name=self.name,
            model_version=self.version,
            uncertainty=uncertainty,
        )

    def predict_proba(self, X: ArrayLike) -> np.ndarray:
        if not self.is_trained:
            raise ModelNotTrainedError(f"{self.name} has not been trained yet")
        X_arr = self._coerce_X(X)
        return self._normalize_proba(self._predict_proba(X_arr))

    # -------------------------------------------------------------- I/O
    def save(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        if self.model is None:
            raise ModelNotTrainedError("Cannot save an untrained model")
        payload = {
            "model": self.model,
            "metadata": {
                "name": self.name,
                "family": self.family,
                "version": self.version,
                "created_at": self.created_at,
                "n_classes": self.n_classes,
                "class_names": self.class_names,
                "random_state": self.random_state,
                "hyperparams": self.hyperparams,
                "training_history": self.training_history,
            },
        }
        joblib.dump(payload, path, compress=3)
        self.logger.info("Model saved", extra={"path": str(path)})
        return path

    def load(self, path: Path) -> "BaseModel":
        if not path.exists():
            raise ModelLoadError(f"Model file not found: {path}")
        try:
            payload = joblib.load(path)
        except Exception as exc:
            raise ModelLoadError(f"Failed to load model: {exc}") from exc
        self.model = payload["model"]
        meta = payload.get("metadata", {})
        self.version = meta.get("version", "v1")
        self.created_at = meta.get("created_at", utc_now_iso())
        self.n_classes = meta.get("n_classes", self.n_classes)
        self.class_names = meta.get("class_names", self.class_names)
        self.hyperparams = meta.get("hyperparams", self.hyperparams)
        self.training_history = meta.get("training_history", {})
        self.is_trained = True
        self.logger.info("Model loaded", extra={"path": str(path), "version": self.version})
        return self

    # -------------------------------------------------------------- utils
    def _coerce_X(self, X: ArrayLike) -> np.ndarray:
        if isinstance(X, pd.DataFrame):
            return X.values.astype(np.float32)
        if isinstance(X, pd.Series):
            return X.values.reshape(1, -1).astype(np.float32)
        arr = np.asarray(X, dtype=np.float32)
        if arr.ndim == 1:
            arr = arr.reshape(1, -1)
        return arr

    def _coerce_y(self, y: ArrayLike) -> np.ndarray:
        if isinstance(y, (pd.Series, pd.DataFrame)):
            return np.asarray(y).ravel().astype(np.int64)
        arr = np.asarray(y).astype(np.int64)
        if arr.ndim > 1:
            arr = arr.ravel()
        return arr

    @staticmethod
    def _normalize_proba(proba: np.ndarray) -> np.ndarray:
        proba = np.clip(proba, 1e-9, 1.0)
        proba = proba / proba.sum(axis=-1, keepdims=True)
        return proba

    @staticmethod
    def _compute_uncertainty(proba_row: np.ndarray) -> float:
        # Normalised entropy 0..1
        p = proba_row / max(proba_row.sum(), 1e-9)
        p = p[p > 0]
        ent = float(-np.sum(p * np.log2(p)))
        return ent / max(np.log2(len(proba_row)), 1.0)

    def _bump_version(self) -> str:
        try:
            n = int(self.version.lstrip("v"))
            return f"v{n + 1}"
        except ValueError:
            return "v1"

    def get_feature_importance(self) -> Optional[np.ndarray]:
        """Override in tree-based models."""
        return None

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__} name={self.name} version={self.version} trained={self.is_trained}>"


__all__ = ["BaseModel", "PredictionResult"]

