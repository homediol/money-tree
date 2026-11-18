# Implementation and acceptance status

Date: 2026-11-14

## Delivered

The Advanced Crash Game Behaviour Detection System runs under this directory as an independent FastAPI service, analysis worker, Behaviour database, migrations, logs, health endpoints, configuration and runtime commands. Its active live source is Winner Predict's PostgreSQL `aviator_rounds` table, opened read-only. The Winner Predict React application exposes the system at `/behaviour`; its requests use `/api/v1/behaviour` and do not send database credentials to the browser. The original Phase 1 import remains immutable and separate from live database sync.

The engine is deterministic and rule based. The package contains no model-training or inference framework. It preserves HIGH (`>2.00`), LOW (`<2.00`) and EXACT (`=2.00`) separately, computes multi-scale windows and sequence features, records explicit source/time uncertainty, and writes a next-100 assessment after every processed round. The default assessment reproduces Phase 1's expanding mean of disjoint future-100 blocks. It does not apply contextual adjustments.

## Phase 1 reconciliation

The imported, immutable snapshot is `phase1-2026-11-13T083013Z` (`2026-11-13T08:30:13.384380Z`), with source SHA-256 `aeaf9c2ef3c504a1fd08d3f7afe83c433b8d3cf8f65bd0cd05cce3bd0f251b89`.

The workspace's `data/roundhistory.json` (SHA-256 `96d18252f04333bdfddf581294afa3c88fbd0ab646a5a8b5a594532a670789d8`) and `backend/data/roundhistory.json` (`21343493762d453a5afc5bbd770428ef829551a3d50f96ff0ed1840e465dfc0a`) are byte-identical to their Phase 1 archived copies. Their workspace modification dates precede the Phase 1 snapshot, so no newer round-history JSON supplement was found. The immutable Phase 1 import uses the reconciled research artifacts; a separate live sync now reads newly stored rows from Winner Predict PostgreSQL after verifying a shared identity overlap.

| Reconciliation item | Imported |
| --- | ---: |
| Raw source records | 171,378 |
| Source records mapped to canonical rounds | 171,378 |
| Canonical rounds | 50,123 |
| HIGH | 24,339 |
| LOW | 25,651 |
| EXACT | 133 |
| Timestamp groups | 427 |
| Phase 1 expanding-baseline blocks | 499 |
| Motif/evidence records | 214 / 214 |

Database reconciliation returned `reconciled`. The Phase 1 baseline replay used 499 target blocks, 249 warm-up blocks and 250 chronological test origins; the reproduced rate MSE was `0.002922870360206869`, matching Phase 1 within tolerance. The reported contextual MSE (`0.00314365132`) remains worse, so no contextual adjustment is active. Thirty of the 124 tested motifs survived the research's Benjamini-Hochberg correction; motif recurrence remains descriptive. The corrected precursor search produced no live precursor rules.

## Collection and ordering limits

Orbit's project-local Data Watch API exposes reading/page IDs, `checkedAt` and payout DOM values, but not round IDs or per-round event timestamps. `checkedAt` is recorded as observation/ingestion time; live event time remains unknown. The parser preserves every decimal digit returned by the DOM value (up to the database's 24 fractional digits) and classifies the exact stored decimal without rounding. If the provider has already rounded the rendered DOM text, hidden multiplier precision is unavailable and cannot be reconstructed. Initial collection pages provider-retained history and only crosses into live canonical order after a unique ordered overlap with the Phase 1 tail. Pre-anchor readings are preserved as quarantined records. If retention does not reach a unique overlap, the collector stores an explicit unverified boundary and does not infer missing outcomes. A changed payload under an existing reading ID is quarantined even when its cursor is unchanged. When a source reading contains two exact ordered copies of its visible payout list, the collector collapses only that exact pair for analytical continuity and records source mappings for both DOM indices while retaining the raw payload. Previously quarantined ambiguous deltas are reprocessed only when the provider returns the same archived payload hash.

The last live deployment was verified with a separate PostgreSQL database, loopback API on port 8100, and the independent Orbit collector. The current local startup path is managed by Node.js and does not launch containers; its configured local database must be running before the API becomes ready. The separately supplied Orbit credentials remain only in ignored `behaviour_detection/.env` with mode `0600`. The last verified collector state accepted reading 6833 with 120 visible DOM items; 60 were an exact duplicate copy and coalesced to 60 outcomes. It appended 1,345 live outcomes alongside the frozen Phase 1 sequence and caught its analysis worker up to position 51,468. The cross-source boundary remains `cross_source_overlap_unverified`. A later Orbit source check returned a changed, shorter payout list and did not establish continuity; the collector held its cursor as `DEGRADED` pending a verifiable overlap.

## Live-first dashboard and context

The main `/behaviour` overview now displays only Orbit connection state, the latest source outcome, rolling 50-round H/L/E statistics, the current descriptive state, recent H/L/E sequence and the dynamic next-100 outlook. It initializes directly from `OrbitWatchState.last_snapshot` instead of waiting for another worker event. The collector's newest-first order is reversed for analysis, full-precision multipliers are reclassified, item/source identities are checked, and the seed window remains distinct from accepted session deltas. Complete durable Orbit-session counts are returned separately. Orbit has no per-payout event timestamp, so the API explicitly reports that event-time gaps are unavailable.

