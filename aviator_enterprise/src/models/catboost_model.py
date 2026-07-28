"""
CatBoost classifier
===================
"""
from __future__ import annotations

from typing import Any, Dict, List

import numpy as np
from catboost import CatBoostClassifier

from src.models.base import BaseModel


class CatBoostModel(BaseModel):
    name = "catboost"
    family = "gradient_boosting"

    def __init__(self, **hp: Any) -> None:
        defaults = dict(
            iterations=800,
            depth=8,
            learning_rate=0.05,
            l2_leaf_reg=3.0,
            loss_function="MultiClass",
            eval_metric="MultiClass",
            random_seed=42,
            early_stopping_rounds=40,
            verbose=False,
            thread_count=-1,
        )
        defaults.update(hp)
        super().__init__(**defaults)

    def _build(self):
        return CatBoostClassifier(
            iterations=self.hyperparams["iterations"],
            depth=self.hyperparams["depth"],
            learning_rate=self.hyperparams["learning_rate"],
            l2_leaf_reg=self.hyperparams["l2_leaf_reg"],
            loss_function=self.hyperparams["loss_function"],
            eval_metric=self.hyperparams["eval_metric"],
            random_seed=self.hyperparams["random_seed"],
            early_stopping_rounds=self.hyperparams.get("early_stopping_rounds"),
            verbose=self.hyperparams["verbose"],
            thread_count=self.hyperparams["thread_count"],
        )

    def _fit(self, X, y, **kwargs) -> Dict[str, List[float]]:
        eval_set = kwargs.get("eval_set")
        fit_kwargs: Dict[str, Any] = {}
        if eval_set is not None:
            fit_kwargs["eval_set"] = eval_set
            fit_kwargs["use_best_model"] = True
        self.model.fit(X, y, **fit_kwargs)
        train_acc = float(self.model.score(X, y))
        history: Dict[str, List[float]] = {"train_accuracy": [train_acc]}
        if hasattr(self.model, "get_evals_result"):
            res = self.model.get_evals_result()
            for k, v in res.items():
                for metric, vals in v.items():
                    history[f"{k}_{metric}"] = [float(x) for x in vals]
        return history

    def _predict_proba(self, X) -> np.ndarray:
        return self.model.predict_proba(X)

    def get_feature_importance(self) -> np.ndarray:
        if self.model is None:
            return None
        return np.asarray(self.model.get_feature_importance(), dtype=np.float32)

