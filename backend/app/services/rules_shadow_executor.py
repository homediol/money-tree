"""Retired adapter for the former stake-managed paper executor."""
from __future__ import annotations

from typing import Any


async def execute_rules_selection_in_shadow(app, selection_id: str,
                                            intent: dict[str, Any]) -> dict[str, Any]:
    """Keep Rules Engine selections observe-only.

    The prior adapter copied an application stake, panel, cashout target, and
    risk profile into a simulated execution. New rule sessions own only
    target-bound decisions, so this compatibility entry point must never
    consult platform controls or create paper trades from those legacy fields.
    Historical paper execution records remain available through their read APIs.
    """
    del app, selection_id, intent
    return {
        "status": "BLOCKED",
        "reason": "RULES_ENGINE_OBSERVE_ONLY",
        "real_money_action_submitted": False,
    }
