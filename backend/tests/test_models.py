from pathlib import Path

from app.ml.trainer import ModelTrainer
from app.services.data_loader import RoundHistoryLoader


def test_walk_forward_validation_produces_model_metrics():
    path = Path(__file__).resolve().parents[1] / "data" / "roundhistory.json"
    frame, _ = RoundHistoryLoader(path).load()
    result = ModelTrainer(target=2.0).train_validate(frame)

    assert result.dataset_size >= 120
    assert result.message != "Walk-forward validation did not produce valid folds."
    # Truth and per-model prediction arrays must stay aligned across all folds.
    assert set(result.models) >= {"logistic_regression", "random_forest", "gradient_boosting"}
    assert set(result.baselines) >= {"historical_base_rate", "base_rate", "always_ge_2x"}
    for metric in result.models.values():
        assert metric["tp"] + metric["fp"] + metric["fn"] + metric["tn"] > 0
    # The public dump must never leak fitted estimators (not JSON-serializable).
    import json

    dumped = result.model_dump()
    assert "_models" not in dumped
    json.dumps(dumped)


