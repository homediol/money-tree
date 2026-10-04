"""Record and score experimental next-round probabilities without execution access."""
from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone

import numpy as np

from app.ml.trainer import feature_schema

log = logging.getLogger("APP.ML.observer")


def _time(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


class CandidateObserver:
    """Only the ML diagnostics page reads this stream; it cannot authorize bets."""

    def __init__(self, repository, registry, max_feature_age_s=600):
        self.repository = repository
        self.registry = registry
        self.max_feature_age_s = max_feature_age_s
        self.last_skip_reason = None
        self.last_attempt_at = None

    def _skip(self, reason):
        self.last_skip_reason = reason
        return None

    def reconcile(self, rounds) -> int:
        if rounds.empty:
            return 0
        by_index = {int(row.round_index): row for row in rounds.itertuples(index=False)}
        latest_index = max(by_index)
        resolved = 0
        for observation in self.repository.list_ml_observations(pending=True, limit=10000):
            source_index = int(observation["source_round_index"])
            target = by_index.get(source_index + 1)
            if target is None:
                if latest_index > source_index + 1:
                    observation.update(status="UNKNOWN", reason="next_round_missing_from_collected_history",
                                       resolved_at=datetime.now(timezone.utc).isoformat())
                    self.repository.resolve_ml_observation(observation)
                    resolved += 1
                continue
            source = by_index.get(source_index)
            created = _time(observation["created_at"])
            target_stored_at = _time(getattr(target, "stored_at", None))
            contiguous = (source is not None and str(source.round_id) == observation["source_round_id"]
                          and int(target.round_index) == source_index + 1
                          and str(target.round_id) != str(source.round_id)
                          and bool(getattr(source, "continuity_verified", False))
                          and bool(getattr(target, "continuity_verified", False))
                          and not bool(getattr(target, "gap_before", False)))
            before_outcome = created is not None and target_stored_at is not None and created < target_stored_at
            if contiguous and before_outcome:
                outcome = int(float(target.multiplier) >= 2)
                probability = float(observation["probability_2x"])
                baseline = float(observation["baseline_probability_2x"])
                observation.update(status="SCORED", target_round_id=str(target.round_id), outcome_2x=outcome,
                                   actual_multiplier=float(target.multiplier),
                                   brier_score=(outcome - probability) ** 2,
                                   baseline_brier_score=(outcome - baseline) ** 2,
                                   resolved_at=datetime.now(timezone.utc).isoformat())
            else:
                observation.update(status="UNKNOWN", target_round_id=str(target.round_id),
                                   reason="round_gap_or_prediction_created_after_outcome",
                                   resolved_at=datetime.now(timezone.utc).isoformat())
            self.repository.resolve_ml_observation(observation)
            resolved += 1
        return resolved

    def observe(self, dataset_service, *, collector_latest_round_id=None):
        self.last_attempt_at = datetime.now(timezone.utc).isoformat()
        self.last_skip_reason = None
        model, metadata = self.registry.candidate()
        rounds = dataset_service.clean_rounds
        if model is None or not metadata:
            return self._skip("candidate_artifact_unavailable")
        if rounds.empty or dataset_service.dataset.empty:
            return self._skip("training_data_unavailable")
        source = rounds.iloc[-1]
        source_id = str(source["round_id"])
        if collector_latest_round_id is not None and str(collector_latest_round_id) != source_id:
            return self._skip("collector_and_dataset_latest_round_differ")
        age = _time(source.get("observed_at") or source.get("timestamp"))
        if age is None or not 0 <= (datetime.now(timezone.utc) - age).total_seconds() <= self.max_feature_age_s:
            return self._skip("source_round_stale_or_future")
        if int(dataset_service.quality.get("latest_contiguous_rounds", 0)) < 100:
            return self._skip("fewer_than_100_contiguous_rounds")
        names, schema_hash, feature_version = feature_schema(dataset_service.dataset)
        if (metadata.get("feature_names") != names or metadata.get("feature_schema_hash") != schema_hash
                or metadata.get("feature_version") != feature_version):
            return self._skip("candidate_feature_schema_mismatch")
        features = dataset_service.latest_features()
        if not features:
            return self._skip("latest_features_unavailable")
        try:
            row = np.array([[float(features[name]) for name in names]], dtype=float)
            if not np.isfinite(row).all():
                return self._skip("nonfinite_latest_features")
            probability = float(model.predict_proba(row)[0, 1])
        except Exception as exc:
            log.warning("experimental prediction failed: %s", exc)
            return self._skip("candidate_prediction_failed")
        if not np.isfinite(probability) or not 0 <= probability <= 1:
            return self._skip("invalid_candidate_probability")
        baseline = ((metadata.get("class_distribution") or {}).get("splits") or {}).get("train", {}).get("positive_rate")
        if baseline is None:
            return self._skip("frozen_training_baseline_unavailable")
        version = metadata["model_version"]
        observation_id = hashlib.sha256(f"{version}|{source_id}".encode()).hexdigest()
        observation = {
            "observation_id": observation_id, "model_version": version,
            "source_round_id": source_id, "source_round_index": int(source["round_index"]),
            "source_round_timestamp": str(source["timestamp"]), "created_at": datetime.now(timezone.utc).isoformat(),
            "probability_2x": probability, "baseline_probability_2x": float(baseline),
            "status": "PENDING", "usable": False, "experimental": True,
            "reason": "candidate_not_deployed" if not metadata.get("validated") else "observation_only",
        }
        return self.repository.save_ml_observation(observation)

    def status(self, limit=50):
        _, candidate = self.registry.candidate()
        version = candidate.get("model_version") if candidate else None
        observations = self.repository.list_ml_observations(limit=limit, model_version=version) if version else []
        scored = [item for item in observations if item["status"] == "SCORED"]
        status = "NO_CANDIDATE" if not version else "WAITING" if self.last_skip_reason else "OBSERVING"
        return {"status": status, "usable": False,
                "model_version": version, "candidate_deployable": bool(candidate and candidate.get("validated")),
                "last_skip_reason": self.last_skip_reason, "last_attempt_at": self.last_attempt_at,
                "latest": observations[0] if observations else None,
                "scored_count": len(scored),
                "mean_brier_score": sum(item["brier_score"] for item in scored) / len(scored) if scored else None,
                "baseline_mean_brier_score": sum(item["baseline_brier_score"] for item in scored) / len(scored) if scored else None,
                "observations": observations}
