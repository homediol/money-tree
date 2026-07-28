"""
Re-exports the configured logger so every module can do
`from src.core.logger import get_logger`.
"""
from config.logging_config import get_logger  # noqa: F401

