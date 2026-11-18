# HIGH series lifecycle

The machine consumes only canonical, committed, verified rows in cohort order. Its JSON snapshot and the worker checkpoint are committed in the same database transaction. `identity_key` is the idempotency key; replayed identities and positions are ignored.

| State before a verified round | Event or condition observed on that round | Result for that round | Next state |
| --- | --- | --- | --- |
| `IDLE` | Trigger horizon becomes usable without HIGH | Observe only | `ARMED_FOR_HIGH` |
| `IDLE` / `ARMED_FOR_HIGH` | Fresh transition into `HIGH_DOMINANT` | Decision round; excluded from Part 1 | `PART1_COLLECTING` |
| `PART1_COLLECTING` | Any state, before R5 | Count once; do not pause | `PART1_COLLECTING` |
| `PART1_COLLECTING` | R5 and horizon is HIGH | Count R5; open Part 2 for the next row | `PART2_ACTIVE` |
| `PART1_COLLECTING` | R5 and horizon is known non-HIGH | Count R5; close with `STOPPED_PART1_NO_HIGH` | `COOLDOWN` at 0/N |
| `PART1_COLLECTING` | R5 horizon unavailable | Count R5; mark completion `DATA_INCOMPLETE` | `PART1_COMPLETED` with stale health flag |
| `PART2_ACTIVE` | Eligibility was already locked before this row | Settle once; then use the updated horizon for the next row | Active, paused, or next batch |
| `PART2_ACTIVE` | Updated state is LOW or another non-HIGH state | Settle eligible row; preserve batch and start pause at 0 | `PART2_PAUSED_LOW_DOMINANT` or `PART2_PAUSED_NON_HIGH` |
| `PART2_PAUSED_*` | Next verified non-HIGH observation | No Part 2 result; increment pause count | Same paused state, or `COOLDOWN` at the limit |
| `PART2_PAUSED_*` | HIGH returns, including on the limit observation | Pause observation only; close interval and resume | `PART2_ACTIVE` on the same batch |
| `COOLDOWN` | Each later verified row | Cooldown observation only | `COOLDOWN`, then `ARMED_FOR_HIGH` at N/N |
| Any state | Feed is stale or a row is not synchronized | No settlement or pause/cooldown increment | Lifecycle preserved; `DATA_STALE` health flag |

The default configuration is trigger horizon 5, pause limit 10 verified rounds, cooldown 10 verified rounds, and no wall-clock timeout. The API validates custom pause limits from 1 through 10,000 and optional timeouts from 1 through 31,536,000 seconds. A time timeout is evaluated by the idle worker and closes the pause interval without adding any round result.

Performance keeps the established 2x convention: multiplier `>= 2.00` is WIN, including EXACT at 2.00x; lower values are LOSS. Net units are wins minus losses and are SHADOW-only descriptive accounting.
