from __future__ import annotations

import json
import hashlib
import logging
import os
import uuid
import warnings
from threading import Lock
from threadpoolctl import threadpool_limits
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import joblib
import numpy as np
import pandas as pd

from app.ml.trainer import ModelTrainer, TrainingResult, feature_schema

log = logging.getLogger("APP.ML")


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
        self._candidate_model = None
        self._candidate_metadata = None
        self._training = False
        self._train_lock = Lock()
        self._error: str | None = None
        self._latest_prediction: dict[str, Any] | None = None
        self._last_stale_warning_key: tuple[str | None, int] | None = None
        self._load_active()

    @staticmethod
    def _atomic_json(path: Path, payload: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)

    def train(self, dataset_service, progress: Callable[[str], None] | None = None) -> TrainingResult:
        if not self._train_lock.acquire(blocking=False):
            raise RuntimeError("training_already_in_progress")
        self._training, self._error = True, None
        try:
            with getattr(dataset_service, "_lock", Lock()):
                snapshot = dataset_service.dataset.copy()
            raw_path = getattr(dataset_service, "raw_path", None)
            source_hash = (hashlib.sha256(raw_path.read_bytes()).hexdigest()
                           if not getattr(dataset_service, "source_loader", None) and raw_path and raw_path.exists()
                           else hashlib.sha256(pd.util.hash_pandas_object(snapshot, index=False).values.tobytes()).hexdigest())
            # Limit native BLAS/OpenMP work so collection and API serving retain CPU.
            with threadpool_limits(limits=2):
                result = (self.trainer.train_validate(snapshot, progress=progress)
                          if progress else self.trainer.train_validate(snapshot))
            result.source_sha256 = source_hash
            if not snapshot.empty:
                result.last_training_round_index = int(snapshot.iloc[-1]["round_index"])
            self.latest = result
            result.model_version = result.model_version or f"attempt-{uuid.uuid4().hex}"
            metadata = result.model_dump()
            candidate_model = getattr(result, "_model", None)
            if candidate_model is not None:
                version_dir = self.model_dir / result.model_version
                version_dir.mkdir(parents=True, exist_ok=False)
                artifact_path = version_dir / "model.joblib"
                joblib.dump({"model": candidate_model, "feature_names": result.feature_names,
                             "feature_version": result.feature_version, "schema_hash": result.feature_schema_hash}, artifact_path)
                metadata["artifact_path"] = str(artifact_path.resolve())
                self._atomic_json(version_dir / "metadata.json", metadata)
            metadata.update(
                validation_folds=metadata.get("walk_forward") or [],
                metrics=metadata.get("test_metrics") or {},
                rejection_reason=None if result.validated else result.selection_reason or result.message,
                deployment_status="DEPLOYED" if result.validated else "NOT_DEPLOYABLE",
                next_evaluation_checkpoint=(result.last_training_round_index or 0) + getattr(self, "retrain_interval", 250),
            )
            champion_valid = bool(self._metadata and self._metadata.get("overfitting_checks", {}).get("deployable"))
            promote = candidate_model is not None and (result.validated or not champion_valid)
            active_metadata = metadata if promote else self._metadata
            if self.repository:
                self.repository.save_model_candidate(metadata)
                if candidate_model is not None:
                    self.repository.save_model_version(result.model_version, result.algorithm or "unknown", metadata["artifact_path"], metadata)
                # PostgreSQL owns both pointers. Binary weights remain artifacts.
                self.repository.save_application_state("ml_registry", {"active": active_metadata, "candidate": metadata})
            elif candidate_model is not None:
                self._atomic_json(self.model_dir / "candidate.json", {"model_version": result.model_version})
            self._candidate_model, self._candidate_metadata = candidate_model, metadata
            if promote:
                # Legacy tooling may still inspect this manifest; it is not
                # authoritative when a repository is configured.
                self._atomic_json(self.active_path, {"model_version": result.model_version,
                                                     "artifact_path": metadata["artifact_path"]})
                self._model, self._metadata = candidate_model, metadata
                self._latest_prediction = None
            return result
        except Exception as exc:
            self._error = str(exc)
            raise
        finally:
            self._training = False
            self._train_lock.release()

    def _read_artifact(self, metadata: dict):
        version_dir = self.model_dir / metadata["model_version"]
        artifact_path = version_dir / "model.joblib"
        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", message="Setting the shape on a NumPy array has been deprecated", category=DeprecationWarning)
            artifact = joblib.load(artifact_path)
        if (artifact.get("feature_names") != metadata.get("feature_names")
                or artifact.get("schema_hash") != metadata.get("feature_schema_hash")
                or artifact.get("feature_version") != metadata.get("feature_version")):
            raise ValueError("model artifact does not match persisted feature schema")
        return artifact["model"]

    def _load_active(self) -> None:
        try:
            persisted = self.repository.load_application_state("ml_registry") if self.repository else None
            if persisted is not None:
                self._metadata = persisted.get("active")
                self._candidate_metadata = persisted.get("candidate")
            else:
                # One-time migration of an existing installation into the DB.
                if not self.active_path.exists():
                    return
                active = json.loads(self.active_path.read_text(encoding="utf-8"))
                self._metadata = json.loads((self.model_dir / active["model_version"] / "metadata.json").read_text())
                self._candidate_metadata = self._metadata
                if not self.repository and (self.model_dir / "candidate.json").exists():
                    candidate = json.loads((self.model_dir / "candidate.json").read_text())
                    self._candidate_metadata = json.loads((self.model_dir / candidate["model_version"] / "metadata.json").read_text())
                if self.repository:
                    self.repository.save_model_candidate(self._candidate_metadata)
                    self.repository.save_application_state("ml_registry", {"active": self._metadata, "candidate": self._candidate_metadata})
            if self._metadata:
                self._model = self._read_artifact(self._metadata)
            if self._candidate_metadata and self._candidate_metadata.get("artifact_path"):
                self._candidate_model = (self._model if self._candidate_metadata.get("model_version") == (self._metadata or {}).get("model_version")
                                         else self._read_artifact(self._candidate_metadata))
        except Exception as exc:
            self._error = str(exc)
            log.exception("model restore failed")

    def candidate(self):
        """Experimental observer only; never a deployment or execution input."""
        return self._candidate_model, self._candidate_metadata

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
            elif dataset_service.quality.get("latest_contiguous_rounds", 0) < 100:
                state = "INSUFFICIENT_RECENT_HISTORY"
        model_validated = bool(self._metadata and self._metadata.get("overfitting_checks", {}).get("deployable", False))
        if state == "READY" and not model_validated:
            state = "NOT_VALIDATED"
        return {"status": state, "candidate_version": (self._candidate_metadata or {}).get("model_version"),
                "active_model_version": self._metadata.get("model_version") if model_validated else None,
                "model_version": self._metadata.get("model_version") if self._metadata else None,
                "feature_version": self._metadata.get("feature_version") if self._metadata else None,
                "compatible": compatible,
                "model_validated": model_validated,
                "deployable": model_validated and state == "READY",
                "quality_state": ("NOT_EVALUATED" if not self._metadata else
                                  "BASELINE_OUTPERFORMED" if self._metadata.get("overfitting_checks", {}).get("deployable") else "BELOW_BASELINE"),
                "validation_message": self._metadata.get("message") if self._metadata else "No trained model is available.",
                "last_error": self._error,
                "latest_prediction_id": self._latest_prediction.get("prediction_id") if self._latest_prediction else None}

    def _features_stale(self, dataset_service) -> bool:
        return not self._history_context(dataset_service)["fresh"]

    def _history_context(self, dataset_service) -> dict[str, Any]:
        if dataset_service.clean_rounds.empty:
            return {"latest_round_timestamp": None, "history_age": None,
                    "staleness_threshold": self.max_feature_age_s, "fresh": False}
        latest = dataset_service.clean_rounds.iloc[-1]
        timestamp = str(latest["timestamp"])
        try:
            age = (datetime.now(timezone.utc) - latest["timestamp_dt"].to_pydatetime()).total_seconds()
        except (KeyError, AttributeError, TypeError, ValueError):
            age = None
        return {"latest_round_timestamp": timestamp, "history_age": age,
                "staleness_threshold": self.max_feature_age_s,
                "fresh": age is not None and 0 <= age <= self.max_feature_age_s}

    def _warn_stale(self, context: dict[str, Any]) -> None:
        key = (context["latest_round_timestamp"], self.max_feature_age_s)
        if self._last_stale_warning_key != key:
            log.warning("ML inference blocked: STALE_HISTORY latest_round_timestamp=%s history_age=%s threshold_seconds=%s",
                        context["latest_round_timestamp"], context["history_age"], self.max_feature_age_s)
            self._last_stale_warning_key = key

    def prediction_payload(self, dataset_service) -> dict[str, Any]:
        """One explicit live inference contract; never expose a non-usable prediction."""
        context = self._history_context(dataset_service)
        count = len(dataset_service.clean_rounds)
        contiguous = int(dataset_service.quality.get("latest_contiguous_rounds") or 0)
        prediction = None
        if count == 0:
            reason = "INSUFFICIENT_HISTORY"
        elif not context["fresh"]:
            reason = "STALE_HISTORY"
            self._warn_stale(context)
        elif count < self.trainer.min_samples or contiguous < 100:
            reason = "INSUFFICIENT_HISTORY"
        elif not self.status(dataset_service)["deployable"]:
            reason = "NO_DEPLOYED_MODEL"
        else:
            prediction = self.predict_latest(dataset_service)
            reason = "OK" if prediction is not None else "NO_DEPLOYED_MODEL"
        return {"usable": prediction is not None, "prediction": prediction,
                "confidence": prediction.get("confidence") if prediction else 0.0,
                "reason": reason, **{key: context[key] for key in
                                    ("latest_round_timestamp", "history_age", "staleness_threshold")},
                "history_age_unit": "seconds"}

    def predict_latest(self, dataset_service, *, allow_stale: bool = False) -> dict[str, Any] | None:
        health = self.status(dataset_service)
        if self.target != 2.0:
            return None
        if health["status"] == "STALE":
            self._warn_stale(self._history_context(dataset_service))
        # The legacy allow_stale flag is intentionally non-operative: stale
        # history must never produce a numeric live prediction.
        if health["status"] != "READY":
            return None
        if not health["deployable"]:
            return None
        if dataset_service.quality.get("latest_contiguous_rounds", 0) < 100:
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
        if not np.isfinite(row).all():
            self._error = "feature vector contains non-finite values"
            return None
        try:
            probability = float(self._model.predict_proba(row)[0, 1])
        except Exception as exc:
            self._error = f"model inference failed: {type(exc).__name__}: {exc}"
            return None
        if not 0 <= probability <= 1:
            self._error = "model returned probability outside [0,1]"
            return None
        source = dataset_service.clean_rounds.iloc[-1]
        checks = self._metadata.get("overfitting_checks") or {}
        base_metrics = ((self._metadata.get("baselines") or {}).get("test") or {}).get("base_rate_probability") or {}
        test_metrics = self._metadata.get("test_metrics") or {}
        baseline_brier = base_metrics.get("brier_score")
        # A conservative skill score, not certainty about the next outcome.
        skill = (max(0.0, min(1.0, (baseline_brier - test_metrics["brier_score"]) / baseline_brier))
                 if baseline_brier and test_metrics.get("brier_score") is not None else 0.0)
        pattern_evidence = [
            {"type": "causal_pattern", "pattern": name,
             "previous_matches": int(latest[f"pattern_{name}_count"]),
             "smoothed_rate": float(latest[f"pattern_{name}_rate"])}
            for name in ("streak", "sequence_2", "sequence_3")
        ]
        prediction = {"prediction_id": uuid.uuid4().hex, "source_round_id": str(source["round_id"]),
                      "usable": True, "reason": "OK",
                      "target": "next_round_ge_2x", "target_code": "NEXT_ROUND_GTE_2X", "probability_2x": probability,
                      "confidence": skill, "confidence_definition": "positive held-out Brier skill versus frozen training frequency (not event certainty)",
                      "data_points_used": int(len(dataset_service.clean_rounds)),
                      "training_samples": self._metadata.get("dataset_size"),
                      "history_rounds_available": int(len(dataset_service.clean_rounds)),
                      "evidence": [{"type": "held_out_model_evaluation", "test_brier_score": test_metrics.get("brier_score"),
                                    "test_log_loss": test_metrics.get("log_loss"),
                                    "test_brier_advantage_ci95": (checks.get("best_baseline_test_uncertainty") or {}).get("ci95"),
                                    "dataset_hash": self._metadata.get("dataset_hash")}, *pattern_evidence],
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

    def fallback_estimate(self, dataset_service) -> dict[str, Any]:
        """Informational frequency, never a numeric prediction or risk input."""
        rows = dataset_service.clean_rounds
        n = len(rows)
        context = self._history_context(dataset_service)
        contiguous = int(dataset_service.quality.get("latest_contiguous_rounds") or 0)
        if n == 0:
            reason = "INSUFFICIENT_HISTORY"
        elif not context["fresh"]:
            reason = "STALE_HISTORY"
            self._warn_stale(context)
        elif n < self.trainer.min_samples or contiguous < 100:
            reason = "INSUFFICIENT_HISTORY"
        else:
            reason = "NO_DEPLOYED_MODEL"
        recent = rows.tail(min(250, n))
        frequency = float((recent["multiplier"] >= 2).mean()) if reason == "NO_DEPLOYED_MODEL" else None
        return {"target": "NEXT_ROUND_GTE_2X", "probability_2x": None,
                "informational_frequency_2x": frequency, "prediction": None,
                "confidence": 0.0, "model_version": None, "data_points_used": len(recent) if frequency is not None else 0,
                "latest_contiguous_rounds": dataset_service.quality.get("latest_contiguous_rounds"),
                "source_round_id": str(rows.iloc[-1]["round_id"]) if n else None,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "usable": False, "reason": reason,
                **{key: context[key] for key in ("latest_round_timestamp", "history_age", "staleness_threshold")},
                "history_age_unit": "seconds",
                "evidence": ([{"type": "observed_frequency", "window": len(recent), "informational_only": True}]
                             if frequency is not None else [])}

    def performance(self) -> dict:
        if self._candidate_metadata:
            return self._candidate_metadata
        if self._metadata:
            return self._metadata
        if self.latest:
            return self.latest.model_dump()
        if self.repository:
            candidates = self.repository.list_model_candidates(1)
            if candidates:
                candidate = candidates[0]
                return {**candidate, "restored_from_persistence": True}
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
        metadata = self.performance()
        return {key: metadata.get(key) for key in ("baselines", "models", "validation_metrics", "test_metrics", "calibration", "walk_forward", "overfitting_checks")}

    def latest_prediction(self) -> dict | None:
        prediction = self._latest_prediction or (self.repository.latest_ml_prediction() if self.repository else None)
        if not prediction:
            return None
        deployable = bool(self._metadata and self._metadata.get("overfitting_checks", {}).get("deployable", False))
        try:
            created = datetime.fromisoformat(prediction["created_at"])
            age = (datetime.now(timezone.utc) - created).total_seconds()
        except (KeyError, TypeError, ValueError):
            age = float("inf")
        fresh = bool(prediction.get("fresh")) and 0 <= age <= self.max_feature_age_s
        return ({**prediction, "fresh": True, "usable": True, "quality_state": "BASELINE_OUTPERFORMED"}
                if fresh and deployable else None)

    def recent_predictions(self, limit: int = 25) -> list[dict]:
        return self.repository.list_ml_predictions(limit) if self.repository else ([self._latest_prediction] if self._latest_prediction else [])
