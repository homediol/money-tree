"""
XGBoost classifier
==================
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import numpy as np
import xgboost as xgb
from sklearn.utils.class_weight import compute_sample_weight

from src.models.base import BaseModel


class XGBoostModel(BaseModel):
    name = "xgboost"
    family = "gradient_boosting"

    def __init__(self, **hp: Any) -> None:
        defaults = dict(
            n_estimators=600,
            max_depth=8,
            learning_rate=0.05,
            subsample=0.85,
            colsample_bytree=0.85,
            reg_alpha=0.1,
            reg_lambda=1.0,
            gamma=0.1,
            min_child_weight=3,
            tree_method="hist",
            device="cpu",
            objective="multi:softprob",
            eval_metric="mlogloss",
            early_stopping_rounds=30,
            verbosity=0,
            random_state=42,
        )
        defaults.update(hp)
        super().__init__(**defaults)

    def _build(self):
        return xgb.XGBClassifier(
            n_estimators=self.hyperparams["n_estimators"],
            max_depth=self.hyperparams["max_depth"],
            learning_rate=self.hyperparams["learning_rate"],
            subsample=self.hyperparams["subsample"],
            colsample_bytree=self.hyperparams["colsample_bytree"],
            reg_alpha=self.hyperparams["reg_alpha"],
            reg_lambda=self.hyperparams["reg_lambda"],
            gamma=self.hyperparams["gamma"],
            min_child_weight=self.hyperparams["min_child_weight"],
            tree_method=self.hyperparams["tree_method"],
            device=self.hyperparams["device"],
            objective=self.hyperparams["objective"],
            eval_metric=self.hyperparams["eval_metric"],
            early_stopping_rounds=self.hyperparams.get("early_stopping_rounds"),
            verbosity=self.hyperparams["verbosity"],
            random_state=self.hyperparams.get("random_state", 42),
            num_class=self.n_classes,
        )

    def _fit(self, X, y, **kwargs) -> Dict[str, List[float]]:
        eval_set = kwargs.get("eval_set")
        sample_weight = kwargs.get("sample_weight")
        if sample_weight is None and self.hyperparams.get("class_weight") == "balanced":
            sample_weight = compute_sample_weight(class_weight="balanced", y=y)
        fit_kwargs = {"sample_weight": sample_weight} if sample_weight is not None else {}
        if eval_set is not None:
            fit_kwargs["eval_set"] = eval_set
            fit_kwargs["verbose"] = False
        self.model.fit(X, y, **fit_kwargs)
        train_acc = float(self.model.score(X, y))
        history: Dict[str, List[float]] = {"train_accuracy": [train_acc]}
        if hasattr(self.model, "evals_result") and self.model.evals_result():
            for k, v in self.model.evals_result().items():
                for metric, vals in v.items():
                    history[f"{k}_{metric}"] = [float(x) for x in vals]
        return history

    def _predict_proba(self, X) -> np.ndarray:
        return self.model.predict_proba(X)

    def get_feature_importance(self) -> np.ndarray:
        if self.model is None:
            return None
        return np.asarray(self.model.feature_importances_, dtype=np.float32)

