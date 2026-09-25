import logging
import os
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path


def configure_logging() -> None:
    log_path = Path(os.environ.get("WINNER_PREDICT_LOG", "logs/backend.log"))
    log_path.parent.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter(
        "%(asctime)sZ %(levelname)s %(name)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    # The trailing Z means UTC. logging uses local time unless its converter is
    # overridden, which previously produced misleading timestamps in Kigali.
    formatter.converter = time.gmtime
    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(formatter)
    file_handler = RotatingFileHandler(log_path, maxBytes=10 * 1024 * 1024,
                                       backupCount=5, encoding="utf-8")
    file_handler.setFormatter(formatter)
    logging.basicConfig(
        level=logging.INFO,
        handlers=[stream, file_handler],
        force=True,
    )


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
