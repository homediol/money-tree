"""Fail-closed platform transaction evidence classification."""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
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

PANEL_VISUAL_STATES = {"BET_AVAILABLE", "BET_QUEUED", "BET_ACTIVE", "BET_RESET", "UNKNOWN"}
_PANEL_ID = re.compile(r"^PANEL_[12]$")
_AMOUNT_TOKEN = re.compile(r"(?<![A-Z])([0-9][0-9,]*(?:\.[0-9]+)?)(?![0-9])")
_CURRENCY_TOKEN = re.compile(r"(?<![A-Z])([A-Z]{3})(?![A-Z])")


def _recognized_button_color(value: Any) -> str | None:
    """Recognize broad CSS color families as supporting, not sole, evidence."""
    text = str(value or "").strip().lower()
    named = {
        "green": "GREEN", "red": "RED", "amber": "AMBER",
        "gold": "AMBER", "yellow": "AMBER",
    }
    if text in named:
        return named[text]
    match = re.search(r"rgba?\(\s*(\d{1,3})\s*,\s*(\d{1,3})\s*,\s*(\d{1,3})", text)
    if not match:
        match = re.fullmatch(r"#([0-9a-f]{3}|[0-9a-f]{6})", text)
        if not match:
            return None
        hex_value = match.group(1)
        if len(hex_value) == 3:
            red, green, blue = (int(ch * 2, 16) for ch in hex_value)
        else:
            red, green, blue = (int(hex_value[index:index + 2], 16) for index in (0, 2, 4))
    else:
        red, green, blue = (min(255, int(channel)) for channel in match.groups())
    if green >= 80 and green >= red * 1.18 and green >= blue * 1.15:
        return "GREEN"
    # Aviator's cash-out control is amber/golden yellow. Bright standard
    # orange (a much larger red-to-green gap) is deliberately not accepted.
    if red >= 175 and green >= 160 and blue <= 125 and red > green and red <= green * 1.5:
        return "AMBER"
    if red >= 100 and red >= green * 1.35 and red >= blue * 1.25:
        return "RED"
    return None


def _panel_amount_and_currency(text: str, currency_hint: Any = None) -> tuple[float | None, str | None]:
    currencies = {item.upper() for item in _CURRENCY_TOKEN.findall(text)
                  if item.upper() not in {"BET", "OUT"}}
    if isinstance(currency_hint, str) and re.fullmatch(r"[A-Za-z]{3}", currency_hint.strip()):
        hinted = currency_hint.strip().upper()
        if hinted not in {"BET", "OUT"}:
            currencies.add(hinted)
    currency = next(iter(currencies)) if len(currencies) == 1 else None
    match = _AMOUNT_TOKEN.search(text)
    amount = None
    if match:
        try:
            amount = float(match.group(1).replace(",", ""))
        except (ValueError, OverflowError):
            amount = None
    return amount, currency


