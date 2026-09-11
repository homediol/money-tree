"""Betting automation module (Part 1).

Receives external betting decisions (never generates predictions), executes
them against the live Aviator browser session via raw CDP (read-only in this
part), and exposes status/control over REST + /ws/live events.

Modes
-----
- REAL:       operates against the real browser session. Real DOM bet placement
              is gated by ``BettingSettings.allow_real_placement`` and is only
              implemented in a later part; until then decisions are validated
              and honestly deferred/rejected — never faked.
- SIMULATION: opt-in demo mode. Clearly labelled as simulation, never presented
              as a real bet, never touches the browser DOM.

State model distinguishes CONNECTED / SIMULATION / NOT_CONNECTED /
WAITING_FOR_BROWSER so callers can always tell simulation from reality.
"""

from app.betting.config import BettingSettings, get_betting_settings
from app.betting.profiles import PROFILES, BettingProfile, profile_keys
from app.betting.schemas import DecisionIntent, SessionStartRequest
from app.betting.session import (
    BettingManager,
    BettingSession,
    configure_default_manager,
    get_betting_manager,
)
from app.betting.states import OutcomeStatus, SessionState

__all__ = [
    "BettingSettings",
    "get_betting_settings",
    "BettingManager",
    "BettingSession",
    "configure_default_manager",
    "get_betting_manager",
    "PROFILES",
    "BettingProfile",
    "profile_keys",
    "DecisionIntent",
    "SessionStartRequest",
    "OutcomeStatus",
    "SessionState",
]

