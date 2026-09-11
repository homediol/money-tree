"""
Entry point for the Aviator ML Enterprise Platform.

Run with:
    uvicorn main:app --host 0.0.0.0 --port 8002 --workers 1
"""
from __future__ import annotations

import os

# Ensure the project root is on the path
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))

from config import configure_logging, settings

configure_logging()

from src.api.app import create_app  # noqa: E402

app = create_app()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "main:app",
        host=settings.host,
        port=settings.port,
        reload=settings.debug,
        log_level=settings.log_level.lower(),
        workers=1 if settings.debug else settings.workers,
    )


