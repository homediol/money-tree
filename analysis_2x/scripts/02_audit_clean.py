#!/usr/bin/env python3
"""02_audit_clean.py - Audit the raw snapshot and build the cleaned analysis set.

Cleaning principles (each decision is logged and counted):
  D1 exact-timestamp same-multiplier duplicates  -> keep one (same platform round captured twice)
  D2 catchup/batch blocks re-observing an earlier contiguous multiplier run -> drop later copy
  D3 near-timestamp (<= NEAR_DUP_S) same-multiplier double-captures -> drop later copy
  Everything else is KEPT, including legitimately consecutive identical multipliers.
  Timestamps are never invented; batch rows with fabricated timestamps that cannot be
  matched to an earlier observation are kept but flagged (their true completion time is
  unknown and they break contiguity).

Outputs: artifacts/rounds_clean.csv, artifacts/data_quality.json, artifacts/dropped_rows.csv
"""
import csv
import hashlib
import json
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BASE = os.path.join(ROOT, "analysis_2x")
ART = os.path.join(BASE, "artifacts")

RAW_CSV = os.path.join(ART, "raw_rounds.csv")
CLEAN_CSV = os.path.join(ART, "rounds_clean.csv")
DROPPED_CSV = os.path.join(ART, "dropped_rows.csv")
DQ_JSON = os.path.join(ART, "data_quality.json")

# Frozen audit parameters (justification printed in data_quality_report.md)
NEAR_DUP_S = 6.0        # two rounds cannot complete within 6 s; same multiplier + dt<=6s => same round
CONTIG_MAX_S = 90.0     # max gap treated as consecutive (frozen after distribution inspection)
MIN_DT_S = 0.0          # dt must be strictly positive
GRAM_W = 6              # length of multiplier n-gram used for overlap detection
GRAM_SEARCH = 200       # how far back to search for the earlier occurrence
SEG_MAX_S = 90.0        # segment break threshold (same as CONTIG_MAX_S)


def parse_ts(s: str):
    if not s:
        return None
    return pd.Timestamp(s)


