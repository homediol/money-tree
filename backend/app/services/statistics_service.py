from __future__ import annotations

import pandas as pd


def statistics(rounds: pd.DataFrame, target: float = 2.0) -> dict:
    if rounds.empty:
        return {"count": 0}
    mult = rounds["multiplier"].astype(float)
    target_series = mult >= target
    return {
        "count": int(len(mult)),
        "target_multiplier": target,
        "base_rate": float(target_series.mean()),
        "below_target_rate": float((~target_series).mean()),
        "mean": float(mult.mean()),
        "median": float(mult.median()),
        "min": float(mult.min()),
        "max": float(mult.max()),
        "std": float(mult.std(ddof=0)),
        "variance": float(mult.var(ddof=0)),
        "pct_below_1_2": float((mult < 1.2).mean()),
        "pct_below_1_5": float((mult < 1.5).mean()),
        "pct_between_2_5": float(((mult >= 2.0) & (mult < 5.0)).mean()),
        "pct_above_5": float((mult >= 5.0).mean()),
        "latest_round_index": int(rounds["round_index"].iloc[-1]),
        "latest_multiplier": float(mult.iloc[-1]),
    }

