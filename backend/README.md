# Winner Predict backend

The active backend is the FastAPI service in `main.py`. It serves PostgreSQL
round history, account and betting safety APIs, reconciliation, operations,
WebSocket updates, and the empty Rules Engine. It does not load prediction
models or start training and statistical evaluation jobs.

Configure `backend/.env` from `.env.example`, install `requirements.txt`, and
run `python -m uvicorn main:app --host 0.0.0.0 --port 8000` from this directory.
Production startup requires the authoritative PostgreSQL database.
