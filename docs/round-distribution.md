# External live round distribution

Winner Predict's active collector remains the only browser/page owner. For each completed round it fsyncs a local JSONL outbox record, posts `round.observed` to the API for immediate authenticated SSE fanout, and persists to PostgreSQL on a background worker. After PostgreSQL confirms the row, it hands off `round.persisted`. External projects use read-only tokens and never receive login/session material or betting controls.

The active backend is FastAPI on port 8000. The collector and API are separate processes connected by authenticated HTTP POSTs to `/api/collector/round-observed` and `/api/collector/round-persisted`; an in-process event emitter is not used across that boundary. The POST handlers require the configured service `API_KEY`. The first handler appends to a local SQLite inbox using WAL and `synchronous=FULL`, then publishes to SSE without waiting for PostgreSQL. PostgreSQL history and committed replay remain authoritative.

The collector's durable event journal is `data/bot/round-event-outbox.json` (JSONL, fsync per append). The API inbox defaults to `data/backend/round-observation-inbox.sqlite3`, with a 50,000 pending-row and 64 MiB limit. Set `COLLECTOR_OBSERVATION_SPOOL_PATH`, `COLLECTOR_OBSERVATION_SPOOL_MAX_RECORDS`, or `COLLECTOR_OBSERVATION_SPOOL_MAX_BYTES` to change it. The collector outbox is bounded by `COLLECTOR_OUTBOX_MAX_RECORDS` and `COLLECTOR_OUTBOX_MAX_BYTES`; the append journal has a 2x byte safety limit and compacts as records progress. Keep both spool locations on persistent local storage. A full or failed spool returns/logs an explicit error and retains upstream records where possible; it never silently drops a round.

## Start the existing services

Set `DATABASE_URL`, `REQUIRE_POSTGRES=true`, and a random `API_KEY` of at least 16 characters in `backend/.env`. Keep the API and collector on the same host or configure both collector endpoint URLs for the API's reachable internal HTTPS address. `scripts/start-all.js` and `bot/collector/lock-runner.js` load `backend/.env` for their child processes.

From the repository root, use separate terminals:

```sh
npm run backend
```

```sh
npm run collector
```

The collector lock runner enforces the existing single collector/browser owner. Do not start a second collector or browser from the API. `npm run backend` is API-only and does not create a collector. For the sibling Node receiver, set `ROUND_API_URL=https://<winner-predict-host>` and `ROUND_API_TOKEN=<read-only-project-token>` in its private `.env`, then run `npm start` from that project. Loopback development may use `ROUND_API_URL=http://127.0.0.1:8000`.

## Create and revoke project tokens

Set a strong `API_KEY` for operator-only token management. The management endpoints require that key even when the dashboard is configured for public read access. The generated project token is shown once; PostgreSQL stores only its SHA-256 hash.

```sh
curl -X POST 'https://<winner-predict-host>/api/v1/projects/<project-id>/tokens' \
  -H 'Authorization: Bearer <operator-API_KEY>' \
  -H 'Content-Type: application/json' \
  -d '{"label":"analytics consumer"}'

curl -X DELETE 'https://<winner-predict-host>/api/v1/projects/<project-id>/tokens/<token-id>' \
  -H 'Authorization: Bearer <operator-API_KEY>'
```

Store the returned `token` in the consumer's secret manager. Never put it in a URL. Revocation closes active SSE connections and prevents future REST requests. External APIs require `Authorization: Bearer <project-token>`; their tokens grant reads only.

Browser origins are denied unless explicitly listed in `ROUND_STREAM_ALLOWED_ORIGINS`, for example the JSON value `["https://consumer.example"]`. That list only controls browser origins; it is not authentication. Server-to-server consumers may omit `Origin`. Remote plain HTTP requests receive `426 https_required_for_external_access`; expose the service externally only behind HTTPS/WSS-capable TLS termination configured to forward the HTTPS scheme. This code change does not open a firewall or publish the service.

## Read committed rounds

All endpoints below are read-only and require a project token.

