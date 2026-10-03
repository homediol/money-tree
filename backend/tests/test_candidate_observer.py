from __future__ import annotations

import json
import shutil
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import numpy as np

from app.database.repository import Repository
from app.ml.candidate_observer import CandidateObserver
from app.ml.model_registry import ModelRegistry
from app.ml.trainer import feature_schema
from app.services.dataset_service import DatasetService
from test_models import trained


def test_rejected_retrain_preserves_deployed_model_and_candidate(trained, tmp_path, monkeypatch):
    root, dataset, _, _, accepted = trained
    model_dir = tmp_path / "models"
    shutil.copytree(root / "models", model_dir)
    repository = Repository(tmp_path / "registry.sqlite3")
    repository.init()
    registry = ModelRegistry(2.0, model_dir, repository, max_feature_age_s=3600)
    rejected = replace(accepted, model_version="ml-rejected-new", validated=False,
                       status="NOT_VALIDATED", selection_reason="failed_earlier_folds",
                       trained_at=datetime.now(timezone.utc).isoformat(),
                       overfitting_checks={**accepted.overfitting_checks, "deployable": False})
    rejected._model = accepted._model
    monkeypatch.setattr(registry.trainer, "train_validate", lambda _: rejected)
    registry.train(dataset)

    assert registry.performance()["model_version"] == "ml-rejected-new"
    assert registry.status(dataset)["model_version"] == accepted.model_version
    assert registry.status(dataset)["deployable"] is True
    assert json.loads((model_dir / "active.json").read_text())["model_version"] == accepted.model_version
    restored = ModelRegistry(2.0, model_dir, repository, max_feature_age_s=3600)
    assert restored.status(dataset)["deployable"] is True
    assert restored.performance()["validated"] is False
    assert restored.candidate()[1]["model_version"] == "ml-rejected-new"
    assert repository.list_model_candidates(1)[0]["deployment_status"] == "NOT_DEPLOYABLE"


def test_experimental_observation_survives_restart_and_scores_only_next_round(tmp_path):
    start = datetime.now(timezone.utc) - timedelta(seconds=130)
    rows = [{"round_id": str(i), "round_index": i,
             "timestamp": (start + timedelta(seconds=i)).isoformat(),
             "multiplier": 2.1 if i % 2 else 1.2} for i in range(1, 131)]
    raw = tmp_path / "rounds.json"
    raw.write_text(json.dumps(rows))
    dataset = DatasetService(raw, tmp_path / "processed", tmp_path / "features")
    dataset.build_training_dataset(persist=False)
    names, digest, version = feature_schema(dataset.dataset)
    class FixedCandidate:
        def predict_proba(self, array):
            assert array.shape[1] == len(names)
            return np.array([[0.6, 0.4]])
    metadata = {"model_version": "experimental-1", "feature_names": names,
                "feature_schema_hash": digest, "feature_version": version,
                "validated": False, "class_distribution": {"splits": {"train": {"positive_rate": 0.5}}}}
    repo = Repository(tmp_path / "observations.sqlite3")
    repo.init()
    observer = CandidateObserver(repo, SimpleNamespace(candidate=lambda: (FixedCandidate(), metadata)), 600)
    forecast = observer.observe(dataset, collector_latest_round_id="130")
    assert forecast["usable"] is False and forecast["probability_2x"] == 0.4
    assert observer.observe(dataset, collector_latest_round_id="130")["observation_id"] == forecast["observation_id"]
    assert observer.observe(dataset, collector_latest_round_id="129") is None
    assert len(repo.list_ml_observations()) == 1

    # A fresh repository instance models a stopped and restarted backend.
    restored = CandidateObserver(Repository(tmp_path / "observations.sqlite3"),
                                 SimpleNamespace(candidate=lambda: (FixedCandidate(), metadata)), 600)
    assert restored.status()["latest"]["status"] == "PENDING"
    rows.append({"round_id": "131", "round_index": 131,
                 "timestamp": (datetime.now(timezone.utc) + timedelta(seconds=2)).isoformat(),
                 "multiplier": 2.7})
    raw.write_text(json.dumps(rows))
    dataset.process_incremental()
    assert restored.reconcile(dataset.clean_rounds) == 1
    assert restored.reconcile(dataset.clean_rounds) == 0
    scored = restored.status()
    assert scored["latest"]["status"] == "SCORED"
    assert scored["latest"]["outcome_2x"] == 1
    assert scored["scored_count"] == 1
    assert scored["mean_brier_score"] == (1 - 0.4) ** 2


def test_gap_does_not_count_as_experimental_success(tmp_path):
    repository = Repository(tmp_path / "gaps.sqlite3")
    repository.init()
    created = datetime.now(timezone.utc) - timedelta(seconds=10)
    payload = {"observation_id": "id", "model_version": "m", "source_round_id": "1",
               "source_round_index": 1, "created_at": created.isoformat(),
               "status": "PENDING", "probability_2x": 0.7,
               "baseline_probability_2x": 0.5}
    repository.save_ml_observation(payload)
    import pandas as pd
    rounds = pd.DataFrame([{"round_id": "1", "round_index": 1, "timestamp": created.isoformat(), "multiplier": 1.5},
                           {"round_id": "other", "round_index": 2,
                            "timestamp": (created + timedelta(seconds=180)).isoformat(), "multiplier": 2.5}])
    observer = CandidateObserver(repository, SimpleNamespace(candidate=lambda: (None, None)))
    assert observer.reconcile(rounds) == 1
    assert repository.list_ml_observations()[0]["status"] == "UNKNOWN"
    missing = {**payload, "observation_id": "missing-next", "source_round_id": "other",
               "source_round_index": 2}
    repository.save_ml_observation(missing)
    later = pd.concat([rounds, pd.DataFrame([{"round_id": "5", "round_index": 5,
                  "timestamp": (created + timedelta(seconds=220)).isoformat(), "multiplier": 1.2}])],
                      ignore_index=True)
    assert observer.reconcile(later) == 1
    assert repository.list_ml_observations(pending=True) == []
