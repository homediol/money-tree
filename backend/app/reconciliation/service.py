"""Post-execution observation, verification and immutable reconciliation."""
from __future__ import annotations

import copy
import uuid
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable


FINAL = {"RECONCILED", "CLOSED", "FAILED", "EXPIRED", "REJECTED"}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


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
            if not execution.get("simulated") and not evidence:
                # History and browser observations are independent. Recording
                # UNKNOWN here would be immutable and could win a harmless
                # scheduling race. Persist the exact result and finish only
                # after the browser supplies placement/cashout evidence.
                stamp = now_iso()
                execution.update({
                    "status": "RECONCILIATION_PENDING",
                    "lifecycle_state": "RECONCILIATION_PENDING",
                    "updated_at": stamp,
                    "exact_round_result": {"round_id": round_id,
                                           "multiplier": multiplier},
                    "error": "awaiting_platform_evidence",
                })
                self.repository.update_execution(execution)
                self.repository.append_execution_event(
                    execution["execution_id"], "RECONCILIATION_PENDING", stamp,
                    execution["exact_round_result"],
                )
                results.append({
                    "execution_id": execution["execution_id"],
                    "round_id": round_id,
                    "status": "RECONCILIATION_PENDING",
                    "outcome": "UNKNOWN",
                })
                continue
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
        placement_confirmed = bool(execution.get("simulated") or evidence.get("placement_confirmed"))
        cashout_requested = bool(evidence.get("cashout_requested"))
        cashout_executed = bool(evidence.get("cashout_executed"))
        cashout_confirmed = bool(execution.get("simulated") and multiplier >= execution["cashout_target"]
                                 or evidence.get("cashout_confirmed"))
        if not placement_confirmed:
            outcome, pnl = "UNKNOWN", None
        elif multiplier < float(execution["cashout_target"]):
            outcome, pnl = "LOSS", -float(execution["bet_amount"])
        elif cashout_confirmed:
            outcome = "WIN"
            pnl = float(execution["bet_amount"]) * (float(execution["cashout_target"]) - 1)
        else:
            outcome, pnl = "UNKNOWN", None

        before = execution.get("balance_before")
        observed = evidence.get("platform_observed_balance")
        if execution.get("simulated") and observed is None:
            observed = evidence.get("balance_after")
        expected = None if before is None or pnl is None else round(float(before) + pnl, 2)
        reconciled = float(observed) if observed is not None else None
        balance_status = "VERIFIED" if observed is not None else "UNKNOWN"
        if (expected is not None and reconciled is not None
                and abs(expected - reconciled) > 0.01):
            balance_status = "MISMATCH"
        if outcome == "UNKNOWN" or balance_status != "VERIFIED":
            final_status = "UNKNOWN"
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
            "balance_after": reconciled, "profit_loss": pnl if reconciled is not None else None,
            "balance_status": balance_status,
            "error": "verification_incomplete" if final_status == "UNKNOWN" else None,
        })
        reconciliation = {
            "reconciliation_id": uuid.uuid4().hex, "execution_id": execution["execution_id"],
            "round_id": round_id, "status": final_status, "outcome": outcome,
            "created_at": stamp, "requested": {"bet_amount": original["bet_amount"],
                                                  "cashout": original["cashout_target"]},
            "actual": {"multiplier": multiplier, "payout": evidence.get("payout"),
                       "round_id": round_id},
            "verification": {"placement_confirmed": placement_confirmed,
                             "cashout_requested": cashout_requested,
                             "cashout_executed": cashout_executed,
                             "cashout_confirmed": cashout_confirmed},
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
        else:
            ledger.append(self._ledger(execution, "RECONCILIATION", None, expected, observed,
                                       None, balance_status, stamp))
        if not self.repository.save_reconciliation(reconciliation, execution, ledger):
            existing = next((x for x in self.repository.list_reconciliations(1000)
                             if x["execution_id"] == execution["execution_id"]), reconciliation)
            return existing
        self.repository.append_execution_event(execution["execution_id"], final_status, stamp,
                                               reconciliation["verification"])
        # Prediction evaluation is downstream of a verified reconciliation.
        # An exact history multiplier alone is insufficient when placement,
        # cashout, or balance evidence is missing; those outcomes remain
        # UNKNOWN and must never enter the evaluation stream.
        if final_status == "RECONCILED":
            await self._evaluate_prediction(execution, round_id, multiplier, stamp)
        action = await self._post_result(execution, reconciliation)
        reconciliation["risk_action"] = action
        await self.emit("reconciliation:updated", reconciliation=reconciliation)
        await self.emit("execution:updated", execution=execution)
        await self.emit("balance:updated", balance=reconciliation["balance"])
        return reconciliation

    async def _post_result(self, execution: dict, reconciliation: dict) -> str:
        action = "CONTINUE"
        if reconciliation["status"] == "UNKNOWN":
            action = "PAUSE"
        manager = self.betting_manager
        status = manager.status() if manager else {}
        balance = reconciliation["balance"].get("reconciled_balance")
        if balance is not None and manager and manager.session:
            session = manager.session
            session.last_balance = balance
            session.last_balance_text = f"{balance:.0f}BIF"
            if session.goal_balance is not None and balance >= session.goal_balance:
                action = "STOP"
        if action in {"PAUSE", "STOP", "EMERGENCY_STOP"} and manager:
            try:
                await manager.stop_session(reason=("reconciliation_unknown" if action == "PAUSE"
                                                   else "goal_reached"))
            except Exception:
                pass
        await self.emit("risk:post_result", action=action,
                        execution_id=execution["execution_id"])
        return action

    @staticmethod
    def _ledger(execution, kind, amount, expected, observed, reconciled, status, stamp):
        return {"ledger_id": uuid.uuid4().hex, "execution_id": execution["execution_id"],
                "session_id": execution.get("session_id"), "event_type": kind,
                "amount": amount, "internal_expected_balance": expected,
                "platform_observed_balance": observed, "reconciled_balance": reconciled,
                "balance_status": status, "recorded_at": stamp,
                "simulated": bool(execution.get("simulated"))}

    async def _evaluate_prediction(self, execution, round_id, multiplier, stamp):
        decision = next((d for d in self.repository.list_decisions(1000)
                         if d["decision_id"] == execution["decision_id"]), None)
        if not decision:
            return
        prediction = next((p for p in self.repository.list_ml_predictions(1000)
                           if p["prediction_id"] == decision["prediction_id"]), None)
        if not prediction or not self._is_strictly_before(prediction.get("source_round_id"), round_id):
            return
        actual = multiplier >= 2.0
        predicted = float(prediction["probability_2x"]) >= .5
        row = {"evaluation_id": uuid.uuid4().hex, "prediction_id": prediction["prediction_id"],
               "round_id": round_id, "original_probability": prediction["probability_2x"],
               "actual_multiplier": multiplier, "actual_target": actual,
               "evaluation_result": "CORRECT" if predicted == actual else "INCORRECT",
               "evaluated_at": stamp, "prediction_snapshot": copy.deepcopy(prediction)}
        if self.repository.save_prediction_evaluation(row):
            await self.emit("prediction:evaluated", evaluation=row)

    @staticmethod
    def _is_strictly_before(source, target) -> bool:
        try:
            return int(source) < int(target)
        except (TypeError, ValueError):
            return str(source) < str(target)
