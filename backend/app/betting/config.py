"""Runtime configuration for the betting module.

All values can be overridden through environment variables so the module can
be pointed at a different Chrome CDP endpoint or operated in tests without
touching application code.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


@dataclass(frozen=True)
class BettingSettings:
    """Knobs for the betting module."""

    #: Chrome DevTools HTTP endpoint. Reuses the same browser as the history
    #: collector (never launches a second browser).
    cdp_http: str = field(default_factory=lambda: os.environ.get(
        "BETTING_CDP_ENDPOINT",
        os.environ.get("BOT_CDP_ENDPOINT", "http://127.0.0.1:9222"),
    ).rstrip("/"))

    #: Master switch for real DOM bet placement. Part 1 ships with this OFF:
    #: the executor performs read-only verification and defers placement.
    allow_real_placement: bool = field(default_factory=lambda: _env_bool(
        "BETTING_ALLOW_REAL_PLACEMENT", False,
    ))

    #: Poll cadence (s) of the executor round monitor.
    poll_interval_s: float = field(default_factory=lambda: _env_float(
        "BETTING_POLL_INTERVAL", 2.0,
    ))

    #: Cadence (s) for browser re-checks while NOT_CONNECTED /
    #: WAITING_FOR_BROWSER.
    browser_recheck_s: float = field(default_factory=lambda: _env_float(
        "BETTING_RECHECK_INTERVAL", 5.0,
    ))

    #: How many ledger entries are kept in memory (ring buffer).
    ledger_cap: int = field(default_factory=lambda: _env_int(
        "BETTING_LEDGER_CAP", 200,
    ))

    #: Reconnect attempts tolerated while CONNECTED before dropping to
    #: WAITING_FOR_BROWSER / NOT_CONNECTED.
    max_consecutive_read_errors: int = field(default_factory=lambda: _env_int(
        "BETTING_MAX_READ_ERRORS", 3,
    ))

    #: Simulation round cadence used for demo runs (no browser needed).
    sim_round_every_s: float = field(default_factory=lambda: _env_float(
        "BETTING_SIM_ROUND_EVERY", 6.0,
    ))


_default_settings = None


def get_betting_settings() -> BettingSettings:
    """Return the process-wide betting settings (built once)."""
    global _default_settings
    if _default_settings is None:
        _default_settings = BettingSettings()
    return _default_settings


