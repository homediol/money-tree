"""Rules Engine selection forwarding through the existing executor gates."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, Mapping

from app.betting.platform_state import classify_betting_window
from app.betting.schemas import DecisionIntent
from app.betting.session import BettingError


class DecisionDispatcher:
    """Forward target-bound rule selections to the existing betting manager.

    This service does not predict, authorize LIVE_REAL, or place platform bets
    itself. The existing API and executor gates remain authoritative.
    """

    def __init__(self, app):
        self.app = app
        self._lock = asyncio.Lock()

    def is_connected(self) -> bool:
        rules = getattr(self.app.state, "rules_engine", None)
        manager = getattr(self.app.state, "betting", None)
        return bool(rules is not None and callable(getattr(rules, "execution_selection", None))
                    and manager is not None and callable(getattr(manager, "submit_decision", None)))

    @staticmethod
    def _blocked(reason: str, *, decision_id: str | None = None) -> dict[str, Any]:
        return {"http_status": 409, "ok": False, "error": "decision_execution_blocked",
                "decision": "NO_ACTION", "automatic_execution": "OFF",
                "decision_id": decision_id, "reasons": [reason]}

    async def dispatch_evaluation(self, evaluation: Mapping[str, Any], *,
                                  delivery_mode: str = "LIVE",
                                  target_already_completed: bool = False) -> dict[str, Any]:
        """Dispatch only fresh selections from the ordered live committed path."""
        mode = str(delivery_mode or "").upper()
        if mode != "LIVE" or target_already_completed:
            return {"status": "SKIPPED", "reason": "NON_LIVE_OR_ALREADY_COMPLETED_TARGET",
                    "dispatched": []}
        if not isinstance(evaluation, Mapping):
            return {"status": "BLOCKED", "reason": "RULE_EVALUATION_UNAVAILABLE",
                    "dispatched": []}
        decisions = []
        for session in evaluation.get("sessions", []) or []:
            decision = session.get("decision") if isinstance(session, Mapping) else None
            if isinstance(decision, Mapping) and decision.get("action") == "SELECT":
                decisions.append(str(decision.get("selection_id") or ""))
        results = []
        for decision_id in dict.fromkeys(item for item in decisions if item):
            result = await self.dispatch_selection(decision_id)
            results.append(result)
        return {"status": "DISPATCHED" if any(row.get("ok") for row in results)
                else "BLOCKED" if results else "NO_SELECTION",
                "dispatched": results}

    async def dispatch_selection(self, decision_id: str) -> dict[str, Any]:
        engine = getattr(self.app.state, "rules_engine", None)
        selection = (engine.execution_selection(decision_id)
                     if engine is not None and callable(
                         getattr(engine, "execution_selection", None)) else None)
        if selection is None:
            return self._blocked("decision_id_does_not_match_a_pending_selection_from_the_active_rules_session",
                                 decision_id=decision_id)
        approved = selection.get("execution_intent") or {}
        manager = getattr(self.app.state, "betting", None)
        if manager is None or getattr(manager, "session", None) is None:
            return self._blocked("existing_betting_executor_session_unavailable", decision_id=decision_id)
        mode = getattr(manager.session, "mode", None)
        mode = getattr(mode, "value", mode)
        phase = getattr(self.app.state, "latest_provider_phase_observation", None) or {}

        # A fresh provider record can bind the rule's prospective canonical
        # target. It must identify the exact source and immediate successor.
        if (mode == "REAL" and not approved.get("target_round_id")
                and callable(getattr(engine, "bind_provider_target", None))):
            selection = engine.bind_provider_target(decision_id, phase) or selection
            approved = selection.get("execution_intent") or {}

        round_id = approved.get("target_round_id")
        expiry = approved.get("expires_at")
        if not round_id or not expiry:
            return self._blocked("provider_target_identity_or_deadline_unverified",
                                 decision_id=decision_id)
        try:
            expires_at = datetime.fromisoformat(str(expiry).replace("Z", "+00:00"))
            if expires_at.tzinfo is None:
                return self._blocked("target_betting_deadline_unverified", decision_id=decision_id)
        except (TypeError, ValueError, OverflowError):
            return self._blocked("target_betting_deadline_unverified", decision_id=decision_id)

        stake = approved.get("requested_stake")
        panel = approved.get("panel")
        target_index = approved.get("target_round_index")
        if (isinstance(stake, bool) or not isinstance(stake, (int, float))
                or int(stake) != stake or stake <= 0 or panel not in (0, 1)
                or isinstance(target_index, bool) or not isinstance(target_index, int)):
            return self._blocked("rules_selection_execution_contract_invalid", decision_id=decision_id)

        body = DecisionIntent(
            decision_id=decision_id,
            round_id=str(round_id),
            cashout=2.0,
            expires_at=expires_at,
            bet_amount=int(stake),
            bet_slot=panel,
            amount_currency=approved.get("requested_stake_currency"),
            source="RULES_ENGINE",
            source_round_id=str(approved.get("source_round_id") or ""),
            target_stream_id=str(approved.get("target_stream_id") or ""),
            target_round_index=target_index,
            provider_round_id=approved.get("provider_round_id"),
        )
        return await self.dispatch_intent(body)

    async def dispatch_intent(self, body: DecisionIntent) -> dict[str, Any]:
        """Validate an API or Rules Engine decision, then call the same executor."""
        async with self._lock:
            engine = getattr(self.app.state, "rules_engine", None)
            selection = (engine.execution_selection(body.decision_id)
                         if engine is not None and callable(
                             getattr(engine, "execution_selection", None)) else None)
            if selection is None:
                return {"http_status": 409, "ok": False,
                        "error": "eligible_rule_decision_not_found",
                        "decision": "NO_ACTION", "automatic_execution": "OFF",
                        "reasons": [
                            "decision_id_does_not_match_a_pending_selection_from_the_active_rules_session",
                        ]}
            approved = selection.get("execution_intent") or {}
            reasons: list[str] = []
            if not approved.get("target_round_id") or body.round_id != str(approved.get("target_round_id")):
                reasons.append("target_round_does_not_match_verified_rule_selection")
            if (body.source_round_id != approved.get("source_round_id")
                    or body.target_stream_id != approved.get("target_stream_id")
                    or body.target_round_index != approved.get("target_round_index")):
                reasons.append("source_or_target_identity_does_not_match_rule_selection")
            if body.provider_round_id != approved.get("provider_round_id"):
                reasons.append("provider_round_id_does_not_match_verified_rule_selection")
            if body.effective_amount() != approved.get("requested_stake"):
                reasons.append("fixed_stake_does_not_match_rules_engine_configuration")
            if body.amount_currency != approved.get("requested_stake_currency"):
                reasons.append("stake_currency_does_not_match_rules_engine_configuration")
            if body.bet_slot != approved.get("panel"):
                reasons.append("execution_panel_does_not_match_rules_engine_configuration")
            if body.cashout != 2.0:
                reasons.append("cashout_must_equal_2_00x")
            expiry = approved.get("expires_at")
            try:
                approved_expiry = datetime.fromisoformat(str(expiry).replace("Z", "+00:00"))
                if approved_expiry.tzinfo is None:
                    approved_expiry = None
            except (TypeError, ValueError, OverflowError):
                approved_expiry = None
            if body.is_expired():
                reasons.append("decision_expired")
            elif approved_expiry is None:
                reasons.append("target_betting_deadline_unverified")
            elif body.expires_at > approved_expiry:
                reasons.append("decision_expiry_exceeds_rule_target_deadline")

            manager = getattr(self.app.state, "betting", None)
            if manager is None or getattr(manager, "session", None) is None:
                reasons.append("existing_betting_executor_session_unavailable")
            emergency_stop = getattr(self.app.state, "emergency_stop", None)
            if emergency_stop is None or emergency_stop.latched:
                reasons.append("emergency_stop_active_or_unavailable")
            modes = getattr(self.app.state, "betting_mode", None)
            session_mode = getattr(getattr(manager, "session", None), "mode", None)
            session_mode = getattr(session_mode, "value", session_mode)
            if session_mode == "REAL":
                phase = getattr(self.app.state, "latest_provider_phase_observation", None) or {}
                if (classify_betting_window(phase) != "BETTING_OPEN"
                        or phase.get("canonical_target_round_id") != body.round_id
                        or phase.get("provider_round_id") != body.provider_round_id
                        or phase.get("target_round_index") != body.target_round_index):
                    reasons.append("provider_round_or_open_deadline_evidence_unverified")
                if phase.get("betting_deadline"):
                    try:
                        deadline = datetime.fromisoformat(
                            str(phase["betting_deadline"]).replace("Z", "+00:00"))
                        if body.expires_at > deadline:
                            reasons.append("decision_expiry_exceeds_provider_betting_deadline")
                    except (TypeError, ValueError, OverflowError):
                        reasons.append("provider_betting_deadline_unverified")
                if modes is None:
                    reasons.append("live_authorization_gate_unavailable")
                else:
                    observation = await modes._read_platform_balance()
                    if observation.get("verified") is not True or observation.get("currency_verified") is not True:
                        reasons.append("selected_platform_account_currency_unverified")
                    elif observation.get("currency") != body.amount_currency:
                        reasons.append("stake_currency_does_not_match_selected_platform_account")
            elif session_mode != "SIMULATION":
                reasons.append("executor_session_is_not_active_in_a_supported_mode")

            if reasons:
                return {"http_status": 409, "ok": False, "error": "decision_execution_blocked",
                        "decision": "NO_ACTION", "automatic_execution": "OFF",
                        "decision_id": body.decision_id, "target_round_id": body.round_id,
                        "reasons": list(dict.fromkeys(reasons))}

            # A collector may replay a committed event after an acknowledgement
            # is lost. The execution ledger is the durable idempotency source;
            # avoid passing a recorded decision to BettingSession, where a
            # duplicate is treated as a live safety fault.
            repository = getattr(manager, "repository", None)
            lookup_execution = getattr(repository, "execution_for", None)
            if callable(lookup_execution):
                existing = lookup_execution(body.decision_id, body.round_id)
                if existing is not None:
                    return {"http_status": 200, "ok": True,
                            "status": "ALREADY_DISPATCHED",
                            "decision_id": body.decision_id,
                            "target_round_id": body.round_id,
                            "execution": existing}

            if session_mode == "REAL":
                gate = await modes.final_execution_gate(body) if modes is not None else {
                    "allowed": False, "reasons": ["live_authorization_gate_unavailable"]}
                if not gate.get("allowed"):
                    return {"http_status": 409, "ok": False, "error": "decision_execution_blocked",
                            "decision": "NO_ACTION", "automatic_execution": "OFF",
                            "decision_id": body.decision_id, "target_round_id": body.round_id,
                            "reasons": list(dict.fromkeys(gate.get("reasons", [])))}
            try:
                entry = await manager.submit_decision(body)
                return {"http_status": 200, "ok": True,
                        "decision_id": body.decision_id,
                        "target_round_id": body.round_id, "execution": entry}
            except BettingError as exc:
                return {"http_status": 409, "ok": False, "error": getattr(exc, "code", "betting_error"),
                        "message": str(exc)}
