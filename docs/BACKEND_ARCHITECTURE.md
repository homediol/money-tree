# Backend reliability architecture

## Minimal migration

Winner Predict already has a FastAPI API, PostgreSQL connection pool, durable
round observation inbox, bounded ordered round queues, dedicated thread pools,
and a Node process supervisor. This change keeps those components and adds
admission control and clearer health boundaries. Redis or a second queue service
is not needed while PostgreSQL is the durable source and the API runs as one
process.

```text
React / Vite
  -> FastAPI (rate + concurrency admission)
      -> existing durable PostgreSQL inbox
      -> bounded ordered SHADOW round queue
      -> isolated analytics executors (including ADIE)
      -> PostgreSQL pool
  -> start-all.js supervises API restart with bounded exponential backoff
```

## Request handling

- API requests are limited to 600 per minute per credential or client address by
  default. Rate-limit state has a configurable 4,096-client bound. Public round
  read routes retain their stricter existing token/IP limits.
- In-flight API work is limited to 64 requests by default. A full limit returns
  HTTP 503 with `Retry-After: 1`; a rate limit returns HTTP 429 with its retry
  interval. Uvicorn also limits accepted concurrent connections to 256 by
  default. Tune with `WINNER_API_MAX_IN_FLIGHT`,
  `WINNER_API_REQUESTS_PER_MINUTE`, `WINNER_API_RATE_LIMIT_CLIENTS`, and
  `WINNER_UVICORN_LIMIT_CONCURRENCY`.
- Health probes and collector round requests keep their existing routes and
  contracts. Collector persistence remains idempotent and durable; full queues
  return retryable 503 responses instead of retaining unbounded work in RAM.
- Successful requests under 500 ms are no longer written individually to the
  application log. Errors, admission rejections, and slow requests remain
  visible with request IDs.

## Work and persistence

- Verified observed and committed rounds continue through the existing durable
  inbox and ordered SHADOW queue. PostgreSQL remains the recovery authority.
- ADIE work is submitted to its existing single-thread executor without holding
  the ordered SHADOW worker. If the target round has already committed by the
  time that worker runs, ADIE records context but cannot lock a late forecast.
- Rules Engine evaluation and persistence remain ordered and preserve the
  current signal/SHADOW behavior. Analytics and history utilities retain their
  dedicated executors and bounded queues.
- The existing psycopg connection pool remains configurable through
  `WINNER_DB_POOL_MIN_SIZE`, `WINNER_DB_POOL_MAX_SIZE`, and
  `WINNER_DB_POOL_ACQUIRE_TIMEOUT_SECONDS`.

## Health and monitoring

- `/health/live` reports only whether the API process can answer.
- `/health` is the supervisor/UI probe. It reads the latest database heartbeat
  from in-memory health state and returns compact process metadata; it does not
  query PostgreSQL or load lifecycle history on every 10-second UI poll.
- `/health/ready` performs a direct PostgreSQL `SELECT 1` in a worker thread.
  `/api/health` and `/api/health/detailed` also run refresh work in a thread.
- Full lifecycle history remains available from `/api/backend/stability`, and
  that endpoint performs its database and Rules Engine reads outside the ASGI
  event loop.
- `/api/metrics` reports current process RSS/CPU/thread count, recent bounded API
  latency percentiles and failure counts, admission status, round queue depth,
  and database pool wait statistics.

## Recovery and limits

`scripts/start-all.js` remains the process owner. It restarts a crashed backend
with bounded exponential backoff capped at 30 seconds. Graceful shutdown and
durable round replay remain in place. The API admission controller is
process-local, so the service should remain a single Uvicorn worker unless a
future deployment adds shared rate-limit and coordination state. The backend
continues to depend on host CPU, memory, storage, and PostgreSQL availability;
application backpressure cannot repair a host-wide disk or memory stall.

## Verification

Focused checks:

```sh
backend/.venv/bin/python -m pytest backend/tests/test_request_admission.py backend/tests/test_backend_health_hot_path.py
backend/.venv/bin/python -m pytest backend/tests/test_backend_stability.py backend/tests/test_latency_hot_path.py
cd frontend && node --test tests/backendConnection.test.js tests/reconnectPolicy.test.js
node --check scripts/start-all.js
```

For a runtime load check, issue concurrent requests to `/api/metrics` while
watching `/health/live` and `/health/ready`. Confirm overload returns 503, rate
exhaustion returns 429, the process stays responsive, and collector rounds
continue to replay in order. Record host CPU, RSS, I/O pressure, request p95,
queue depth, and database pool waits; do not infer stable capacity from unit
tests or a single health response.

### Runtime snapshot during this change

At `2026-11-16T09:33+02:00`, local `/health/live` could not connect. The
supervised backend PID was in Linux uninterruptible `D` state at
`submit_bio_wait`, and the collector was in `jbd2_log_wait_commit`. Host I/O
pressure reported `some avg10=98.34%`; `/home` was 81% full (37 GB free), so
the evidence points to a storage-latency stall rather than a full filesystem.
A graceful signal was sent only to the verified backend child; it did not exit
while blocked in kernel I/O, and its instance lock remained held. No second
backend was started. As a result, live recovery and end-to-end load measurements
could not be completed safely in this environment. Re-run those checks after
the kernel I/O stall clears; the unit tests below validate admission behavior,
not production capacity.
