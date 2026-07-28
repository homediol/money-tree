"""
Model Registry
==============

Versioned, disk-backed registry of trained models.  Supports:
  * promotion/demotion of a version to ACTIVE
  * alias lookups (latest, active, best)
  * metric comparison across versions
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from config.settings import settings
from src.core.exceptions import ModelNotFoundError, RegistryError
from src.core.helpers import ensure_dir, utc_now_iso
from src.core.logger import get_logger

logger = get_logger(__name__)


@dataclass
class ModelVersion:
    model_name: str
    version: str
    artifact_path: str
    calibrator_path: Optional[str] = None
    metrics: Dict[str, float] = field(default_factory=dict)
    feature_names: List[str] = field(default_factory=list)
    created_at: str = field(default_factory=utc_now_iso)
    is_active: bool = False
    tags: List[str] = field(default_factory=list)
    notes: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "ModelVersion":
        return cls(**d)


class ModelRegistry:
    def __init__(self, base_dir: Optional[Path] = None) -> None:
        self.base_dir = ensure_dir(base_dir or settings.models_dir)  # type: ignore[arg-type]
        self.index_path = self.base_dir / "registry_index.json"
        self._index: Dict[str, Any] = self._load_index()
        self.logger = logger

    # ---------------------------------------------------------- index
    def _load_index(self) -> Dict[str, Any]:
        if not self.index_path.exists():
            return {"models": {}, "active": {}}
        try:
            return json.loads(self.index_path.read_text(encoding="utf-8"))
        except Exception:
            return {"models": {}, "active": {}}

    def _save_index(self) -> None:
        self.index_path.write_text(json.dumps(self._index, indent=2, default=str), encoding="utf-8")

    # ---------------------------------------------------------- CRUD
    def register(self, version: ModelVersion) -> None:
        key = version.model_name
        versions = self._index["models"].setdefault(key, [])
        if version.is_active:
            for existing in versions:
                existing["is_active"] = False
        versions[:] = [
            existing for existing in versions
            if existing.get("version") != version.version
        ]
        versions.append(version.to_dict())
        if version.is_active:
            self._index["active"][key] = version.version
        self._save_index()
        self.logger.info("Registered model", extra={"name": key, "version": version.version})

    def list(self, model_name: Optional[str] = None) -> List[ModelVersion]:
        out: List[ModelVersion] = []
        if model_name:
            for d in self._index["models"].get(model_name, []):
                out.append(ModelVersion.from_dict(d))
        else:
            for name, versions in self._index["models"].items():
                for d in versions:
                    out.append(ModelVersion.from_dict(d))
        return sorted(out, key=lambda v: v.created_at, reverse=True)

    def get(self, model_name: str, version: Optional[str] = None) -> ModelVersion:
        versions = self._index["models"].get(model_name, [])
        if not versions:
            raise ModelNotFoundError(f"No versions registered for '{model_name}'")
        if version is None:
            return ModelVersion.from_dict(versions[-1])
        for d in versions:
            if d["version"] == version:
                return ModelVersion.from_dict(d)
        raise ModelNotFoundError(f"Version '{version}' not found for '{model_name}'")

    def get_active(self, model_name: str) -> ModelVersion:
        active = self._index.get("active", {}).get(model_name)
        if not active:
            return self.get(model_name)
        return self.get(model_name, active)

    def set_active(self, model_name: str, version: str) -> None:
        versions = self._index["models"].get(model_name, [])
        if not any(v["version"] == version for v in versions):
            raise ModelNotFoundError(f"Version '{version}' not found for '{model_name}'")
        for v in versions:
            v["is_active"] = (v["version"] == version)
        self._index["active"][model_name] = version
        self._save_index()
        self.logger.info("Active model updated", extra={"name": model_name, "version": version})

    def best(self, model_name: str, metric: str = "f1_macro") -> ModelVersion:
        versions = self.list(model_name)
        if not versions:
            raise ModelNotFoundError(f"No versions for '{model_name}'")
        return max(versions, key=lambda v: v.metrics.get(metric, float("-inf")))

    def best_overall(self, metric: str = "f1_macro") -> Optional[ModelVersion]:
        all_versions = self.list()
        if not all_versions:
            return None
        return max(all_versions, key=lambda v: v.metrics.get(metric, float("-inf")))

    def compare(self, model_name: str, metric: str = "f1_macro") -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        for v in self.list(model_name):
            rows.append({
                "version": v.version,
                "is_active": v.is_active,
                "created_at": v.created_at,
                **{k: v.metrics.get(k) for k in (
                    "accuracy", "balanced_accuracy", "precision_macro", "recall_macro",
                    "f1_macro", "f1_weighted", "mcc", "cohen_kappa", "log_loss",
                    "roc_auc", "pr_auc", "brier_score",
                )},
                metric: v.metrics.get(metric),
            })
        return rows


__all__ = ["ModelRegistry", "ModelVersion"]
