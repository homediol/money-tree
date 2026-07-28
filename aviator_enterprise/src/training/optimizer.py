"""
Hyperparameter Optimisation
============================

Bayesian optimisation via Optuna.  Supports any model registered in
`src.models.MODEL_REGISTRY`; the search space for each model is defined
in `_search_spaces` and is easily extended.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

import numpy as np
import optuna
from sklearn.model_selection import StratifiedKFold
from sklearn.metrics import log_loss

from config.settings import settings
from src.core.exceptions import HyperparameterOptimizationError
from src.core.logger import get_logger
from src.models import build_model, BaseModel

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Search spaces
# ---------------------------------------------------------------------------
def _space_random_forest(trial: optuna.Trial) -> Dict[str, Any]:
    return dict(
        n_estimators=trial.suggest_int("n_estimators", 200, 800, step=100),
        max_depth=trial.suggest_int("max_depth", 4, 24),
        min_samples_split=trial.suggest_int("min_samples_split", 2, 20),
        min_samples_leaf=trial.suggest_int("min_samples_leaf", 1, 10),
        max_features=trial.suggest_categorical("max_features", ["sqrt", "log2", 0.5, 0.7]),
    )


def _space_xgboost(trial: optuna.Trial) -> Dict[str, Any]:
    return dict(
        n_estimators=trial.suggest_int("n_estimators", 200, 1000, step=100),
        max_depth=trial.suggest_int("max_depth", 3, 12),
        learning_rate=trial.suggest_float("learning_rate", 1e-3, 0.2, log=True),
        subsample=trial.suggest_float("subsample", 0.5, 1.0),
        colsample_bytree=trial.suggest_float("colsample_bytree", 0.5, 1.0),
        reg_alpha=trial.suggest_float("reg_alpha", 1e-3, 10.0, log=True),
        reg_lambda=trial.suggest_float("reg_lambda", 1e-3, 10.0, log=True),
        gamma=trial.suggest_float("gamma", 1e-3, 5.0, log=True),
        min_child_weight=trial.suggest_int("min_child_weight", 1, 10),
    )


def _space_lightgbm(trial: optuna.Trial) -> Dict[str, Any]:
    return dict(
        n_estimators=trial.suggest_int("n_estimators", 200, 1200, step=100),
        learning_rate=trial.suggest_float("learning_rate", 1e-3, 0.2, log=True),
        num_leaves=trial.suggest_int("num_leaves", 15, 255, log=True),
        min_data_in_leaf=trial.suggest_int("min_data_in_leaf", 5, 50),
        feature_fraction=trial.suggest_float("feature_fraction", 0.5, 1.0),
        bagging_fraction=trial.suggest_float("bagging_fraction", 0.5, 1.0),
        lambda_l1=trial.suggest_float("lambda_l1", 1e-3, 10.0, log=True),
        lambda_l2=trial.suggest_float("lambda_l2", 1e-3, 10.0, log=True),
    )


def _space_catboost(trial: optuna.Trial) -> Dict[str, Any]:
    return dict(
        iterations=trial.suggest_int("iterations", 300, 1500, step=100),
        depth=trial.suggest_int("depth", 4, 10),
        learning_rate=trial.suggest_float("learning_rate", 1e-3, 0.2, log=True),
        l2_leaf_reg=trial.suggest_float("l2_leaf_reg", 0.5, 10.0, log=True),
    )


def _space_logreg(trial: optuna.Trial) -> Dict[str, Any]:
    return dict(
        C=trial.suggest_float("C", 1e-3, 100.0, log=True),
        penalty=trial.suggest_categorical("penalty", ["l1", "l2"]),
    )


def _space_nn(trial: optuna.Trial) -> Dict[str, Any]:
    return dict(
        hidden=[trial.suggest_categorical("h1", [64, 128, 256, 512]),
                trial.suggest_categorical("h2", [32, 64, 128, 256]),
                trial.suggest_categorical("h3", [16, 32, 64, 128])],
        dropout=trial.suggest_float("dropout", 0.1, 0.5),
        lr=trial.suggest_float("lr", 1e-4, 5e-3, log=True),
        weight_decay=trial.suggest_float("weight_decay", 1e-6, 1e-2, log=True),
        batch_size=trial.suggest_categorical("batch_size", [32, 64, 128, 256]),
    )


SEARCH_SPACES: Dict[str, Callable[[optuna.Trial], Dict[str, Any]]] = {
    "random_forest": _space_random_forest,
    "xgboost": _space_xgboost,
    "lightgbm": _space_lightgbm,
    "catboost": _space_catboost,
    "logistic_regression": _space_logreg,
    "neural_network": _space_nn,
}


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------
@dataclass
class OptimizationResult:
    best_params: Dict[str, Any]
    best_score: float
    n_trials: int
    study_history: List[Dict[str, Any]]
    duration_seconds: float


# ---------------------------------------------------------------------------
# Optimizer
# ---------------------------------------------------------------------------
class HyperparameterOptimizer:
    def __init__(self, model_name: str, *, n_trials: int = 50, timeout: int = 1800, cv_folds: int = 5, metric: str = "log_loss") -> None:
        if model_name not in SEARCH_SPACES:
            raise HyperparameterOptimizationError(f"No search space defined for model '{model_name}'")
        self.model_name = model_name
        self.n_trials = n_trials
        self.timeout = timeout
        self.cv_folds = cv_folds
        self.metric = metric
        self.logger = get_logger(self.__class__.__name__)

    # ---------------------------------------------------------- public
    def optimize(self, X: np.ndarray, y: np.ndarray) -> OptimizationResult:
        optuna.logging.set_verbosity(optuna.logging.WARNING)
        study = optuna.create_study(direction="minimize" if self.metric == "log_loss" else "maximize")
        history: List[Dict[str, Any]] = []
        t0 = time.perf_counter()

        def _objective(trial: optuna.Trial) -> float:
            params = SEARCH_SPACES[self.model_name](trial)
            model = build_model(self.model_name, **params)
            score = self._cv_score(model, X, y)
            history.append({"trial": trial.number, "score": float(score), "params": params})
            return score

        try:
            study.optimize(_objective, n_trials=self.n_trials, timeout=self.timeout, show_progress_bar=False)
        except Exception as exc:
            self.logger.exception("Optuna optimisation failed")
            raise HyperparameterOptimizationError(f"Optuna failed: {exc}") from exc

        dt = time.perf_counter() - t0
        return OptimizationResult(
            best_params=study.best_params,
            best_score=float(study.best_value),
            n_trials=len(study.trials),
            study_history=history,
            duration_seconds=dt,
        )

    # ---------------------------------------------------------- internals
    def _cv_score(self, model: BaseModel, X: np.ndarray, y: np.ndarray) -> float:
        skf = StratifiedKFold(n_splits=self.cv_folds, shuffle=True, random_state=42)
        scores: List[float] = []
        for train_idx, val_idx in skf.split(X, y):
            m = build_model(self.model_name, **model.hyperparams)
            m.fit(X[train_idx], y[train_idx])
            proba = m.predict_proba(X[val_idx])
            if self.metric == "log_loss":
                n_classes = len(np.unique(y))
                scores.append(float(log_loss(y[val_idx], proba, labels=list(range(n_classes)))))
            else:  # accuracy
                scores.append(float((proba.argmax(1) == y[val_idx]).mean()))
        return float(np.mean(scores))


__all__ = ["HyperparameterOptimizer", "OptimizationResult", "SEARCH_SPACES"]

