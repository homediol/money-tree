"""
Structured logging configuration using structlog.

All log records are emitted as JSON in production for ingestion by log
aggregators (Loki, ELK, CloudWatch).  In development a coloured console
renderer is used.
"""
from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path
from typing import Any, Dict

import structlog

from config.settings import settings


def configure_logging() -> None:
    """Idempotent global logger configuration."""

    log_dir: Path = settings.logs_dir  # type: ignore[assignment]
    log_dir.mkdir(parents=True, exist_ok=True)

    # --- shared processors ---
    shared_processors = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.processors.UnicodeDecoder(),
    ]

    # --- renderer: JSON in production, console in dev ---
    if settings.app_env in ("production", "staging"):
        renderer: Any = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer(colors=True)

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_processors,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            renderer,
        ],
    )

    # console handler
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(formatter)

    # rotating file handler
    file_handler = logging.handlers.RotatingFileHandler(
        log_dir / "aviator_enterprise.log",
        maxBytes=20 * 1024 * 1024,
        backupCount=10,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(console)
    root.addHandler(file_handler)
    root.setLevel(getattr(logging, settings.log_level, logging.INFO))

    # silence noisy third-parties
    for noisy in ("urllib3", "asyncio", "matplotlib"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)


__all__ = ["configure_logging", "get_logger"]