```http
GET /api/v1/rounds/latest
GET /api/v1/rounds?after_sequence=123&stream_id=<stream-id>&limit=500
GET /api/v1/collector/status
GET /api/v1/rounds/stream
```

`limit` is clamped to 1–500. Pages are ordered by the durable `stream_sequence`; `next_cursor` is the last returned sequence, and `has_more` indicates another page. Always resume with both `stream_id` and `next_cursor`. A cursor from a different stream, before retention, or ahead of the stream returns HTTP 409 with `error: resync_required`, a reason, the current retention boundaries, and `resync_after_sequence`. Do not advance past that response without deliberately resynchronizing.

Committed events are retained up to `ROUND_STREAM_RETENTION` rows (default 100,000; bounded to 1,000–1,000,000). Old committed events are pruned in stream order; a client older than the retained boundary must resync. The stream ID is created with the PostgreSQL distribution metadata and remains stable across normal restarts. Recreating that metadata starts a new stream ID and invalidates old cursors. `stream_sequence` uses PostgreSQL's durable identity generator and increases across streams; it can have gaps after transaction rollbacks and retention pruning. Recreating the event table also recreates that generator. Clients use `next_cursor`, not an assumption that every integer exists.

The event contract is versioned. A committed example:

```json
{
  "schema_version": "1.0",
  "event_id": "aviator-round:<round-id>:persisted",
  "event_type": "round.persisted",
  "stage": "PERSISTED",
  "stream_id": "<stream-id>",
  "stream_sequence": 124,
  "committed_sequence": 124,
  "observation_id": "<stable-observation-id>",
  "round_id": "<round-id>",
  "identity_source": "COLLECTOR_OBSERVATION_HASH",
  "multiplier": 2.14,
  "platform_timestamp": null,
  "observed_at": "2026-10-09T12:48:05.839Z",
  "stored_at": "2026-10-09T12:48:06.539Z",
  "published_at": "2026-10-09T12:48:06.552Z",
  "durability": "COMMITTED",
  "continuity_status": "VERIFIED",
  "delivery_mode": "LIVE",
  "round_index": 23291,
  "platform_round_id": null,
  "platform_round_index": null
}
```

Before commit, the corresponding pending event uses the same `stream_id`, `observation_id`, and `round_id`, but its own event ID and null commit fields:

```json
{
  "schema_version": "1.0",
  "event_id": "aviator-round:<round-id>:observed",
  "event_type": "round.observed",
  "stage": "OBSERVED",
  "stream_id": "<stream-id>",
  "stream_sequence": null,
  "committed_sequence": null,
  "observation_id": "<stable-observation-id>",
  "round_id": "<round-id>",
  "identity_source": "UNKNOWN",
  "multiplier": 2.14,
  "platform_timestamp": null,
  "observed_at": "2026-10-09T12:48:05.839Z",
  "stored_at": null,
  "published_at": "2026-10-09T12:48:05.852Z",
  "durability": "PENDING",
  "continuity_status": "UNVERIFIED",
  "delivery_mode": "LIVE"
}
```

`round.observed` and `round.persisted` use the same `observation_id` and `round_id`, but distinct event IDs (`aviator-round:<round-id>:observed` and `aviator-round:<round-id>:persisted`). Both include `schema_version`, `stream_id`, `identity_source`, multiplier and timestamps; unavailable platform fields are null. An observed event has `durability: "PENDING"`, null `committed_sequence` and `stored_at`, and no `stream_sequence`. It is appended durably to the API inbox before fanout. PostgreSQL has not yet confirmed it.

The background collector worker reuses its PostgreSQL pool, persists parameterized/idempotent batches with bounded concurrency, and retries with capped backoff. A persisted event is sent only after commit and exact canonical round ID/multiplier confirmation. Its durable PostgreSQL replay row carries a distinct event ID and `committed_sequence`; the collector keeps its local outbox record until this committed-event handoff is acknowledged. This covers retries and process crashes after commit but before publication without claiming exactly-once delivery.

