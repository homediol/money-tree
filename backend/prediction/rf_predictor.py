"""
prediction/rf_predictor.py
===========================
RandomForest inference wrapper.

Loads rf_model.joblib + rf_scaler.pkl and provides a predict()
method returning the same dict format as AviatorPredictorV2.

The RF predictor is used as a second opinion alongside the LSTM.
When both engines are available, their probability vectors are
blended (60% LSTM + 40% RF) to improve calibration.
"""

from __future__ import annotations

import logging
import pickle
import sys
from pathlib import Path
from typing import Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from utils import (
    CATEGORIES,
    MIN_CONFIDENCE,
    MIN_CONFIDENCE_STATISTICAL,
    category_to_recommended_cashout as _cashout,
    risk_level as _risk,
)

log = logging.getLogger(__name__)

ROOT           = Path(__file__).resolve().parent.parent.parent
MODELS_DIR     = ROOT / "models"
RF_MODEL_PATH  = MODELS_DIR / "rf_model.joblib"
RF_SCALER_PATH = MODELS_DIR / "rf_scaler.pkl"

NUM_CLASSES = len(CATEGORIES)


class RFPredictor:
    """Singleton RF predictor — call get_rf_predictor() to obtain."""

    def __init__(self):
        self._model  = None
        self._scaler = None
        self._loaded = False

    def _load(self) -> bool:
        if self._loaded:
            return self._model is not None
        if not RF_MODEL_PATH.exists() or not RF_SCALER_PATH.exists():
            log.warning("RF model/scaler not found — RF predictor unavailable.")
            self._loaded = True
            return False
        try:
            import joblib
            # Prefer calibrated model (isotonic) over raw RF
            cal_path = MODELS_DIR / "rf_calibrated.joblib"
            model_path = cal_path if cal_path.exists() else RF_MODEL_PATH
            self._model = joblib.load(model_path)
            with open(RF_SCALER_PATH, "rb") as fh:
                self._scaler = pickle.load(fh)
            self._loaded = True
            label = "calibrated RF" if cal_path.exists() else "raw RF"
            log.info("RFPredictor loaded %s from %s", label, model_path)
            return True
        except Exception as exc:
            log.error("Failed to load RF model: %s", exc)
            self._loaded = True
            return False

    def predict(self, multipliers: List[float]) -> Optional[Dict]:
        """
        Return probability dict or None if RF is unavailable.

        Returns dict with keys matching AviatorPredictorV2 output.
        """
        if not self._load():
            return None

        from training.feature_engineering import compute_features
        import numpy as np

        window   = multipliers[-20:]
        features = compute_features(window)
        X        = np.array([features], dtype="float32")
        X_s      = self._scaler.transform(X)

        proba = self._model.predict_proba(X_s)[0]   # shape (5,)

        # predict_proba respects class order of rf.classes_
        # Map back to CATEGORIES order
        classes = list(self._model.classes_)
        probs   = {}
        for i, cat in enumerate(CATEGORIES):
            idx = classes.index(i) if i in classes else None
            probs[cat] = round(float(proba[idx]) * 100, 2) if idx is not None else 0.0

        # Fix rounding residual
        diff     = round(100.0 - sum(probs.values()), 2)
        top_cat  = max(probs, key=probs.get)
        probs[top_cat] = round(probs[top_cat] + diff, 2)

        prediction = max(probs, key=probs.get)
        confidence = probs[prediction]

        from prediction.statistical_predictor import (
            _vol_regime, _streak, _cat, _recent_trend, get_risk_params
        )
        cats       = [_cat(v) for v in multipliers[-20:]]
        regime     = _vol_regime(multipliers[-20:])
        streak_cat, streak_len = _streak(cats)
        trend      = _recent_trend(multipliers)

        min_conf, bet_frac_base = get_risk_params(prediction, regime)
        if confidence >= min_conf:
            excess     = (confidence - min_conf) / max(100.0 - min_conf, 1.0)
            bet_frac   = round(bet_frac_base * (0.6 + 0.4 * excess), 4)
            rec_action = "BET"
        else:
            bet_frac   = 0.0
            rec_action = "SKIP"

        return {
            "prediction":          prediction,
            "confidence":          confidence,
            "recommended_cashout": _cashout(prediction, confidence),
            "risk_level":          _risk(prediction, confidence),
            "probabilities":       probs,
            "engine":              "random_forest",
            "low_confidence":      confidence < MIN_CONFIDENCE,
            "regime":              regime,
            "streak":              {"category": streak_cat, "length": streak_len},
            "trend":               trend,
            "action":              rec_action,
            "bet_fraction":        bet_frac,
        }


