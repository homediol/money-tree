# Winner Predict backend

The active backend is the FastAPI service in `main.py`. It serves PostgreSQL
round history, SHADOW Rules Engine sessions, signal APIs, and WebSocket updates.
It does not execute bets, load prediction models, or start training jobs.

Configure `backend/.env` from `.env.example`, install `requirements.txt`, and
run `python -m uvicorn main:app --host 0.0.0.0 --port 8000` from this directory.
Production startup requires the authoritative PostgreSQL database.

The versioned API is documented at `/docs` (`/openapi.json` for the schema).
`GET /api/v1/rounds/latest` and `GET /api/v1/rounds` are anonymous, read-only
integration endpoints; they use PostgreSQL committed-round data and are limited
to 120 requests per minute per source IP. Remote private APIs require the owner
`API_KEY`, except explicitly scoped round/signal integration routes, which
validate their consumer or publisher bearer token. A dashboard opened from an
allowed localhost origin receives a short-lived HttpOnly browser session; the
owner key itself is never returned to frontend code. `PUBLIC_ACCESS` and
`PUBLIC_READ_ONLY` are retained only for configuration compatibility and do not
disable authentication. See [the public API guide](../docs/public-api.md) for
URLs, response formats, pagination, CORS, and client examples.

`/health/live` checks process liveness and `/health/ready` checks
application/database readiness. Consumer and publisher credentials are
owner-managed under `/api/v1/keys`; only hashes are persisted. Publishers use
`signals:write` and report liveness through `POST /api/v1/publisher/heartbeat`.
The detailed signal routes distribute authoritative Rules Engine signals and do not execute bets. The public `GET /api/v1/signals/latest/action` route projects the same latest canonical published signal as `/api/v1/signals/latest` and `/api/v1/signals`: `BET` maps to `OK`, and the actual `target_round_index` is formatted as `#<round number>`. With no eligible published action it returns the same no-data response as `/latest`, `{"ok":true,"signal":null}`; it does not generate `WAIT` or read the collector/Rules Engine while polling. See the [public API guide](../docs/public-api.md) and [language examples](../docs/signal-action-integration.md).
