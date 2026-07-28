"""
Data Preprocessor
=================

Converts raw `RoundRecord`s into a tidy pandas DataFrame and handles:
  * chronological ordering
  * missing-value interpolation
  * outlier detection (|z| > 5 capped)
  * derived `category` and `log_multiplier` columns
"""
from __future__ import annotations

from typing import List, Optional

import numpy as np
import pandas as pd

from config.settings import settings
from src.core.helpers import safe_float
from src.data.collector import RoundRecord
from src.core.logger import get_logger

logger = get_logger(__name__)


class Preprocessor:
    """Stateless transformer from `RoundRecord` list to a DataFrame."""

    OUTLIER_Z_THRESHOLD = 5.0

    def transform(
        self,
        records: List[RoundRecord],
        *,
        boundaries: Optional[List[float]] = None,
        names: Optional[List[str]] = None,
    ) -> pd.DataFrame:
        if not records:
            return pd.DataFrame()

        boundaries = boundaries or settings.category_boundary_list()
        names = names or settings.category_name_list()

        df = pd.DataFrame([r.to_dict() for r in records])
        df = df.sort_values("round_id").reset_index(drop=True)

        # parse timestamps
        if "timestamp" in df.columns:
            df["timestamp"] = pd.to_numeric(df["timestamp"], errors="coerce")
            df["ts_utc"] = pd.to_datetime(df["timestamp"], unit="s", errors="coerce", utc=True)
            df["ts_utc"] = df["ts_utc"].ffill().bfill()

        # cap outliers in log space
        log_mult = np.log1p(df["multiplier"].astype(float))
        z = (log_mult - log_mult.mean()) / (log_mult.std() + 1e-9)
        mask = z.abs() > self.OUTLIER_Z_THRESHOLD
        if mask.any():
            logger.warning("Capping outliers", extra={"count": int(mask.sum())})
            df.loc[mask, "multiplier"] = np.expm1(
                log_mult.mean() + np.sign(z[mask]) * self.OUTLIER_Z_THRESHOLD * log_mult.std()
            )

        # interpolate any NaN multipliers
        if df["multiplier"].isna().any():
            df["multiplier"] = df["multiplier"].interpolate(method="linear", limit_direction="both")

        # derived
        df["category"] = [self._cat_idx(m, boundaries) for m in df["multiplier"]]
        df["category_name"] = [names[i] for i in df["category"]]
        df["log_multiplier"] = np.log1p(df["multiplier"])
        df["interarrival_s"] = df["timestamp"].diff() if "timestamp" in df.columns else 0.0
        return df

    @staticmethod
    def _cat_idx(m: float, boundaries: List[float]) -> int:
        for i, b in enumerate(boundaries):
            if m < b:
                return i
        return len(boundaries)


__all__ = ["Preprocessor"]

