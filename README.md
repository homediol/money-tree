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

From the repo root, `npm start` (or `./start.sh`) launches every service under
one supervisor process. The supervisor validates that port 8000 belongs to
Winner Predict, restarts the backend with capped exponential backoff after an
unexpected exit, and forwards Ctrl+C/SIGTERM for graceful shutdown.

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
npm start
```

Equivalently, from the repository root run `npm run backend`.

For intentional hot-reload development only, use
`cd backend && npm run dev`. Normal startup does not use Uvicorn's extra
reloader process.

- Interactive API docs (Swagger): <http://localhost:8000/docs>
- Process/database health: <http://localhost:8000/health>
- Live file monitor: when `backend/data/roundhistory.json` changes the app
  reloads and broadcasts `updated_analysis` over the WebSocket.

Configuration is optional — copy `backend/.env.example` to `backend/.env`
and edit if you need different paths, thresholds, or CORS origins. Defaults
point at `data/roundhistory.json`, `backend/trained_models/`, and
`backend/winner_predict.sqlite3`.

For any deployment reachable by other machines, set `API_KEY` to a random
value of at least 16 characters. The backend will then require that value as a
Bearer token for every `/api/*` endpoint and for `/ws/live`. Enter the same
value on the dashboard's Settings page; it is kept only in session storage.
Keep `CORS_ORIGINS` restricted to the exact dashboard origins.

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

In development the frontend uses same-origin `/api`, `/health`, and `/ws`
routes, which Vite proxies to `BACKEND_PROXY_TARGET` (default
`http://127.0.0.1:8000`). This avoids CORS and hostname mismatches. Set
`VITE_API_BASE_URL` and `VITE_WS_URL` only when the browser must connect to a
separate backend origin.

If the backend starts late or restarts, the dashboard shows a compact
`Reconnecting…` indicator and probes it with capped exponential backoff. The
active page reloads its data automatically after recovery; no manual Retry or
browser refresh is needed.

### Browser profile fallback

The collector normally uses `data/bot/chrome-profile-new-email`. If that
profile cannot navigate to Aviator, it retries with the last-used Google Chrome
profile under `~/.config/google-chrome`. Set `BOT_GOOGLE_PROFILE` (for example,
`Profile 5`) to select a specific profile. When normal Chrome already owns that
profile, either close Chrome before starting the collector or launch Chrome
with remote debugging enabled on `BOT_CDP_PORT` (default `9222`); the collector
will attach to it and will not remove a live profile lock.

When the selected Google profile is already open without remote debugging, the
collector creates an isolated snapshot at `data/bot/chrome-google-fallback`
containing only authentication/application state. It never removes the live
profile lock or launches a second process against the active profile.

If the dedicated profile cannot open Aviator, or opens the URL without a game
iframe for `BOT_INITIAL_FRAME_TIMEOUT` (default 30 seconds), the collector
launches a fresh incognito-style Chrome context. It visits `https://winner.rw/`
first, signs in using the configured collector credentials, and then opens the
direct Aviator route. This avoids live-profile locks and stale browser state.

### 3. Tests

```bash
cd backend
. .venv/bin/activate
pytest tests/          # or just: pytest
```

The included root `pytest.ini` also makes `pytest` work from the repository
root when the backend dependencies are installed in the active environment.

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

The current FastAPI backend uses `data/roundhistory.json`. The loader detects the JSON
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

From the project root, with `backend/.venv` installed:

```bash
PYTHONPATH=backend backend/.venv/bin/python backend/scripts/next_round_ml.py train
PYTHONPATH=backend backend/.venv/bin/python backend/scripts/next_round_ml.py predict
PYTHONPATH=backend backend/.venv/bin/python -m pytest backend/tests/test_models.py backend/tests/test_dataset_service.py -q
```

The supervised label for row N is its own `>=2x` outcome; all its features
use only rounds before N. For live inference, the unknown next row is a
placeholder with no outcome. Features include multiple rolling distributions,
robust log statistics, streaks, transition rates, and causally smoothed Pattern
Engine state frequencies. A collector gap cannot be treated as a known
immediately following round. The feature schema is versioned and rejects
unapproved columns.

Candidates are Logistic Regression, Random Forest, Extra Trees, histogram
Gradient Boosting, and optionally XGBoost/LightGBM when installed. The fixed
70/10/5/15 chronological splits are for fitting, selection, calibration
checking, and an untouched final test. Three earlier expanding walk-forward
folds are also evaluated. Model choice uses only the earlier folds and
selection set; the final test can reject deployment but cannot choose a model.
Class weighting is enabled when the fitting period is substantially imbalanced.
A sigmoid calibrator is fitted on selection predictions only when a separate
holdout shows an improvement in both Brier and log loss. Diagnostics include
calibration bins, PR/ROC-AUC, precision/recall/F1, log loss, Brier, confusion
matrices, three test-period checks, and block-bootstrap Brier advantage against
simple frozen/causal/rolling frequency baselines.

If no candidate demonstrates a stable out-of-sample advantage, `/api/ml/estimate`
returns an informational, **non-usable** 250-round frequency. No unvalidated
ML output enters the Evidence → Decision → Risk path. Even a validated ML
prediction only supplies evidence; it never places or sizes a bet. Predictions
are blocked when history is stale or the latest contiguous history is too short.

Training snapshots are archived by SHA-256 under `backend/artifacts/training_datasets/`.
Versioned model artifacts and complete evaluation reports are stored under
`backend/trained_models/`; the latest version is referenced by `active.json`.
Do not load model artifacts from untrusted sources (joblib uses pickle).

## Limitations

Aviator multiplier sequences may be random or adversarially generated.
Historical relationships can disappear. Small samples are unreliable. The
project is designed for transparent research and monitoring, not guaranteed
outcome prediction.
