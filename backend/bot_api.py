#!/home/diolo/winner predict/backend/.venv/bin/python3
"""
FastAPI service for controlling the Playwright automation bot.

Provides REST endpoints for:
  - POST /bot/start   — launch the bot with phone + password
  - POST /bot/stop    — gracefully stop the bot
  - GET  /bot/status  — current bot status and health
  - GET  /bot/logs    — recent bot output

Runs as a separate service on port 5001.
"""

import logging
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Dict, Any

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

# Ensure the backend package is importable
sys.path.insert(0, str(Path(__file__).resolve().parent))

import bot_runner

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
    phone: str = Field(..., min_length=5, description="Winner.rw account phone number")
    password: str = Field(..., min_length=1, description="Account password")
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
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Endpoints ─────────────────────────────────────────────────────────

@app.get("/")
async def root():
    return {"service": "aviator-bot-api", "status": "online"}


@app.post("/bot/start", response_model=Dict[str, Any])
async def start_bot(req: StartRequest) -> Dict[str, Any]:
    """Start the Playwright bot with the given credentials."""
    logger.info("Start request for phone %s (headless=%s)", req.phone[-4:], req.headless)
    result = bot_runner.start(req.phone, req.password, req.headless)
    if not result.get("success"):
        raise HTTPException(status_code=409, detail=result.get("error", "Failed to start bot"))
    return result


@app.post("/bot/stop", response_model=StopResponse)
async def stop_bot() -> StopResponse:
    """Stop the running bot gracefully."""
    logger.info("Stop request received")
    result = bot_runner.stop()
    return StopResponse(**result)


@app.post("/bot/manual-aviator", response_model=Dict[str, Any])
async def manual_aviator() -> Dict[str, Any]:
    """Tell the running bot the user has manually reached Aviator."""
    logger.info("Manual Aviator confirmation received")
    result = bot_runner.request_manual_start()
    if not result.get("success"):
        raise HTTPException(status_code=409, detail=result.get("error", "Bot is not ready"))
    return result


@app.get("/bot/status", response_model=Dict[str, Any])
async def get_bot_status() -> Dict[str, Any]:
    """Get the current bot status and health."""
    return bot_runner.get_status()


@app.get("/bot/logs")
async def get_bot_logs(tail: int = 100):
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
