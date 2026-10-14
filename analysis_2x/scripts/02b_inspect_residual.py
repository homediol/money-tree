#!/usr/bin/env python3
"""02b_inspect_residual.py (v2) - Inspect kept rows sharing an exact timestamp with
rows carrying DIFFERENT multipliers (residual batch rows after D1/D2/D3 v1).

Per residual equal-ts group this reports:
  - all earlier occurrences of the group's mult sequence (string search over kept
    sequence), nearest-earlier distance, occurrence count
  - longest matching PREFIX (+forward extension) and SUFFIX (+backward extension)
    against earlier kept runs  -> detects mixed [dup-prefix][new-suffix] blocks
  - index-neighborhood facts (never used to invent timestamps)

Classes:
  FULL_DUP_EARLIER / PARTIAL_PREFIX / PARTIAL_SUFFIX / ONLY_LATER / NO_MATCH

Outputs: stdout summary + examples with canonical context; artifacts/residual_groups.csv
"""
import os
import sys
from bisect import bisect_right
from collections import Counter

import numpy as np
import pandas as pd

ROOT = "/home/diolo/winner predict"
ART = os.path.join(ROOT, "analysis_2x", "artifacts")
OUT_CSV = os.path.join(ART, "residual_groups.csv")
COLLECTOR_FIRST = pd.Timestamp("2026-09-27 11:20:16.915+00:00")


def fmt_m(x) -> str:
    return f"{round(float(x), 2):.2f}"


