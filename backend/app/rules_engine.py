"""Prospective, shadow-only Rules Engine migrated from aviator-shadow-experiment-node.

The registry freezes source semantics by mode.  Engines own their state; the
coordinator is the only component allowed to create a combined selection.
No code in this module can authorize or submit a real-money action.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import json
import hashlib
import re
from threading import RLock
from typing import Any, Mapping
from uuid import uuid4

ENGINE_VERSION = "WINNER_RULES_ENGINE_SHADOW_V1"
STATE_KEY = "rules_engine_state_v1"
WINDOW_SIZE = 50
H_THRESHOLD = 2.0
MODES = {"START_CHECK", "LEGACY_SEPARATE_TRACKER", "FIVE_RULE_TRACKER", "ALL_RULES_TOGETHER"}
COMBINED_POLICY_ID = "USER_PLAYING_POLICY"
START_CHECK_POLICY_ID = "USER_PLAYING_POLICY_START_CHECK"
LEGACY_POLICY_ID = "USER_PLAYING_POLICY_LEGACY_TRACKER"


def _research_definitions() -> list[dict[str, Any]]:
    return [
        {
            "rule_id": "RECENT_5_H_GE_4", "version": "1.0.0",
            "name": "Recent 5: at least 4 H", "mode": "FIVE_RULE_TRACKER|ALL_RULES_TOGETHER",
            "parameters": {}, "target_offset": 1, "history_required": 5,
            "condition": {"window": 5, "outcome": "H", "operator": ">=", "count": 4,
                          "includes_source": True, "latest_source_must_be_h": False},
            "initial_state": {"status": "WARMING_UP", "pending_target": None, "failure_count": 0,
                              "recovery_count": 0},
            "transition_rules": ["Use the latest five consecutive verified completed outcomes including source.",
                                 "TRUE at four or more H; FALSE only when the verified window is ready.",
                                 "Each TRUE requests source round + 1 once; only its own request resolves its state."],
            "reason_rw": "H 4 muri rounds 5: condition yujujwe.",
        },
        {
            "rule_id": "PATTERN_LH", "version": "1.0.0",
            "name": "Pattern LH", "mode": "FIVE_RULE_TRACKER|ALL_RULES_TOGETHER",
            "parameters": {}, "target_offset": 1, "history_required": 2,
            "condition": {"pattern": ["L", "H"], "includes_source": True},
            "initial_state": {"status": "WARMING_UP", "pending_target": None, "failure_count": 0,
                              "recovery_count": 0},
            "transition_rules": ["TRUE exactly for the latest verified pair L,H.",
                                 "Each TRUE requests the immediately following target once."],
            "reason_rw": "Rounds ebyiri ziheruka ni L,H.",
        },
        {
            "rule_id": "PATTERN_HH", "version": "1.0.0",
            "name": "Pattern HH", "mode": "FIVE_RULE_TRACKER|ALL_RULES_TOGETHER",
            "parameters": {}, "target_offset": 1, "history_required": 2,
            "condition": {"pattern": ["H", "H"], "includes_source": True,
                          "reevaluate_each_source": True, "overlap_allowed": True},
            "initial_state": {"status": "WARMING_UP", "pending_target": None, "failure_count": 0,
                              "recovery_count": 0},
            "transition_rules": ["TRUE exactly for the latest verified pair H,H.",
                                 "Repeated HH pairs may request successive distinct targets."],
            "reason_rw": "Rounds ebyiri ziheruka ni H,H.",
        },
        {
            "rule_id": "SOURCE_H_MULT_2_3", "version": "1.0.0",
            "name": "Source multiplier [2, 3)", "mode": "FIVE_RULE_TRACKER|ALL_RULES_TOGETHER",
            "parameters": {}, "target_offset": 1, "history_required": 1,
            "condition": {"minimum_inclusive": 2.0, "maximum_exclusive": 3.0},
            "initial_state": {"status": "WAIT", "pending_target": None, "failure_count": 0,
                              "recovery_count": 0},
            "transition_rules": ["TRUE when the original source multiplier is >=2.00 and <3.00.",
                                 "Each TRUE requests the immediately following target once."],
            "reason_rw": "Igikubo cya source kiri hagati ya 2.00 na 3.00.",
        },
        {
            "rule_id": "AFTER_HH_CONTINUE_MAX_3", "version": "1.0.0",
            "name": "After HH: continue for at most 3 targets", "mode": "FIVE_RULE_TRACKER|ALL_RULES_TOGETHER",
            "parameters": {}, "target_offset": 1, "history_required": 2,
            "condition": {"trigger_pattern": ["H", "H"], "maximum_selections": 3,
                          "ignore_triggers_while_active": True, "stop_on_ww_or_ll": False,
                          "rearm_after_third_resolved_target": True},
            "initial_state": {"cycle_mode": "IDLE", "cycle": None, "pending_target": None,
                              "failure_count": 0, "recovery_count": 0},
            "transition_rules": ["Idle verified HH requests target one and starts a private cycle.",
                                 "After each owned target resolves, request the next consecutive target up to three.",
                                 "Ignore HH while active; do not reset or extend the cycle.",
                                 "Complete and rearm from the third resolved target source regardless of result.",
                                 "Do not apply STANDARD first-WIN or WW/LL stopping."],
            "reason_rw": "HH yabonetse; itegeko risaba target ikurikira.",
        },
    ]


RESEARCH_RULES = _research_definitions()
_research_by_id = {row["rule_id"]: row for row in RESEARCH_RULES}

START_CHECK_POLICY = {
    "rule_id": START_CHECK_POLICY_ID, "version": "AVIATOR_2X_CONSECUTIVE_STOP_V3",
    "name": "Start a Check: source legacy policy", "mode": "START_CHECK",
    "parameters": {"stake_rwf": 100, "cashout_multiplier": 2.0, "window_size": WINDOW_SIZE,
                    "profit_target_rwf": 400, "loss_limit_rwf": 400},
    "target_offset": 1,
    "condition": {"standard_trigger": ["L", "H"], "trigger_h_counts_as_previous_result": True,
                  "standard_stop": ["HH", "LL"], "two_bet_wins_activates_after_two_failed_ll_cycles": True,
                  "two_bet_wins_stop": "two consecutive selected H outcomes"},
    "initial_state": {"rule_state": "WAIT_LOW", "mode": "STANDARD", "previous_outcome": None,
                      "failed_cycle_count": 0, "consecutive_bet_wins": 0, "pending_target": None},
    "transition_rules": ["WAIT_LOW waits for L; WAIT_HIGH waits for H.",
                          "H triggers source+1; legacy reducer seeds previous outcome H.",
                          "STANDARD stops on HH or LL; LL terminal L satisfies the low condition.",
                          "Two consecutive LL-ending STANDARD cycles activate TWO_BET_WINS.",
                          "TWO_BET_WINS stops after two consecutive selected H outcomes; trigger H is not a bet.",
                          "NET +400/-400 stops new selections while the 50-round window continues observing."],
    "source_semantics_note": "Distinct from All Rules Together; preserves rule-reducer.js behavior.",
}

LEGACY_SINGLE_TRACKER_POLICY = {
    "rule_id": LEGACY_POLICY_ID, "version": "AVIATOR_2X_CONSECUTIVE_STOP_V3",
    "name": "Legacy separate tracker: single rule", "mode": "LEGACY_SEPARATE_TRACKER",
    "parameters": {"stake_rwf": 100, "cashout_multiplier": 2.0, "window_size": WINDOW_SIZE,
                    "profit_target_rwf": 400, "loss_limit_rwf": 400},
    "target_offset": 1,
    "condition": deepcopy(START_CHECK_POLICY["condition"]),
    "initial_state": deepcopy(START_CHECK_POLICY["initial_state"]),
    "transition_rules": deepcopy(START_CHECK_POLICY["transition_rules"]),
    "source_semantics_note": "Separate state and shadow ledger; source is live-shadow-test.js.",
}

COMBINED_POLICY = {
    "rule_id": COMBINED_POLICY_ID, "version": "AVIATOR_2X_CONSECUTIVE_STOP_V3",
    "name": "Accepted user playing policy", "mode": "ALL_RULES_TOGETHER",
    "parameters": {}, "target_offset": 1,
    "condition": {"standard_trigger": ["L", "H"], "trigger_h_counts_as_selected_result": False,
                  "standard_first_selected_h_stops": True,
                  "after_first_selected_l_stop_on": ["HH", "LL"],
                  "two_bet_wins_activates_after_two_consecutive_standard_ll_cycles": True,
                  "two_bet_wins_trigger_h_counts_as_selected_result": False,
                  "two_bet_wins_continue_until": ["HH", "LL"],
                  "two_bet_wins_ll_keeps_mode": True, "two_bet_wins_hh_clears_mode": True},
    "initial_state": {"rule_state": "WAIT_LOW", "mode": "STANDARD", "failed_cycle_count": 0,
                      "pending_target": None, "selected_result_streak": []},
    "transition_rules": ["WAIT_LOW waits for L; WAIT_HIGH waits for H.",
                          "H proposes source+1 and is not a selected WIN.",
                          "In STANDARD, the first selected H ends the cycle; after first selected L, continue to HH or LL.",
                          "Two consecutive STANDARD LL cycles activate TWO_BET_WINS; a successful STANDARD cycle resets that streak.",
                          "TWO_BET_WINS keeps mode after LL and clears to STANDARD after selected HH.",
                          "Only this policy's own matching combined selection advances selected-result counters."],
    "source_semantics_note": "Accepted All Rules Together contract; nested mode, not a duplicate rule instance.",
}


def _now(value: str | None = None) -> str:
    stamp = value or datetime.now(timezone.utc).isoformat()
    if not isinstance(stamp, str) or not stamp or _parse_time(stamp) is None:
        raise ValueError("evaluated_at must be an ISO timestamp")
    return stamp


def _parse_time(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, TypeError, ValueError):
        return None


def _multiplier(value: Any) -> float | None:
    if isinstance(value, bool) or value is None or (isinstance(value, str) and not value.strip()):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if result >= 1 and result < float("inf") else None


def classify_outcome(value: Any) -> str | None:
    number = _multiplier(value)
    return None if number is None else ("H" if number >= H_THRESHOLD else "L")


def _identity(event: Mapping[str, Any], *, require_sequence: bool = True) -> dict[str, Any] | None:
    if not isinstance(event, Mapping):
        return None
    multiplier = _multiplier(event.get("multiplier"))
    try:
        index = int(event.get("round_index"))
        sequence = int(event.get("stream_sequence") or 0)
    except (TypeError, ValueError, OverflowError):
        return None
    stream_id = event.get("stream_id")
    round_id = event.get("round_id")
    observation_id = event.get("observation_id") or round_id
    if (not isinstance(stream_id, str) or not stream_id.strip()
            or not isinstance(round_id, str) or not round_id.strip()
            or not isinstance(observation_id, str) or not observation_id.strip()
            or index < 1 or (require_sequence and sequence < 1) or multiplier is None):
        return None
    return {
        "stream_id": stream_id, "stream_sequence": sequence, "round_id": round_id,
        "observation_id": observation_id, "round_index": index,
        "platform_round_id": event.get("platform_round_id"),
        "identity_source": event.get("identity_source"), "multiplier": multiplier,
        "outcome": classify_outcome(multiplier), "continuity_status": event.get("continuity_status"),
        "durability": event.get("durability"), "event_type": event.get("event_type"),
        "observed_at": event.get("observed_at"),
        "stored_at": event.get("stored_at") or event.get("commit_acknowledged_at"),
    }


def _reference(row: Mapping[str, Any]) -> dict[str, Any]:
    return {"streamId": row["stream_id"], "roundId": row["round_id"],
            "observationId": row["observation_id"], "roundIndex": row["round_index"],
            "multiplier": row["multiplier"], "outcome": row["outcome"]}


def _expected(row: Mapping[str, Any]) -> dict[str, Any]:
    return {"streamId": row["stream_id"], "roundId": None, "observationId": None,
            "roundIndex": row["round_index"] + 1,
            "expectedImmediateSuccessorOf": _reference(row)}


def _fresh_engine(rule_id: str) -> dict[str, Any]:
    if rule_id in {START_CHECK_POLICY_ID, LEGACY_POLICY_ID}:
        return {"rule_state": "WAIT_LOW", "mode": "STANDARD", "previous_outcome": None,
                "failed_cycle_count": 0, "consecutive_bet_wins": 0, "pending_selection_id": None,
                "pending_target": None, "selected_outcomes": [], "failure_count": 0, "recovery_count": 0}
    if rule_id == COMBINED_POLICY_ID:
        return {"rule_state": "WAIT_LOW", "mode": "STANDARD", "previous_outcome": None,
                "failed_cycle_count": 0, "consecutive_bet_wins": 0, "pending_selection_id": None,
                "pending_target": None, "selected_outcomes": [], "cycle_number": 0,
                "failure_count": 0, "recovery_count": 0}
    state = {"pending_selection_id": None, "pending_target": None, "failure_count": 0,
             "recovery_count": 0, "selected_wins": 0, "selected_losses": 0}
    if rule_id == "AFTER_HH_CONTINUE_MAX_3":
        state.update({"cycle_mode": "IDLE", "cycle": None})
    return state


def _result(rule_id: str, version: str, session: Mapping[str, Any], source: Mapping[str, Any],
            target: Mapping[str, Any] | None, eligible: bool | None, status: str, reason: str,
            inputs: Mapping[str, Any], before: Mapping[str, Any], after: Mapping[str, Any],
            evaluated_at: str) -> dict[str, Any]:
    return {
        "rule_id": rule_id, "rule_version": version,
        "source_round": _reference(source), "target_round": deepcopy(target),
        "evaluated_at": evaluated_at, "result": eligible, "eligible": eligible,
        "status": status, "reason": reason,
        "state_snapshot": {"version": 1, "before": deepcopy(dict(before)), "after": deepcopy(dict(after))},
        "proposal": {"ruleId": rule_id, "ruleVersion": version, "experimentId": session["session_id"],
                     "sourceRoundRef": _reference(source), "expectedTargetRef": deepcopy(target),
                     "eligible": eligible, "status": status, "reason": reason,
                     "evaluatedAt": evaluated_at, "relevantInputs": deepcopy(dict(inputs)),
                     "privateState": {"before": deepcopy(dict(before)), "after": deepcopy(dict(after))}},
    }


def _history_is_contiguous(history: list[dict[str, Any]], count: int, stream_id: str) -> list[dict[str, Any]] | None:
    tail = history[-count:]
    if len(tail) != count:
        return None
    for i, row in enumerate(tail):
        if row.get("continuity_status") != "VERIFIED" or row.get("outcome") not in {"H", "L"}:
            return None
        if row.get("stream_id") != stream_id:
            return None
        if i and row.get("round_index") != tail[i - 1].get("round_index", 0) + 1:
            return None
    return tail


class RulesEngine:
    """Rule catalogue, independent shadow-session states, and shared coordinator."""

    def __init__(self, persistence: Any | None = None) -> None:
        self._rules: tuple[Any, ...] = ()  # compatibility: no executable real-money rules
        self._lock = RLock()
        self._persistence = persistence
        self.observation_inbox: Any | None = None
        self.last_evaluation: dict[str, Any] | None = None
        self.last_evaluation_at: str | None = None
        self.configuration_version = 0
        self.rule_configuration: dict[str, dict[str, Any]] = {}
        self.presets: dict[str, dict[str, Any]] = {}
        self.sessions: dict[str, dict[str, Any]] = {}
        self.evaluation_count = 0
        if persistence is not None:
            saved = persistence.load_application_state(STATE_KEY)
            if isinstance(saved, dict) and saved.get("engine_version") == ENGINE_VERSION:
                self._restore(saved)
            self.recover_financial_state()

    @staticmethod
    def not_configured(rule_id: str) -> dict[str, Any]:
        return {"rule_id": str(rule_id), "enabled": True, "evaluated_round": None,
                "result": False, "reason": "NOT_CONFIGURED"}

    @staticmethod
    def aggregate(evaluations: Any) -> dict[str, Any]:
        rows = list(evaluations or ())
        # Compatibility for the retired, target-less preview API. This returns
        # only a preview label; it cannot create a prospective selection. The
        # migrated session coordinator below accepts only explicit target refs.
        if rows and all(isinstance(item, Mapping) and "eligible" not in item for item in rows):
            if not all("evaluated_round" in item for item in rows):
                return RulesEngine._no_action(rows, "INVALID_RULE_EVALUATION")
            seen_legacy: set[str] = set()
            true_legacy: list[str] = []
            for item in rows:
                rule_id = item.get("rule_id")
                result = item.get("result")
                if isinstance(rule_id, str) and rule_id in seen_legacy:
                    return RulesEngine._no_action(rows, "INVALID_RULE_ID")
                if (not isinstance(rule_id, str) or not rule_id.strip()
                        or not isinstance(item.get("enabled", True), bool)
                        or not isinstance(result, bool) or not isinstance(item.get("reason"), str)):
                    return RulesEngine._no_action(rows, "INVALID_RULE_EVALUATION")
                seen_legacy.add(rule_id)
                if item.get("enabled", True) and result:
                    true_legacy.append(rule_id)
            enabled_legacy = sum(item.get("enabled", True) is True for item in rows)
            return {"result": bool(true_legacy), "true_rule_ids": true_legacy,
                    "rule_evaluations": deepcopy(rows),
                    "decision": "RULE_MATCH" if true_legacy else "NO_ACTION",
                    "reason": "RULE_MATCH" if true_legacy else "NO_RULE_MATCHED",
                    "rules_configured": len(rows), "rules_enabled": enabled_legacy,
                    "automatic_execution": "OFF", "can_bet": False,
                    "preview_only": True, "selection_created": False}
        seen: set[str] = set()
        enabled: list[dict[str, Any]] = []
        refs: set[str] = set()
        for item in rows:
            if not isinstance(item, Mapping):
                return RulesEngine._no_action(rows, "INVALID_RULE_EVALUATION")
            rule_id = item.get("rule_id")
            if not isinstance(rule_id, str) or not rule_id.strip() or rule_id in seen:
                return RulesEngine._no_action(rows, "INVALID_RULE_ID")
            seen.add(rule_id)
            eligible = item.get("eligible", item.get("result"))
            if (not isinstance(item.get("enabled", True), bool)
                    or (eligible is not None and not isinstance(eligible, bool))
                    or not isinstance(item.get("reason"), str)):
                return RulesEngine._no_action(rows, "INVALID_RULE_EVALUATION")
            if item.get("enabled", True) and eligible is True:
                target = item.get("expectedTargetRef", item.get("target_round"))
                if not isinstance(target, Mapping):
                    return RulesEngine._no_action(rows, "TRUE_WITHOUT_TARGET")
                stream_ref = target.get("streamId", target.get("stream_id"))
                round_ref = target.get("roundIndex", target.get("round_index"))
                if (not isinstance(stream_ref, str) or not stream_ref.strip()
                        or isinstance(round_ref, bool) or not isinstance(round_ref, int) or round_ref < 1):
                    return RulesEngine._no_action(rows, "INVALID_TARGET_IDENTITY")
                refs.add(json.dumps(target, sort_keys=True, separators=(",", ":"), default=str))
                enabled.append(dict(item))
        if len(refs) > 1:
            return RulesEngine._no_action(rows, "TARGET_MISMATCH")
        true_ids = [item["rule_id"] for item in enabled]
        true_ids.extend(item.get("ruleId") for item in rows
                        if isinstance(item, Mapping) and item.get("enabled", True) is True
                        and item.get("eligible", item.get("result")) is True
                        and item.get("ruleId") and item.get("ruleId") not in true_ids)
        n_enabled = sum(1 for item in rows if isinstance(item, Mapping) and item.get("enabled", True) is True)
        decision = "SELECT" if true_ids else ("WAIT" if n_enabled else "NO_ACTION")
        return {"result": bool(true_ids), "true_rule_ids": true_ids, "rule_evaluations": deepcopy(rows),
                "decision": decision,
                "reason": "RULE_MATCHED" if true_ids else ("NO_RULE_MATCHED" if n_enabled else "NOT_CONFIGURED"),
                "rules_configured": len(rows), "rules_enabled": n_enabled,
                "automatic_execution": "OFF", "can_bet": False}

    @staticmethod
    def _no_action(rows: Any, reason: str) -> dict[str, Any]:
        values = list(rows)
        return {"result": False, "true_rule_ids": [], "rule_evaluations": deepcopy(values),
                "decision": "BLOCKED" if reason in {"TARGET_MISMATCH", "TRUE_WITHOUT_TARGET"} else "NO_ACTION",
                "reason": reason, "rules_configured": len(values), "rules_enabled": 0,
                "automatic_execution": "OFF", "can_bet": False}

    def _snapshot(self) -> dict[str, Any]:
        return {"engine_version": ENGINE_VERSION, "configuration_version": self.configuration_version,
                "rule_configuration": deepcopy(self.rule_configuration), "presets": deepcopy(self.presets),
                "sessions": deepcopy(self.sessions), "last_evaluation": deepcopy(self.last_evaluation),
                "last_evaluation_at": self.last_evaluation_at, "evaluation_count": self.evaluation_count}

    def _restore(self, state: dict[str, Any]) -> None:
        self.configuration_version = int(state.get("configuration_version") or 0)
        self.rule_configuration = deepcopy(state.get("rule_configuration") or {})
        self.presets = deepcopy(state.get("presets") or {})
        self.sessions = deepcopy(state.get("sessions") or {})
        self.last_evaluation = deepcopy(state.get("last_evaluation"))
        self.last_evaluation_at = state.get("last_evaluation_at")
        self.evaluation_count = int(state.get("evaluation_count") or 0)

    def _persist(self) -> None:
        if self._persistence is not None:
            snapshot = self._snapshot()
            persist = getattr(self._persistence, "save_rules_engine_snapshot", None)
            if callable(persist):
                persist(snapshot, _now())
            else:
                self._persistence.save_application_state(STATE_KEY, snapshot, _now())

    def _apply_reconciled_financials(self, session: dict[str, Any], execution: Mapping[str, Any],
                                    financials: Mapping[str, Any], at: str) -> None:
        stop = session.setdefault("financial_stop", {
            "status": "ACTIVE", "reason": None, "realized_betting_pl": None,
            "currency": None, "win_count": 0, "loss_count": 0, "reconciled_bets": 0,
            "reconciled_execution_ids": [],
        })
        execution_id = str(execution.get("execution_id") or "")
        ids = list(stop.get("reconciled_execution_ids") or [])
        if execution_id and execution_id not in ids:
            ids.append(execution_id)
        stop.update({"reconciled_execution_ids": ids,
                     "realized_betting_pl": financials.get("realized_betting_pl"),
                     "currency": financials.get("currency"),
                     "currency_status": financials.get("currency_status"),
                     "win_count": int(financials.get("win_count") or 0),
                     "loss_count": int(financials.get("loss_count") or 0),
                     "reconciled_bets": int(financials.get("reconciled_bets") or 0),
                     "updated_at": at})
        selection_id = str(execution.get("decision_id") or "")
        selection = next((item for item in session.get("selections", [])
                          if str(item.get("selection_id")) == selection_id), None)
        if selection is not None:
            selection["financial_result"] = {
                "status": "RECONCILED", "result": execution.get("result"),
                "execution_id": execution_id, "profit_loss": execution.get("profit_loss"),
                "currency": stop.get("currency"), "reconciled_at": at,
            }

        config = session.get("configuration_snapshot") or {}
        reason = None
        win_limit_enabled = config.get("reconciled_win_limit_enabled",
                                       config.get("win_limit_enabled", False))
        win_limit_count = config.get("reconciled_win_limit_count",
                                     config.get("win_limit_count"))
        loss_count_enabled = config.get("reconciled_loss_count_limit_enabled",
                                        config.get("loss_limit_enabled", False))
        loss_count_limit = config.get("reconciled_loss_count_limit",
                                      config.get("loss_limit_count"))
        if win_limit_enabled and stop["win_count"] >= int(win_limit_count or 1):
            reason = "WIN_LIMIT_REACHED"
        elif loss_count_enabled and stop["loss_count"] >= int(loss_count_limit or 1):
            reason = "LOSS_LIMIT_REACHED"
        else:
            profit_enabled = bool(config.get("reconciled_profit_target_enabled",
                                             config.get("profit_target_enabled", False)))
            profit_target = config.get("reconciled_profit_target_rwf",
                                       config.get("profit_target_rwf"))
            legacy_loss_money_enabled = (session.get("mode") != "ALL_RULES_TOGETHER"
                                         and config.get("loss_limit_enabled")
                                         and config.get("loss_limit_rwf") is not None)
            loss_enabled = bool(config.get("reconciled_loss_limit_enabled",
                                           legacy_loss_money_enabled))
            loss_limit = config.get("reconciled_loss_limit_rwf", config.get("loss_limit_rwf"))
            if profit_enabled or loss_enabled:
                expected_currency = ("RWF" if profit_enabled or loss_enabled
                                     else config.get("stake_currency"))
                if (stop["reconciled_bets"] > 0
                        and (stop.get("realized_betting_pl") is None
                             or not expected_currency
                             or stop.get("currency") != expected_currency)):
                    reason = "STOP_CONTROL_CURRENCY_UNVERIFIED"
                elif stop.get("realized_betting_pl") is not None:
                    if (profit_enabled and stop["realized_betting_pl"] >=
                            float(profit_target if profit_target is not None else float("inf"))):
                        reason = "PROFIT_TARGET_REACHED"
                    elif (loss_enabled and stop["realized_betting_pl"] <=
                            -float(loss_limit if loss_limit is not None else float("inf"))):
                        reason = "LOSS_LIMIT_REACHED"

        # A manual, emergency, or already-triggered stop cannot be cleared by
        # a later receipt. Unknown transactions remain paused until a person
        # explicitly resumes after reconciliation.
        if reason:
            stop.update({"status": "STOPPED", "reason": reason, "stopped_at": at})
            if session.get("selection_stop_reason") not in {"MANUAL_STOP", "EMERGENCY_STOP", "RESET"}:
                session.update({"selection_status": "STOPPED", "selection_stop_reason": reason,
                                "selection_stopped_at": at,
                                "selection_stopped_at_round_id": execution.get("target_round_id"),
                                "status": "STOPPED", "stop_reason": reason,
                                "ended_at": at, "auto_next_armed": False})
        elif stop.get("status") == "RECONCILIATION_REQUIRED":
            stop.update({"status": "RECONCILIATION_CLEARED", "reason": None,
                         "reconciled_at": at})

    def record_financial_reconciliation(self, execution: Mapping[str, Any],
                                        reconciliation: Mapping[str, Any], *,
                                        persist: bool = True) -> dict[str, Any]:
        """Apply one receipt-backed real transaction to its shared rule session."""
        if self._persistence is None or execution.get("simulated") is True:
            return {"ok": False, "reason": "real_rule_session_reconciliation_unavailable"}
        record = getattr(self._persistence, "record_rule_execution_reconciliation", None)
        if not callable(record):
            return {"ok": False, "reason": "rule_reconciliation_store_unavailable"}
        saved = record(dict(execution), dict(reconciliation))
        if not saved.get("ok"):
            return saved
        session_id = str(saved.get("session_id") or "")
        with self._lock:
            session = self.sessions.get(session_id)
            if session is None:
                return {"ok": False, "reason": "rule_session_not_restored", "session_id": session_id}
            self._apply_reconciled_financials(
                session, execution, saved.get("financials") or {},
                str(reconciliation.get("created_at") or execution.get("resolved_at") or _now()),
            )
            if persist:
                self._persist()
            stop_snapshot = deepcopy(session.get("financial_stop"))
            result = {"ok": True, "duplicate": bool(saved.get("duplicate")),
                      "session_id": session_id, "financial_stop": stop_snapshot}
        return result

    def block_unknown_financial_execution(self, execution: Mapping[str, Any],
                                          reason: str) -> dict[str, Any]:
        """Pause one rule session for an unresolved transaction without counting it."""
        if self._persistence is None:
            return {"ok": False, "reason": "rule_reconciliation_store_unavailable"}
        mark_pending = getattr(self._persistence, "mark_rule_execution_pending", None)
        if not callable(mark_pending):
            return {"ok": False, "reason": "rule_reconciliation_store_unavailable"}
        intent = mark_pending(dict(execution), reason)
        if not intent:
            return {"ok": False, "reason": "rule_execution_intent_not_found"}
        session_id = str(intent.get("session_id") or "")
        with self._lock:
            session = self.sessions.get(session_id)
            if session is None:
                return {"ok": False, "reason": "rule_session_not_restored", "session_id": session_id}
            stop = session.setdefault("financial_stop", {
                "status": "ACTIVE", "reason": None, "realized_betting_pl": None,
                "currency": None, "win_count": 0, "loss_count": 0, "reconciled_bets": 0,
                "reconciled_execution_ids": [],
            })
            if stop.get("status") != "STOPPED":
                stop.update({"status": "RECONCILIATION_REQUIRED",
                             "reason": "UNKNOWN_TRANSACTION_REQUIRES_RECONCILIATION",
                             "execution_id": execution.get("execution_id"), "blocker": reason,
                             "updated_at": _now()})
                session.update({"status": "PAUSED", "selection_status": "STOPPED",
                                "stop_reason": "UNKNOWN_TRANSACTION_REQUIRES_RECONCILIATION",
                                "selection_stop_reason": "UNKNOWN_TRANSACTION_REQUIRES_RECONCILIATION"})
            self._persist()
            return {"ok": True, "session_id": session_id,
                    "financial_stop": deepcopy(stop)}

    def recover_financial_state(self) -> None:
        """Rebuild reconciled totals and unresolved-transaction blocks after restart."""
        if self._persistence is None:
            return
        list_reconciled = getattr(self._persistence, "list_reconciled_rule_executions", None)
        open_executions = getattr(self._persistence, "open_executions", None)
        get_intent = getattr(self._persistence, "rule_execution_intent", None)
        if not callable(list_reconciled):
            return
        changed = False
        for session_id in list(self.sessions):
            for execution in list_reconciled(session_id):
                result = execution.get("result")
                event = {"status": "RECONCILED", "outcome": result,
                         "round_id": execution.get("target_round_id"),
                         "created_at": execution.get("resolved_at") or _now()}
                applied = self.record_financial_reconciliation(execution, event, persist=False)
                changed = changed or applied.get("ok") is True
        if callable(open_executions) and callable(get_intent):
            for execution in open_executions():
                intent = get_intent(str(execution.get("decision_id") or ""))
                if intent and str(intent.get("session_id") or "") in self.sessions:
                    blocked = self.block_unknown_financial_execution(
                        execution, str(execution.get("error") or "restart_reconciliation_required"))
                    changed = changed or blocked.get("ok") is True
        if changed:
            self._persist()

    def registry(self) -> dict[str, Any]:
        rows = [deepcopy(START_CHECK_POLICY), deepcopy(LEGACY_SINGLE_TRACKER_POLICY),
                *deepcopy(RESEARCH_RULES), deepcopy(COMBINED_POLICY)]
        for row in rows:
            config = self.rule_configuration.get(row["rule_id"], {})
            row["configured"] = config.get("configured", False)
            row["enabled"] = config.get("enabled", False)
            row["configuration"] = deepcopy(config.get("parameters", row.get("parameters", {})))
        return {"engine_version": ENGINE_VERSION, "configuration_version": self.configuration_version,
                "rules": rows, "modes": [
                    {"id": "START_CHECK", "source_section": "Run Controls — Start a Check",
                     "rule_ids": [START_CHECK_POLICY_ID], "window_size": WINDOW_SIZE,
                     "stake_rwf": 100, "stop_semantics": "NET ±400 RWF"},
                    {"id": "LEGACY_SEPARATE_TRACKER", "source_section": "Legacy — Separate Tracker",
                     "rule_ids": [LEGACY_POLICY_ID], "window_size": WINDOW_SIZE,
                     "stop_semantics": "50 verified rounds; one isolated legacy policy state"},
                    {"id": "FIVE_RULE_TRACKER", "source_section": "Five independent rules",
                     "rule_ids": [row["rule_id"] for row in RESEARCH_RULES], "window_size": WINDOW_SIZE,
                     "stop_semantics": "50 verified rounds; independent evaluations feed one shared selection per target"},
                    {"id": "ALL_RULES_TOGETHER", "source_section": "Test — All Rules Together",
                     "rule_ids": [row["rule_id"] for row in RESEARCH_RULES] + [COMBINED_POLICY_ID],
                     "window_size": WINDOW_SIZE, "stop_semantics": "combined WIN/LOSS counts"},
                ]}

    def configure(self, rule_id: str, *, enabled: bool, parameters: Mapping[str, Any] | None = None) -> dict[str, Any]:
        known = {START_CHECK_POLICY_ID, LEGACY_POLICY_ID, COMBINED_POLICY_ID, *_research_by_id}
        if rule_id not in known or not isinstance(enabled, bool):
            raise ValueError("INVALID_RULE_CONFIGURATION")
        allowed = {} if rule_id not in {START_CHECK_POLICY_ID, LEGACY_POLICY_ID} else {
            "profit_target_enabled", "profit_target_rwf", "loss_limit_enabled", "loss_limit_rwf",
            "window_size", "auto_next_check_enabled",
        }
        values = dict(parameters or {})
        if set(values) - set(allowed):
            raise ValueError("UNSUPPORTED_RULE_PARAMETER")
        if rule_id not in {START_CHECK_POLICY_ID, LEGACY_POLICY_ID} and values:
            raise ValueError("RULE_HAS_NO_CONFIGURABLE_PARAMETERS")
        defaults = {"profit_target_enabled": True, "profit_target_rwf": 400,
                    "loss_limit_enabled": True, "loss_limit_rwf": 400,
                    "window_size": WINDOW_SIZE, "auto_next_check_enabled": False}
        if rule_id in {START_CHECK_POLICY_ID, LEGACY_POLICY_ID}:
            defaults.update(values)
            for key in ("profit_target_enabled", "loss_limit_enabled", "auto_next_check_enabled"):
                if not isinstance(defaults[key], bool):
                    raise ValueError("INVALID_BOOLEAN_PARAMETER")
            for enabled_key, amount_key in (("profit_target_enabled", "profit_target_rwf"),
                                            ("loss_limit_enabled", "loss_limit_rwf")):
                if defaults[enabled_key] and (isinstance(defaults[amount_key], bool)
                    or not isinstance(defaults[amount_key], int) or defaults[amount_key] <= 0):
                    raise ValueError("INVALID_NET_LIMIT")
            if (isinstance(defaults["window_size"], bool) or not isinstance(defaults["window_size"], int)
                    or not 1 <= defaults["window_size"] <= 100000):
                raise ValueError("INVALID_WINDOW_SIZE")
        with self._lock:
            self.configuration_version += 1
            self.rule_configuration[rule_id] = {"configured": True, "enabled": enabled,
                "version": self.configuration_version,
                "parameters": defaults if rule_id in {START_CHECK_POLICY_ID, LEGACY_POLICY_ID} else {}}
            self._persist()
            return deepcopy(self.rule_configuration[rule_id])

    def save_preset(self, name: str, mode: str, rule_ids: list[str], options: Mapping[str, Any] | None = None) -> dict[str, Any]:
        if not isinstance(name, str) or not name.strip() or mode not in MODES:
            raise ValueError("INVALID_PRESET")
        valid = self._allowed_rules(mode)
        if (not isinstance(rule_ids, list) or len(set(rule_ids)) != len(rule_ids)
                or any(rule_id not in valid for rule_id in rule_ids)):
            raise ValueError("INVALID_PRESET_RULES")
        value = {"name": name.strip(), "mode": mode, "rule_ids": list(rule_ids),
                 "options": deepcopy(dict(options or {})), "configuration_version": self.configuration_version,
                 "saved_at": _now()}
        with self._lock:
            self.presets[value["name"]] = value
            self._persist()
        return deepcopy(value)

    @staticmethod
    def _allowed_rules(mode: str) -> set[str]:
        if mode == "START_CHECK":
            return {START_CHECK_POLICY_ID}
        if mode == "LEGACY_SEPARATE_TRACKER":
            return {LEGACY_POLICY_ID}
        if mode == "FIVE_RULE_TRACKER":
            return set(_research_by_id)
        if mode == "ALL_RULES_TOGETHER":
            return {*_research_by_id, COMBINED_POLICY_ID}
        return set()

    def start_session(self, *, mode: str, rule_ids: list[str], boundary: Mapping[str, Any],
                      prestart_context: list[Mapping[str, Any]] | None = None,
                      options: Mapping[str, Any] | None = None, now: str | None = None) -> dict[str, Any]:
        now = _now(now)
        if mode not in MODES or not isinstance(rule_ids, list) or len(set(rule_ids)) != len(rule_ids):
            raise ValueError("INVALID_SESSION_CONFIGURATION")
        allowed = self._allowed_rules(mode)
        if any(rule_id not in allowed for rule_id in rule_ids):
            raise ValueError("RULE_NOT_ALLOWED_IN_MODE")
        for rule_id in rule_ids:
            configured = self.rule_configuration.get(rule_id)
            if not configured or configured.get("configured") is not True or configured.get("enabled") is not True:
                raise ValueError("RULE_NOT_ENABLED")
        if not rule_ids:
            # A zero-rule session is legal and reports NO_ACTION; it never selects.
            pass
        start = _identity(boundary)
        if not start or boundary.get("event_type") != "round.persisted" or boundary.get("durability") != "COMMITTED" \
                or boundary.get("continuity_status") != "VERIFIED":
            raise ValueError("VERIFIED_COMMITTED_BOUNDARY_REQUIRED")
        opts = dict(options or {})
        window_size = opts.get("window_size", WINDOW_SIZE)
        if mode in {"START_CHECK", "LEGACY_SEPARATE_TRACKER"}:
            expected_policy = START_CHECK_POLICY_ID if mode == "START_CHECK" else LEGACY_POLICY_ID
            if rule_ids not in ([], [expected_policy]):
                raise ValueError("SINGLE_POLICY_MODE_ACCEPTS_ONLY_ITS_POLICY")
            configured = self.rule_configuration.get(expected_policy, {}).get("parameters", {})
            defaults = {"window_size": WINDOW_SIZE, "stake_rwf": 100, "cashout_multiplier": 2.0,
                        "profit_target_enabled": True, "profit_target_rwf": 400,
                        "loss_limit_enabled": True, "loss_limit_rwf": 400,
                        "auto_next_check_enabled": False, "execution_panel": None,
                        "stake_currency": "RWF"}
            defaults.update(configured)
            defaults.update(opts)
            opts = defaults
            if opts["stake_rwf"] != 100 or opts["cashout_multiplier"] != 2.0:
                raise ValueError("START_CHECK_FIXED_STAKE_AND_CASHOUT")
            for key in ("profit_target_enabled", "loss_limit_enabled", "auto_next_check_enabled"):
                if not isinstance(opts.get(key), bool):
                    raise ValueError("INVALID_BOOLEAN_PARAMETER")
            if opts["profit_target_enabled"] and (isinstance(opts["profit_target_rwf"], bool)
                    or not isinstance(opts["profit_target_rwf"], int) or opts["profit_target_rwf"] <= 0):
                raise ValueError("INVALID_NET_LIMIT")
            if opts["loss_limit_enabled"] and (isinstance(opts["loss_limit_rwf"], bool)
                    or not isinstance(opts["loss_limit_rwf"], int) or opts["loss_limit_rwf"] <= 0):
                raise ValueError("INVALID_NET_LIMIT")
        elif mode == "ALL_RULES_TOGETHER":
            if "fixed_stake_rwf" not in opts:
                opts["fixed_stake_rwf"] = 100
            opts.setdefault("stake_currency", "RWF")
            if isinstance(opts["fixed_stake_rwf"], bool) or not isinstance(opts["fixed_stake_rwf"], int) or opts["fixed_stake_rwf"] < 1:
                raise ValueError("INVALID_FIXED_STAKE")
            if opts["stake_currency"] != "RWF":
                raise ValueError("FIXED_STAKE_RWF_CURRENCY_MUST_BE_RWF")
            for key in ("win_limit_enabled", "loss_limit_enabled", "auto_next_check_enabled"):
                opts.setdefault(key, False)
                if not isinstance(opts[key], bool):
                    raise ValueError("INVALID_BOOLEAN_PARAMETER")
            max_selections = opts.get("max_selections")
            if max_selections is not None and (isinstance(max_selections, bool)
                    or not isinstance(max_selections, int) or max_selections < 1):
                raise ValueError("INVALID_MAX_SELECTIONS")
            for enabled_key, amount_key in (("win_limit_enabled", "win_limit_count"),
                                            ("loss_limit_enabled", "loss_limit_count")):
                if opts[enabled_key]:
                    count = opts.get(amount_key)
                    if isinstance(count, bool) or not isinstance(count, int) or count < 1:
                        raise ValueError("INVALID_RESULT_COUNT_LIMIT")
                else:
                    opts[amount_key] = None
        else:
            retained = dict(opts)
            opts = {"window_size": retained.get("max_observed_rounds", window_size),
                    "execution_panel": retained.get("execution_panel", 0),
                    "stake_currency": retained.get("stake_currency")}
            for key in ("reconciled_win_limit_enabled", "reconciled_win_limit_count",
                        "reconciled_loss_count_limit_enabled", "reconciled_loss_count_limit",
                        "reconciled_profit_target_enabled", "reconciled_profit_target_rwf",
                        "reconciled_loss_limit_enabled", "reconciled_loss_limit_rwf"):
                if key in retained:
                    opts[key] = retained[key]
        execution_panel = opts.get("execution_panel")
        if execution_panel is not None and (isinstance(execution_panel, bool) or execution_panel not in (0, 1)):
            raise ValueError("INVALID_EXECUTION_PANEL")
        opts["execution_panel"] = execution_panel
        if "stake_currency" in opts and opts["stake_currency"] is not None:
            currency = str(opts["stake_currency"]).strip().upper()
            if not re.fullmatch(r"[A-Z]{3}", currency):
                raise ValueError("INVALID_STAKE_CURRENCY")
            opts["stake_currency"] = currency
        if (("fixed_stake_rwf" in opts or "stake_rwf" in opts)
                and opts.get("stake_currency") != "RWF"):
            raise ValueError("RWF_STAKE_FIELD_CURRENCY_MUST_BE_RWF")
        # These controls are evaluated from receipt and balance reconciliations.
        # The existing shadow-result limits above remain separate and keep their
        # established semantics.
        reconciled_controls = (
            ("reconciled_win_limit_enabled", "reconciled_win_limit_count"),
            ("reconciled_loss_count_limit_enabled", "reconciled_loss_count_limit"),
        )
        for enabled_key, count_key in reconciled_controls:
            if enabled_key in opts and not isinstance(opts[enabled_key], bool):
                raise ValueError("INVALID_BOOLEAN_PARAMETER")
            if opts.get(enabled_key):
                count = opts.get(count_key)
                if isinstance(count, bool) or not isinstance(count, int) or count < 1:
                    raise ValueError("INVALID_RESULT_COUNT_LIMIT")
            elif enabled_key in opts:
                opts[count_key] = None
        for enabled_key, amount_key in (
                ("reconciled_profit_target_enabled", "reconciled_profit_target_rwf"),
                ("reconciled_loss_limit_enabled", "reconciled_loss_limit_rwf")):
            if enabled_key in opts and not isinstance(opts[enabled_key], bool):
                raise ValueError("INVALID_BOOLEAN_PARAMETER")
            if opts.get(enabled_key) and (isinstance(opts.get(amount_key), bool)
                    or not isinstance(opts.get(amount_key), int) or opts[amount_key] <= 0):
                raise ValueError("INVALID_NET_LIMIT")
        window_size = opts.get("max_observed_rounds", opts.get("window_size", WINDOW_SIZE))
        if isinstance(window_size, bool) or not isinstance(window_size, int) or not 1 <= window_size <= 100000:
            raise ValueError("INVALID_WINDOW_SIZE")
        context: list[dict[str, Any]] = []
        for item in prestart_context or []:
            row = _identity(item, require_sequence=False)
            if (row and item.get("continuity_status") == "VERIFIED" and row["stream_id"] == start["stream_id"]):
                if context and row["round_index"] != context[-1]["round_index"] + 1:
                    context = []
                context.append(row)
        context = context[-5:]
        synchronization_point = context[-1] if context and context[-1]["round_index"] > start["round_index"] else start
        session_id = str(uuid4())
        engines = {rule_id: _fresh_engine(rule_id) for rule_id in rule_ids}
        snapshot_version = max(1, self.configuration_version)
        session = {
            "session_id": session_id, "mode": mode, "status": "RUNNING", "selection_status": "ACTIVE",
            "started_at": now, "ended_at": None, "window_size": window_size,
            "configuration_version": snapshot_version, "configuration_snapshot": deepcopy(opts),
            "rule_ids": list(rule_ids), "rule_versions": {rid: self._version(rid) for rid in rule_ids},
            "manifest": self._manifest(rule_ids),
            "start_boundary": _reference(start) | {"streamSequence": start["stream_sequence"]},
            "synchronization_point": _reference(synchronization_point),
            "last_stream_id": start["stream_id"], "last_stream_sequence": start["stream_sequence"],
            "last_round_index": synchronization_point["round_index"],
            "last_round_id": synchronization_point["round_id"],
            "context": context, "engines": engines, "observed_rounds": 0,
            "prestart_skipped_rounds": 0,
            "selections": [], "rule_selections": {rid: [] for rid in rule_ids},
            "evaluations": [], "decisions": [], "total_wins": 0, "total_losses": 0,
            "net_profit_rwf": 0, "selection_stop_reason": None, "stop_reason": None,
            "financial_stop": {"status": "ACTIVE", "reason": None,
                               "realized_betting_pl": None, "currency": None,
                               "win_count": 0, "loss_count": 0, "reconciled_bets": 0,
                               "reconciled_execution_ids": []},
            "auto_next_armed": False, "automatic_execution": "OFF",
        }
        with self._lock:
            if any(existing.get("mode") == mode
                   and existing.get("last_stream_id") == start["stream_id"]
                   and existing.get("status") in {"RUNNING", "PAUSED"}
                   and existing.get("selection_status") == "ACTIVE"
                   for existing in self.sessions.values()):
                raise ValueError("SESSION_ALREADY_ACTIVE_FOR_MODE")
            # A manually started run owns the next boundary.  Do not also
            # launch a previously armed automatic continuation for that mode.
            for existing in self.sessions.values():
                if (existing.get("mode") == mode
                        and existing.get("last_stream_id") == start["stream_id"]
                        and existing.get("auto_next_armed")):
                    existing["auto_next_armed"] = False
                    existing["auto_next_block_reason"] = "MANUAL_SESSION_STARTED"
            self.sessions[session_id] = session
            self._persist()
        return deepcopy(session)

    def _version(self, rule_id: str) -> str:
        if rule_id in {START_CHECK_POLICY_ID, LEGACY_POLICY_ID}:
            return (START_CHECK_POLICY["version"] if rule_id == START_CHECK_POLICY_ID
                    else LEGACY_SINGLE_TRACKER_POLICY["version"])
        if rule_id == COMBINED_POLICY_ID:
            return COMBINED_POLICY["version"]
        return _research_by_id[rule_id]["version"]

    @staticmethod
    def _definition(rule_id: str) -> dict[str, Any]:
        if rule_id == START_CHECK_POLICY_ID:
            return deepcopy(START_CHECK_POLICY)
        if rule_id == LEGACY_POLICY_ID:
            return deepcopy(LEGACY_SINGLE_TRACKER_POLICY)
        if rule_id == COMBINED_POLICY_ID:
            return deepcopy(COMBINED_POLICY)
        return deepcopy(_research_by_id[rule_id])

    def _manifest(self, rule_ids: list[str]) -> dict[str, Any]:
        entries = [self._definition(rule_id) for rule_id in rule_ids]
        encoded = json.dumps(entries, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return {"manifest_version": "WINNER_RULES_MANIFEST_V1", "rules": entries,
                "sha256": hashlib.sha256(encoded.encode("utf-8")).hexdigest()}

    def _resolve(self, session: dict[str, Any], source: dict[str, Any], at: str) -> None:
        target_index = source["round_index"]
        stream_id = source["stream_id"]
        combined = next((item for item in session["selections"]
                         if item["status"] == "PENDING" and item["expected_target"]["streamId"] == stream_id
                         and item["expected_target"]["roundIndex"] == target_index), None)
        if combined:
            outcome = source["outcome"]
            result = "WIN" if outcome == "H" else "LOSS"
            combined.update({"status": result, "result": result, "actual_multiplier": source["multiplier"],
                             "actual_target": _reference(source), "resolved_at": at,
                             "target_persistence_status": source.get("durability", "UNKNOWN")})
            session["total_wins"] += result == "WIN"
            session["total_losses"] += result == "LOSS"
            stake = session["configuration_snapshot"].get("fixed_stake_rwf", 100)
            session["net_profit_rwf"] += stake if result == "WIN" else -stake
            for rule_id in combined["contributing_rule_ids"]:
                state = session["engines"][rule_id]
                if state.get("pending_selection_id") == combined["selection_id"]:
                    self._advance_selected_state(session, rule_id, state, source, result, combined, at)
                    state["pending_selection_id"] = None
                    state["pending_target"] = None
            config = session["configuration_snapshot"]
            if session["mode"] == "ALL_RULES_TOGETHER":
                if config.get("win_limit_enabled") and session["total_wins"] >= config.get("win_limit_count", 1):
                    self._stop_selecting(session, "WIN_LIMIT_REACHED", at, source)
                if config.get("loss_limit_enabled") and session["total_losses"] >= config.get("loss_limit_count", 1):
                    self._stop_selecting(session, "LOSS_LIMIT_REACHED", at, source)
                if config.get("max_selections") and len(session["selections"]) >= config["max_selections"]:
                    self._stop_selecting(session, "MAX_SELECTIONS_REACHED", at, source)
            if session["mode"] in {"START_CHECK", "LEGACY_SEPARATE_TRACKER"}:
                limit = session["configuration_snapshot"]
                if limit.get("profit_target_enabled") and session["net_profit_rwf"] >= limit.get("profit_target_rwf", 400):
                    self._stop_selecting(session, "PROFIT_TARGET_REACHED", at, source)
                if limit.get("loss_limit_enabled") and session["net_profit_rwf"] <= -limit.get("loss_limit_rwf", 400):
                    self._stop_selecting(session, "LOSS_LIMIT_REACHED", at, source)
        for rule_id, selections in session["rule_selections"].items():
            selection = next((item for item in selections if item["status"] == "PENDING"
                              and item["expected_target"]["streamId"] == stream_id
                              and item["expected_target"]["roundIndex"] == target_index), None)
            if selection:
                result = "WIN" if source["outcome"] == "H" else "LOSS"
                selection.update({"status": result, "result": result, "actual_multiplier": source["multiplier"],
                                  "actual_target": _reference(source), "resolved_at": at,
                                  "target_persistence_status": source.get("durability", "UNKNOWN")})
                state = session["engines"][rule_id]
                if state.get("pending_selection_id") == selection["selection_id"]:
                    self._advance_selected_state(session, rule_id, state, source, result, selection, at)
                    state["pending_selection_id"] = None
                    state["pending_target"] = None

    def _advance_selected_state(self, session: dict[str, Any], rule_id: str, state: dict[str, Any],
                                source: dict[str, Any], result: str, selection: dict[str, Any], at: str) -> None:
        state["selected_wins"] = int(state.get("selected_wins", 0)) + (result == "WIN")
        state["selected_losses"] = int(state.get("selected_losses", 0)) + (result == "LOSS")
        outcome = "H" if result == "WIN" else "L"
        if rule_id == "AFTER_HH_CONTINUE_MAX_3":
            cycle = state.get("cycle")
            if cycle and cycle.get("active") and cycle.get("cycle_id") == selection.get("cycle_id"):
                cycle["resolved_count"] += 1
                cycle["last_result"] = result
                if cycle["resolved_count"] >= 3:
                    cycle["active"] = False
                    cycle["status"] = "COMPLETED"
                    state["cycle_mode"] = "IDLE"
            return
        if rule_id not in {START_CHECK_POLICY_ID, LEGACY_POLICY_ID, COMBINED_POLICY_ID}:
            return
        state["selected_outcomes"].append(outcome)
        special = state["mode"] == "TWO_BET_WINS"
        previous = state.get("previous_outcome")
        if not special and rule_id in {START_CHECK_POLICY_ID, LEGACY_POLICY_ID}:
            previous = previous or "H"  # exact legacy reducer seed from trigger H
            if previous == outcome:
                if outcome == "H":
                    state["rule_state"] = "WAIT_LOW"
                    state["failed_cycle_count"] = 0
                    state["mode"] = "STANDARD"
                else:
                    state["rule_state"] = "WAIT_HIGH"
                    state["failed_cycle_count"] = int(state.get("failed_cycle_count", 0)) + 1
                    if state["failed_cycle_count"] >= 2:
                        state["mode"] = "TWO_BET_WINS"
                state["previous_outcome"] = None
                state["selected_outcomes"] = []
                return
            state["previous_outcome"] = outcome
            return
        if not special:
            if len(state["selected_outcomes"]) == 1 and outcome == "H":
                state.update({"rule_state": "WAIT_LOW", "previous_outcome": None,
                              "failed_cycle_count": 0, "selected_outcomes": []})
                return
            if previous == outcome:
                if outcome == "H":
                    state.update({"rule_state": "WAIT_LOW", "previous_outcome": None,
                                  "failed_cycle_count": 0, "selected_outcomes": []})
                else:
                    state.update({"rule_state": "WAIT_HIGH", "previous_outcome": None,
                                  "failed_cycle_count": int(state.get("failed_cycle_count", 0)) + 1,
                                  "selected_outcomes": []})
                    if state["failed_cycle_count"] >= 2:
                        state["mode"] = "TWO_BET_WINS"
                return
            state["previous_outcome"] = outcome
            return
        if rule_id in {START_CHECK_POLICY_ID, LEGACY_POLICY_ID}:
            streak = int(state.get("consecutive_bet_wins", 0)) + 1 if outcome == "H" else 0
            state["previous_outcome"] = outcome
            state["consecutive_bet_wins"] = streak
            if streak >= 2:
                state.update({"rule_state": "WAIT_LOW", "mode": "STANDARD", "previous_outcome": None,
                              "failed_cycle_count": 0, "consecutive_bet_wins": 0, "selected_outcomes": []})
            return
        if previous == outcome:
            if outcome == "H":
                state.update({"rule_state": "WAIT_LOW", "mode": "STANDARD", "previous_outcome": None,
                              "failed_cycle_count": 0, "consecutive_bet_wins": 0, "selected_outcomes": []})
            else:
                state.update({"rule_state": "WAIT_HIGH", "previous_outcome": None,
                              "selected_outcomes": []})
            return
        state["previous_outcome"] = outcome
        state["consecutive_bet_wins"] = int(state.get("consecutive_bet_wins", 0)) + 1 if outcome == "H" else 0

    def _stop_selecting(self, session: dict[str, Any], reason: str, at: str, source: Mapping[str, Any]) -> None:
        if session.get("selection_status") == "ACTIVE":
            session.update({"selection_status": "STOPPED", "selection_stop_reason": reason,
                            "selection_stopped_at": at,
                            "selection_stopped_at_round_id": source.get("round_id")})

    def _proposal(self, session: dict[str, Any], rid: str, source: dict[str, Any],
                  history: list[dict[str, Any]], at: str) -> dict[str, Any]:
        state = session["engines"][rid]
        before = deepcopy(state)
        expected = _expected(source)
        if state.get("pending_selection_id"):
            reason = "Target yasabwe n'iri tegeko ntirakemuwe." if rid not in {START_CHECK_POLICY_ID, LEGACY_POLICY_ID, COMBINED_POLICY_ID} else "Rule irategereje target yayo."
            return _result(rid, session["rule_versions"][rid], session, source, expected, None,
                           "PENDING_TARGET", reason, {"pendingTarget": state.get("pending_target")}, before, state, at)
        if rid in {START_CHECK_POLICY_ID, LEGACY_POLICY_ID, COMBINED_POLICY_ID}:
            return self._policy_proposal(session, rid, source, expected, at, before)
        definition = _research_by_id[rid]
        tail = _history_is_contiguous(history, definition["history_required"], source["stream_id"])
        if tail is None:
            after = deepcopy(state)
            reason = "Amakuru akurikirana ntarahagije."
            return _result(rid, definition["version"], session, source, expected, None,
                           "UNAVAILABLE_HISTORY", reason, {"historyRequired": definition["history_required"]},
                           before, after, at)
        values = [item["outcome"] for item in tail]
        matched = False
        cycle_id = None
        if rid == "RECENT_5_H_GE_4":
            matched = values.count("H") >= 4
            reason = f"H {values.count('H')} muri rounds 5: condition yujujwe." if matched else f"H {values.count('H')} muri rounds 5; condition ntiyujujwe."
        elif rid == "PATTERN_LH":
            matched = values == ["L", "H"]
            reason = "Rounds ebyiri ziheruka ni L,H." if matched else "Rounds ebyiri ziheruka si L,H."
        elif rid == "PATTERN_HH":
            matched = values == ["H", "H"]
            reason = "Rounds ebyiri ziheruka ni H,H." if matched else "Rounds ebyiri ziheruka si H,H."
        elif rid == "SOURCE_H_MULT_2_3":
            number = source["multiplier"]
            matched = 2.0 <= number < 3.0
            reason = "Igikubo cya source kiri hagati ya 2.00 na 3.00." if matched else "Igikubo cya source kiri hanze ya [2.00, 3.00)."
        else:
            cycle = state.get("cycle")
            if cycle and cycle.get("active"):
                matched = int(cycle.get("requested_count", 0)) < 3
                cycle_id = cycle.get("cycle_id")
                reason = "Cycle ya HH irasaba target ikurikiraho." if matched else "Cycle ya HH yarasabiye targets eshatu."
            else:
                matched = values == ["H", "H"]
                reason = "HH yabonetse; itegeko risaba target ikurikira." if matched else "HH ntiraboneka ku source."
        inputs = {"history": [{"roundId": item["round_id"], "roundIndex": item["round_index"],
                                "multiplier": item["multiplier"], "outcome": item["outcome"]} for item in tail],
                  "condition": deepcopy(definition["condition"])}
        if rid == "AFTER_HH_CONTINUE_MAX_3":
            inputs["continuationCycle"] = deepcopy(state.get("cycle"))
        return _result(rid, definition["version"], session, source, expected, matched,
                       "TRUE" if matched else "FALSE", reason, inputs, before, state, at)

    def _policy_proposal(self, session: dict[str, Any], rid: str, source: dict[str, Any],
                         expected: dict[str, Any], at: str, before: dict[str, Any]) -> dict[str, Any]:
        state = session["engines"][rid]
        outcome = source["outcome"]
        mode = state["mode"]
        matched = False
        reason = "Rule iracyategereje L."
        transition: dict[str, Any] | None = None
        after = deepcopy(state)
        if state["rule_state"] == "WAIT_LOW":
            if outcome == "L":
                matched = False
                reason = "Rule irategereje H nyuma ya L." if rid in {START_CHECK_POLICY_ID, LEGACY_POLICY_ID} else "L yabonetse; itegereje H."
                after["rule_state"] = "WAIT_HIGH"
        elif state["rule_state"] == "WAIT_HIGH":
            if outcome == "H":
                matched = True
                reason = "L,H yujujwe; H isaba target ikurikira." if rid == COMBINED_POLICY_ID else "L,H yujujwe; H isaba target ikurikira."
                transition = {"type": "TRIGGER"}
        elif state["rule_state"] == "PLAYING":
            matched = True
            reason = "Policy ikomeje target zikurikirana; H/L yatoranyijwe ni yo ibarwa." \
                if rid == COMBINED_POLICY_ID else "Cycle iracyakora; source yasabye target ikurikiraho."
            transition = {"type": "CONTINUE"}
        return _result(rid, session["rule_versions"][rid], session, source, expected, matched,
                       "TRUE" if matched else "FALSE", reason,
                       {"sourceOutcome": outcome, "privateMode": mode, "transition": transition},
                       before, after, at)

    def _commit_contributor(self, session: dict[str, Any], proposal: dict[str, Any], selection: dict[str, Any]) -> None:
        rid = proposal["rule_id"]
        state = session["engines"][rid]
        target = deepcopy(selection["expected_target"])
        target["contributorRuleId"] = rid
        state["pending_selection_id"] = selection["selection_id"]
        state["pending_target"] = target
        if rid == "AFTER_HH_CONTINUE_MAX_3":
            cycle = state.get("cycle")
            if not cycle or not cycle.get("active"):
                cycle = {"cycle_id": str(uuid4()), "active": True, "status": "ACTIVE",
                         "requested_count": 0, "resolved_count": 0, "started_at": selection["selected_at"]}
                state["cycle"] = cycle
                state["cycle_mode"] = "CONTINUE_MAX_3"
            cycle["requested_count"] += 1
            cycle["last_source_round_id"] = selection["source_round"]["roundId"]
            selection["cycle_id"] = cycle["cycle_id"]
        elif rid in {START_CHECK_POLICY_ID, LEGACY_POLICY_ID, COMBINED_POLICY_ID}:
            relevant = proposal["proposal"].get("relevantInputs", {})
            if relevant.get("transition", {}).get("type") == "TRIGGER":
                state["rule_state"] = "PLAYING"
                state["previous_outcome"] = "H" if rid in {START_CHECK_POLICY_ID, LEGACY_POLICY_ID} else None
                state["selected_outcomes"] = []
                state["cycle_number"] = int(state.get("cycle_number", 0)) + 1
            elif relevant.get("transition", {}).get("type") == "CONTINUE":
                state["rule_state"] = "PLAYING"

    def observe_round(self, event: Mapping[str, Any], *, now: str | None = None,
                      target_already_completed: bool = False) -> dict[str, Any]:
        """Consume a verified observed or committed event, preserving its two stages."""
        at = _now(now)
        if not isinstance(event, Mapping):
            event = {}
        observed_stage = (event.get("event_type") == "round.observed" and event.get("durability") == "PENDING")
        committed_stage = (event.get("event_type") == "round.persisted" and event.get("durability") == "COMMITTED")
        row = _identity(event, require_sequence=not observed_stage)
        trusted = bool(row and (observed_stage or committed_stage)
                       and event.get("continuity_status") == "VERIFIED")
        with self._lock:
            if not trusted:
                status = ("UNAVAILABLE_CONTINUITY" if event.get("continuity_status") != "VERIFIED" else
                          "UNAVAILABLE_INVALID_INPUT" if _multiplier(event.get("multiplier")) is None
                          else "UNAVAILABLE_NOT_COMMITTED" if event.get("event_type") != "round.persisted"
                          or event.get("durability") != "COMMITTED" else "UNAVAILABLE_IDENTITY")
                reason = ("Amakuru akurikirana ntarahagije." if status == "UNAVAILABLE_CONTINUITY"
                          else "Multiplier cyangwa identity ntiyemejwe." if status != "UNAVAILABLE_NOT_COMMITTED"
                          else "Round itarangiye kandi ngo ibikwe ntisuzumwa.")
                invalid_ref = {"streamId": event.get("stream_id"), "roundId": event.get("round_id"),
                               "observationId": event.get("observation_id"),
                               "roundIndex": event.get("round_index"), "multiplier": event.get("multiplier"),
                               "outcome": None}
                sessions = []
                for session in self.sessions.values():
                    if session.get("status") not in {"RUNNING", "PAUSED"}:
                        continue
                    evaluations = []
                    for rid in session["rule_ids"]:
                        before = deepcopy(session["engines"][rid])
                        result = _result(rid, session["rule_versions"][rid], session, {
                            "stream_id": str(event.get("stream_id") or ""),
                            "round_id": str(event.get("round_id") or ""),
                            "observation_id": str(event.get("observation_id") or event.get("round_id") or ""),
                            "round_index": int(event.get("round_index") or 0),
                            "multiplier": _multiplier(event.get("multiplier")) or 0.0,
                            "outcome": None,
                        }, None, None, status, reason, {"eventType": event.get("event_type"),
                            "durability": event.get("durability"),
                            "continuityStatus": event.get("continuity_status")},
                            before, before, at)
                        result["source_round"] = invalid_ref
                        result["proposal"]["sourceRoundRef"] = invalid_ref
                        evaluations.append(result)
                    sessions.append({"session_id": session["session_id"], "rule_evaluations": evaluations,
                                     "decision": {"action": "BLOCKED", "reason": status,
                                                  "true_rule_ids": [], "selection_id": None}})
                evaluation = {"decision": "BLOCKED", "result": False, "reason": status,
                              "source_round": invalid_ref, "rule_evaluations": [], "true_rule_ids": [],
                              "sessions": sessions, "automatic_execution": "OFF", "can_bet": False,
                              "evaluated_at": at}
                self.last_evaluation, self.last_evaluation_at = evaluation, at
                self._persist()
                return deepcopy(evaluation)
            self._spawn_auto_next_sessions(row, at)
            record_key = (f"pending:{row['stream_id']}:{row['round_id']}" if observed_stage
                          else f"{row['stream_id']}:{row['stream_sequence']}")
            results: list[dict[str, Any]] = []
            for session in list(self.sessions.values()):
                committed_key = f"{row['stream_id']}:{row['stream_sequence']}"
                pending_key = f"pending:{row['stream_id']}:{row['round_id']}"
                if committed_stage:
                    committed_prior = session.get("event_records", {}).get(committed_key)
                    pending_prior = session.get("event_records", {}).get(pending_key)
                    if committed_prior:
                        if committed_prior.get("source_round") != _reference(row):
                            session.update({"status": "PAUSED", "stop_reason": "ROUND_IDENTITY_CONFLICT"})
                        results.append(deepcopy(committed_prior))
                        continue
                    if pending_prior:
                        if pending_prior.get("source_round") != _reference(row):
                            session.update({"status": "PAUSED", "stop_reason": "OBSERVED_COMMIT_IDENTITY_CONFLICT"})
                            continue
                        pending_prior["persistence_status"] = "COMMITTED"
                        pending_prior["committed_stream_sequence"] = row["stream_sequence"]
                        session["event_records"][committed_key] = pending_prior
                        for saved_evaluation in session.get("evaluations", []):
                            if (saved_evaluation.get("source_round") == pending_prior.get("source_round")
                                    and saved_evaluation.get("evaluated_at") == pending_prior.get("evaluated_at")):
                                saved_evaluation["persistence_status"] = "COMMITTED"
                                saved_evaluation["committed_stream_sequence"] = row["stream_sequence"]
                        session["last_stream_sequence"] = max(int(session.get("last_stream_sequence", 0)), row["stream_sequence"])
                        for selection in session.get("selections", []):
                            if (selection.get("actual_target") or {}).get("roundId") == row["round_id"]:
                                selection["target_persistence_status"] = "COMMITTED"
                        for selections in session.get("rule_selections", {}).values():
                            for selection in selections:
                                if (selection.get("actual_target") or {}).get("roundId") == row["round_id"]:
                                    selection["target_persistence_status"] = "COMMITTED"
                        results.append(deepcopy(pending_prior))
                        continue
                prior = session.get("event_records", {}).get(record_key)
                if prior:
                    if prior.get("source_round") != _reference(row):
                        session["status"] = "PAUSED"
                        session["stop_reason"] = "ROUND_IDENTITY_CONFLICT"
                    results.append(deepcopy(prior))
                    continue
                if session.get("status") not in {"RUNNING", "PAUSED", "MANUALLY_STOPPED"}:
                    continue
                if session["status"] == "PAUSED" and session.get("stop_reason") not in {None, "MANUAL_PAUSE"}:
                    continue
                if row["stream_id"] != session["last_stream_id"]:
                    session.update({"status": "PAUSED", "stop_reason": "STREAM_IDENTITY_CHANGED"})
                    continue
                if not observed_stage and row["stream_sequence"] <= session["last_stream_sequence"]:
                    # A replay older than the persisted cursor is a no-op; no retrospective selections.
                    continue
                if row["round_index"] != session["last_round_index"] + 1:
                    session.update({"status": "PAUSED", "stop_reason": "ROUND_INDEX_GAP",
                                    "gap_expected_index": session["last_round_index"] + 1,
                                    "gap_received_index": row["round_index"]})
                    continue
                observed_at = _parse_time(row.get("observed_at"))
                started_at = _parse_time(session.get("started_at"))
                if (committed_stage and observed_at is not None and started_at is not None
                        and observed_at.tzinfo is not None and started_at.tzinfo is not None
                        and observed_at.astimezone(timezone.utc) <= started_at.astimezone(timezone.utc)):
                    # A PostgreSQL commit can trail the API observation inbox.
                    # Advance continuity and keep the outcome as history, but
                    # never evaluate a source that existed before this session.
                    session["context"].append(deepcopy(row))
                    session["context"] = session["context"][-5:]
                    session["prestart_skipped_rounds"] = int(
                        session.get("prestart_skipped_rounds", 0)) + 1
                    session.update({"last_stream_sequence": row["stream_sequence"],
                                    "last_round_index": row["round_index"],
                                    "last_round_id": row["round_id"]})
                    continue
                if session["status"] == "PAUSED" and session.get("stop_reason") == "MANUAL_PAUSE":
                    self._resolve(session, row, at)
                    session["context"].append(deepcopy(row))
                    session["context"] = session["context"][-5:]
                    session["observed_rounds"] += 1
                    session.update({"status": "PAUSED", "last_stream_sequence": row["stream_sequence"],
                                    "last_round_index": row["round_index"], "last_round_id": row["round_id"]})
                    record = {"source_round": _reference(row), "rule_evaluations": [],
                              "decision": {"action": "WAIT", "reason": "MANUALLY_PAUSED"},
                              "evaluated_at": at}
                    session.setdefault("event_records", {})[record_key] = record
                    results.append(record)
                    continue
                self._resolve(session, row, at)
                history = deepcopy(session["context"]) + [deepcopy(row)]
                session["observed_rounds"] += 1
                session["context"] = history[-5:]
                final_window_source = session["observed_rounds"] >= session["window_size"]
                active = (session["selection_status"] == "ACTIVE" and session["status"] == "RUNNING"
                          and not final_window_source and not target_already_completed)
                evaluation_rows: list[dict[str, Any]] = []
                proposals: list[dict[str, Any]] = []
                for rid in session["rule_ids"]:
                    state = session["engines"][rid]
                    before = deepcopy(state)
                    try:
                        proposal = self._proposal(session, rid, row, history, at)
                        if state.get("failure_count", 0):
                            state["failure_count"] = 0
                            state["recovery_count"] = int(state.get("recovery_count", 0)) + 1
                    except Exception as exc:
                        state["failure_count"] = int(state.get("failure_count", 0)) + 1
                        proposal = _result(rid, session["rule_versions"][rid], session, row, _expected(row), None,
                            "EVALUATION_ERROR", "Iri tegeko ntiryabashije gusuzuma amakuru.",
                            {"errorCategory": type(exc).__name__}, before, state, at)
                    proposal["enabled"] = True  # Session snapshot owns participation.
                    if target_already_completed and proposal.get("eligible") is True:
                        proposal["eligible"] = None
                        proposal["result"] = None
                        proposal["status"] = "INVALID_LATE"
                        proposal["reason"] = "Target yamaze kuboneka; nta cyemezo cyasubijwe inyuma."
                        proposal["proposal"].update({"eligible": None, "status": "INVALID_LATE",
                                                     "reason": proposal["reason"]})
                    if final_window_source and proposal.get("eligible") is not None:
                        proposal["eligible"] = None
                        proposal["result"] = None
                        proposal["status"] = "WINDOW_COMPLETED_NO_TARGET"
                        proposal["reason"] = "Idirishya rirangiye; nta target nshya yemewe."
                        proposal["proposal"].update({"eligible": None,
                                                     "status": "WINDOW_COMPLETED_NO_TARGET",
                                                     "reason": proposal["reason"]})
                    if proposal.get("eligible") is False and rid in {START_CHECK_POLICY_ID, LEGACY_POLICY_ID, COMBINED_POLICY_ID}:
                        # A waiting-state transition is owned by this policy and
                        # depends only on its verified source, not aggregation.
                        session["engines"][rid]["rule_state"] = proposal["state_snapshot"]["after"].get("rule_state")
                    evaluation_rows.append(proposal)
                    if active:
                        proposals.append(proposal)
                target_identity_guard = ("TARGET_ALREADY_COMPLETED" if target_already_completed
                                          else "NOT_APPLICABLE")
                target_guard_unavailable = False
                if active and any(proposal.get("eligible") is True for proposal in proposals):
                    if target_already_completed:
                        target_identity_guard = "TARGET_ALREADY_COMPLETED"
                    else:
                        checker = getattr(self._persistence, "round_continuity_for_indices", None)
                        observation_checker = getattr(self.observation_inbox,
                                                      "has_pending_round_index", None)
                        target_index = int(row["round_index"]) + 1
                        try:
                            existing_target = (checker([target_index])
                                               if callable(checker) else {})
                            already_observed = (bool(observation_checker(target_index))
                                                if callable(observation_checker) else False)
                        except Exception:
                            existing_target = None
                            already_observed = False
                            target_guard_unavailable = True
                            target_identity_guard = "TARGET_IDENTITY_CHECK_UNAVAILABLE"
                        else:
                            if existing_target and target_index in existing_target:
                                target_already_completed = True
                                target_identity_guard = "TARGET_ALREADY_COMPLETED"
                            elif already_observed:
                                target_already_completed = True
                                target_identity_guard = "TARGET_ALREADY_OBSERVED"
                            elif callable(checker) or callable(observation_checker):
                                target_identity_guard = "NO_COMPLETED_TARGET_FOUND"
                    if target_already_completed or target_guard_unavailable:
                        reason = ("Target yamaze kuboneka; nta cyemezo cyasubijwe inyuma."
                                  if target_already_completed else
                                  "Target ntiyashoboye kugenzurwa; icyemezo kirahagaritswe.")
                        status = "INVALID_LATE" if target_already_completed else "UNAVAILABLE_TARGET_IDENTITY"
                        for proposal in proposals:
                            if proposal.get("eligible") is not True:
                                continue
                            proposal.update({"eligible": None, "result": None,
                                             "status": status, "reason": reason})
                            proposal.setdefault("proposal", {}).update({
                                "eligible": None, "status": status, "reason": reason,
                            })
                if active and session["mode"] in {"FIVE_RULE_TRACKER", "ALL_RULES_TOGETHER"}:
                    combined = self._combine(session, row, proposals, at)
                elif active and session["mode"] in {"START_CHECK", "LEGACY_SEPARATE_TRACKER"}:
                    combined = self._combine(session, row, proposals, at)
                else:
                    wait_reason = ("TARGET_ALREADY_COMPLETED" if target_already_completed else
                                   "TARGET_IDENTITY_CHECK_UNAVAILABLE" if target_guard_unavailable else
                                   "WINDOW_COMPLETED_NO_TARGET" if final_window_source else
                                   session.get("selection_stop_reason") or "NO_ACTIVE_RULES")
                    combined = {"action": "NO_ACTION" if not session.get("rule_ids") else "WAIT", "reason":
                                "NO_ENABLED_RULES" if not session.get("rule_ids") else wait_reason,
                                "true_rule_ids": [], "selection_id": None}
                if target_guard_unavailable:
                    combined = {"action": "WAIT", "reason": "TARGET_IDENTITY_CHECK_UNAVAILABLE",
                                "true_rule_ids": [], "selection_id": None}
                if session["observed_rounds"] >= session["window_size"]:
                    session["status"] = "COMPLETED"
                    session["ended_at"] = at
                    session["stop_reason"] = "WINDOW_COMPLETED"
                    if session["selection_status"] == "ACTIVE":
                        self._stop_selecting(session, "WINDOW_COMPLETED", at, row)
                    session["auto_next_armed"] = bool(session["configuration_snapshot"].get("auto_next_check_enabled"))
                session.update({"last_stream_sequence": row["stream_sequence"] if not observed_stage else session["last_stream_sequence"],
                                "last_round_index": row["round_index"], "last_round_id": row["round_id"]})
                record = {"session_id": session["session_id"],
                          "source_round": _reference(row), "target_round": _expected(row),
                          "rule_evaluations": evaluation_rows, "decision": combined, "evaluated_at": at,
                          "persistence_status": "PENDING" if observed_stage else "COMMITTED",
                          "target_identity_guard": target_identity_guard}
                if target_already_completed:
                    late_reason = (target_identity_guard if target_identity_guard in {
                        "TARGET_ALREADY_COMPLETED", "TARGET_ALREADY_OBSERVED"} else "TARGET_ALREADY_COMPLETED")
                    record["decision"] = {"action": "WAIT", "reason": late_reason,
                                           "true_rule_ids": [], "selection_id": None}
                shared_action = ("SELECT" if record["decision"].get("action") == "SELECT"
                                 else "NO_SELECT" if evaluation_rows else "NOT_EVALUATED")
                shared_decision = {
                    "decision_id": f"{session['session_id']}:{row['stream_id']}:{row['stream_sequence']}:shared",
                    "session_id": session["session_id"],
                    "source_round": _reference(row),
                    "target_round": _expected(row),
                    "action": shared_action,
                    "reason": record["decision"].get("reason"),
                    "true_rule_ids": list(record["decision"].get("true_rule_ids") or []),
                    "selection_id": record["decision"].get("selection_id"),
                    "evaluated_at": at,
                    "persistence_status": record["persistence_status"],
                }
                record["shared_decision"] = shared_decision
                session.setdefault("event_records", {})[record_key] = record
                if committed_stage:
                    session["event_records"][committed_key] = record
                session["evaluations"].append(deepcopy(record))
                session["evaluations"] = session["evaluations"][-500:]
                session.setdefault("decisions", []).append(deepcopy(shared_decision))
                session["decisions"] = session["decisions"][-500:]
                self.evaluation_count += len(evaluation_rows)
                results.append(record)
            actions = [item.get("decision", {}).get("action") for item in results]
            if len(results) == 1:
                decision = actions[0]
                selected_rule_ids = list(results[0].get("decision", {}).get("true_rule_ids", []))
            elif results:
                decision = "SESSION_SCOPED"
                selected_rule_ids = []
            else:
                decision = "NO_ACTION"
                selected_rule_ids = []
            output = {"source_round": _reference(row), "target_round": _expected(row),
                      "decision": decision, "result": decision == "SELECT",
                      "true_rule_ids": selected_rule_ids,
                      "sessions": results, "automatic_execution": "OFF", "can_bet": False,
                      "evaluated_at": at}
            self.last_evaluation = output
            self.last_evaluation_at = at
            self._persist()
            return deepcopy(output)

    def _spawn_auto_next_sessions(self, current: dict[str, Any], at: str) -> None:
        for previous in list(self.sessions.values()):
            if not previous.get("auto_next_armed") or previous.get("status") != "COMPLETED":
                continue
            if (current["stream_id"] != previous.get("last_stream_id")
                    or current["round_index"] != previous.get("last_round_index", 0) + 1):
                continue
            active_peer = any(item is not previous
                              and item.get("mode") == previous.get("mode")
                              and item.get("last_stream_id") == current["stream_id"]
                              and item.get("status") in {"RUNNING", "PAUSED"}
                              and item.get("selection_status") == "ACTIVE"
                              for item in self.sessions.values())
            if active_peer:
                previous["auto_next_armed"] = False
                previous["auto_next_block_reason"] = "SESSION_ALREADY_ACTIVE_FOR_MODE"
                continue
            boundary = next((item for item in reversed(previous.get("context", []))
                             if item.get("round_index") == previous.get("last_round_index")), None)
            if not boundary:
                previous["auto_next_armed"] = False
                previous["auto_next_block_reason"] = "LAST_VERIFIED_BOUNDARY_UNAVAILABLE"
                continue
            session_id = str(uuid4())
            rule_ids = list(previous["rule_ids"])
            clone = {
                "session_id": session_id, "mode": previous["mode"], "status": "RUNNING",
                "selection_status": "ACTIVE", "started_at": at, "ended_at": None,
                "window_size": previous["window_size"],
                "configuration_version": previous["configuration_version"],
                "configuration_snapshot": deepcopy(previous["configuration_snapshot"]),
                "rule_ids": rule_ids, "rule_versions": deepcopy(previous["rule_versions"]),
                "manifest": deepcopy(previous["manifest"]),
                "start_boundary": _reference(boundary) | {"streamSequence": boundary["stream_sequence"]},
                "last_stream_id": boundary["stream_id"], "last_stream_sequence": boundary["stream_sequence"],
                "last_round_index": boundary["round_index"], "last_round_id": boundary["round_id"],
                "context": deepcopy(previous["context"][-5:]),
                "engines": {rid: _fresh_engine(rid) for rid in rule_ids}, "observed_rounds": 0,
                "selections": [], "rule_selections": {rid: [] for rid in rule_ids},
                "evaluations": [], "decisions": [], "total_wins": 0, "total_losses": 0,
                "net_profit_rwf": 0, "selection_stop_reason": None, "stop_reason": None,
                "financial_stop": {"status": "ACTIVE", "reason": None,
                                   "realized_betting_pl": None, "currency": None,
                                   "win_count": 0, "loss_count": 0, "reconciled_bets": 0,
                                   "reconciled_execution_ids": []},
                "auto_next_armed": False, "automatic_execution": "OFF",
            }
            previous["auto_next_armed"] = False
            previous["auto_next_session_id"] = session_id
            self.sessions[session_id] = clone

    def _combine(self, session: dict[str, Any], source: dict[str, Any],
                 proposals: list[dict[str, Any]], at: str) -> dict[str, Any]:
        true_rows = [row for row in proposals if row.get("eligible") is True]
        expected_source, expected_target = _reference(source), _expected(source)
        if any(row.get("proposal", {}).get("sourceRoundRef") != expected_source
               or row.get("proposal", {}).get("expectedTargetRef") != expected_target for row in true_rows):
            return {"action": "BLOCKED", "reason": "TARGET_MISMATCH", "true_rule_ids": [], "selection_id": None}
        config = session["configuration_snapshot"]
        if session["selection_status"] != "ACTIVE":
            return {"action": "WAIT", "reason": session.get("selection_stop_reason") or "SELECTIONS_STOPPED",
                    "true_rule_ids": [], "selection_id": None}
        if not true_rows:
            if not session.get("rule_ids"):
                return {"action": "NO_ACTION", "reason": "NO_ENABLED_RULES", "true_rule_ids": [], "selection_id": None}
            return {"action": "WAIT", "reason": "NO_TRUE_PROPOSALS", "true_rule_ids": [], "selection_id": None}
        if config.get("max_selections") and len(session["selections"]) >= config["max_selections"]:
            self._stop_selecting(session, "MAX_SELECTIONS_REACHED", at, source)
            return {"action": "WAIT", "reason": "MAX_SELECTIONS_REACHED", "true_rule_ids": [], "selection_id": None}
        selection_id = str(uuid4())
        contributors = [{"rule_id": proposal["rule_id"], "rule_version": proposal["rule_version"],
                         "reason": proposal["reason"], "proposal": deepcopy(proposal["proposal"])}
                        for proposal in true_rows]
        selection = {"selection_id": selection_id, "session_id": session["session_id"],
                     "source_round": _reference(source), "expected_target": _expected(source),
                     "contributing_rule_ids": [row["rule_id"] for row in true_rows],
                     "contributors": contributors, "selected_at": at, "status": "PENDING", "result": "PENDING",
                     "actual_multiplier": None, "actual_target": None, "resolved_at": None,
                     "stake_rwf": config.get("fixed_stake_rwf", 100)}
        target = selection["expected_target"]
        panel = config.get("execution_panel", 0)
        intent_id = hashlib.sha256(
            f"{target['streamId']}\0{target['roundIndex']}\0{panel}".encode("utf-8")
        ).hexdigest()
        requested_stake = config.get("requested_stake")
        if requested_stake is None:
            requested_stake = config.get("fixed_stake_rwf", selection["stake_rwf"])
        selection["execution_intent"] = {
            "intent_id": intent_id,
            "decision_id": selection_id,
            "source_round_id": selection["source_round"]["roundId"],
            "target_round_id": target.get("roundId"),
            "target_stream_id": target["streamId"],
            "target_round_index": target["roundIndex"],
            "selected_rule_ids": list(selection["contributing_rule_ids"]),
            "requested_stake": requested_stake,
            "requested_stake_currency": config.get("stake_currency"),
            "cashout_target": 2.0,
            "created_at": at,
            "expires_at": target.get("expiresAt"),
            "session_id": session["session_id"],
            "panel": panel,
            "execution_status": "BLOCKED_READ_ONLY",
            "blockers": [
                "REAL_MONEY_EXECUTION_DISABLED",
                "EXPLICIT_SESSION_AUTHORIZATION_REQUIRED",
                "TARGET_ROUND_ID_UNVERIFIED",
                "BETTING_DEADLINE_UNVERIFIED",
                "BET_AMOUNT_CURRENCY_UNVERIFIED" if not config.get("stake_currency") else
                    "STAKE_CURRENCY_NOT_RECONCILED",
                "VERIFIED_BALANCE_AND_SUFFICIENT_FUNDS_REQUIRED",
                "CASHOUT_PLATFORM_ACKNOWLEDGEMENT_UNVERIFIED",
                "PLATFORM_ACCEPTANCE_AND_SETTLEMENT_UNVERIFIED",
            ],
        }
        session["selections"].append(selection)
        for proposal in true_rows:
            self._commit_contributor(session, proposal, selection)
        return {"action": "SELECT", "reason": "ONE_OR_MORE_RULES_TRUE", "selection_id": selection_id,
                "true_rule_ids": list(selection["contributing_rule_ids"]),
                "target_round": deepcopy(selection["expected_target"]),
                "contributor_reasons": [{"rule_id": row["rule_id"], "reason": row["reason"]} for row in true_rows]}

    def _separate(self, session: dict[str, Any], source: dict[str, Any],
                  proposals: list[dict[str, Any]], at: str) -> dict[str, Any]:
        true_rows = [row for row in proposals if row.get("eligible") is True]
        selected: list[str] = []
        selection_ids: list[str] = []
        expected_source, expected_target = _reference(source), _expected(source)
        if any(row.get("proposal", {}).get("sourceRoundRef") != expected_source
               or row.get("proposal", {}).get("expectedTargetRef") != expected_target for row in true_rows):
            return {"action": "BLOCKED", "reason": "TARGET_MISMATCH", "true_rule_ids": [], "selection_id": None}
        for proposal in true_rows:
            rid = proposal["rule_id"]
            selection_id = str(uuid4())
            selection = {"selection_id": selection_id, "session_id": session["session_id"],
                         "rule_id": rid, "rule_version": proposal["rule_version"],
                         "source_round": _reference(source), "expected_target": _expected(source),
                         "reason": proposal["reason"], "selected_at": at,
                         "status": "PENDING", "result": "PENDING", "actual_multiplier": None,
                         "actual_target": None, "resolved_at": None}
            session["rule_selections"][rid].append(selection)
            self._commit_contributor(session, proposal, selection)
            selected.append(rid)
            selection_ids.append(selection_id)
        return {"action": "SELECT" if selected else ("WAIT" if session.get("rule_ids") else "NO_ACTION"),
                "reason": "INDEPENDENT_TRACKER_SELECTIONS" if selected else "NO_TRUE_PROPOSALS",
                "true_rule_ids": selected, "selection_ids": selection_ids}

    def _start_check_summary(self, session: dict[str, Any]) -> dict[str, Any]:
        rid = START_CHECK_POLICY_ID
        state = session.get("engines", {}).get(rid, {})
        return {"state": state.get("rule_state", "WAIT_LOW"), "mode": state.get("mode", "STANDARD"),
                "failed_cycles": state.get("failed_cycle_count", 0),
                "selected_wins": state.get("selected_wins", 0), "selected_losses": state.get("selected_losses", 0)}

    def session_action(self, session_id: str, action: str, *, now: str | None = None) -> dict[str, Any]:
        at = _now(now)
        if action not in {"pause", "resume", "stop", "reset", "emergency_stop"}:
            raise ValueError("INVALID_SESSION_ACTION")
        with self._lock:
            session = self.sessions.get(session_id)
            if not session:
                raise KeyError("SESSION_NOT_FOUND")
            if action == "pause" and session["status"] == "RUNNING":
                session.update({"status": "PAUSED", "stop_reason": "MANUAL_PAUSE", "paused_at": at})
            elif action == "resume" and session["status"] == "PAUSED" and session.get("stop_reason") == "MANUAL_PAUSE":
                session.update({"status": "RUNNING", "stop_reason": None, "resumed_at": at})
            elif (action == "resume" and session["status"] == "PAUSED"
                  and session.get("stop_reason") == "UNKNOWN_TRANSACTION_REQUIRES_RECONCILIATION"):
                has_open = getattr(self._persistence, "has_open_rule_session_executions", None)
                if callable(has_open) and has_open(session_id):
                    raise ValueError("UNRECONCILED_EXECUTION_REMAINS")
                financial_stop = session.get("financial_stop") or {}
                if financial_stop.get("status") == "STOPPED":
                    raise ValueError("SESSION_STOP_LIMIT_REACHED")
                session.update({"status": "RUNNING", "stop_reason": None,
                                "selection_status": "ACTIVE", "selection_stop_reason": None,
                                "resumed_at": at})
                financial_stop.update({"status": "ACTIVE", "reason": None,
                                       "resumed_after_reconciliation_at": at})
                session["financial_stop"] = financial_stop
            elif action in {"stop", "emergency_stop"}:
                reason = "EMERGENCY_STOP" if action == "emergency_stop" else "MANUAL_STOP"
                prior_reason = session.get("stop_reason")
                if prior_reason and prior_reason not in {"MANUAL_PAUSE", "UNKNOWN_TRANSACTION_REQUIRES_RECONCILIATION"}:
                    session["integrity_stop_reason"] = prior_reason
                session.update({"status": "MANUALLY_STOPPED", "selection_status": "STOPPED",
                                "selection_stop_reason": reason, "stop_reason": reason,
                                "ended_at": at, "auto_next_armed": False})
                session.setdefault("financial_stop", {}).update({
                    "status": "STOPPED", "reason": reason, "stopped_at": at,
                })
            elif action == "reset":
                session.update({"status": "RESET", "selection_status": "STOPPED",
                                "selection_stop_reason": "RESET", "stop_reason": "RESET", "ended_at": at,
                                "auto_next_armed": False})
                session.setdefault("financial_stop", {}).update({
                    "status": "STOPPED", "reason": "RESET", "stopped_at": at,
                })
            else:
                raise ValueError("INVALID_SESSION_TRANSITION")
            self._persist()
            return deepcopy(session)

    def session(self, session_id: str) -> dict[str, Any] | None:
        value = self.sessions.get(session_id)
        return deepcopy(value) if value else None

    def sessions_status(self) -> list[dict[str, Any]]:
        return [self._session_summary(item) for item in sorted(self.sessions.values(),
                key=lambda row: row.get("started_at", ""), reverse=True)]

    @staticmethod
    def _has_pending_event_records(session: Mapping[str, Any]) -> bool:
        return any(record.get("persistence_status") == "PENDING"
                   for record in (session.get("event_records") or {}).values())

    @staticmethod
    def _session_accepts_source_events(session: Mapping[str, Any]) -> bool:
        status = session.get("status")
        if status == "RUNNING":
            return True
        if status == "PAUSED" and session.get("stop_reason") in {None, "MANUAL_PAUSE"}:
            return True
        if status == "MANUALLY_STOPPED":
            selections = list(session.get("selections") or [])
            selections.extend(item for rows in (session.get("rule_selections") or {}).values()
                              for item in rows)
            return any(item.get("status") == "PENDING" for item in selections)
        return False

    def _selected_session(self) -> dict[str, Any] | None:
        rows = sorted(self.sessions.values(),
                      key=lambda row: row.get("started_at", ""), reverse=True)
        running = next((item for item in rows if item.get("status") == "RUNNING"), None)
        if running:
            return running
        return next((item for item in rows if item.get("status") == "PAUSED"
                     and item.get("stop_reason") in {
                         None, "MANUAL_PAUSE", "UNKNOWN_TRANSACTION_REQUIRES_RECONCILIATION"}), None)

    def stream_cursor(self, stream_id: str, latest_sequence: int) -> int:
        cursors = [int(row.get("last_stream_sequence", latest_sequence))
                   for row in self.sessions.values()
                   if row.get("last_stream_id") == stream_id
                   and (self._session_accepts_source_events(row)
                        or self._has_pending_event_records(row))]
        return min(cursors) if cursors else int(latest_sequence)

    @staticmethod
    def _session_summary(session: Mapping[str, Any]) -> dict[str, Any]:
        # New sessions count each shared target once, regardless of how many
        # independent rules contributed. Keep the old per-rule ledger summary
        # readable for sessions persisted by the previous tracker version.
        rows = session.get("selections", [])
        if session.get("mode") == "FIVE_RULE_TRACKER" and not rows:
            legacy_rows = [item for values in session.get("rule_selections", {}).values() for item in values]
            if legacy_rows:
                rows = legacy_rows
                results = {status: sum(item.get("status") == status for item in rows)
                           for status in ("WIN", "LOSS", "PENDING")}
            else:
                results = {"WIN": 0, "LOSS": 0, "PENDING": 0}
        else:
            pending = sum(1 for row in rows if row.get("status") == "PENDING")
            results = {"WIN": session.get("total_wins", 0), "LOSS": session.get("total_losses", 0), "PENDING": pending}
        selection_count = len(rows)
        configuration = session.get("configuration_snapshot") or {}
        requested_stake = configuration.get("requested_stake")
        if requested_stake is None:
            requested_stake = configuration.get("fixed_stake_rwf", configuration.get("stake_rwf"))
        return {"session_id": session["session_id"], "mode": session["mode"], "status": session["status"],
                "selection_status": session.get("selection_status"), "started_at": session.get("started_at"),
                "ended_at": session.get("ended_at"), "rule_ids": list(session.get("rule_ids", [])),
                "configuration_version": session.get("configuration_version"),
                "observed_rounds": session.get("observed_rounds", 0), "window_size": session.get("window_size"),
                "prestart_skipped_rounds": session.get("prestart_skipped_rounds", 0),
                "selection_count": selection_count, "results": results,
                "net_profit_rwf": session.get("net_profit_rwf", 0),
                "stop_reason": session.get("selection_stop_reason") or session.get("stop_reason"),
                "integrity_stop_reason": session.get("integrity_stop_reason"),
                "selection_results_kind": "SHADOW_ROUND_OUTCOMES",
                "execution_panel": configuration.get("execution_panel"),
                "requested_stake": requested_stake,
                "requested_stake_currency": configuration.get("stake_currency"),
                "financial_stop": deepcopy(session.get("financial_stop") or {
                    "status": "ACTIVE", "reason": None, "realized_betting_pl": None,
                    "currency": None, "win_count": 0, "loss_count": 0, "reconciled_bets": 0,
                }),
                "automatic_execution": "OFF"}

    def ui_setup_configuration(self) -> dict[str, Any]:
        """Return the active session's UI stake and single selected panel.

        This is configuration only. It is independent of whether any rule has
        produced TRUE and it never authorizes a financial action.
        """
        with self._lock:
            active = self._selected_session()
            if not active:
                return {"status": "BLOCKED", "reason": "NO_ACTIVE_RULES_SESSION",
                        "session_id": None, "requested_stake": None,
                        "requested_stake_currency": None, "execution_panel": None,
                        "automatic_execution": "OFF"}
            config = deepcopy(active.get("configuration_snapshot") or {})
            requested_stake = config.get("requested_stake")
            if requested_stake is None:
                requested_stake = config.get("fixed_stake_rwf", config.get("stake_rwf"))
            currency = config.get("stake_currency")
            if currency is None and ("fixed_stake_rwf" in config or "stake_rwf" in config):
                currency = "RWF"
            panel = config.get("execution_panel")
            if (isinstance(requested_stake, bool)
                    or not isinstance(requested_stake, (int, float))
                    or requested_stake <= 0):
                return {"status": "BLOCKED", "reason": "SESSION_STAKE_NOT_CONFIGURED",
                        "session_id": active.get("session_id"), "requested_stake": None,
                        "requested_stake_currency": currency, "execution_panel": panel,
                        "automatic_execution": "OFF"}
            if not isinstance(currency, str) or not re.fullmatch(r"[A-Z]{3}", currency):
                return {"status": "BLOCKED", "reason": "SESSION_STAKE_CURRENCY_UNVERIFIED",
                        "session_id": active.get("session_id"), "requested_stake": requested_stake,
                        "requested_stake_currency": None, "execution_panel": panel,
                        "automatic_execution": "OFF"}
            if panel is None:
                return {"status": "BLOCKED", "reason": "SESSION_EXECUTION_PANEL_NOT_SELECTED",
                        "session_id": active.get("session_id"), "requested_stake": requested_stake,
                        "requested_stake_currency": currency, "execution_panel": None,
                        "automatic_execution": "OFF"}
            if isinstance(panel, bool) or panel not in (0, 1):
                return {"status": "BLOCKED", "reason": "SESSION_EXECUTION_PANEL_INVALID",
                        "session_id": active.get("session_id"), "requested_stake": requested_stake,
                        "requested_stake_currency": currency, "execution_panel": None,
                        "automatic_execution": "OFF"}
            return {"status": "CONFIGURED", "reason": None,
                    "session_id": active.get("session_id"),
                    "session_status": active.get("status"),
                    "requested_stake": requested_stake,
                    "requested_stake_currency": currency,
                    "execution_panel": panel,
                    "automatic_execution": "OFF"}

    def evaluate(self, evaluated_round: dict[str, Any] | None = None) -> dict[str, Any]:
        # Compatibility endpoint deliberately does not infer from a loose/latest row.
        # Only committed, ordered round.persisted events can reach observe_round().
        at = _now()
        active_sessions = any(item.get("status") == "RUNNING" and item.get("selection_status") == "ACTIVE"
                              for item in self.sessions.values())
        if evaluated_round is not None and active_sessions:
            result = {"result": False, "true_rule_ids": [], "rule_evaluations": [],
                      "decision": "BLOCKED", "reason": "COMMITTED_EVENT_REQUIRED",
                      "source_round": deepcopy(evaluated_round), "automatic_execution": "OFF", "can_bet": False}
        elif evaluated_round is not None:
            result = self.aggregate(())
            result["evaluated_round"] = deepcopy(evaluated_round)
            result["reason"] = "NOT_CONFIGURED"
        else:
            result = self.aggregate(())
        result["evaluated_at"] = at
        self.last_evaluation, self.last_evaluation_at = result, at
        self._persist()
        return deepcopy(result)

    def execution_authorized(self) -> bool:
        return False

    def needs_round_observation(self) -> bool:
        """Only session-owned state needs each collector event.

        An empty engine must not make the collector wait for a state write on
        every round. Completed sessions armed for auto-next still need the
        boundary event, and manually stopped windows keep observing.
        """
        with self._lock:
            return any(
                self._session_accepts_source_events(item)
                or item.get("status") == "MANUALLY_STOPPED"
                or self._has_pending_event_records(item)
                or (item.get("status") == "COMPLETED" and item.get("auto_next_armed") is True)
                for item in self.sessions.values()
            )

    def status(self) -> dict[str, Any]:
        active = self._selected_session()
        summary = self._session_summary(active) if active else None
        configured = sum(1 for row in self.rule_configuration.values() if row.get("configured"))
        enabled = sum(1 for row in self.rule_configuration.values() if row.get("configured") and row.get("enabled"))
        if active:
            configured = len(active.get("rule_ids", []))
            enabled = len(active.get("rule_ids", []))
        latest = (self.last_evaluation or {}).get("source_round",
                  (self.last_evaluation or {}).get("evaluated_round"))
        return {"status": active.get("status") if active else ("READY" if configured else "NOT_CONFIGURED"),
                "rules_configured": configured, "rules_enabled": enabled,
                "configuration_version": self.configuration_version,
                "presets": deepcopy(self.presets),
                "last_rule_evaluation": self.last_evaluation_at or "NONE",
                "last_evaluated_round": latest, "decision": (self.last_evaluation or {}).get("decision", "NO_ACTION"),
                "automatic_execution": "OFF", "rule_evaluations": (self.last_evaluation or {}).get("rule_evaluations", []),
                "active_session": summary, "sessions": self.sessions_status(), "selection_count": summary.get("selection_count", 0) if summary else 0,
                "total_wins": summary.get("results", {}).get("WIN", 0) if summary else 0,
                "total_losses": summary.get("results", {}).get("LOSS", 0) if summary else 0,
                "pending": summary.get("results", {}).get("PENDING", 0) if summary else 0,
                "can_bet": False}
