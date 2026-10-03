from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

from app.ml.opportunity import (OpportunityResearchEngine, _best_validation_threshold,
                                build_opportunity_frame, validated_rounds)
from app.database.repository import Repository


def rounds(values):
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return pd.DataFrame([{
        "round_id": f"round-{index + 1}", "round_index": index + 1,
        "timestamp": (start + timedelta(seconds=index * 10)).isoformat(),
        "multiplier": value,
    } for index, value in enumerate(values)])


def test_opportunity_features_for_a_target_use_only_prior_rounds():
    values = [1.1 + (index % 7) / 10 for index in range(180)]
    changed_future = list(values)
    changed_future[120] = 8.0
    changed_future[121:] = [5.0] * (len(values) - 121)

    first, _ = build_opportunity_frame(rounds(values))
    second, _ = build_opportunity_frame(rounds(changed_future))
    a = first.loc[first.target_round_index == 121].iloc[0]
    b = second.loc[second.target_round_index == 121].iloc[0]
    feature_columns = [name for name in first.columns if name not in {
        "target_hit", "target_multiplier_value", "target_round_id", "target_round_index",
        "source_round_id", "source_round_index"}]

    assert a[feature_columns].to_dict() == b[feature_columns].to_dict()
    assert int(a.target_hit) == 0
    assert int(b.target_hit) == 1


def test_validation_rejects_duplicate_ids_and_indices_but_keeps_equal_real_observations():
    data = rounds([1.5, 1.5, 1.5, 2.2])
    data.loc[1, "timestamp"] = data.loc[0, "timestamp"]
    data.loc[2, "round_id"] = data.loc[0, "round_id"]
    data.loc[3, "round_index"] = data.loc[0, "round_index"]

    clean, quality = validated_rounds(data)

    assert len(clean) == 2
    assert quality["duplicate_id"] == 1
    assert quality["duplicate_index"] == 1
    assert quality["sequence_replay"] == 0


def test_live_features_are_built_after_the_latest_observed_round():
    values = [1.2] * 115 + [3.0, 1.1, 4.0, 1.5, 2.4]
    latest = OpportunityResearchEngine._latest_features(rounds(values))

    assert latest["observed_prior_5"] == 5
    assert latest["rate_ge_2_1_5"] == 3 / 5


def test_threshold_search_uses_validation_quantiles_and_abstains_without_lift():
    probabilities = np.full(400, .5)
    labels = np.tile([0, 1], 200)
    result = _best_validation_threshold(labels, probabilities, float(labels.mean()))

    assert result["threshold"] == .5
    assert result["selection_qualified"] is False
    assert all(row["selection_qualified"] is False for row in result["curves"])


class RepositoryStub:
    def __init__(self):
        self.state = {"status": "NOT_EVALUATED", "shadow_ready": False}

    def load_application_state(self, _key):
        return self.state

    def list_opportunity_observations(self, *, status=None, limit=200):
        return []


def test_unvalidated_detector_returns_no_signal_and_abstains():
    engine = OpportunityResearchEngine(RepositoryStub(), "/tmp/selective-opportunity-test")
    result = engine.current(rounds([1.2] * 130))

    assert result["current_opportunity"]["status"] == "NO_SIGNAL"
    assert result["current_opportunity"]["abstain"] is True
    assert result["current_opportunity"]["reason_codes"] == ["no_validated_selective_model"]


def test_shadow_prediction_is_frozen_and_resolves_only_once(tmp_path):
    repository = Repository(tmp_path / "opportunity.sqlite3")
    repository.init()
    payload = {"observation_id": "obs", "model_version": "v1", "source_round_id": "r100",
               "source_round_index": 100, "created_at": "2026-01-01T00:00:00Z",
               "status": "PENDING", "signal": True, "probability_2_1x": .72}

    frozen = repository.save_opportunity_observation(payload)
    repository.resolve_opportunity_observation(frozen | {"status": "SCORED", "outcome_2_1x": 1})
    duplicate = repository.save_opportunity_observation(payload | {"probability_2_1x": .05})

    assert duplicate["probability_2_1x"] == .72
    assert duplicate["status"] == "SCORED"
    assert duplicate["outcome_2_1x"] == 1
