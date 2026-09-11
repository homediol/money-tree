# Winner Predict

Winner Predict is a statistical pattern analysis and machine learning research platform for historical Aviator multiplier data.

The research target is:

```text
NEXT OUTCOME >= 2.00x
```

This project does not provide guaranteed predictions. Every output is labeled as `STATISTICAL PATTERN ANALYSIS` and includes probability, confidence, sample size, historical evidence, sequence similarity, and model validation context.

> The repository root also contains an older Flask-based "Aviator Prediction System"
> prototype (`backend/utils.py` era), the `bot/` collector, and the sibling
> `aviator_enterprise/` app. Only the FastAPI + React stack described below is
> the current "Winner Predict" application.

## Architecture

```text
backend/
  main.py              FastAPI entrypoint (uvicorn main:app)
  app/api/             route modules: statistics, analysis, signals, history, patterns, models
  app/services/        data loading, patterns, probabilities, similarity, signals
  app/ml/              walk-forward model training and ensemble prediction
  app/core/config.py   pydantic-settings configuration (see .env.example)
  app/database/        SQLite repository
  data/                roundhistory.json copy used by the backend
  tests/               pytest suite
frontend/
  src/pages/           dashboard, history, patterns, signals, models, settings
  src/components/      reusable cards, metrics, evidence panel
  src/hooks/           data fetching + WebSocket live refresh
```

## Quickstart (local)

### One-command stack — `npm start`

From the repo root, `npm start` launches every service under one supervisor
process (Ctrl+C stops them together):

| Service | What it is | Port |
| ------- | ---------- | ---- |
| `backend/main.py` | **Winner Predict** FastAPI API | 8000 |
| frontend (vite dev) | React dashboard | 5173 |
| flask (`backend/run.py`) | legacy prediction API | 5000 |
| bot-api (`backend/bot_api.py`) | legacy bot control API | 5001 |
| collector (`bot/`) | live round data collector | — |
| enterprise (`aviator_enterprise/`) | legacy ML API | 8002 |

Port 8000 is reserved for the Winner Predict backend. The legacy enterprise
app was moved to port 8002 so the two never collide (if an older instance of
that app is still bound to 8000, stop it first or you will see an
address-in-use error). For a minimal two-terminal setup, follow sections 1
and 2 below instead.

### 1. Backend

```bash
cd backend
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

- Interactive API docs (Swagger): <http://localhost:8000/docs>
- Live file monitor: when `backend/data/roundhistory.json` changes the app
  reloads and broadcasts `updated_analysis` over the WebSocket.

Configuration is optional — copy `backend/.env.example` to `backend/.env`
and edit if you need different paths, thresholds, or CORS origins. Defaults
point at `backend/data/`, `backend/trained_models/`, and
`backend/winner_predict.sqlite3`.

> Python 3.13 note: `requirements.txt` uses version ranges so the newest
> NumPy/Pandas/scikit-learn wheels install on 3.13. The pinned legacy versions
> (`numpy 1.24`, `pandas 2.1`) have no 3.13 wheels.

### 2. Frontend

```bash
cd frontend
npm install
npm run dev
```

The dashboard runs at <http://localhost:5173>.

The frontend talks to the backend directly at
`http://localhost:8000` (`VITE_API_BASE_URL`) and opens
`ws://localhost:8000/ws/live` (`VITE_WS_URL`). Override both at dev/build time
with environment variables if the backend runs elsewhere.

### 3. Tests

```bash
cd backend
. .venv/bin/activate
pytest tests/          # or just: pytest
```

The suite covers the API routes, data loading, pattern discovery, signal
fusion, sequence similarity, and walk-forward model validation.

## Docker Compose

```bash
docker compose up --build
```

- Backend: <http://localhost:8000> (`python:3.11-slim`)
- Frontend: <http://localhost:5173> (`node:20-alpine`)

The frontend container mounts an anonymous `/app/frontend/node_modules`
volume so host node_modules (built against glibc) never leak into the musl
alpine image. Dependencies are installed inside the containers on every `up`.

> Port note: `npm start` runs the legacy `aviator_enterprise` app on port
> 8002, so it no longer collides with this stack. If an older instance is
> still bound to host port 8000, stop it before `docker compose up`.

## API

| Method | Path                      | Purpose                                   |
| ------ | ------------------------- | ----------------------------------------- |
| GET    | `/api/status`             | Backend health and dataset summary        |
| GET    | `/api/statistics`         | Dataset statistics                        |
| GET    | `/api/analysis/current`   | Current full analysis payload             |
| GET    | `/api/signal/current`     | Current composite signal                  |
| GET    | `/api/signal/history`     | Stored signal history                     |
| GET    | `/api/history`            | Recent rounds (`?limit=N`)                |
| GET    | `/api/patterns`           | Discovered patterns                       |
| GET    | `/api/patterns/{id}`      | Single pattern detail                     |
| GET    | `/api/models`             | Saved model runs                          |
| GET    | `/api/models/performance` | Latest validated model performance        |
| POST   | `/api/models/train`       | Run walk-forward training + validation    |
| POST   | `/api/analysis/recalculate` | Force analysis recalc                   |
| WS     | `/ws/live`                | Live updates (`updated_analysis`, ...)    |

## Dataset

The backend uses `backend/data/roundhistory.json`. The loader detects the JSON
shape before parsing. The current dataset is an array of round records:

```json
{
  "multiplier": 1.07,
  "timestamp": "2026-07-28T13:37:37.587Z",
  "round_index": 434
}
```

On load it reports total records, valid records, invalid records, duplicates
removed, first/last timestamps, and first/last round indexes.

## How Analysis Works

Winner Predict calculates the historical base rate for rounds `>= 2.00x`, then
discovers patterns directly from the round history:

- low streaks from 1 through 6+ rounds below 2x
- high streaks
- low, medium, and high volatility windows
- similar recent sequences using normalized Euclidean distance, cosine
  similarity, and DTW

The final signal combines pattern probability, sequence matching probability,
machine learning probability, historical base rate, sample-size protection,
volatility, and confidence checks.

Patterns with fewer than the configured minimum examples (default `30`) are
marked `INSUFFICIENT DATA`.

## Model Validation

The ML system trains Logistic Regression, Random Forest, and Gradient Boosting
models when scikit-learn is installed. Validation uses chronological
walk-forward splits only — the model never shuffles rounds and never trains on
future data when evaluating earlier rounds.

Models are compared against the historical base-rate probability, `always >= 2x`,
`always < 2x`, and random 50/50 baselines. If validation does not beat the
baseline, the UI and API return `MODEL NOT VALIDATED`.

Trained artifacts are written to `backend/trained_models/`; metrics and signal
history persist in `backend/winner_predict.sqlite3`.

## Limitations

Aviator multiplier sequences may be random or adversarially generated.
Historical relationships can disappear. Small samples are unreliable. The
project is designed for transparent research and monitoring, not guaranteed
outcome prediction.