def classify_bet_panel_observation(raw: Mapping[str, Any] | None, *,
                                   now: datetime | None = None,
                                   max_age_seconds: float = 15.0) -> dict[str, Any]:
    """Normalize one panel's visible DOM evidence without asserting a bet receipt.

    The action text and enabled state lead the classification. Recognized
    contradictory colors or simultaneous action labels fail closed to UNKNOWN.
    """
    source_row = raw if isinstance(raw, Mapping) else {}
    panel_id = str(source_row.get("panel_id") or "UNKNOWN")
    frame_identity = str(source_row.get("frame_identity") or "")[:180] or None
    source = str(source_row.get("source") or "AVIATOR_BETTING_PANEL_READ_ONLY")[:80]
    button_text = " ".join(str(source_row.get("button_text") or "").split())[:240]
    main_label = " ".join(str(source_row.get("main_label") or "").split())[:80]
    if not main_label:
        match = re.match(r"\s*(bet|cancel|cash\s*out)\b", button_text, re.IGNORECASE)
        main_label = match.group(1) if match else ""
    secondary_text = " ".join(str(source_row.get("secondary_text") or "").split())[:160]
    combined_text = " ".join(part for part in (button_text, secondary_text) if part)
    normalized_text = " ".join(combined_text.casefold().split())
    observed_at = str(source_row.get("observed_at") or "")
    evidence = {
        "button_visible": source_row.get("button_visible") is True,
        "button_enabled": source_row.get("button_enabled") is True,
        "aria_label": str(source_row.get("aria_label") or "")[:160] or None,
        "title": str(source_row.get("title") or "")[:160] or None,
        "button_attributes": dict(source_row.get("button_attributes") or {})
            if isinstance(source_row.get("button_attributes"), Mapping) else {},
        "button_color": str(source_row.get("button_color") or "")[:80] or None,
        "recognized_color": _recognized_button_color(source_row.get("button_color")),
        "visible_action_labels": [str(item)[:120] for item in source_row.get("visible_action_labels", [])]
            if isinstance(source_row.get("visible_action_labels"), (list, tuple)) else [],
        "pending_indicator": source_row.get("pending_indicator") is True,
        "active_indicator": source_row.get("active_indicator") is True,
    }
    normalized_main = re.sub(r"\s+", " ", main_label.casefold()).strip()
    color = evidence["recognized_color"]
    expected_color = {"bet": "GREEN", "cancel": "RED", "cash out": "AMBER"}.get(normalized_main)
    queued_text = bool(re.search(r"\bwaiting for (?:the )?next round\b", normalized_text))
    visible_labels = " ".join(evidence["visible_action_labels"]).casefold()
    has_cancel = normalized_main == "cancel" or bool(re.search(r"\bcancel\b", visible_labels))
    has_cashout = normalized_main == "cash out" or bool(re.search(r"\bcash\s*out\b", visible_labels))
    has_bet = normalized_main == "bet" or bool(re.search(r"\bbet\b", visible_labels))
    explicit_reset = str(source_row.get("data_state") or "").strip().casefold() in {"reset", "idle", "clear"}

    state = "UNKNOWN"
    reason = "PANEL_EVIDENCE_INCOMPLETE"
    confidence = 0.0
    if not _PANEL_ID.fullmatch(panel_id) or not frame_identity:
        reason = "PANEL_OR_FRAME_IDENTITY_UNKNOWN"
    else:
        try:
            stamp = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
            if stamp.tzinfo is None:
                raise ValueError("timezone required")
            reference = now or datetime.now(timezone.utc)
            if reference.tzinfo is None:
                reference = reference.replace(tzinfo=timezone.utc)
            age = (reference.astimezone(timezone.utc) - stamp.astimezone(timezone.utc)).total_seconds()
        except (TypeError, ValueError, OverflowError):
            age = None
        if age is None:
            reason = "OBSERVATION_TIMESTAMP_INVALID"
        elif age < -30 or age > max_age_seconds:
            reason = "STALE_OR_FUTURE_OBSERVATION"
        elif source_row.get("panel_visible") is not True:
            reason = "PANEL_NOT_VISIBLE"
        elif source_row.get("button_visible") is not True:
            if explicit_reset:
                state, reason, confidence = "BET_RESET", "EXPLICIT_PANEL_RESET", 0.9
            else:
                reason = "ACTION_CONTROL_NOT_VISIBLE"
        elif sum((has_bet, has_cancel, has_cashout)) > 1:
            reason = "CONFLICTING_VISIBLE_ACTIONS"
        elif (source_row.get("pending_indicator") is True and normalized_main == "bet") or (
            source_row.get("active_indicator") is True and normalized_main in {"bet", "cancel"}
        ):
            reason = "CONTRADICTORY_PENDING_OR_ACTIVE_INDICATOR"
        elif expected_color and color and color != expected_color:
            reason = "BUTTON_TEXT_COLOR_CONFLICT"
        elif normalized_main == "bet" and source_row.get("button_enabled") is True:
            state, reason = "BET_AVAILABLE", "BET_ACTION_VISIBLE_ENABLED"
        elif normalized_main == "cancel" and queued_text and source_row.get("button_enabled") is True:
            state, reason = "BET_QUEUED", "CANCEL_AND_NEXT_ROUND_WAIT_VISIBLE"
        elif normalized_main == "cash out" and source_row.get("button_enabled") is True:
            state, reason = "BET_ACTIVE", "CASHOUT_ACTION_VISIBLE_ENABLED"
        else:
            reason = "STATE_LABEL_OR_ENABLED_STATUS_INCOMPLETE"
        if state != "UNKNOWN":
            confidence = 0.98 if color == expected_color else 0.88

    stake_value = source_row.get("displayed_stake_text")
    stake_text = (str(stake_value) if stake_value is not None
                  else "" if normalized_main == "cash out" else button_text)
    stake_amount, stake_currency = _panel_amount_and_currency(stake_text, source_row.get("currency_hint"))
    cashout_text = str(source_row.get("cashout_text") or button_text)
    cashout_amount = None
    if normalized_main == "cash out" or state == "BET_ACTIVE":
        amount_match = _AMOUNT_TOKEN.search(cashout_text)
        if amount_match:
            try:
                cashout_amount = float(amount_match.group(1).replace(",", ""))
            except (ValueError, OverflowError):
                cashout_amount = None
        _, cashout_currency = _panel_amount_and_currency(cashout_text, source_row.get("currency_hint"))
        if stake_currency is None:
            stake_currency = cashout_currency

    result = {
        "panel_id": panel_id,
        "slot": int(panel_id[-1]) - 1 if _PANEL_ID.fullmatch(panel_id) else None,
        "state": state,
        "button_text": button_text or None,
        "displayed_stake": stake_amount,
        "currency": stake_currency,
        "cashout_amount": cashout_amount,
        "observed_at": observed_at or None,
        "source": source,
        "evidence": {**evidence, "reason": reason, "panel_visible": source_row.get("panel_visible") is True,
                     "main_label": main_label or None, "secondary_text": secondary_text or None},
        "confidence": confidence,
        "frame_identity": frame_identity,
        "pending_bet_state": ("PENDING" if state == "BET_QUEUED" else
                              "ACTIVE" if state == "BET_ACTIVE" else
                              "CLEAR" if state in {"BET_AVAILABLE", "BET_RESET"} else "UNKNOWN"),
    }
    return result


