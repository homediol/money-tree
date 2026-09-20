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
    settings = get_settings()
    parser = argparse.ArgumentParser(description="Chronological next-round 2x ML evaluation")
    parser.add_argument("action", choices=("train", "predict"))
    parser.add_argument("--data", type=Path, default=settings.data_path)
    parser.add_argument("--model-dir", type=Path, default=settings.model_dir)
    parser.add_argument("--database", type=Path, default=settings.database_path)
    args = parser.parse_args()
    raw = args.data.read_bytes()
    json.loads(raw)
    raw_hash = hashlib.sha256(raw).hexdigest()
    archive = args.model_dir.parent / "artifacts" / "training_datasets" / f"{raw_hash}.json"
    if args.action == "train" and not archive.exists():
        archive.parent.mkdir(parents=True, exist_ok=True)
        pending = archive.with_suffix(".tmp")
        pending.write_bytes(raw)
        os.replace(pending, archive)
    with tempfile.TemporaryDirectory(prefix="winner-ml-") as temp:
        snapshot = Path(temp) / "roundhistory.json"; snapshot.write_bytes(raw)
        service = DatasetService(snapshot, Path(temp) / "processed", Path(temp) / "features")
        service.build_training_dataset(persist=False)
        repo = Repository(args.database); repo.init()
        registry = ModelRegistry(2.0, args.model_dir, repo, max_feature_age_s=settings.ml_max_history_age)
        if args.action == "train":
            result = registry.train(service)
            print(json.dumps({"status": result.status, "selected_model": result.algorithm,
                              "validated": result.validated, "selection_reason": result.selection_reason,
                              "model_version": result.model_version, "source_sha256": raw_hash,
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
