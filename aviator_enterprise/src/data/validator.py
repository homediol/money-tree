"""
Data Validator
==============

Schema-aware validation of round records.  All checks are pure functions
that return a list of `ValidationIssue`s so callers can decide what to do
(correct, drop, raise).
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

from src.core.exceptions import DataValidationError
from src.data.collector import RoundRecord
from src.core.logger import get_logger

logger = get_logger(__name__)


class Severity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


@dataclass(slots=True)
class ValidationIssue:
    field: str
    severity: Severity
    message: str
    value: Any = None
    record_id: Optional[int] = None


# ---------------------------------------------------------------------------
# Validators
# ---------------------------------------------------------------------------
class RoundValidator:
    """Validates a batch of RoundRecord objects."""

    MIN_MULTIPLIER = 1.0
    MAX_MULTIPLIER = 1000.0

    def validate(self, records: List[RoundRecord]) -> List[ValidationIssue]:
        issues: List[ValidationIssue] = []
        for r in records:
            issues.extend(self._validate_single(r))
        return issues

    def validate_and_clean(self, records: List[RoundRecord]) -> List[RoundRecord]:
        """Drop records with ERROR severity issues, keep those with warnings."""
        issues = self.validate(records)
        bad_ids = {i.record_id for i in issues if i.severity == Severity.ERROR}
        if bad_ids:
            logger.warning("Dropping invalid records", extra={"count": len(bad_ids)})
        return [r for r in records if r.round_id not in bad_ids]

    # ------------------------------------------------------------- single
    def _validate_single(self, r: RoundRecord) -> List[ValidationIssue]:
        out: List[ValidationIssue] = []
        if r.multiplier < self.MIN_MULTIPLIER:
            out.append(ValidationIssue(
                "multiplier", Severity.ERROR,
                f"multiplier {r.multiplier} below minimum {self.MIN_MULTIPLIER}",
                r.multiplier, r.round_id,
            ))
        if r.multiplier > self.MAX_MULTIPLIER:
            out.append(ValidationIssue(
                "multiplier", Severity.WARNING,
                f"multiplier {r.multiplier} above expected maximum {self.MAX_MULTIPLIER}",
                r.multiplier, r.round_id,
            ))
        if r.round_id <= 0:
            out.append(ValidationIssue(
                "round_id", Severity.ERROR,
                f"round_id {r.round_id} must be positive",
                r.round_id, r.round_id,
            ))
        if r.timestamp is not None and r.timestamp < 0:
            out.append(ValidationIssue(
                "timestamp", Severity.WARNING,
                "negative timestamp", r.timestamp, r.round_id,
            ))
        if not np.isfinite(r.multiplier):
            out.append(ValidationIssue(
                "multiplier", Severity.ERROR,
                "non-finite multiplier", r.multiplier, r.round_id,
            ))
        return out


# ---------------------------------------------------------------------------
# DataFrame-level validator
# ---------------------------------------------------------------------------
class DataFrameValidator:
    """Validates a tabular dataset (after feature engineering)."""

    REQUIRED_COLUMNS = ["multiplier"]

    def validate(self, df: pd.DataFrame) -> List[ValidationIssue]:
        issues: List[ValidationIssue] = []
        for col in self.REQUIRED_COLUMNS:
            if col not in df.columns:
                issues.append(ValidationIssue(col, Severity.ERROR, f"missing required column '{col}'"))

        if "multiplier" in df.columns:
            nulls = int(df["multiplier"].isna().sum())
            if nulls:
                issues.append(ValidationIssue("multiplier", Severity.WARNING, f"{nulls} null values"))
            infs = int(((~np.isfinite(df["multiplier"].fillna(0))).sum()))
            if infs:
                issues.append(ValidationIssue("multiplier", Severity.ERROR, f"{infs} non-finite values"))

        # duplicate index
        if df.index.duplicated().any():
            n = int(df.index.duplicated().sum())
            issues.append(ValidationIssue("index", Severity.WARNING, f"{n} duplicate index rows"))

        # high null ratio
        null_ratio = df.isna().mean()
        bad = null_ratio[null_ratio > 0.5]
        for col, ratio in bad.items():
            issues.append(ValidationIssue(
                col, Severity.WARNING,
                f"column has {ratio:.1%} missing values",
            ))
        return issues

    def raise_if_errors(self, issues: List[ValidationIssue]) -> None:
        errors = [i for i in issues if i.severity == Severity.ERROR]
        if errors:
            raise DataValidationError(
                f"Dataset failed validation with {len(errors)} errors",
                context={"issues": [i.__dict__ for i in errors]},
            )


__all__ = [
    "Severity", "ValidationIssue",
    "RoundValidator", "DataFrameValidator",
]

