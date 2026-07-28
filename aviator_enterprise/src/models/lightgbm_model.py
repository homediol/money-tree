"""
LightGBM classifier
===================
"""
from __future__ import annotations

from typing import Any, Dict, List

import numpy as np
import lightgbm as lgb
from sklearn.utils.class_weight import compute_sample_weight

from src.models.base import BaseModel


class LightGBMModel(BaseModel):
    name = "lightgbm"
    family = "gradient_boosting"

    def __init__(self, **hp: Any) -> None:
        defaults = dict(
            n_estimators=800,
            learning_rate=0.04,
            num_leaves=63,
            max_depth=-1,
            min_data_in_leaf=20,
            feature_fraction=0.85,
            bagging_fraction=0.85,
            bagging_freq=5,
            lambda_l1=0.1,
            lambda_l2=1.0,
            objective="multiclass",
            metric="multi_logloss",
            random_state=42,
            n_jobs=-1,
            verbose=-1,
        )
        defaults.update(hp)
        super().__init__(**defaults)

    def _build(self):
        return lgb.LGBMClassifier(
            n_estimators=self.hyperparams["n_estimators"],
            learning_rate=self.hyperparams["learning_rate"],
            num_leaves=self.hyperparams["num_leaves"],
            max_depth=self.hyperparams["max_depth"],
            min_data_in_leaf=self.hyperparams["min_data_in_leaf"],
            feature_fraction=self.hyperparams["feature_fraction"],
            bagging_fraction=self.hyperparams["bagging_fraction"],
            bagging_freq=self.hyperparams["bagging_freq"],
            lambda_l1=self.hyperparams["lambda_l1"],
            lambda_l2=self.hyperparams["lambda_l2"],
            objective=self.hyperparams["objective"],
            random_state=self.hyperparams["random_state"],
            n_jobs=self.hyperparams["n_jobs"],
            verbose=self.hyperparams["verbose"],
            num_class=self.n_classes,
        )

    def _fit(self, X, y, **kwargs) -> Dict[str, List[float]]:
        eval_set = kwargs.get("eval_set")
        sample_weight = kwargs.get("sample_weight")
        if sample_weight is None and self.hyperparams.get("class_weight") == "balanced":
            sample_weight = compute_sample_weight(class_weight="balanced", y=y)
        callbacks = [lgb.early_stopping(30, verbose=False), lgb.log_evaluation(period=0)]
        fit_kwargs: Dict[str, Any] = {"callbacks": callbacks}
        if sample_weight is not None:
            fit_kwargs["sample_weight"] = sample_weight
        if eval_set is not None:
            fit_kwargs["eval_set"] = eval_set
        self.model.fit(X, y, **fit_kwargs)
        train_acc = float(self.model.score(X, y))
        history: Dict[str, List[float]] = {"train_accuracy": [train_acc]}
        if hasattr(self.model, "evals_result_") and self.model.evals_result_:
            for k, v in self.model.evals_result_.items():
                for metric, vals in v.items():
                    history[f"{k}_{metric}"] = [float(x) for x in vals]
        return history

    def _predict_proba(self, X) -> np.ndarray:
        return self.model.predict_proba(X)

    def get_feature_importance(self) -> np.ndarray:
        if self.model is None:
            return None
        return np.asarray(self.model.feature_importances_, dtype=np.float32)

