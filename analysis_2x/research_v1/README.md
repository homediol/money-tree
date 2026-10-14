# Aviator 2.00x historical research

This isolated pipeline reads the hash-pinned PostgreSQL snapshot and audited clean-set files in `../artifacts/`. It does not connect to or alter the betting runtime. Historical rule configs are disabled and never activate betting.

The frozen plan is `research_plan.json`. It records the snapshot boundary, candidate family, split boundaries, support thresholds, statistical methods, session assumptions, and known contamination from earlier model searches. The latest 20% is a contaminated retrospective period, not an untouched final test. Only future rounds collected after a frozen policy selection can provide confirmation.

## Dependencies

From the Winner Predict repository root:

```bash
python3 -m venv .venv-analysis2x
source .venv-analysis2x/bin/activate
python -m pip install -r analysis_2x/research_v1/requirements.txt
```

The implementation was run with Python's installed NumPy, pandas, and Matplotlib versions recorded in `run_manifest.json`.

## Reproduce

Run the stages from the repository root in a fresh sibling output directory so the completed one-shot evaluation is not overwritten:

```bash
mkdir -p analysis_2x/research_v1_repro
cp analysis_2x/research_v1/research_plan.json analysis_2x/research_v1/run_analysis.py analysis_2x/research_v1/verify.py analysis_2x/research_v1/requirements.txt analysis_2x/research_v1_repro/
python3 analysis_2x/research_v1_repro/run_analysis.py audit
python3 analysis_2x/research_v1_repro/run_analysis.py search
python3 analysis_2x/research_v1_repro/run_analysis.py search
python3 analysis_2x/research_v1_repro/run_analysis.py finalize
python3 analysis_2x/research_v1_repro/verify.py
```

The first search scores discovery and validation only and freezes `frozen_shortlist.json`. Repeating search checks deterministic output hashes and still does not inspect final-period outcomes. `finalize` evaluates only that shortlist once; it persists `final_test_record.json` before building reports. Re-running `finalize` reuses that saved evaluation. Search is locked after the retrospective final period has been opened.

The source PostgreSQL service was unavailable during the run. The extracted snapshot has 22,373 rows and a UTC extraction time of 2026-10-06 21:50:44. To analyze newer data, first create a new immutable PostgreSQL extraction and its hash manifest, then freeze a new plan. Do not overwrite these hash-pinned inputs or reuse this final period as untouched evidence.

## Outputs

- `data_quality_report.md` documents the source boundary, overlap cleaning, gaps, timestamp limitations, and identity scope. Explicit legacy `platform_timestamp` values are unverified and used only for labeled daily/UTC-hour diagnostics.
- `analysis_report.md` gives descriptive results, legacy timestamp diagnostics, candidate comparisons, Kinyarwanda summary, and idealized session replay.
- `candidate_rules.csv` contains the full predeclared family and discovery/validation results; only frozen shortlist rows receive retrospective final results.
- `rules/*.json` and matching Markdown files are disabled policy examples with explicit evidence and reason codes.
- `selected_rounds.csv` records past-only trigger features, BET/WAIT/STOP decisions, and observed target outcomes.
- `prospective_validation_plan.md` specifies how to validate one user-selected rule on genuinely new rounds.
- `plots/*.png`, `backtest_results.json`, `null_checks.json`, and `run_manifest.json` retain supporting evidence and hashes.

Two-unit cashout replay uses idealized net profit of +1 normalized unit on a hit and -1 on a miss. Bankroll and stop settings in configs are illustrative parameters only. The historical replay does not verify live execution.
