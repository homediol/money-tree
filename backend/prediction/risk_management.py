"""
prediction/risk_management.py
==============================
Translates a model prediction into actionable betting guidance.

Skip Quality Guard
------------------
Implements four inter-dependent safety rules:

1. COOLDOWN — after 3 consecutive SKIP failures, force BET/LOW for
   COOLDOWN_ROUNDS rounds before allowing SKIP again.

2. SKIP ACCURACY override — if skip accuracy drops below 40% over the
   last 20 predictions, override to BET with LOW risk until it recovers.

3. SKIP CONFIDENCE THRESHOLD — only recommend SKIP when:
       confidence > 70%  AND  historical skip success rate > 60%
   If either condition fails, downgrade SKIP → BET.

4. ENGINE-AWARE THRESHOLDS — statistical_ensemble has a lower confidence
   ceiling so a lower threshold applies to avoid always returning SKIP.
"""

from __future__ import annotations

import logging
import sys
import threading
from collections import deque
from pathlib import Path
from typing import Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from utils import (
    CATEGORIES,
    MIN_CONFIDENCE as _MIN_CONF_TF,
    MIN_CONFIDENCE_STATISTICAL as _MIN_CONF_STAT,
    category_to_recommended_cashout as recommended_cashout,
    risk_level,
    read_json,
    DECISIONS_PATH,
)

log = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────

SKIP_CONF_THRESHOLD      = 70.0   # minimum confidence to recommend SKIP
SKIP_SUCCESS_RATE_MIN    = 0.60   # historical skip success rate floor
SKIP_ACCURACY_FLOOR      = 0.40   # if skip accuracy < this → override to BET
SKIP_ACCURACY_WINDOW     = 20     # decisions window for accuracy calculation
CONSECUTIVE_FAIL_LIMIT   = 3      # consecutive skip failures before cooldown
COOLDOWN_ROUNDS          = 5      # rounds of forced BET after cooldown trigger

# VERY_HIGH guard constants
VH_MULTIPLIER_FLOOR      = 3.0    # predicted cashout must imply >= this
VH_MIN_CONFIDENCE        = 50.0   # minimum confidence for VERY_HIGH
VH_ACTUAL_FLOOR          = 4.0    # historical actuals must average >= this
VH_FP_WINDOW             = 10     # window for false-positive rate check
VH_FP_DISABLE_THRESHOLD  = 0.30   # if success rate < 30% → disable VERY_HIGH

# Suggested bet fraction of bankroll (Kelly-inspired, conservative)
_BET_FRACTION = {
    "VERY_LOW":  0.05,
    "LOW":       0.08,
    "MEDIUM":    0.05,
    "HIGH":      0.03,
    "VERY_HIGH": 0.02,
}


def _threshold(engine: str) -> float:
    return _MIN_CONF_STAT if "statistical" in engine else _MIN_CONF_TF


def confidence_band(confidence: float) -> str:
    """
    Classify confidence into actionable bands.
    Calibrated for statistical_ensemble engine (max ~40%) and TF engine (max ~70%).

    90–100% = STRONG_BET   (very rare, TF only)
    55–89%  = STRONG_BET
    40–54%  = BET
    28–39%  = WEAK_BET
    Below 28% = SKIP
    """
    try:
        from utils import CONF_BAND_STRONG, CONF_BAND_BET, CONF_BAND_WEAK
    except ImportError:
        CONF_BAND_STRONG, CONF_BAND_BET, CONF_BAND_WEAK = 55.0, 40.0, 28.0

    if confidence >= CONF_BAND_STRONG:  return "STRONG_BET"
    if confidence >= CONF_BAND_BET:     return "BET"
    if confidence >= CONF_BAND_WEAK:    return "WEAK_BET"
    return "SKIP"


def bet_fraction(category: str, confidence: float, engine: str = "") -> float:
    """
    Suggested bankroll fraction scaled by confidence band.
    STRONG_BET → 100% of base fraction
    BET        →  75%
    WEAK_BET   →  40%
    SKIP       →   0%
    """
    band = confidence_band(confidence)
    if band == "SKIP":
        return 0.0
    base  = _BET_FRACTION.get(category, 0.03)
    scale = {"STRONG_BET": 1.0, "BET": 0.75, "WEAK_BET": 0.40}.get(band, 0.0)
    return round(base * scale, 4)


def action(category: str, confidence: float, engine: str = "") -> str:
    """BET / WEAK_BET / SKIP based on confidence band."""
    band = confidence_band(confidence)
    if band == "SKIP":
        return "SKIP"
    if band == "WEAK_BET":
        # Only take WEAK_BET on safer categories
        return "BET" if category in ("VERY_LOW", "LOW") else "SKIP"
    return "BET"


