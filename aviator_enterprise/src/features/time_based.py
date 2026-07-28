"""
Time-based feature generators
=============================

Hour of day, day of week, time-since-last-extreme, gap between rounds,
session length, time-of-day cyclical encoding.
"""
from __future__ import annotations

from typing import Optional

import numpy as np
import pandas as pd

from src.core.logger import get_logger

logger = get_logger(__name__)


class TimeBasedFeatures:
    def build(self, df: pd.DataFrame, timestamp_col: str = "ts_utc") -> pd.DataFrame:
        out = df.copy()
        if timestamp_col not in out.columns or out[timestamp_col].isna().all():
            return out

        ts = pd.to_datetime(out[timestamp_col], utc=True, errors="coerce")
        out["hour"] = ts.dt.hour
        out["minute"] = ts.dt.minute
        out["day_of_week"] = ts.dt.dayofweek
        out["day_of_month"] = ts.dt.day
        out["is_weekend"] = (ts.dt.dayofweek >= 5).astype(int)
        out["is_night"] = ((ts.dt.hour < 6) | (ts.dt.hour >= 22)).astype(int)

        # cyclical encoding
        out["hour_sin"] = np.sin(2 * np.pi * out["hour"] / 24.0)
        out["hour_cos"] = np.cos(2 * np.pi * out["hour"] / 24.0)
        out["dow_sin"] = np.sin(2 * np.pi * out["day_of_week"] / 7.0)
        out["dow_cos"] = np.cos(2 * np.pi * out["day_of_week"] / 7.0)
        out["minute_sin"] = np.sin(2 * np.pi * out["minute"] / 60.0)
        out["minute_cos"] = np.cos(2 * np.pi * out["minute"] / 60.0)

        # time-since-last-extreme (>= 10x)
        if "multiplier" in out.columns:
            extreme = (out["multiplier"] >= 10.0).astype(int)
            # cumcount since last extreme
            out["rounds_since_extreme"] = extreme.groupby((extreme == 1).cumsum()).cumcount()
            out["rounds_since_extreme"] = out["rounds_since_extreme"].where(extreme == 0, 0)

        # inter-arrival time
        if "timestamp" in out.columns:
            dt = out["timestamp"].diff()
            out["interarrival_s"] = dt
            out["interarrival_log"] = np.log1p(dt.clip(lower=0))
        return out


__all__ = ["TimeBasedFeatures"]

