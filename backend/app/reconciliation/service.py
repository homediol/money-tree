"""Post-execution observation, verification and immutable reconciliation."""
from __future__ import annotations

import copy
import uuid
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from app.betting.platform_state import (
    PlatformBetState,
    classify_platform_bet_result,
    classify_platform_bet_state,
)


FINAL = {"RECONCILED", "CLOSED", "FAILED", "EXPIRED", "REJECTED"}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _finite_nonnegative(value: Any) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and value >= 0 and value not in {float("inf"), float("-inf")})


def _same_amount(left: Any, right: Any) -> bool:
    return (_finite_nonnegative(left) and _finite_nonnegative(right)
            and abs(float(left) - float(right)) <= 0.005)


class InvalidTransition(ValueError):
    pass


class ReconciliationService:
    ALLOWED = {
        "AUTHORIZED": {"RISK_APPROVED"}, "RISK_APPROVED": {"QUEUED"},
        "QUEUED": {"PLACED", "FAILED", "EXPIRED"},
        "PLACED": {"ROUND_ACTIVE", "UNKNOWN"},
        "ROUND_ACTIVE": {"CASHOUT_PENDING", "CASHED_OUT", "LOST", "UNKNOWN"},
        "CASHOUT_PENDING": {"CASHED_OUT", "LOST", "UNKNOWN"},
        "CASHED_OUT": {"RECONCILIATION_PENDING"}, "LOST": {"RECONCILIATION_PENDING"},
        "UNKNOWN": {"RECONCILIATION_PENDING"},
        "RECONCILIATION_PENDING": {"RECONCILED", "UNKNOWN"},
        "RECONCILED": {"CLOSED"}, "CLOSED": set(), "FAILED": set(), "EXPIRED": set(),
    }

    def __init__(self, repository, *, broadcaster: Callable[[dict], Awaitable[None]] | None = None,
                 betting_manager=None, risk_manager=None):
        self.repository = repository
        self.broadcaster = broadcaster
        self.betting_manager = betting_manager
        self.risk_manager = risk_manager
        self.rules_engine = None

    async def emit(self, kind: str, **payload) -> None:
        if self.broadcaster:
            await self.broadcaster({"type": kind, **payload})

    def transition(self, execution: dict, target: str, evidence: dict | None = None) -> dict:
        current = execution.get("lifecycle_state") or {
            "VALIDATING": "QUEUED", "BET_PLACED": "PLACED",
            "CASHED_OUT": "CASHED_OUT", "LOST": "LOST", "UNKNOWN": "UNKNOWN",
        }.get(execution.get("status"), execution.get("status"))
        if target not in self.ALLOWED.get(current, set()):
            raise InvalidTransition(f"invalid execution transition {current} -> {target}")
        stamp = now_iso()
        execution["lifecycle_state"] = target
        execution["updated_at"] = stamp
        self.repository.append_execution_event(execution["execution_id"], target, stamp,
                                               evidence or {})
        return execution

    async def reconcile_round(self, round_result: dict[str, Any],
                              platform_evidence: dict[str, dict] | None = None) -> list[dict]:
        """Reconcile only executions whose immutable target matches this exact round."""
        round_id = str(round_result["round_id"])
        multiplier = float(round_result["multiplier"])
        results = []
        for execution in self.repository.open_executions():
            if str(execution.get("target_round_id")) != round_id:
                continue
            evidence = {**(execution.get("platform_evidence") or {}),
                        **(platform_evidence or {}).get(execution["execution_id"], {})}
            results.append(await self.reconcile(execution, round_id=round_id,
                                                multiplier=multiplier, evidence=evidence))
        return results

    async def reconcile(self, execution: dict[str, Any], *, round_id: str,
                        multiplier: float, evidence: dict[str, Any]) -> dict:
        if str(execution.get("target_round_id")) != str(round_id):
            raise ValueError("round_mismatch")
        # Browser observations and History Collector results arrive on
        # independent loops. Keep the former on the execution so exact-round
        # reconciliation can consume it even when no in-memory evidence map is
        # available to this call.
        durable_evidence = execution.get("platform_evidence") or {}
        evidence = {**durable_evidence, **(evidence or {})}
        original = copy.deepcopy(execution)
        stamp = now_iso()
        simulated = bool(execution.get("simulated"))
        platform_state = (PlatformBetState.ACCEPTED if simulated
                          else classify_platform_bet_state(evidence))
        placement_confirmed = simulated or platform_state == PlatformBetState.ACCEPTED
        cashout_requested = bool(evidence.get("cashout_requested"))
        cashout_executed = bool(evidence.get("cashout_executed"))
        cashout_confirmed = bool(simulated and multiplier >= execution["cashout_target"]
                                 or evidence.get("cashout_confirmed") is True)
        pending_reason = None
        accepted_stake = None
        accepted_currency = None
        actual_payout = evidence.get("payout")
        pnl = None
        if simulated:
            if multiplier < float(execution["cashout_target"]):
                outcome, pnl = "LOSS", -float(execution["bet_amount"])
            elif cashout_confirmed:
                outcome = "WIN"
                pnl = float(execution["bet_amount"]) * (float(execution["cashout_target"]) - 1)
            else:
                outcome, pnl = "UNKNOWN", None
        else:
            outcome = classify_platform_bet_result(evidence)
            accepted_stake = evidence.get("accepted_stake")
            accepted_currency = evidence.get("accepted_currency") or evidence.get("currency")
            if not placement_confirmed:
                pending_reason = "platform_acceptance_receipt_unverified"
            elif not _same_amount(execution.get("cashout_target"), 2.0):
                pending_reason = "cashout_target_mismatch"
            elif str(evidence.get("target_round_id")) != str(round_id):
                pending_reason = "platform_receipt_target_mismatch"
            elif str(evidence.get("decision_id")) != str(execution.get("decision_id")):
                pending_reason = "platform_receipt_decision_mismatch"
            elif not _same_amount(accepted_stake, execution.get("bet_amount")):
                pending_reason = "accepted_stake_mismatch"
            elif not isinstance(accepted_currency, str) or not accepted_currency.strip():
                pending_reason = "accepted_currency_unverified"
            elif (execution.get("platform_session_identity")
                  and evidence.get("session_identity") != execution["platform_session_identity"]):
                pending_reason = "platform_session_mismatch"
            elif outcome == "UNKNOWN":
                pending_reason = "platform_settlement_receipt_unverified"
            elif outcome == "WIN" and multiplier < float(execution["cashout_target"]):
                pending_reason = "settlement_conflicts_with_round_result"
            elif outcome == "LOSS" and multiplier >= float(execution["cashout_target"]):
                pending_reason = "settlement_conflicts_with_cashout_target"
            elif not _finite_nonnegative(actual_payout):
                pending_reason = "actual_payout_unverified"
            elif outcome == "WIN" and not _same_amount(
                    actual_payout, float(accepted_stake) * float(execution["cashout_target"])):
                pending_reason = "actual_payout_mismatch"
            elif outcome == "LOSS" and not _same_amount(actual_payout, 0):
                pending_reason = "loss_payout_mismatch"
            elif evidence.get("balance_before_verified") is not True:
                pending_reason = "pre_bet_balance_unverified"
            elif not _finite_nonnegative(execution.get("balance_before")):
                pending_reason = "pre_bet_balance_unverified"
            elif execution.get("balance_currency") != accepted_currency:
                pending_reason = "pre_bet_balance_currency_mismatch"
            elif (not isinstance(evidence.get("balance_provider_identity"), str)
                  or not evidence.get("balance_provider_identity")):
                pending_reason = "balance_provider_unverified"
            elif evidence.get("balance_session_identity") != evidence.get("session_identity"):
                pending_reason = "balance_session_mismatch"
            elif evidence.get("balance_observed_at") is None:
                pending_reason = "balance_observation_time_unverified"
            elif (evidence.get("balance_currency") or evidence.get("platform_balance_currency")) != accepted_currency:
                pending_reason = "balance_currency_mismatch"
            elif evidence.get("platform_observed_balance") is None:
                pending_reason = "post_bet_balance_unverified"
            elif not _finite_nonnegative(evidence.get("platform_observed_balance")):
                pending_reason = "post_bet_balance_unverified"
            else:
                # The platform's receipt is the authority. The displayed round
                # multiplier is only a consistency check and never creates an outcome.
                pnl = float(actual_payout) - float(accepted_stake)
                cashout_confirmed = outcome == "WIN"

        before = execution.get("balance_before")
        observed_candidate = evidence.get("platform_observed_balance")
        balance_observation_verified = (
            simulated or (
                _finite_nonnegative(observed_candidate)
                and bool(evidence.get("balance_provider_identity"))
                and evidence.get("balance_session_identity") == evidence.get("session_identity")
                and bool(evidence.get("balance_observed_at"))
                and (evidence.get("balance_currency") or evidence.get("platform_balance_currency"))
                    == accepted_currency
            )
        )
        observed = observed_candidate if balance_observation_verified else None
        if execution.get("simulated") and observed is None:
            observed = evidence.get("balance_after")
        expected = None if before is None or pnl is None else round(float(before) + pnl, 2)
        reconciled = float(observed) if observed is not None else None
        balance_status = "VERIFIED" if observed is not None else "UNKNOWN"
        if (expected is not None and reconciled is not None
                and abs(expected - reconciled) > 0.01):
            balance_status = "MISMATCH"
            reconciled = None
        if not simulated and pending_reason is None and balance_status != "VERIFIED":
            pending_reason = "post_bet_balance_unverified"
        if outcome == "UNKNOWN" or pending_reason is not None or balance_status != "VERIFIED":
            final_status = "RECONCILIATION_PENDING"
            outcome = "UNKNOWN"
            pnl = None
        else:
            final_status = "RECONCILED"

        execution.update({
            "status": final_status, "lifecycle_state": final_status,
            "updated_at": stamp, "resolved_at": stamp,
            "result_multiplier": multiplier, "result": outcome,
            "cashout_requested": cashout_requested,
            "cashout_executed": cashout_executed,
            "cashout_confirmed": cashout_confirmed,
            "cashout_target": execution.get("cashout_target"),
            "actual_payout": actual_payout,
            "balance_after": reconciled, "profit_loss": pnl if reconciled is not None else None,
            "balance_status": balance_status,
            "platform_evidence": evidence,
            "exact_round_result": {"round_id": round_id, "multiplier": multiplier},
            "error": pending_reason or ("verification_incomplete" if final_status != "RECONCILED" else None),
        })
        reconciliation = {
            "reconciliation_id": uuid.uuid4().hex, "execution_id": execution["execution_id"],
            "round_id": round_id, "status": final_status, "outcome": outcome,
            "created_at": stamp, "requested": {"bet_amount": original["bet_amount"],
                                                  "cashout": original["cashout_target"]},
            "actual": {"multiplier": multiplier, "payout": actual_payout,
                       "round_id": round_id},
            "verification": {"platform_bet_state": str(platform_state),
                             "settlement_status": outcome,
                             "placement_confirmed": placement_confirmed,
                             "cashout_requested": cashout_requested,
                             "cashout_executed": cashout_executed,
                             "cashout_confirmed": cashout_confirmed,
                             "pending_reason": pending_reason},
            "balance": {"internal_expected_balance": expected,
                        "platform_observed_balance": observed,
                        "reconciled_balance": reconciled,
                        "balance_status": balance_status},
        }
        ledger = []
        if final_status == "RECONCILED":
            event = "PAYOUT_CREDIT" if outcome == "WIN" else "RECONCILIATION"
            ledger.append(self._ledger(execution, event, pnl, expected, observed, reconciled,
                                       balance_status, stamp))
        if final_status != "RECONCILED":
            # UNKNOWN is durable as pending execution evidence, but is not an
            # immutable terminal reconciliation. A later receipt/balance read
            # can resolve it without rewriting a financial result.
            execution["status"] = "RECONCILIATION_PENDING"
            execution["lifecycle_state"] = "RECONCILIATION_PENDING"
            self.repository.update_execution(execution)
            self.repository.append_execution_event(execution["execution_id"],
                "RECONCILIATION_PENDING", stamp, reconciliation["verification"])
            reconciliation["risk_action"] = await self._post_result(execution, reconciliation)
            await self.emit("reconciliation:updated", reconciliation=reconciliation)
            await self.emit("execution:updated", execution=execution)
            return reconciliation
        if not self.repository.save_reconciliation(reconciliation, execution, ledger):
            existing = next((x for x in self.repository.list_reconciliations(1000)
                             if x["execution_id"] == execution["execution_id"]), reconciliation)
            # A prior process may have committed the receipt and exited before
            # updating the Rules Engine stop state. Re-delivery is idempotent.
            await self._apply_rules_session_result(execution, existing)
            return existing
        self.repository.append_execution_event(execution["execution_id"], final_status, stamp,
                                               reconciliation["verification"])
        action = await self._post_result(execution, reconciliation)
        reconciliation["risk_action"] = action
        await self.emit("reconciliation:updated", reconciliation=reconciliation)
        await self.emit("execution:updated", execution=execution)
        await self.emit("balance:updated", balance=reconciliation["balance"])
        return reconciliation

    async def _post_result(self, execution: dict, reconciliation: dict) -> str:
        action = "CONTINUE"
        if reconciliation["status"] != "RECONCILED":
            action = "PAUSE"
        await self._apply_rules_session_result(execution, reconciliation)
        manager = self.betting_manager
        status = manager.status() if manager else {}
        balance = reconciliation["balance"].get("reconciled_balance")
        if balance is not None and manager and manager.session:
            session = manager.session
            session.last_balance = balance
            currency = (execution.get("balance_currency")
                        or (execution.get("platform_evidence") or {}).get("balance_currency"))
            session.last_balance_text = f"{balance:.2f} {currency}" if currency else f"{balance:.2f}"
        if action in {"PAUSE", "EMERGENCY_STOP"} and manager:
            try:
                await manager.stop_session(reason=("reconciliation_unknown" if action == "PAUSE"
                                                   else "emergency_stop"))
            except Exception:
                pass
        await self.emit("risk:post_result", action=action,
                        execution_id=execution["execution_id"])
        return action

    async def _apply_rules_session_result(self, execution: dict,
                                          reconciliation: dict) -> None:
        """Connect receipt settlement to the owning shared rule session."""
        engine = self.rules_engine
        if engine is None:
            return
        if reconciliation.get("status") == "RECONCILED":
            method = getattr(engine, "record_financial_reconciliation", None)
            if callable(method):
                method(execution, reconciliation)
            return
        method = getattr(engine, "block_unknown_financial_execution", None)
        if callable(method):
            reason = ((reconciliation.get("verification") or {}).get("pending_reason")
                      or reconciliation.get("status") or "reconciliation_pending")
            method(execution, str(reason))

    @staticmethod
    def _ledger(execution, kind, amount, expected, observed, reconciled, status, stamp):
        return {"ledger_id": uuid.uuid4().hex, "execution_id": execution["execution_id"],
                "session_id": execution.get("session_id"), "event_type": kind,
                "amount": amount, "internal_expected_balance": expected,
                "platform_observed_balance": observed, "reconciled_balance": reconciled,
                "balance_status": status, "recorded_at": stamp,
                "simulated": bool(execution.get("simulated"))}
