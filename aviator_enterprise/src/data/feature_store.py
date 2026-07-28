"""
Feature Store
=============

Versioned, on-disk store of engineered feature matrices and their
metadata.  Follows the layout used by Feast/Vertex Feature Store but
implemented with plain parquet + JSON for zero infrastructure cost.
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd

from config.settings import settings
from src.core.exceptions import DataNotFoundError
from src.core.helpers import deterministic_hash, utc_now_iso, ensure_dir
from src.core.logger import get_logger

logger = get_logger(__name__)


@dataclass
class FeatureSetMetadata:
    name: str
    version: str
    feature_names: List[str]
    n_samples: int
    n_features: int
    created_at: str
    source_hash: str
    extra: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "FeatureSetMetadata":
        return cls(**d)


class FeatureStore:
    """Disk-backed feature store with version pinning."""

    def __init__(self, base_dir: Optional[Path] = None) -> None:
        self.base_dir = ensure_dir(base_dir or (settings.datasets_dir / "feature_store"))  # type: ignore[operator]
        self._lock = asyncio.Lock()

    # ---------------------------------------------------------- write
    async def write(self, name: str, df: pd.DataFrame, *, source_hash: str = "", extra: Optional[Dict[str, Any]] = None) -> FeatureSetMetadata:
        async with self._lock:
            version = self._next_version(name)
            version_dir = ensure_dir(self.base_dir / name / version)
            data_path = version_dir / "features.parquet"
            meta_path = version_dir / "metadata.json"

            storage_format = "parquet"
            try:
                df.to_parquet(data_path, index=False)
            except Exception as exc:
                storage_format = "pickle"
                data_path = version_dir / "features.pkl"
                df.to_pickle(data_path)
                logger.warning("Parquet unavailable; wrote pickle feature set: %s", exc)
            meta = FeatureSetMetadata(
                name=name,
                version=version,
                feature_names=[c for c in df.columns if c not in ("round_id", "ts_utc")],
                n_samples=len(df),
                n_features=len(df.columns),
                created_at=utc_now_iso(),
                source_hash=source_hash or deterministic_hash(df.head(100).to_dict()),
                extra={**(extra or {}), "storage_format": storage_format, "data_path": str(data_path)},
            )
            meta_path.write_text(json.dumps(meta.to_dict(), indent=2), encoding="utf-8")
            logger.info("Feature set written", extra={"name": name, "version": version, "rows": len(df)})
            return meta

    # ---------------------------------------------------------- read
    async def read(self, name: str, version: Optional[str] = None) -> pd.DataFrame:
        version = version or self._latest_version(name)
        if version is None:
            raise DataNotFoundError(f"No versions found for feature set '{name}'")
        meta = await self.metadata(name, version)
        storage_format = meta.extra.get("storage_format", "parquet")
        path = Path(meta.extra.get("data_path") or self.base_dir / name / version / "features.parquet")
        if not path.exists():
            raise DataNotFoundError(f"Feature set not found: {name}:{version}")
        if storage_format == "pickle" or path.suffix == ".pkl":
            return pd.read_pickle(path)
        return pd.read_parquet(path)

    async def metadata(self, name: str, version: Optional[str] = None) -> FeatureSetMetadata:
        version = version or self._latest_version(name)
        if version is None:
            raise DataNotFoundError(f"No versions found for feature set '{name}'")
        path = self.base_dir / name / version / "metadata.json"
        if not path.exists():
            raise DataNotFoundError(f"Metadata not found: {name}:{version}")
        return FeatureSetMetadata.from_dict(json.loads(path.read_text()))

    # ---------------------------------------------------------- list
    def list(self, name: Optional[str] = None) -> List[FeatureSetMetadata]:
        results: List[FeatureSetMetadata] = []
        names = [name] if name else [p.name for p in self.base_dir.iterdir() if p.is_dir()]
        for n in names:
            for version_dir in (self.base_dir / n).iterdir():
                if not version_dir.is_dir():
                    continue
                meta_path = version_dir / "metadata.json"
                if meta_path.exists():
                    results.append(FeatureSetMetadata.from_dict(json.loads(meta_path.read_text())))
        return sorted(results, key=lambda m: m.created_at, reverse=True)

    # ---------------------------------------------------------- internals
    def _latest_version(self, name: str) -> Optional[str]:
        target = self.base_dir / name
        if not target.exists():
            return None
        versions = sorted([p.name for p in target.iterdir() if p.is_dir()])
        return versions[-1] if versions else None

    def _next_version(self, name: str) -> str:
        latest = self._latest_version(name)
        if latest is None:
            return "v1"
        try:
            n = int(latest.lstrip("v"))
            return f"v{n + 1}"
        except ValueError:
            ts = datetime.utcnow().strftime("%Y%m%d%H%M%S")
            return f"v_{ts}"


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------
_default_store: Optional[FeatureStore] = None


def get_feature_store() -> FeatureStore:
    global _default_store
    if _default_store is None:
        _default_store = FeatureStore()
    return _default_store


__all__ = ["FeatureStore", "FeatureSetMetadata", "get_feature_store"]
