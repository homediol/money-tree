"""Convenience re-exports for the models layer."""
from __future__ import annotations

import importlib
from typing import Optional, Type

from src.core.logger import get_logger
from src.models.base import BaseModel, PredictionResult  # noqa: F401

logger = get_logger(__name__)


def _import_model(module_name: str, class_name: str) -> Optional[Type[BaseModel]]:
    try:
        module = importlib.import_module(module_name)
        return getattr(module, class_name)
    except Exception as exc:  # pragma: no cover - defensive import guard
        logger.warning("Model '%s' unavailable: %s", class_name, exc)
        return None


RandomForestModel = _import_model("src.models.random_forest", "RandomForestModel")
XGBoostModel = _import_model("src.models.xgboost_model", "XGBoostModel")
LightGBMModel = _import_model("src.models.lightgbm_model", "LightGBMModel")
CatBoostModel = _import_model("src.models.catboost_model", "CatBoostModel")
LogisticRegressionModel = _import_model("src.models.logistic_regression", "LogisticRegressionModel")
NeuralNetworkModel = _import_model("src.models.neural_network", "NeuralNetworkModel")
LSTMModel = _import_model("src.models.lstm", "LSTMModel")
TransformerModel = _import_model("src.models.transformer", "TransformerModel")
EnsembleModel = _import_model("src.models.ensemble", "EnsembleModel")
EnsembleConfig = _import_model("src.models.ensemble", "EnsembleConfig")


MODEL_REGISTRY = {
    "random_forest": RandomForestModel,
    "xgboost": XGBoostModel,
    "lightgbm": LightGBMModel,
    "catboost": CatBoostModel,
    "logistic_regression": LogisticRegressionModel,
    "neural_network": NeuralNetworkModel,
    "lstm": LSTMModel,
    "transformer": TransformerModel,
    "ensemble": EnsembleModel,
}
MODEL_REGISTRY = {name: cls for name, cls in MODEL_REGISTRY.items() if cls is not None}


def build_model(name: str, **hp) -> BaseModel:
    if name not in MODEL_REGISTRY:
        available = ", ".join(sorted(MODEL_REGISTRY)) or "none"
        raise ValueError(f"Unknown model: {name}. Available: {available}")
    return MODEL_REGISTRY[name](**hp)


__all__ = ["MODEL_REGISTRY", "build_model", "BaseModel", "PredictionResult",
           "RandomForestModel", "XGBoostModel", "LightGBMModel", "CatBoostModel",
           "LogisticRegressionModel", "NeuralNetworkModel", "LSTMModel",
           "TransformerModel", "EnsembleModel", "EnsembleConfig"]