# ── Skip Quality Guard ────────────────────────────────────────────────────

class SkipQualityGuard:
    """
    Thread-safe guard that enforces skip quality rules across calls.

    State is maintained in memory (resets on server restart) and derived
    from the last N resolved decisions on first access so historical data
    is respected even after a restart.
    """

    def __init__(self) -> None:
        self._lock               = threading.Lock()
        self._initialised        = False

        # Rolling window of resolved skip outcomes: True=success, False=fail
        self._skip_outcomes: deque = deque(maxlen=SKIP_ACCURACY_WINDOW)

        # Consecutive skip failures (resets on any BET or skip success)
        self._consecutive_fails: int = 0

        # Cooldown counter (rounds remaining in forced-BET cooldown)
        self._cooldown_remaining: int = 0

    # ── Initialisation from historical data ───────────────────────────

    def _ensure_initialised(self) -> None:
        """Load the last SKIP_ACCURACY_WINDOW resolved decisions on first call."""
        if self._initialised:
            return
        try:
            decisions = read_json(DECISIONS_PATH, [])
            if not isinstance(decisions, list):
                decisions = []
            resolved = [
                d for d in decisions
                if d.get("action") == "SKIP" and d.get("actual_multiplier") is not None
            ]
            for d in resolved[-SKIP_ACCURACY_WINDOW:]:
                outcome = d.get("correct", False)
                self._skip_outcomes.append(bool(outcome))
            log.debug(
                "SkipQualityGuard initialised with %d historical skip outcomes",
                len(self._skip_outcomes),
            )
        except Exception as exc:
            log.warning("SkipQualityGuard init failed: %s", exc)
        self._initialised = True

    # ── Public metrics ────────────────────────────────────────────────

    def skip_success_rate(self) -> Optional[float]:
        """
        Fraction of historical SKIPs that were 'successful'.
        A SKIP is successful when the actual multiplier was LOW or VERY_LOW
        (i.e., skipping was the right call — the round crashed early).
        Returns None when there is insufficient history.
        """
        with self._lock:
            self._ensure_initialised()
            if len(self._skip_outcomes) < 3:
                return None
            return sum(self._skip_outcomes) / len(self._skip_outcomes)

    def skip_accuracy(self) -> Optional[float]:
        """Alias for skip_success_rate — used in override check."""
        return self.skip_success_rate()

    def cooldown_active(self) -> bool:
        with self._lock:
            return self._cooldown_remaining > 0

    def status(self) -> Dict:
        with self._lock:
            self._ensure_initialised()
            rate = (
                sum(self._skip_outcomes) / len(self._skip_outcomes)
                if self._skip_outcomes else None
            )
            return {
                "skip_success_rate":     round(rate, 4) if rate is not None else None,
                "consecutive_fails":     self._consecutive_fails,
                "cooldown_remaining":    self._cooldown_remaining,
                "skip_outcomes_tracked": len(self._skip_outcomes),
            }

    # ── Core gate ─────────────────────────────────────────────────────

    def apply(
        self,
        raw_action: str,
        confidence: float,
        category:   str,
        engine:     str = "",
    ) -> Dict:
        """
        Apply all skip quality rules and return a final action dict:

            {
              "action":         "BET" | "SKIP",
              "skip_override":  reason_str | None,   # why SKIP was overridden
              "skip_quality":   { ... }               # guard metrics
            }
        """
        with self._lock:
            self._ensure_initialised()

            override_reason: Optional[str] = None

            # Decrement cooldown counter each call (= each round)
            if self._cooldown_remaining > 0:
                self._cooldown_remaining -= 1

            if raw_action == "SKIP":
                final_action, override_reason = self._evaluate_skip(confidence)
            else:
                final_action = "BET"

            quality = {
                "skip_success_rate":  round(
                    sum(self._skip_outcomes) / len(self._skip_outcomes), 4
                ) if self._skip_outcomes else None,
                "consecutive_fails":    self._consecutive_fails,
                "cooldown_remaining":   self._cooldown_remaining,
                "skip_outcomes_window": len(self._skip_outcomes),
            }

            return {
                "action":        final_action,
                "skip_override": override_reason,
                "skip_quality":  quality,
            }

    def _evaluate_skip(self, confidence: float) -> tuple[str, Optional[str]]:
        """
        Evaluate whether a raw SKIP should be honoured or overridden.
        Returns (final_action, override_reason).
        Must be called with self._lock held.
        """
        rate = (
            sum(self._skip_outcomes) / len(self._skip_outcomes)
            if self._skip_outcomes else None
        )

        # Rule 1 — cooldown active → force BET
        if self._cooldown_remaining > 0:
            return "BET", (
                f"Cooldown active ({self._cooldown_remaining} rounds remaining) "
                f"after {CONSECUTIVE_FAIL_LIMIT} consecutive skip failures"
            )

        # Rule 2 — skip accuracy below floor → override to BET
        if rate is not None and rate < SKIP_ACCURACY_FLOOR:
            return "BET", (
                f"Skip accuracy {rate*100:.1f}% < {SKIP_ACCURACY_FLOOR*100:.0f}% floor "
                f"(last {len(self._skip_outcomes)} decisions) — overriding to BET/LOW"
            )

        # Rule 3 — skip confidence + success rate gate
        if confidence < SKIP_CONF_THRESHOLD:
            return "BET", (
                f"Confidence {confidence:.1f}% < {SKIP_CONF_THRESHOLD:.0f}% "
                "threshold required for SKIP"
            )

        if rate is not None and rate < SKIP_SUCCESS_RATE_MIN:
            return "BET", (
                f"Skip success rate {rate*100:.1f}% < {SKIP_SUCCESS_RATE_MIN*100:.0f}% "
                "required for SKIP"
            )

        return "SKIP", None

    # ── Feedback loop — call after each resolved round ────────────────

    def record_outcome(self, action_taken: str, was_correct: bool) -> None:
        """
        Record the outcome of a completed decision so the guard can update
        its state for future predictions.

        Call this after backfill populates actual_multiplier.
        """
        with self._lock:
            self._ensure_initialised()
            if action_taken == "SKIP":
                self._skip_outcomes.append(was_correct)
                if was_correct:
                    self._consecutive_fails = 0
                else:
                    self._consecutive_fails += 1
                    if self._consecutive_fails >= CONSECUTIVE_FAIL_LIMIT:
                        self._cooldown_remaining = COOLDOWN_ROUNDS
                        log.warning(
                            "SkipQualityGuard: %d consecutive skip failures — "
                            "activating %d-round cooldown",
                            self._consecutive_fails,
                            COOLDOWN_ROUNDS,
                        )
                        self._consecutive_fails = 0
            elif action_taken == "BET":
                # A BET resets the consecutive fail counter
                self._consecutive_fails = 0


