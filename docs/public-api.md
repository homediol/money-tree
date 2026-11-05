# Public API guide

Winner Predict exposes three anonymous, read-only integration routes: committed Aviator round reads and the action-only latest-signal read. They do not return pending observations, account data, betting records, or control operations. No API key, token, or authorization header is required.

## Base URLs and documentation

- Production: `https://<your-winner-predict-domain>`
- Local development: `http://127.0.0.1:8000`
- Swagger UI: `https://<your-winner-predict-domain>/docs`
- OpenAPI JSON: `https://<your-winner-predict-domain>/openapi.json`

Replace the production host with the hostname configured for your deployment. Remote HTTP requests are rejected; loopback HTTP is allowed for development.

The backend also exposes unauthenticated operational/documentation reads: `GET /`, `GET /health`, `GET /health/live`, and `GET /health/ready`, plus the Swagger UI and OpenAPI schema above. They report service/readiness metadata only; the system status, metrics, history, and business-data APIs under `/api/` remain private unless listed below or protected by a scoped integration token.

## Public endpoints

### `GET /api/v1/signals/latest/action`

For a published action, this read-only public endpoint returns exactly `action` and `index`. It does not require an API key or consumer token and does not return signal, decision, round, or session identifiers, timestamps, matched rules, or other metadata. With no eligible published action, it returns the same no-data response as `/api/v1/signals/latest`.

Full URL: `https://<your-winner-predict-domain>/api/v1/signals/latest/action`

When a canonical signal with a `BET` decision has been published, the response is:

```json
{
  "action": "OK",
  "index": "#20063"
}
```

`OK` reflects the published positive decision. It is not a guarantee that the Aviator round will win. If the canonical publishing pipeline publishes a genuine `WAIT`, it remains `WAIT`:

```json
{
  "action": "WAIT",
  "index": "#20064"
}
```

This endpoint is a read-only projection of the same canonical `BetSignalHub.list_signals` feed used by `GET /api/v1/signals/latest` and `GET /api/v1/signals`. A published `BET` maps to `OK`; a genuine canonical published `WAIT` remains `WAIT`. The action is not recalculated from current Rules Engine or collector state. `index` is formatted from that signal's actual `target_round_index`; the source index, timestamp, and database ID are never substituted. The feed preserves repository newest-first order using the server-side publication timestamp written with the durable signal insert; publisher-supplied decision timestamps do not reorder new signals. Existing historical timestamps are not rewritten. If no eligible canonical action is available, the endpoint uses the existing latest-signal no-data response: `{"ok":true,"signal":null}`. The current publisher accepts `BET` decisions and does not publish `WAIT` decisions. Polling does not publish or mutate signals. The endpoint keeps its existing anonymous public-read access policy, transport/origin controls, and 120 requests per minute per source IP limit; the detailed signal endpoints remain scoped to `signals:read` consumers.

Working JavaScript, TypeScript, Python, PHP, Java, C#, and cURL examples are in [Signal action integrations](signal-action-integration.md). Each example reads only `action` and `index`.

### `GET /api/v1/rounds/latest`

Returns the latest committed round and retention metadata. No query parameters or request body are required. If no round has been committed yet, `round` is `null`.

Full URL: `https://<your-winner-predict-domain>/api/v1/rounds/latest`

Example response (values are illustrative; the endpoint returns database records):

```json
{
  "ok": true,
  "stream_id": "<stream-id>",
  "project_id": null,
  "round": {
    "event_type": "round.persisted",
    "stage": "PERSISTED",
    "stream_sequence": 124,
    "round_id": "<round-id>",
    "round_index": 23291,
    "multiplier": 2.14,
    "durability": "COMMITTED"
  },
  "retention": {
    "earliest_sequence": 1,
    "latest_sequence": 124,
    "retained_events": 124
  }
}
```

### `GET /api/v1/rounds`

Lists committed round events in pages. The default order is ascending by `stream_sequence`; set `order=desc` for newest-first pages.

Full URL: `https://<your-winner-predict-domain>/api/v1/rounds`

| Parameter | Type | Meaning |
|---|---|---|
| `after_sequence` | integer, default `0` | Ascending cursor. Use `next_cursor` from the prior page. |
| `before_sequence` | integer | Descending cursor. Use `next_cursor` from the prior descending page. |
| `stream_id` | string | Required when resuming a page; prevents using a cursor against a different stream. |
| `limit` | integer, 1–500, default `500` | Maximum records returned. |
| `search` | string, 1–128 characters | Case-insensitive substring search in `round_id`. |
| `min_multiplier` | number greater than 0 | Inclusive minimum multiplier. |
| `max_multiplier` | number greater than 0 | Inclusive maximum multiplier. |
| `order` | `asc` or `desc`, default `asc` | Sort by durable stream sequence. |

