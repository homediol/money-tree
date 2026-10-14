# rate_5_le_2

**Status:** `EDGE_UNVERIFIED` · **Enabled:** no · **Target:** 2.00x

## Entry rule

At most 2 of the previous 5 rounds were >=2.00x. If it is true after a source round, the historical replay considers only the next eligible round. The rule uses no target-round information.

## Historical evidence

- Discovery: 1,370 selections, 634 hits, 736 losses, hit rate 46.28%, baseline 47.27%, ideal return -7.45% per unit staked.
- Validation: 406 selections, 222 hits, 184 losses, hit rate 54.68%, baseline 51.31%, ideal return 9.36% per unit staked.
- Retrospective final period: 493 selections, 229 hits, 264 losses, hit rate 46.45%, baseline 46.06%, ideal return -7.10% per unit staked. This period is contaminated by prior model evaluation and is not confirmatory.
- At 2.00x, the idealized break-even hit rate is 50% before execution costs. Historical cashout settlement is assumed, not verified.

## Use conditions

This is a research configuration only. It is disabled and must not be auto-selected. Before any future use, a user must explicitly choose it and set stake, cooldown, session loss limit, consecutive-loss limit, and profit goal. Runtime eligibility remains separate from safety: authentication, browser health, verified continuity, target validity, balance, duplicate protection, session limits, and the existing risk engine must all pass. Missing or stale data produces `STOP`.