# ── Module-level singleton ────────────────────────────────────────────────

_guard: Optional[SkipQualityGuard] = None
_guard_lock = threading.Lock()


def get_skip_guard() -> SkipQualityGuard:
    global _guard
    with _guard_lock:
        if _guard is None:
            _guard = SkipQualityGuard()
    return _guard


# ── VERY_HIGH False-Positive Guard ───────────────────────────────────────

class VeryHighGuard:
    """
    Thread-safe guard that prevents VERY_HIGH misclassification.

    Rules applied in order:
      1. MULTIPLIER FLOOR — if the recommended cashout implies < 3.0×,
         VERY_HIGH is illegal regardless of confidence.
      2. VALIDATION LAYER — VERY_HIGH requires confidence > 50% AND
         the rolling average of historical VERY_HIGH actuals >= 4.0×.
      3. FALSE-POSITIVE FILTER — if success rate across the last 10
         VERY_HIGH predictions < 30%, temporarily redirect to HIGH
         until rate recovers.
    """

    def __init__(self) -> None:
        self._lock         = threading.Lock()
        self._initialised  = False
        # (actual_multiplier, was_correct) for last VH_FP_WINDOW VH predictions
        self._vh_outcomes: deque = deque(maxlen=VH_FP_WINDOW)
        self._disabled     = False   # True while false-positive rate < threshold

    def _ensure_initialised(self) -> None:
        if self._initialised:
            return
        try:
            decisions = read_json(DECISIONS_PATH, [])
            if not isinstance(decisions, list):
                decisions = []
            vh_resolved = [
                d for d in decisions
                if d.get("prediction") == "VERY_HIGH"
                and d.get("actual_multiplier") is not None
            ]
            for d in vh_resolved[-VH_FP_WINDOW:]:
                self._vh_outcomes.append({
                    "actual":  float(d["actual_multiplier"]),
                    "correct": bool(d.get("correct", False)),
                })
            self._update_disabled_state()
            log.debug(
                "VeryHighGuard initialised with %d historical VH outcomes (disabled=%s)",
                len(self._vh_outcomes), self._disabled,
            )
        except Exception as exc:
            log.warning("VeryHighGuard init failed: %s", exc)
        self._initialised = True

    def _update_disabled_state(self) -> None:
        """Recompute disabled flag from current outcomes."""
        if len(self._vh_outcomes) < 3:
            self._disabled = False
            return
        rate = sum(1 for o in self._vh_outcomes if o["correct"]) / len(self._vh_outcomes)
        self._disabled = rate < VH_FP_DISABLE_THRESHOLD

    def status(self) -> Dict:
        with self._lock:
            self._ensure_initialised()
            outcomes = list(self._vh_outcomes)
            rate = (
                sum(1 for o in outcomes if o["correct"]) / len(outcomes)
                if outcomes else None
            )
            avg_actual = (
                sum(o["actual"] for o in outcomes) / len(outcomes)
                if outcomes else None
            )
            return {
                "disabled":           self._disabled,
                "success_rate":       round(rate, 4) if rate is not None else None,
                "avg_actual_mult":    round(avg_actual, 4) if avg_actual is not None else None,
                "outcomes_tracked":   len(outcomes),
            }

    def apply(self, prediction: str, confidence: float, cashout: float) -> Dict:
        """
        Check whether the prediction should stay as VERY_HIGH or be
        downgraded to HIGH.

        Returns:
            {
              "prediction":         final category,
              "vh_downgrade_reason": reason | None,
            }
        """
        if prediction != "VERY_HIGH":
            return {"prediction": prediction, "vh_downgrade_reason": None}

        with self._lock:
            self._ensure_initialised()

            # Rule 1 — multiplier floor
            if cashout < VH_MULTIPLIER_FLOOR:
                return {
                    "prediction": "HIGH",
                    "vh_downgrade_reason": (
                        f"Multiplier floor: cashout {cashout:.2f}× "
                        f"< {VH_MULTIPLIER_FLOOR}× minimum for VERY_HIGH"
                    ),
                }

            # Rule 2 — confidence validation
            if confidence < VH_MIN_CONFIDENCE:
                return {
                    "prediction": "HIGH",
                    "vh_downgrade_reason": (
                        f"Confidence {confidence:.1f}% "
                        f"< {VH_MIN_CONFIDENCE:.0f}% required for VERY_HIGH"
                    ),
                }

            # Rule 2 — historical actual floor (if we have data)
            if self._vh_outcomes:
                avg_actual = sum(o["actual"] for o in self._vh_outcomes) / len(self._vh_outcomes)
                if avg_actual < VH_ACTUAL_FLOOR:
                    return {
                        "prediction": "HIGH",
                        "vh_downgrade_reason": (
                            f"Historical VERY_HIGH actuals avg {avg_actual:.2f}× "
                            f"< {VH_ACTUAL_FLOOR}× floor "
                            f"(last {len(self._vh_outcomes)} predictions)"
                        ),
                    }

            # Rule 3 — false-positive filter
            if self._disabled:
                rate = sum(1 for o in self._vh_outcomes if o["correct"]) / len(self._vh_outcomes)
                return {
                    "prediction": "HIGH",
                    "vh_downgrade_reason": (
                        f"False-positive filter: VERY_HIGH success rate "
                        f"{rate*100:.1f}% < {VH_FP_DISABLE_THRESHOLD*100:.0f}% "
                        f"(last {len(self._vh_outcomes)} predictions) — "
                        "redirecting to HIGH"
                    ),
                }

            return {"prediction": "VERY_HIGH", "vh_downgrade_reason": None}

    def record_outcome(self, prediction: str, actual_multiplier: float, was_correct: bool) -> None:
        """Call after backfill resolves a VERY_HIGH prediction."""
        if prediction != "VERY_HIGH":
            return
        with self._lock:
            self._ensure_initialised()
            self._vh_outcomes.append({
                "actual":  actual_multiplier,
                "correct": was_correct,
            })
            self._update_disabled_state()


