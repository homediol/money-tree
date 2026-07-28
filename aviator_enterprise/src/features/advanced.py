"""
Advanced Feature Generator (Enterprise v3)
==========================================

Produces higher-order and interaction features that complement the
statistical and time-based modules:

  * Autocorrelation at lags 1–5  (serial structure detection)
  * Hurst exponent (long-range dependence / mean-reversion signal)
  * Approximate entropy (complexity / regularity)
  * Spectral features via DFT (dominant frequency, spectral entropy)
  * Cross-feature interactions (category × EMA, volatility × momentum)
  * Non-linearity indicators (sign changes, reversal rate, zero-cross rate)
  * Quantile distances at multiple windows
  * Run-length features (consecutive above/below threshold)
"""
from __future__ import annotations

import warnings
from typing import Optional

import numpy as np
import pandas as pd
from scipy.signal import periodogram
from scipy.stats import kurtosis as sp_kurtosis, skew as sp_skew

from src.core.logger import get_logger

logger = get_logger(__name__)
warnings.filterwarnings("ignore", category=RuntimeWarning)


class AdvancedFeatures:
    """
    Computes advanced features over the multiplier series.

    All methods operate on a rolling window to respect temporal order
    and avoid look-ahead bias.  The minimum window is enforced via
    `min_periods` throughout.
    """

    WINDOWS = (20, 50, 100)

    def build(self, df: pd.DataFrame, target_col: str = "multiplier") -> pd.DataFrame:
        if target_col not in df.columns:
            return df
        out = df.copy()
        x = out[target_col].astype(float)
        log_x = np.log1p(x.clip(lower=1.0))

        out = self._autocorrelation(out, x)
        out = self._hurst(out, log_x)
        out = self._approx_entropy(out, x)
        out = self._spectral(out, x)
        out = self._nonlinearity(out, x, log_x)
        out = self._run_length(out, x)
        out = self._quantile_distances(out, x)
        out = self._interaction_features(out, x)
        return out

    # --------------------------------------------------------------- autocorr
    def _autocorrelation(self, out: pd.DataFrame, x: pd.Series) -> pd.DataFrame:
        for lag in (1, 2, 3, 5):
            out[f"autocorr_{lag}"] = (
                x.rolling(50, min_periods=10).apply(
                    lambda v: float(pd.Series(v).autocorr(lag=lag))
                    if len(v) > lag + 5
                    else 0.0,
                    raw=False,
                )
            )
        return out

    # --------------------------------------------------------------- hurst
    def _hurst(self, out: pd.DataFrame, log_x: pd.Series) -> pd.DataFrame:
        """
        Simplified R/S Hurst exponent over a rolling window.
        H > 0.5  → trending  (persistence)
        H < 0.5  → mean-reverting (anti-persistence)
        H ≈ 0.5  → random walk
        """
        for w in (50, 100):
            if w > len(log_x):
                continue
            out[f"hurst_{w}"] = log_x.rolling(w, min_periods=20).apply(
                self._hurst_rs, raw=True
            )
        return out

    @staticmethod
    def _hurst_rs(arr: np.ndarray) -> float:
        n = len(arr)
        if n < 8:
            return 0.5
        try:
            lags = range(2, n // 2)
            rs_vals = []
            for lag in lags:
                chunks = [arr[i: i + lag] for i in range(0, n - lag, lag)]
                rs_chunk = []
                for c in chunks:
                    mean_c = c.mean()
                    cumdev = np.cumsum(c - mean_c)
                    r = cumdev.max() - cumdev.min()
                    s = c.std()
                    if s > 1e-9:
                        rs_chunk.append(r / s)
                if rs_chunk:
                    rs_vals.append(np.mean(rs_chunk))
            if len(rs_vals) < 3:
                return 0.5
            log_rs = np.log(rs_vals)
            log_lags = np.log(list(range(2, n // 2))[: len(rs_vals)])
            H = float(np.polyfit(log_lags, log_rs, 1)[0])
            return float(np.clip(H, 0.0, 1.0))
        except Exception:
            return 0.5

    # --------------------------------------------------------------- approx entropy
    def _approx_entropy(self, out: pd.DataFrame, x: pd.Series) -> pd.DataFrame:
        for w in (20, 50):
            if w > len(x):
                continue
            out[f"approx_entropy_{w}"] = x.rolling(w, min_periods=5).apply(
                self._apen, raw=True
            )
        return out

    @staticmethod
    def _apen(arr: np.ndarray, m: int = 2, r_coeff: float = 0.2) -> float:
        """Approximate entropy — lower = more regular."""
        n = len(arr)
        if n < m + 1:
            return 0.0
        try:
            r = r_coeff * arr.std()
            if r < 1e-9:
                return 0.0

            def phi(length: int) -> float:
                templates = np.array([arr[i: i + length] for i in range(n - length + 1)])
                counts = np.sum(
                    np.max(np.abs(templates[:, None] - templates[None, :]), axis=-1) <= r,
                    axis=0,
                )
                return float(np.mean(np.log(counts / (n - length + 1))))

            return abs(phi(m) - phi(m + 1))
        except Exception:
            return 0.0

    # --------------------------------------------------------------- spectral
    def _spectral(self, out: pd.DataFrame, x: pd.Series) -> pd.DataFrame:
        for w in (32, 64):
            if w > len(x):
                continue
            col_dom = f"spectral_dom_freq_{w}"
            col_ent = f"spectral_entropy_{w}"
            out[col_dom] = np.nan
            out[col_ent] = np.nan
            for i in range(w, len(x) + 1, max(w // 4, 1)):
                chunk = x.iloc[max(0, i - w): i].values
                if len(chunk) < 4:
                    continue
                try:
                    freqs, power = periodogram(chunk)
                    if power.sum() < 1e-9:
                        continue
                    dom = float(freqs[np.argmax(power)])
                    p_norm = power / power.sum()
                    ent = float(-np.sum(p_norm * np.log2(p_norm + 1e-12)))
                    idx = i - 1
                    out.at[idx, col_dom] = dom
                    out.at[idx, col_ent] = ent
                except Exception:
                    pass
            out[col_dom] = out[col_dom].ffill().fillna(0.0)
            out[col_ent] = out[col_ent].ffill().fillna(0.0)
        return out

    # --------------------------------------------------------------- nonlinearity
    def _nonlinearity(self, out: pd.DataFrame, x: pd.Series, log_x: pd.Series) -> pd.DataFrame:
        # Sign changes per window — how often does direction reverse?
        for w in (10, 20, 50):
            if w > len(x):
                continue
            sign_changes = np.sign(x.diff()).diff().abs() / 2.0
            out[f"sign_change_rate_{w}"] = sign_changes.rolling(w, min_periods=2).mean()

        # Zero-cross rate of log-returns
        log_ret = log_x.diff()
        for w in (10, 20):
            out[f"zero_cross_rate_{w}"] = log_ret.rolling(w, min_periods=2).apply(
                lambda v: float((np.diff(np.sign(v)) != 0).mean()), raw=True
            )

        # Mean absolute deviation (faster proxy for volatility)
        for w in (10, 20, 50):
            if w > len(x):
                continue
            out[f"mad_{w}"] = x.rolling(w, min_periods=2).apply(
                lambda v: float(np.mean(np.abs(v - v.mean()))), raw=True
            )

        # Skewness and kurtosis of log-returns
        for w in (30, 50):
            if w > len(x):
                continue
            out[f"ret_skew_{w}"] = log_ret.rolling(w, min_periods=10).skew()
            out[f"ret_kurt_{w}"] = log_ret.rolling(w, min_periods=10).kurt()

        return out

    # --------------------------------------------------------------- run-length
    def _run_length(self, out: pd.DataFrame, x: pd.Series) -> pd.DataFrame:
        """Encode consecutive runs of above/below median."""
        for threshold in (1.5, 2.0, 5.0):
            above = (x >= threshold).astype(int)
            grp = (above != above.shift()).cumsum()
            run_len = grp.groupby(grp).cumcount() + 1
            col = f"run_len_above_{threshold:.0f}".replace(".", "_")
            out[col] = run_len * above
            # Max run length in rolling window
            for w in (20, 50):
                out[f"max_run_above_{threshold:.0f}_{w}".replace(".", "_")] = (
                    out[col].rolling(w, min_periods=1).max()
                )
        return out

    # --------------------------------------------------------------- quantile
    def _quantile_distances(self, out: pd.DataFrame, x: pd.Series) -> pd.DataFrame:
        for w in (50, 100):
            if w > len(x):
                continue
            for q in (0.1, 0.25, 0.75, 0.9):
                q_val = x.rolling(w, min_periods=10).quantile(q)
                col = f"dist_q{int(q*100)}_{w}"
                out[col] = x - q_val
        return out

    # --------------------------------------------------------------- interactions
    def _interaction_features(self, out: pd.DataFrame, x: pd.Series) -> pd.DataFrame:
        # Interaction of volatility × momentum if both available
        if "vol_20" in out.columns and "roc_5" in out.columns:
            out["vol_x_mom"] = out["vol_20"] * out["roc_5"].abs()
        # EMA ratio (short over long)
        if "ema_5" in out.columns and "ema_50" in out.columns:
            denom = out["ema_50"].replace(0, np.nan)
            out["ema_ratio_5_50"] = out["ema_5"] / denom
            out["ema_ratio_5_50"] = out["ema_ratio_5_50"].fillna(1.0)
        # Streak × confidence proxy
        if "streak_length" in out.columns and "roll_std_20" in out.columns:
            out["streak_volatility"] = out["streak_length"] * out["roll_std_20"]
        return out


__all__ = ["AdvancedFeatures"]
