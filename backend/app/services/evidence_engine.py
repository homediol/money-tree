"""Immutable, cutoff-safe explanation and confidence snapshots for ML estimates."""
from __future__ import annotations

import math
import uuid
from datetime import datetime, timezone
from typing import Any

import pandas as pd

from app.services.pattern_engine import PatternEngine, wilson_interval

PATTERN_VERSION = "part5-v1"
SCORE_WEIGHTS = {
    "data_quality": 0.20, "sample_quality": 0.20, "stability": 0.15,
    "model_quality": 0.20, "agreement": 0.15, "recent_stability": 0.10,
}


def _bounded(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def confidence_label(score: float) -> str:
    return ("VERY_LOW" if score < 20 else "LOW" if score < 40 else "MEDIUM"
            if score < 60 else "HIGH" if score < 80 else "VERY_HIGH")


def evidence_strength(sample_size: int, minimum: int, ci: dict, stability: str) -> str:
    if sample_size < minimum:
        return "VERY_WEAK"
    width = 1.0 if ci.get("lower") is None else ci["upper"] - ci["lower"]
    if sample_size < minimum * 2 or width > .30:
        return "WEAK"
    if sample_size < minimum * 3 or width > .20 or stability == "UNSTABLE":
        return "MODERATE"
    if width > .12 or stability != "STABLE":
        return "STRONG"
    return "VERY_STRONG"


def normalized_stability(value: str, sample_size: int, minimum: int) -> str:
    if sample_size < minimum or value == "INSUFFICIENT_DATA":
        return "INSUFFICIENT_DATA"
    return {"STABLE": "STABLE", "WEAK": "MODERATELY_STABLE", "VARIABLE": "UNSTABLE"}.get(value, "UNSTABLE")


def agreement_state(ml: float | None, pattern: float | None, baseline: float | None,
                    sufficient: bool) -> str:
    if ml is None or pattern is None or baseline is None or not sufficient:
        return "INSUFFICIENT_DATA"
    ml_delta, pattern_delta = ml - baseline, pattern - baseline
    if ml_delta * pattern_delta < 0 and abs(ml_delta) >= .05 and abs(pattern_delta) >= .05:
        return "HIGH_DISAGREEMENT"
    if ml_delta * pattern_delta < 0:
        return "MIXED"
    spread = max(ml, pattern, baseline) - min(ml, pattern, baseline)
    if spread <= .05:
        return "STRONG_AGREEMENT"
    if spread <= .10 and ml_delta * pattern_delta >= 0:
        return "MODERATE_AGREEMENT"
    return "MIXED"


class EvidenceEngine:
    def __init__(self, repository, model_registry, *, target: float = 2.0,
                 min_sample_size: int = 30, freshness_seconds: int = 600,
                 analytics_report_engine=None):
        self.repository = repository
        self.model_registry = model_registry
        self.target = float(target)
        self.minimum = int(min_sample_size)
        self.freshness_seconds = freshness_seconds
        self.analytics_report_engine = analytics_report_engine

    def _cutoff(self, dataset_service, source_round_id: str) -> pd.DataFrame:
        rounds = dataset_service.clean_rounds
        matches = rounds.index[rounds["round_id"].astype(str) == str(source_round_id)]
        return rounds.iloc[: int(matches[0]) + 1].copy() if len(matches) else pd.DataFrame()

    def _baselines(self, rounds: pd.DataFrame) -> dict:
        values = rounds["multiplier"].astype(float).tolist()
        result = {}
        for name, window in (("historical", None), ("recent_50", 50), ("recent_250", 250), ("recent_1000", 1000)):
            sample = values if window is None else values[-window:]
            successes = sum(v >= self.target for v in sample)
            result[name] = {"rate": successes / len(sample) if sample else None, "successes": successes,
                            "failures": len(sample) - successes, "sample_size": len(sample),
                            "requested_window": window}
        return result

    def _current_pattern(self, rounds: pd.DataFrame) -> dict:
        engine = PatternEngine(self.target, self.minimum)
        current = engine.current_pattern(rounds)
        threshold = self.target
        length = int(current.get("low_streak", 0))
        bucket = min(length, 6)
        pattern_id = f"streak_below_{str(threshold).replace('.', '_')}_{'6_plus' if bucket == 6 else bucket}_target_{str(self.target).replace('.', '_')}"
        found = next((p for p in engine.streak_patterns(rounds, (threshold,)) if p["pattern_id"] == pattern_id), None)
        if found:
            return found
        return {"pattern_id": pattern_id, "pattern": current.get("label", "No measured streak"),
                "sample_size": 0, "successes": 0, "failures": 0, "success_rate": None,
                "baseline_rate": None, "difference_from_baseline": None,
                "confidence_interval": wilson_interval(0, 0), "stability": "INSUFFICIENT_DATA",
                "test": {"success_rate": None, "sample_size": 0}}

    @staticmethod
    def _metric(metadata: dict, section: str, name: str) -> float | None:
        value = metadata.get(section, {}).get(name)
        return float(value) if isinstance(value, (int, float)) and math.isfinite(value) else None

    def _model_quality(self, prediction: dict) -> dict:
        meta = self.model_registry.metadata_for_version(prediction.get("model_version"))
        test = meta.get("test_metrics") or {}
        calibration = meta.get("calibration") or {}
        checks = meta.get("overfitting_checks") or {}
        baseline = meta.get("baselines") or {}
        brier = test.get("brier_score")
        base_brier = ((baseline.get("test") or {}).get("base_rate_probability") or baseline.get("base_rate") or {}).get("brier_score")
        if base_brier is None:
            base_brier = (baseline.get("historical_probability") or {}).get("test_metrics", {}).get("brier_score")
        ece = calibration.get("test_ece", calibration.get("ece", calibration.get("expected_calibration_error")))
        deployable = bool(checks.get("deployable", False))
        brier_skill = _bounded((base_brier - brier) / base_brier) if isinstance(brier, (int, float)) and isinstance(base_brier, (int, float)) and base_brier else 0.0
        calibration_score = 1.0 - _bounded((ece or .25) / .25)
        score = _bounded(.45 * brier_skill + .35 * calibration_score + .20 * float(deployable))
        return {"available": bool(meta), "model_version": prediction.get("model_version"), "algorithm": meta.get("algorithm"),
                "brier_score": brier, "baseline_brier_score": base_brier, "log_loss": test.get("log_loss"),
                "calibration_ece": ece, "walk_forward": meta.get("walk_forward"), "deployable": deployable,
                "quality_score": score, "quality_state": "VALIDATED" if deployable else "BELOW_BASELINE_OR_UNVALIDATED"}

    def _data_quality(self, prediction: dict, rounds: pd.DataFrame) -> dict:
        last = pd.to_datetime(rounds.iloc[-1]["timestamp_dt"], utc=True) if len(rounds) else None
        created = pd.to_datetime(prediction.get("created_at"), utc=True, errors="coerce")
        age = (created - last).total_seconds() if last is not None and not pd.isna(created) else None
        fresh = age is not None and 0 <= age <= self.freshness_seconds
        raw = prediction.get("data_quality_at_prediction") or {}
        has_raw_snapshot = bool(raw)
        return {"valid_rounds_at_cutoff": len(rounds),
                "invalid_records": raw.get("invalid_rounds", 0),
                "duplicates": raw.get("duplicates", 0),
                "missing_values": raw.get("missing_values", 0),
                "raw_quarantine_at_cutoff": (raw if has_raw_snapshot else None),
                "data_timestamp": str(rounds.iloc[-1]["timestamp"]) if len(rounds) else None,
                "age_seconds_at_prediction": age, "fresh": fresh, "scope": "validated_history_cutoff",
                "quality_score": 1.0 if fresh else .45}

    def build(self, prediction: dict | None, dataset_service) -> dict | None:
        if not prediction:
            return None
        existing = self.repository.evidence_for_prediction(prediction["prediction_id"])
        if existing:
            return existing
        rounds = self._cutoff(dataset_service, prediction["source_round_id"])
        if rounds.empty:
            return None
        baselines = self._baselines(rounds)
        pattern = self._current_pattern(rounds)
        stability = normalized_stability(pattern["stability"], pattern["sample_size"], self.minimum)
        ci = pattern["confidence_interval"]
        strength = evidence_strength(pattern["sample_size"], self.minimum, ci, stability)
        probability = float(prediction["probability_2x"])
        base_rate = baselines["historical"]["rate"]
        agreement = agreement_state(probability, pattern.get("success_rate"), base_rate,
                                    pattern["sample_size"] >= self.minimum)
        model_quality = self._model_quality(prediction)
        data_quality = self._data_quality(prediction, rounds)
        sample_score = min(1.0, pattern["sample_size"] / max(1, self.minimum * 3))
        if ci.get("lower") is not None:
            sample_score *= _bounded(1 - (ci["upper"] - ci["lower"]))
        stability_score = {"STABLE": 1.0, "MODERATELY_STABLE": .65, "UNSTABLE": .25,
                           "INSUFFICIENT_DATA": .05}[stability]
        agreement_score = {"STRONG_AGREEMENT": 1.0, "MODERATE_AGREEMENT": .75, "MIXED": .45,
                           "HIGH_DISAGREEMENT": .15, "INSUFFICIENT_DATA": .20}[agreement]
        recent_rates = [baselines[key]["rate"] for key in ("recent_50", "recent_250", "recent_1000")]
        recent_score = _bounded(1 - max(abs(rate - base_rate) for rate in recent_rates if rate is not None) / .20) if base_rate is not None else 0
        components = {"data_quality": data_quality["quality_score"], "sample_quality": sample_score,
                      "stability": stability_score, "model_quality": model_quality["quality_score"],
                      "agreement": agreement_score, "recent_stability": recent_score}
        score = round(100 * sum(SCORE_WEIGHTS[k] * components[k] for k in SCORE_WEIGHTS), 2)
        # A model that did not beat its held-out baseline cannot support high
        # system confidence, regardless of an attractive pattern rate.
        if not model_quality["deployable"]:
            score = min(score, 39.99)
        if not data_quality["fresh"]:
            score = min(score, 59.99)
        index = {
            "ml.probability": probability, "baseline.historical": base_rate,
            "pattern.rate": pattern.get("success_rate"), "pattern.sample_size": pattern["sample_size"],
            "pattern.confidence_interval": ci, "pattern.stability": stability,
            "model.quality": model_quality, "data.quality": data_quality,
            "agreement.state": agreement, "confidence.components": components,
        }
        explanations = self._explanations(probability, base_rate, pattern, stability, agreement,
                                           model_quality, data_quality, self.minimum)
        features = dataset_service.features_after_round(prediction["source_round_id"])
        selected = {key: features.get(key) for key in ("last_1", "last_2", "last_3", "last_4", "last_5",
                    "streak_below_2", "rate_2x_last_50", "mean_last_25", "std_last_25") if features and key in features}
        now = datetime.now(timezone.utc).isoformat()
        analytics_research = (self.analytics_report_engine.research_features_through(
            prediction["source_round_id"]) if self.analytics_report_engine else None)
        snapshot = {
            "evidence_id": uuid.uuid4().hex, "prediction_id": prediction["prediction_id"],
            "source_round_id": prediction["source_round_id"], "target": prediction.get("target", "next_round_ge_2x"),
            "ml_probability": probability, "predicted_class": prediction.get("predicted_class"),
            "model_version": prediction.get("model_version"), "feature_version": prediction.get("feature_version"),
            "pattern_version": PATTERN_VERSION, "history_count_at_cutoff": len(rounds),
            "history_data_timestamp": str(rounds.iloc[-1]["timestamp"]), "baselines": baselines,
            "pattern": pattern, "model_quality": model_quality, "data_quality": data_quality,
            "relevant_features": selected, "stability": stability, "agreement": agreement,
            "evidence_strength": strength, "confidence": confidence_label(score), "confidence_score": score,
            "confidence_components": components, "confidence_weights": SCORE_WEIGHTS,
            "evidence_index": index, "explanations": explanations,
            "analytics_research_evidence": analytics_research,
            "calculated_at": now, "immutable": True,
            "notice": "Historical evidence and model estimates are uncertain and are not betting decisions.",
        }
        return self.repository.save_evidence_snapshot(snapshot)

    @staticmethod
    def _explanations(ml: float, baseline: float | None, pattern: dict, stability: str,
                      agreement: str, model: dict, data: dict, minimum: int) -> list[dict]:
        items = []
        delta = None if baseline is None else ml - baseline
        items.append({"type": "supporting" if delta is not None and delta > 0 else "reducing",
                      "text": f"The model estimate is {ml:.1%}; the cutoff historical baseline is {baseline:.1%}." if baseline is not None else f"The model estimate is {ml:.1%}; no baseline is available.",
                      "evidence_ref": "ml.probability"})
        rate, n = pattern.get("success_rate"), pattern["sample_size"]
        items.append({"type": "supporting" if rate is not None and baseline is not None and rate > baseline and n >= 30 else "reducing",
                      "text": f"The current pattern was followed by the target {pattern['successes']} of {n} times ({rate:.1%})." if rate is not None else "The current pattern has no observed following outcomes.",
                      "evidence_ref": "pattern.sample_size"})
        if n < minimum:
            items.append({"type": "reducing", "text": f"The pattern sample has {n} observations, below the evidence minimum.", "evidence_ref": "pattern.sample_size"})
        items.append({"type": "supporting" if stability == "STABLE" else "reducing",
                      "text": f"Chronological pattern stability is {stability}.", "evidence_ref": "pattern.stability"})
        items.append({"type": "supporting" if model["deployable"] else "reducing",
                      "text": f"Model validation state is {model['quality_state']}; Brier score is {model.get('brier_score')}." ,
                      "evidence_ref": "model.quality"})
        items.append({"type": "supporting" if data["fresh"] else "reducing",
                      "text": "The feature data was fresh at prediction time." if data["fresh"] else "Feature freshness could not be confirmed at prediction time.",
                      "evidence_ref": "data.quality"})
        items.append({"type": "supporting" if "AGREEMENT" in agreement and agreement != "HIGH_DISAGREEMENT" else "reducing",
                      "text": f"ML, pattern, and baseline evidence are classified as {agreement}.",
                      "evidence_ref": "agreement.state"})
        return items