# ── Module-level singletons ───────────────────────────────────────────────

_guard: Optional[SkipQualityGuard] = None
_vh_guard: Optional[VeryHighGuard] = None
_guard_lock = threading.Lock()


def get_skip_guard() -> SkipQualityGuard:
    global _guard
    with _guard_lock:
        if _guard is None:
            _guard = SkipQualityGuard()
    return _guard


def get_vh_guard() -> VeryHighGuard:
    global _vh_guard
    with _guard_lock:
        if _vh_guard is None:
            _vh_guard = VeryHighGuard()
    return _vh_guard


# ── full_guidance ─────────────────────────────────────────────────────────

def full_guidance(prediction_result: Dict) -> Dict:
    """
    Augment a raw predictor result with full risk management fields,
    including:
      1. Bayesian-weighted probability calibration
      2. VERY_HIGH false-positive guard
      3. Skip quality guard
      4. Recalibration mode override
    """
    # ── Step 1: Bayesian calibration + recalibration mode ─────────────────
    from prediction.calibration_engine import get_calibration_engine
    calibrated = get_calibration_engine().apply(prediction_result)

    cat    = calibrated.get("prediction", "VERY_LOW")
    conf   = float(calibrated.get("confidence", 0))
    engine = calibrated.get("engine", "")

    # ── Step 1b: Confidence calibration (inversion + correction factor) ───
    from prediction.confidence_calibrator import get_confidence_calibrator
    _cal_result = get_confidence_calibrator().calibrate(conf)
    conf = _cal_result["calibrated_conf"]
    calibrated["confidence"]            = conf
    calibrated["conf_calibrated_conf"]  = _cal_result["calibrated_conf"]
    calibrated["conf_correction_factor"] = _cal_result["correction_factor"]
    calibrated["conf_inverted"]         = _cal_result["inverted"]
    calibrated["conf_bin"]              = _cal_result["bin"]
    calibrated["conf_reason"]           = _cal_result["reason"]

    has_risk = "action" in calibrated and "bet_fraction" in calibrated

    # ── Step 2: compute baseline cashout ──────────────────────────────────
    cashout = recommended_cashout(cat, conf)
    rl      = risk_level(cat, conf)

    # ── Step 2b: risk-tier validation (MEDIUM gate) ───────────────────────
    from prediction.risk_tier_validator import get_risk_tier_validator
    rtv_result = get_risk_tier_validator().validate(rl, conf, cashout, engine)
    rl         = rtv_result["risk_level"]              # may be downgraded LOW
    rcs        = rtv_result["risk_confidence_score"]
    rtv_reason = rtv_result["downgrade_reason"]

    # If MEDIUM was downgraded, recompute cashout using recalibrated params
    if rtv_reason and rtv_result.get("medium_params"):
        mp      = rtv_result["medium_params"]
        conf_n  = max(0.0, min(conf, 100.0)) / 100.0
        cashout = round(mp["base"] + (mp["top"] - mp["base"]) * conf_n, 2)

    # ── Step 3: VERY_HIGH guard ────────────────────────────────────────────
    vh_result    = get_vh_guard().apply(cat, conf, cashout)
    cat          = vh_result["prediction"]
    vh_downgrade = vh_result["vh_downgrade_reason"]

    if vh_downgrade:
        cashout = recommended_cashout(cat, conf)
        rl      = risk_level(cat, conf)

    # ── Step 4: raw action ─────────────────────────────────────────────────
    if has_risk and "statistical" in engine and not vh_downgrade:
        raw_action = calibrated["action"]
    else:
        raw_action = action(cat, conf, engine)

    # ── Step 5: recalibration mode forces SKIP ────────────────────────────
    if calibrated.get("recalibration_active"):
        raw_action = "SKIP"

    # ── Step 6: skip quality guard ─────────────────────────────────────────
    guard        = get_skip_guard()
    guard_result = guard.apply(raw_action, conf, cat, engine)
    final_action = guard_result["action"]

    # Recalibration always wins over skip guard — keep SKIP
    if calibrated.get("recalibration_active"):
        final_action = "SKIP"

    # If SKIP was overridden to BET by skip guard, force LOW-risk parameters
    if raw_action == "SKIP" and final_action == "BET" and not calibrated.get("recalibration_active"):
        cat_override = "LOW"
        cashout      = recommended_cashout(cat_override, conf)
        rl           = "LOW"
        bf           = _BET_FRACTION.get(cat_override, 0.05)
    else:
        cat_override = None
        bf = (
            calibrated.get("bet_fraction")
            if has_risk and "statistical" in engine and not vh_downgrade
            else bet_fraction(cat, conf, engine)
        )

    result = {
        **calibrated,
        "prediction":             cat,
        "recommended_cashout":    cashout,
        "risk_level":             rl,
        "action":                 final_action,
        "bet_fraction":           bf,
        "skip_override":          guard_result["skip_override"],
        "skip_quality":           guard_result["skip_quality"],
        "vh_downgrade_reason":    vh_downgrade,
        "risk_tier_downgrade":    rtv_reason,
        "risk_confidence_score":  rcs,
        "medium_locked":          rtv_result.get("medium_locked", False),
        "medium_gates":           rtv_result.get("gates"),
        "medium_params":          rtv_result.get("medium_params"),
    }

    if cat_override:
        result["risk_override_category"] = cat_override

    return result