def main() -> int:
    df = pd.read_csv(RAW_CSV, dtype={"round_id": str, "platform_round_id": str})
    df["ts"] = pd.to_datetime(df["timestamp"], utc=True, format="ISO8601")
    df["mult"] = df["multiplier"].astype(float)
    n_raw = len(df)

    dq = {"n_raw": n_raw}
    dq["source_counts"] = df["source"].value_counts().to_dict()

    # ---- uniqueness of identifiers -------------------------------------
    dq["dup_round_id"] = int(df["round_id"].duplicated().sum())
    dq["dup_round_index"] = int(df["round_index"].duplicated().sum())
    dq["min_multiplier"] = float(df["mult"].min())
    dq["max_multiplier"] = float(df["mult"].max())
    dq["mult_below_1"] = int((df["mult"] < 1.0).sum())
    dq["mult_precision_violations"] = int(
        (np.abs(df["mult"] * 100 - np.round(df["mult"] * 100)) > 1e-6).sum())
    dq["ts_null"] = int(df["ts"].isna().sum())

    # ---- ordering diagnostics ------------------------------------------
    by_idx = df.sort_values(["round_index", "id"])
    ts_prev = by_idx["ts"].shift()
    inv = by_idx[by_idx["ts"] < ts_prev]
    dq["index_order_ts_inversions"] = int(len(inv))
    dq["index_order_ts_inversion_max_s"] = (
        float((ts_prev - inv["ts"]).dt.total_seconds().max()) if len(inv) else 0.0)

    # ---- index gaps -------------------------------------------------------
    gaps = []
    ri = by_idx["round_index"].to_numpy()
    for a, b, ta, tb in zip(ri[:-1], ri[1:], by_idx["ts"][:-1], by_idx["ts"][1:]):
        if b - a > 1:
            gaps.append({"after_index": int(a), "next_index": int(b),
                         "missing_count": int(b - a - 1),
                         "ts_after": ta.isoformat(), "ts_next": tb.isoformat()})
    dq["index_gaps"] = gaps
    dq["missing_index_total"] = int(sum(g["missing_count"] for g in gaps))

    # ---- canonical order ---------------------------------------------------
    df = df.sort_values(["ts", "round_index", "id"]).reset_index(drop=True)
    df["canon_pos"] = np.arange(len(df))
    idx_inversions_ts_order = int(
        (df["round_index"].diff().dropna() < 0).sum())
    dq["round_index_inversions_in_ts_order"] = idx_inversions_ts_order

    # ---- exact-timestamp groups --------------------------------------------
    grp_sizes = df.groupby("ts")["round_id"].transform("size")
    eq_groups = df[grp_sizes > 1]
    dq["exact_ts_groups_gt1"] = int(df.groupby("ts").size().gt(1).sum())
    dq["exact_ts_rows_in_groups_gt1"] = int(len(eq_groups))
    same_mult_groups = int(eq_groups.groupby("ts")["mult"].nunique().eq(1).sum())

    # ---- cleaning pass ------------------------------------------------------
    drop = {}          # canon_pos -> (rule, detail)
    # D1: within equal-ts groups, identical multipliers -> keep one
    for ts_val, g in df.groupby("ts"):
        if len(g) < 2:
            continue
        for m, gg in g.groupby("mult"):
            if len(gg) > 1:
                keep_i = gg.sort_values(["round_index", "id"]).index[0]
                for i in gg.index:
                    if i != keep_i:
                        drop[i] = ("D1_exact_ts_same_mult",
                                   f"ts={ts_val} mult={m}")
    # D2: batch/overlap blocks - repeated contiguous multiplier run observed again
    mults = df["mult"].round(2).tolist()
    srcs = df["source"].tolist()
    tss = df["ts"].tolist()
    gram_map = defaultdict(list)   # gram -> list of end positions (canonical)
    for j in range(len(df)):
        if j + 1 < GRAM_W:
            continue
        gram = tuple(mults[j - GRAM_W + 1: j + 1])
        window = gram_map.get(gram, [])
        for i_end in reversed(window):
            if j - i_end > GRAM_SEARCH:
                break
            i_start = i_end - GRAM_W + 1
            j_start = j - GRAM_W + 1
            if i_start < 0:
                continue
            # earlier occurrence must be internally contiguous
            ok = True
            for k in range(i_start + 1, i_end + 1):
                dt = (tss[k] - tss[k - 1]).total_seconds()
                if not (0 < dt <= 120):
                    ok = False
                    break
            if not ok:
                continue
            # later copy must be catchup/batch evidence (equal-ts block or catchup source)
            later_ts = [tss[k] for k in range(j_start, j + 1)]
            batch_like = len(set(later_ts)) == 1 or any(
                srcs[k] == "collector_catchup" for k in range(j_start, j + 1))
            if not batch_like:
                continue
            # extend match forward while multipliers align and later rows stay batch-like
            end_j = j
            while end_j + 1 < len(df):
                nxt = end_j + 1
                i_next = i_start + (nxt - j_start)
                if i_next > i_end and (mults[i_next] if i_next < len(df) else None) == mults[nxt] \
                        and (tss[nxt] in later_ts or srcs[nxt] == "collector_catchup"):
                    end_j = nxt
                    later_ts.append(tss[nxt])
                else:
                    break
            for k in range(j_start, end_j + 1):
                if k not in drop:
                    drop[k] = ("D2_overlap_reobserved",
                               f"matches earlier canon {i_start}-{i_end} "
                               f"idx {df.at[i_start,'round_index']}-{df.at[i_end,'round_index']}")
            break
        gram_map.setdefault(gram, []).append(j)

    # D3: near-timestamp same-multiplier double captures (iterative, on survivors)
    alive = [i for i in range(len(df)) if i not in drop]
    changed = True
    while changed:
        changed = False
        new_alive = []
        for pos, i in enumerate(alive):
            if pos == 0:
                new_alive.append(i)
                continue
            p = new_alive[-1]
            dt = (tss[i] - tss[p]).total_seconds()
            if 0 <= dt <= NEAR_DUP_S and mults[i] == mults[p]:
                drop[i] = ("D3_near_ts_same_mult",
                           f"dt={dt:.3f}s mult={mults[i]}")
                changed = True
            else:
                new_alive.append(i)
        alive = new_alive

    df["kept"] = True
    df["drop_rule"] = ""
    df["drop_detail"] = ""
    for i, (rule, detail) in drop.items():
        df.at[i, "kept"] = False
        df.at[i, "drop_rule"] = rule
        df.at[i, "drop_detail"] = detail

    kept = df[df["kept"]].copy().reset_index(drop=True)

    # ---- contiguity / segments on kept rows ---------------------------------
    kept["dt_prev_s"] = kept["ts"].diff().dt.total_seconds()
    kept.loc[0, "dt_prev_s"] = np.nan
    kept["contig_prev"] = (kept["dt_prev_s"] > 0) & (kept["dt_prev_s"] <= CONTIG_MAX_S)
    kept.loc[0, "contig_prev"] = False
    kept["idx_jump_prev"] = kept["round_index"].diff().ne(1)
    kept.loc[0, "idx_jump_prev"] = False
    seg = [0]
    for v in kept["contig_prev"]:
        seg.append(seg[-1] if v else seg[-1] + 1)
    kept["segment_id"] = seg[1:]
    kept["batch_ts_flag"] = (
        kept["ts"].duplicated(keep=False) &
        kept.groupby("ts")["mult"].transform("nunique").gt(1)) | \
        (kept["source"] == "collector_catchup")

    # propagate per-row analysis columns back onto the full frame by canon_pos
    for col in ("dt_prev_s", "contig_prev", "idx_jump_prev", "segment_id",
                "batch_ts_flag"):
        df[col] = np.nan
        df[col] = df["canon_pos"].map(
            pd.Series(kept[col].to_numpy(), index=kept["canon_pos"]))

    # ---- dt distribution ------------------------------------------------------
    dts = kept["dt_prev_s"].dropna()
    dq["dt_stats"] = {
        "n": int(len(dts)), "min": float(dts.min()), "p1": float(dts.quantile(.01)),
        "p50": float(dts.quantile(.5)), "p90": float(dts.quantile(.9)),
        "p95": float(dts.quantile(.95)), "p99": float(dts.quantile(.99)),
        "max": float(dts.max()),
        "frac_le_6s": float((dts <= 6).mean()),
        "frac_le_90s": float((dts <= 90).mean()),
        "frac_gt_90s": float((dts > 90).mean()),
    }
    seg_sizes = kept.groupby("segment_id").size()
    dq["segments"] = {
        "count": int(seg_sizes.size),
        "rounds_in_segments_ge_10": int(seg_sizes[seg_sizes >= 10].sum()),
        "segments_ge_10": int((seg_sizes >= 10).sum()),
        "segments_ge_100": int((seg_sizes >= 100).sum()),
        "largest": int(seg_sizes.max()),
        "size_histogram_head": {str(k): int(v) for k, v in
                                sorted(Counter(np.minimum(seg_sizes, 20)).items())[:21]},
    }
    dq["cleaning"] = {
        "D1_exact_ts_same_mult_dropped": int(sum(1 for r, _ in drop.values() if r.startswith("D1"))),
        "D2_overlap_reobserved_dropped": int(sum(1 for r, _ in drop.values() if r.startswith("D2"))),
        "D3_near_ts_same_mult_dropped": int(sum(1 for r, _ in drop.values() if r.startswith("D3"))),
        "kept_rows": int(len(kept)),
        "dropped_rows": int(len(drop)),
        "kept_with_batch_ts_flag": int(kept["batch_ts_flag"].sum()),
        "exact_ts_groups_same_mult": same_mult_groups,
    }
    dq["kept_source_counts"] = kept["source"].value_counts().to_dict()
    dq["kept_index_range"] = [int(kept["round_index"].min()), int(kept["round_index"].max())]
    dq["kept_ts_range"] = [kept["ts"].min().isoformat(), kept["ts"].max().isoformat()]

    # ---- daily coverage (Africa/Kigali, UTC+2 fixed) --------------------------
    kig = kept["ts"].dt.tz_convert("Africa/Kigali")
    daily = kig.dt.date.value_counts().sort_index()
    dq["daily_counts_kigali"] = {str(k): int(v) for k, v in daily.items()}
    dq["active_days"] = int((daily >= 10).sum())

    # ---- outputs ---------------------------------------------------------------
    out_cols = ["round_id", "round_index", "multiplier", "ts", "source",
                "platform_round_id", "canon_pos", "dt_prev_s", "contig_prev",
                "idx_jump_prev", "segment_id", "batch_ts_flag", "kept",
                "drop_rule", "drop_detail"]
    df[out_cols].to_csv(CLEAN_CSV, index=False)
    df[~df["kept"]][out_cols].to_csv(DROPPED_CSV, index=False)

    with open(DQ_JSON, "w") as f:
        json.dump(dq, f, indent=2, default=str)

    print(json.dumps({k: dq[k] for k in (
        "n_raw", "cleaning", "dt_stats", "segments", "active_days",
        "index_order_ts_inversions", "missing_index_total",
        "round_index_inversions_in_ts_order")}, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
