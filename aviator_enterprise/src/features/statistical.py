"""
Statistical Feature Generator (Enterprise v3)
==============================================

Generates a comprehensive set of rolling and distributional features
from the raw multiplier series:

  * Rolling statistics (mean, std, min, max, median, skewness, kurtosis)
  * Exponential moving averages (EMA) and EMA deviation
  * Lag features and lag differences
  * Volatility (historical std-dev, variance, Parkinson range estimator)
  * Momentum (rate of change, log-returns, trend strength, acceleration)
  * Streak analysis (consecutive high/low runs, streak mean)
  * Frequency analysis (fraction of rounds in each category per window)
  * Shannon entropy of rolling distribution
  * Z-scores at multiple lookback windows
  * Distance from rolling extrema
  * Rolling percentile ranks (CDFs)
"""
from __future__ import annotations

from typing import List, Optional

import numpy as np
import pandas as pd
from scipy import stats as sp_stats

from config.settings import settings
from src.core.logger import get_logger

logger = get_logger(__name__)

# Bin boundaries for frequency / entropy features
_FREQ_BINS = [1.5, 2.0, 5.0, 15.0]
_FREQ_LABELS = ["vlow_freq", "low_freq", "med_freq", "high_freq"]


class StatisticalFeatures:
    """
    Generates all rolling and distributional features from the
    multiplier series.  Pure functions; no state is mutated on
    the input DataFrame.
    """

    def __init__(
        self,
        rolling_windows: Optional[List[int]] = None,
        ema_spans: Optional[List[int]] = None,
        lag_periods: Optional[List[int]] = None,
        volatility_windows: Optional[List[int]] = None,
    ) -> None:
        self.rolling_windows = rolling_windows or settings.rolling_window_list()
        self.ema_spans = ema_spans or settings.ema_span_list()
        self.lag_periods = lag_periods or settings.lag_period_list()
        self.volatility_windows = volatility_windows or settings.volatility_window_list()

    # ---------------------------------------------------------------- public
    def build(self, df: pd.DataFrame, target_col: str = "multiplier") -> pd.DataFrame:
        out = df.copy()
        x = out[target_col].astype(float)
        log_x = np.log1p(x.clip(lower=1.0))

        out = self._add_rolling(out, x, log_x)
        out = self._add_ema(out, x, log_x)
        out = self._add_lag(out, x, log_x)
        out = self._add_volatility(out, x, log_x)
        out = self._add_momentum(out, x, log_x)
        out = self._add_streak(out, x)
        out = self._add_frequency(out, x)
        out = self._add_entropy(out, x)
        out = self._add_zscore(out, log_x)
        out = self._add_extrema(out, x)
        out = self._add_percentile(out, x)
        return out

    # ---------------------------------------------------------------- rolling
    def _add_rolling(
        self, out: pd.DataFrame, x: pd.Series, log_x: pd.Series
    ) -> pd.DataFrame:
        for w in self.rolling_windows:
            if w > len(x):
                continue
            r = x.rolling(w, min_periods=1)
            rl = log_x.rolling(w, min_periods=1)
            out[f"roll_mean_{w}"] = r.mean()
            out[f"roll_std_{w}"] = r.std().fillna(0.0)
            out[f"roll_min_{w}"] = r.min()
            out[f"roll_max_{w}"] = r.max()
            out[f"roll_median_{w}"] = r.median()
            out[f"roll_sum_{w}"] = r.sum()
            out[f"roll_logmean_{w}"] = rl.mean()
            out[f"roll_logstd_{w}"] = rl.std().fillna(0.0)
            out[f"roll_skew_{w}"] = x.rolling(w, min_periods=4).skew().fillna(0.0)
            out[f"roll_kurt_{w}"] = x.rolling(w, min_periods=8).kurt().fillna(0.0)
            # Range normalised
            rng = (out[f"roll_max_{w}"] - out[f"roll_min_{w}"]).clip(lower=1e-9)
            out[f"roll_range_{w}"] = rng
            out[f"roll_norm_{w}"] = (x - out[f"roll_min_{w}"]) / rng
        return out

    # ---------------------------------------------------------------- EMA
    def _add_ema(
        self, out: pd.DataFrame, x: pd.Series, log_x: pd.Series
    ) -> pd.DataFrame:
        for s in self.ema_spans:
            out[f"ema_{s}"] = x.ewm(span=s, adjust=False).mean()
            out[f"ema_log_{s}"] = log_x.ewm(span=s, adjust=False).mean()
            out[f"ema_diff_{s}"] = out[f"ema_{s}"] - x
            # EMA of variance (EWMA volatility estimate)
            out[f"ema_vol_{s}"] = x.ewm(span=s, adjust=False).std().fillna(0.0)
        return out

    # ---------------------------------------------------------------- lag
    def _add_lag(
        self, out: pd.DataFrame, x: pd.Series, log_x: pd.Series
    ) -> pd.DataFrame:
        for lag in self.lag_periods:
            out[f"lag_{lag}"] = x.shift(lag)
            out[f"lag_log_{lag}"] = log_x.shift(lag)
            out[f"lag_diff_{lag}"] = x - x.shift(lag)
            out[f"lag_ratio_{lag}"] = x / x.shift(lag).clip(lower=1.0)
        return out

    # ---------------------------------------------------------------- volatility
    def _add_volatility(
        self, out: pd.DataFrame, x: pd.Series, log_x: pd.Series
    ) -> pd.DataFrame:
        log_ret = log_x.diff()
        for w in self.volatility_windows:
            if w > len(x):
                continue
            out[f"vol_{w}"] = log_ret.rolling(w, min_periods=2).std().fillna(0.0)
            out[f"vol_var_{w}"] = log_ret.rolling(w, min_periods=2).var().fillna(0.0)
            # EWMA volatility (alternative estimator)
            out[f"ewma_vol_{w}"] = (
                log_ret.ewm(span=w, adjust=False).std().fillna(0.0)
            )
            # Parkinson range estimator (uses rolling high/low)
            if f"roll_min_{w}" in out and f"roll_max_{w}" in out:
                with np.errstate(invalid="ignore", divide="ignore"):
                    hl = np.log1p(
                        out[f"roll_max_{w}"] / out[f"roll_min_{w}"].clip(lower=1.0)
                    )
                out[f"vol_range_{w}"] = (hl / (4.0 * np.log(2.0)) ** 0.5).fillna(0.0)
        return out

    # ---------------------------------------------------------------- momentum
    def _add_momentum(
        self, out: pd.DataFrame, x: pd.Series, log_x: pd.Series
    ) -> pd.DataFrame:
        out["roc_1"] = x.pct_change(1).fillna(0.0)
        out["roc_3"] = x.pct_change(3).fillna(0.0)
        out["roc_5"] = x.pct_change(5).fillna(0.0)
        out["roc_10"] = x.pct_change(10).fillna(0.0)
        out["roc_20"] = x.pct_change(20).fillna(0.0)
        out["log_ret_1"] = log_x.diff(1).fillna(0.0)
        out["log_ret_3"] = log_x.diff(3).fillna(0.0)
        out["log_ret_5"] = log_x.diff(5).fillna(0.0)
        out["acceleration"] = out["log_ret_1"].diff().fillna(0.0)
        out["jerk"] = out["acceleration"].diff().fillna(0.0)

        # Trend direction and strength for all EMA pairs
        ema_spans = self.ema_spans
        for i in range(len(ema_spans) - 1):
            s, l = ema_spans[i], ema_spans[i + 1]
            ema_s_col = f"ema_{s}"
            ema_l_col = f"ema_{l}"
            if ema_s_col in out and ema_l_col in out:
                denom = out[ema_l_col].clip(lower=1.0)
                out[f"trend_{s}_{l}"] = np.sign(out[ema_s_col] - out[ema_l_col])
                out[f"trend_strength_{s}_{l}"] = (
                    (out[ema_s_col] - out[ema_l_col]) / denom
                ).clip(-5, 5)
        return out

    # ---------------------------------------------------------------- streak
    def _add_streak(self, out: pd.DataFrame, x: pd.Series) -> pd.DataFrame:
        for threshold in (2.0, 5.0):
            col = f"is_above_{threshold:.0f}".replace(".", "_")
            above = (x >= threshold).astype(int)
            grp = (above != above.shift()).cumsum()
            out[f"streak_len_{col}"] = grp.groupby(grp).cumcount() + 1
            out[col] = above
            out[f"streak_avg_{col}"] = x.groupby(grp).transform("mean")
        return out

    # ---------------------------------------------------------------- frequency
    def _add_frequency(self, out: pd.DataFrame, x: pd.Series) -> pd.DataFrame:
        for w in (10, 30, 100):
            if w > len(x):
                continue
            for b, lbl in zip(_FREQ_BINS, _FREQ_LABELS):
                out[f"{lbl}_{w}"] = (
                    x.rolling(w, min_periods=1)
                    .apply(lambda v, b=b: float((v < b).mean()), raw=True)
                )
        return out

    # ---------------------------------------------------------------- entropy
    def _add_entropy(self, out: pd.DataFrame, x: pd.Series) -> pd.DataFrame:
        def _shannon(v: np.ndarray) -> float:
            if len(v) == 0:
                return 0.0
            cats = np.digitize(v, _FREQ_BINS)
            _, counts = np.unique(cats, return_counts=True)
            p = counts / counts.sum()
            p = p[p > 0]
            return float(-np.sum(p * np.log2(p)))

        for w in (10, 30, 50):
            if w > len(x):
                continue
            out[f"entropy_{w}"] = (
                x.rolling(w, min_periods=2).apply(_shannon, raw=True)
            )
        return out

    # ---------------------------------------------------------------- z-score
    def _add_zscore(self, out: pd.DataFrame, log_x: pd.Series) -> pd.DataFrame:
        for w in (20, 50, 100, 200):
            if w > len(log_x):
                continue
            m = log_x.rolling(w, min_periods=5).mean()
            s = log_x.rolling(w, min_periods=5).std().replace(0, np.nan)
            out[f"zscore_{w}"] = ((log_x - m) / s).fillna(0.0).clip(-6, 6)
        return out

    # ---------------------------------------------------------------- extrema
    def _add_extrema(self, out: pd.DataFrame, x: pd.Series) -> pd.DataFrame:
        for w in (10, 20, 50):
            if w > len(x):
                continue
            mn = x.rolling(w, min_periods=1).min()
            mx = x.rolling(w, min_periods=1).max()
            rng = (mx - mn).clip(lower=1e-9)
            out[f"dist_from_min_{w}"] = (x - mn).clip(lower=0.0)
            out[f"dist_from_max_{w}"] = (mx - x).clip(lower=0.0)
            out[f"position_in_range_{w}"] = ((x - mn) / rng).fillna(0.5)
        return out

    # ---------------------------------------------------------------- percentile
    def _add_percentile(self, out: pd.DataFrame, x: pd.Series) -> pd.DataFrame:
        for w in (50, 100, 200):
            if w > len(x):
                continue
            out[f"pct_rank_{w}"] = (
                x.rolling(w, min_periods=5).apply(
                    lambda v: float(sp_stats.percentileofscore(v, v[-1]) / 100.0),
                    raw=True,
                )
            )
        return out


__all__ = ["StatisticalFeatures"]
