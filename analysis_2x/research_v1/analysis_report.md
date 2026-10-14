# Historical 2.00x analysis

This is an exploratory retrospective analysis. It does not establish a profitable or live-verified strategy.

## Descriptive history

- Retained archive records: 20,769; >=2.00x: 10,041/20,769 (48.35%, Wilson 95% 47.67%–49.03%).
- Verified-order analysis records: 4,297 across 27 segments. The evidence is overlap-verified order only; no platform IDs or platform order indices are present.
- Multiplier quantiles: {"0.0": 1.0, "0.01": 1.0, "0.05": 1.02, "0.1": 1.0780000000000018, "0.25": 1.29, "0.5": 1.94, "0.75": 3.86, "0.9": 9.844000000000014, "0.95": 20.291999999999973, "0.99": 96.5496, "1.0": 66500.81}.
- Bands: {"1.20-<1.50": 3409, "1.50-<2.00": 3394, "2.00-<3.00": 3329, "3.00-<5.00": 2690, "5.00-<10.00": 1973, "<1.20": 3925, ">=10.00": 2049}.
- Below-2x streaks: 1,049; longest 11. At-least-2x streaks: 1,062; longest 13.
- Completed hit-to-hit waiting intervals: 2,040; median 2.000 verified rounds, longest 12.
- Rolling verified-order rates:
  - 25 rounds: 3,762 windows, mean 48.06%, median 48.00%, 5th–95th percentile 32.00%–64.00%.
  - 50 rounds: 3,370 windows, mean 48.22%, median 48.00%, 5th–95th percentile 38.00%–60.00%.
  - 100 rounds: 2,847 windows, mean 48.08%, median 48.00%, 5th–95th percentile 40.00%–57.00%.
  - 250 rounds: 1,947 windows, mean 47.45%, median 47.20%, 5th–95th percentile 43.60%–51.20%.
  - 500 rounds: 1,030 windows, mean 47.46%, median 47.40%, 5th–95th percentile 45.20%–50.20%.
- Capture-time chronological blocks are included below as coverage diagnostics; they are not platform event-time analyses.

| Capture-time block | Rows | >=2x rate | UTC span |
|---:|---:|---:|---|
| 1 | 2,077 | 48.68% | 2026-07-28T13:37:37.587000+00:00 to 2026-09-06T22:35:53.697000+00:00 |
| 2 | 2,077 | 48.53% | 2026-09-06T22:36:19.721000+00:00 to 2026-09-18T03:10:00.890000+00:00 |
| 3 | 2,077 | 48.00% | 2026-09-18T03:10:17.014000+00:00 to 2026-09-19T04:21:26.676000+00:00 |
| 4 | 2,077 | 48.29% | 2026-09-19T04:21:40.875000+00:00 to 2026-09-24T21:23:15.488000+00:00 |
| 5 | 2,077 | 47.81% | 2026-09-24T21:23:51.544000+00:00 to 2026-09-26T04:33:31.833000+00:00 |
| 6 | 2,077 | 46.65% | 2026-09-26T04:33:42.410000+00:00 to 2026-09-27T20:37:28.661000+00:00 |
| 7 | 2,077 | 49.49% | 2026-09-27T20:38:12.489000+00:00 to 2026-10-02T03:50:29.362000+00:00 |
| 8 | 2,077 | 49.59% | 2026-10-02T03:50:44.324000+00:00 to 2026-10-04T06:30:37.252000+00:00 |
| 9 | 2,077 | 47.28% | 2026-10-04T06:30:50.400000+00:00 to 2026-10-05T06:25:04.175000+00:00 |
| 10 | 2,076 | 49.13% | 2026-10-05T06:25:04.175000+00:00 to 2026-10-06T19:37:05.564000+00:00 |

Unverified legacy `platform_timestamp` diagnostics only (not trusted game-event time): **11,969 retained rows**, across **17 UTC dates**. They are excluded from predictive features and chronology.

