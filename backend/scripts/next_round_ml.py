"""Train or inspect the production next-round probability model without betting.

From the project root:
  PYTHONPATH=backend backend/.venv/bin/python backend/scripts/next_round_ml.py train
  PYTHONPATH=backend backend/.venv/bin/python backend/scripts/next_round_ml.py predict
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
from pathlib import Path

from app.core.config import get_settings
from app.database.repository import Repository
from app.ml.model_registry import ModelRegistry
from app.services.dataset_service import DatasetService


def main() -> None:
    # Settings uses a cwd-relative .env; run with the same backend configuration
    # as Uvicorn even when this command is launched from the repository root.
    os.chdir(Path(__file__).resolve().parents[1])
    settings = get_settings()
    parser = argparse.ArgumentParser(description="Chronological next-round 2x ML evaluation")
    parser.add_argument("action", choices=("train", "predict"))
    parser.add_argument("--data", type=Path, default=settings.data_path)
    parser.add_argument("--model-dir", type=Path, default=settings.model_dir)
    parser.add_argument("--database", type=Path, default=settings.database_path)
    args = parser.parse_args()
    repo = Repository(args.database, settings.database_url, settings.require_postgres)
    repo.init()
    if repo.database_url:
        # One immutable PostgreSQL query is the evaluation snapshot. The local
        # JSON history can lag the collector and must not become authoritative.
        rows = repo.load_rounds()
        raw = json.dumps(rows, sort_keys=True, separators=(",", ":")).encode()
        source_loader = lambda: rows
    else:
        raw = args.data.read_bytes()
        json.loads(raw)
        source_loader = None
    raw_hash = hashlib.sha256(raw).hexdigest()
    archive = args.model_dir.parent / "artifacts" / "training_datasets" / f"{raw_hash}.json"
    if args.action == "train" and not archive.exists():
        archive.parent.mkdir(parents=True, exist_ok=True)
        pending = archive.with_suffix(".tmp")
        pending.write_bytes(raw)
        os.replace(pending, archive)
    with tempfile.TemporaryDirectory(prefix="winner-ml-") as temp:
        snapshot = Path(temp) / "roundhistory.json"
        if source_loader is None:
            snapshot.write_bytes(raw)
        service = DatasetService(snapshot, Path(temp) / "processed", Path(temp) / "features",
                                 source_loader=source_loader)
        service.build_training_dataset(persist=False)
        registry = ModelRegistry(2.0, args.model_dir, repo, max_feature_age_s=settings.ml_max_history_age,
                                 model_concurrency=settings.ml_model_concurrency)
        if args.action == "train":
            result = registry.train(service)
            print(json.dumps({"status": result.status, "selected_model": result.algorithm,
                              "validated": result.validated, "selection_reason": result.selection_reason,
                              "model_version": result.model_version,
                              "cycle_id": result.cycle_id, "ranking": result.ranking,
                              "model_concurrency": result.model_concurrency,
                              "deployment_outcome": registry.performance().get("deployment_outcome"),
                              "source_sha256": result.source_sha256,
                              "snapshot_sha256": raw_hash,
                              "source": "postgresql" if repo.database_url else "json",
                              "dataset_snapshot": str(archive) if args.action == "train" else None,
                              "dataset_hash": result.dataset_hash, "dataset_size": result.dataset_size,
                              "splits": result.splits, "feature_count": len(result.feature_names or []),
                              "data_quality": service.quality, "training_data_quality": result.data_quality,
                              "baselines": result.baselines, "models": result.models,
                              "test_metrics": result.test_metrics, "calibration": result.calibration,
                              "walk_forward": result.walk_forward, "overfitting_checks": result.overfitting_checks,
                              "feature_importance": result.feature_importance}, indent=2))
        else:
            payload = registry.prediction_payload(service)
            print(json.dumps({"status": registry.status(service), **payload,
                              "informational_fallback": None if payload["usable"] else registry.fallback_estimate(service)}, indent=2))


if __name__ == "__main__":
    main()
