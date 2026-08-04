"""
prediction/predictor.py
========================
Production predictor. Loads model.keras + scaler.pkl when available,
otherwise uses the high-quality statistical ensemble fallback.

Falls back to statistical_ensemble if TF is unavailable or model not trained yet.
Detects single-class bias automatically.
"""

from __future__ import annotations

import logging
import pickle
import sys
import threading
from collections import Counter
from pathlib import Path
from typing import Dict, List, Optional

# Ensure backend root is on sys.path
_BACKEND = Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from utils import (
    CATEGORIES,
    MIN_CONFIDENCE,
    MIN_CONFIDENCE_TO_STORE,
    MIN_CONFIDENCE_STATISTICAL,
    category_to_recommended_cashout as _cashout,
    risk_level as _risk,
)

NUM_CLASSES    = len(CATEGORIES)
WINDOW_SIZE    = 20
BIAS_THRESHOLD = 0.70

ROOT        = Path(__file__).resolve().parent.parent.parent
MODELS_DIR  = ROOT / "models"
MODEL_PATH  = MODELS_DIR / "model.keras"
SCALER_PATH = MODELS_DIR / "scaler.pkl"

log = logging.getLogger(__name__)

_load_lock = threading.Lock()

# Rolling prediction history for bias detection
_recent_preds: List[str] = []
_BIAS_WINDOW = 50


def _cat_index(v: float) -> int:
    if v < 1.50: return 0
    if v < 2.00: return 1
    if v < 5.00: return 2
    if v < 15.0: return 3
    return 4


def _check_bias(prediction: str) -> Optional[str]:
    _recent_preds.append(prediction)
    if len(_recent_preds) > _BIAS_WINDOW:
        _recent_preds.pop(0)
    if len(_recent_preds) < 10:
        return None
    dist = Counter(_recent_preds)
    top_cat, top_count = dist.most_common(1)[0]
    ratio = top_count / len(_recent_preds)
    if ratio > BIAS_THRESHOLD:
        return f"Model bias detected: '{top_cat}' = {ratio*100:.1f}% of last {len(_recent_preds)} predictions."
    return None


