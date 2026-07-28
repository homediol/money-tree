"""
Logistic Regression (multinomial)
=================================
"""
from __future__ import annotations

from typing import Any, Dict, List

import numpy as np
from sklearn.linear_model import LogisticRegression

from src.models.base import BaseModel


class LogisticRegressionModel(BaseModel):
    name = "logistic_regression"
    family = "linear"

    def __init__(self, **hp: Any) -> None:
        defaults = dict(
            penalty="l2",
            C=1.0,
            solver="lbfgs",
            max_iter=2000,
            class_weight="balanced",
            multi_class="multinomial",
            random_state=42,
            n_jobs=-1,
        )
        defaults.update(hp)
        super().__init__(**defaults)

    def _build(self):
        return LogisticRegression(
            penalty=self.hyperparams["penalty"],
            C=self.hyperparams["C"],
            solver=self.hyperparams["solver"],
            max_iter=self.hyperparams["max_iter"],
            class_weight=self.hyperparams.get("class_weight"),
            multi_class=self.hyperparams["multi_class"],
            random_state=self.hyperparams["random_state"],
            n_jobs=self.hyperparams["n_jobs"],
        )

    def _fit(self, X, y, **kwargs) -> Dict[str, List[float]]:
        self.model.fit(X, y)
        return {"train_accuracy": [float(self.model.score(X, y))]}

    def _predict_proba(self, X) -> np.ndarray:
        return self.model.predict_proba(X)