Committed history (`/rounds`, `/rounds/latest`) remains PostgreSQL-only. Pending observations use a separate cursor: `GET /api/v1/rounds/pending?after_sequence=<pending-cursor>` or the `pending_after_sequence` stream query. Reconnect with both the committed `Last-Event-ID: <stream-id>:<committed-sequence>` and the pending cursor. Pending events never advance the committed cursor. Delivery is at least once; consumers deduplicate by `observation_id` and update one logical round record as it moves from PENDING to COMMITTED. Never use multiplier equality as identity: equal consecutive multipliers are separate rounds.

After backend downtime, pending replay sets `delivery_mode: "RECOVERED"`. Collector records restored from its local outbox are also marked `RECOVERED`; they must not be treated as new prospective decisions. The API inbox retains committed tombstones for bounded duplicate suppression. If a pending row was already pruned from the API inbox after commit, it is available through committed PostgreSQL REST history.

`round_id` can be collector-generated. `identity_source` is `PLATFORM_ROUND_ID`, `PLATFORM_ROUND_INDEX`, `COLLECTOR_OBSERVATION_HASH`, `LOCAL_SEQUENCE`, or `UNKNOWN`; a missing platform identity remains null and is never relabeled as a platform ID. Unknown continuity and gaps remain distinct: `continuity_status` is `VERIFIED`, `GAP_BEFORE`, or `UNVERIFIED`.

## SSE subscription and recovery

Use `fetch` or another HTTP streaming client that can set an Authorization header. Browser `EventSource` cannot set a bearer header, so do not place the token in the stream URL. Send the committed resume cursor in `Last-Event-ID: <stream_id>:<sequence>` and the pending resume cursor as `pending_after_sequence`. A first subscription without a cursor starts after the captured current committed boundary; use REST first to import retained history. The server reserves the subscriber before reading the committed snapshot, flushes SSE headers immediately, replays committed rows through that boundary, then replays the durable pending inbox and drains its live queue. This protects the replay/live boundary for both stages.

Events are named `round.observed`, `round.persisted`, `stream.ready`, `stream.replay_complete`, `heartbeat`, `stream.overflow`, `auth.revoked`, and `server.shutdown`. Responses use `text/event-stream`, terminate each frame with a blank line, disable cache/compression/proxy buffering, and send an initial comment before database snapshot work. Heartbeats run every 15 seconds and disconnect cleanup releases subscriptions. Each connection has a bounded queue of 256 events, each token can hold at most four streams, and the backend caps all external streams at 200. A slow client is disconnected with `stream.overflow`; it cannot block collection or other clients. Reconnect with both cursors and catch up through REST. Delivery is at least once, not exactly once. Clients must be idempotent.

## Status and latency

`GET /api/v1/collector/status` reports outbox/backlog size, oldest pending observation age, persistence lag, collector delivery failures, queue overflow, connected clients, and available stage latency summaries (p50, p95, maximum, sample count). The measured stages are local append, append-to-API receipt estimate, API receipt-to-SSE publication, publication-to-receiver estimate, and collector-monotonic observation-to-PostgreSQL commit. The first two and commit samples reset when the collector restarts; backend fanout samples reset when the backend restarts; receiver delivery samples remain in receiver state. Cross-machine delivery includes NTP midpoint clock offset uncertainty. Do not combine different measurement clocks or treat a one-way RTT estimate as exact. Metrics remain null when there are no samples. `round.observed` carries no game-open guarantee: preserve `game_phase` when supplied, and report timing as unknown when no verified deadline is available.

The focused local tests use delayed in-memory database doubles and loopback API fixtures only. They do not measure live cross-process p95. Measure the service's own status samples under healthy operation before claiming the sub-second target.

## Node.js receiver example

Node 18 or newer, with no added dependency:

```sh
WINNER_PREDICT_URL='https://<winner-predict-host>' \
WINNER_PREDICT_ROUND_TOKEN='<project-token>' \
node examples/round-distribution-client.mjs
```

The sample imports retained committed pages, subscribes with a resumable cursor, reconnects with REST catch-up, and upserts each stage in memory by stable event identity. Production consumers should persist their own `round_id`/`event_id` idempotency key with their business update because delivery is at least once.
