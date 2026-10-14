"""Fail-closed platform transaction evidence classification."""
from __future__ import annotations

import re
from enum import StrEnum
from typing import Any, Mapping


class PlatformBetState(StrEnum):
    NOT_ATTEMPTED = "NOT_ATTEMPTED"
    PENDING = "PENDING"
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"
    UNKNOWN = "UNKNOWN"


_INSUFFICIENT_BALANCE = re.compile(
    r"\b(?:insufficient\s+(?:account\s+)?balance|"
    r"balance\s+(?:is\s+)?insufficient|not\s+enough\s+balance)\b",
    re.IGNORECASE,
)


def detect_insufficient_balance(messages: Any) -> dict[str, Any]:
    """Recognize an explicit insufficient-balance message, without inference.

    ``messages`` must come from an observed platform alert or a controlled
    mock. A low account balance by itself is not treated as a rejection.
    """
    if isinstance(messages, str):
        values = [messages]
    elif isinstance(messages, (list, tuple)):
        values = [item if isinstance(item, str) else str(item.get("text") or "")
                  for item in messages if isinstance(item, (str, Mapping))]
    else:
        values = []
    observed = [" ".join(value.split()) for value in values if value.strip()]
    matched = [value for value in observed if _INSUFFICIENT_BALANCE.search(value)]
    return {
        "detected": bool(matched),
        "observed": bool(observed),
        "matched_messages": matched,
        "source": "OBSERVED_ALERT_TEXT" if matched else None,
    }


def classify_betting_window(evidence: Mapping[str, Any] | None,
                            panel: int | None = None) -> str:
    """Classify the betting window from the owned Aviator controls only."""
    if not isinstance(evidence, Mapping):
        return "UNKNOWN"
    panel_index = evidence.get("panel_index")
    if (evidence.get("source") != "AVIATOR_BETTING_PANEL_READ_ONLY"
            or evidence.get("selector") != ".btn-success.bet"
            or type(panel_index) is not int or panel_index < 0
            or (panel is not None and panel_index != panel)
            or evidence.get("button_visible") is not True
            or evidence.get("stake_input_visible") is not True
            or evidence.get("bet_tab_active") is not True):
        return "UNKNOWN"
    if evidence.get("button_enabled") is True:
        return "BETTING_OPEN"
    if evidence.get("button_enabled") is False:
        return "BETTING_CLOSED"
    return "UNKNOWN"


def classify_platform_bet_state(evidence: Mapping[str, Any] | None) -> PlatformBetState:
    """Classify only receipt-backed platform evidence; clicks and DOM are UNKNOWN."""
    if not isinstance(evidence, Mapping):
        return PlatformBetState.UNKNOWN
    state = str(evidence.get("platform_status") or evidence.get("status") or "").upper()
    receipt_id = evidence.get("platform_receipt_id") or evidence.get("receipt_id")
    target_id = evidence.get("target_round_id")
    session_id = evidence.get("session_identity")
    observed_at = evidence.get("platform_observed_at") or evidence.get("accepted_at")
    stake = evidence.get("accepted_stake")
    currency = evidence.get("accepted_currency") or evidence.get("currency")
    common_receipt = bool(evidence.get("platform_evidence") is True
                          and receipt_id and target_id and session_id and observed_at)
    if (state in {"REJECTED", "DECLINED", "FAILED"} and common_receipt):
        return PlatformBetState.REJECTED
    if state in {"PENDING", "QUEUED", "PROCESSING"} and common_receipt:
        return PlatformBetState.PENDING
    if (state in {"ACCEPTED", "PLACED"} and common_receipt
            and isinstance(stake, (int, float)) and not isinstance(stake, bool) and stake > 0
            and isinstance(currency, str) and currency.strip()
            and evidence.get("decision_id")):
        return PlatformBetState.ACCEPTED
    if evidence.get("click_performed") is True or evidence.get("button_enabled") is True:
        return PlatformBetState.UNKNOWN
    return PlatformBetState.NOT_ATTEMPTED if not evidence else PlatformBetState.UNKNOWN


def classify_platform_bet_result(evidence: Mapping[str, Any] | None) -> str:
    """A round multiplier alone never settles a bet result."""
    if not isinstance(evidence, Mapping) or evidence.get("platform_evidence") is not True:
        return "UNKNOWN"
    if (not evidence.get("platform_receipt_id") or not evidence.get("settlement_receipt_id")
            or not evidence.get("target_round_id") or not evidence.get("session_identity")
            or not evidence.get("settled_at")):
        return "UNKNOWN"
    result = str(evidence.get("bet_result") or "").upper()
    return result if result in {"WIN", "LOSS"} else "UNKNOWN"
