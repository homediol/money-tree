"""
Domain-specific exceptions for the Aviator ML Enterprise Platform.

Using dedicated exception types (instead of bare `Exception`) lets the
API layer translate failures into correct HTTP status codes and lets the
monitoring layer categorise incidents.
"""
from __future__ import annotations

from typing import Any, Dict, Optional


class AviatorBaseError(Exception):
    """Root for all custom exceptions raised by the platform."""

    status_code: int = 500
    error_code: str = "INTERNAL_ERROR"

    def __init__(self, message: str, *, context: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(message)
        self.message = message
        self.context = context or {}

    def to_dict(self) -> Dict[str, Any]:
        return {
            "error_code": self.error_code,
            "message": self.message,
            "context": self.context,
        }


# ----------------------------- DATA LAYER -----------------------------------
class DataError(AviatorBaseError):
    status_code = 400
    error_code = "DATA_ERROR"


class DataValidationError(DataError):
    status_code = 422
    error_code = "DATA_VALIDATION_ERROR"


class DataNotFoundError(DataError):
    status_code = 404
    error_code = "DATA_NOT_FOUND"


class DataCorruptionError(DataError):
    status_code = 500
    error_code = "DATA_CORRUPTION_ERROR"


class InsufficientDataError(DataError):
    status_code = 400
    error_code = "INSUFFICIENT_DATA"


# ----------------------------- FEATURES ------------------------------------
class FeatureEngineeringError(AviatorBaseError):
    status_code = 500
    error_code = "FEATURE_ENGINEERING_ERROR"


# ----------------------------- MODELS --------------------------------------
class ModelError(AviatorBaseError):
    status_code = 500
    error_code = "MODEL_ERROR"


class ModelNotFoundError(ModelError):
    status_code = 404
    error_code = "MODEL_NOT_FOUND"


class ModelNotTrainedError(ModelError):
    status_code = 409
    error_code = "MODEL_NOT_TRAINED"


class ModelTrainingError(ModelError):
    status_code = 500
    error_code = "MODEL_TRAINING_ERROR"


class ModelLoadError(ModelError):
    status_code = 500
    error_code = "MODEL_LOAD_ERROR"


# ----------------------------- TRAINING ------------------------------------
class TrainingError(AviatorBaseError):
    status_code = 500
    error_code = "TRAINING_ERROR"


class HyperparameterOptimizationError(TrainingError):
    status_code = 500
    error_code = "HYPERPARAMETER_OPTIMIZATION_ERROR"


# ----------------------------- DRIFT / MONITORING --------------------------
class DriftDetectedError(AviatorBaseError):
    status_code = 200  # not a server error — it's an observation
    error_code = "DRIFT_DETECTED"


class MonitoringError(AviatorBaseError):
    status_code = 500
    error_code = "MONITORING_ERROR"


# ----------------------------- PREDICTION ----------------------------------
class PredictionError(AviatorBaseError):
    status_code = 500
    error_code = "PREDICTION_ERROR"


class LowConfidenceError(PredictionError):
    status_code = 200  # legitimate outcome — caller should treat as SKIP
    error_code = "LOW_CONFIDENCE"


# ----------------------------- REGISTRY ------------------------------------
class RegistryError(AviatorBaseError):
    status_code = 500
    error_code = "REGISTRY_ERROR"


__all__ = [
    "AviatorBaseError",
    "DataError", "DataValidationError", "DataNotFoundError",
    "DataCorruptionError", "InsufficientDataError",
    "FeatureEngineeringError",
    "ModelError", "ModelNotFoundError", "ModelNotTrainedError",
    "ModelTrainingError", "ModelLoadError",
    "TrainingError", "HyperparameterOptimizationError",
    "DriftDetectedError", "MonitoringError",
    "PredictionError", "LowConfidenceError",
    "RegistryError",
]

