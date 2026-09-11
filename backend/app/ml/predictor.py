from __future__ import annotations

import numpy as np
import pandas as pd

from app.services.feature_engineering import build_current_features


class MLPredictor:
    def predict(self, trainer_result, rounds: pd.DataFrame, target: float = 2.0) -> dict:
        if not trainer_result or not getattr(trainer_result, "validated", False):
            return {"validated": False, "status": "MODEL NOT VALIDATED", "probability": None, "models": {}}
        models = getattr(trainer_result, "_models", {})
        feature_cols = trainer_result.ensemble.get("feature_columns", [])
        weights = trainer_result.ensemble.get("weights", {})
        features = build_current_features(rounds["multiplier"].astype(float).tolist(), target)
        row = np.array([[float(features.get(col, 0.0)) for col in feature_cols]], dtype=float)
        outputs = {}
        for name, model in models.items():
            outputs[name] = float(model.predict_proba(row)[0, 1])
        if not outputs:
            return {"validated": False, "status": "MODEL NOT VALIDATED", "probability": None, "models": {}}
        probability = sum(outputs[name] * weights.get(name, 0.0) for name in outputs)
        return {"validated": True, "status": "VALIDATED", "probability": probability, "models": outputs, "weights": weights}

