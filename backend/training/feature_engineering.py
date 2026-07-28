"""
training/feature_engineering.py
================================
82-feature advanced feature engineering for Aviator crash prediction.

IMPROVEMENTS over v1 (72 features):
  - Removed 2 near-constant features (min_20, median_20)
  - Added EMA-5, EMA-10, EMA-20 (better than SMA for recency)
  - Added RSI-style oscillator (momentum)
  - Added Bollinger Band position (volatility regime)
  - Added crash frequency ratios per regime
  - Added inter-crash gap indicators
  - Better normalisation to prevent gradient issues

Feature groups (82 total):
  A: Raw log-scaled windows      [0–34]   35 features
  B: EMA indicators              [35–37]   3 features  (replaces SMA)
  C: Spread & shape              [38–46]   9 features
  D: Distributional moments      [47–49]   3 features
  E: Category counts (last 10)   [50–54]   5 features
  F: Category frequencies full   [55–59]   5 features
  G: Streaks & gaps              [60–65]   6 features
  H: Momentum & trend            [66–71]   6 features
  I: Regime & advanced           [72–81]  10 features  ← NEW
"""

from __future__ import annotations

import csv
import math
import statistics
from pathlib import Path
from typing import Dict, List, Optional, Tuple

_THRESHOLDS = [1.50, 2.00, 5.00, 15.0]
CATEGORIES  = ["VERY_LOW", "LOW", "MEDIUM", "HIGH", "VERY_HIGH"]


# ── Feature name list ─────────────────────────────────────────────────────

FEATURE_NAMES: List[str] = (
    [f"last_5_mult_{i+1}"  for i in range(5)]   +   #  0– 4
    [f"last_10_mult_{i+1}" for i in range(10)]  +   #  5–14
    [f"last_20_mult_{i+1}" for i in range(20)]  +   # 15–34
    [
        # Group B: EMA indicators
        "ema_5", "ema_10", "ema_20",                # 35–37
        # Group C: spread & shape
        "std_5", "std_10", "std_20",                # 38–40
        "variance_20", "max_20",                    # 41–42
        "volatility_cv", "range_ratio",             # 43–44 (cv=std/mean, range/mean)
        "kurtosis_20", "skewness_20",               # 45–46
        # Group D: moments
        "entropy_20", "entropy_10", "entropy_5",    # 47–49
        # Group E: counts in last 10
        "vl_cnt10", "l_cnt10", "m_cnt10",           # 50–52
        "h_cnt10",  "vh_cnt10",                     # 53–54
        # Group F: frequencies full window
        "freq_vl", "freq_l", "freq_m",              # 55–57
        "freq_h",  "freq_vh",                       # 58–59
        # Group G: streaks & gaps
        "streak_low", "streak_high",                # 60–61
        "streak_medium", "streak_vl",               # 62–63
        "time_since_high", "time_since_vh",         # 64–65
        # Group H: momentum & trend
        "mom_5_20", "mom_10_20",                    # 66–67
        "slope_5", "slope_10",                      # 68–69
        "rsi_7", "rsi_14",                          # 70–71  ← NEW: RSI oscillator
        # Group I: regime & advanced
        "bb_position",                              # 72  Bollinger Band position
        "regime_low", "regime_med", "regime_high",  # 73–75 one-hot regime
        "crash_rate_5",                             # 76 fraction of last 5 that crashed <2x
        "crash_rate_10",                            # 77
        "high_rate_5",                              # 78 fraction of last 5 that were HIGH+
        "high_rate_10",                             # 79
        "mean_reversion",                           # 80 (last_mult - global_mean) / global_std
        "cycle_position",                           # 81 position in approximate avg cycle length
    ]
)

FEATURE_DIM = len(FEATURE_NAMES)   # 82
assert FEATURE_DIM == 82, f"Expected 82, got {FEATURE_DIM}"


# ── Low-level helpers ─────────────────────────────────────────────────────

def _log_scale(v: float) -> float:
    return math.log(min(max(float(v), 1.0), 100.0)) / math.log(100.0)

def _category(v: float) -> int:
    for i, t in enumerate(_THRESHOLDS):
        if v < t: return i
    return len(_THRESHOLDS)

def _norm(x: float, cap: float = 100.0) -> float:
    return min(abs(x) / cap, 1.0)

