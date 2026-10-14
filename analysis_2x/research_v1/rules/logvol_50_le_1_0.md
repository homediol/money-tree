# logvol_50_le_1_0

**Status:** `EDGE_UNVERIFIED` · **Enabled:** no · **Target:** 2.00x

## Entry rule

Standard deviation of log multipliers over the previous 50 rounds <= 1.00. If it is true after a source round, the historical replay considers only the next eligible round. The rule uses no target-round information.

## Historical evidence

- Discovery: 1,442 selections, 690 hits, 752 losses, hit rate 47.85%, baseline 48.00%, ideal return -4.30% per unit staked.
- Validation: 342 selections, 187 hits, 155 losses, hit rate 54.68%, baseline 52.49%, ideal return 9.36% per unit staked.
- Retrospective final period: 319 selections, 149 hits, 170 losses, hit rate 46.71%, baseline 46.28%, ideal return -6.58% per unit staked. This period is contaminated by prior model evaluation and is not confirmatory.
- At 2.00x, the idealized break-even hit rate is 50% before execution costs. Historical cashout settlement is assumed, not verified.

## Use conditions

This is a research configuration only. It is disabled and must not be auto-selected. Before any future use, a user must explicitly choose it and set stake, cooldown, session loss limit, consecutive-loss limit, and profit goal. Runtime eligibility remains separate from safety: authentication, browser health, verified continuity, target validity, balance, duplicate protection, session limits, and the existing risk engine must all pass. Missing or stale data produces `STOP`.
