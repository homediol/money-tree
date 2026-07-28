"""
Dataset Registry
================

Tracks every dataset version used to train models.  Enables reproducibility
by binding a `dataset_version` to each `model_version`.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd

from config.settings import settings
from src.core.helpers import deterministic_hash, ensure_dir, utc_now_iso
from src.core.logger import get_logger

logger = get_logger(__name__)


@dataclass
class DatasetVersion:
    name: str
    version: str
    n_samples: int
    n_features: int
    source_hash: str
    class_distribution: Dict[str, int]
    created_at: str
    extra: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "DatasetVersion":
        return cls(**d)


class DatasetRegistry:
    def __init__(self, base_dir: Optional[Path] = None) -> None:
        self.base_dir = ensure_dir(base_dir or settings.datasets_dir)  # type: ignore[arg-type]
        self.index_path = self.base_dir / "datasets_index.json"
        self._index = self._load_index()

    def _load_index(self) -> Dict[str, Any]:
        if not self.index_path.exists():
            return {"datasets": {}}
        try:
            return json.loads(self.index_path.read_text(encoding="utf-8"))
        except Exception:
            return {"datasets": {}}

    def _save_index(self) -> None:
        self.index_path.write_text(json.dumps(self._index, indent=2, default=str), encoding="utf-8")

    def register(self, name: str, df: pd.DataFrame, *, class_column: str = "category_name", extra: Optional[Dict[str, Any]] = None) -> DatasetVersion:
        if name not in self._index["datasets"]:
            self._index["datasets"][name] = []
        existing_versions = [d["version"] for d in self._index["datasets"][name]]
        version = f"v{len(existing_versions) + 1}"

        source_hash = deterministic_hash(df.head(200).to_dict())
        class_dist = {str(k): int(v) for k, v in df[class_column].value_counts().to_dict().items()} if class_column in df.columns else {}

        ds = DatasetVersion(
            name=name,
            version=version,
            n_samples=len(df),
            n_features=len(df.columns),
            source_hash=source_hash,
            class_distribution=class_dist,
            created_at=utc_now_iso(),
            extra=extra or {},
        )
        # store parquet
        path = self.base_dir / name / f"{version}.parquet"
        path.parent.mkdir(parents=True, exist_ok=True)
        df.to_parquet(path, index=False)
        ds.extra["path"] = str(path)

        self._index["datasets"][name].append(ds.to_dict())
        self._save_index()
        self.logger.info("Dataset registered", extra={"name": name, "version": version, "n": len(df)})
        return ds

    def list(self, name: Optional[str] = None) -> List[DatasetVersion]:
        if name:
            return [DatasetVersion.from_dict(d) for d in self._index["datasets"].get(name, [])]
        out: List[DatasetVersion] = []
        for versions in self._index["datasets"].values():
            for d in versions:
                out.append(DatasetVersion.from_dict(d))
        return sorted(out, key=lambda d: d.created_at, reverse=True)

    def get_latest(self, name: str) -> Optional[DatasetVersion]:
        versions = self._index["datasets"].get(name, [])
        if not versions:
            return None
        return DatasetVersion.from_dict(versions[-1])

    def get_path(self, name: str, version: str) -> Optional[Path]:
        for d in self._index["datasets"].get(name, []):
            if d["version"] == version:
                p = d.get("extra", {}).get("path")
                if p:
                    return Path(p)
        return None


__all__ = ["DatasetRegistry", "DatasetVersion"]

