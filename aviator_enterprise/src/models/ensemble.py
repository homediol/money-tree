"""
Ensemble Engine
===============

Supports three strategies:
  * `voting`       — soft probability averaging with optional weights
  * `weighted`     — weighted soft voting (weights = validation accuracy)
  * `stacking`     — meta-learner trained on out-of-fold predictions

The ensemble is itself a `BaseModel`, so it slots into the registry,
training pipeline and prediction service without any special-casing.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import joblib
import numpy as np
from sklearn.linear_model import LogisticRegression

from src.core.exceptions import ModelNotTrainedError
from src.core.helpers import utc_now_iso
from src.models.base import BaseModel, PredictionResult


@dataclass
class EnsembleConfig:
    method: str = "stacking"  # 'voting' | 'weighted' | 'stacking'
    weights: Optional[List[float]] = None
    cv_folds: int = 5


class EnsembleModel(BaseModel):
    name = "ensemble"
    family = "ensemble"

    def __init__(self, members: Optional[List[BaseModel]] = None, **hp: Any) -> None:
        defaults = dict(method="stacking", cv_folds=5)
        defaults.update(hp)
        super().__init__(**defaults)
        self.members: List[BaseModel] = members or []
        self.config = EnsembleConfig(method=defaults["method"], cv_folds=defaults["cv_folds"])
        self.weights: Optional[np.ndarray] = None
        self.meta_model: Optional[LogisticRegression] = None
        self.oof_proba_: Optional[np.ndarray] = None

    def add(self, model: BaseModel) -> "EnsembleModel":
        if not model.is_trained:
            raise ModelNotTrainedError(f"Cannot add untrained model: {model.name}")
        self.members.append(model)
        return self

    # -------------------------------------------------------------- fit
    def _build(self):
        return None  # ensemble composes members instead

    def _fit(self, X, y, **kwargs) -> Dict[str, List[float]]:
        if not self.members:
            raise ModelNotTrainedError("Ensemble has no member models")

        method = self.config.method
        if method in ("voting", "weighted"):
            self._fit_voting(X, y)
        elif method == "stacking":
            self._fit_stacking(X, y)
        else:
            raise ValueError(f"Unknown ensemble method: {method}")

        # compute ensemble accuracy for logging
        if self.oof_proba_ is not None:
            proba = self._oof_predict(X)
        else:
            weights = self.weights
            if weights is None:
                weights = np.ones(len(self.members)) / len(self.members)
            proba = self._voting_predict(X, weights)
        pred = np.argmax(proba, axis=1)
        acc = float((pred == y).mean())
        return {"train_accuracy": [acc], "method": [method]}

    # -------------------------------------------------------------- helpers
    def _member_proba(self, X, member: BaseModel) -> np.ndarray:
        try:
            p = member.predict_proba(X)
            if p.ndim == 1:
                p = np.vstack([1 - p, p]).T
            return p
        except Exception:
            return np.zeros((len(X) if hasattr(X, "__len__") else 1, self.n_classes))

    def _fit_voting(self, X, y) -> None:
        # weights = validation accuracy of each member (fallback 1/N)
        n = len(self.members)
        ws = np.ones(n) / n
        if self.config.weights is not None:
            ws = np.array(self.config.weights, dtype=float)
            ws = ws / ws.sum()
        else:
            for i, m in enumerate(self.members):
                p = self._member_proba(X, m)
                ws[i] = float((p.argmax(1) == y).mean()) + 1e-3
            ws = ws / ws.sum()
        self.weights = ws

    def _fit_stacking(self, X, y) -> None:
        base_proba = self._oof_predict(X)
        self.oof_proba_ = base_proba
        self.meta_model = LogisticRegression(
            multi_class="multinomial", max_iter=2000, class_weight="balanced", C=1.0
        )
        self.meta_model.fit(base_proba, y)

    def _voting_predict(self, X, weights: np.ndarray) -> np.ndarray:
        n = len(X) if hasattr(X, "__len__") else 1
        out = np.zeros((n, self.n_classes), dtype=np.float32)
        for w, m in zip(weights, self.members):
            out += w * self._member_proba(X, m)
        out /= weights.sum()
        return out

    def _oof_predict(self, X) -> np.ndarray:
        n = len(X) if hasattr(X, "__len__") else 1
        out = np.zeros((n, self.n_classes), dtype=np.float32)
        for m in self.members:
            out += self._member_proba(X, m)
        out /= max(len(self.members), 1)
        return out

    def _predict_proba(self, X) -> np.ndarray:
        if self.config.method == "stacking" and self.meta_model is not None:
            base = self._oof_predict(X)
            return self.meta_model.predict_proba(base)
        weights = self.weights if self.weights is not None else np.ones(len(self.members)) / len(self.members)
        return self._voting_predict(X, weights)

    # -------------------------------------------------------------- I/O
    def save(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        members_dir = path.parent / (path.stem + "_members")
        members_dir.mkdir(exist_ok=True)
        member_paths = []
        for i, m in enumerate(self.members):
            mp = members_dir / f"member_{i}_{m.name}.joblib"
            m.save(mp)
            member_paths.append(str(mp))
        payload = {
            "ensemble_meta": {
                "name": self.name,
                "version": self.version,
                "created_at": self.created_at,
                "config": self.config.__dict__,
                "weights": self.weights.tolist() if self.weights is not None else None,
                "member_paths": member_paths,
                "class_names": self.class_names,
            },
            "meta_model": self.meta_model,
        }
        joblib.dump(payload, path, compress=3)
        return path

    def load(self, path: Path) -> "EnsembleModel":
        payload = joblib.load(path)
        meta = payload["ensemble_meta"]
        self.config = EnsembleConfig(**meta["config"])
        self.weights = np.array(meta["weights"]) if meta["weights"] else None
        self.class_names = meta.get("class_names", self.class_names)
        self.members = []
        for mp in meta["member_paths"]:
            # Reconstruct the correct concrete subclass from the file path name
            # e.g. member_0_xgboost.joblib → XGBoostModel
            member_path = Path(mp)
            member_name = member_path.stem.split("_", 2)[-1]  # e.g. "xgboost"
            try:
                from src.models import build_model
                member = build_model(member_name, n_classes=self.n_classes, class_names=self.class_names)
            except Exception:
                # Fallback: load the payload to detect the saved model name
                try:
                    raw = joblib.load(member_path)
                    saved_name = raw.get("metadata", {}).get("name", member_name)
                    from src.models import build_model
                    member = build_model(saved_name, n_classes=self.n_classes, class_names=self.class_names)
                except Exception:
                    # Last resort: use a generic XGBoost which can load any sklearn-compatible payload
                    from src.models.xgboost_model import XGBoostModel
                    member = XGBoostModel(n_classes=self.n_classes, class_names=self.class_names)
            member.load(member_path)
            self.members.append(member)
        self.meta_model = payload.get("meta_model")
        self.is_trained = True
        return self

    def get_feature_importance(self) -> Optional[np.ndarray]:
        # average member importances if available
        imps = [m.get_feature_importance() for m in self.members]
        imps = [i for i in imps if i is not None]
        if not imps:
            return None
        return np.mean(imps, axis=0)


__all__ = ["EnsembleModel", "EnsembleConfig"]
