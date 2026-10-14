# binary_3_010

**Status:** `EDGE_UNVERIFIED` · **Enabled:** no · **Target:** 2.00x

## Entry rule

The previous 3 outcomes were L-H-L (H >=2.00x, L <2.00x). If it is true after a source round, the historical replay considers only the next eligible round. The rule uses no target-round information.

## Historical evidence

- Discovery: 310 selections, 150 hits, 160 losses, hit rate 48.39%, baseline 47.34%, ideal return -3.23% per unit staked.
- Validation: 105 selections, 59 hits, 46 losses, hit rate 56.19%, baseline 51.30%, ideal return 12.38% per unit staked.
- Retrospective final period: 116 selections, 45 hits, 71 losses, hit rate 38.79%, baseline 45.89%, ideal return -22.41% per unit staked. This period is contaminated by prior model evaluation and is not confirmatory.
- At 2.00x, the idealized break-even hit rate is 50% before execution costs. Historical cashout settlement is assumed, not verified.

## Use conditions

This is a research configuration only. It is disabled and must not be auto-selected. Before any future use, a user must explicitly choose it and set stake, cooldown, session loss limit, consecutive-loss limit, and profit goal. Runtime eligibility remains separate from safety: authentication, browser health, verified continuity, target validity, balance, duplicate protection, session limits, and the existing risk engine must all pass. Missing or stale data produces `STOP`.
