from __future__ import annotations


class ValidationEngine:
    def confidence(self, sample_size: int, probability: float | None, base_rate: float | None, min_sample_size: int) -> str:
        if probability is None or sample_size < min_sample_size:
            return "LOW"
        edge = abs(probability - (base_rate or 0.5))
        if sample_size >= min_sample_size * 5 and edge >= 0.12:
            return "HIGH"
        if sample_size >= min_sample_size and edge >= 0.06:
            return "MEDIUM"
        return "LOW"

    def status(self, probability: float | None, confidence: str, model_validated: bool, min_sample_ok: bool, signal_threshold: float, strong_threshold: float) -> str:
        if not min_sample_ok:
            return "INSUFFICIENT DATA"
        if probability is None:
            return "NO SIGNAL"
        if not model_validated:
            return "MODEL NOT VALIDATED"
        if probability >= strong_threshold and confidence in {"MEDIUM", "HIGH"}:
            return "STRONG STATISTICAL SIGNAL"
        if probability >= signal_threshold and confidence in {"MEDIUM", "HIGH"}:
            return "MODERATE STATISTICAL SIGNAL"
        if probability >= signal_threshold:
            return "WEAK SIGNAL"
        return "NO SIGNAL"

