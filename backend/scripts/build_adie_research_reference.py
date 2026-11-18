#!/usr/bin/env python3
"""Build ADIE's compact, fixed Phase 2 context reference from verified artifacts."""
from __future__ import annotations

import argparse
import csv
import gzip
import io
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PHASE2 = ROOT / "research_phase2_2026-11-16" / "datasets"
OUTPUT = ROOT / "backend" / "app" / "resources" / "adie_research_reference.csv.gz"
MANIFEST = OUTPUT.with_name("adie_research_reference_manifest.json")
FEATURES = ["decision_position", "prev_20_high_share_nonexact", "prev_50_high_share_nonexact",
            "prev_100_high_share_nonexact", "current_streak_outcome", "current_streak_length",
            "prev_50_hl_alternation_rate", "prev_50_log_sd"]
EVAL = ["decision_position", "neighbor_count", "mean_distance"]
for horizon in (5, 10, 20, 30, 50):
    EVAL += [f"matured_n_{horizon}", f"context_H_net_per_round_{horizon}",
             f"context_L_net_per_round_{horizon}"]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=OUTPUT)
    args = parser.parse_args()
    eval_path = PHASE2 / "context_similarity_evaluation.csv.gz"
    feature_path = PHASE2 / "round_level_features.csv.gz"
    if not eval_path.exists() or not feature_path.exists():
        raise SystemExit("required Phase 2 context artifacts are missing")
    evals = {}
    with gzip.open(eval_path, "rt", newline="", encoding="utf-8") as src:
        for row in csv.DictReader(src):
            evals[row["decision_position"]] = row
    args.output.parent.mkdir(parents=True, exist_ok=True)
    fields = FEATURES + EVAL[1:]
    written = 0
    with gzip.open(feature_path, "rt", newline="", encoding="utf-8") as src, \
            args.output.open("wb") as raw:
        with gzip.GzipFile(filename="", fileobj=raw, mode="wb", compresslevel=9, mtime=0) as compressed:
            with io.TextIOWrapper(compressed, encoding="utf-8", newline="") as dst:
                reader = csv.DictReader(src)
                writer = csv.DictWriter(dst, fieldnames=fields)
                writer.writeheader()
                for row in reader:
                    ev = evals.get(row["decision_position"])
                    if not ev:
                        continue
                    out = {name: row.get(name, "") for name in FEATURES}
                    out.update({name: ev.get(name, "") for name in EVAL[1:]})
                    writer.writerow(out)
                    written += 1
    def sha256(path):
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    manifest = {
        "artifact": args.output.name,
        "artifact_sha256": sha256(args.output),
        "source_phase": "research_phase2_2026-11-16",
        "sources": {
            "round_level_features.csv.gz": sha256(feature_path),
            "context_similarity_evaluation.csv.gz": sha256(eval_path),
        },
        "rows": written,
        "fingerprint": ["prev_20_high_share_nonexact", "prev_50_high_share_nonexact",
                        "prev_100_high_share_nonexact", "current_streak_length/10",
                        "prev_50_hl_alternation_rate", "prev_50_log_sd"],
        "selection": "nearest Euclidean fingerprint, at most 25 contexts spaced 60 historical positions; reference outcomes remain exploratory",
        "decision_rule": "no normal candidate qualifies in policy v1 because Phase 2 corrected tests validated no predictive edge",
    }
    manifest_path = MANIFEST if args.output.resolve() == OUTPUT.resolve() else args.output.with_name("adie_research_reference_manifest.json")
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {written} context rows to {args.output}")


if __name__ == "__main__":
    main()
