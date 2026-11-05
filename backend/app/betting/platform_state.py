"""Fail-closed platform transaction evidence classification."""
from __future__ import annotations

import re
from datetime import datetime, timezone
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
    """Classify only fresh provider phase and deadline evidence.

    A visible, enabled Bet button is presentation state. It does not identify
    the provider phase, round, or betting deadline.
    """
    if not isinstance(evidence, Mapping):
        return "UNKNOWN"
    if (evidence.get("source") != "AVIATOR_GAME_PHASE_READ_ONLY"
            or not isinstance(evidence.get("provider_round_id"), str)
            or not evidence.get("provider_round_id")
            or not isinstance(evidence.get("canonical_target_round_id"), str)
            or not evidence.get("canonical_target_round_id")
            or evidence.get("target_identity_verified") is not True
            or evidence.get("target_started") is not False):
        return "UNKNOWN"
    phase = str(evidence.get("phase_state") or evidence.get("phase") or "").upper()
    if phase not in {"BETTING_OPEN", "BETTING_CLOSED"}:
        return "UNKNOWN"
    proof = evidence.get("target_identity_proof")
    try:
        source_index = int(evidence.get("source_round_index"))
        target_index = int(evidence.get("target_round_index"))
    except (TypeError, ValueError, OverflowError):
        return "UNKNOWN"
    if (not evidence.get("source_round_id") or source_index < 1
            or target_index != source_index + 1
            or not isinstance(proof, Mapping)
            or proof.get("provider_round_id") != evidence.get("provider_round_id")
            or proof.get("target_round_id") != evidence.get("canonical_target_round_id")
            or proof.get("target_not_started") is not True
            or proof.get("proof_method") != "platform_target_id_before_start"):
        return "UNKNOWN"
    provider_evidence = evidence.get("evidence")
    if (not isinstance(provider_evidence, Mapping)
            or provider_evidence.get("source") != "AVIATOR_GAME_PHASE_READ_ONLY"
            or provider_evidence.get("provider_phase_source") != "EXPLICIT_PROVIDER_DATA_ATTRIBUTES"
            or provider_evidence.get("provider_phase_selector") != (
                "[data-aviator-phase][data-provider-round-id][data-canonical-target-round-id][data-betting-deadline]"
            )
            or provider_evidence.get("selector") != ".bet-block .btn-success.bet"
            or provider_evidence.get("stake_input_visible") is not True
            or provider_evidence.get("bet_tab_active") is not True):
        return "UNKNOWN"
    if phase == "BETTING_OPEN" and not (
        provider_evidence.get("button_visible") is True
        and provider_evidence.get("button_enabled") is True
    ):
        return "UNKNOWN"
    try:
        observed_at = datetime.fromisoformat(
            str(evidence.get("observed_at") or "").replace("Z", "+00:00"))
        deadline = datetime.fromisoformat(
            str(evidence.get("betting_deadline") or "").replace("Z", "+00:00"))
        if observed_at.tzinfo is None or deadline.tzinfo is None:
            return "UNKNOWN"
        now = datetime.now(timezone.utc)
        age = (now - observed_at.astimezone(timezone.utc)).total_seconds()
        remaining = (deadline.astimezone(timezone.utc) - now).total_seconds()
        if age < 0 or age > 5:
            return "UNKNOWN"
    except (TypeError, ValueError, OverflowError):
        return "UNKNOWN"
    if phase == "BETTING_OPEN" and remaining > 0:
        return "BETTING_OPEN"
    if phase == "BETTING_CLOSED" or remaining <= 0:
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
