# Aviator Prediction System

Full-stack Aviator multiplier prediction demo:

- Python Flask API
- TensorFlow/Keras LSTM classifier
- NumPy and Pandas preprocessing
- React, Tailwind CSS, Axios dashboard
- `roundhistory.json` input and `decisions.json` output

This is an ML engineering project, not a guarantee of future gambling outcomes. Crash-style games are high variance and can be non-predictive depending on the source system.

## Project Structure

```text
backend/
  app.py
  model.py
  trainer.py
  utils.py
  requirements.txt
frontend/
  src/api/
  src/components/
  src/pages/
  src/styles/
data/
  roundhistory.json
bot/
  playwright_bot.py
decisions.json
```

## Backend Setup

```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate
python app.py
```

API runs at `http://localhost:5000`.

If Flask is missing on your machine, install the lightweight Python 3.13 dependencies inside a virtual environment:

```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate
pip install Flask gunicorn
python app.py
```

For Python versions with TensorFlow wheel support, install the LSTM stack instead:

```bash
pip install -r requirements.txt
```

Endpoints:

- `POST /train`
- `GET /predict`
- `GET /history`
- `GET /accuracy`
- `POST /retrain`
- `GET /decisions`

Example prediction response:

```json
{
  "prediction": "MEDIUM",
  "confidence": 95.4,
  "recommended_cashout": 2.1,
  "risk_level": "LOW"
}
```

## Frontend Setup

```bash
cd frontend
npm install
npm run dev
```

Dashboard runs at `http://localhost:5173` and proxies API calls to Flask.

## Data Format

Edit `data/roundhistory.json` with live or exported rounds:

```json
[
  { "round_id": 1, "multiplier": 1.42, "timestamp": "2026-05-21T00:00:00Z" },
  { "round_id": 2, "multiplier": 3.20, "timestamp": "2026-05-21T00:00:08Z" }
]
```

The backend also accepts a plain numeric array or an object with `rounds` or `history`.

## Automation Hook

`GET /predict` appends the newest prediction to `decisions.json`. `bot/playwright_bot.py` is a safe placeholder that reads the latest decision and prints it, ready for a permitted Playwright workflow.
# my-job
# money-tree
