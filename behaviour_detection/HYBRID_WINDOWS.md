# Hybrid dominance windows

The hybrid layer is a descriptive extension to the existing Behaviour Detection worker. It consumes the same persisted canonical rounds and does not change SHADOW decisions, Rules Performance calculations, or ADIE selection.

## Cohort and lifecycle

Schema migration 7 creates one `PROSPECTIVE` cohort and records its activation time. Imported research rows and rows inserted before activation are excluded. The first post-activation row whose existing source-order quality is verified receives local cohort position 1. Positions are local to this cohort; they are not provider-global round IDs.

Each cohort window covers 100 eligible observed outcomes at positions `[1..100]`, `[101..200]`, and so on. It is completed only after the 100th eligible row is committed. Time estimates never fill a position or complete a window. A report records the exact source identities, policy version, counts, opening/middle/closing summaries, clusters, relationships, and any authoritative source-sequence gaps. A restarted worker resumes from the persisted cohort position and window rows.

The read-only endpoints are `GET /api/v1/behaviour/hybrid` and `GET /api/v1/behaviour/hybrid/windows?limit=20`.

## Detection policy (`hybrid-dominance-v1`)

- H, L, and E remain separate. HIGH/LOW shares and Wilson intervals use only non-EXACT rows.
- A direction is labelled dominant at HIGH share `>= 0.60` or `<= 0.40`, with at least half of the horizon observed as non-EXACT and at least four non-EXACT outcomes. Smaller rolling horizons remain explicitly insufficient.
- The live 5/10/20/30/50/100 horizons use past and current verified rows only. A 10-round versus 50-round share change of at least 10 percentage points labels directional momentum. The worker requires three successive updates before confirming a state or momentum change.
- A cluster bridges one to three opposite H/L outcomes. EXACT terminates a cluster. Run counts, interrupted clusters, span, and density describe structure; they are not forecast evidence.
- The historical descriptive reference is the Phase 2 canonical set: 53,503 rounds, including 25,966 H, 27,398 L, and 139 E. Its H share among non-EXACT rounds is about 48.66%. The research reports found no validated predictive advantage after multiple-testing correction; the dashboard labels all these results descriptive.

## Time and coverage

Platform event timestamps and delivery-observation timestamps are analyzed separately. Cadence samples require a positive gap between adjacent rounds from the same source that share the same timestamp role; timestamp ties are counted and excluded. Recent samples are capped at 500 verified canonical observations. Rows before cohort activation may calibrate cadence and freshness, but their outcomes never enter prospective rolling analysis or windows. Median, empirical p10/p90/p95, and median absolute deviation summarize cadence. Estimated window durations use the event cadence when at least 20 positive event-time gaps exist, otherwise delivery cadence.

Freshness and possible-missing warnings use delivery cadence where available. The stale threshold is the maximum of the Phase 1 p99 gap (180.0452 seconds), three times recent p95, and `median + 6 × 1.4826 × MAD`. A plausible missing-round range divides the stale interval by recent p90/p10 cadence and is always marked low confidence. It does not assert round identities. Same-source authoritative sequence-number gaps can be confirmed; an upstream source-gap flag without a count is reported without inventing one.

The API returns observed-round progress and estimated-time progress separately. Collector outages and unhealthy source status remain visible and stop repeat alerts. Event time is used for window duration only when available consistently across that window; otherwise the report uses a consistent observation or ingestion timestamp role.

## HIGH-dominance forward 2x audit (`rolling-high-dominance-next5-at-least-2x-v4`)

When a row in the live rolling 5/10/20/30/50/100 horizon panel changes into `HIGH_DOMINANT`, the worker opens one prospective series anchored on that verified decision round. If several horizons enter HIGH on the same update, it follows the widest newly-high horizon and displays that source. The initial five-round batch starts with the next eligible verified cohort round after the decision, then counts four more. A multiplier of at least 2.00x, including exactly 2.00x, is a 2x WIN; anything below 2.00x is a LOSS. This audit rule is separate from SHADOW settlement and does not place or alter a prediction.

The overview card separates the initial five-round check from five-round continuation batches. Continuation starts after the initial fifth round only if the same source horizon is still `HIGH_DOMINANT`; the boundary round is not counted twice. It keeps collecting while that horizon remains HIGH and stops immediately after observing a verified round that makes it `LOW_DOMINANT` (or another non-HIGH state). The transition round's multiplier result is included before stopping. If another horizon remains HIGH when the watched horizon exits, the worker can hand off to it using the current verified round as a fresh decision anchor; it never backdates the audit. If an authoritative source sequence proves a gap or reversal within a batch, it marks that batch incomplete rather than filling the missing result. The batches, source identities, phase, triggering horizon and last observed rolling-horizon states are persisted by migrations 8 through 12. Migrations 10 through 12 repair batch anchoring so the first sampled result is the next verified round after the HIGH decision and rebase an in-progress legacy series without double-counting its decision round. LOW dominance never starts a series.

Historical Part 1 and Part 2 totals cover all completed prior series and are aggregated independently by phase. The UI shows the newest five batches per phase first; “View all” requests older saved batches in bounded pages. Batch records can be deleted only under the existing series and phase safeguards.

## Confirmed HIGH lifecycle test (`confirmed-high-forward-checkpoints-5-10-30-50-v1`)

This is a separate, durable descriptive test keyed to the cohort's confirmed overall `HIGH_DOMINANT` state, not the rolling-horizon 2x audit above. A confirmed transition into HIGH opens an episode anchored on that decision round. If enabled while HIGH is already confirmed and no prior episode exists, the first verified update opens a prospective episode without backfilling older observations. Scoring starts on the verified round after the anchor. Cumulative checkpoints are recorded at 5, 10, 30, and 50 observations. The episode stops at the first confirmed non-HIGH state; the observation that causes the state exit is included as its final sample. A later confirmed transition back into HIGH opens a new episode. Completed 50-round episodes do not restart until HIGH exits and re-enters.

The test card displays the current confirmed state, momentum and strength, episode progress, checkpoint results, H/L/EXACT counts, and hypothetical 2x WIN/LOSS/net results. Multiplier `>= 2.00x` counts as a test WIN (so EXACT 2.00x is included in WIN and separately counted as EXACT); HIGH classification itself remains strict `> 2.00x`. These observations are not prediction evidence and do not alter SHADOW, Rules Performance, or ADIE. Episodes persist in the Behaviour database and resume after worker restart. Schema migration 13 adds the episode store and its active lookup index.

## Notifications and browser limits

Behaviour events use the existing shared notification manager and its local settings, sound verification, voice selection, cooldowns, and queue. Active dominance repeats use one timer and stop on stale/degraded data, a different current rolling state, mute, stop, or route teardown. Synthetic tests never call the backend and cannot affect rounds or reports.

Web Audio and SpeechSynthesis require browser support and user interaction. Playback status does not establish speaker audibility; the user must confirm it. A suspended tab or closed browser cannot be relied on for notification output. Server-side analysis and persisted reports continue through the existing worker independently of browser audio.