def panel_observation_transition(previous: Mapping[str, Any] | None,
                                 current: Mapping[str, Any] | None) -> dict[str, Any]:
    """Deduplicate repeated state observations and reject out-of-order samples."""
    old = previous if isinstance(previous, Mapping) else {}
    new = current if isinstance(current, Mapping) else {}
    old_id, new_id = old.get("observed_at"), new.get("observed_at")
    stale = False
    if old_id and new_id:
        try:
            old_time = datetime.fromisoformat(str(old_id).replace("Z", "+00:00"))
            new_time = datetime.fromisoformat(str(new_id).replace("Z", "+00:00"))
            stale = old_time.tzinfo is not None and new_time.tzinfo is not None and new_time < old_time
        except (TypeError, ValueError, OverflowError):
            stale = True
    old_state = str(old.get("state") or "UNKNOWN")
    new_state = str(new.get("state") or "UNKNOWN")
    frame_reloaded = bool(old.get("frame_identity") and new.get("frame_identity")
                          and old.get("frame_identity") != new.get("frame_identity"))
    duplicate = bool(old and new and not frame_reloaded and old_state == new_state
                     and old.get("button_text") == new.get("button_text")
                     and old.get("displayed_stake") == new.get("displayed_stake")
                     and old.get("currency") == new.get("currency")
                     and old.get("cashout_amount") == new.get("cashout_amount"))
    return {
        "changed": bool(new) and not stale and (not duplicate or frame_reloaded),
        "duplicate": duplicate,
        "stale": stale,
        "frame_reloaded": frame_reloaded,
        "from_state": "UNKNOWN" if frame_reloaded else old_state if old else None,
        # The green control is currently available, but its transition from
        # a queued/active bet is a reset event. Preserve the observed state so
        # consumers can distinguish the event from the live panel state.
        "to_state": ("BET_RESET" if old_state in {"BET_QUEUED", "BET_ACTIVE"}
                     and new_state == "BET_AVAILABLE" and not frame_reloaded
                     else new_state if new else "UNKNOWN"),
        "current_state": new_state if new else "UNKNOWN",
        "observed_at": new_id,
    }


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
    """Classify a betting window only from explicit phase/deadline evidence.

    A visible enabled green Bet action reports panel affordance, not whether
    the provider is currently accepting bets.
    """
    if not isinstance(evidence, Mapping):
        return "UNKNOWN"
    panel_index = evidence.get("panel_index", evidence.get("slot"))
    if (evidence.get("source") != "AVIATOR_EXPLICIT_BETTING_PHASE_DOM"
            or type(panel_index) is not int or panel_index not in (0, 1)
            or (panel is not None and panel_index != panel)
            or evidence.get("phase_indicator_visible") is not True):
        return "UNKNOWN"
    explicit_state = str(evidence.get("explicit_window_state") or "").upper()
    if explicit_state == "BETTING_OPEN":
        return "BETTING_OPEN"
    if explicit_state == "BETTING_CLOSED":
        return "BETTING_CLOSED"
    return "UNKNOWN"


