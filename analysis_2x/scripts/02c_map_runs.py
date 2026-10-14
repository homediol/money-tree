#!/usr/bin/env python3
"""02c_map_runs.py - Map exact multiplier-run overlaps between every raw
equal-timestamp batch block and the rest of the raw snapshot.

For each block (all raw rows sharing one exact ts, >=2 rows; block sequence in
round_index order) enumerate MAXIMAL runs (len >= 2) where the block's mult
sequence equals a contiguous run of raw rows outside the block. Block rows
never serve as match targets, so a block never matches itself.

Per run we record: length, signed gap (block_min_canon - partner_end; >=0 means
the partner lies entirely before the block), how many partner rows v1 kept,
partner sources, and whether (block_index - partner_index) is constant over the
run (relabeling-offset evidence).

Per block we record row-coverage by runs of len>=2 and len>=3, plus a compact
structure string. A null calibration repeats the same run-finder on fake blocks
whose mults are i.i.d.-sampled from the kept-univariate distribution (same size
mix), giving the accidental-match background for threshold choice.

Outputs: artifacts/block_runs.csv, artifacts/block_coverage.csv, stdout summary.
"""
import os
import sys
from collections import Counter, defaultdict

import numpy as np
import pandas as pd

ROOT = "/home/diolo/winner predict"
ART = os.path.join(ROOT, "analysis_2x", "artifacts")


def fmt_m(x) -> str:
    return f"{round(float(x), 2):.2f}"


def find_runs(seq, M, blocked, pos_by_mult, min_len=2):
    """All maximal runs (j, p, t): seq[j..j+t-1] == M[p..p+t-1], t>=min_len,
    p..p+t-1 free of blocked positions, left-maximal (not extendable left)."""
    L, N = len(seq), len(M)
    runs = []
    for j in range(L):
        for p in pos_by_mult.get(seq[j], ()):
            if blocked[p]:
                continue
            if j > 0 and p > 0 and not blocked[p - 1] and M[p - 1] == seq[j - 1]:
                continue  # continuation; found from an earlier start
            t = 1
            while (j + t < L and p + t < N and not blocked[p + t]
                   and M[p + t] == seq[j + t]):
                t += 1
            if t >= min_len:
                runs.append((j, p, t))
    return runs