def main() -> int:
    clean = pd.read_csv(os.path.join(ART, "rounds_clean.csv"),
                        dtype={"round_id": str, "platform_round_id": str})
    clean["ts"] = pd.to_datetime(clean["ts"], utc=True, format="ISO8601")
    clean["mult"] = clean["multiplier"].astype(float)
    clean = clean.sort_values("canon_pos").reset_index(drop=True)
    kept = clean[clean["kept"]].reset_index(drop=True).copy()
    kept_tss = kept["ts"].tolist()
    kept_idxs = kept["round_index"].astype(int).tolist()

    gsize = kept.groupby("ts")["round_id"].transform("size")
    gnmult = kept.groupby("ts")["mult"].transform("nunique")
    res_mask = (gsize > 1) & (gnmult > 1)
    res = kept[res_mask]
    groups_ts = sorted(res["ts"].unique())
    print(f"kept_rows={len(kept)} residual_rows={len(res)} "
          f"residual_groups={len(groups_ts)}")
    sizes = res.groupby("ts").size()
    print("group_size_dist:", dict(sorted(Counter(sizes.tolist()).items())))
    print("residual_source_mix:", res["source"].value_counts().to_dict())

    kept_mults = [fmt_m(m) for m in kept["mult"]]
    full_str = ",".join(kept_mults) + ","
    offsets = [0]
    for s in kept_mults:
        offsets.append(offsets[-1] + len(s) + 1)

    def strpos_to_kept_pos(o: int) -> int:
        return bisect_right(offsets, o) - 1

    def find_all(seq_str: str):
        pos, out = 0, []
        while True:
            i = full_str.find(seq_str, pos)
            if i < 0:
                return out
            out.append(strpos_to_kept_pos(i))
            pos = i + 1

    def best_prefix_cover(seq, s_pos):
        """longest coverage of seq by an earlier occurrence + forward extension"""
        best = (0, -1, -1)  # total, occ_start, ext_end_in_seq
        for p in range(len(seq), 1, -1):
            sub = ",".join(seq[:p]) + ","
            for i_start in find_all(sub):
                if i_start + p - 1 >= s_pos:
                    continue
                k = p
                while (k < len(seq) and i_start + k < s_pos
                       and kept_mults[i_start + k] == seq[k]):
                    k += 1
                if k > best[0]:
                    best = (k, i_start, k)
        return best

    def best_suffix_cover(seq, s_pos):
        """longest coverage of seq by an earlier occurrence + backward extension"""
        L = len(seq)
        best = (0, -1, -1)  # total, occ_start, covered_from_index_in_seq
        for q in range(L, 1, -1):
            sub = ",".join(seq[L - q:]) + ","
            for i_start in find_all(sub):
                if i_start + q - 1 >= s_pos:
                    continue
                # backward extension: seq[L-q-1] vs kept[i_start-1], etc.
                k = q
                while (k < L and i_start - (k - q) - 1 >= 0
                       and kept_mults[i_start - (k - q) - 1] == seq[L - k - 1]):
                    k += 1
                if k > best[0]:
                    best = (k, i_start - (k - q), L - k)
        return best

    rows, examples = [], {}
    for ts_val in groups_ts:
        g = kept[kept["ts"] == ts_val].sort_values("round_index")
        seq = [fmt_m(m) for m in g["mult"]]
        seq_str = ",".join(seq) + ","
        s_pos = int(g.index.min())   # kept-frame positional index (0..n_kept-1)
        e_pos = int(g.index.max())
        L = len(seq)
        occ = find_all(seq_str)
        earlier = [p for p in occ if p + L - 1 < s_pos]
        later = [p for p in occ if p > e_pos]
        p_tot, p_start, _ = best_prefix_cover(seq, s_pos)
        s_tot, s_start, _ = best_suffix_cover(seq, s_pos)

        if earlier:
            nearest = max(earlier, key=lambda p: p + L)
            nearest_dist = s_pos - (nearest + L)
            cls = "FULL_DUP_EARLIER"
        elif p_tot >= 3:
            cls, nearest, nearest_dist = "PARTIAL_PREFIX", p_start, s_pos - (p_start + p_tot)
        elif s_tot >= 3:
            cls, nearest, nearest_dist = "PARTIAL_SUFFIX", s_start, s_pos - (s_start + s_tot)
        elif later:
            cls, nearest, nearest_dist = "ONLY_LATER", min(later), later[0] - e_pos
        else:
            cls, nearest, nearest_dist = "NO_MATCH", -1, -1

        others_idx = kept.drop(g.index)["round_index"].astype(int)
        idx_g = g["round_index"].astype(int)
        idx_set = set(others_idx)
        prev_idx = kept_idxs[s_pos - 1] if s_pos > 0 else -1
        next_idx = kept_idxs[e_pos + 1] if e_pos + 1 < len(kept) else -1
        rows.append({
            "ts": ts_val.isoformat(), "era": "legacy" if ts_val < COLLECTOR_FIRST else "collector",
            "n": L, "mults": ",".join(seq),
            "sources": ",".join(sorted(set(g["source"]))),
            "idx_min": int(idx_g.min()), "idx_max": int(idx_g.max()),
            "prev_canon_idx": prev_idx, "next_canon_idx": next_idx,
            "n_occ_earlier": len(earlier), "n_occ_later": len(later),
            "nearest_dist": nearest_dist,
            "best_prefix_cover": p_tot, "best_suffix_cover": s_tot,
            "class": cls, "kept_pos_start": s_pos, "kept_pos_end": e_pos,
            "canon_pos_start": int(g["canon_pos"].min()),
        })
        key = cls if cls in ("FULL_DUP_EARLIER", "ONLY_LATER", "NO_MATCH") else cls
        examples.setdefault(key, []).append((ts_val, g, s_pos, e_pos))

    rdf = pd.DataFrame(rows)
    rdf.to_csv(OUT_CSV, index=False)

    print("\n== classification (groups) ==")
    print(rdf["class"].value_counts().to_dict())
    print("rows_by_class:", {c: int(rdf.loc[rdf['class'] == c, 'n'].sum())
                             for c in rdf["class"].unique()})
    print("era x class:")
    print(rdf.groupby(["era", "class"]).size().to_dict())

    fd = rdf[rdf["class"] == "FULL_DUP_EARLIER"]
    print("\nFULL_DUP nearest_dist quantiles by size:")
    for sz_min, sz_max in ((2, 2), (3, 3), (4, 5), (6, 1000)):
        sub = fd[(fd["n"] >= sz_min) & (fd["n"] <= sz_max)]
        if len(sub):
            q = sub["nearest_dist"].quantile([.1, .5, .9]).round(1).to_dict()
            print(f"  size {sz_min}-{sz_max}: n={len(sub)} "
                  f"adjacent(<=3): {int((sub['nearest_dist'] <= 3).sum())} "
                  f"dist q10/50/90={q}")
    print("n_occ_earlier dist:", Counter(fd["n_occ_earlier"].clip(upper=5)).most_common())
    print("PARTIAL_PREFIX cover dist:",
          Counter(rdf.loc[rdf['class'] == 'PARTIAL_PREFIX', 'best_prefix_cover']).most_common())
    print("PARTIAL_SUFFIX cover dist:",
          Counter(rdf.loc[rdf['class'] == 'PARTIAL_SUFFIX', 'best_suffix_cover']).most_common())

    # ---- examples with canonical context -------------------------------------
    def show_group(ts_val, g, s_pos, e_pos, note=""):
        lo, hi = max(0, s_pos - 3), min(len(kept) - 1, e_pos + 3)
        print(f"-- ts={ts_val} pos={s_pos}-{e_pos} {note}")
        for i in range(lo, hi + 1):
            r = kept.iloc[i]
            mark = "*" if s_pos <= i <= e_pos else " "
            dprev = r["dt_prev_s"] if "dt_prev_s" in kept.columns else ""
            print(f"{mark} pos={i:5d} idx={kept_idxs[i]:6d} "
                  f"mult={kept_mults[i]:>6s} src={r['source']:<18} "
                  f"rid={r['round_id']:<8} ts={r['ts'].isoformat()} "
                  f"dt_prev={dprev}")

    for cls in ("FULL_DUP_EARLIER", "PARTIAL_PREFIX", "PARTIAL_SUFFIX",
                "ONLY_LATER", "NO_MATCH"):
        exs = examples.get(cls, [])
        if not exs:
            continue
        print(f"\n== examples: {cls} ({len(exs)} available, showing <=3) ==")
        # pick smallest, a mid, largest for variety
        exs_sorted = sorted(exs, key=lambda t: t[1].shape[0])
        picks = {exs_sorted[0][0], exs_sorted[-1][0]}
        if len(exs_sorted) > 2:
            picks.add(exs_sorted[len(exs_sorted) // 2][0])
        for ts_val, g, s_pos, e_pos in exs:
            if ts_val in picks:
                show_group(ts_val, g, s_pos, e_pos)
    return 0


if __name__ == "__main__":
    sys.exit(main())