def _norm_signed(x: float, cap: float = 5.0) -> float:
    return max(-1.0, min(1.0, x / cap))

def _ema(values: List[float], period: int) -> float:
    if not values: return 0.0
    k = 2.0 / (period + 1)
    e = values[0]
    for v in values[1:]:
        e = v * k + e * (1 - k)
    return e

def _ema_series(values: List[float], period: int) -> List[float]:
    """Return full EMA series."""
    if not values: return []
    k = 2.0 / (period + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out

def _rsi(values: List[float], period: int) -> float:
    """RSI oscillator normalised to [0, 1]."""
    if len(values) < period + 1:
        return 0.5
    changes = [values[i] - values[i-1] for i in range(1, len(values))]
    recent  = changes[-period:]
    gains   = [c for c in recent if c > 0]
    losses  = [-c for c in recent if c < 0]
    avg_g   = sum(gains)  / period if gains  else 0.0
    avg_l   = sum(losses) / period if losses else 0.0
    if avg_l == 0: return 1.0
    rs  = avg_g / avg_l
    rsi = 1.0 - 1.0 / (1.0 + rs)
    return round(rsi, 4)

def _linear_slope(values: List[float]) -> float:
    n = len(values)
    if n < 2: return 0.0
    xm  = (n - 1) / 2.0
    ym  = sum(values) / n
    num = sum((i - xm) * (v - ym) for i, v in enumerate(values))
    den = sum((i - xm) ** 2 for i in range(n))
    return num / den if den else 0.0

def _skewness(values, mean, std):
    n = len(values)
    if n < 3 or std == 0: return 0.0
    return sum(((v - mean) / std) ** 3 for v in values) * n / ((n-1) * (n-2))

def _kurtosis(values, mean, std):
    n = len(values)
    if n < 4 or std == 0: return 0.0
    return sum(((v - mean) / std) ** 4 for v in values) / n - 3.0

def _entropy(cats: List[int]) -> float:
    n = len(cats)
    if n == 0: return 0.0
    me = math.log(len(CATEGORIES))
    if me == 0: return 0.0
    counts = [cats.count(i) for i in range(len(CATEGORIES))]
    e = -sum((c/n) * math.log(c/n + 1e-12) for c in counts if c > 0)
    return e / me

def _streak_of(values, pred) -> int:
    count = 0
    for v in reversed(values):
        if pred(v): count += 1
        else: break
    return count

def _time_since(values, pred) -> int:
    for i, v in enumerate(reversed(values)):
        if pred(v): return i
    return len(values)

def _vol_regime(window: List[float]) -> str:
    if len(window) < 5: return "medium"
    std = statistics.pstdev(window[-min(20, len(window)):])
    mn  = statistics.mean(window[-min(20, len(window)):])
    cv  = std / mn if mn > 0 else 0.0
    if cv < 0.45: return "low"
    if cv > 0.85: return "high"
    return "medium"


# ── Main feature extractor ────────────────────────────────────────────────

def compute_features(window: List[float]) -> List[float]:
    """
    Compute the full 82-feature vector.
    All values normalised to [0,1] or [-1,1].

    OUTLIER PROTECTION: multipliers > 100× are capped at 100× before
    feature computation to prevent extreme values (e.g. 23882×) from
    distorting rolling averages, EMAs, and standard deviations.
    """
    # Cap outliers FIRST — prevents distortion of all rolling statistics
    n    = len(window)
    vals = [min(float(v), 100.0) for v in window]   # cap at 100×
    if n == 0: raise ValueError("window must not be empty")

    w5  = vals[-5:]  if n >= 5  else vals
    w10 = vals[-10:] if n >= 10 else vals
    w20 = vals[-20:] if n >= 20 else vals

    pad = _log_scale(vals[0])

    # ── Group A: log-scaled windows (35) ─────────────────────────────
    s5  = [pad] * (5  - len(w5))  + [_log_scale(v) for v in w5]
    s10 = [pad] * (10 - len(w10)) + [_log_scale(v) for v in w10]
    s20 = [pad] * (20 - len(w20)) + [_log_scale(v) for v in w20]

    # ── Group B: EMA indicators (3) ──────────────────────────────────
    ema5  = _ema(w5,  5)
    ema10 = _ema(w10, 10)
    ema20 = _ema(w20, 20)

    # ── Group C: spread & shape (9) ──────────────────────────────────
    std5   = statistics.pstdev(w5)  if len(w5)  > 1 else 0.0
    std10  = statistics.pstdev(w10) if len(w10) > 1 else 0.0
    std20  = statistics.pstdev(w20) if len(w20) > 1 else 0.0
    var20  = std20 ** 2
    mx20   = max(w20)
    mn20   = min(w20)
    mean20 = statistics.mean(w20)
    cv20   = std20 / mean20 if mean20 > 0 else 0.0
    rng_ratio = (mx20 - mn20) / mean20 if mean20 > 0 else 0.0
    kurt20  = _kurtosis(w20, mean20, std20)
    skew20  = _skewness(w20, mean20, std20)

    # ── Group D: entropy (3) ─────────────────────────────────────────
    cats20  = [_category(v) for v in w20]
    cats10  = [_category(v) for v in w10]
    cats5   = [_category(v) for v in w5]
    ent20   = _entropy(cats20)
    ent10   = _entropy(cats10)
    ent5    = _entropy(cats5)

    # ── Group E: counts last 10 (5) ──────────────────────────────────
    n10     = len(cats10)
    vl10    = cats10.count(0) / n10
    l10     = cats10.count(1) / n10
    m10     = cats10.count(2) / n10
    h10     = cats10.count(3) / n10
    vh10    = cats10.count(4) / n10

    # ── Group F: frequencies full (5) ────────────────────────────────
    cats_all = [_category(v) for v in vals]
    n_all    = len(cats_all)
    fvl = cats_all.count(0) / n_all
    fl  = cats_all.count(1) / n_all
    fm  = cats_all.count(2) / n_all
    fh  = cats_all.count(3) / n_all
    fvh = cats_all.count(4) / n_all

    # ── Group G: streaks & gaps (6) ──────────────────────────────────
    streak_low    = _streak_of(vals, lambda v: v < 2.0)
    streak_high   = _streak_of(vals, lambda v: v >= 5.0)
    streak_med    = _streak_of(vals, lambda v: 2.0 <= v < 5.0)
    streak_vl     = _streak_of(vals, lambda v: v < 1.5)
    t_since_h     = _time_since(vals, lambda v: v >= 5.0)
    t_since_vh    = _time_since(vals, lambda v: v >= 15.0)

    # ── Group H: momentum & trend (6) ────────────────────────────────
    mean5  = statistics.mean(w5)
    mean10 = statistics.mean(w10)
    mom5   = (mean5  / mean20 - 1.0) if mean20 > 0 else 0.0
    mom10  = (mean10 / mean20 - 1.0) if mean20 > 0 else 0.0
    slope5 = _linear_slope(w5)
    slp10  = _linear_slope(w10)
    rsi7   = _rsi(vals, 7)
    rsi14  = _rsi(vals, 14)

    # ── Group I: regime & advanced (10) ──────────────────────────────
    # Bollinger Band position: where is last value relative to 20-round band?
    bb_upper = mean20 + 2 * std20
    bb_lower = max(1.0, mean20 - 2 * std20)
    bb_range = bb_upper - bb_lower
    bb_pos   = (vals[-1] - bb_lower) / bb_range if bb_range > 0 else 0.5
    bb_pos   = max(0.0, min(1.0, bb_pos))

    # Regime one-hot
    regime = _vol_regime(vals)
    r_low  = 1.0 if regime == "low"    else 0.0
    r_med  = 1.0 if regime == "medium" else 0.0
    r_high = 1.0 if regime == "high"   else 0.0

    # Crash rate (fraction below 2×)
    cr5  = sum(1 for v in w5  if v < 2.0) / len(w5)
    cr10 = sum(1 for v in w10 if v < 2.0) / len(w10)

    # High crash rate (fraction >= 5×)
    hr5  = sum(1 for v in w5  if v >= 5.0) / len(w5)
    hr10 = sum(1 for v in w10 if v >= 5.0) / len(w10)

    # Mean reversion signal
    global_mean = 3.5    # approximate provably-fair mean (1/0.99 ≈ 1/(1-h) integral)
    global_std  = 8.0    # empirical std
    mr = (vals[-1] - global_mean) / global_std
    mr = max(-3.0, min(3.0, mr)) / 3.0   # clip to [-1,1]

    # Cycle position: how many rounds since last "reset" (very low crash)
    last_vl = _time_since(vals, lambda v: v < 1.2)
    cycle_pos = min(last_vl / 10.0, 1.0)

    MAX_MULT   = 100.0
    MAX_STD    = 20.0
    MAX_VAR    = 400.0
    MAX_STREAK = 20.0
    MAX_TIME   = float(n)

    features = (
        s5 + s10 + s20                                      # 35
        + [_norm(ema5, MAX_MULT), _norm(ema10, MAX_MULT), _norm(ema20, MAX_MULT)]  # 3
        + [
            _norm(std5,   MAX_STD), _norm(std10, MAX_STD), _norm(std20, MAX_STD),
            _norm(var20,  MAX_VAR), _norm(mx20,  MAX_MULT),
            _norm(cv20,   5.0),     _norm(rng_ratio, 20.0),
            _norm_signed(kurt20, 10.0), _norm_signed(skew20, 3.0),
          ]                                                  # 9
        + [ent20, ent10, ent5]                               # 3
        + [vl10, l10, m10, h10, vh10]                       # 5
        + [fvl, fl, fm, fh, fvh]                            # 5
        + [
            _norm(streak_low,  MAX_STREAK), _norm(streak_high, MAX_STREAK),
            _norm(streak_med,  MAX_STREAK), _norm(streak_vl,   MAX_STREAK),
            _norm(t_since_h,   MAX_TIME),   _norm(t_since_vh,  MAX_TIME),
          ]                                                  # 6
        + [
            _norm_signed(mom5,  1.0), _norm_signed(mom10, 1.0),
            _norm_signed(slope5, 5.0), _norm_signed(slp10, 5.0),
            rsi7, rsi14,
          ]                                                  # 6
        + [
            bb_pos, r_low, r_med, r_high,
            cr5, cr10, hr5, hr10,
            _norm_signed(mr, 1.0), cycle_pos,
          ]                                                  # 10
    )

    assert len(features) == FEATURE_DIM, f"Expected {FEATURE_DIM}, got {len(features)}"
    return features


def compute_features_named(window: List[float]) -> Dict[str, float]:
    return dict(zip(FEATURE_NAMES, compute_features(window)))


def build_feature_matrix(
    multipliers: List[float],
    window_size: int = 20,
) -> Tuple[List[List[float]], List[int]]:
    X, y = [], []
    for i in range(window_size, len(multipliers)):
        X.append(compute_features(multipliers[i - window_size: i]))
        y.append(_category(multipliers[i]))
    return X, y


def export_features_csv(
    multipliers: List[float],
    output_path: Optional[Path] = None,
    window_size: int = 20,
) -> Path:
    if output_path is None:
        output_path = Path(__file__).resolve().parent.parent.parent / "data" / "features.csv"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    X, y = build_feature_matrix(multipliers, window_size=window_size)
    with output_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["sample_index"] + FEATURE_NAMES + ["label", "category"])
        for i, (feats, label) in enumerate(zip(X, y)):
            writer.writerow([i] + [round(v, 6) for v in feats] + [label, CATEGORIES[label]])
    return output_path


def make_tf_dataset(multipliers, window_size=20, batch_size=256, shuffle=True):
    try:
        import tensorflow as tf
        import numpy as np
    except ImportError as exc:
        raise RuntimeError("TensorFlow required") from exc
    X, y = build_feature_matrix(multipliers, window_size=window_size)
    ds = tf.data.Dataset.from_tensor_slices(
        (np.array(X, "float32"), np.array(y, "int64"))
    )
    if shuffle:
        ds = ds.shuffle(min(10000, len(X)), reshuffle_each_iteration=True)
    return ds.batch(batch_size).prefetch(tf.data.AUTOTUNE)


if __name__ == "__main__":
    import sys, logging
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from utils import load_round_history
    rounds = load_round_history()
    mults  = [r["multiplier"] for r in rounds]
    print(f"Loaded {len(mults)} rounds")
    print(f"FEATURE_DIM = {FEATURE_DIM}")
    sample = compute_features_named(mults[-20:])
    print("\nSample features (last 20 rounds):")
    for name, val in list(sample.items())[-20:]:
        print(f"  {name:<25} = {val:.4f}")
    out = export_features_csv(mults)
    print(f"\nCSV: {out}")
