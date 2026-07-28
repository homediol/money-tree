"""
training/evaluate_model.py
===========================
Tests the saved model against the most recent N rounds and writes a
detailed evaluation log to logs/evaluation.jsonl (one JSON per line).

Run:
    cd backend && python -m training.evaluate_model --rounds 200
"""

from __future__ import annotations

import argparse
import json
import logging
import pickle
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

CATEGORIES  = ["VERY_LOW", "LOW", "MEDIUM", "HIGH", "VERY_HIGH"]
ROOT        = Path(__file__).resolve().parent.parent.parent
MODELS_DIR  = ROOT / "models"
LOGS_DIR    = Path(__file__).resolve().parent.parent / "logs"
LOGS_DIR.mkdir(parents=True, exist_ok=True)
EVAL_LOG    = LOGS_DIR / "evaluation.jsonl"

log = logging.getLogger(__name__)


def _category(v: float) -> str:
    if v < 1.50: return "VERY_LOW"
    if v < 2.00: return "LOW"
    if v < 5.00: return "MEDIUM"
    if v < 15.0: return "HIGH"
    return "VERY_HIGH"


def evaluate(n_recent: int = 200) -> dict:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s: %(message)s")

    # --- Load artefacts ---
    model_path   = MODELS_DIR / "model.keras"
    scaler_path  = MODELS_DIR / "scaler.pkl"

    if not model_path.exists():
        raise FileNotFoundError(f"No model at {model_path}. Run train_model first.")
    if not scaler_path.exists():
        raise FileNotFoundError(f"No scaler at {scaler_path}.")

    try:
        from tensorflow import keras
    except ImportError as e:
        raise RuntimeError("TensorFlow not installed.") from e

    import numpy as np

    model = keras.models.load_model(str(model_path))
    with open(scaler_path, "rb") as f:
        scaler = pickle.load(f)

    # --- Load rounds ---
    from utils import load_round_history
    from training.feature_engineering import build_feature_matrix

    rounds = load_round_history()
    if len(rounds) < 25:
        raise ValueError("Need at least 25 rounds to evaluate.")

    multipliers = [r["multiplier"] for r in rounds]

    # Build features for the last n_recent rounds
    window_size = 20
    X_all, y_all = build_feature_matrix(multipliers, window_size=window_size)
    # Each sample i corresponds to multipliers[window_size + i]
    start_idx = len(multipliers) - len(X_all)

    # Take last n_recent
    X_recent = X_all[-n_recent:]
    y_recent = y_all[-n_recent:]
    rounds_recent = rounds[start_idx:][-n_recent:]

    X_np = scaler.transform(np.array(X_recent, dtype="float32"))
    probs = model.predict(X_np, verbose=0)
    preds = np.argmax(probs, axis=1)

    # --- Write evaluation log ---
    results = []
    correct = 0
    for i, (pred_idx, true_idx, prob_row, rd) in enumerate(
        zip(preds, y_recent, probs, rounds_recent)
    ):
        pred_cat   = CATEGORIES[int(pred_idx)]
        actual_cat = CATEGORIES[int(true_idx)]
        conf       = round(float(prob_row[pred_idx]) * 100, 2)
        is_correct = pred_cat == actual_cat
        if is_correct:
            correct += 1

        entry = {
            "timestamp":        datetime.now(timezone.utc).isoformat(),
            "round_id":         rd.get("round_id"),
            "actual_multiplier":rd.get("multiplier"),
            "actual_category":  actual_cat,
            "prediction":       pred_cat,
            "confidence":       conf,
            "result":           "Correct" if is_correct else "Wrong",
            "probabilities":    {
                CATEGORIES[j]: round(float(prob_row[j]) * 100, 2)
                for j in range(len(CATEGORIES))
            },
        }
        results.append(entry)

    # Append to JSONL log
    with open(EVAL_LOG, "a") as f:
        for entry in results:
            f.write(json.dumps(entry) + "\n")

    accuracy    = round(correct / len(results) * 100, 2) if results else 0.0
    from collections import Counter
    pred_dist   = Counter(CATEGORIES[int(p)] for p in preds)
    dominant    = pred_dist.most_common(1)[0]
    bias        = dominant[1] / len(preds) > 0.70

    summary = {
        "evaluated":       len(results),
        "correct":         correct,
        "accuracy_pct":    accuracy,
        "prediction_dist": dict(pred_dist),
        "bias_detected":   bias,
        "bias_warning":    f"Model bias detected: '{dominant[0]}' = {dominant[1]} / {len(preds)} predictions"
                           if bias else None,
    }

    log.info("Evaluation complete: %d rounds  accuracy=%.2f%%", len(results), accuracy)
    if bias:
        log.warning(summary["bias_warning"])

    # Save summary
    with open(LOGS_DIR / "last_evaluation.json", "w") as f:
        json.dump(summary, f, indent=2)

    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--rounds", type=int, default=200)
    args = parser.parse_args()
    result = evaluate(args.rounds)
    print(json.dumps(result, indent=2))