Use only `after_sequence` with `order=asc`, or `before_sequence` with `order=desc`. When `has_more` is true, send the returned `next_cursor`, `stream_id`, filters, and order in the next request. If a cursor is outside retained history or belongs to another stream, the API returns `409 resync_required` with the current retention boundaries.

Example request:

```http
GET /api/v1/rounds?after_sequence=123&stream_id=<stream-id>&limit=100&min_multiplier=1.2&max_multiplier=5
```

Example response (record values are illustrative):

```json
{
  "ok": true,
  "project_id": null,
  "stream_id": "<stream-id>",
  "rounds": [
    {
      "event_type": "round.persisted",
      "stage": "PERSISTED",
      "stream_sequence": 124,
      "round_id": "<round-id>",
      "round_index": 23291,
      "multiplier": 2.14,
      "durability": "COMMITTED"
    }
  ],
  "next_cursor": 124,
  "cursor_direction": "after",
  "has_more": false,
  "retention": {
    "earliest_sequence": 1,
    "latest_sequence": 124,
    "retained_events": 124
  }
}
```

## Status codes and errors

- `200`: the request succeeded, including an empty result set.
- `401`: an optional recognized consumer token was supplied but is invalid or revoked. Omit the token to use anonymous public access.
- `403`: the supplied browser `Origin` is not configured.
- `409`: a stream cursor needs resynchronization.
- `422`: invalid query parameter or incompatible cursor parameters.
- `426`: a non-loopback caller used HTTP instead of HTTPS.
- `429`: the source IP exceeded 120 requests per minute; retry after the `Retry-After` value.
- `503`: PostgreSQL or the committed round stream is unavailable.

Public route errors use a JSON envelope such as `{"ok":false,"error":"validation_error","details":[]}`. Responses include `X-Request-ID` for support and log correlation.

## Integration examples

All examples call the public routes without an API key.

### JavaScript `fetch`

```js
const response = await fetch(
  "https://<your-winner-predict-domain>/api/v1/rounds/latest"
);
if (!response.ok) throw new Error(`Winner Predict returned ${response.status}`);
const data = await response.json();
console.log(data.round);
```

### Axios

```js
import axios from "axios";

const { data } = await axios.get(
  "https://<your-winner-predict-domain>/api/v1/rounds",
  { params: { after_sequence: 0, limit: 100, min_multiplier: 1.5 } }
);
console.log(data.rounds, data.next_cursor);
```

### Python Requests

```python
import requests

response = requests.get(
    "https://<your-winner-predict-domain>/api/v1/rounds/latest",
    timeout=10,
)
response.raise_for_status()
print(response.json()["round"])
```

### cURL

```sh
curl --fail-with-body \
  'https://<your-winner-predict-domain>/api/v1/rounds?limit=100&order=desc'
```

## CORS for browser integrations

Server-to-server clients such as Python, PHP, Node.js, Android, and iOS can call the HTTPS URLs directly. Browser apps must add their exact origin (scheme, host, and port) to `ROUND_STREAM_ALLOWED_ORIGINS` or `CORS_ORIGINS`, for example:

```dotenv
ROUND_STREAM_ALLOWED_ORIGINS=["https://consumer.example"]
```

CORS controls whether browsers can read a response; it does not authenticate a caller. The anonymous routes are read-only and still apply HTTPS, origin validation, bounded queries, and IP rate limiting.

## Private and scoped routes

No anonymous write endpoint is enabled. Private dashboard, customer, betting, history-control, rules, session, recovery, backup, restore, and operational APIs require the owner `API_KEY` in `Authorization: Bearer <API_KEY>`. The legacy `PUBLIC_ACCESS` and `PUBLIC_READ_ONLY` settings are ignored and cannot disable that protection.

External pending-round, collector-status, and event-stream reads require an owner-managed read-only consumer token. Full signal feeds and selected-rule routes require the corresponding scoped consumer token; the action-only latest-signal route is public. Signal publishing and publisher heartbeats require a publisher credential with `signals:write`. These credentials are never placed in URLs. See [round distribution](round-distribution.md) for token management and resumable streaming.

When `API_KEY` is not configured, remote private API requests fail closed with `503 api_key_not_configured`. Local loopback development remains available without a key unless one is configured.

The separate bot-control service listens on port `5001`. `GET /` is a minimal health response. `POST /bot/start`, `POST /bot/stop`, `POST /bot/manual-aviator`, `GET /bot/status`, and `GET /bot/logs` remain private because they can submit account credentials, control automation, or expose process logs. They require the owner bearer key for remote use, enforce HTTPS and origin allowlists, and apply per-IP rate limits. The older root `server.js` controller is not started by `npm start`; its `/bot/*` actions are also protected if launched manually.
