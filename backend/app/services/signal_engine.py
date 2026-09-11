from __future__ import annotations

import pandas as pd

from app.ml.ensemble import weighted_average
from app.ml.predictor import MLPredictor
from app.ml.model_registry import ModelRegistry
from app.services.feature_engineering import build_current_features
from app.services.pattern_engine import PatternEngine
from app.services.probability_engine import ProbabilityEngine
from app.services.similarity_engine import SimilarityEngine
from app.services.validation_engine import ValidationEngine


class SignalEngine:
    def __init__(self, target: float, min_sample_size: int, signal_threshold: float, strong_signal_threshold: float, model_registry: ModelRegistry):
        self.target = target
        self.min_sample_size = min_sample_size
        self.signal_threshold = signal_threshold
        self.strong_signal_threshold = strong_signal_threshold
        self.patterns = PatternEngine(target, min_sample_size)
        self.probability = ProbabilityEngine(target, min_sample_size)
        self.similarity = SimilarityEngine(target, min_sample_size)
        self.validation = ValidationEngine()
        self.model_registry = model_registry
        self.ml_predictor = MLPredictor()

    def current_analysis(self, rounds: pd.DataFrame) -> dict:
        if rounds.empty:
            return {"label": "STATISTICAL PATTERN ANALYSIS", "status": "INSUFFICIENT DATA", "error": "No valid rounds available."}

        base = self.probability.base_rate(rounds)
        pattern = self.patterns.conditional_for_current(rounds)
        sequence = self.similarity.find_similar(rounds, sequence_length=3, limit=25)
        features = build_current_features(rounds["multiplier"].astype(float).tolist(), self.target)
        ml = self.ml_predictor.predict(self.model_registry.latest, rounds, self.target)

        pattern_prob = pattern.get("probability") if pattern.get("sufficient_data") else None
        sequence_prob = sequence.get("probability") if sequence.get("sufficient_data") else None
        ml_prob = ml.get("probability") if ml.get("validated") else None
        final_probability = weighted_average(
            [
                (pattern_prob, 0.38),
                (sequence_prob, 0.24),
                (ml_prob, 0.28),
                (base.get("probability"), 0.10),
            ]
        )
        if final_probability is None:
            final_probability = base.get("probability")

        evidence_sample = max(int(pattern.get("occurrences") or 0), int(sequence.get("similar_cases") or 0))
        min_sample_ok = bool(pattern.get("sufficient_data") or sequence.get("sufficient_data"))
        confidence = self.validation.confidence(evidence_sample, final_probability, base.get("probability"), self.min_sample_size)
        status = self.validation.status(final_probability, confidence, bool(ml.get("validated")), min_sample_ok, self.signal_threshold, self.strong_signal_threshold)

        recent = [round(float(v), 2) for v in rounds["multiplier"].tail(10).tolist()]
        result = {
            "label": "STATISTICAL PATTERN ANALYSIS",
            "target": f"NEXT OUTCOME >= {self.target:.2f}x",
            "current_pattern": pattern,
            "historical_evidence": {
                "matches": pattern.get("occurrences", 0),
                "next_ge_target": pattern.get("success_count", 0),
                "next_below_target": pattern.get("failure_count", 0),
                "historical_rate": pattern.get("probability"),
            },
            "sequence_similarity": sequence,
            "ml_estimate": ml,
            "base_rate": base,
            "features": features,
            "recent_multipliers": recent,
            "final_probability": final_probability,
            "confidence": confidence,
            "status": status,
            "why": [
                "The signal combines current streak evidence, similar historical sequences, model validation, and the historical base rate.",
                "All probabilities are statistical estimates from roundhistory.json and do not guarantee the next outcome.",
            ],
            "data_source": "roundhistory.json",
        }
        return result

    def formatted(self, analysis: dict) -> str:
        p = analysis.get("final_probability")
        pattern = analysis.get("current_pattern", {})
        evidence = analysis.get("historical_evidence", {})
        ml = analysis.get("ml_estimate", {})
        seq = analysis.get("sequence_similarity", {})
        pct = f"{p * 100:.2f}%" if isinstance(p, (int, float)) else "N/A"
        ml_pct = f"{ml.get('probability') * 100:.2f}%" if isinstance(ml.get("probability"), (int, float)) else "MODEL NOT VALIDATED"
        seq_pct = f"{seq.get('probability') * 100:.2f}%" if isinstance(seq.get("probability"), (int, float)) else "INSUFFICIENT DATA"
        return f"""
────────────────────────────

STATISTICAL PATTERN ANALYSIS

TARGET:
{analysis.get('target')}

CURRENT PATTERN:
{pattern.get('label')}

HISTORICAL MATCHES:
{evidence.get('matches', 0)}

NEXT >= 2X:
{evidence.get('next_ge_target', 0)}

HISTORICAL RATE:
{(evidence.get('historical_rate') or 0) * 100:.2f}%

ML ESTIMATE:
{ml_pct}

SEQUENCE SIMILARITY:
{seq_pct}

FINAL ESTIMATED PROBABILITY:
{pct}

CONFIDENCE:
{analysis.get('confidence')}

STATUS:
{analysis.get('status')}

────────────────────────────

WHY?

The current sequence has appeared in similar historical situations when enough
examples exist. This is statistical evidence only and does not guarantee the
next outcome.

DATA SOURCE:
roundhistory.json

────────────────────────────
""".strip()

