#!/home/diolo/winner predict/backend/.venv/bin/python3
"""
FastAPI compatibility service for Orbit Round History status.

The former browser-based history runner is retired. Status and logs remain
available for compatibility; start requests cannot launch Playwright.

Runs as a separate service on port 5001.
"""

import logging
import hmac
import sys
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Dict, Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

# Ensure the backend package is importable
sys.path.insert(0, str(Path(__file__).resolve().parent))

import bot_runner
from app.core.config import Settings

bot_settings = Settings(_env_file=Path(__file__).resolve().parent / ".env")

# ── Logging ───────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[
        logging.FileHandler(Path(__file__).resolve().parent / "bot-api.log"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger("bot-api")


# ── Pydantic models ───────────────────────────────────────────────────

class StartRequest(BaseModel):
    phone: str = Field(..., min_length=5, max_length=32, description="Winner.rw account phone number")
    password: str = Field(..., min_length=1, max_length=512, description="Account password")
    headless: bool = Field(False, description="Run browser in headless mode")


class StopResponse(BaseModel):
    success: bool
    message: str = ""
    error: str = ""
    status: str = "idle"


# ── FastAPI app ───────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Bot API service starting...")
    yield
    # Shutdown: stop the bot if running
    try:
        result = bot_runner.stop()
        logger.info("Bot stopped during shutdown: %s", result)
    except Exception as exc:
        logger.error("Error stopping bot during shutdown: %s", exc)
    logger.info("Bot API service stopped.")


app = FastAPI(
    title="Aviator Bot Control API",
    version="1.0.0",
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=list(dict.fromkeys([*bot_settings.cors_origins,
                                      *bot_settings.round_stream_allowed_origins])),
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)

_request_times: dict[str, deque[float]] = defaultdict(deque)


def _local_request(request: Request) -> bool:
    peer = request.client.host if request.client else ""
    host = str(request.url.hostname or "").strip("[]").lower()
    local = {"127.0.0.1", "::1", "localhost"}
    return peer == "testclient" or (peer in local and host in local)


@app.middleware("http")
async def protect_bot_control(request: Request, call_next):
    if request.method == "OPTIONS":
        return await call_next(request)
    if request.url.path == "/":
        return await call_next(request)
    origin = request.headers.get("origin")
    allowed_origins = set(bot_settings.cors_origins) | set(bot_settings.round_stream_allowed_origins)
    if origin and origin not in allowed_origins:
        logger.warning("bot API rejected disallowed origin path=%s", request.url.path)
        return JSONResponse(status_code=403, content={"ok": False, "error": "origin_not_allowed"})
    local = _local_request(request)
    if request.url.scheme != "https" and not local:
        logger.warning("bot API rejected insecure remote request path=%s", request.url.path)
        return JSONResponse(status_code=426,
                            content={"ok": False, "error": "https_required_for_external_access"})

    if request.url.path.startswith("/bot/"):
        peer = request.client.host if request.client else "unknown"
        now = time.monotonic()
        bucket = _request_times[peer]
        while bucket and bucket[0] <= now - 60:
            bucket.popleft()
        limit = 10 if request.url.path in {"/bot/start", "/bot/stop", "/bot/manual-aviator"} else 60
        if len(bucket) >= limit:
            retry_after = max(1, int(60 - (now - bucket[0])))
            logger.warning("bot API rate limit exceeded path=%s", request.url.path)
            return JSONResponse(status_code=429,
                                content={"ok": False, "error": "rate_limit_exceeded"},
                                headers={"Retry-After": str(retry_after)})
        bucket.append(now)

    api_key = bot_settings.api_key
    if not api_key:
        if not local:
            logger.error("bot API private route disabled because API_KEY is not configured")
            return JSONResponse(status_code=503,
                                content={"ok": False, "error": "api_key_not_configured"},
                                headers={"WWW-Authenticate": "Bearer"})
    else:
        supplied = request.headers.get("authorization", "")
        if not hmac.compare_digest(supplied, f"Bearer {api_key}"):
            logger.warning("bot API authentication failed path=%s", request.url.path)
            return JSONResponse(status_code=401,
                                content={"ok": False, "error": "authentication_required"},
                                headers={"WWW-Authenticate": "Bearer"})

    raw_content_length = request.headers.get("content-length")
    try:
        body_size = int(raw_content_length) if raw_content_length is not None else 0
    except ValueError:
        body_size = 65_537
    if body_size < 0:
        return JSONResponse(status_code=400,
                            content={"ok": False, "error": "invalid_content_length"})
    if (request.method in {"POST", "PUT", "PATCH"}
            and request.headers.get("transfer-encoding") and raw_content_length is None):
        return JSONResponse(status_code=411,
                            content={"ok": False, "error": "content_length_required"})
    if body_size > 65_536:
        logger.warning("bot API rejected oversized request path=%s", request.url.path)
        return JSONResponse(status_code=413,
                            content={"ok": False, "error": "request_body_too_large"})
    return await call_next(request)


# ── Endpoints ─────────────────────────────────────────────────────────

@app.get("/")
async def root():
    return {"service": "aviator-bot-api", "status": "online"}


@app.post("/bot/start", response_model=Dict[str, Any])
async def start_bot(req: StartRequest) -> Dict[str, Any]:
    """The retired browser collector cannot be started through this API."""
    raise HTTPException(status_code=410, detail="round_history_uses_orbit_data_watch_api")


@app.post("/bot/stop", response_model=StopResponse)
async def stop_bot() -> StopResponse:
    """Stop the running bot gracefully."""
    logger.info("Stop request received")
    result = bot_runner.stop()
    return StopResponse(**result)


@app.post("/bot/manual-aviator", response_model=Dict[str, Any])
async def manual_aviator() -> Dict[str, Any]:
    """Manual browser collection has been retired."""
    raise HTTPException(status_code=410, detail="round_history_uses_orbit_data_watch_api")


@app.get("/bot/status", response_model=Dict[str, Any])
async def get_bot_status() -> Dict[str, Any]:
    """Get the current bot status and health."""
    return bot_runner.get_status()


@app.get("/bot/logs")
async def get_bot_logs(tail: int = Query(default=100, ge=1, le=1000)):
    """Get recent bot output logs."""
    logs = bot_runner.get_logs(tail=tail)
    return logs


# ── Entry point ───────────────────────────────────────────────────────

def main():
    import uvicorn
    uvicorn.run(
        "bot_api:app",
        host="0.0.0.0",
        port=5001,
        reload=False,
        log_level="info",
    )


if __name__ == "__main__":
    main()
