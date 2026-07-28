"""
Random Forest classifier
========================
"""
from __future__ import annotations

from typing import Any, Dict, List

import numpy as np
from sklearn.ensemble import RandomForestClassifier

from src.models.base import BaseModel


class RandomForestModel(BaseModel):
    name = "random_forest"
    family = "tree"

    def __init__(self, **hp: Any) -> None:
        defaults = dict(
            n_estimators=400,
            max_depth=18,
            min_samples_split=4,
            min_samples_leaf=2,
            max_features="sqrt",
            class_weight="balanced_subsample",
            n_jobs=-1,
            random_state=42,
        )
        defaults.update(hp)
        super().__init__(**defaults)

    def _build(self):
        return RandomForestClassifier(
            n_estimators=self.hyperparams["n_estimators"],
            max_depth=self.hyperparams["max_depth"],
            min_samples_split=self.hyperparams["min_samples_split"],
            min_samples_leaf=self.hyperparams["min_samples_leaf"],
            max_features=self.hyperparams["max_features"],
            class_weight=self.hyperparams.get("class_weight"),
            n_jobs=self.hyperparams.get("n_jobs", -1),
            random_state=self.hyperparams.get("random_state", 42),
        )

    def _fit(self, X, y, **kwargs) -> Dict[str, List[float]]:
        self.model.fit(X, y)
        train_acc = float(self.model.score(X, y))
        return {"train_accuracy": [train_acc]}

    def _predict_proba(self, X) -> np.ndarray:
        return self.model.predict_proba(X)

    def get_feature_importance(self) -> np.ndarray:
        if self.model is None:
            return None
        return np.asarray(self.model.feature_importances_, dtype=np.float32)

