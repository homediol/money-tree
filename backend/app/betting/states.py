"""Session states and outcome statuses for the betting module."""
from __future__ import annotations

from enum import Enum


class SessionState(str, Enum):
    """Lifecycle state of a betting session.

    CONNECTED, SIMULATION, NOT_CONNECTED and WAITING_FOR_BROWSER are the
    values the UI must distinguish:
      - CONNECTED            → real browser session verified and usable.
      - SIMULATION           → opt-in demo mode (never a real bet).
      - NOT_CONNECTED        → browser unreachable / required frames missing.
      - WAITING_FOR_BROWSER  → was connected, lost the browser, re-checking.
    """

    IDLE = "IDLE"
    STARTING = "STARTING"
    CONNECTED = "CONNECTED"
    SIMULATION = "SIMULATION"
    NOT_CONNECTED = "NOT_CONNECTED"
    WAITING_FOR_BROWSER = "WAITING_FOR_BROWSER"
    STOPPING = "STOPPING"
    STOPPED = "STOPPED"
    ERROR = "ERROR"

    @property
    def is_active(self) -> bool:
        return self in {
            SessionState.STARTING,
            SessionState.CONNECTED,
            SessionState.SIMULATION,
            SessionState.NOT_CONNECTED,
            SessionState.WAITING_FOR_BROWSER,
        }


class Mode(str, Enum):
    """How a session executes decisions."""

    REAL = "REAL"
    SIMULATION = "SIMULATION"


class OutcomeStatus(str, Enum):
    """Lifecycle of a single decision/ledger entry.

    ``deferred`` is the honest Part-1 state for a validated real decision that
    cannot be placed yet (placement gated) — it is *never* reported as placed.
    """

    RECEIVED = "received"
    PLACED = "placed"          # bet placed on the live table (real or sim)
    RESOLVED = "resolved"      # round finished, win/loss computed
    DEFERRED = "deferred"      # validated but placement unavailable/gated
    REJECTED = "rejected"      # failed validation or safety pre-flight


class DecisionRejectReason(str, Enum):
    NONE = "none"
    SESSION_NOT_RUNNING = "session_not_running"
    NOT_CONNECTED = "browser_not_connected"
    INSUFFICIENT_BALANCE = "insufficient_balance"
    UI_NOT_READY = "betting_ui_not_ready"
    PLACEMENT_DISABLED = "real_placement_disabled"
    PLACEMENT_DRIVER_UNAVAILABLE = "placement_driver_not_implemented"
    INTERNAL_ERROR = "internal_error"

