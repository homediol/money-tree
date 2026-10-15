"""Fail-closed bridge from durable Rules Engine SELECTs to paper execution."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

from app.betting.schemas import DecisionIntent
from app.shadow import shadow_target_id


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _record(repository, decision_id: str, status: str, reason: str | None = None,
            **evidence) -> dict[str, Any]:
    payload = {"status": status, "reason": reason, "updated_at": _now(), **evidence}
    saved = repository.record_rule_shadow_execution(decision_id, payload)
    return saved.get("shadow_execution", payload) if saved else payload


async def execute_rules_selection_in_shadow(app, selection_id: str,
                                            intent: dict[str, Any]) -> dict[str, Any]:
    """Route one prospective shared selection through existing risk/shadow gates.

    This function never invokes the Aviator betting button. The only browser
    access is a read-only setup verification against the existing shared CDP
    page. Target identity is the canonical stream/index successor, not a
    guessed provider round ID.
    """
    repository = app.state.wp.repository
    decision_id = str(selection_id or "")
    if not decision_id or not isinstance(intent, dict):
        return {"status": "BLOCKED", "reason": "RULE_SELECTION_INTENT_UNAVAILABLE"}
    persisted = repository.rule_execution_intent(decision_id)
    if not persisted:
        return {"status": "BLOCKED", "reason": "DURABLE_RULE_INTENT_UNAVAILABLE"}
    previous = persisted.get("shadow_execution")
    if isinstance(previous, dict):
        return {**previous, "already_recorded": True}

    def blocked(reason: str, **evidence):
        return _record(repository, decision_id, "BLOCKED", reason, **evidence)

    session_id = str(intent.get("session_id") or "")
    rules_session = app.state.rules_engine.session(session_id) if session_id else None
    selection = next((row for row in (rules_session or {}).get("selections", [])
                      if str(row.get("selection_id")) == decision_id), None)
    if not rules_session or not selection:
        return blocked("RULE_SESSION_OR_SELECTION_UNAVAILABLE")
    if rules_session.get("status") != "RUNNING" or rules_session.get("selection_status") != "ACTIVE":
        return blocked("RULE_SESSION_NOT_RUNNING")

    panel = intent.get("selected_panel")
    stake = intent.get("requested_stake")
    currency = str(intent.get("requested_stake_currency") or "").upper()
    if type(panel) is not int or panel not in (0, 1):
        return blocked("EXECUTION_PANEL_NOT_SELECTED")
    if isinstance(stake, bool) or not isinstance(stake, int) or stake < 1 or currency != "BIF":
        return blocked("FIXED_BIF_STAKE_NOT_CONFIGURED",
                       requested_stake=stake, requested_currency=currency or None)
    if float(intent.get("cashout_target") or 0) != 2.0:
        return blocked("CASHOUT_TARGET_MISMATCH", requested_cashout=intent.get("cashout_target"))

    mode_manager = getattr(app.state, "betting_mode", None)
    shadow = getattr(app.state, "shadow", None)
    if (not mode_manager or getattr(mode_manager, "mode", None) != "SHADOW_REALISTIC"
            or getattr(mode_manager, "live_authorized", False)
            or bool(getattr(getattr(app.state, "live", None), "live_active", False))):
        return blocked("SHADOW_REALISTIC_MODE_REQUIRED")
    if (not shadow or shadow.mode != "SHADOW" or not getattr(mode_manager, "shadow_ready", False)
            or not isinstance(shadow.session, dict)):
        return blocked("SHADOW_EXECUTOR_NOT_RUNNING")
    if bool(getattr(getattr(app.state, "risk", None), "emergency_latched", False)):
        return blocked("EMERGENCY_STOP_ACTIVE")

    shadow_config = shadow.session.get("config") or {}
    enabled_panels = [row for row in shadow_config.get("panels", []) if row.get("enabled")]
    if (shadow_config.get("mode") != "MANUAL" or len(enabled_panels) != 1
            or enabled_panels[0].get("panel") != panel + 1
            or enabled_panels[0].get("stake") != stake
            or float(enabled_panels[0].get("cashout") or 0) != 2.0):
        return blocked("SHADOW_EXECUTOR_CONFIGURATION_MISMATCH",
                       selected_panel=panel + 1, configured_panels=enabled_panels)

    # Confirm the operator's current controls immediately before paper
    # execution. This is read-only and reuses the existing browser/page/frame.
    from app.api.live import _manual_setup_observation
    from app.betting.browser_client import AviatorBrowserClient

    client = AviatorBrowserClient(app.state.betting.settings)
    try:
        snapshot = await asyncio.wait_for(client.snapshot(), timeout=5)
    except Exception as exc:
        return blocked("SHARED_AVIATOR_READBACK_UNAVAILABLE", error=type(exc).__name__)
    finally:
        try:
            await client.close()
        except Exception:
            pass
    live_manager = getattr(app.state, "live", None)
    prior_readbacks = getattr(live_manager, "manual_setup_readbacks", None)
    observed, next_readbacks = _manual_setup_observation(snapshot, {
        "execution_panel": panel, "requested_stake": stake,
        "requested_stake_currency": currency,
        "configuration_source": "RULES_ENGINE_SESSION",
    }, prior_readbacks)
    if live_manager is not None:
        live_manager.manual_setup_readbacks = next_readbacks
    checklist = {row.get("item"): row.get("status") for row in observed.get("checklist", [])}
    panel_cashout = (observed.get("cashout_panels") or [])[panel]
    setup_evidence = {
        "selected_panel": panel + 1,
        "expected_stake": stake, "expected_currency": currency,
        "observed_stake": observed.get("observed_stake"),
        "observed_currency": observed.get("observed_currency"),
        "stake_status": checklist.get("Stake manually entered", "UNAVAILABLE"),
        "cashout_status": panel_cashout.get("status"),
        "cashout_multiplier": panel_cashout.get("multiplier"),
        "auto_cashout": panel_cashout.get("auto_cashout"),
        "auto_bet": panel_cashout.get("auto_bet"),
        "pending_bet_state": panel_cashout.get("pending_bet_state"),
        "observed_at": observed.get("observed_at"),
        "frame_identity": panel_cashout.get("frame_identity"),
        "source": observed.get("source"),
    }
    stake_status = setup_evidence["stake_status"]
    if stake_status != "STAKE_VERIFIED":
        reason = ("STAKE_MISMATCH" if stake_status == "STAKE_MISMATCH"
                  else "STAKE_READBACK_UNAVAILABLE")
        return blocked(reason, setup=setup_evidence)
    if panel_cashout.get("status") != "CASHOUT_VERIFIED":
        return blocked("AUTO_CASHOUT_CONFIGURATION_NOT_VERIFIED", setup=setup_evidence)
    if checklist.get("No active or pending bet") != "VERIFIED":
        return blocked("ACTIVE_OR_PENDING_BET", setup=setup_evidence)
    if str(observed.get("account_currency") or "").upper() != currency:
        return blocked("ACCOUNT_CURRENCY_MISMATCH", setup=setup_evidence,
                       account_currency=observed.get("account_currency"))

    target_ref = intent.get("target_round_ref") or {}
    stream_id = str(intent.get("target_stream_id") or target_ref.get("streamId") or "")
    target_index = intent.get("target_round_index", target_ref.get("roundIndex"))
    if (not stream_id or isinstance(target_index, bool) or not isinstance(target_index, int)
            or target_index < 1 or target_ref.get("streamId") != stream_id
            or target_ref.get("roundIndex") != target_index):
        return blocked("PROSPECTIVE_TARGET_IDENTITY_UNAVAILABLE", setup=setup_evidence)
    source_ref = selection.get("source_round") or {}
    if source_ref.get("streamId") != stream_id or target_index != int(source_ref.get("roundIndex") or 0) + 1:
        return blocked("TARGET_IS_NOT_IMMEDIATE_SUCCESSOR", setup=setup_evidence)
    target_id = shadow_target_id(stream_id, target_index)

    profile = str(shadow.session.get("profile") or "").upper()
    try:
        risk_intent = DecisionIntent(
            decision_id=decision_id, round_id=target_id, execute=True,
            profile=profile, cashout=2.0, bet_amount=stake, source="rules_engine:shared_select",
        )
        risk_result = await app.state.risk.evaluate(risk_intent, mode_manager.betting_status())
        risk_evidence = risk_result.public()
    except Exception as exc:
        return blocked("RISK_EVALUATION_UNAVAILABLE", setup=setup_evidence,
                       error=type(exc).__name__)
    if not risk_evidence.get("approved"):
        return blocked("RISK_REJECTED", setup=setup_evidence, risk_evaluation=risk_evidence)

    result = await shadow.submit_decision({
        "decision_id": decision_id, "target_round_id": target_id,
        "target_stream_id": stream_id, "target_round_index": target_index,
        "target_round_ref": target_ref, "target_identity_status": "CANONICAL_STREAM_INDEX",
        "profile": profile, "status": "READY_FOR_EXECUTION",
        "risk_evaluation": risk_evidence,
    }, emergency_stop=bool(app.state.risk.emergency_latched))
    if result.get("accepted") is not True:
        return blocked(str(result.get("reason") or "SHADOW_EXECUTION_REJECTED"),
                       setup=setup_evidence, risk_evaluation=risk_evidence)
    trade_ids = [str(row.get("execution_id")) for row in result.get("trades", [])
                 if row.get("execution_id")]
    return _record(repository, decision_id, "PAPER_AUTHORIZED", None,
                   shadow_session_id=shadow.session.get("session_id"),
                   target_round_id=target_id, target_stream_id=stream_id,
                   target_round_index=target_index,
                   target_identity_status="CANONICAL_STREAM_INDEX",
                   trade_ids=trade_ids, setup=setup_evidence,
                   risk_evaluation=risk_evidence,
                   platform_acceptance="NOT_APPLICABLE_SHADOW",
                   settlement="PENDING_TARGET_ROUND",
                   real_money_action_submitted=False)