def main() -> int:
    raw = pd.read_csv(os.path.join(ART, "raw_rounds.csv"),
                      dtype={"round_id": str, "platform_round_id": str})
    raw["ts"] = pd.to_datetime(raw["timestamp"], utc=True, format="ISO8601")
    raw["mult"] = raw["multiplier"].astype(float)
    raw["canon_pos"] = np.arange(len(raw))
    clean = pd.read_csv(os.path.join(ART, "rounds_clean.csv"),
                        dtype={"round_id": str, "platform_round_id": str})
    kept_by_pos = pd.Series(clean["kept"].to_numpy(),
                            index=clean["canon_pos"].to_numpy())
    raw["kept_v1"] = raw["canon_pos"].map(kept_by_pos).astype(bool)

    M = [fmt_m(m) for m in raw["mult"]]
    N = len(M)
    idxs = raw["round_index"].astype(int).tolist()
    srcs = raw["source"].tolist()
    tss = raw["ts"].tolist()
    pos_by_mult = defaultdict(list)
    for i, m in enumerate(M):
        pos_by_mult[m].append(i)
    print(f"raw_rows={N} dup_round_index_raw="
          f"{int(raw['round_index'].duplicated().sum())}")

    gsize = raw.groupby("ts")["round_id"].transform("size")
    blocks_ts = sorted(raw.loc[gsize > 1, "ts"].unique())
    kept_mask = raw["kept_v1"].to_numpy()
    kept_mult_counts = Counter(m for i, m in enumerate(M) if kept_mask[i])

    block_of_pos = {}
    for ts_val0 in blocks_ts:
        for cp in raw.loc[raw["ts"] == ts_val0, "canon_pos"].astype(int):
            block_of_pos[cp] = ts_val0

    run_rows, block_rows = [], []
    for ts_val in blocks_ts:
        g = raw[raw["ts"] == ts_val].sort_values(
            ["round_index", "canon_pos"], kind="stable")
        bp = g["canon_pos"].astype(int).tolist()
        seq = [fmt_m(m) for m in g["mult"]]
        bidx = g["round_index"].astype(int).tolist()
        blocked = np.zeros(N, dtype=bool)
        blocked[bp] = True
        bset = set(bp)
        runs = find_runs(seq, M, blocked, pos_by_mult, min_len=2)
        cov2 = np.zeros(len(seq), dtype=bool)
        cov3 = np.zeros(len(seq), dtype=bool)
        era = "legacy" if ts_val < pd.Timestamp("2026-09-27 11:20:16.915+00:00") \
            else "collector"
        for (j, p, t) in runs:
            gap = bp[0] - (p + t)          # >=0: partner entirely before block
            pk = int(raw["kept_v1"].to_numpy()[p:p + t].sum())
            offs = [bidx[j + s] - idxs[p + s] for s in range(t)]
            off_const = len(set(offs)) == 1
            run_rows.append({
                "ts": ts_val.isoformat(), "era": era, "block_n": len(seq),
                "j_start": j, "j_end": j + t - 1, "p_start": p, "len": t,
                "gap": gap, "partner_kept": pk, "partner_dropped": t - pk,
                "off_const": off_const,
                "offs": ",".join(str(o) for o in offs) if t <= 8 else "",
                "p_srcs": ",".join(sorted(set(srcs[p:p + t]))),
                "p_batch_frac": round(np.mean([
                    1 if pp in block_of_pos else 0
                    for pp in range(p, p + t)]), 3),
            })
            cov2[j:j + t] = True
            if t >= 3:
                cov3[j:j + t] = True
        parts = []
        covered = np.zeros(len(seq), dtype=bool)
        for (j, p, t) in sorted(runs, key=lambda r: (-r[2], r[0])):
            if covered[j:j + t].any():
                continue  # keep structure string readable: best runs only
            covered[j:j + t] = True
            gap = bp[0] - (p + t)
            parts.append(f"{j}-{j+t-1}@p{p}(L{t},g{gap})")
        uncov = [str(k) for k in np.where(~covered)[0]]
        block_rows.append({
            "ts": ts_val.isoformat(), "era": era, "n": len(seq),
            "idx_min": min(bidx), "idx_max": max(bidx),
            "srcs": ",".join(sorted(set(g["source"]))),
            "cov2_rows": int(cov2.sum()), "cov3_rows": int(cov3.sum()),
            "n_runs": len(runs),
            "structure": ";".join(parts) + ("|UNC:" + ",".join(uncov)
                                            if uncov else ""),
        })

    rdf = pd.DataFrame(run_rows)
    bdf = pd.DataFrame(block_rows)
    rdf.to_csv(os.path.join(ART, "block_runs.csv"), index=False)
    bdf.to_csv(os.path.join(ART, "block_coverage.csv"), index=False)

    print(f"\nblocks={len(bdf)} block_rows={int(bdf['n'].sum())} "
          f"runs_ge2={len(rdf)}")
    print("run_len_dist:", dict(sorted(Counter(rdf["len"]).items())))
    print("\ncoverage by era (blocks):")
    for era, sub in bdf.groupby("era"):
        full3 = int((sub["cov3_rows"] == sub["n"]).sum())
        zero3 = int((sub["cov3_rows"] == 0).sum())
        print(f"  {era}: blocks={len(sub)} rows={int(sub['n'].sum())} "
              f"full_cov3={full3} zero_cov3={zero3} "
              f"rows_cov3={int(sub['cov3_rows'].sum())}")

    print("\nlen>=3 run gap dist (real):")
    r3 = rdf[rdf["len"] >= 3]
    print("  before(gap>=0):", Counter(pd.cut(r3.loc[r3["gap"] >= 0, "gap"],
          [-1, 0, 3, 10, 50, 200, 10**9]).astype(str)).most_common())
    print("  after (gap<0):", int((r3["gap"] < 0).sum()))
    print("len>=3 partner status:", Counter(
        ("kept" if k == t else "mixed" if k else "dropped")
        for k, t in zip(r3["partner_kept"], r3["len"])).most_common())
    print("len>=3 off_const:", Counter(r3["off_const"]).most_common())
    print("len>=3 partner batch-frac:", Counter(
        pd.cut(r3["p_batch_frac"], [-0.01, 0.01, 0.99, 1.01]).astype(str)
    ).most_common())
    print("len>=3 far runs (|gap|>10):")
    print(r3[r3["gap"].abs() > 10]
          [["ts", "era", "block_n", "j_start", "p_start", "len", "gap",
            "partner_kept", "p_batch_frac", "p_srcs", "off_const"]]
          .to_string(index=False))
    print("len==2 runs: total", int((rdf["len"] == 2).sum()),
          "adjacent(|gap|<=2):",
          int(((rdf["len"] == 2) & (rdf["gap"].abs() <= 2)).sum()))

    # ---- null calibration -----------------------------------------------------
    rng = np.random.default_rng(42)
    sizes = bdf["n"].to_numpy()
    mult_pool = np.array(list(kept_mult_counts.elements()))
    fake_len, fake_gap, fake_off = [], [], []
    fake_adj2 = 0
    for _ in range(len(sizes)):
        L = int(rng.choice(sizes))
        seq = [fmt_m(m) for m in rng.choice(mult_pool, size=L, replace=True)]
        w = int(rng.integers(0, N - L))
        blocked = np.zeros(N, dtype=bool)
        blocked[w:w + L] = True
        for (j, p, t) in find_runs(seq, M, blocked, pos_by_mult, min_len=2):
            fake_len.append(t)
            fake_gap.append(w - (p + t))
            if t == 2 and abs(w - (p + t)) <= 2:
                fake_adj2 += 1
    print("\nNULL (fake blocks): n_runs_ge2=", len(fake_len),
          "run_len_dist:", dict(sorted(Counter(fake_len).items())),
          "adjacent2(|gap|<=2):", fake_adj2,
          "len3:", sum(1 for t in fake_len if t == 3))

    print("\nlen==2 adjacent runs (real) with partner batch-frac:")
    amb = rdf[(rdf["len"] == 2) & (rdf["gap"].abs() <= 2)]
    print(amb[["ts", "era", "block_n", "j_start", "p_start", "gap",
               "partner_kept", "p_batch_frac", "p_srcs"]]
          .to_string(index=False))

    print("\nfull-cov3 blocks by (era, size bucket):")
    bdf["size_bucket"] = pd.cut(bdf["n"], [1, 2, 3, 5, 9, 1000],
                                labels=["2", "3", "4-5", "6-9", "10+"])
    print(bdf[bdf["cov3_rows"] == bdf["n"]]
          .groupby(["era", "size_bucket"], observed=True).size().to_dict())
    print("\nzero-cov3 blocks by (era, size bucket):")
    print(bdf[bdf["cov3_rows"] == 0]
          .groupby(["era", "size_bucket"], observed=True).size().to_dict())

    print("\nexample structures (one per era x coverage class):")
    shown = set()
    for _, r in bdf.iterrows():
        cls = ("FULL" if r["cov3_rows"] == r["n"] else
               "PART" if r["cov3_rows"] > 0 else "NONE")
        key = (r["era"], cls)
        if key in shown:
            continue
        shown.add(key)
        print(f"  [{key}] ts={r['ts']} n={r['n']} idx={r['idx_min']}-{r['idx_max']} "
              f"srcs={r['srcs']}")
        print(f"      {r['structure'][:220]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