class AviatorPredictorV2:
    """
    Production predictor. Singleton — call get_predictor() to get the instance.
    Uses TF LSTM model when available, statistical ensemble otherwise.
    """

    def __init__(self):
        self._model  = None
        self._scaler = None
        self._loaded = False

    def _load(self) -> bool:
        """Attempt to load model + scaler. Returns True on success."""
        if self._loaded:
            return self._model is not None

        with _load_lock:
            if self._loaded:
                return self._model is not None

            if not MODEL_PATH.exists() or not SCALER_PATH.exists():
                log.warning("Model or scaler not found — using statistical ensemble.")
                self._loaded = True
                return False

            try:
                from tensorflow import keras
                import numpy as np  # noqa: F401

                self._model = keras.models.load_model(str(MODEL_PATH))
                with open(SCALER_PATH, "rb") as f:
                    self._scaler = pickle.load(f)
                self._loaded = True
                log.info("AviatorPredictorV2 loaded model from %s", MODEL_PATH)
                return True
            except ImportError as exc:
                log.warning("ML dependency missing (%s) — using statistical ensemble.", exc)
                self._loaded = True
                return False
            except Exception as exc:
                log.error("Failed to load model: %s — using statistical ensemble.", exc)
                self._loaded = True
                return False

    def predict(self, multipliers: List[float]) -> Dict:
        if len(multipliers) < WINDOW_SIZE:
            raise ValueError(f"Need at least {WINDOW_SIZE} rounds.")
        if self._load():
            lstm_result = self._predict_tf(multipliers)
            try:
                from prediction.rf_predictor import get_rf_predictor, blend_predictions
                rf_result = get_rf_predictor().predict(multipliers)
                if rf_result is not None:
                    return blend_predictions(lstm_result, rf_result,
                                             lstm_weight=0.10, rf_weight=0.90)
            except Exception as exc:
                log.debug("RF blend skipped: %s", exc)
            return lstm_result
        return self._predict_statistical(multipliers)

    # ── TF path ───────────────────────────────────────────────────────

    def _predict_tf(self, multipliers: List[float]) -> Dict:
        """Run prediction with the true-sequence LSTM (20, 8) input."""
        from training.train_model import build_sequence, _apply_seq_scaler, SEQ_LEN
        from prediction.statistical_predictor import _vol_regime, _streak, _cat, _recent_trend
        import numpy as np

        # Build (1, 20, 8) input sequence
        window   = multipliers[-SEQ_LEN:]
        seq      = build_sequence(window)               # (20, 8) list
        X_raw    = np.array([seq], dtype="float32")     # (1, 20, 8)
        X        = _apply_seq_scaler(self._scaler, X_raw)

        probs_raw = self._model.predict(X, verbose=0)[0]

        total = float(probs_raw.sum())
        probs = {CATEGORIES[i]: round(float(probs_raw[i]) / total * 100, 2)
                 for i in range(NUM_CLASSES)}
        diff = round(100.0 - sum(probs.values()), 2)
        top  = max(probs, key=probs.get)
        probs[top] = round(probs[top] + diff, 2)

        prediction = max(probs, key=probs.get)
        confidence = probs[prediction]
        bias_warn  = _check_bias(prediction)

        # Compute regime/streak/trend for contextual signals
        cats       = [_cat(v) for v in multipliers[-20:]]
        regime     = _vol_regime(multipliers[-20:])
        streak_cat, streak_len = _streak(cats)
        trend      = _recent_trend(multipliers)

        # Risk management
        from prediction.statistical_predictor import get_risk_params
        min_conf, bet_frac_base = get_risk_params(prediction, regime)
        if confidence >= min_conf:
            excess     = (confidence - min_conf) / max(100.0 - min_conf, 1.0)
            bet_frac   = round(bet_frac_base * (0.6 + 0.4 * excess), 4)
            rec_action = "BET"
        else:
            bet_frac   = 0.0
            rec_action = "SKIP"

        result = {
            "prediction":          prediction,
            "confidence":          confidence,
            "recommended_cashout": _cashout(prediction, confidence),
            "risk_level":          _risk(prediction, confidence),
            "probabilities":       probs,
            "engine":              "tensorflow_lstm_seq",
            "low_confidence":      confidence < MIN_CONFIDENCE,
            "bias_warning":        bias_warn,
            "regime":              regime,
            "streak":              {"category": streak_cat, "length": streak_len},
            "trend":               trend,
            "action":              rec_action,
            "bet_fraction":        bet_frac,
        }
        if bias_warn:
            log.warning(bias_warn)
        return result

    # ── Statistical ensemble path ──────────────────────────────────────

    def _predict_statistical(self, multipliers: List[float]) -> Dict:
        """
        Multi-layer statistical predictor:
          - 10-round sequence memory (pattern matching like LSTM)
          - Volatility regime detection
          - Streak/momentum signals
          - Built-in risk management layer (BET/SKIP thresholds per regime)
        """
        from prediction.statistical_predictor import get_statistical_predictor
        from utils import ARTIFACT_DIR, write_json

        stat_model = get_statistical_predictor(multipliers)
        result     = stat_model.predict(multipliers)

        probs      = result["probs"]
        prediction = result["prediction"]
        confidence = result["confidence"]
        bias_warn  = _check_bias(prediction)

        # The model's own risk layer already computed action + bet_fraction
        # based on per-regime thresholds — use those directly
        action_val   = result.get("action", "SKIP")
        bet_frac_val = result.get("bet_fraction", 0.0)

        # Keep fallback_model.json in sync for backwards compat
        try:
            cats        = [_cat_index(v) for v in multipliers]
            counts      = {c: 2 for c in CATEGORIES}
            transitions = {c: {n: 1 for n in CATEGORIES} for c in CATEGORIES}
            for idx, lbl in enumerate(cats):
                cat = CATEGORIES[lbl]
                counts[cat] += 1
                if idx:
                    prev = CATEGORIES[cats[idx - 1]]
                    transitions[prev][cat] += 1
            write_json(ARTIFACT_DIR / "fallback_model.json",
                       {"counts": counts, "transitions": transitions})
        except Exception:
            pass

        return {
            "prediction":          prediction,
            "confidence":          confidence,
            "recommended_cashout": _cashout(prediction, confidence),
            "risk_level":          _risk(prediction, confidence),
            "probabilities":       probs,
            "engine":              "statistical_ensemble",
            "low_confidence":      confidence < MIN_CONFIDENCE_STATISTICAL,
            "bias_warning":        bias_warn,
            "regime":              result.get("regime"),
            "certainty":           result.get("certainty"),
            "streak":              result.get("streak"),
            "trend":               result.get("trend"),
            "seq_hit":             result.get("seq_hit", False),
            # Override action/bet_fraction from the risk management layer
            "action":              action_val,
            "bet_fraction":        bet_frac_val,
        }


# ── module-level singleton ────────────────────────────────────────────────

_predictor: Optional[AviatorPredictorV2] = None


def get_predictor() -> AviatorPredictorV2:
    global _predictor
    if _predictor is None:
        _predictor = AviatorPredictorV2()
    return _predictor
