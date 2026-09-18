from __future__ import annotations

import json
import os
import uuid
import warnings
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import joblib
import numpy as np

from app.ml.trainer import ModelTrainer, TrainingResult, feature_schema


class ModelRegistry:
    """Versioned model persistence, health checks and lightweight inference."""

    def __init__(self, target: float = 2.0, model_dir: Path | None = None, repository=None, max_feature_age_s: int = 600):
        self.trainer = ModelTrainer(target)
        self.target = target
        self.model_dir = Path(model_dir or Path(__file__).resolve().parents[2] / "trained_models")
        self.active_path = self.model_dir / "active.json"
        self.repository = repository
        self.max_feature_age_s = max_feature_age_s
        self.latest: TrainingResult | None = None
        self._model = None
        self._metadata: dict[str, Any] | None = None
        self._training = False
        self._error: str | None = None
        self._latest_prediction: dict[str, Any] | None = None
        self._load_active()

    @staticmethod
    def _atomic_json(path: Path, payload: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        os.replace(temporary, path)

    def train(self, dataset_service) -> TrainingResult:
        self._training, self._error = True, None
        try:
            result = self.trainer.train_validate(dataset_service.dataset.copy())
            self.latest = result
            if not result.validated or not getattr(result, "_model", None):
                return result
            version_dir = self.model_dir / result.model_version
            version_dir.mkdir(parents=True, exist_ok=False)
            artifact_path = version_dir / "model.joblib"
            metadata_path = version_dir / "metadata.json"
            joblib.dump({"model": result._model, "feature_names": result.feature_names,
                         "feature_version": result.feature_version, "schema_hash": result.feature_schema_hash}, artifact_path)
            metadata = result.model_dump() | {"artifact_path": str(artifact_path)}
            self._atomic_json(metadata_path, metadata)
            self._atomic_json(self.active_path, {"model_version": result.model_version,
                                                 "metadata_path": str(metadata_path), "artifact_path": str(artifact_path)})
            self._model, self._metadata = result._model, metadata
            if self.repository:
                self.repository.save_model_version(result.model_version, result.algorithm or "unknown", str(artifact_path), metadata)
            return result
        except Exception as exc:
            self._error = str(exc)
            raise
        finally:
            self._training = False

    def _load_active(self) -> None:
        if not self.active_path.exists():
            return
        try:
            active = json.loads(self.active_path.read_text(encoding="utf-8"))
            metadata = json.loads(Path(active["metadata_path"]).read_text(encoding="utf-8"))
            # Joblib currently triggers a harmless NumPy 2.5 array-shape
            # deprecation while reading its own pickle format. Scope the
            # suppression to artifact loading so application warnings remain.
            with warnings.catch_warnings():
                warnings.filterwarnings("ignore", message="Setting the shape on a NumPy array has been deprecated", category=DeprecationWarning)
                artifact = joblib.load(active["artifact_path"])
            self._model, self._metadata = artifact["model"], metadata
        except Exception as exc:
            self._model, self._metadata, self._error = None, None, str(exc)

    def status(self, dataset_service=None) -> dict[str, Any]:
        if self._training:
            state = "TRAINING"
        elif self._error:
            state = "ERROR"
        elif self._model is None or self._metadata is None:
            state = "NOT_TRAINED"
        else:
            state = "READY"
        compatible = None
        if state == "READY" and dataset_service is not None:
            names, schema_hash, feature_version = feature_schema(dataset_service.dataset)
            compatible = names == self._metadata.get("feature_names") and schema_hash == self._metadata.get("feature_schema_hash") and feature_version == self._metadata.get("feature_version")
            if not compatible:
                state = "INCOMPATIBLE"
            elif self._features_stale(dataset_service):
                state = "STALE"
        return {"status": state, "model_version": self._metadata.get("model_version") if self._metadata else None,
                "feature_version": self._metadata.get("feature_version") if self._metadata else None,
                "compatible": compatible,
                "deployable": bool(self._metadata and self._metadata.get("overfitting_checks", {}).get("deployable", False)),
                "quality_state": ("BASELINE_OUTPERFORMED" if self._metadata and self._metadata.get("overfitting_checks", {}).get("deployable") else "BELOW_BASELINE"),
                "last_error": self._error,
                "latest_prediction_id": self._latest_prediction.get("prediction_id") if self._latest_prediction else None}

    def _features_stale(self, dataset_service) -> bool:
        if dataset_service.clean_rounds.empty:
            return True
        timestamp = dataset_service.clean_rounds.iloc[-1]["timestamp_dt"]
        age = (datetime.now(timezone.utc) - timestamp.to_pydatetime()).total_seconds()
        return age > self.max_feature_age_s

    def predict_latest(self, dataset_service, *, allow_stale: bool = False) -> dict[str, Any] | None:
        health = self.status(dataset_service)
        if health["status"] not in ({"READY", "STALE"} if allow_stale else {"READY"}):
            return None
        if not health["deployable"]:
            return None
        latest = dataset_service.latest_features()
        if not latest:
            return None
        feature_names = self._metadata["feature_names"]
        try:
            row = np.array([[float(latest[name]) for name in feature_names]], dtype=float)
        except (KeyError, TypeError, ValueError) as exc:
            self._error = f"feature vector incompatible: {exc}"
            return None
        probability = float(self._model.predict_proba(row)[0, 1])
        if not 0 <= probability <= 1:
            self._error = "model returned probability outside [0,1]"
            return None
        source = dataset_service.clean_rounds.iloc[-1]
        prediction = {"prediction_id": uuid.uuid4().hex, "source_round_id": str(source["round_id"]),
                      "target": "next_round_ge_2x", "probability_2x": probability,
                      "predicted_class": int(probability >= .5), "model_version": self._metadata["model_version"],
                      "feature_version": self._metadata["feature_version"], "created_at": datetime.now(timezone.utc).isoformat(),
                      "data_timestamp": str(source["timestamp"]), "fresh": health["status"] == "READY",
                      "data_quality_at_prediction": {
                          "total_rounds": dataset_service.quality.get("total_rounds"),
                          "valid_rounds": dataset_service.quality.get("valid_rounds"),
                          "invalid_rounds": dataset_service.quality.get("invalid_rounds"),
                          "duplicates": dataset_service.quality.get("duplicates"),
                          "missing_values": dataset_service.quality.get("missing_values"),
                      },
                      "estimate_notice": "Model estimate only; not a betting decision or guarantee."}
        self._latest_prediction = prediction
        if self.repository:
            self.repository.save_ml_prediction(prediction)
        return prediction

    def performance(self) -> dict:
        if self._metadata:
            return self._metadata
        if self.latest:
            return self.latest.model_dump()
        return {"status": "NOT_TRAINED", "validated": False, "models": {}, "baselines": {}}

    def model_info(self) -> dict:
        return self._metadata or {"status": "NOT_TRAINED"}

    def metadata_for_version(self, version: str | None) -> dict:
        if version and self._metadata and self._metadata.get("model_version") == version:
            return self._metadata
        path = self.model_dir / str(version) / "metadata.json" if version else None
        try:
            return json.loads(path.read_text(encoding="utf-8")) if path and path.exists() else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def metrics(self) -> dict:
        if not self._metadata:
            return {"status": "NOT_TRAINED"}
        return {key: self._metadata.get(key) for key in ("baselines", "models", "validation_metrics", "test_metrics", "calibration", "walk_forward", "overfitting_checks")}

    def latest_prediction(self) -> dict | None:
        prediction = self._latest_prediction or (self.repository.latest_ml_prediction() if self.repository else None)
        if not prediction:
            return None
        deployable = bool(self._metadata and self._metadata.get("overfitting_checks", {}).get("deployable", False))
        return {**prediction, "fresh": bool(prediction.get("fresh")) and deployable,
                "usable": deployable,
                "quality_state": "BASELINE_OUTPERFORMED" if deployable else "BELOW_BASELINE"}

    def recent_predictions(self, limit: int = 25) -> list[dict]:
        return self.repository.list_ml_predictions(limit) if self.repository else ([self._latest_prediction] if self._latest_prediction else [])