Every worker assessment stores the live rolling-50 context, state evidence and current H/L deviation from the frozen Phase 1 reference. A cached, one-time-per-process Phase 1 scan indexes all available 50-round contexts and their following 50/100 outcomes. Matching analogue summaries are descriptive, overlapping and dependent; no recovery likelihood is emitted and the historical expanding baseline is unchanged. The API applies this same read-only comparison to the initial seed before the first new worker result arrives. No database schema or Winner Predict logic changed.

## Verification

- Winner Predict source handoff (2026-11-14): PostgreSQL `aviator_rounds` returned 32,714 rounds through index 33,148. The latest 100 Phase 1 rows matched by round ID, multiplier, and consecutive source/analysis order; sync anchored at source index 32,502 and copied the 646 newer rows. Seven rows with a source `gap_before` marker retain explicit uncertainty; the latest 50 source rows are continuity-verified. Behaviour's sync health is `HEALTHY` and reports those gaps.
- The Behaviour overview and frontend API proxy returned HTTP 200 with the latest Winner Predict round, rolling context, sequence, and timestamp provenance. The analysis worker caught up through position 50,769 after processing all 646 synced rounds. Runtime starts `api`, `worker`, and `winner-source`; it no longer starts the legacy Orbit collector. The frontend production build passed (2,558 modules).

- Phase 1 import: `already_imported` on idempotent rerun; 50,123 canonical rounds and 171,378 raw rows retained.
- Phase 1 reconciliation: passed with all counts and baseline replay above. The importer compares exported raw source bytes with their Phase 1 manifest hashes, verifies the actual consumed research files and records a content digest. Schema migrations corrected the original Phase 1 reference block positions to the one-based positions emitted by the research calculation and added incremental all-round log-volatility and tail statistics.
- Backend: full suite passed after the live-context and cursor recovery changes (`39 passed`, one existing Starlette/httpx deprecation warning), including initial Orbit seed, rolling50 classification, historical analogue evidence labels, per-new-round worker assessment updates and a held ambiguous cursor.
- Behaviour frontend API: 2 targeted tests passed; they verify the isolated versioned namespace and omitted credentials.
- Frontend production build: passed after the live overview simplification (Vite 5.4.21, 2,558 transformed modules).
- Full existing frontend test suite: 28 passed, 3 failed for unrelated current-suite inconsistencies: tests import absent `modelCandidates.js` and `panelSimulation.js`, and a legacy Rules Engine assertion expects an old label. Those existing areas were left untouched.
- Python bytecode compilation: passed.
- Private environment: `behaviour_detection/.env` is ignored and mode `0600`; its values are never printed. No ML packages are present in the Behavior runtime lockfile.
- Runtime: API liveness/readiness and database-backed health returned HTTP 200; all 14 data endpoints returned HTTP 200. Worker is `HEALTHY/IDLE` at analysis position 51,468. Orbit list/latest/history requests returned HTTP 200 and the collector recovered through cursor 6833. It has 1,345 canonical live rounds, 233 archived Orbit readings, 89 quarantined readings, and 1,413 source-item mappings. A current Orbit probe shows 120 visible items, 60 after exact duplicate-half coalescing, and a source error of `CHROME_CONNECTION_OFFLINE`; the independent collector remains running and retries.
- Live-first deployment: the overview endpoint returned HTTP 200 with a valid 60-outcome saved Orbit context, rolling50 H=20/L=29/E=1, latest outcome 1.30x LOW, 1,345 accepted session deltas, 14,229 descriptive analogue windows and no recovery probability. The baseline remains 499 blocks and 48.588 expected HIGH rounds in 100. A later Orbit source reading advanced to cursor 6842 with only 41 visible payouts and no provable overlap with saved cursor 6833. The collector preserved the last verified 60-outcome context, quarantined the first unverified reading, and reports `DEGRADED` with `CURSOR_HELD_UNVERIFIED_OVERLAP`. Fresh incoming round acceptance cannot be verified until the upstream source returns a continuous payout list.
- Local development frontend and API bind to loopback; the read-only development API proxy is not exposed to LAN clients. Collector and worker processes have independent advisory singleton locks; collector/import transactions share a round-position writer lock.
- The final Qodo code review completed with four findings. The concurrent Phase 1 import/enrichment race is fixed with one command-scoped PostgreSQL advisory lock and a regression test. The API access finding is bounded by loopback-only publishing and a documented requirement that a remote trusted reverse proxy enforce access policy. The other findings refer to pre-existing route removals outside this change and an incomplete Qodo path scope; the Phase 1 sources are present and were imported/reconciled from the actual package.
- The workspace-wide frontend suite has 28 passing tests and three existing failures: imports of absent `modelCandidates.js` and `panelSimulation.js`, plus a Rules Engine assertion for a legacy label. These failures are outside the Behaviour page; the Behaviour API tests and frontend production build pass.

## Start and operations

Run `npm run behaviour:setup`, `npm run behaviour:import-history`, `npm run behaviour:reconcile`, then `npm start`. The shared frontend and Behaviour services are managed by the Node.js supervisor; `npm run behaviour:start` starts only Behaviour services. The independent Orbit URL/key and selected watch ID are configured locally in the private environment. Use `npm run behaviour:health`, `npm run behaviour:status`, `npm run behaviour:logs` and `npm run behaviour:stop` for operations. A `CHROME_CONNECTION_OFFLINE` source health result means the Orbit API responded but its connected browser must be reconnected before new payout readings can arrive.