# ── Module singleton ──────────────────────────────────────────────────────

_rf_predictor: Optional[RFPredictor] = None


def get_rf_predictor() -> RFPredictor:
    global _rf_predictor
    if _rf_predictor is None:
        _rf_predictor = RFPredictor()
    return _rf_predictor


# ── Ensemble blend helper ─────────────────────────────────────────────────

def blend_predictions(
    lstm_result: Dict,
    rf_result: Optional[Dict],
    lstm_weight: float = 0.60,
    rf_weight: float   = 0.40,
) -> Dict:
    """
    Blend LSTM and RF probability vectors.
    NaN-safe: any bad probability value is replaced with uniform before blending.
    Returns the LSTM result unchanged if RF is unavailable.
    """
    if rf_result is None:
        return lstm_result

    import math

    def _safe_prob(probs: Dict, cat: str) -> float:
        """Get a probability value, returning uniform fallback if NaN/missing."""
        v = probs.get(cat, 0.0)
        try:
            fv = float(v)
            return fv if math.isfinite(fv) else 100.0 / NUM_CLASSES
        except (TypeError, ValueError):
            return 100.0 / NUM_CLASSES

    lstm_probs = lstm_result.get("probabilities", {})
    rf_probs   = rf_result.get("probabilities", {})

    # Weighted average — NaN-safe
    blended = {}
    for cat in CATEGORIES:
        lp = _safe_prob(lstm_probs, cat) / 100.0
        rp = _safe_prob(rf_probs,   cat) / 100.0
        blended[cat] = lstm_weight * lp + rf_weight * rp

    # Normalise to exactly 100%
    total = sum(blended.values()) or 1.0
    blended = {c: round(v / total * 100, 2) for c, v in blended.items()}
    diff    = round(100.0 - sum(blended.values()), 2)
    top_cat = max(blended, key=blended.get)
    blended[top_cat] = round(blended[top_cat] + diff, 2)

    prediction = max(blended, key=blended.get)
    confidence = blended[prediction]

    # Safe confidence values for metadata
    lstm_conf = lstm_result.get("confidence", 0.0)
    rf_conf   = rf_result.get("confidence", 0.0)
    lstm_conf = lstm_conf if math.isfinite(float(lstm_conf or 0)) else 0.0
    rf_conf   = rf_conf   if math.isfinite(float(rf_conf   or 0)) else 0.0

    # Pick contextual signals from higher-confidence model
    if rf_conf > lstm_conf:
        regime   = rf_result.get("regime")
        streak   = rf_result.get("streak")
        trend    = rf_result.get("trend")
    else:
        regime   = lstm_result.get("regime")
        streak   = lstm_result.get("streak")
        trend    = lstm_result.get("trend")

    # Recalculate action/bet_fraction from blended confidence
    from prediction.statistical_predictor import get_risk_params
    min_conf, bet_base = get_risk_params(prediction, regime or "medium")
    if confidence >= min_conf:
        excess   = (confidence - min_conf) / max(100.0 - min_conf, 1.0)
        bet_frac = round(bet_base * (0.6 + 0.4 * excess), 4)
        action   = "BET"
    else:
        bet_frac = 0.0
        action   = "SKIP"

    return {
        **lstm_result,
        "prediction":          prediction,
        "confidence":          confidence,
        "recommended_cashout": _cashout(prediction, confidence),
        "risk_level":          _risk(prediction, confidence),
        "probabilities":       blended,
        "engine":              "lstm_rf_ensemble",
        "lstm_confidence":     lstm_conf,
        "rf_confidence":       rf_conf,
        "regime":              regime,
        "streak":              streak,
        "trend":               trend,
        "action":              action,
        "bet_fraction":        bet_frac,
    }