| Legacy timestamp UTC date | Rows | >=2x hits | >=2x rate |
|---|---:|---:|---:|
| 2026-07-28 | 99 | 43 | 43.43% |
| 2026-07-29 | 336 | 153 | 45.54% |
| 2026-07-30 | 11 | 8 | 72.73% |
| 2026-08-04 | 16 | 6 | 37.50% |
| 2026-09-06 | 1,829 | 907 | 49.59% |
| 2026-09-07 | 660 | 336 | 50.91% |
| 2026-09-12 | 527 | 239 | 45.35% |
| 2026-09-17 | 232 | 113 | 48.71% |
| 2026-09-18 | 1,997 | 969 | 48.52% |
| 2026-09-19 | 972 | 457 | 47.02% |
| 2026-09-20 | 320 | 151 | 47.19% |
| 2026-09-21 | 317 | 146 | 46.06% |
| 2026-09-22 | 958 | 467 | 48.75% |
| 2026-09-24 | 355 | 180 | 50.70% |
| 2026-09-25 | 1,698 | 807 | 47.53% |
| 2026-09-26 | 1,060 | 488 | 46.04% |
| 2026-09-27 | 582 | 282 | 48.45% |

| Legacy timestamp UTC hour | Rows | >=2x hits | >=2x rate |
|---:|---:|---:|---:|
| 00:00 | 883 | 445 | 50.40% |
| 01:00 | 889 | 442 | 49.72% |
| 02:00 | 776 | 379 | 48.84% |
| 03:00 | 744 | 341 | 45.83% |
| 04:00 | 918 | 442 | 48.15% |
| 05:00 | 883 | 417 | 47.23% |
| 06:00 | 233 | 113 | 48.50% |
| 07:00 | 32 | 14 | 43.75% |
| 08:00 | 236 | 102 | 43.22% |
| 09:00 | 433 | 200 | 46.19% |
| 10:00 | 325 | 151 | 46.46% |
| 11:00 | 487 | 244 | 50.10% |
| 12:00 | 605 | 274 | 45.29% |
| 13:00 | 523 | 237 | 45.32% |
| 14:00 | 437 | 213 | 48.74% |
| 15:00 | 321 | 159 | 49.53% |
| 16:00 | 123 | 59 | 47.97% |
| 17:00 | 133 | 70 | 52.63% |
| 18:00 | 29 | 14 | 48.28% |
| 19:00 | 191 | 85 | 44.50% |
| 20:00 | 366 | 175 | 47.81% |
| 21:00 | 604 | 292 | 48.34% |
| 22:00 | 876 | 409 | 46.69% |
| 23:00 | 922 | 475 | 51.52% |

## Search and validation

The frozen search evaluated 582 predeclared candidates. Holm adjustment covered the complete family. The independent null simulation estimated its baseline from discovery only. Best-search null-tail probability: 65.97%.

The short list was frozen after discovery and validation. The latest period was opened once afterward, but is contaminated by prior model candidate evaluation and cannot be called an untouched final test.

| Rule | Discovery n | Discovery rate (95% CI) | Baseline | Ideal ROI | Validation n | Validation rate (95% CI) | Baseline | Ideal ROI | Final n | Final rate (95% CI) | Baseline | Ideal ROI | Matched random | Status |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|
| binary_3_010 | 310 | 48.39% [42.88%–53.94%] | 47.34% | -3.23% | 105 | 56.19% [46.65%–65.30%] | 51.30% | 12.38% | 116 | 38.79% [30.42%–47.88%] | 45.89% | -22.41% | 45.54% | EDGE_UNVERIFIED |
| rate_5_le_2 | 1,370 | 46.28% [43.65%–48.92%] | 47.27% | -7.45% | 406 | 54.68% [49.82%–59.46%] | 51.31% | 9.36% | 493 | 46.45% [42.09%–50.86%] | 46.06% | -7.10% | 45.52% | EDGE_UNVERIFIED |
| logvol_50_le_1_0 | 1,442 | 47.85% [45.28%–50.43%] | 48.00% | -4.30% | 342 | 54.68% [49.38%–59.87%] | 52.49% | 9.36% | 319 | 46.71% [41.31%–52.19%] | 46.28% | -6.58% | 47.49% | EDGE_UNVERIFIED |

At 2.00x, idealized break-even is 50% before costs. A positive lift versus a sub-50% historical baseline is not enough to establish positive return.

## Chronological replay

Historical outcomes assume a fixed one-unit stake, +1 unit for a 2.00x hit, -1 unit for a miss, one active round at a time, 100-round sessions, and stop limits of -3 units, three consecutive losses, or +3 units. These are parameterized examples, not user preferences or verified execution.

