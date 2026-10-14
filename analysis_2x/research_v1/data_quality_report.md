# Data quality report

## Source and snapshot

- Source: PostgreSQL table `public.aviator_rounds`; the recorded extraction is an all-source snapshot taken 2026-10-06T21:50:44.081995+00:00.
- Extracted rows: **22,373**, matching the extraction manifest table count. Snapshot SHA-256: `4a905837bc07dc2333cbfb9f237a192c10dcfec30dfef84d93cdfa2c36ff9387`.
- The local PostgreSQL service did not answer `pg_isready` during this run, so the live table count could not be refreshed. Reported count is the verified historical extraction boundary, not a claim about the current live table.
- Source mix: `{"collector": 7564, "collector_catchup": 1596, "legacy_import": 13213}`.

## Identity, order, and continuity

- `round_id`, primary `id`, and local `round_index` duplicates: `{'round_id': 0, 'round_index': 0, 'primary_id': 0}`.
- All `22,373` entries use `round_identity_type=COLLECTOR_OBSERVATION_HASH` and `round_index_source=LOCAL_SEQUENCE`; platform round IDs present: **0**, platform indices present: **0**.
- `continuity_verified=true`: **4,344** rows; proof counts: `{"HISTORY_SNAPSHOT_OVERLAP": 4344, "LEGACY_UNVERIFIED": 18029}`. These flags establish overlap-verified relative order only, not platform identity.
- Declared `gap_before` rows: **25**. Missing local index values in the raw snapshot: **28**. Timestamp inversions in local-index order: **63**.
- Compatibility `timestamp` values parse in UTC on **22,373/22,373** rows, spanning `2026-07-28T13:37:37.587000+00:00` to `2026-10-06T19:37:05.564000+00:00`. `platform_timestamp` is populated on **13,140** rows and parses on **13,140** (unparseable populated values: 0; range `2026-07-28T13:37:37.587000+00:00` to `2026-09-27T06:16:20.433000+00:00`); source counts are `{"legacy_import": 13140}`. It equals the compatibility timestamp on **13,140** rows, has **0** continuity-verified rows, and identity-confidence counts `{"UNKNOWN": 13140}`. Provenance is unverified; do not treat these values as confirmed platform event times.
- Predictive pattern evaluation is restricted to **4,297** kept rows with `HISTORY_SNAPSHOT_OVERLAP` order proof, split into **27** segments (largest 1,036). Features reset at each declared gap, unverified row, or missing raw index.

## Multiplier validity and overlap cleaning

- Minimum multiplier: **1.00x**; maximum: **66500.81x**.
- Multipliers below 1.00x: **0**; non-finite values: **0**; values not representable to two decimals: **0**.
- Existing audited clean set retains **20,769** rows and excludes **1,604** rows: exact same-timestamp same-multiplier **100**, repeated catch-up overlap **1,443**, and near-timestamp same-multiplier captures **61**.
- Equal multipliers alone are never a deduplication key. Legitimate adjacent equal outcomes remain. Exclusion decisions rely on timestamp/source/sequence overlap evidence and are listed in `../artifacts/dropped_rows.csv`.

## Coverage and limits

- The table combines `legacy_import`, live collector, and collector catch-up observations. It is a convenience archive, not a random sample of all platform rounds; gaps, catch-up batches, and changing collection coverage can bias frequency and chronology summaries.
- No rows contain a platform round ID or index. Although `13,140` rows have an explicit `platform_timestamp`, all are from legacy imports and none are continuity-verified; daily/UTC-hour summaries are reported only as unverified legacy-field diagnostics, not trusted game-time effects. These values do not enter candidate features, ordering, continuity, or prospective claims.
- Historical reports in the repository already examined earlier portions of this archive. The current predictive order subset remains order-only and cannot establish a repeatable or live betting edge.
- Raw snapshot hash: `4a905837bc07dc2333cbfb9f237a192c10dcfec30dfef84d93cdfa2c36ff9387`. Clean-set hash: `fc1911b0b25629f73f2a26c3399f8ea3a44eebb9661a48bd303322e4503a6abf`. Existing quality-audit hash: `37fcffceec2069af267a23822e1a0702ed903ed13fb090615b42d66a84a66b00`.