def classify_round_phase(*, betting_window: Mapping[str, Any] | None = None,
                         phase_hints: Any = None,
                         status_messages: Any = None) -> str:
    """Classify only explicit live game-phase evidence from the shared frame.

    A disabled or hidden bet button is not enough to infer a round phase.
    Status text and stage class tokens must state a phase, or the verified
    Bet-tab control classifier must establish an open/closed betting window.
    """
    hints = phase_hints if isinstance(phase_hints, (list, tuple)) else []
    messages = status_messages if isinstance(status_messages, (list, tuple)) else []
    evidence: list[tuple[str, str]] = []
    for item in (*hints, *messages):
        if isinstance(item, Mapping):
            if item.get("visible") is False:
                continue
            evidence.append((str(item.get("cls") or item.get("class") or ""),
                             str(item.get("txt") or item.get("text") or "")))
        elif isinstance(item, str):
            evidence.append(("", item))

    for classes, text in evidence:
        normalized = " ".join(f"{classes} {text}".lower().replace("_", " ").split())
        if (re.search(r"\b(?:flew away|crashed|round (?:is )?(?:complete|completed|finished))\b", normalized)
                or re.search(r"\b(?:round|game)[-_ ]?(?:state[-_ ]?)?(?:crashed|completed|finished)\b", normalized)):
            return "ROUND_ENDED"
    for classes, text in evidence:
        normalized = " ".join(f"{classes} {text}".lower().replace("_", " ").split())
        stage_evidence = "stage" in classes.lower() or "stage" in normalized
        transition_label = re.search(
            r"\b(?:ufc\s*(?:/|vs\.?|versus)\s*aviator|aviator\s*(?:/|vs\.?|versus)\s*ufc)\b"
            r"|\b(?:ufc|aviator)\b.{0,32}\b(?:transition|intermission)\b",
            normalized,
        )
        if (transition_label or re.search(
                r"\b(?:waiting for (?:the )?next round|next round (?:starts|starting)|waiting for bets)\b",
                normalized)
                or re.search(r"\b(?:stage|round)[-_ ]?(?:state[-_ ]?)?(?:waiting|idle|between)\b", normalized)):
            # A generic page mention of the game name is not intermission
            # evidence; a transition label must be in a matched stage node.
            if not transition_label or stage_evidence:
                return "INTERMISSION"
    state = str((betting_window or {}).get("state") or "UNKNOWN").upper()
    if state == "BETTING_OPEN":
        return state
    if state == "BETTING_CLOSED":
        return state
    for classes, text in evidence:
        normalized = " ".join(f"{classes} {text}".lower().replace("_", " ").split())
        if (re.search(r"\b(?:round|game) (?:is )?(?:in progress|running|flying)\b", normalized)
                or re.search(r"\b(?:stage|round)[-_ ]?(?:state[-_ ]?)?(?:flying|in progress|running)\b", normalized)
                or re.fullmatch(r"\s*\d+(?:[.,]\d+)?\s*x\s*", text.strip(), flags=re.IGNORECASE)):
            return "ROUND_RUNNING"
    return "UNKNOWN"


def observe_betting_deadline(*, phase_hints: Any = None,
                             status_messages: Any = None,
                             observed_at: str | None = None) -> dict[str, Any]:
    """Return a deadline only when the page explicitly displays a countdown."""
    values: list[tuple[str, str]] = []
    for item in (*(phase_hints or []), *(status_messages or [])):
        if isinstance(item, Mapping):
            if item.get("visible") is False:
                continue
            values.append((str(item.get("text") or item.get("txt") or ""),
                           str(item.get("source") or "SHARED_AVIATOR_FRAME_DOM")))
        elif isinstance(item, str):
            values.append((item, "SHARED_AVIATOR_FRAME_DOM"))
    pattern = re.compile(
        r"\b(?:betting|bets?)(?:\s+window)?\s+(?:closes|ends)\s+in\s+(\d+(?:\.\d+)?)\s*(?:s|sec|seconds?)\b",
        re.IGNORECASE,
    )
    for text, source in values:
        match = pattern.search(text)
        if not match:
            continue
        try:
            stamp = datetime.fromisoformat(str(observed_at).replace("Z", "+00:00")) if observed_at else datetime.now(timezone.utc)
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=timezone.utc)
            deadline = stamp + timedelta(seconds=float(match.group(1)))
        except (OverflowError, TypeError, ValueError):
            break
        seconds = float(match.group(1))
        if seconds > 300:
            return {"status": "UNKNOWN", "deadline_at": None,
                    "reason": "BETTING_COUNTDOWN_OUT_OF_RANGE"}
        return {"status": "VERIFIED", "deadline_at": deadline.isoformat(),
                "remaining_seconds": seconds, "source": source,
                "observed_at": stamp.isoformat(), "text": text.strip()}
    return {"status": "UNKNOWN", "deadline_at": None,
            "reason": "EXPLICIT_BETTING_COUNTDOWN_NOT_OBSERVED"}


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