| Rule | Period | Opportunities | Bets | Wins | Losses | Net units | Return/staked | Max drawdown | Longest losing sequence | Session stop frequency |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| binary_3_010 | discovery | 310 | 203 | 100 | 103 | -3.00 | -1.48% | 25.00 | 3 | 59.02% |
| binary_3_010 | validation | 105 | 48 | 26 | 22 | 4.00 | 8.33% | 25.00 | 3 | 59.02% |
| binary_3_010 | retrospective_final | 116 | 76 | 29 | 47 | -18.00 | -23.68% | 25.00 | 3 | 59.02% |
| rate_5_le_2 | discovery | 1,370 | 233 | 104 | 129 | -25.00 | -10.73% | 39.00 | 3 | 81.97% |
| rate_5_le_2 | validation | 406 | 75 | 38 | 37 | 1.00 | 1.33% | 39.00 | 3 | 81.97% |
| rate_5_le_2 | retrospective_final | 493 | 61 | 26 | 35 | -9.00 | -14.75% | 39.00 | 3 | 81.97% |
| logvol_50_le_1_0 | discovery | 1,442 | 208 | 101 | 107 | -6.00 | -2.88% | 22.00 | 3 | 68.85% |
| logvol_50_le_1_0 | validation | 342 | 42 | 23 | 19 | 4.00 | 9.52% | 22.00 | 3 | 68.85% |
| logvol_50_le_1_0 | retrospective_final | 319 | 44 | 22 | 22 | 0.00 | 0.00% | 22.00 | 3 | 68.85% |
| NO_BET | discovery | 0 | 0 | 0 | 0 | 0.00 | n/a | 0.00 | 0 | 0.00% |
| NO_BET | validation | 0 | 0 | 0 | 0 | 0.00 | n/a | 0.00 | 0 | 0.00% |
| NO_BET | retrospective_final | 0 | 0 | 0 | 0 | 0.00 | n/a | 0.00 | 0 | 0.00% |

The `NO_BET` reference has zero exposure and zero net loss/profit; return per unit staked is undefined with no stake. Rule baselines are unconditional rates on each eligible period, with matched-coverage random-entry rates shown above.

Execution sensitivity scenarios are in `backtest_results.json`; they are hypothetical and do not estimate platform failure rates.

## Conclusion

**NO_VALIDATED_EDGE.** No rule receives `HISTORICAL_EDGE_SUPPORTED`: the archive has order-only identities and the retrospective final period overlaps prior model search. The generated rules are disabled and labeled `EDGE_UNVERIFIED` or `INSUFFICIENT_DATA`; they are options for explicit user selection, not automatic recommendations.

**Mu Kinyarwanda:** Ibi bisubizo bisobanura amateka gusa; ntibyemeza ko amateka ateganya neza uruziga rukurikira cyangwa ko gutega byunguka. Ku ntego ya 2.00x, nibura 50% by'intsinzi birakenewe mbere y'ibiciro n'amakosa yo gushyira igitego. Amakuru afite urutonde rwemejwe binyuze mu kugereranya snapshots gusa, kandi igihe cya nyuma cyari cyararebwe mu isuzuma ry'icyitegererezo. Bityo nta `HISTORICAL_EDGE_SUPPORTED` yabonetse. Amategeko yakozwe aracyafunze; hitamo rimwe gusa nyuma yo gusuzuma igihombo n'imbibi zaryo.

## Reproduction

Run from the Winner Predict repository root. To preserve this completed one-shot evaluation, reproduce into a fresh sibling output folder:

```bash
mkdir -p analysis_2x/research_v1_repro
cp analysis_2x/research_v1/research_plan.json analysis_2x/research_v1/run_analysis.py analysis_2x/research_v1/verify.py analysis_2x/research_v1/requirements.txt analysis_2x/research_v1_repro/
python3 analysis_2x/research_v1_repro/run_analysis.py audit
python3 analysis_2x/research_v1_repro/run_analysis.py search
python3 analysis_2x/research_v1_repro/run_analysis.py search
python3 analysis_2x/research_v1_repro/run_analysis.py finalize
python3 analysis_2x/research_v1_repro/verify.py
```
