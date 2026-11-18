"""Prospective, shadow-only Rules Engine migrated from aviator-shadow-experiment-node.

The registry freezes source semantics by mode. Engines own their state; the
coordinator is the only component allowed to create a combined selection.
"""
from __future__ import annotations

from copy import deepcopy
from collections import deque
from datetime import datetime, timedelta, timezone
import json
import hashlib
import logging
import math
import re
from threading import RLock
import time
from typing import Any, Mapping
from uuid import NAMESPACE_URL, uuid4, uuid5

ENGINE_VERSION = "WINNER_RULES_ENGINE_SHADOW_V1"
log = logging.getLogger(__name__)
STATE_KEY = "rules_engine_state_v1"
WINDOW_SIZE = 50
RULE_PERFORMANCE_WINDOW_SIZE = 100
RULE_PERFORMANCE_IDLE_RESET_SECONDS = 300
H_THRESHOLD = 2.0
RULE_LOSS_LIMIT = 2
RULE_CYCLE_MAX_TARGETS = 3
NET_OUTCOME_LIMIT_REASONS = {"WIN_LIMIT_REACHED", "LOSS_LIMIT_REACHED"}
SESSION_LIMIT_STOP_REASONS = {*NET_OUTCOME_LIMIT_REASONS, "MAX_SELECTIONS_REACHED",
                             "PROFIT_TARGET_REACHED", "AUTO_NEXT_RESULT_LIMIT_REACHED"}
MODES = {"START_CHECK", "LEGACY_SEPARATE_TRACKER", "FIVE_RULE_TRACKER", "ALL_RULES_TOGETHER"}
COMBINED_POLICY_ID = "USER_PLAYING_POLICY"
START_CHECK_POLICY_ID = "USER_PLAYING_POLICY_START_CHECK"
LEGACY_POLICY_ID = "USER_PLAYING_POLICY_LEGACY_TRACKER"
SPECIAL_SKIP_POLICY_IDS = {START_CHECK_POLICY_ID, COMBINED_POLICY_ID}
RULE_PERFORMANCE_CHECKPOINT_ID = "__rule_performance_observer__"


def _research_definitions() -> list[dict[str, Any]]:
    return [
        {
            "rule_id": "RECENT_5_H_GE_4", "version": "2.0.0",
            "name": "Recent 5: at least 4 H", "mode": "FIVE_RULE_TRACKER|ALL_RULES_TOGETHER",
            "parameters": {}, "target_offset": 1, "history_required": 5,
            "condition": {"window": 5, "outcome": "H", "operator": ">=", "count": 4,
                          "includes_source": True, "latest_source_must_be_h": False,
                          "maximum_selections": 1, "loss_limit": RULE_LOSS_LIMIT},
            "initial_state": {"status": "WARMING_UP", "pending_target": None, "failure_count": 0,
                              "recovery_count": 0},
            "transition_rules": ["Use the latest five consecutive verified completed outcomes including source.",
                                 "A fresh 4-of-5 H entry starts one target; the signal must clear before it can start again.",
                                 "Two settled losses for this rule pause it until a fresh entry signal."],
            "reason_rw": "H 4 muri rounds 5: condition yujujwe.",
        },
        {
            "rule_id": "PATTERN_LH", "version": "2.0.0",
            "name": "Pattern LH", "mode": "FIVE_RULE_TRACKER|ALL_RULES_TOGETHER",
            "parameters": {}, "target_offset": 1, "history_required": 2,
            "condition": {"pattern": ["L", "H"], "includes_source": True,
                          "maximum_selections": 1, "loss_limit": RULE_LOSS_LIMIT},
            "initial_state": {"status": "WARMING_UP", "pending_target": None, "failure_count": 0,
                              "recovery_count": 0},
            "transition_rules": ["A fresh verified L,H signal starts one target; clear and reappear before restarting.",
                                 "Two settled losses for this rule pause it until a fresh L,H entry."],
            "reason_rw": "Rounds ebyiri ziheruka ni L,H.",
        },
        {
            "rule_id": "PATTERN_HH", "version": "2.0.0",
            "name": "Pattern HH", "mode": "FIVE_RULE_TRACKER|ALL_RULES_TOGETHER",
            "parameters": {}, "target_offset": 1, "history_required": 2,
            "condition": {"pattern": ["H", "H"], "includes_source": True,
                          "reevaluate_each_source": True, "overlap_allowed": False,
                          "maximum_selections": 1, "loss_limit": RULE_LOSS_LIMIT},
            "initial_state": {"status": "WARMING_UP", "pending_target": None, "failure_count": 0,
                              "recovery_count": 0},
            "transition_rules": ["A fresh verified H,H signal starts one target; one continuous H streak is one entry.",
                                 "The H signal must break before another H,H cycle can start.",
                                 "Two settled losses for this rule pause it until a fresh H,H entry."],
            "reason_rw": "Rounds ebyiri ziheruka ni H,H.",
        },
        {
            "rule_id": "SOURCE_H_MULT_2_3", "version": "2.0.0",
            "name": "Source multiplier (2, 3)", "mode": "FIVE_RULE_TRACKER|ALL_RULES_TOGETHER",
            "parameters": {}, "target_offset": 1, "history_required": 1,
            "condition": {"minimum_exclusive": 2.0, "maximum_exclusive": 3.0,
                          "maximum_selections": 1, "loss_limit": RULE_LOSS_LIMIT},
            "initial_state": {"status": "WAIT", "pending_target": None, "failure_count": 0,
                              "recovery_count": 0},
            "transition_rules": ["A fresh source multiplier >2.00 and <3.00 starts one target.",
                                 "The signal must clear before it can start again.",
                                 "Two settled losses for this rule pause it until a fresh entry signal."],
            "reason_rw": "Igikubo cya source kiri hagati ya 2.00 na 3.00.",
        },
        {
            "rule_id": "AFTER_HH_CONTINUE_MAX_3", "version": "2.0.0",
            "name": "After HH: continue for at most 3 targets", "mode": "FIVE_RULE_TRACKER|ALL_RULES_TOGETHER",
            "parameters": {}, "target_offset": 1, "history_required": 2,
            "condition": {"trigger_pattern": ["H", "H"], "maximum_selections": 3,
                          "loss_limit": RULE_LOSS_LIMIT,
                          "ignore_triggers_while_active": True, "stop_on_ww_or_ll": False,
                          "rearm_after_third_resolved_target": True},
            "initial_state": {"cycle_mode": "IDLE", "cycle": None, "pending_target": None,
                              "failure_count": 0, "recovery_count": 0},
            "transition_rules": ["Idle verified HH requests target one and starts a private cycle.",
                                 "After each owned target resolves, request the next consecutive target up to three.",
                                 "Ignore HH while active; do not reset or extend the cycle.",
                                 "Two settled losses for this rule stop its cycle immediately.",
                                 "After two losses, wait for the HH entry to clear and appear again.",
                                 "Complete and rearm after the third resolved target; wait for HH to clear before a new cycle."],
            "reason_rw": "HH yabonetse; itegeko risaba target ikurikira.",
        },
    ]


RESEARCH_RULES = _research_definitions()
_research_by_id = {row["rule_id"]: row for row in RESEARCH_RULES}

SPECIAL_SKIP_POLICY_RULE = (
    "After H trigger, selected LOSS then WIN enters special skip: skip one source, "
    "bet after L <= 2.00, repeat after WIN, and end the cycle after two special wins "
    "or a skipped H > 2.00. One selected LOSS during the active special skip ends "
    "that policy cycle immediately."
)

START_CHECK_POLICY = {
    "rule_id": START_CHECK_POLICY_ID, "version": "AVIATOR_2X_CONSECUTIVE_STOP_V4",
    "name": "Start a Check: source legacy policy", "mode": "START_CHECK",
    "parameters": {"stake_rwf": 100, "cashout_multiplier": 2.0, "window_size": WINDOW_SIZE,
                    "profit_target_rwf": 400},
    "target_offset": 1,
    "condition": {"standard_trigger": ["L", "H"], "trigger_h_counts_as_previous_result": True,
                  "standard_stop": ["HH", "LL"], "two_bet_wins_activates_after_two_failed_ll_cycles": True,
                  "two_bet_wins_stop": "two consecutive selected H outcomes",
                  "h_loss_win_special_skip": True, "loss_limit": RULE_LOSS_LIMIT},
    "initial_state": {"rule_state": "WAIT_LOW", "mode": "STANDARD", "previous_outcome": None,
                      "failed_cycle_count": 0, "consecutive_bet_wins": 0, "pending_target": None},
    "transition_rules": ["WAIT_LOW waits for L; WAIT_HIGH waits for H.",
                          "H triggers source+1; legacy reducer seeds previous outcome H.",
                          "STANDARD stops on HH or LL; LL terminal L satisfies the low condition.",
                          SPECIAL_SKIP_POLICY_RULE,
                          "Two consecutive LL-ending STANDARD cycles activate TWO_BET_WINS.",
                          "TWO_BET_WINS stops after two consecutive selected H outcomes; trigger H is not a bet.",
                          "Two settled losses pause this policy until a new L,H entry.",
                          "A +400 profit target can stop new selections while the 50-round window continues observing."],
    "source_semantics_note": "Distinct from All Rules Together; preserves rule-reducer.js behavior.",
}

LEGACY_SINGLE_TRACKER_POLICY = {
    "rule_id": LEGACY_POLICY_ID, "version": "AVIATOR_2X_CONSECUTIVE_STOP_V4",
    "name": "Legacy separate tracker: single rule", "mode": "LEGACY_SEPARATE_TRACKER",
    "parameters": {"stake_rwf": 100, "cashout_multiplier": 2.0, "window_size": WINDOW_SIZE,
                    "profit_target_rwf": 400},
    "target_offset": 1,
    "condition": {**deepcopy(START_CHECK_POLICY["condition"]), "h_loss_win_special_skip": False},
    "initial_state": deepcopy(START_CHECK_POLICY["initial_state"]),
    "transition_rules": [rule for rule in START_CHECK_POLICY["transition_rules"]
                          if rule != SPECIAL_SKIP_POLICY_RULE],
    "source_semantics_note": "Separate state and shadow ledger; source is live-shadow-test.js.",
}

COMBINED_POLICY = {
    "rule_id": COMBINED_POLICY_ID, "version": "AVIATOR_2X_CONSECUTIVE_STOP_V4",
    "name": "Accepted user playing policy", "mode": "ALL_RULES_TOGETHER",
    "parameters": {}, "target_offset": 1,
    "condition": {"standard_trigger": ["L", "H"], "trigger_h_counts_as_selected_result": False,
                  "standard_first_selected_h_stops": True,
                  "after_first_selected_l_stop_on": ["HH", "LL"],
                  "h_loss_win_special_skip": True,
                  "two_bet_wins_activates_after_two_consecutive_standard_ll_cycles": True,
                  "two_bet_wins_trigger_h_counts_as_selected_result": False,
                  "two_bet_wins_continue_until": ["HH", "LL"],
                  "two_bet_wins_ll_keeps_mode": True, "two_bet_wins_hh_clears_mode": True,
                  "loss_limit": RULE_LOSS_LIMIT},
    "initial_state": {"rule_state": "WAIT_LOW", "mode": "STANDARD", "failed_cycle_count": 0,
                      "pending_target": None, "selected_result_streak": []},
    "transition_rules": ["WAIT_LOW waits for L; WAIT_HIGH waits for H.",
                          "H proposes source+1 and is not a selected WIN.",
                          "In STANDARD, the first selected H ends the cycle; after first selected L, continue to HH or LL.",
                          SPECIAL_SKIP_POLICY_RULE,
                          "Two consecutive STANDARD LL cycles activate TWO_BET_WINS; a successful STANDARD cycle resets that streak.",
                          "TWO_BET_WINS keeps mode after LL and clears to STANDARD after selected HH.",
                          "Two settled losses pause this policy until a new L,H entry.",
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
    # The shared Rules Engine's 2x settlement convention counts an exact
    # 2.00x result as HIGH/WIN. Behaviour's rolling classification keeps EXACT
    # distinct for dominance calculations; that separate descriptive class
    # must not turn an exact cashout target into a SHADOW loss.
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


def _with_rule_lifecycle(state: dict[str, Any]) -> dict[str, Any]:
    state.setdefault("cycle_mode", "IDLE")
    state.setdefault("cycle", None)
    state.setdefault("entry_signal_latched", False)
    state.setdefault("loss_limit_losses", 0)
    state.setdefault("loss_limit_blocked", False)
    state.setdefault("loss_limit_reached_at", None)
    state.setdefault("loss_limit_reached_round_index", None)
    state.setdefault("loss_limit_terminal_round_index", None)
    return state


def _fresh_engine(rule_id: str) -> dict[str, Any]:
    if rule_id in {START_CHECK_POLICY_ID, LEGACY_POLICY_ID}:
        state = {"rule_state": "WAIT_LOW", "mode": "STANDARD", "previous_outcome": None,
                "failed_cycle_count": 0, "consecutive_bet_wins": 0, "pending_selection_id": None,
                "pending_target": None, "selected_outcomes": [], "failure_count": 0, "recovery_count": 0}
        if rule_id in SPECIAL_SKIP_POLICY_IDS:
            state.update({"special_skip_active": False, "special_skip_after_round_index": None,
                          "special_skip_win_count": 0,
                          "special_skip_terminal_round_index": None})
        return _with_rule_lifecycle(state)
    if rule_id == COMBINED_POLICY_ID:
        return _with_rule_lifecycle({"rule_state": "WAIT_LOW", "mode": "STANDARD", "previous_outcome": None,
                "failed_cycle_count": 0, "consecutive_bet_wins": 0, "pending_selection_id": None,
                "pending_target": None, "selected_outcomes": [], "cycle_number": 0,
                "failure_count": 0, "recovery_count": 0,
                "special_skip_active": False, "special_skip_after_round_index": None,
                "special_skip_win_count": 0, "special_skip_terminal_round_index": None})
    state = {"pending_selection_id": None, "pending_target": None, "failure_count": 0,
             "recovery_count": 0, "selected_wins": 0, "selected_losses": 0}
    if rule_id == "AFTER_HH_CONTINUE_MAX_3":
        state.update({"cycle_mode": "IDLE", "cycle": None})
    return _with_rule_lifecycle(state)


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


def _stable_selection_id(session: Mapping[str, Any], source: Mapping[str, Any],
                         rule_ids: list[str]) -> str:
    """Keep a decision's identity stable when a durable inbox event is replayed."""
    expected = _expected(source)
    identity = "\0".join(("winner-predict-selection-v1",
        str(session.get("session_id") or ""), str(source.get("stream_id") or ""),
        str(source.get("round_id") or ""), str(expected.get("roundIndex") or ""),
        ",".join(str(rule_id) for rule_id in rule_ids)))
    return str(uuid5(NAMESPACE_URL, identity))


def _history_is_contiguous(history: list[dict[str, Any]], count: int, stream_id: str) -> list[dict[str, Any]] | None:
    tail = history[-count:]
    if len(tail) != count:
        return None
    for i, row in enumerate(tail):
        starts_segment = (i == 0 and row.get("continuity_status") == "GAP_BEFORE"
                          and row.get("continuity_boundary") is True)
        if ((row.get("continuity_status") != "VERIFIED" and not starts_segment)
                or row.get("outcome") not in {"H", "L"}):
            return None
        if row.get("stream_id") != stream_id:
            return None
        if i and row.get("round_index") != tail[i - 1].get("round_index", 0) + 1:
            return None
    return tail


class RulesEngine:
    """Rule catalogue, independent shadow-session states, and shared coordinator."""

    RESTARTABLE_INTEGRITY_STOPS = {
        "ROUND_INDEX_GAP",
        "ROUND_IDENTITY_CONFLICT",
        "OBSERVED_COMMIT_IDENTITY_CONFLICT",
        "STREAM_IDENTITY_CHANGED",
    }

    def __init__(self, persistence: Any | None = None, *,
                 performance_observer: bool = True) -> None:
        self._lock = RLock()
        self._persistence = persistence
        self.observation_inbox: Any | None = None
        self.last_evaluation: dict[str, Any] | None = None
        self.last_evaluation_at: str | None = None
        self.configuration_version = 0
        self.rule_configuration: dict[str, dict[str, Any]] = {}
        self.presets: dict[str, dict[str, Any]] = {}
        self.sessions: dict[str, dict[str, Any]] = {}
        self._performance_observer_enabled = performance_observer
        self._performance_observers: dict[str, dict[str, Any]] = {}
        self._performance_observer_error: str | None = None
        self.evaluation_count = 0
        self._persistence_generation = 0
        self._delivery_metrics: dict[str, deque[float]] = {
            "rules_engine_processing_ms": deque(maxlen=2000),
            "target_settlement_processing_ms": deque(maxlen=2000),
            "target_settlement_durable_ms": deque(maxlen=2000),
        }
        self._delivery_metric_ids: dict[str, set[str]] = {key: set() for key in self._delivery_metrics}
        self._delivery_metric_order: dict[str, deque[str]] = {
            key: deque() for key in self._delivery_metrics
        }
        self._last_status_latency_metrics: dict[str, Any] = {}
        if persistence is not None:
            saved = persistence.load_application_state(STATE_KEY)
            if isinstance(saved, dict) and saved.get("engine_version") == ENGINE_VERSION:
                self._restore(saved)
                performance_window_migrated = self._upgrade_rule_performance_windows()
                prime_ledger_cache = getattr(persistence, "prime_rules_engine_ledger_cache", None)
                ledger_cache_primed = False
                if callable(prime_ledger_cache):
                    # The restored application snapshot and its queryable
                    # ledger were committed together for current-format data.
                    # Seed the incremental writer so the first new round does
                    # not replay durable history. Migrate any sessions found
                    # only in a legacy snapshot during startup.
                    ledger_cache_primed = bool(prime_ledger_cache(self._snapshot()))
                if ledger_cache_primed:
                    self._persist()
                elif performance_window_migrated:
                    self._persist(session_ids={RULE_PERFORMANCE_CHECKPOINT_ID})

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
                "rules_configured": len(rows), "rules_enabled": n_enabled}

    @staticmethod
    def _no_action(rows: Any, reason: str) -> dict[str, Any]:
        values = list(rows)
        return {"result": False, "true_rule_ids": [], "rule_evaluations": deepcopy(values),
                "decision": "BLOCKED" if reason in {"TARGET_MISMATCH", "TRUE_WITHOUT_TARGET"} else "NO_ACTION",
                "reason": reason, "rules_configured": len(values), "rules_enabled": 0}

    def _snapshot(self, *, session_ids: set[str] | None = None) -> dict[str, Any]:
        sessions = (self.sessions if session_ids is None else
                    {session_id: self.sessions[session_id] for session_id in session_ids
                     if session_id in self.sessions})
        observer_state = {}
        if self._performance_observer_enabled:
            observer_state = {
                rule_id: {
                    "engine": observer["engine"]._snapshot(),
                    "history": deepcopy(observer.get("history") or []),
                    "context": deepcopy(observer.get("context") or []),
                    "last_event": deepcopy(observer.get("last_event")),
                    "segment_start_round_index": int(observer.get("segment_start_round_index") or 0),
                    "rounds_processed": int(observer.get("rounds_processed") or 0),
                    "manual_reset_at": observer.get("manual_reset_at"),
                    "manual_reset_round_index": observer.get("manual_reset_round_index"),
                }
                for rule_id, observer in self._performance_observers.items()
            }
        return {"engine_version": ENGINE_VERSION, "_rules_engine_generation": self._persistence_generation,
                "configuration_version": self.configuration_version,
                "rule_configuration": deepcopy(self.rule_configuration), "presets": deepcopy(self.presets),
                # Persistence only reads this snapshot synchronously. _persist
                # holds the engine lock through serialization, so copying every
                # nested round/evaluation record here only adds a second full
                # traversal of the session payload without improving safety.
                "sessions": dict(sessions), "rule_performance_observers": observer_state,
                "rule_performance_observer_error": self._performance_observer_error,
                "last_evaluation": deepcopy(self.last_evaluation),
                "last_evaluation_at": self.last_evaluation_at, "evaluation_count": self.evaluation_count}

    def persist_current_snapshot(self, *, session_ids: set[str] | None = None) -> dict[str, float]:
        """Capture state under the rules lock, then persist without holding it.

        The collector observation inbox is the durable event log. A caller may
        therefore let the ordered Rules Engine continue after capturing this
        immutable snapshot; it must acknowledge the covered inbox events only
        after this method reports a successful database commit.
        """
        if self._persistence is None:
            return {"snapshot_serialization_ms": 0.0, "database_persistence_ms": 0.0,
                    "persistence_total_ms": 0.0}
        started_ns = time.perf_counter_ns()
        with self._lock:
            snapshot_started_ns = time.perf_counter_ns()
            snapshot = deepcopy(self._snapshot(session_ids=session_ids))
            snapshot_finished_ns = time.perf_counter_ns()
        persist = getattr(self._persistence, "save_rules_engine_snapshot", None)
        if callable(persist):
            persist(snapshot, _now())
        else:
            self._persistence.save_application_state(STATE_KEY, snapshot, _now())
        finished_ns = time.perf_counter_ns()
        return {
            "snapshot_serialization_ms": round(
                (snapshot_finished_ns - snapshot_started_ns) / 1_000_000.0, 3),
            "database_persistence_ms": round(
                (finished_ns - snapshot_finished_ns) / 1_000_000.0, 3),
            "persistence_total_ms": round((finished_ns - started_ns) / 1_000_000.0, 3),
        }

    def _restore(self, state: dict[str, Any]) -> None:
        self._persistence_generation = int(state.get("_rules_engine_generation") or 0)
        self.configuration_version = int(state.get("configuration_version") or 0)
        original_rule_configuration = deepcopy(state.get("rule_configuration") or {})
        original_presets = deepcopy(state.get("presets") or {})
        original_sessions = deepcopy(state.get("sessions") or {})
        self.rule_configuration = deepcopy(state.get("rule_configuration") or {})
        self.presets = deepcopy(state.get("presets") or {})
        self.sessions = deepcopy(state.get("sessions") or {})
        self._performance_observers = {}
        self._performance_observer_error = state.get("rule_performance_observer_error")
        if self._performance_observer_enabled:
            for rule_id, saved in (state.get("rule_performance_observers") or {}).items():
                if not isinstance(saved, Mapping):
                    continue
                observer_engine = RulesEngine(None, performance_observer=False)
                observer_engine._restore(deepcopy(saved.get("engine") or {}))
                saved_history = deepcopy(saved.get("history") or [])
                # Older observer snapshots treated gaps in the global stream
                # sequence as missing rounds. Those empty windows contain no
                # evaluations or outcomes; remove only those false boundaries.
                saved_history = [window for window in saved_history
                    if not self._is_empty_false_collector_gap(window)]
                self._performance_observers[str(rule_id)] = {
                    "engine": observer_engine,
                    "history": saved_history,
                    "context": deepcopy(saved.get("context") or []),
                    "last_event": deepcopy(saved.get("last_event")),
                    "segment_start_round_index": int(saved.get("segment_start_round_index") or 0),
                    "rounds_processed": int(saved.get("rounds_processed") or 0),
                    "manual_reset_at": saved.get("manual_reset_at"),
                    "manual_reset_round_index": saved.get("manual_reset_round_index"),
                }
        # Old snapshots may contain controls from the removed loss-limit
        # subsystem. Keep result history but discard those controls on load.
        removed_controls = {
            "loss_limit_rwf",
            "reconciled_loss_count_limit_enabled", "reconciled_loss_count_limit",
            "reconciled_loss_limit_enabled", "reconciled_loss_limit_rwf",
            "reconciled_profit_target_enabled", "reconciled_profit_target_rwf",
            "reconciled_win_limit_enabled", "reconciled_win_limit_count",
        }
        for row in self.rule_configuration.values():
            params = row.get("parameters") if isinstance(row, dict) else None
            if isinstance(params, dict):
                for key in removed_controls:
                    params.pop(key, None)
        for preset in self.presets.values():
            options = preset.get("options") if isinstance(preset, dict) else None
            if isinstance(options, dict):
                if options.get("net_outcome_limit_version") != 1:
                    options.pop("loss_limit_enabled", None)
                    options.pop("loss_limit_count", None)
                for key in removed_controls:
                    options.pop(key, None)
        for session in self.sessions.values():
            saved_selections = session.get("selections")
            if isinstance(saved_selections, list):
                normalized_selections: list[dict[str, Any]] = []
                selection_positions: dict[str, int] = {}
                for selection in saved_selections:
                    if not isinstance(selection, dict):
                        continue
                    selection_id = str(selection.get("selection_id") or selection.get("decision_id") or "")
                    if not selection_id:
                        normalized_selections.append(selection)
                        continue
                    position = selection_positions.get(selection_id)
                    if position is None:
                        selection_positions[selection_id] = len(normalized_selections)
                        normalized_selections.append(selection)
                        continue
                    previous = normalized_selections[position]
                    # The same shared target was accidentally persisted twice
                    # by older builds. Keep one identity and retain its most
                    # advanced settlement fields.
                    if previous.get("status") == "PENDING" and selection.get("status") != "PENDING":
                        normalized_selections[position] = {**previous, **selection}
                    else:
                        for key, value in selection.items():
                            if previous.get(key) in (None, "", [], {}) and value not in (None, "", [], {}):
                                previous[key] = value
                if len(normalized_selections) != len(saved_selections):
                    session["selections"] = normalized_selections
            if "total_cancelled" not in session:
                selections = session.get("selections") or []
                cancelled_ids = {str(row.get("selection_id") or row.get("decision_id"))
                    for row in selections if isinstance(row, Mapping)
                    and row.get("status") == "CANCELLED"
                    and (row.get("selection_id") or row.get("decision_id"))}
                anonymous_cancelled = sum(1 for row in selections
                    if isinstance(row, Mapping) and row.get("status") == "CANCELLED"
                    and not (row.get("selection_id") or row.get("decision_id")))
                session["total_cancelled"] = len(cancelled_ids) + anonymous_cancelled
            if session.get("status") in {"RUNNING", "SETTLING", "PAUSED"}:
                engines = session.setdefault("engines", {})
                versions = session.setdefault("rule_versions", {})
                manifest_changed = False
                for rule_id in session.get("rule_ids") or []:
                    if rule_id not in self._performance_rule_ids():
                        continue
                    rule_state = engines.get(rule_id)
                    if not isinstance(rule_state, dict):
                        rule_state = _fresh_engine(rule_id)
                        engines[rule_id] = rule_state
                    lifecycle_defaults = _fresh_engine(rule_id)
                    for key in ("cycle_mode", "cycle", "entry_signal_latched",
                                "loss_limit_losses", "loss_limit_blocked",
                                "loss_limit_reached_at", "loss_limit_reached_round_index",
                                "loss_limit_terminal_round_index"):
                        default = lifecycle_defaults[key]
                        if key == "entry_signal_latched":
                            cycle = rule_state.get("cycle")
                            default = bool(rule_state.get("pending_selection_id")
                                           or (isinstance(cycle, Mapping) and cycle.get("active")))
                        if key not in rule_state:
                            rule_state[key] = deepcopy(default)
                    if (rule_id in {START_CHECK_POLICY_ID, LEGACY_POLICY_ID, COMBINED_POLICY_ID}
                            and rule_state.get("rule_state") == "PLAYING"
                            and not (rule_state.get("cycle") or {}).get("active")):
                        # Older running policy sessions did not persist an
                        # explicit cycle. Rebuild its open cycle from the
                        # reducer state and in-flight target.
                        pending_id = rule_state.get("pending_selection_id")
                        selections = session.get("rule_selections", {}).get(rule_id) or []
                        pending = next((item for item in selections
                                        if item.get("selection_id") == pending_id), None)
                        selected_outcomes = list(rule_state.get("selected_outcomes") or [])
                        cycle_start = pending or next((item for item in reversed(selections)
                                                       if item.get("status") in {"WIN", "LOSS"}), None)
                        entry_ref = (cycle_start or {}).get("source_round") or {}
                        cycle_id = str(uuid5(NAMESPACE_URL, "winner-predict-restored-cycle:" + ":".join((
                            str(session.get("session_id") or ""), rule_id,
                            str(rule_state.get("cycle_number") or 1),
                            str(entry_ref.get("roundId", entry_ref.get("round_id")) or "unknown"),
                        ))))
                        cycle = {
                            "cycle_id": cycle_id, "active": True, "status": "ACTIVE",
                            "requested_count": len(selected_outcomes) + int(bool(pending_id)),
                            "resolved_count": len(selected_outcomes),
                            "loss_count": sum(value == "L" for value in selected_outcomes),
                            "maximum_targets": None,
                            "end_condition": "Policy stop transition or two losses; then wait for a fresh entry.",
                            "entry_round_id": entry_ref.get("roundId", entry_ref.get("round_id")),
                            "started_at": (cycle_start or {}).get("selected_at"),
                            "restored_from_policy_state": True,
                        }
                        rule_state["cycle"] = cycle
                        rule_state["cycle_mode"] = "POLICY_CYCLE"
                        if pending is not None:
                            pending.setdefault("rule_cycle_ids", {})[rule_id] = cycle_id
                    previous_version = versions.get(rule_id)
                    current_version = self._version(rule_id)
                    if previous_version != current_version:
                        old_key = self._performance_configuration_key(
                            rule_id, rule_version=previous_version) if previous_version else None
                        saved_key = (session.get("rule_configuration_keys") or {}).get(rule_id)
                        if old_key is not None and saved_key == old_key:
                            session.setdefault("rule_configuration_keys", {})[rule_id] = (
                                self._performance_configuration_key(rule_id))
                        versions[rule_id] = current_version
                        manifest_changed = True
                if manifest_changed:
                    session["manifest"] = self._manifest(session.get("rule_ids") or [])
            config = session.get("configuration_snapshot")
            if isinstance(config, dict):
                if config.get("net_outcome_limit_version") != 1:
                    config.pop("loss_limit_enabled", None)
                    config.pop("loss_limit_count", None)
                for key in removed_controls:
                    config.pop(key, None)
            session.pop("financial_stop", None)
            self._normalize_legacy_limit_stop(session)
            self._ensure_legacy_stop_provenance(session)
            self._recover_auto_next_boundary(session)
            if self._effective_session_status(session) == "COMPLETED":
                # Older persisted snapshots can retain SETTLING after their
                # full verified observation window was already consumed.
                session["status"] = "COMPLETED"
            if session.get("status") == "RUNNING":
                if session.get("stop_reason"):
                    # A persisted session-level stop is fail-closed if an old
                    # snapshot also claimed it was still RUNNING.
                    session["status"] = "PAUSED"
                elif (session.get("selection_status") == "STOPPED"
                      and session.get("selection_stop_reason")):
                    session["status"] = "SETTLING"
            self._sync_automatic_candidate_lifecycle(session)
        self.last_evaluation = deepcopy(state.get("last_evaluation"))
        self.last_evaluation_at = state.get("last_evaluation_at")
        self.evaluation_count = int(state.get("evaluation_count") or 0)
        if (self._persistence is not None
                and (self.rule_configuration != original_rule_configuration
                     or self.presets != original_presets
                     or self.sessions != original_sessions)):
            # Persist stop-state normalization without deleting any historical
            # stop events, so stale reasons do not return after the next boot.
            self._persist()

    def _upgrade_rule_performance_windows(self) -> bool:
        """Convert fixed observer batches to rolling windows without losing live progress."""
        changed = False
        for rule_id, observer in self._performance_observers.items():
            engine = observer.get("engine")
            if not isinstance(engine, RulesEngine):
                continue
            for session in engine.sessions.values():
                if session.get("status") not in {"RUNNING", "SETTLING"}:
                    changed = self._trim_performance_observer_ledger(session) or changed
                    continue
                configuration = session.get("configuration_snapshot")
                if isinstance(configuration, dict):
                    already_rolling = (configuration.get("performance_window_size")
                                       == RULE_PERFORMANCE_WINDOW_SIZE
                                       and configuration.get("run_until_stopped") is True
                                       and session.get("window_size") is None)
                    configuration.update({"window_size": None, "max_observed_rounds": None,
                                           "run_until_stopped": True,
                                           "performance_window_size": RULE_PERFORMANCE_WINDOW_SIZE})
                    session["window_size"] = None
                    session["run_until_stopped"] = True
                    if session.get("performance_segment_start_round_index") is None:
                        boundary = (session.get("start_boundary") or {}).get("roundIndex")
                        try:
                            session["performance_segment_start_round_index"] = int(boundary) + 1
                        except (TypeError, ValueError, OverflowError):
                            session["performance_segment_start_round_index"] = int(
                                observer.get("segment_start_round_index") or 0)
                    if not already_rolling:
                        changed = True
                changed = self._trim_performance_observer_ledger(session) or changed
            history = observer.get("history") or []
            latest = observer.get("last_event") or {}
            if (history and history[-1].get("completed_window") is True
                    and not history[-1].get("interruption_reason")
                    and history[-1].get("performance_configuration_key")
                        == self._performance_configuration_key(rule_id)
                    and str(history[-1].get("stream_id") or "")
                        == str(latest.get("stream_id") or "")
                    and str(history[-1].get("last_round_index") or "")
                        == str(latest.get("round_index") or "")
                    and self._performance_event_is_recent(latest)):
                if self._rehydrate_rolling_performance_window(observer, rule_id, latest):
                    changed = True
        return changed

    @staticmethod
    def _performance_event_is_recent(event: Mapping[str, Any]) -> bool:
        timestamp = _parse_time(event.get("api_received_at") or event.get("observed_at"))
        if timestamp is None:
            return False
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - timestamp.astimezone(timezone.utc)).total_seconds()
        return -60 <= age <= RULE_PERFORMANCE_IDLE_RESET_SECONDS

    def _rehydrate_rolling_performance_window(self, observer: dict[str, Any], rule_id: str,
                                               latest: Mapping[str, Any]) -> bool:
        """Seed the new rolling window from canonical committed rounds after a full batch."""
        snapshot_reader = getattr(self._persistence, "round_distribution_snapshot", None)
        event_reader = getattr(self._persistence, "round_distribution_events", None)
        if not callable(snapshot_reader) or not callable(event_reader):
            return False
        try:
            snapshot = snapshot_reader()
            stream_id = str(snapshot.get("stream_id") or "")
            latest_sequence = int(snapshot.get("latest_sequence") or 0)
            after_sequence = max(int(snapshot.get("earliest_sequence") or 1) - 1,
                                 latest_sequence - 500)
            events = event_reader(stream_id, after_sequence, latest_sequence, limit=500)
        except Exception:
            return False
        committed = [row for row in events if isinstance(row, Mapping)
                     and row.get("event_type") == "round.persisted"
                     and row.get("durability") == "COMMITTED"
                     and row.get("continuity_status") == "VERIFIED"
                     and row.get("stream_id") == stream_id]
        if len(committed) < RULE_PERFORMANCE_WINDOW_SIZE + 5:
            return False
        tail = committed[-(RULE_PERFORMANCE_WINDOW_SIZE + 5):]
        try:
            indices = [int(row.get("round_index")) for row in tail]
            sequences = [int(row.get("stream_sequence")) for row in tail]
        except (TypeError, ValueError, OverflowError):
            return False
        if (any(right != left + 1 for left, right in zip(indices, indices[1:]))
                or any(right <= left for left, right in zip(sequences, sequences[1:]))
                or tail[-1].get("round_id") != latest.get("round_id")
                or tail[-1].get("stream_id") != latest.get("stream_id")):
            return False
        prestart = tail[:5]
        replay = tail[5:]
        first_timestamp = _parse_time(replay[0].get("observed_at")
                                      or replay[0].get("api_received_at"))
        if first_timestamp is None:
            return False
        if first_timestamp.tzinfo is None:
            first_timestamp = first_timestamp.replace(tzinfo=timezone.utc)
        started_at = (first_timestamp.astimezone(timezone.utc)
                      - timedelta(microseconds=1)).isoformat()
        engine = observer["engine"]
        self._configure_performance_engine(rule_id, engine)
        options = {"window_size": None, "max_observed_rounds": None,
                   "run_until_stopped": True,
                   "performance_window_size": RULE_PERFORMANCE_WINDOW_SIZE,
                   "profit_target_enabled": False,
                   "win_limit_enabled": False,
                   "loss_limit_enabled": False,
                   "auto_next_check_enabled": False,
                   "auto_next_on_limit_enabled": False,
                   "fixed_stake_rwf": 100, "stake_currency": "RWF"}
        previous_sessions = engine.sessions
        engine.sessions = {}
        try:
            created = engine.start_session(mode=self._performance_mode(rule_id),
                rule_ids=[rule_id], boundary=prestart[-1], prestart_context=prestart,
                options=options, now=started_at)
            session = engine.sessions[created["session_id"]]
            session["performance_configuration_key"] = self._performance_configuration_key(rule_id)
            session["performance_segment_start_round_index"] = indices[5]
            session["configuration_snapshot"].update({
                "window_size": None, "max_observed_rounds": None,
                "run_until_stopped": True,
                "performance_window_size": RULE_PERFORMANCE_WINDOW_SIZE,
            })
            for event in replay:
                engine.observe_round(event, target_already_completed=False)
        except Exception:
            engine.sessions = previous_sessions
            return False
        observer["context"] = deepcopy(replay[-5:])
        history = observer.get("history") or []
        if history and history[-1].get("completed_window") is True:
            history.pop()
        return True

    @staticmethod
    def _performance_rule_ids() -> list[str]:
        return [START_CHECK_POLICY_ID, LEGACY_POLICY_ID,
                *[row["rule_id"] for row in RESEARCH_RULES], COMBINED_POLICY_ID]

    @staticmethod
    def _performance_mode(rule_id: str) -> str:
        if rule_id == START_CHECK_POLICY_ID:
            return "START_CHECK"
        if rule_id == LEGACY_POLICY_ID:
            return "LEGACY_SEPARATE_TRACKER"
        if rule_id == COMBINED_POLICY_ID:
            return "ALL_RULES_TOGETHER"
        return "FIVE_RULE_TRACKER"

    def _performance_configuration_key(self, rule_id: str, *, rule_version: str | None = None) -> str:
        definition = self._definition(rule_id)
        config = self.rule_configuration.get(rule_id) or {}
        effective = {"rule_version": rule_version or definition["version"],
                     "parameters": config.get("parameters") or definition.get("parameters") or {}}
        encoded = json.dumps(effective, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def _ensure_automatic_rule_candidates(self, session: dict[str, Any]) -> None:
        """Keep every policy in an automatic session's mode available for recovery."""
        allowed = self._allowed_rules(str(session.get("mode") or ""))
        rule_ids = session.setdefault("rule_ids", [])
        changed = False
        for rule_id in self._performance_rule_ids():
            if rule_id not in allowed:
                continue
            if rule_id not in rule_ids:
                rule_ids.append(rule_id)
                changed = True
            session.setdefault("rule_versions", {}).setdefault(rule_id, self._version(rule_id))
            session.setdefault("engines", {}).setdefault(rule_id, _fresh_engine(rule_id))
            session.setdefault("rule_selections", {}).setdefault(rule_id, [])
            session.setdefault("rule_configuration_keys", {}).setdefault(
                rule_id, self._performance_configuration_key(rule_id))
        if changed:
            session["manifest"] = self._manifest(rule_ids)

    @staticmethod
    def _sync_automatic_candidate_lifecycle(session: dict[str, Any]) -> None:
        """Refresh visible per-rule cycle/stop state without changing its score."""
        choice = session.get("automatic_rule_choice")
        if not isinstance(choice, dict):
            return
        engines = session.get("engines") or {}
        for candidate in choice.get("candidates") or []:
            if not isinstance(candidate, dict):
                continue
            state = engines.get(candidate.get("rule_id")) or {}
            cycle = state.get("cycle") or {}
            candidate.update({
                "loss_limit": RULE_LOSS_LIMIT,
                "loss_limit_losses": int(state.get("loss_limit_losses") or 0),
                "loss_limit_blocked": bool(state.get("loss_limit_blocked")),
                "loss_limit_reached_round_index": state.get("loss_limit_reached_round_index"),
                "entry_signal_latched": bool(state.get("entry_signal_latched")),
                "rule_state": state.get("rule_state"),
                "cycle_status": cycle.get("status") or "IDLE",
                "cycle_active": bool(cycle.get("active")),
                "cycle_requested_targets": int(cycle.get("requested_count") or 0),
                "cycle_resolved_targets": int(cycle.get("resolved_count") or 0),
                "cycle_loss_count": int(cycle.get("loss_count") or 0),
                "cycle_maximum_targets": cycle.get("maximum_targets"),
                "cycle_end_condition": cycle.get("end_condition"),
                "cycle_end_reason": cycle.get("end_reason"),
            })

    def _automatically_enable_qualified_rule(self, rule_id: str) -> bool:
        """Persist the enabled state when an automatic candidate earns its way back."""
        current = self.rule_configuration.get(rule_id) or {}
        if current.get("configured") is True and current.get("enabled") is True:
            return False
        parameters = deepcopy(current.get("parameters") or {})
        if rule_id in {START_CHECK_POLICY_ID, LEGACY_POLICY_ID}:
            defaults = {"profit_target_enabled": True, "profit_target_rwf": 400,
                        "window_size": WINDOW_SIZE, "auto_next_check_enabled": False,
                        "auto_next_on_limit_enabled": False}
            defaults.update(parameters)
            parameters = defaults
        else:
            parameters = {}
        self.configuration_version += 1
        self.rule_configuration[rule_id] = {
            "configured": True,
            "enabled": True,
            "version": self.configuration_version,
            "parameters": parameters,
        }
        return True

    @staticmethod
    def _performance_event_checkpoint(event: Mapping[str, Any]) -> dict[str, Any]:
        fields = ("stream_id", "stream_sequence", "round_id", "round_index", "multiplier",
                  "event_type", "durability", "continuity_status", "observed_at",
                  "api_received_at", "delivery_mode")
        return {key: deepcopy(event.get(key)) for key in fields if event.get(key) is not None}

    def _performance_observer(self, rule_id: str) -> dict[str, Any]:
        observer = self._performance_observers.get(rule_id)
        if observer is None:
            observer = {"engine": RulesEngine(None, performance_observer=False),
                        "history": [], "context": [], "last_event": None,
                        "segment_start_round_index": 0,
                        "rounds_processed": 0,
                        "manual_reset_at": None,
                        "manual_reset_round_index": None}
            self._performance_observers[rule_id] = observer
        return observer

    def _configure_performance_engine(self, rule_id: str, engine: "RulesEngine") -> None:
        source_config = self.rule_configuration.get(rule_id) or {}
        definition = self._definition(rule_id)
        params = deepcopy(source_config.get("parameters") or definition.get("parameters") or {})
        engine.configuration_version = max(engine.configuration_version, self.configuration_version, 1)
        engine.rule_configuration[rule_id] = {
            "configured": True, "enabled": True,
            "version": int(source_config.get("version") or engine.configuration_version),
            "parameters": params,
        }

    @staticmethod
    def _performance_window(session: Mapping[str, Any], rule_id: str, *,
                            interruption: str | None = None) -> dict[str, Any]:
        configuration = session.get("configuration_snapshot") or {}
        raw_window_size = configuration.get("performance_window_size", RULE_PERFORMANCE_WINDOW_SIZE)
        window_size = (int(raw_window_size) if isinstance(raw_window_size, int)
                       and not isinstance(raw_window_size, bool) and raw_window_size > 0
                       else RULE_PERFORMANCE_WINDOW_SIZE)
        start_index = session.get("performance_segment_start_round_index")
        if start_index is None:
            boundary = (session.get("start_boundary") or {}).get("roundIndex")
            try:
                start_index = int(boundary) + 1
            except (TypeError, ValueError, OverflowError):
                start_index = 1
        try:
            start_index = int(start_index)
            last_round_index = int(session.get("last_round_index") or 0)
        except (TypeError, ValueError, OverflowError):
            start_index = 1
            last_round_index = 0
        window_start_index = max(start_index, last_round_index - window_size + 1)

        def source_index(row: Mapping[str, Any]) -> int | None:
            source = row.get("source_round") or row.get("sourceRound") or {}
            value = source.get("roundIndex", source.get("round_index")) if isinstance(source, Mapping) else None
            try:
                return int(value)
            except (TypeError, ValueError, OverflowError):
                return None

        selections = list((session.get("rule_selections") or {}).get(rule_id) or [])
        if not selections:
            selections = [item for item in session.get("selections") or []
                          if rule_id in (item.get("matched_rule_ids")
                                         or item.get("contributing_rule_ids") or [])]
        selections = [item for item in selections
                      if window_start_index <= (source_index(item) or 0) <= last_round_index]
        wins = sum(str(item.get("status") or item.get("result") or "").upper() == "WIN" for item in selections)
        losses = sum(str(item.get("status") or item.get("result") or "").upper() == "LOSS" for item in selections)
        pending = sum(str(item.get("status") or item.get("result") or "").upper()
                      in {"PENDING", "UNKNOWN", ""} for item in selections)
        eligible_checks = false_checks = recovered_rounds = 0
        observed_indices: set[int] = set()
        for evaluation in session.get("evaluations") or []:
            index = source_index(evaluation)
            if index is None or not window_start_index <= index <= last_round_index:
                continue
            observed_indices.add(index)
            commit_evidence = evaluation.get("commit_evidence") or {}
            if str(commit_evidence.get("delivery_mode") or "").upper() == "RECOVERED":
                recovered_rounds += 1
            for check in evaluation.get("rule_evaluations") or []:
                if check.get("rule_id") != rule_id:
                    continue
                eligible = check.get("eligible", check.get("result"))
                eligible_checks += eligible is True
                false_checks += eligible is False
        status = RulesEngine._effective_session_status(session)
        observed_rounds = min(window_size, max(len(observed_indices),
            min(window_size, int(session.get("observed_rounds") or 0))))
        complete = interruption is None and observed_rounds >= window_size and pending == 0
        return {
            "rule_id": rule_id, "name": RulesEngine._definition(rule_id)["name"],
            "session_id": session.get("session_id"), "mode": session.get("mode"),
            "stream_id": session.get("last_stream_id"),
            "last_round_index": session.get("last_round_index"),
            "performance_configuration_key": session.get("performance_configuration_key"),
            "session_status": ("INTERRUPTED" if interruption else status),
            "started_at": session.get("started_at"), "ended_at": session.get("ended_at"),
            "observed_rounds": observed_rounds, "window_size": window_size,
            "rolling_window": True, "window_start_round_index": window_start_index,
            "window_end_round_index": last_round_index,
            "wins": wins, "losses": losses, "pending": pending,
            "eligible_checks": eligible_checks, "false_checks": false_checks,
            "recovered_rounds": recovered_rounds,
            "direct_selections": wins + losses + pending, "shared_attributions": 0,
            "settled": wins + losses, "net": wins - losses,
            "completed_window": complete, "interruption_reason": interruption,
        }

    @staticmethod
    def _is_empty_false_collector_gap(window: Any) -> bool:
        if not isinstance(window, Mapping) or window.get("interruption_reason") != "COLLECTOR_EVENT_GAP":
            return False
        try:
            return all(int(window.get(key) or 0) == 0 for key in (
                "observed_rounds", "eligible_checks", "false_checks", "wins", "losses"))
        except (TypeError, ValueError, OverflowError):
            return False

    def _archive_performance_windows(self, observer: dict[str, Any], rule_id: str, *,
                                     interruption: str | None = None,
                                     force: bool = False) -> None:
        engine = observer["engine"]
        for session_id, session in list(engine.sessions.items()):
            status = engine._effective_session_status(session)
            has_pending = self._pending_shared_result_count(session) > 0
            terminal = status in {"COMPLETED", "MANUALLY_STOPPED", "RESET", "RECONFIGURED", "PAUSED"}
            if not force and (not terminal or has_pending):
                continue
            window = self._performance_window(session, rule_id, interruption=interruption)
            observer["history"].append(window)
            observer["history"] = observer["history"][-250:]
            engine.sessions.pop(session_id, None)

    def _start_performance_window(self, observer: dict[str, Any], rule_id: str,
                                  boundary: Mapping[str, Any]) -> None:
        engine = observer["engine"]
        self._configure_performance_engine(rule_id, engine)
        options = {
            "window_size": None,
            "max_observed_rounds": None,
            "run_until_stopped": True,
            "performance_window_size": RULE_PERFORMANCE_WINDOW_SIZE,
            "profit_target_enabled": False,
            "win_limit_enabled": False,
            "loss_limit_enabled": False,
            "auto_next_check_enabled": False,
            "auto_next_on_limit_enabled": False,
            "fixed_stake_rwf": 100, "stake_currency": "RWF",
        }
        created = engine.start_session(
            mode=self._performance_mode(rule_id), rule_ids=[rule_id], boundary=boundary,
            prestart_context=deepcopy(observer.get("context") or []), options=options,
        )
        session = engine.sessions[created["session_id"]]
        session["performance_configuration_key"] = self._performance_configuration_key(rule_id)
        session["performance_segment_start_round_index"] = int(boundary["round_index"]) + 1
        session["window_size"] = None
        session["run_until_stopped"] = True
        session["configuration_snapshot"].update({
            "window_size": None, "max_observed_rounds": None,
            "run_until_stopped": True,
            "performance_window_size": RULE_PERFORMANCE_WINDOW_SIZE,
        })

    @staticmethod
    def _trim_performance_observer_ledger(session: dict[str, Any]) -> bool:
        """Bound private observer history to the rolling window plus a small cushion."""
        if ((session.get("configuration_snapshot") or {}).get("performance_window_size")
                != RULE_PERFORMANCE_WINDOW_SIZE):
            return False
        changed = False
        evaluations = session.get("evaluations") or []
        if len(evaluations) > RULE_PERFORMANCE_WINDOW_SIZE + 5 or any(
                isinstance(row, Mapping) and ("state_snapshot" in row or len(row) > 5)
                for row in evaluations[-(RULE_PERFORMANCE_WINDOW_SIZE + 5):]):
            compact = []
            for row in evaluations[-(RULE_PERFORMANCE_WINDOW_SIZE + 5):]:
                if not isinstance(row, Mapping):
                    continue
                checks = [{"rule_id": check.get("rule_id"),
                           "eligible": check.get("eligible", check.get("result"))}
                          for check in (row.get("rule_evaluations") or [])
                          if isinstance(check, Mapping) and check.get("rule_id")]
                evidence = row.get("commit_evidence") or {}
                compact.append({
                    "source_round": row.get("source_round") or {},
                    "evaluated_at": row.get("evaluated_at"),
                    "rule_evaluations": checks,
                    "commit_evidence": ({"delivery_mode": evidence.get("delivery_mode")}
                                         if isinstance(evidence, Mapping) else {}),
                })
            session["evaluations"] = compact
            changed = True
        for key, keep in (("decisions", 0), ("selections", 200)):
            rows = session.get(key) or []
            if len(rows) > keep:
                session[key] = rows[-keep:] if keep else []
                changed = True
        rule_selections = session.get("rule_selections") or {}
        trimmed_rule_selections = {
            rule_id: rows[-200:] if len(rows) > 200 else rows
            for rule_id, rows in rule_selections.items()
        }
        if any(len(rows or []) > 200 for rows in rule_selections.values()):
            session["rule_selections"] = trimmed_rule_selections
            changed = True
        event_records = session.get("event_records")
        if isinstance(event_records, dict) and event_records:
            pending = {key: value for key, value in event_records.items()
                       if isinstance(value, Mapping)
                       and value.get("persistence_status") == "PENDING"}
            if len(pending) != len(event_records):
                session["event_records"] = pending
                changed = True
        return changed

    def _observe_rule_performance_round(self, event: Mapping[str, Any]) -> bool:
        """Track each registered rule in an isolated, report-only collector session."""
        if not self._performance_observer_enabled:
            return False
        changed = False
        checkpoint = self._performance_event_checkpoint(event)
        stream_id = str(checkpoint.get("stream_id") or "")
        round_id = str(checkpoint.get("round_id") or "")
        try:
            sequence = int(checkpoint.get("stream_sequence") or 0)
        except (TypeError, ValueError, OverflowError):
            return False
        if not stream_id or not round_id or sequence < 1:
            return False

        for rule_id in self._performance_rule_ids():
            observer = self._performance_observer(rule_id)
            engine = observer["engine"]
            current_key = self._performance_configuration_key(rule_id)
            if any(row.get("performance_configuration_key") != current_key
                   for row in engine.sessions.values()):
                self._archive_performance_windows(
                    observer, rule_id, interruption="CONFIGURATION_CHANGED", force=True)
                engine.sessions.clear()
                observer["segment_start_round_index"] = int(checkpoint.get("round_index") or 0)
                changed = True
            prior = observer.get("last_event") or {}
            prior_stream = str(prior.get("stream_id") or "")
            try:
                prior_sequence = int(prior.get("stream_sequence") or 0)
            except (TypeError, ValueError, OverflowError):
                prior_sequence = 0
            try:
                round_index = int(checkpoint.get("round_index") or 0)
                prior_round_index = int(prior.get("round_index") or 0)
            except (TypeError, ValueError, OverflowError):
                round_index = prior_round_index = 0
            if prior_stream == stream_id and sequence <= prior_sequence:
                # Ordered inbox replay is idempotent. A conflicting or older
                # event must not rewrite already scored source/target pairs.
                if sequence < prior_sequence:
                    continue
                same_committed_round = (
                    str(prior.get("round_id") or "") == round_id
                    and prior_round_index == round_index
                    and _multiplier(prior.get("multiplier")) == _multiplier(checkpoint.get("multiplier"))
                )
                if same_committed_round:
                    continue
                self._archive_performance_windows(
                    observer, rule_id, interruption="ROUND_IDENTITY_CONFLICT", force=True)
                engine.sessions.clear()
                observer["context"] = []
                observer["segment_start_round_index"] = round_index + 1
                observer["last_event"] = {**checkpoint, "continuity_status": "CONFLICT"}
                observer["rounds_processed"] = int(observer.get("rounds_processed") or 0) + 1
                changed = True
                continue

            continuity = checkpoint.get("continuity_status")
            stream_changed = bool(prior_stream and prior_stream != stream_id)
            round_gap = bool(prior_stream == stream_id and prior_round_index
                             and round_index != prior_round_index + 1)
            idle_reset = False
            if (str(checkpoint.get("delivery_mode") or "LIVE").upper() != "RECOVERED"
                    and prior_stream == stream_id and prior_sequence and sequence > prior_sequence):
                current_at = _parse_time(checkpoint.get("api_received_at")
                                         or checkpoint.get("observed_at"))
                prior_at = _parse_time(prior.get("api_received_at")
                                       or prior.get("observed_at"))
                if current_at is not None and prior_at is not None:
                    if current_at.tzinfo is None:
                        current_at = current_at.replace(tzinfo=timezone.utc)
                    if prior_at.tzinfo is None:
                        prior_at = prior_at.replace(tzinfo=timezone.utc)
                    idle_seconds = (current_at.astimezone(timezone.utc)
                                    - prior_at.astimezone(timezone.utc)).total_seconds()
                    idle_reset = idle_seconds > RULE_PERFORMANCE_IDLE_RESET_SECONDS
            if idle_reset:
                self._archive_performance_windows(
                    observer, rule_id, interruption="INACTIVE_FEED", force=True)
                engine.sessions.clear()
                observer["context"] = []
                observer["segment_start_round_index"] = round_index + 1
                changed = True
            if stream_changed or round_gap:
                reason = ("STREAM_CHANGED" if stream_changed else
                          "ROUND_INDEX_GAP")
                self._archive_performance_windows(observer, rule_id,
                                                  interruption=reason, force=True)
                engine.sessions.clear()
                observer["context"] = []
                observer["segment_start_round_index"] = round_index + 1
                changed = True

            if continuity != "VERIFIED":
                # Let the isolated ledger mark any outstanding target as
                # skipped by the committed gap, then retain that partial
                # window as interrupted. It cannot bridge unknown rounds.
                if engine.sessions:
                    engine.observe_round(event, target_already_completed=False)
                    self._archive_performance_windows(observer, rule_id,
                                                      interruption="ROUND_GAP", force=True)
                    engine.sessions.clear()
                observer["context"] = []
                observer["segment_start_round_index"] = round_index + 1
                observer["last_event"] = checkpoint
                observer["rounds_processed"] = int(observer.get("rounds_processed") or 0) + 1
                changed = True
                continue

            active = any(engine._effective_session_status(session) in {"RUNNING", "SETTLING"}
                         for session in engine.sessions.values())
            skip_current = False
            if not active:
                boundary = (checkpoint if idle_reset or stream_changed or round_gap else
                            prior if prior.get("continuity_status") == "VERIFIED" and prior_stream == stream_id
                            else checkpoint)
                self._start_performance_window(observer, rule_id, boundary)
                try:
                    boundary_sequence = int(boundary.get("stream_sequence") or 0)
                except (TypeError, ValueError, OverflowError):
                    boundary_sequence = 0
                skip_current = boundary_sequence == sequence
            if not skip_current:
                engine.observe_round(event, target_already_completed=False)
                for observed_session in engine.sessions.values():
                    self._trim_performance_observer_ledger(observed_session)
                behavior_context = self._adaptive_context_signature(
                    [*(observer.get("context") or []), checkpoint])
                for observed_session in engine.sessions.values():
                    for evaluation in observed_session.get("evaluations") or []:
                        ref = evaluation.get("source_round") or {}
                        if (ref.get("streamId", ref.get("stream_id")) == stream_id
                                and ref.get("roundIndex", ref.get("round_index")) == round_index):
                            evaluation["behavior_context"] = behavior_context
                    for selection in list(observed_session.get("selections") or []) + [
                            item for group in (observed_session.get("rule_selections") or {}).values()
                            for item in group]:
                        ref = selection.get("source_round") or {}
                        if (ref.get("streamId", ref.get("stream_id")) == stream_id
                                and ref.get("roundIndex", ref.get("round_index")) == round_index):
                            selection["behavior_context"] = behavior_context

            context = observer.setdefault("context", [])
            if not context or context[-1].get("round_id") != round_id:
                context.append(deepcopy(checkpoint))
                observer["context"] = context[-5:]
            observer["last_event"] = checkpoint
            observer["rounds_processed"] = int(observer.get("rounds_processed") or 0) + 1

            # Keep one observer session alive. Its performance view is a rolling
            # 100-round slice; 100/100 is full, not a reason to reset its cursor.
            # Only a continuity interruption or a prolonged feed idle archives it.
            self._archive_performance_windows(observer, rule_id)
            changed = True
        return changed

    def observe_rule_performance_round(self, event: Mapping[str, Any], *,
                                       defer_persistence: bool = False) -> bool:
        """Update the report-only observer after latency-sensitive signal work."""
        if (not self._performance_observer_enabled
                or event.get("event_type") != "round.persisted"
                or event.get("durability") != "COMMITTED"):
            return False
        with self._lock:
            try:
                changed = self._observe_rule_performance_round(event)
                self._performance_observer_error = None
            except Exception as exc:
                # Reporting must not block or reject delivery of a committed
                # signal. The durable inbox can replay the event after error.
                self._performance_observer_error = type(exc).__name__
                changed = True
                log.exception("rule performance collector observer failed round=%s",
                              event.get("round_id"))
            if changed and not defer_persistence:
                self._persist(session_ids={RULE_PERFORMANCE_CHECKPOINT_ID})
            return changed

    def _persist(self, *, session_ids: set[str] | None = None) -> dict[str, float]:
        if self._persistence is not None:
            started_ns = time.perf_counter_ns()
            with self._lock:
                snapshot_started_ns = time.perf_counter_ns()
                self._persistence_generation += 1
                snapshot = self._snapshot(session_ids=session_ids)
                snapshot_finished_ns = time.perf_counter_ns()
                persist = getattr(self._persistence, "save_rules_engine_snapshot", None)
                if callable(persist):
                    persist(snapshot, _now())
                else:
                    # Legacy persistence adapters have no session-level upsert;
                    # they must keep receiving the complete snapshot.
                    fallback_snapshot = self._snapshot() if session_ids is not None else snapshot
                    self._persistence.save_application_state(STATE_KEY, fallback_snapshot, _now())
                finished_ns = time.perf_counter_ns()
                return {
                    "snapshot_serialization_ms": round(
                        (snapshot_finished_ns - snapshot_started_ns) / 1_000_000.0, 3),
                    "database_persistence_ms": round(
                        (finished_ns - snapshot_finished_ns) / 1_000_000.0, 3),
                    "persistence_total_ms": round(
                        (finished_ns - started_ns) / 1_000_000.0, 3),
                }
        return {"snapshot_serialization_ms": 0.0,
                "database_persistence_ms": 0.0, "persistence_total_ms": 0.0}

    def _record_delivery_metric(self, stage: str, event_id: str, value_ms: float) -> None:
        values = self._delivery_metrics.get(stage)
        if values is None or not event_id:
            return
        seen = self._delivery_metric_ids[stage]
        order = self._delivery_metric_order[stage]
        if event_id in seen:
            return
        seen.add(event_id)
        order.append(event_id)
        if len(order) > 10000:
            seen.discard(order.popleft())
        if math.isfinite(value_ms) and value_ms >= 0:
            values.append(float(value_ms))

    @staticmethod
    def _latency_summary(values: deque[float]) -> dict[str, Any]:
        ordered = sorted(values)
        if not ordered:
            return {"samples": 0, "average_ms": None, "p50_ms": None,
                    "p95_ms": None, "p99_ms": None, "max_ms": None}

        def percentile(probability: float) -> float:
            index = min(len(ordered) - 1, max(0, math.ceil(probability * len(ordered)) - 1))
            return round(ordered[index], 3)

        return {"samples": len(ordered), "average_ms": round(sum(ordered) / len(ordered), 3),
                "p50_ms": percentile(0.50),
                "p95_ms": percentile(0.95), "p99_ms": percentile(0.99),
                "max_ms": round(ordered[-1], 3)}

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
                     "window_size": WINDOW_SIZE, "stop_semantics": "one shared target-bound selection"},
                ]}

    def configure(self, rule_id: str, *, enabled: bool, parameters: Mapping[str, Any] | None = None) -> dict[str, Any]:
        known = {START_CHECK_POLICY_ID, LEGACY_POLICY_ID, COMBINED_POLICY_ID, *_research_by_id}
        if rule_id not in known or not isinstance(enabled, bool):
            raise ValueError("INVALID_RULE_CONFIGURATION")
        allowed = {} if rule_id not in {START_CHECK_POLICY_ID, LEGACY_POLICY_ID} else {
            "profit_target_enabled", "profit_target_rwf", "window_size",
            "auto_next_check_enabled", "auto_next_on_limit_enabled",
        }
        values = dict(parameters or {})
        if set(values) - set(allowed):
            raise ValueError("UNSUPPORTED_RULE_PARAMETER")
        if rule_id not in {START_CHECK_POLICY_ID, LEGACY_POLICY_ID} and values:
            raise ValueError("RULE_HAS_NO_CONFIGURABLE_PARAMETERS")
        defaults = {"profit_target_enabled": True, "profit_target_rwf": 400,
                    "window_size": WINDOW_SIZE, "auto_next_check_enabled": False,
                    "auto_next_on_limit_enabled": False}
        if rule_id in {START_CHECK_POLICY_ID, LEGACY_POLICY_ID}:
            defaults.update(values)
            for key in ("profit_target_enabled", "auto_next_check_enabled",
                        "auto_next_on_limit_enabled"):
                if not isinstance(defaults[key], bool):
                    raise ValueError("INVALID_BOOLEAN_PARAMETER")
            for enabled_key, amount_key in (("profit_target_enabled", "profit_target_rwf"),):
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
            self._persist(session_ids=set())
            return deepcopy(self.rule_configuration[rule_id])

    def save_preset(self, name: str, mode: str, rule_ids: list[str], options: Mapping[str, Any] | None = None) -> dict[str, Any]:
        if not isinstance(name, str) or not name.strip() or mode not in MODES:
            raise ValueError("INVALID_PRESET")
        valid = self._allowed_rules(mode)
        if (not isinstance(rule_ids, list) or len(set(rule_ids)) != len(rule_ids)
                or any(rule_id not in valid for rule_id in rule_ids)):
            raise ValueError("INVALID_PRESET_RULES")
        preset_options = deepcopy(dict(options or {}))
        session_window = preset_options.get(
            "max_observed_rounds", preset_options.get("window_size", WINDOW_SIZE))
        requested_run_until_stopped = preset_options.get(
            "run_until_stopped", session_window is None)
        if not isinstance(requested_run_until_stopped, bool):
            raise ValueError("INVALID_BOOLEAN_PARAMETER")
        run_until_stopped = requested_run_until_stopped or session_window is None
        if session_window is not None and (
                isinstance(session_window, bool) or not isinstance(session_window, int)
                or session_window < 1):
            raise ValueError("INVALID_WINDOW_SIZE")
        if run_until_stopped:
            session_window = None
            preset_options.update({"auto_next_check_enabled": False,
                                   "auto_next_on_limit_enabled": False,
                                   "auto_next_limits_enabled": False,
                                   "auto_next_win_count": None,
                                   "auto_next_loss_count": None})
        preset_options["window_size"] = session_window
        preset_options["max_observed_rounds"] = session_window
        preset_options["run_until_stopped"] = run_until_stopped
        if mode in {"START_CHECK", "LEGACY_SEPARATE_TRACKER", "ALL_RULES_TOGETHER"}:
            preset_options.setdefault("auto_next_check_enabled", False)
            preset_options.setdefault("auto_next_on_limit_enabled", False)
            if (not isinstance(preset_options["auto_next_check_enabled"], bool)
                    or not isinstance(preset_options["auto_next_on_limit_enabled"], bool)):
                raise ValueError("INVALID_BOOLEAN_PARAMETER")
        else:
            preset_options.pop("auto_next_on_limit_enabled", None)
        for key in (
            "loss_limit_rwf",
            "reconciled_loss_count_limit_enabled", "reconciled_loss_count_limit",
            "reconciled_loss_limit_enabled", "reconciled_loss_limit_rwf",
            "reconciled_profit_target_enabled", "reconciled_profit_target_rwf",
            "reconciled_win_limit_enabled", "reconciled_win_limit_count",
        ):
            preset_options.pop(key, None)
        if mode == "ALL_RULES_TOGETHER":
            self._validate_net_outcome_limits(preset_options)
            self._validate_auto_next_result_limits(preset_options)
            preset_options["net_outcome_limit_version"] = 1
        else:
            preset_options.pop("loss_limit_enabled", None)
            preset_options.pop("loss_limit_count", None)
            preset_options.pop("auto_next_limits_enabled", None)
            preset_options.pop("auto_next_win_count", None)
            preset_options.pop("auto_next_loss_count", None)
        value = {"name": name.strip(), "mode": mode, "rule_ids": list(rule_ids),
                 "options": preset_options, "configuration_version": self.configuration_version,
                 "saved_at": _now()}
        with self._lock:
            self.presets[value["name"]] = value
            self._persist(session_ids=set())
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

    @staticmethod
    def _performance_signal_momentum(session: Mapping[str, Any], rule_id: str,
                                     *, sample_size: int = 3) -> dict[str, Any]:
        """Compare TRUE-signal rates in the latest and preceding three checks."""
        checks_by_round: dict[int, bool] = {}
        for evaluation in session.get("evaluations") or []:
            if not isinstance(evaluation, Mapping):
                continue
            source = evaluation.get("source_round") or {}
            raw_index = source.get("roundIndex", source.get("round_index")) \
                if isinstance(source, Mapping) else None
            try:
                round_index = int(raw_index)
            except (TypeError, ValueError, OverflowError):
                continue
            for check in evaluation.get("rule_evaluations") or []:
                if not isinstance(check, Mapping) or check.get("rule_id") != rule_id:
                    continue
                signal = check.get("eligible", check.get("result"))
                if isinstance(signal, bool):
                    checks_by_round[round_index] = signal
                break
        checks = sorted(checks_by_round.items())
        recent = checks[-sample_size:]
        previous = checks[-2 * sample_size:-sample_size]
        recent_hits = sum(signal for _, signal in recent)
        previous_hits = sum(signal for _, signal in previous)
        momentum_pp = None
        trend = "UNKNOWN"
        if len(recent) == sample_size and len(previous) == sample_size:
            momentum_pp = round((recent_hits - previous_hits) * 100 / sample_size, 1)
            if recent_hits > previous_hits and recent_hits >= 2:
                trend = "RISING"
            elif recent_hits < previous_hits and previous_hits >= 2:
                trend = "FALLING"
            else:
                trend = "STEADY"
        return {"signal_trend": trend, "signal_momentum_pp": momentum_pp,
                "recent_signal_hits": recent_hits, "recent_signal_checks": len(recent),
                "previous_signal_hits": previous_hits, "previous_signal_checks": len(previous),
                "signal_sample_count": len(checks)}

    @staticmethod
    def _performance_outcome_momentum(session: Mapping[str, Any], rule_id: str,
                                      window: Mapping[str, Any], *,
                                      sample_size: int = 3,
                                      minimum_sample: int = 2) -> dict[str, Any]:
        """Compare recent settled outcome rates inside the active 100-round window."""
        try:
            start_index = int(window.get("window_start_round_index") or 0)
            end_index = int(window.get("window_end_round_index") or 0)
        except (TypeError, ValueError, OverflowError):
            start_index = end_index = 0

        selections = list((session.get("rule_selections") or {}).get(rule_id) or [])
        if not selections:
            selections = [item for item in session.get("selections") or []
                          if rule_id in (item.get("matched_rule_ids")
                                         or item.get("contributing_rule_ids") or [])]

        outcomes_by_round: dict[int, int] = {}
        for selection in selections:
            source = selection.get("source_round") or selection.get("sourceRound") or {}
            raw_index = (source.get("roundIndex", source.get("round_index"))
                         if isinstance(source, Mapping) else None)
            try:
                round_index = int(raw_index)
            except (TypeError, ValueError, OverflowError):
                continue
            if not start_index <= round_index <= end_index:
                continue
            status = str(selection.get("status") or selection.get("result") or "").upper()
            if status == "WIN":
                outcomes_by_round[round_index] = 1
            elif status == "LOSS":
                outcomes_by_round[round_index] = -1

        outcomes = [outcomes_by_round[index] for index in sorted(outcomes_by_round)]
        recent = outcomes[-sample_size:]
        previous = outcomes[-2 * sample_size:-sample_size]
        recent_wins = sum(value > 0 for value in recent)
        recent_losses = sum(value < 0 for value in recent)
        previous_wins = sum(value > 0 for value in previous)
        previous_losses = sum(value < 0 for value in previous)
        trend = "INSUFFICIENT"
        momentum_pp = None
        if len(recent) >= minimum_sample and len(previous) >= minimum_sample:
            recent_rate = recent_wins / len(recent)
            previous_rate = previous_wins / len(previous)
            momentum_pp = round((recent_rate - previous_rate) * 100, 1)
            if momentum_pp > 0:
                trend = "RISING"
            elif momentum_pp <= -20:
                trend = "FALLING"
            else:
                trend = "STEADY"
        return {
            "performance_trend": trend,
            "performance_momentum_pp": momentum_pp,
            "recent_outcome_wins": recent_wins,
            "recent_outcome_losses": recent_losses,
            "recent_outcome_count": len(recent),
            "recent_outcome_net": recent_wins - recent_losses,
            "previous_outcome_wins": previous_wins,
            "previous_outcome_losses": previous_losses,
            "previous_outcome_count": len(previous),
            "previous_outcome_net": previous_wins - previous_losses,
            "outcome_sample_count": len(outcomes),
            "outcome_momentum_sample_size": sample_size,
            "outcome_momentum_minimum_sample": minimum_sample,
        }

    @staticmethod
    def _adaptive_context_signature(history: list[Mapping[str, Any]]) -> str:
        return "".join(str(row.get("outcome") or "").upper()
                       for row in history[-3:]
                       if str(row.get("outcome") or "").upper() in {"H", "L"})

    @staticmethod
    def _adaptive_selection_identity(selection: Mapping[str, Any]) -> tuple[str, int, int] | None:
        source = selection.get("source_round") or selection.get("sourceRound") or {}
        target = selection.get("actual_target") or selection.get("expected_target") or {}
        try:
            stream = str(source.get("streamId", source.get("stream_id")) or "")
            source_index = int(source.get("roundIndex", source.get("round_index")) or 0)
            target_index = int(target.get("roundIndex", target.get("round_index")) or 0)
        except (TypeError, ValueError, OverflowError):
            return None
        return (stream, source_index, target_index) if stream and source_index > 0 and target_index > 0 else None

    def _adaptive_rule_evidence(self, session: Mapping[str, Any], rule_id: str,
                                source: Mapping[str, Any], context: str) -> dict[str, Any]:
        stream = str(source.get("stream_id") or "")
        source_index = int(source.get("round_index") or 0)
        reset_index = int(session.get("adaptive_performance_reset_round_index") or 0)
        records: dict[tuple[str, int, int], dict[str, Any]] = {}
        signals: set[int] = set()
        observer = self._performance_observers.get(rule_id) or {}
        observed_engine = observer.get("engine")
        for observed in (getattr(observed_engine, "sessions", {}) or {}).values():
            if (observed.get("last_stream_id") != stream
                    or observed.get("performance_configuration_key") != self._performance_configuration_key(rule_id)):
                continue
            rows = list(observed.get("selections") or [])
            rows.extend(item for values in (observed.get("rule_selections") or {}).values() for item in values)
            for item in rows:
                status = str(item.get("status") or item.get("result") or "").upper()
                identity = self._adaptive_selection_identity(item)
                if identity and identity[0] == stream and status in {"WIN", "LOSS"}:
                    records[identity] = {"identity": identity, "result": status,
                                         "context": item.get("behavior_context")}
            for evaluation in observed.get("evaluations") or []:
                ref = evaluation.get("source_round") or {}
                try:
                    index = int(ref.get("roundIndex", ref.get("round_index")) or 0)
                except (TypeError, ValueError, OverflowError):
                    continue
                if any(check.get("rule_id") == rule_id
                       and check.get("eligible", check.get("result")) is True
                       for check in evaluation.get("rule_evaluations") or []):
                    signals.add(index)
        for item in (session.get("rule_selections") or {}).get(rule_id, []):
            status = str(item.get("status") or item.get("result") or "").upper()
            identity = self._adaptive_selection_identity(item)
            if (identity and identity[0] == stream and identity[1] > reset_index
                    and status in {"WIN", "LOSS"}):
                records.setdefault(identity, {"identity": identity, "result": status,
                                              "context": item.get("behavior_context")})
        for evaluation in session.get("evaluations") or []:
            ref = evaluation.get("source_round") or {}
            try:
                index = int(ref.get("roundIndex", ref.get("round_index")) or 0)
            except (TypeError, ValueError, OverflowError):
                continue
            if index > reset_index and index >= source_index - 49 and any(check.get("rule_id") == rule_id
                and check.get("eligible", check.get("result")) is True
                for check in evaluation.get("rule_evaluations") or []):
                signals.add(index)

        ordered = sorted(records.values(), key=lambda item: item["identity"][1])
        cutoff_50 = source_index - 49
        rolling = [item for item in ordered if item["identity"][1] >= cutoff_50]
        recent = [item for item in rolling if item["identity"][1] >= source_index - 9]
        medium = [item for item in rolling if item["identity"][1] < source_index - 9]
        older = [item for item in ordered if item["identity"][1] < cutoff_50]
        archived = []
        for row in observer.get("history") or []:
            try:
                window_end = int(row.get("window_end_round_index") or row.get("last_round_index") or 0)
            except (TypeError, ValueError, OverflowError):
                continue
            if (row.get("stream_id") == stream
                    and window_end > reset_index
                    and row.get("performance_configuration_key") == self._performance_configuration_key(rule_id)
                    and window_end < cutoff_50):
                archived.append(row)
        old_wins = sum(item["result"] == "WIN" for item in older) + sum(int(row.get("wins") or 0) for row in archived)
        old_losses = sum(item["result"] == "LOSS" for item in older) + sum(int(row.get("losses") or 0) for row in archived)

        def counts(items: list[dict[str, Any]]) -> tuple[int, int]:
            return sum(item["result"] == "WIN" for item in items), sum(item["result"] == "LOSS" for item in items)

        wins, losses = counts(rolling)
        recent_wins, recent_losses = counts(recent)
        medium_wins, medium_losses = counts(medium)
        total_history_wins = wins + old_wins
        total_history_losses = losses + old_losses
        all_history_sample = total_history_wins + total_history_losses
        baseline = total_history_wins / all_history_sample if all_history_sample else 0.5
        total = wins + losses
        posterior = (wins + 6 * baseline) / (total + 6) if total else baseline
        recent_n, medium_n = recent_wins + recent_losses, medium_wins + medium_losses
        recent_rate = recent_wins / recent_n if recent_n else baseline
        medium_rate = medium_wins / medium_n if medium_n else baseline
        long_n = old_wins + old_losses
        long_rate = old_wins / long_n if long_n else baseline
        context_rows = [item for item in rolling if isinstance(item.get("context"), str)
            and len(item["context"]) == len(context)
            and sum(a != b for a, b in zip(item["context"], context)) <= 1]
        context_wins, context_losses = counts(context_rows)
        context_n = context_wins + context_losses
        context_rate = (context_wins + 4 * baseline) / (context_n + 4) if context_n else baseline
        stability = max(0.0, 1 - min(1.0, (abs(recent_rate - medium_rate)
                                            + abs(medium_rate - long_rate)) / 1.5))
        confidence = total / (total + 12)
        blended = 0.50 * posterior + 0.25 * recent_rate + 0.15 * medium_rate + 0.10 * long_rate
        score = (baseline + confidence * (blended - baseline)
                 + min(0.25, context_n / (context_n + 12) * 0.25) * (context_rate - baseline)
                 + max(-0.08, min(0.08, recent_rate - medium_rate)) * 0.15
                 - (1 - stability) * 0.12)
        return {
            "rolling_50": {"wins": wins, "losses": losses, "settled": total,
                           "net": wins - losses, "window_start_round_index": cutoff_50,
                           "window_end_round_index": source_index,
                           "win_rate": round(wins / total, 4) if total else None},
            "recent": {"wins": recent_wins, "losses": recent_losses, "settled": recent_n,
                       "win_rate": round(recent_rate, 4) if recent_n else None},
            "medium": {"wins": medium_wins, "losses": medium_losses, "settled": medium_n,
                       "win_rate": round(medium_rate, 4) if medium_n else None},
            "long_term": {"wins": old_wins, "losses": old_losses, "settled": long_n,
                          "win_rate": round(long_rate, 4) if long_n else None,
                          "archived_windows": len(archived)},
            "all_history": {"wins": wins + old_wins, "losses": losses + old_losses,
                            "settled": total + long_n, "net": wins - losses + old_wins - old_losses},
            "baseline_win_rate": round(baseline, 4), "baseline_sample_count": all_history_sample,
            "posterior_win_rate": round(posterior, 4),
            "context_evidence": {"context": context, "similar_samples": context_n,
                                 "wins": context_wins, "losses": context_losses,
                                 "adjusted_win_rate": round(context_rate, 4)},
            "confidence": round(confidence, 4), "stability": round(stability, 4),
            "momentum": round(recent_rate - medium_rate, 4),
            "score": round(max(0.0, min(1.0, score)), 4),
            "signal_rounds_50": len({index for index in signals if index >= cutoff_50}),
            "signal_rate_50": round(len({index for index in signals if index >= cutoff_50}) / 50, 4),
            "performance_round_index": (observer.get("last_event") or {}).get("round_index"),
            "performance_feed_fresh": bool(
                (observer.get("last_event") or {}).get("stream_id") == stream
                and (observer.get("last_event") or {}).get("continuity_status") == "VERIFIED"
                and 0 <= source_index - int((observer.get("last_event") or {}).get("round_index") or 0) <= 5),
            "signal_round_set": sorted(index for index in signals if index >= cutoff_50),
        }

    def _adaptive_baseline_comparison(self, session: Mapping[str, Any]) -> dict[str, Any]:
        def target_key(item: Mapping[str, Any]) -> tuple[str, int] | None:
            target = item.get("actual_target") or item.get("expected_target") or {}
            try:
                stream = str(target.get("streamId", target.get("stream_id")) or "")
                index = int(target.get("roundIndex", target.get("round_index")) or 0)
            except (TypeError, ValueError, OverflowError):
                return None
            return (stream, index) if stream and index > 0 else None

        portfolio: dict[tuple[str, int], str] = {}
        fixed: dict[str, dict[tuple[str, int], str]] = {}
        for rule_id, rows in (session.get("rule_selections") or {}).items():
            fixed[rule_id] = {}
            for item in rows or []:
                result = str(item.get("status") or item.get("result") or "").upper()
                key = target_key(item)
                if key and result in {"WIN", "LOSS"}:
                    fixed[rule_id][key] = result
                    portfolio.setdefault(key, result)
        # The per-rule observer runs every registered policy independently,
        # even when Adaptive did not select it. Use those forward ledgers for
        # the fixed-rule comparator so it is not biased toward Adaptive's
        # chosen candidates. Compare only on target rounds the portfolio
        # actually covered.
        for rule_id in session.get("rule_ids") or []:
            observer = self._performance_observers.get(rule_id) or {}
            observer_engine = observer.get("engine")
            for observed_session in (getattr(observer_engine, "sessions", {}) or {}).values():
                if (observed_session.get("mode") != session.get("mode")
                        or observed_session.get("performance_configuration_key")
                           != self._performance_configuration_key(rule_id)):
                    continue
                rows = list(observed_session.get("selections") or [])
                rows.extend(item for values in (observed_session.get("rule_selections") or {}).values()
                            for item in values or [])
                for item in rows:
                    result = str(item.get("status") or item.get("result") or "").upper()
                    key = target_key(item)
                    if key and result in {"WIN", "LOSS"}:
                        fixed.setdefault(rule_id, {}).setdefault(key, result)
        automatic: dict[tuple[str, int], str] = {}
        for item in session.get("adaptive_automatic_baseline_selections") or []:
            result = str(item.get("status") or item.get("result") or "").upper()
            key = target_key(item)
            if key and result in {"WIN", "LOSS"}:
                automatic[key] = result
        for other in self.sessions.values():
            if (other.get("session_id") == session.get("session_id")
                    or other.get("adaptive_rule_selection") is True
                    or other.get("automatic_rule_selection") is not True
                    or other.get("mode") != session.get("mode")):
                continue
            for item in other.get("selections") or []:
                result = str(item.get("status") or item.get("result") or "").upper()
                key = target_key(item)
                if key and result in {"WIN", "LOSS"}:
                    automatic[key] = result

        def summary(reference: Mapping[tuple[str, int], str], *, scope: Mapping[tuple[str, int], str] | None = None) -> dict[str, Any]:
            keys = set(reference) if scope is None else set(reference) & set(scope)
            outcomes = [reference[key] for key in keys]
            wins, losses = outcomes.count("WIN"), outcomes.count("LOSS")
            return {"status": "AVAILABLE" if outcomes else "NO_OVERLAPPING_TARGETS",
                    "wins": wins, "losses": losses, "net": wins - losses,
                    "settled_targets": len(outcomes),
                    "win_rate": round(wins / len(outcomes), 4) if outcomes else None}

        fixed_rows = [(rule_id, summary(outcomes, scope=portfolio))
                      for rule_id, outcomes in fixed.items()]
        fixed_rows = [item for item in fixed_rows if item[1]["settled_targets"]]
        best_fixed = max(fixed_rows, key=lambda item: (
            item[1]["net"], item[1]["settled_targets"], item[0]), default=None)
        return {"comparison_scope": "MATCHING_VERIFIED_TARGET_ROUNDS",
            "adaptive_portfolio": summary(portfolio),
            "fixed_rule": ({"rule_id": best_fixed[0], **best_fixed[1]} if best_fixed else
                           {"status": "COLLECTING_FORWARD_OUTCOMES", "settled_targets": 0}),
            "automatic": summary(automatic, scope=portfolio)}

    def _adaptive_rule_choice(self, session: dict[str, Any], source: Mapping[str, Any],
                              history: list[Mapping[str, Any]], at: str) -> dict[str, Any]:
        self._ensure_automatic_rule_candidates(session)
        context = self._adaptive_context_signature(history)
        previous = session.get("adaptive_rule_choice") or {}
        incumbents = set(previous.get("rule_ids") or [])
        candidates: list[dict[str, Any]] = []
        for rule_id in session.get("rule_ids") or []:
            state = session.get("engines", {}).get(rule_id) or {}
            cycle = state.get("cycle") or {}
            row: dict[str, Any] = {"rule_id": rule_id, "available": True,
                "selected": False, "validated": False, "evidence_status": "EXPLORATORY",
                "reason": "EXPLORATORY_INSUFFICIENT_SETTLED_EVIDENCE",
                "loss_limit": RULE_LOSS_LIMIT,
                "loss_limit_losses": int(state.get("loss_limit_losses") or 0),
                "loss_limit_blocked": bool(state.get("loss_limit_blocked")),
                "cycle_status": cycle.get("status") or "IDLE"}
            blocked = bool(state.get("loss_limit_blocked") and state.get("entry_signal_latched"))
            waiting = bool(rule_id not in {START_CHECK_POLICY_ID, LEGACY_POLICY_ID, COMBINED_POLICY_ID}
                           and state.get("entry_signal_latched") and not cycle.get("active"))
            if blocked or waiting:
                row.update({"available": False, "reason": "RULE_WAITING_FOR_NEW_ENTRY"})
            elif session.get("rule_configuration_keys", {}).get(rule_id) != self._performance_configuration_key(rule_id):
                row.update({"available": False, "reason": "SESSION_RULE_CONFIGURATION_CHANGED"})
            else:
                evidence = self._adaptive_rule_evidence(session, rule_id, source, context)
                row.update({key: value for key, value in evidence.items() if key != "signal_round_set"})
                row["signal_round_set"] = evidence["signal_round_set"]
                sample = int(evidence["rolling_50"]["settled"])
                baseline = float(evidence["baseline_win_rate"])
                row["validated"] = bool(evidence.get("performance_feed_fresh") is True
                    and sample >= 8 and evidence["score"] >= baseline + 0.035
                    and evidence["confidence"] >= 0.35 and evidence["stability"] >= 0.45)
                row["evidence_status"] = ("VALIDATED" if row["validated"] else
                    "DEVELOPING" if sample >= 3 else "EXPLORATORY")
                row["reason"] = "BASELINE_ADJUSTED_STABLE_EDGE" if row["validated"] else (
                    "DEVELOPING_EVIDENCE" if sample else "EXPLORATORY_INSUFFICIENT_SETTLED_EVIDENCE")
            candidates.append(row)

        available = [row for row in candidates if row["available"]]
        baseline = sum(float(row.get("baseline_win_rate") or 0.5) for row in available) / len(available) if available else 0.5
        ranked = sorted(available, key=lambda row: (
            -(float(row.get("score") or 0.5) + (0.015 if row["rule_id"] in incumbents else 0)), row["rule_id"]))
        selected: list[dict[str, Any]] = []
        for row in ranked:
            incumbent = row["rule_id"] in incumbents
            score = float(row.get("score") or 0.5)
            if not (row["validated"] and score >= baseline + 0.02) and not (incumbent and score >= baseline - 0.06):
                if incumbent:
                    row["reason"] = "REMOVED_AFTER_EDGE_DECAY_OR_UNSTABLE_EVIDENCE"
                continue
            signals = set(row.get("signal_round_set") or [])
            duplicate = next((prior for prior in selected if signals and prior.get("signal_round_set")
                and len(signals & set(prior["signal_round_set"])) / len(signals | set(prior["signal_round_set"])) >= 0.8), None)
            if duplicate:
                overlap = len(signals & set(duplicate["signal_round_set"])) / len(signals | set(duplicate["signal_round_set"]))
                row["maximum_signal_overlap"] = round(overlap, 4)
                if not (incumbent and score > float(duplicate.get("score") or 0) + 0.10):
                    row.update({"reason": "EXCLUDED_HIGH_SIGNAL_OVERLAP",
                                "overlaps_rule_id": duplicate["rule_id"]})
                    continue
                selected.remove(duplicate)
                duplicate.update({"selected": False, "reason": "REMOVED_FOR_STRONGER_DIVERSE_CANDIDATE"})
            row.update({"selected": True,
                        "reason": "RETAINED_WITH_HYSTERESIS" if incumbent and not row["validated"]
                                  else "DIVERSE_VALIDATED_EDGE"})
            selected.append(row)
        if not selected and available:
            fallback = max(available, key=lambda row: (float(row.get("score") or 0.5), row["rule_id"]))
            fallback.update({"selected": True, "validated": False,
                             "evidence_status": "EXPLORATORY", "reason": "EXPLORATORY_BEST_AVAILABLE"})
            selected = [fallback]
        selected_ids = [row["rule_id"] for row in selected]
        previous_ids = list(previous.get("rule_ids") or [])
        changed = selected_ids != previous_ids
        if changed:
            session["adaptive_selection_version"] = int(session.get("adaptive_selection_version") or 0) + 1
        decision = {"rule_ids": selected_ids,
            "reason": ("NO_ELIGIBLE_RULES" if not selected_ids else
                       "EXPLORATORY_BEST_AVAILABLE" if not any(row["validated"] for row in selected) else
                       "ADAPTIVE_PORTFOLIO_UPDATED" if changed else "ADAPTIVE_PORTFOLIO_RETAINED"),
            "source_round_index": int(source.get("round_index") or 0), "evaluated_at": at,
            "context": {"high_low_signature": context, "source_outcome": source.get("outcome"),
                        "window_rounds": 50, "baseline_win_rate": round(baseline, 4)},
            "selection_version": int(session.get("adaptive_selection_version") or 0),
            "decision_sequence": len(session.get("adaptive_decisions") or []) + 1,
            "added": [{"rule_id": rid, "reason": next(row["reason"] for row in selected if row["rule_id"] == rid)}
                      for rid in selected_ids if rid not in previous_ids],
            "removed": [{"rule_id": rid, "reason": next((row["reason"] for row in candidates
                        if row["rule_id"] == rid), "NO_LONGER_SELECTED")}
                        for rid in previous_ids if rid not in selected_ids],
            "candidates": [{key: value for key, value in row.items() if key != "signal_round_set"}
                           for row in candidates]}
        session["adaptive_rule_choice"] = decision
        session["active_rule_ids"] = selected_ids
        session.setdefault("adaptive_decisions", []).append(deepcopy(decision))
        session["adaptive_baseline_comparison"] = self._adaptive_baseline_comparison(session)
        return decision

    def _adaptive_automatic_baseline_proposals(self, session: dict[str, Any],
            source: Mapping[str, Any], history: list[Mapping[str, Any]], at: str
    ) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
        engines = session.setdefault("adaptive_automatic_baseline_engines", {})
        for rule_id in session.get("rule_ids") or []:
            engines.setdefault(rule_id, _fresh_engine(rule_id))
        baseline_session = {
            "session_id": f"{session['session_id']}:automatic-baseline",
            "mode": session["mode"], "rule_ids": list(session.get("rule_ids") or []),
            "rule_versions": deepcopy(session.get("rule_versions") or {}),
            "rule_configuration_keys": deepcopy(session.get("rule_configuration_keys") or {}),
            "configuration_snapshot": deepcopy(session.get("configuration_snapshot") or {}),
            "engines": engines, "manifest": deepcopy(session.get("manifest") or []),
        }
        # Run the unchanged Automatic chooser against a private configuration
        # copy, so its normal re-enable behavior cannot mutate live rule config.
        baseline_engine = RulesEngine(None, performance_observer=False)
        baseline_engine.rule_configuration = deepcopy(self.rule_configuration)
        baseline_engine.configuration_version = self.configuration_version
        baseline_engine._performance_observers = self._performance_observers
        baseline_engine._performance_observer_error = self._performance_observer_error
        choice = baseline_engine._automatic_rule_choice(baseline_session, source)
        baseline_proposals = []
        for rule_id in choice.get("rule_ids") or []:
            proposal = self._proposal(baseline_session, rule_id, source, history, at)
            proposal["enabled"] = True
            proposal["selected_by_automatic_baseline"] = True
            if proposal.get("eligible") is True:
                baseline_proposals.append(proposal)
        session["adaptive_automatic_baseline_choice"] = deepcopy(choice)
        session.setdefault("adaptive_automatic_baseline_decisions", []).append({
            "source_round_index": int(source.get("round_index") or 0),
            "evaluated_at": at, "choice": deepcopy(choice),
            "selected_rule_ids": [], "action": "WAIT",
        })
        session["adaptive_automatic_baseline_engines"] = engines
        return baseline_session, choice, baseline_proposals

    def _record_adaptive_automatic_baseline(self, session: dict[str, Any],
            baseline_session: dict[str, Any], choice: Mapping[str, Any],
            proposals: list[dict[str, Any]], source: Mapping[str, Any], at: str) -> None:
        true_rows = [row for row in proposals if row.get("eligible") is True]
        decision = (session.get("adaptive_automatic_baseline_decisions") or [])[-1:]
        if not true_rows:
            if decision:
                decision[0].update({"selected_rule_ids": [], "action": "WAIT"})
            return
        rule_ids = [row["rule_id"] for row in true_rows]
        base_id = _stable_selection_id(session, source, rule_ids)
        selection_id = str(uuid5(NAMESPACE_URL, f"adaptive-automatic-baseline:{base_id}"))
        selection = {"selection_id": selection_id, "decision_id": selection_id,
            "session_id": session["session_id"], "source_round": _reference(source),
            "expected_target": _expected(source), "contributing_rule_ids": rule_ids,
            "matched_rule_ids": rule_ids, "selected_at": at, "status": "PENDING",
            "result": "PENDING", "baseline_selection": True}
        for proposal in true_rows:
            self._commit_contributor(baseline_session, proposal, selection)
        session.setdefault("adaptive_automatic_baseline_selections", []).append(selection)
        session["adaptive_automatic_baseline_engines"] = baseline_session["engines"]
        if decision:
            decision[0].update({"selection_id": selection_id,
                "selected_rule_ids": rule_ids, "action": "SELECT"})

    def _automatic_rule_choice(self, session: dict[str, Any], source: Mapping[str, Any]) -> dict[str, Any]:
        """Choose qualifying policies from settled outcomes available before this source decision."""
        result: dict[str, Any] = {"rule_ids": [], "reason": "NO_QUALIFIED_RULE",
                                  "source_round_index": source["round_index"], "candidates": []}
        if self._performance_observer_error:
            result["reason"] = "PERFORMANCE_OBSERVER_UNAVAILABLE"
            return result
        self._ensure_automatic_rule_candidates(session)
        for rule_id in session["rule_ids"]:
            candidate: dict[str, Any] = {"rule_id": rule_id, "qualified": False}
            result["candidates"].append(candidate)
            rule_state = session.get("engines", {}).get(rule_id) or {}
            cycle_state = rule_state.get("cycle") or {}
            candidate.update({"loss_limit_losses": int(rule_state.get("loss_limit_losses") or 0),
                              "loss_limit": RULE_LOSS_LIMIT,
                              "loss_limit_blocked": bool(rule_state.get("loss_limit_blocked")),
                              "loss_limit_reached_round_index": rule_state.get("loss_limit_reached_round_index"),
                              "entry_signal_latched": bool(rule_state.get("entry_signal_latched")),
                              "rule_state": rule_state.get("rule_state"),
                              "cycle_status": cycle_state.get("status") or "IDLE",
                              "cycle_active": bool(cycle_state.get("active")),
                              "cycle_requested_targets": int(cycle_state.get("requested_count") or 0),
                              "cycle_resolved_targets": int(cycle_state.get("resolved_count") or 0),
                              "cycle_loss_count": int(cycle_state.get("loss_count") or 0),
                              "cycle_maximum_targets": cycle_state.get("maximum_targets"),
                              "cycle_end_condition": cycle_state.get("end_condition"),
                              "cycle_end_reason": cycle_state.get("end_reason")})
            if rule_state.get("loss_limit_blocked"):
                if rule_id in {START_CHECK_POLICY_ID, LEGACY_POLICY_ID, COMBINED_POLICY_ID}:
                    fresh_entry = (rule_state.get("rule_state") == "WAIT_HIGH"
                                   and source.get("outcome") == "H")
                else:
                    fresh_entry = not rule_state.get("entry_signal_latched")
                if not fresh_entry:
                    candidate["reason"] = "RULE_LOSS_LIMIT_WAITING_FOR_NEW_ENTRY"
                    candidate["loss_limit_losses"] = int(rule_state.get("loss_limit_losses") or 0)
                    continue
            elif (rule_id not in {START_CHECK_POLICY_ID, LEGACY_POLICY_ID, COMBINED_POLICY_ID}
                  and rule_state.get("entry_signal_latched")
                  and not ((rule_state.get("cycle") or {}).get("active"))):
                candidate["reason"] = "RULE_WAITING_FOR_NEW_ENTRY"
                continue
            current_key = self._performance_configuration_key(rule_id)
            if session.get("rule_configuration_keys", {}).get(rule_id) != current_key:
                candidate["reason"] = "SESSION_RULE_CONFIGURATION_CHANGED"
                continue
            observer = self._performance_observers.get(rule_id)
            latest = (observer or {}).get("last_event") or {}
            lag = source["round_index"] - int(latest.get("round_index") or 0)
            if (latest.get("stream_id") != source["stream_id"]
                    or latest.get("continuity_status") != "VERIFIED" or not 0 <= lag <= 5
                    or (lag == 0 and latest.get("round_id") != source["round_id"])):
                candidate["reason"] = "PERFORMANCE_FEED_LAGGING"
                continue
            candidate["performance_round_index"] = latest["round_index"]
            active_windows = [row for row in observer["engine"].sessions.values()
                              if row.get("last_stream_id") == source["stream_id"]
                              and row.get("performance_configuration_key") == current_key
                              and int(row.get("last_round_index") or 0)
                                  >= int(observer.get("segment_start_round_index") or 0)]
            if not active_windows:
                candidate["reason"] = "NO_RECENT_SETTLED_RESULTS"
                continue
            # Decide from the active 100-round policy window only. Archived
            # windows can explain history, but old positive results must not
            # keep a rule selected after its current window turns weak.
            window_session = max(active_windows, key=lambda row: int(row.get("last_round_index") or 0))
            window = self._performance_window(window_session, rule_id)
            window_round = int(window.get("last_round_index") or 0)
            if (window.get("stream_id") != source["stream_id"] or window_round < 1
                    or source["round_index"] - window_round > RULE_PERFORMANCE_WINDOW_SIZE):
                candidate["reason"] = "NO_RECENT_SETTLED_RESULTS"
                continue
            settled = int(window.get("settled") or 0)
            wins = int(window.get("wins") or 0)
            losses = int(window.get("losses") or 0)
            net = int(window.get("net") or 0)
            candidate.update({"settled": settled, "wins": wins, "losses": losses,
                              "net_wins": net, "windows": 1,
                              "window_rounds": int(window.get("observed_rounds") or 0),
                              "window_size": int(window.get("window_size") or RULE_PERFORMANCE_WINDOW_SIZE),
                              "window_mode": "ROLLING",
                              "window_start_round_index": window.get("window_start_round_index"),
                              "window_end_round_index": window.get("window_end_round_index"),
                              "recovered_rounds": int(window.get("recovered_rounds") or 0),
                              "window_last_round_index": window_round})
            outcome_momentum = self._performance_outcome_momentum(
                window_session, rule_id, window)
            signal_momentum = self._performance_signal_momentum(window_session, rule_id)
            candidate.update(outcome_momentum)
            candidate.update(signal_momentum)
            # Recent outcomes can qualify a recovering rule before its full
            # rolling net turns positive. A material outcome decline or a
            # falling TRUE-signal rate removes confidence despite old wins.
            if outcome_momentum["performance_trend"] == "FALLING":
                candidate["reason"] = "PERFORMANCE_MOMENTUM_FALLING"
                continue
            if outcome_momentum["performance_trend"] == "RISING":
                candidate["qualified"] = True
                candidate["reason"] = "PERFORMANCE_MOMENTUM_RISING"
                recent_rate = (outcome_momentum["recent_outcome_wins"]
                               / max(1, outcome_momentum["recent_outcome_count"]))
                candidate["score"] = round(
                    0.5 + recent_rate * 0.25
                    + max(0.0, outcome_momentum["performance_momentum_pp"] or 0) / 400,
                    4)
                candidate["automatically_reenabled"] = self._automatically_enable_qualified_rule(rule_id)
                continue
            if signal_momentum["signal_trend"] == "FALLING":
                candidate["reason"] = "SIGNAL_MOMENTUM_FALLING"
                continue
            if signal_momentum["signal_trend"] == "RISING":
                candidate["qualified"] = True
                candidate["reason"] = "SIGNAL_MOMENTUM_RISING"
                candidate["score"] = round(
                    signal_momentum["recent_signal_hits"] / max(1, signal_momentum["recent_signal_checks"])
                    + max(0, signal_momentum["signal_momentum_pp"]) / 100, 4)
                candidate["automatically_reenabled"] = self._automatically_enable_qualified_rule(rule_id)
                continue
            if settled < 3:
                candidate["reason"] = "INSUFFICIENT_POSITIVE_EVIDENCE"
                continue
            if net <= 0:
                candidate["reason"] = "NO_POSITIVE_NET"
                continue
            candidate["qualified"] = True
            candidate["reason"] = "POSITIVE_SETTLED_EDGE"
            candidate["score"] = round(net / settled, 4)
            candidate["automatically_reenabled"] = self._automatically_enable_qualified_rule(rule_id)
        qualified = [row for row in result["candidates"] if row["qualified"]]
        qualified.sort(key=lambda row: (-row["score"], -row["net_wins"], row["rule_id"]))
        # Keep every independently qualified rule active. The shared
        # coordinator still creates at most one target-bound selection when
        # several chosen rules match the same source round.
        if qualified:
            result["rule_ids"] = [row["rule_id"] for row in qualified]
            result["reason"] = ("MULTIPLE_RULES_QUALIFIED" if len(qualified) > 1
                                else qualified[0]["reason"])
        elif (any(row.get("reason") == "PERFORMANCE_FEED_LAGGING" for row in result["candidates"])
              and all(row.get("reason") in {"PERFORMANCE_FEED_LAGGING", "RULE_DISABLED"}
                      for row in result["candidates"])):
            result["reason"] = "PERFORMANCE_FEED_LAGGING"
        return result

    def preview_rule_selection(self, *, mode: str, selection_mode: str,
                               source: Mapping[str, Any] | None,
                               history: list[Mapping[str, Any]] | None = None) -> dict[str, Any]:
        """Preview the live chooser without creating or mutating a SHADOW session."""
        if selection_mode not in {"automatic", "adaptive"}:
            raise ValueError("INVALID_RULE_SELECTION_PREVIEW_MODE")
        if mode not in MODES:
            raise ValueError("INVALID_SESSION_CONFIGURATION")
        allowed = self._allowed_rules(mode)
        rule_ids = [rule_id for rule_id in self._performance_rule_ids() if rule_id in allowed]
        if source is None:
            return {"status": "WAITING_FOR_VERIFIED_ROUND", "selection_mode": selection_mode,
                    "mode": mode, "source_round": None, "selected_rule_ids": [],
                    "reason": "WAITING_FOR_VERIFIED_ROUND",
                    "candidates": [{"rule_id": rule_id, "selected": False,
                        "reason": "WAITING_FOR_VERIFIED_ROUND"} for rule_id in rule_ids]}

        normalized_source = _identity(source, require_sequence=False)
        if (not normalized_source or source.get("event_type") != "round.persisted"
                or source.get("durability") != "COMMITTED"
                or source.get("continuity_status") != "VERIFIED"):
            return {"status": "WAITING_FOR_VERIFIED_ROUND", "selection_mode": selection_mode,
                    "mode": mode, "source_round": None, "selected_rule_ids": [],
                    "reason": "WAITING_FOR_VERIFIED_ROUND",
                    "candidates": [{"rule_id": rule_id, "selected": False,
                        "reason": "WAITING_FOR_VERIFIED_ROUND"} for rule_id in rule_ids]}

        normalized_history = []
        for item in list(history or []) + [source]:
            row = _identity(item, require_sequence=False)
            if (row and row["stream_id"] == normalized_source["stream_id"]
                    and row["continuity_status"] == "VERIFIED"
                    and row["durability"] == "COMMITTED"):
                if not normalized_history or row["round_index"] > normalized_history[-1]["round_index"]:
                    normalized_history.append(row)
        normalized_history = normalized_history[-5:]
        if not normalized_history or normalized_history[-1]["round_id"] != normalized_source["round_id"]:
            normalized_history.append(normalized_source)

        with self._lock:
            preview_session = {
                "session_id": "selection-preview", "mode": mode, "rule_ids": rule_ids,
                "rule_versions": {rule_id: self._version(rule_id) for rule_id in rule_ids},
                "rule_configuration_keys": {rule_id: self._performance_configuration_key(rule_id)
                    for rule_id in rule_ids},
                "engines": {rule_id: _fresh_engine(rule_id) for rule_id in rule_ids},
                "rule_selections": {rule_id: [] for rule_id in rule_ids},
                "selections": [], "evaluations": [], "adaptive_rule_choice": None,
                "adaptive_decisions": [], "adaptive_selection_version": 0,
                "active_rule_ids": [],
            }
            preview_engine = self
            if selection_mode == "automatic":
                # Automatic can mark a disabled policy for re-enabling after it
                # qualifies. Run that logic against a private config copy here.
                preview_engine = RulesEngine(None, performance_observer=False)
                preview_engine.rule_configuration = deepcopy(self.rule_configuration)
                preview_engine.configuration_version = self.configuration_version
                preview_engine._performance_observers = self._performance_observers
                preview_engine._performance_observer_error = self._performance_observer_error
                choice = preview_engine._automatic_rule_choice(preview_session, normalized_source)
            else:
                choice = self._adaptive_rule_choice(
                    preview_session, normalized_source, normalized_history, _now())
            candidates = [{key: value for key, value in row.items() if key != "signal_round_set"}
                          for row in choice.get("candidates") or []]
            return {"status": "READY", "selection_mode": selection_mode, "mode": mode,
                    "source_round": {"round_id": normalized_source["round_id"],
                        "round_index": normalized_source["round_index"],
                        "outcome": normalized_source["outcome"]},
                    "evaluated_at": _now(), "selected_rule_ids": list(choice.get("rule_ids") or []),
                    "reason": choice.get("reason"), "selection_version": choice.get("selection_version"),
                    "candidates": candidates}

    @staticmethod
    def _validate_net_outcome_limits(options: dict[str, Any]) -> None:
        for enabled_key, count_key in (
            ("win_limit_enabled", "win_limit_count"),
            ("loss_limit_enabled", "loss_limit_count"),
        ):
            options.setdefault(enabled_key, False)
            if not isinstance(options[enabled_key], bool):
                raise ValueError("INVALID_BOOLEAN_PARAMETER")
            if options[enabled_key]:
                count = options.get(count_key)
                if isinstance(count, bool) or not isinstance(count, int) or count < 1:
                    raise ValueError("INVALID_RESULT_COUNT_LIMIT")
            else:
                options[count_key] = None

    @staticmethod
    def _validate_auto_next_result_limits(options: dict[str, Any]) -> None:
        options.setdefault("auto_next_limits_enabled", False)
        if not isinstance(options["auto_next_limits_enabled"], bool):
            raise ValueError("INVALID_BOOLEAN_PARAMETER")
        counts = ("auto_next_win_count", "auto_next_loss_count")
        if options["auto_next_limits_enabled"] and not any(options.get(key) is not None for key in counts):
            raise ValueError("AUTO_NEXT_RESULT_LIMIT_REQUIRED")
        for key in counts:
            count = options.get(key)
            if count is None:
                continue
            if isinstance(count, bool) or not isinstance(count, int) or count < 1:
                raise ValueError("INVALID_RESULT_COUNT_LIMIT")
        if not options["auto_next_limits_enabled"]:
            for key in counts:
                options[key] = None

    def start_session(self, *, mode: str, rule_ids: list[str], boundary: Mapping[str, Any],
                      prestart_context: list[Mapping[str, Any]] | None = None,
                      options: Mapping[str, Any] | None = None, now: str | None = None,
                      replace_session_id: str | None = None,
                      reconfigure_session_id: str | None = None) -> dict[str, Any]:
        now = _now(now)
        if replace_session_id is not None and reconfigure_session_id is not None:
            raise ValueError("INVALID_SESSION_REPLACEMENT")
        if mode not in MODES or not isinstance(rule_ids, list) or len(set(rule_ids)) != len(rule_ids):
            raise ValueError("INVALID_SESSION_CONFIGURATION")
        requested_options = dict(options or {})
        requested_session_window = requested_options.get(
            "max_observed_rounds", requested_options.get("window_size", WINDOW_SIZE))
        requested_run_until_stopped = requested_options.get(
            "run_until_stopped", requested_session_window is None)
        if not isinstance(requested_run_until_stopped, bool):
            raise ValueError("INVALID_BOOLEAN_PARAMETER")
        run_until_stopped = requested_run_until_stopped or requested_session_window is None
        if run_until_stopped:
            # The explicit user choice removes only the round cap. Configured
            # result/profit/selection limits remain active and stop the run.
            requested_options["window_size"] = None
            requested_options["max_observed_rounds"] = None
            # Do not create a successor session automatically from this mode.
            requested_options.update({"auto_next_check_enabled": False,
                                      "auto_next_on_limit_enabled": False,
                                      "auto_next_limits_enabled": False,
                                      "auto_next_win_count": None,
                                      "auto_next_loss_count": None})
        automatic_rule_selection = requested_options.get("automatic_rule_selection", False)
        adaptive_rule_selection = requested_options.get("adaptive_rule_selection", False)
        behaviour_detection_enabled = requested_options.get("behaviour_detection_enabled", False)
        if not isinstance(automatic_rule_selection, bool) or not isinstance(adaptive_rule_selection, bool):
            raise ValueError("INVALID_BOOLEAN_PARAMETER")
        if not isinstance(behaviour_detection_enabled, bool):
            raise ValueError("INVALID_BOOLEAN_PARAMETER")
        if automatic_rule_selection and adaptive_rule_selection:
            raise ValueError("RULE_SELECTION_MODES_ARE_MUTUALLY_EXCLUSIVE")
        if automatic_rule_selection:
            if rule_ids:
                raise ValueError("AUTOMATIC_RULE_SELECTION_REQUIRES_NO_FIXED_RULES")
            rule_ids = [rule_id for rule_id in self._performance_rule_ids()
                        if rule_id in self._allowed_rules(mode)]
        if adaptive_rule_selection:
            if rule_ids:
                raise ValueError("ADAPTIVE_RULE_SELECTION_REQUIRES_NO_FIXED_RULES")
            rule_ids = [rule_id for rule_id in self._performance_rule_ids()
                        if rule_id in self._allowed_rules(mode)]
        allowed = self._allowed_rules(mode)
        if any(rule_id not in allowed for rule_id in rule_ids):
            raise ValueError("RULE_NOT_ALLOWED_IN_MODE")
        for rule_id in rule_ids:
            configured = self.rule_configuration.get(rule_id)
            if not (automatic_rule_selection or adaptive_rule_selection) and (not configured or configured.get("configured") is not True or configured.get("enabled") is not True):
                raise ValueError("RULE_NOT_ENABLED")
        if not rule_ids:
            # A zero-rule session is legal and reports NO_ACTION; it never selects.
            pass
        watch_boundary = (boundary.get("event_type") == "round.observed"
                          and boundary.get("durability") == "PENDING"
                          and boundary.get("delivery_mode") == "LIVE"
                          and isinstance(boundary.get("source_metadata"), Mapping)
                          and boundary["source_metadata"].get("source") == "ORBIT_DATA_WATCH_API")
        start = _identity(boundary, require_sequence=not watch_boundary)
        if not start or (not watch_boundary and (boundary.get("event_type") != "round.persisted" or boundary.get("durability") != "COMMITTED")) \
                or boundary.get("continuity_status") != "VERIFIED":
            raise ValueError("VERIFIED_COMMITTED_BOUNDARY_REQUIRED")
        opts = requested_options
        window_size = opts.get("window_size", WINDOW_SIZE)
        if mode in {"START_CHECK", "LEGACY_SEPARATE_TRACKER"}:
            expected_policy = START_CHECK_POLICY_ID if mode == "START_CHECK" else LEGACY_POLICY_ID
            if rule_ids not in ([], [expected_policy]):
                raise ValueError("SINGLE_POLICY_MODE_ACCEPTS_ONLY_ITS_POLICY")
            configured = self.rule_configuration.get(expected_policy, {}).get("parameters", {})
            defaults = {"window_size": WINDOW_SIZE, "stake_rwf": 100, "cashout_multiplier": 2.0,
                        "profit_target_enabled": True, "profit_target_rwf": 400,
                        "auto_next_check_enabled": False, "auto_next_on_limit_enabled": False,
                        "stake_currency": "RWF"}
            defaults.update(configured)
            defaults.update(opts)
            opts = defaults
            opts["fixed_stake_rwf"] = opts.get("fixed_stake_rwf", opts["stake_rwf"])
            if opts["cashout_multiplier"] != 2.0:
                raise ValueError("START_CHECK_CASHOUT_MUST_BE_2_00")
            for key in ("profit_target_enabled", "auto_next_check_enabled",
                        "auto_next_on_limit_enabled"):
                if not isinstance(opts.get(key), bool):
                    raise ValueError("INVALID_BOOLEAN_PARAMETER")
            if opts["profit_target_enabled"] and (isinstance(opts["profit_target_rwf"], bool)
                    or not isinstance(opts["profit_target_rwf"], int) or opts["profit_target_rwf"] <= 0):
                raise ValueError("INVALID_NET_LIMIT")
        elif mode == "ALL_RULES_TOGETHER":
            if "fixed_stake_rwf" not in opts:
                opts["fixed_stake_rwf"] = 100
            opts.setdefault("stake_currency", "RWF")
            if isinstance(opts["fixed_stake_rwf"], bool) or not isinstance(opts["fixed_stake_rwf"], int) or opts["fixed_stake_rwf"] < 1:
                raise ValueError("INVALID_FIXED_STAKE")
            if opts["stake_currency"] != "RWF":
                raise ValueError("FIXED_STAKE_RWF_CURRENCY_MUST_BE_RWF")
            for key in ("win_limit_enabled", "loss_limit_enabled", "auto_next_check_enabled",
                        "auto_next_on_limit_enabled"):
                opts.setdefault(key, False)
                if not isinstance(opts[key], bool):
                    raise ValueError("INVALID_BOOLEAN_PARAMETER")
            max_selections = opts.get("max_selections")
            if max_selections is not None and (isinstance(max_selections, bool)
                    or not isinstance(max_selections, int) or max_selections < 1):
                raise ValueError("INVALID_MAX_SELECTIONS")
            self._validate_net_outcome_limits(opts)
            self._validate_auto_next_result_limits(opts)
            opts["net_outcome_limit_version"] = 1
        else:
            retained = dict(opts)
            opts = {"window_size": retained.get("max_observed_rounds", window_size),
                    "stake_currency": retained.get("stake_currency", "RWF")}
            opts["fixed_stake_rwf"] = retained.get("fixed_stake_rwf", 100)
        if "stake_currency" in opts and opts["stake_currency"] is not None:
            currency = str(opts["stake_currency"]).strip().upper()
            if not re.fullmatch(r"[A-Z]{3}", currency):
                raise ValueError("INVALID_STAKE_CURRENCY")
            opts["stake_currency"] = currency
        if (("fixed_stake_rwf" in opts or "stake_rwf" in opts)
                and opts.get("stake_currency") != "RWF"):
            raise ValueError("RWF_STAKE_FIELD_CURRENCY_MUST_BE_RWF")
        # Retain the fixed frontend stake in each rule mode. Removed loss-limit
        # settings from older preset payloads are deliberately ignored.
        stake = opts.get("fixed_stake_rwf", opts.get("stake_rwf", 100))
        if isinstance(stake, bool) or not isinstance(stake, int) or stake < 1:
            raise ValueError("INVALID_FIXED_STAKE")
        opts["fixed_stake_rwf"] = stake
        opts.pop("stake_rwf", None)
        for key in ("loss_limit_rwf",
                    "reconciled_loss_count_limit_enabled", "reconciled_loss_count_limit",
                    "reconciled_loss_limit_enabled", "reconciled_loss_limit_rwf",
            "reconciled_profit_target_enabled", "reconciled_profit_target_rwf",
            "reconciled_win_limit_enabled", "reconciled_win_limit_count"):
            opts.pop(key, None)
        if mode != "ALL_RULES_TOGETHER":
            opts.pop("loss_limit_enabled", None)
            opts.pop("loss_limit_count", None)
        window_size = opts.get("max_observed_rounds", opts.get("window_size", WINDOW_SIZE))
        if window_size is not None and (isinstance(window_size, bool)
                or not isinstance(window_size, int) or window_size < 1):
            raise ValueError("INVALID_WINDOW_SIZE")
        opts["window_size"] = window_size
        opts["max_observed_rounds"] = window_size
        opts["run_until_stopped"] = window_size is None
        opts["behaviour_detection_enabled"] = behaviour_detection_enabled
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
            "strategy_id": ("BEHAVIOUR_ROLLING_HIGH" if behaviour_detection_enabled else
                            "ADAPTIVE" if adaptive_rule_selection else
                            "AUTOMATIC" if automatic_rule_selection else "RULES"),
            "started_at": now, "ended_at": None, "window_size": window_size,
            "run_until_stopped": window_size is None,
            "configuration_version": snapshot_version, "configuration_snapshot": deepcopy(opts),
            "rule_ids": list(rule_ids), "rule_versions": {rid: self._version(rid) for rid in rule_ids},
            "behaviour_detection_enabled": behaviour_detection_enabled,
            "rule_configuration_keys": ({rid: self._performance_configuration_key(rid)
                                         for rid in rule_ids}
                                        if automatic_rule_selection or adaptive_rule_selection else {}),
            "automatic_rule_selection": automatic_rule_selection,
            "adaptive_rule_selection": adaptive_rule_selection,
            "active_rule_ids": [], "automatic_rule_choice": None,
            "adaptive_rule_choice": None, "adaptive_decisions": [],
            "adaptive_selection_version": 0,
            "adaptive_automatic_baseline_choice": None,
            "adaptive_automatic_baseline_decisions": [],
            "adaptive_automatic_baseline_selections": [],
            "adaptive_automatic_baseline_engines": ({rid: _fresh_engine(rid) for rid in rule_ids}
                if adaptive_rule_selection else {}),
            "manifest": self._manifest(rule_ids),
            "start_boundary": _reference(start) | {"streamSequence": start["stream_sequence"]},
            "synchronization_point": _reference(synchronization_point),
            "last_stream_id": start["stream_id"], "last_stream_sequence": start["stream_sequence"],
            "last_round_index": synchronization_point["round_index"],
            "last_round_id": synchronization_point["round_id"],
            "context": context, "engines": engines, "observed_rounds": 0,
            "prestart_skipped_rounds": 0,
            "selections": [], "rule_selections": {rid: [] for rid in rule_ids},
            "adaptive_signal_selections": [],
            "evaluations": [], "decisions": [], "total_wins": 0, "total_losses": 0,
            "total_cancelled": 0,
            "net_profit_rwf": 0, "selection_stop_reason": None, "stop_reason": None,
            "auto_next_armed": False, "auto_next_trigger": None,
            "limit_stop_latched": False,
            "auto_next_armed_at": None, "auto_next_block_reason": None,
            "auto_next_boundary": None, "auto_next_context": [],
            "auto_next_start_round_id": None,
        }
        with self._lock:
            dirty_session_ids = {session_id}
            if reconfigure_session_id is not None:
                reconfiguring = self.sessions.get(str(reconfigure_session_id))
                if (reconfiguring is None
                        or reconfiguring.get("status") not in {"RUNNING", "SETTLING", "PAUSED"}):
                    raise ValueError("RULES_SESSION_NOT_ACTIVE")
            conflicts = [existing for existing in self.sessions.values()
                         if existing.get("mode") == mode
                         and (reconfigure_session_id is None
                              or existing.get("session_id") != str(reconfigure_session_id))
                         and existing.get("last_stream_id") == start["stream_id"]
                         and existing.get("status") in {"RUNNING", "SETTLING", "PAUSED"}
                         and (existing.get("selection_status") == "ACTIVE"
                              or existing.get("status") == "SETTLING")]
            replaceable = None
            if replace_session_id is not None:
                replaceable = self.sessions.get(str(replace_session_id))
                if (replaceable is None
                        or replaceable.get("mode") != mode
                        or replaceable.get("status") != "PAUSED"
                        or replaceable.get("selection_status") != "ACTIVE"
                        or replaceable.get("stop_reason") not in self.RESTARTABLE_INTEGRITY_STOPS):
                    raise ValueError("SESSION_NOT_RESTARTABLE")
                # Integrity pauses can span collector stream changes, and old
                # integrity-paused sessions can coexist after repeated gaps.
                # They are inert while PAUSED, so only a live run or a manual
                # pause on the current stream should prevent this explicit
                # replacement. Keep all prior outcomes attached to their
                # original session when archiving the selected one.
                unresolved_conflicts = [existing for existing in self.sessions.values()
                    if existing is not replaceable
                    and existing.get("mode") == mode
                    and (existing.get("status") in {"RUNNING", "SETTLING"}
                         or (existing.get("last_stream_id") == start["stream_id"]
                             and existing.get("status") == "PAUSED"
                             and existing.get("selection_status") == "ACTIVE"
                             and existing.get("stop_reason") not in self.RESTARTABLE_INTEGRITY_STOPS))]
                if unresolved_conflicts:
                    raise ValueError("SESSION_ALREADY_ACTIVE_FOR_MODE")
                selections = list(replaceable.get("selections") or [])
                selections.extend(item for rows in (replaceable.get("rule_selections") or {}).values()
                                  for item in rows)
                pending_shadow_outcomes = sum(
                    str(item.get("status") or item.get("result") or "").upper() == "PENDING"
                    for item in selections)
                if self._has_pending_event_records(replaceable):
                    raise ValueError("SESSION_HAS_PENDING_EVENTS")
            elif conflicts:
                raise ValueError("SESSION_ALREADY_ACTIVE_FOR_MODE")
            # A manually started run owns the next boundary.  Do not also
            # launch a previously armed automatic continuation for that mode.
            for existing in self.sessions.values():
                if (existing.get("mode") == mode
                        and (reconfigure_session_id is None
                             or existing.get("session_id") != str(reconfigure_session_id))
                        and existing.get("last_stream_id") == start["stream_id"]
                        and existing.get("auto_next_armed")):
                    existing["auto_next_armed"] = False
                    existing["auto_next_block_reason"] = "MANUAL_SESSION_STARTED"
                    dirty_session_ids.add(str(existing["session_id"]))
            if replaceable is not None:
                prior_reason = str(replaceable.get("stop_reason"))
                replaceable.update({"status": "MANUALLY_STOPPED", "ended_at": now,
                                    "auto_next_armed": False, "auto_next_trigger": None,
                                    "auto_next_armed_at": None,
                                    "integrity_stop_reason": prior_reason})
                self._record_session_stop(
                    replaceable, "REPLACED_AFTER_INTEGRITY_STOP", now,
                    "OPERATOR_CONFIRMED_SHADOW_RESTART", stop_selection=True,
                    details={"new_session_id": session_id,
                             "prior_integrity_reason": prior_reason,
                             "preserved_pending_shadow_outcomes": pending_shadow_outcomes},
                )
                session["replaced_session_id"] = str(replace_session_id)
                session["replacement_reason"] = prior_reason
                dirty_session_ids.add(str(replace_session_id))
            self.sessions[session_id] = session
            self._persist(session_ids=dirty_session_ids)
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

    @staticmethod
    def _selection_targets(selection: Mapping[str, Any], source: Mapping[str, Any]) -> bool:
        target = selection.get("expected_target") or {}
        if not isinstance(target, Mapping):
            return False
        expected_stream = target.get("streamId", target.get("stream_id"))
        expected_index = target.get("roundIndex", target.get("round_index"))
        expected_id = target.get("roundId", target.get("round_id"))
        return (expected_stream == source.get("stream_id")
                and expected_index == source.get("round_index")
                and (expected_id is None or expected_id == source.get("round_id")))

    @staticmethod
    def _settle_selection(selection: dict[str, Any], source: dict[str, Any], at: str,
                          *, stake: float, payout_multiplier: float) -> dict[str, Any]:
        """Apply one verified target result and preserve its evidence and accounting."""
        win = source["outcome"] == "H"
        result = "WIN" if win else "LOSS"
        payout = round(stake * payout_multiplier, 2) if win else 0.0
        profit_loss = round(payout - stake, 2) if win else round(-stake, 2)
        target = _reference(source)
        selection.update({
            "decision_id": selection.get("decision_id") or selection.get("selection_id"),
            "source_round_id": (selection.get("source_round") or {}).get("roundId"),
            "target_round_id": source.get("round_id"),
            "stake_rwf": stake,
            "payout_multiplier": payout_multiplier,
            "status": result,
            "result": result,
            "settlement_status": result,
            "settlement_phase": "SETTLED",
            "actual_multiplier": source["multiplier"],
            "actual_target": target,
            "resolved_at": at,
            "settled_at": at,
            "target_round_completed_at": source.get("observed_at"),
            "target_persistence_status": source.get("durability", "UNKNOWN"),
            "payout_rwf": payout,
            "profit_loss_rwf": profit_loss,
            "settlement_evidence": {
                "event_type": source.get("event_type"),
                "durability": source.get("durability", "UNKNOWN"),
                "continuity_status": source.get("continuity_status", "UNKNOWN"),
                "stream_id": source.get("stream_id"),
                "stream_sequence": source.get("stream_sequence"),
                "target_round_id": source.get("round_id"),
                "target_round_index": source.get("round_index"),
                "target_multiplier": source.get("multiplier"),
                "target_observed_at": source.get("observed_at"),
                "settled_at": at,
            },
        })
        expected_target = selection.get("expected_target")
        if isinstance(expected_target, dict):
            expected_target["roundId"] = source.get("round_id")
        return {"decision_id": selection.get("decision_id"),
                "selection_id": selection.get("selection_id"),
                "source_round_id": selection.get("source_round_id"),
                "target_round_id": source.get("round_id"),
                "result": result, "stake_rwf": stake, "payout_rwf": payout,
                "profit_loss_rwf": profit_loss, "settled_at": at}

    @staticmethod
    def _mark_target_persistence_committed(session: dict[str, Any], source: Mapping[str, Any]) -> bool:
        changed = False
        evidence = {
            "event_type": source.get("event_type"),
            "durability": source.get("durability", "UNKNOWN"),
            "continuity_status": source.get("continuity_status", "UNKNOWN"),
            "stream_id": source.get("stream_id"),
            "stream_sequence": source.get("stream_sequence"),
            "target_round_id": source.get("round_id"),
            "stored_at": source.get("stored_at"),
        }
        rows = list(session.get("selections") or [])
        rows.extend(selection for group in (session.get("rule_selections") or {}).values()
                    for selection in group)
        rows.extend(session.get("adaptive_signal_selections") or [])
        rows.extend(session.get("adaptive_automatic_baseline_selections") or [])
        for selection in rows:
            actual_target = selection.get("actual_target") or {}
            if (selection.get("status") not in {"WIN", "LOSS"}
                    or actual_target.get("streamId") != source.get("stream_id")
                    or actual_target.get("roundId") != source.get("round_id")):
                continue
            if (selection.get("target_persistence_status") == "COMMITTED"
                    and (selection.get("settlement_evidence") or {}).get("persistence_confirmation") == evidence):
                continue
            selection["target_persistence_status"] = "COMMITTED"
            changed = True
            settlement_evidence = selection.setdefault("settlement_evidence", {})
            if isinstance(settlement_evidence, dict):
                settlement_evidence["persistence_confirmation"] = deepcopy(evidence)
        last_settled = session.get("last_settled_target")
        if (isinstance(last_settled, dict)
                and last_settled.get("stream_id") == source.get("stream_id")
                and last_settled.get("round_id") == source.get("round_id")):
            if (last_settled.get("durability") != "COMMITTED"
                    or last_settled.get("persistence_confirmation") != evidence):
                last_settled["durability"] = "COMMITTED"
                last_settled["persistence_confirmation"] = deepcopy(evidence)
                changed = True
        return changed

    def _resolve(self, session: dict[str, Any], source: dict[str, Any], at: str) -> list[dict[str, Any]]:
        target_index = source["round_index"]
        stream_id = source["stream_id"]
        settled: list[dict[str, Any]] = []
        seen_decisions: set[str] = set()
        for combined in session.get("selections", []):
            if combined.get("status") != "PENDING" or not self._selection_targets(combined, source):
                continue
            decision_id = str(combined.get("decision_id") or combined.get("selection_id") or "")
            if decision_id and decision_id in seen_decisions:
                continue
            if decision_id:
                seen_decisions.add(decision_id)
            configuration = session.get("configuration_snapshot") or {}
            raw_stake = combined.get("stake_rwf")
            if raw_stake is None:
                raw_stake = configuration.get("fixed_stake_rwf", 100)
            raw_payout = combined.get("payout_multiplier")
            if raw_payout is None:
                raw_payout = configuration.get("cashout_multiplier")
            if raw_payout is None:
                raw_payout = 2.0
            try:
                stake = float(raw_stake)
                payout_multiplier = float(raw_payout)
            except (TypeError, ValueError, OverflowError):
                combined["settlement_block_reason"] = "STAKE_OR_PAYOUT_CONFIGURATION_INVALID"
                continue
            if (not math.isfinite(stake) or stake <= 0 or not math.isfinite(payout_multiplier)
                    or payout_multiplier != 2.0):
                combined["settlement_block_reason"] = "STAKE_OR_PAYOUT_CONFIGURATION_INVALID"
                continue
            settled_item = self._settle_selection(combined, source, at,
                stake=stake, payout_multiplier=payout_multiplier)
            settled.append(settled_item)
            session["net_profit_rwf"] = round(float(session.get("net_profit_rwf") or 0)
                                                + settled_item["profit_loss_rwf"], 2)
            for rule_id in combined.get("contributing_rule_ids", []):
                state = session.get("engines", {}).get(rule_id)
                if not state:
                    continue
                if state.get("pending_selection_id") == combined.get("selection_id"):
                    self._advance_selected_state(session, rule_id, state, source,
                        settled_item["result"], combined, at)
                    state["pending_selection_id"] = None
                    state["pending_target"] = None
        if settled:
            wins, losses = self._settled_shared_result_counts(session)
            session["total_wins"] = wins
            session["total_losses"] = losses
            config = session["configuration_snapshot"]
            if session["mode"] == "ALL_RULES_TOGETHER":
                # Per-window automatic-next limits use the session's settled
                # net outcome. Their counts are separate from the legacy
                # stop controls, so keep those controls from preempting them.
                if config.get("auto_next_limits_enabled") is not True:
                    self._reconcile_net_outcome_limit_stop(session, wins, losses, at, source)
                if config.get("max_selections") and len(session["selections"]) >= config["max_selections"]:
                    self._stop_selecting(session, "MAX_SELECTIONS_REACHED", at, source,
                                         stop_source="MAX_SELECTIONS")
                if config.get("auto_next_limits_enabled") is True:
                    reached = self._reconcile_custom_window_result_limit_stop(
                        session, wins, losses, at, source)
                    win_limit = config.get("auto_next_win_count")
                    loss_limit = config.get("auto_next_loss_count")
                    net_outcome = wins - losses
                    if session.get("selection_status") == "ACTIVE" and reached:
                        self._stop_selecting(session, "AUTO_NEXT_RESULT_LIMIT_REACHED", at, source,
                            stop_source="AUTO_NEXT_RESULT_LIMIT",
                            stop_details={"reached": reached, "settled_wins": wins,
                                "settled_losses": losses, "win_count_limit": win_limit,
                                "loss_count_limit": loss_limit,
                                "net_outcome": net_outcome})
                self._arm_auto_next_after_limit(
                    session, session.get("selection_stop_reason"), at, source)
            if session["mode"] in {"START_CHECK", "LEGACY_SEPARATE_TRACKER"}:
                limit = session["configuration_snapshot"]
                if limit.get("profit_target_enabled") and session["net_profit_rwf"] >= limit.get("profit_target_rwf", 400):
                    self._stop_selecting(session, "PROFIT_TARGET_REACHED", at, source,
                                         stop_source="PROFIT_TARGET")
                    self._arm_auto_next_after_limit(
                        session, session.get("selection_stop_reason"), at, source)
        for rule_id, selections in session["rule_selections"].items():
            for selection in selections:
                if selection.get("status") != "PENDING" or not self._selection_targets(selection, source):
                    continue
                result = "WIN" if source["outcome"] == "H" else "LOSS"
                selection.update({"status": result, "result": result,
                    "actual_multiplier": source["multiplier"], "actual_target": _reference(source),
                    "resolved_at": at,
                    "target_persistence_status": source.get("durability", "UNKNOWN")})
                if session.get("adaptive_rule_selection") is True:
                    settled.append({"selection_id": selection.get("selection_id"),
                        "rule_id": rule_id,
                        "source_round_id": (selection.get("source_round") or {}).get("roundId"),
                        "target_round_id": source.get("round_id"), "result": result,
                        "settled_at": at, "independent_rule_outcome": True})
                state = session["engines"][rule_id]
                if state.get("pending_selection_id") == selection.get("selection_id"):
                    self._advance_selected_state(session, rule_id, state, source, result, selection, at)
                    state["pending_selection_id"] = None
                    state["pending_target"] = None
        for envelope in session.get("adaptive_signal_selections") or []:
            if envelope.get("status") != "PENDING" or not self._selection_targets(envelope, source):
                continue
            result = "WIN" if source["outcome"] == "H" else "LOSS"
            envelope.update({"status": result, "result": result,
                "actual_multiplier": source["multiplier"], "actual_target": _reference(source),
                "resolved_at": at, "target_persistence_status": source.get("durability", "UNKNOWN")})
        if session.get("adaptive_rule_selection") is True:
            baseline_engines = session.get("adaptive_automatic_baseline_engines") or {}
            for selection in session.get("adaptive_automatic_baseline_selections") or []:
                if selection.get("status") != "PENDING" or not self._selection_targets(selection, source):
                    continue
                result = "WIN" if source["outcome"] == "H" else "LOSS"
                selection.update({"status": result, "result": result,
                    "actual_multiplier": source["multiplier"], "actual_target": _reference(source),
                    "resolved_at": at, "target_persistence_status": source.get("durability", "UNKNOWN")})
                for rule_id in selection.get("contributing_rule_ids") or []:
                    state = baseline_engines.get(rule_id)
                    if state and state.get("pending_selection_id") == selection.get("selection_id"):
                        self._advance_selected_state(session, rule_id, state, source, result, selection, at)
                        state["pending_selection_id"] = None
                        state["pending_target"] = None
                settled.append({"selection_id": selection.get("selection_id"),
                    "target_round_id": source.get("round_id"), "result": result,
                    "settled_at": at, "automatic_baseline": True})
            wins, losses, _pending, unique_targets = self._adaptive_session_result_counts(session)
            session["total_wins"] = wins
            session["total_losses"] = losses
            stake = float((session.get("configuration_snapshot") or {}).get("fixed_stake_rwf") or 100)
            session["net_profit_rwf"] = round((wins - losses) * stake, 2)
            configuration = session.get("configuration_snapshot") or {}
            if session.get("mode") == "ALL_RULES_TOGETHER":
                if configuration.get("auto_next_limits_enabled") is not True:
                    self._reconcile_net_outcome_limit_stop(session, wins, losses, at, source)
                maximum = configuration.get("max_selections")
                if isinstance(maximum, int) and not isinstance(maximum, bool) and unique_targets >= maximum:
                    self._stop_selecting(session, "MAX_SELECTIONS_REACHED", at, source,
                                         stop_source="MAX_SELECTIONS")
                if configuration.get("auto_next_limits_enabled") is True:
                    self._reconcile_custom_window_result_limit_stop(session, wins, losses, at, source)
                self._arm_auto_next_after_limit(session, session.get("selection_stop_reason"), at, source)
            if (session.get("mode") in {"START_CHECK", "LEGACY_SEPARATE_TRACKER"}
                    and configuration.get("profit_target_enabled") is True
                    and session["net_profit_rwf"] >= configuration.get("profit_target_rwf", 400)):
                self._stop_selecting(session, "PROFIT_TARGET_REACHED", at, source,
                                     stop_source="PROFIT_TARGET")
                self._arm_auto_next_after_limit(session, session.get("selection_stop_reason"), at, source)
        if (session.get("limit_stop_latched") is True
                and session.get("status") == "SETTLING"
                and self._pending_selection_count(session) == 0):
            session["status"] = "COMPLETED"
            session["ended_at"] = at
        return settled

    def _advance_selected_state(self, session: dict[str, Any], rule_id: str, state: dict[str, Any],
                                source: dict[str, Any], result: str, selection: dict[str, Any], at: str) -> None:
        self._advance_selected_state_transition(session, rule_id, state, source, result, selection, at)
        if rule_id in {START_CHECK_POLICY_ID, LEGACY_POLICY_ID, COMBINED_POLICY_ID}:
            cycle = state.get("cycle")
            if cycle and cycle.get("active") and state.get("rule_state") != "PLAYING":
                cycle.update({"active": False, "status": "COMPLETED",
                              "ended_at": at, "end_round_index": source.get("round_index"),
                              "end_reason": "POLICY_STOP_TRANSITION"})
                state["cycle_mode"] = "IDLE"

    def _advance_selected_state_transition(self, session: dict[str, Any], rule_id: str,
                                           state: dict[str, Any], source: dict[str, Any],
                                           result: str, selection: dict[str, Any], at: str) -> None:
        state["selected_wins"] = int(state.get("selected_wins", 0)) + (result == "WIN")
        state["selected_losses"] = int(state.get("selected_losses", 0)) + (result == "LOSS")
        outcome = "H" if result == "WIN" else "L"
        if result == "LOSS":
            state["loss_limit_losses"] = int(state.get("loss_limit_losses") or 0) + 1
        cycle = state.get("cycle")
        cycle_id = (selection.get("rule_cycle_ids") or {}).get(rule_id) or selection.get("cycle_id")
        if cycle and cycle.get("active") and cycle.get("cycle_id") == cycle_id:
            cycle["resolved_count"] = int(cycle.get("resolved_count") or 0) + 1
            cycle["loss_count"] = int(cycle.get("loss_count") or 0) + (result == "LOSS")
            cycle["last_result"] = result
            cycle["last_target_round_id"] = source.get("round_id")
            maximum = (int(_research_by_id[rule_id]["condition"].get("maximum_selections") or 1)
                       if rule_id in _research_by_id else None)
            if maximum is not None and cycle["resolved_count"] >= maximum:
                cycle.update({"active": False, "status": "COMPLETED",
                              "ended_at": at, "end_round_index": source["round_index"],
                              "end_reason": "MAXIMUM_TARGETS_REACHED"})
                state["cycle_mode"] = "IDLE"
        if int(state.get("loss_limit_losses") or 0) >= RULE_LOSS_LIMIT:
            state.update({"loss_limit_blocked": True, "loss_limit_reached_at": at,
                          "loss_limit_reached_round_index": source.get("round_index"),
                          "loss_limit_terminal_round_index": source.get("round_index"),
                          "entry_signal_latched": True, "cycle_mode": "BLOCKED_UNTIL_NEW_ENTRY"})
            if cycle and cycle.get("active"):
                cycle.update({"active": False, "status": "LOSS_LIMIT_REACHED",
                              "ended_at": at, "end_round_index": source.get("round_index"),
                              "end_reason": "LOSS_LIMIT_REACHED"})
            if rule_id in {START_CHECK_POLICY_ID, LEGACY_POLICY_ID, COMBINED_POLICY_ID}:
                state.update({"rule_state": "WAIT_LOW", "mode": "STANDARD",
                              "previous_outcome": None, "failed_cycle_count": 0,
                              "consecutive_bet_wins": 0, "selected_outcomes": [],
                              "special_skip_active": False,
                              "special_skip_after_round_index": None,
                              "special_skip_win_count": 0,
                              "special_skip_terminal_round_index": None})
            return
        if rule_id in _research_by_id:
            return
        if rule_id not in {START_CHECK_POLICY_ID, LEGACY_POLICY_ID, COMBINED_POLICY_ID}:
            return
        state["selected_outcomes"].append(outcome)
        special = state["mode"] == "TWO_BET_WINS"
        previous = state.get("previous_outcome")
        if rule_id in SPECIAL_SKIP_POLICY_IDS and state.get("special_skip_active") and not special:
            if outcome == "H":
                special_wins = int(state.get("special_skip_win_count", 0)) + 1
                if special_wins >= 2:
                    state.update({"rule_state": "WAIT_LOW", "mode": "STANDARD",
                                  "previous_outcome": None, "failed_cycle_count": 0,
                                  "consecutive_bet_wins": 0, "selected_outcomes": [],
                                  "special_skip_active": False,
                                  "special_skip_after_round_index": None,
                                  "special_skip_win_count": 0})
                    return
                state.update({"previous_outcome": "H", "special_skip_win_count": special_wins,
                              "special_skip_after_round_index": int(source["round_index"])})
                return
            # A real selected LOSS inside the special sequence ends this policy
            # cycle immediately. The same verified round is evaluated below,
            # so retain its index to prevent WAIT_LOW reprocessing it.
            state.update({"rule_state": "WAIT_LOW", "mode": "STANDARD",
                          "previous_outcome": None, "consecutive_bet_wins": 0,
                          "selected_outcomes": [], "special_skip_active": False,
                          "special_skip_after_round_index": None,
                          "special_skip_win_count": 0,
                          "special_skip_terminal_round_index": int(source["round_index"])})
            return
        if (rule_id in SPECIAL_SKIP_POLICY_IDS and not special
                and state.get("mode") == "STANDARD" and not state.get("special_skip_active")
                and previous == "L" and outcome == "H"
                and state["selected_outcomes"] == ["L", "H"]):
            state.update({"special_skip_active": True,
                          "special_skip_after_round_index": int(source["round_index"]),
                          "special_skip_win_count": 0, "previous_outcome": "H"})
            return
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

    @staticmethod
    def _source_round(source: Mapping[str, Any] | None) -> dict[str, Any] | None:
        if not isinstance(source, Mapping):
            return None
        round_id = source.get("round_id", source.get("roundId"))
        stream_id = source.get("stream_id", source.get("streamId"))
        round_index = source.get("round_index", source.get("roundIndex"))
        if round_id is None and stream_id is None and round_index is None:
            return None
        return {"stream_id": stream_id, "round_id": round_id, "round_index": round_index}

    @staticmethod
    def _append_stop_event(session: dict[str, Any], reason: str, at: str, stop_source: str,
                           source_round: Mapping[str, Any] | None, scope: str,
                           details: Mapping[str, Any] | None = None) -> dict[str, Any]:
        event = {"reason": reason, "source": stop_source, "stopped_at": at,
                 "source_round": deepcopy(dict(source_round)) if source_round else None,
                 "scope": scope, "details": deepcopy(dict(details or {}))}
        events = session.get("stop_history")
        if not isinstance(events, list):
            events = []
            session["stop_history"] = events
        if not any(item.get("reason") == reason and item.get("stopped_at") == at
                   and item.get("scope") == scope for item in events if isinstance(item, Mapping)):
            events.append(event)
        return event

    @staticmethod
    def _resume_after_round_gap(session: dict[str, Any], row: Mapping[str, Any], at: str,
                                expected_index: int, *, skip_boundary_target: bool = False) -> None:
        """Start a clean verified history segment after a committed index gap."""
        received_index = int(row.get("round_index") or 0)
        remaining_late_transitions = []
        for item in session.get("late_target_transitions") or []:
            if not isinstance(item, Mapping):
                continue
            try:
                target_index = int(item.get("target_round_index") or 0)
            except (TypeError, ValueError, OverflowError):
                continue
            target_is_boundary = skip_boundary_target and target_index == received_index
            if (item.get("target_stream_id") != row.get("stream_id")
                    or target_index > received_index
                    or (target_index == received_index and not target_is_boundary)):
                remaining_late_transitions.append(item)
                continue
            raw_proposal = item.get("proposal")
            proposal = deepcopy(dict(raw_proposal)) if isinstance(raw_proposal, Mapping) else {}
            resolution = {"status": "TARGET_SKIPPED_BY_ROUND_GAP",
                          "target_round_index": target_index,
                          "actual_bet_placed": False}
            proposal["late_target_resolution"] = resolution
            nested = proposal.get("proposal")
            if not isinstance(nested, dict):
                nested = {}
                proposal["proposal"] = nested
            relevant = nested.get("relevantInputs")
            if not isinstance(relevant, dict):
                relevant = {}
                nested["relevantInputs"] = relevant
            relevant["lateTargetResolution"] = deepcopy(resolution)
            for record in (session.get("event_records") or {}).values():
                if not isinstance(record, Mapping):
                    continue
                if record.get("source_round") != item.get("source_round"):
                    continue
                for saved in record.get("rule_evaluations") or []:
                    if isinstance(saved, dict) and saved.get("rule_id") == item.get("rule_id"):
                        saved.update(deepcopy(proposal))
            for record in session.get("evaluations") or []:
                if not isinstance(record, Mapping):
                    continue
                if record.get("source_round") != item.get("source_round"):
                    continue
                for saved in record.get("rule_evaluations") or []:
                    if isinstance(saved, dict) and saved.get("rule_id") == item.get("rule_id"):
                        saved.update(deepcopy(proposal))
        session["late_target_transitions"] = remaining_late_transitions
        ledgers = [session.get("selections") or []]
        ledgers.extend((session.get("rule_selections") or {}).values())
        for selections in ledgers:
            for selection in selections:
                if selection.get("status") != "PENDING":
                    continue
                target = selection.get("expected_target") or selection.get("target_round") or {}
                try:
                    target_index = int(target.get("roundIndex", target.get("round_index")))
                except (TypeError, ValueError, OverflowError):
                    continue
                target_stream = target.get("streamId", target.get("stream_id"))
                target_was_skipped = (target_index < received_index
                                      or (skip_boundary_target and target_index == received_index))
                if target_stream == row.get("stream_id") and target_was_skipped:
                    selection.update({"status": "SKIPPED_GAP", "result": None,
                                      "settlement_status": "SKIPPED_GAP",
                                      "settlement_phase": "ROUND_GAP",
                                      "settlement_block_reason": "TARGET_INDEX_SKIPPED_BY_ROUND_GAP",
                                      "gap_skipped_at": at})
        pending_selection_ids = {
            str(selection.get("selection_id"))
            for selections in ledgers for selection in selections
            if selection.get("status") == "PENDING" and selection.get("selection_id")
        }
        previous_states = session.get("engines") or {}
        reset_states: dict[str, dict[str, Any]] = {}
        for rule_id in session.get("rule_ids") or []:
            fresh = _fresh_engine(rule_id)
            previous = previous_states.get(rule_id) or {}
            # Keep cumulative results, but reset consecutive failures and
            # pattern state because they belong to the broken segment.
            for key in ("selected_wins", "selected_losses", "recovery_count"):
                if key in previous:
                    fresh[key] = previous[key]
            pending_id = previous.get("pending_selection_id")
            if pending_id is not None and str(pending_id) in pending_selection_ids:
                fresh["pending_selection_id"] = pending_id
                fresh["pending_target"] = deepcopy(previous.get("pending_target"))
            reset_states[rule_id] = fresh
        session["engines"] = reset_states
        session["context"] = []
        gap = {
            "expected_index": int(expected_index),
            "received_index": received_index,
            "missing_from_index": int(expected_index) if received_index > expected_index else None,
            "missing_to_index": received_index - 1 if received_index > expected_index else None,
            "round_id": row.get("round_id"),
            "resumed_at": at,
        }
        session["last_continuity_gap"] = gap
        session.setdefault("continuity_gap_history", []).append(deepcopy(gap))
        session["continuity_gap_history"] = session["continuity_gap_history"][-100:]
        log.warning("SHADOW_CONTINUITY_GAP session_id=%s stream_id=%s missing_from=%s missing_to=%s boundary_round_id=%s boundary_index=%s",
                    session.get("session_id"), row.get("stream_id"), gap["missing_from_index"],
                    gap["missing_to_index"], row.get("round_id"), received_index)
        manual_pause = (session.get("status") == "PAUSED"
                        and session.get("stop_reason") == "MANUAL_PAUSE")
        resumed_status = ("PAUSED" if manual_pause else
                          "RUNNING" if session.get("selection_status") == "ACTIVE" else "SETTLING")
        resumed_reason = "MANUAL_PAUSE" if manual_pause else None
        updates = {"status": resumed_status, "stop_reason": resumed_reason,
                   "gap_expected_index": None, "gap_received_index": None,
                   # The committed round itself becomes the first source in
                   # the new segment, so the normal path accepts it below.
                   "last_round_index": int(row.get("round_index") or 1) - 1,
                   "last_round_id": None}
        if not manual_pause:
            updates.update({"stop_source": None, "stop_at": None, "stop_round_id": None,
                            "stop_details": {}})
        session.update(updates)

    @staticmethod
    def _persisted_late_target(source: Mapping[str, Any], target_index: int,
                               target: Mapping[str, Any] | None) -> dict[str, Any] | None:
        if not isinstance(target, Mapping):
            return None
        verified = target.get("continuity_verified")
        if not (verified is True or verified == 1):
            return None
        multiplier = _multiplier(target.get("multiplier"))
        round_id = target.get("round_id")
        try:
            actual_index = int(target.get("round_index") or 0)
        except (TypeError, ValueError, OverflowError):
            return None
        if (multiplier is None or not isinstance(round_id, str) or not round_id.strip()
                or actual_index != int(target_index)):
            return None
        return {"stream_id": source["stream_id"], "round_id": round_id,
                "observation_id": round_id, "round_index": int(target_index),
                "multiplier": multiplier, "outcome": classify_outcome(multiplier),
                "event_type": "round.persisted", "durability": "COMMITTED",
                "continuity_status": "VERIFIED"}

    @staticmethod
    def _observed_late_target(source: Mapping[str, Any], target_index: int,
                              event: Mapping[str, Any] | None) -> dict[str, Any] | None:
        if (not isinstance(event, Mapping) or event.get("event_type") != "round.observed"
                or event.get("durability") != "PENDING"
                or event.get("continuity_status") != "VERIFIED"):
            return None
        target = _identity(event, require_sequence=False)
        if (target is None or target.get("stream_id") != source.get("stream_id")
                or target.get("round_index") != int(target_index)):
            return None
        return target

    def _apply_late_target_to_rule_state(self, session: dict[str, Any], proposal: dict[str, Any],
                                         target: Mapping[str, Any], at: str) -> dict[str, Any] | None:
        rule_id = str(proposal.get("rule_id") or "")
        state = (session.get("engines") or {}).get(rule_id)
        if (not isinstance(state, dict) or state.get("pending_selection_id")
                or target.get("outcome") not in {"H", "L"}):
            return None
        selection_id = "late:" + hashlib.sha256("\0".join((
            str(session.get("session_id") or ""), rule_id,
            str((proposal.get("source_round") or {}).get("roundId") or ""),
            str((proposal.get("target_round") or {}).get("roundIndex") or ""),
        )).encode("utf-8")).hexdigest()
        virtual_selection = {"selection_id": selection_id, "selected_at": at,
                             "source_round": deepcopy(proposal.get("source_round") or {}),
                             "expected_target": deepcopy(proposal.get("target_round") or {}),
                             "contributing_rule_ids": [rule_id]}
        self._commit_contributor(session, proposal, virtual_selection)
        result = "WIN" if target.get("outcome") == "H" else "LOSS"
        self._advance_selected_state(session, rule_id, state, dict(target), result,
                                     virtual_selection, at)
        if state.get("pending_selection_id") == selection_id:
            state["pending_selection_id"] = None
            state["pending_target"] = None
        resolution = {"status": "APPLIED_TO_RULE_STATE", "result": result,
                      "target_round": _reference(target),
                      "target_persistence_status": target.get("durability"),
                      "actual_bet_placed": False}
        proposal["late_target_resolution"] = deepcopy(resolution)
        proposal.setdefault("proposal", {}).setdefault("relevantInputs", {})[
            "lateTargetResolution"] = deepcopy(resolution)
        after = deepcopy(state)
        proposal.setdefault("state_snapshot", {})["after"] = after
        proposal.setdefault("proposal", {}).setdefault("privateState", {})["after"] = deepcopy(after)
        return resolution

    def _resolve_deferred_late_targets(self, session: dict[str, Any], row: Mapping[str, Any],
                                       at: str, *, observed_stage: bool = False,
                                       source_metadata: Mapping[str, Any] | None = None) -> bool:
        pending = session.get("late_target_transitions") or []
        if not pending:
            return False
        remaining = []
        changed = False
        for item in pending:
            if not isinstance(item, Mapping):
                continue
            try:
                target_index = int(item.get("target_round_index") or 0)
                current_index = int(row.get("round_index") or 0)
            except (TypeError, ValueError, OverflowError):
                continue
            if item.get("target_stream_id") != row.get("stream_id"):
                remaining.append(item)
                continue
            target: dict[str, Any] | None = None
            if target_index == current_index:
                if (row.get("continuity_status") == "VERIFIED"
                        and row.get("event_type") in {"round.observed", "round.persisted"}
                        and row.get("durability") in {"PENDING", "COMMITTED"}
                        and row.get("round_id") and _multiplier(row.get("multiplier")) is not None):
                    target = dict(row)
            if target is None and target_index <= current_index:
                # Retry canonical stores when the target's first observation
                # lacked continuity proof or its event was missed. Do this
                # before allowing a later source to evaluate stale policy state.
                checker = getattr(self._persistence, "round_continuity_for_indices", None)
                if (observed_stage and isinstance(source_metadata, Mapping)
                        and source_metadata.get("source") == "ORBIT_DATA_WATCH_API"):
                    checker = None
                if callable(checker):
                    try:
                        persisted = checker([target_index])
                    except Exception:
                        persisted = None
                    if isinstance(persisted, Mapping):
                        target = self._persisted_late_target(
                            {"stream_id": row.get("stream_id")}, target_index,
                            persisted.get(target_index))
                if target is None:
                    reader = getattr(self.observation_inbox, "pending_round_for_index", None)
                    if callable(reader):
                        try:
                            observed = reader(target_index)
                        except Exception:
                            observed = None
                        target = self._observed_late_target(
                            {"stream_id": row.get("stream_id")}, target_index, observed)
            if target is None:
                remaining.append(item)
                continue
            raw_proposal = item.get("proposal")
            proposal = deepcopy(dict(raw_proposal)) if isinstance(raw_proposal, Mapping) else {}
            resolution = self._apply_late_target_to_rule_state(session, proposal, target, at)
            if resolution is None:
                remaining.append(item)
                continue
            source_round = item.get("source_round")
            rule_id = proposal.get("rule_id")
            for record in (session.get("event_records") or {}).values():
                if not isinstance(record, Mapping):
                    continue
                if record.get("source_round") != source_round:
                    continue
                for saved in record.get("rule_evaluations") or []:
                    if isinstance(saved, dict) and saved.get("rule_id") == rule_id:
                        saved.update(deepcopy(proposal))
            for record in session.get("evaluations") or []:
                if not isinstance(record, Mapping):
                    continue
                if record.get("source_round") != source_round:
                    continue
                for saved in record.get("rule_evaluations") or []:
                    if isinstance(saved, dict) and saved.get("rule_id") == rule_id:
                        saved.update(deepcopy(proposal))
            latest = session.get("last_evaluation")
            if isinstance(latest, dict) and latest.get("source_round") == source_round:
                for saved in latest.get("rule_evaluations") or []:
                    if isinstance(saved, dict) and saved.get("rule_id") == rule_id:
                        saved.update(deepcopy(proposal))
            changed = True
        session["late_target_transitions"] = remaining
        return changed

    def _record_session_stop(self, session: dict[str, Any], reason: str, at: str,
                             stop_source: str, source_round: Mapping[str, Any] | None = None,
                             *, stop_selection: bool = False,
                             details: Mapping[str, Any] | None = None) -> None:
        ref = self._source_round(source_round) or self._source_round({
            "stream_id": session.get("last_stream_id"), "round_id": session.get("last_round_id"),
            "round_index": session.get("last_round_index"),
        })
        scope = "SESSION_AND_SELECTION" if stop_selection else "SESSION"
        event = self._append_stop_event(session, reason, at, stop_source, ref, scope, details)
        session.update({"stop_reason": reason, "stop_source": stop_source, "stop_at": at,
                        "stop_round_id": (ref or {}).get("round_id"), "stop_details": deepcopy(dict(details or {})),
                        "last_stop_event": event})
        if stop_selection:
            session.update({"selection_status": "STOPPED", "selection_stop_reason": reason,
                            "selection_stop_source": stop_source, "selection_stopped_at": at,
                            "selection_stopped_at_round_id": (ref or {}).get("round_id"),
                            "selection_stop_details": deepcopy(dict(details or {}))})

    def _stop_selecting(self, session: dict[str, Any], reason: str, at: str,
                        source: Mapping[str, Any], *, stop_source: str | None = None,
                        stop_details: Mapping[str, Any] | None = None) -> None:
        if session.get("selection_status") == "ACTIVE":
            ref = self._source_round(source)
            stop_source = stop_source or "RULE_ENGINE"
            if ((session.get("configuration_snapshot") or {}).get("run_until_stopped") is True
                    and reason in SESSION_LIMIT_STOP_REASONS):
                self._record_session_stop(session, reason, at, stop_source, source,
                    stop_selection=True, details=stop_details)
                session["limit_stop_latched"] = True
                if self._pending_selection_count(session) > 0:
                    session["status"] = "SETTLING"
                else:
                    session["status"] = "COMPLETED"
                    session["ended_at"] = at
                self._sync_automatic_candidate_lifecycle(session)
                return
            event = self._append_stop_event(session, reason, at, stop_source, ref,
                                            "SELECTION", stop_details)
            session.update({"selection_status": "STOPPED", "selection_stop_reason": reason,
                            "selection_stop_source": stop_source,
                            "selection_stopped_at": at,
                            "selection_stopped_at_round_id": (ref or {}).get("round_id"),
                            "selection_stop_details": deepcopy(dict(stop_details or {})),
                            "last_selection_stop_event": event})
            if session.get("status") == "RUNNING":
                # The collector continues resolving pending targets and
                # completing the observation window, but new selections stop.
                session["status"] = "SETTLING"
            self._sync_automatic_candidate_lifecycle(session)

    def _reconcile_net_outcome_limit_stop(self, session: dict[str, Any], wins: int, losses: int,
                                          at: str, source: Mapping[str, Any]) -> None:
        """Keep the active selection stop aligned with settled net outcomes.

        Stop events are append-only history. A net limit can cease to be true
        as already-pending selections settle, so its active fields and status
        must be cleared without deleting that historical event.
        """
        if session.get("limit_stop_latched") is True:
            return
        config = session.get("configuration_snapshot") or {}
        net_stop = self._net_outcome_stop_reason(wins, losses, config)
        active_reason = session.get("selection_stop_reason")
        active_source = session.get("selection_stop_source")
        is_net_stop = (active_reason in NET_OUTCOME_LIMIT_REASONS
                       and active_source in {None, "NET_OUTCOME_LIMIT", "LEGACY_UNVERIFIED"})

        if net_stop is None:
            if is_net_stop:
                stopped_at = session.get("selection_stopped_at")
                if stopped_at and not any(
                    isinstance(event, Mapping)
                    and event.get("reason") == active_reason
                    and event.get("stopped_at") == stopped_at
                    and event.get("scope") in {"SELECTION", "SESSION_AND_SELECTION"}
                    for event in (session.get("stop_history") or [])
                ):
                    self._append_stop_event(session, active_reason, stopped_at,
                        active_source or "NET_OUTCOME_LIMIT", None, "SELECTION",
                        session.get("selection_stop_details"))
                session.update({"selection_status": "ACTIVE", "selection_stop_reason": None,
                    "selection_stop_source": None, "selection_stopped_at": None,
                    "selection_stopped_at_round_id": None, "selection_stop_details": {}})
                if session.get("status") == "SETTLING":
                    session["status"] = "RUNNING"
            if (session.get("stop_reason") in NET_OUTCOME_LIMIT_REASONS
                    and session.get("stop_source") in {None, "NET_OUTCOME_LIMIT", "LEGACY_UNVERIFIED"}):
                session.update({"stop_reason": None, "stop_source": None, "stop_at": None,
                                "stop_round_id": None, "stop_details": {}})
            if (session.get("auto_next_trigger") == "NET_OUTCOME_LIMIT"
                    and session.get("auto_next_armed")
                    and not session.get("auto_next_session_id")):
                session.update({"auto_next_armed": False, "auto_next_trigger": None,
                                "auto_next_armed_at": None,
                                "auto_next_block_reason": "LIMIT_CONDITION_NO_LONGER_ACTIVE"})
            return

        limit_name = "WIN_LIMIT" if net_stop == "WIN_LIMIT_REACHED" else "LOSS_LIMIT"
        details = {"limit": limit_name,
                   "limit_count": config.get("win_limit_count" if limit_name == "WIN_LIMIT"
                                              else "loss_limit_count"),
                   "settled_wins": wins, "settled_losses": losses,
                   "net_outcome": wins - losses}
        if session.get("selection_status") == "ACTIVE":
            self._stop_selecting(session, net_stop, at, source,
                                 stop_source="NET_OUTCOME_LIMIT", stop_details=details)
            return
        if is_net_stop:
            if active_reason != net_stop:
                ref = self._source_round(source)
                event = self._append_stop_event(session, net_stop, at, "NET_OUTCOME_LIMIT",
                                                ref, "SELECTION", details)
                session.update({"selection_stop_reason": net_stop,
                    "selection_stop_source": "NET_OUTCOME_LIMIT",
                    "selection_stopped_at": at,
                    "selection_stopped_at_round_id": (ref or {}).get("round_id"),
                    "last_selection_stop_event": event})
            session["selection_stop_details"] = details
            if session.get("status") == "RUNNING":
                session["status"] = "SETTLING"

    @staticmethod
    def _arm_auto_next_after_limit(session: dict[str, Any], reason: str | None,
                                   at: str, source: Mapping[str, Any]) -> None:
        if session.get("configuration_snapshot", {}).get("auto_next_on_limit_enabled") is not True:
            return
        # Per-window WIN/LOSS rules always finish their current window. The
        # immediate trigger continues to apply to legacy configured stops.
        if reason == "AUTO_NEXT_RESULT_LIMIT_REACHED":
            return
        # Keep the first configured trigger. In particular, a maximum-selection
        # limit is armed when its final selection is created; settlement of
        # that selection must not move the next-check boundary one round later.
        if session.get("auto_next_armed") is True:
            return
        if reason not in {"PROFIT_TARGET_REACHED", "MAX_SELECTIONS_REACHED",
                          *NET_OUTCOME_LIMIT_REASONS}:
            return
        expected_source = ("PROFIT_TARGET" if reason == "PROFIT_TARGET_REACHED"
                           else "MAX_SELECTIONS" if reason == "MAX_SELECTIONS_REACHED"
                           else "NET_OUTCOME_LIMIT")
        if (session.get("selection_status") != "STOPPED"
                or session.get("selection_stop_reason") != reason
                or session.get("selection_stop_source") != expected_source
                or session.get("status") not in {"SETTLING", "COMPLETED"}
                or session.get("auto_next_session_id")):
            return
        context = deepcopy(session.get("context") or [])
        if not context or context[-1].get("round_id") != source.get("round_id"):
            context.append(deepcopy(dict(source)))
        session.update({"auto_next_armed": True, "auto_next_trigger": "LIMIT_REACHED",
                        "auto_next_armed_at": at,
                        "auto_next_after_round_id": source.get("round_id"),
                        "auto_next_boundary": None,
                        "auto_next_context": context[-5:],
                        "auto_next_block_reason": None})
        if (source.get("event_type") == "round.persisted"
                and source.get("durability") == "COMMITTED"
                and source.get("continuity_status") == "VERIFIED"
                and int(source.get("stream_sequence") or 0) > 0):
            RulesEngine._record_auto_next_boundary(session, source)

    @staticmethod
    def _auto_next_window_trigger_enabled(session: Mapping[str, Any]) -> bool:
        configuration = session.get("configuration_snapshot") or {}
        custom_limit_reached = RulesEngine._custom_window_result_limit_reached(session)
        return configuration.get("auto_next_check_enabled") is True or custom_limit_reached

    @staticmethod
    def _custom_window_result_limit_reached(session: Mapping[str, Any]) -> bool:
        configuration = session.get("configuration_snapshot") or {}
        return (configuration.get("auto_next_limits_enabled") is True
            and session.get("selection_stop_reason") == "AUTO_NEXT_RESULT_LIMIT_REACHED"
            and session.get("selection_stop_source") == "AUTO_NEXT_RESULT_LIMIT")

    @staticmethod
    def _recover_auto_next_boundary(session: dict[str, Any]) -> None:
        """Recover a legacy armed boundary only from matching commit evidence."""
        configuration = session.get("configuration_snapshot") or {}
        window_auto_next = (session.get("status") == "COMPLETED"
            and session.get("auto_next_trigger") in {None, "WINDOW_COMPLETED"}
            and RulesEngine._auto_next_window_trigger_enabled(session))
        limit_auto_next = (session.get("status") in {"SETTLING", "COMPLETED"}
            and session.get("auto_next_trigger") == "LIMIT_REACHED"
            and configuration.get("auto_next_on_limit_enabled") is True
            and session.get("selection_stop_source") in {
                "PROFIT_TARGET", "NET_OUTCOME_LIMIT", "MAX_SELECTIONS", "AUTO_NEXT_RESULT_LIMIT"}
            and session.get("selection_stop_reason") in {
                "PROFIT_TARGET_REACHED", "MAX_SELECTIONS_REACHED",
                "AUTO_NEXT_RESULT_LIMIT_REACHED",
                *NET_OUTCOME_LIMIT_REASONS})
        if (session.get("auto_next_armed") is not True
                and session.get("auto_next_block_reason") == "AUTO_NEXT_COMMITTED_BOUNDARY_UNAVAILABLE"
                and not session.get("auto_next_session_id")
                and (window_auto_next or limit_auto_next)):
            # Older processing could disarm auto-next before the matching
            # persisted event attached its commit proof. Retry recovery from
            # the durable event ledger instead of leaving the session stopped.
            session["auto_next_armed"] = True
            session["auto_next_armed_at"] = (session.get("auto_next_armed_at")
                                               or session.get("ended_at"))
            session["auto_next_block_reason"] = None
        if (session.get("auto_next_armed") is not True
                or isinstance(session.get("auto_next_boundary"), Mapping)):
            return
        after_round_id = session.get("auto_next_after_round_id")
        context = deepcopy(session.get("context") or [])
        candidate = next((row for row in reversed(context)
                          if row.get("round_id") == after_round_id), None)
        if not isinstance(candidate, dict):
            return
        committed = next((record for record in (session.get("event_records") or {}).values()
            if record.get("persistence_status") == "COMMITTED"
            and (record.get("source_round") or {}).get("roundId") == after_round_id), None)
        if not isinstance(committed, Mapping):
            return
        evidence = committed.get("commit_evidence") or {}
        sequence = committed.get("committed_stream_sequence") or evidence.get("stream_sequence")
        if (evidence.get("event_type") != "round.persisted"
                or evidence.get("durability") != "COMMITTED"
                or evidence.get("continuity_status") != "VERIFIED"
                or isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 1):
            return
        candidate.update({"stream_sequence": sequence, "event_type": "round.persisted",
                          "durability": "COMMITTED", "continuity_status": "VERIFIED"})
        session["auto_next_boundary"] = deepcopy(candidate)
        for index, row in enumerate(context):
            if (row.get("stream_id") == candidate.get("stream_id")
                    and row.get("round_id") == after_round_id):
                context[index] = deepcopy(candidate)
        session["auto_next_context"] = context[-5:]

    @staticmethod
    def _record_auto_next_boundary(session: dict[str, Any], source: Mapping[str, Any]) -> None:
        """Freeze the committed round and context that triggered auto-next."""
        if (source.get("event_type") != "round.persisted"
                or source.get("durability") != "COMMITTED"
                or source.get("continuity_status") != "VERIFIED"
                or int(source.get("stream_sequence") or 0) < 1):
            return
        boundary = deepcopy(dict(source))
        context = deepcopy(session.get("auto_next_context") or session.get("context") or [])
        replaced = False
        for index, row in enumerate(context):
            if row.get("stream_id") == boundary.get("stream_id") \
                    and row.get("round_id") == boundary.get("round_id"):
                context[index] = boundary
                replaced = True
        if not replaced:
            context.append(boundary)
        session["auto_next_boundary"] = boundary
        session["auto_next_context"] = context[-5:]

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
            proposal = self._policy_proposal(session, rid, source, expected, at, before)
            transition = proposal["proposal"].get("relevantInputs", {}).get("transition") or {}
            transition_type = transition.get("type")
            policy_cycle = state.get("cycle") or {}
            if transition_type in {"TRIGGER", "CONTINUE", "SPECIAL_SKIP_BET"}:
                proposal["proposal"]["relevantInputs"]["ruleCycle"] = {
                    "action": "START" if transition_type == "TRIGGER" else "CONTINUE",
                    "cycle_id": policy_cycle.get("cycle_id"),
                    "active": bool(policy_cycle.get("active")),
                    "maximum_targets": None,
                    "end_condition": "Policy stop transition or two losses; then wait for a fresh entry.",
                }
            proposal["proposal"]["relevantInputs"]["lossLimit"] = {
                "losses": int(state.get("loss_limit_losses") or 0),
                "limit": RULE_LOSS_LIMIT,
                "blocked": bool(state.get("loss_limit_blocked")),
            }
            if transition_type == "TRIGGER":
                if state.get("entry_signal_latched") and not state.get("loss_limit_blocked"):
                    reason = "L,H entry signal was already used; waiting for a new L,H cycle."
                    proposal.update({"eligible": False, "result": False,
                                     "status": "WAITING_FOR_NEW_ENTRY", "reason": reason})
                    proposal["proposal"].update({"eligible": False,
                                                  "status": "WAITING_FOR_NEW_ENTRY", "reason": reason})
                    transition["type"] = "ENTRY_ALREADY_USED"
                elif state.get("loss_limit_blocked") and state.get("entry_signal_latched"):
                    reason = "LOSS_LIMIT_WAITING_FOR_NEW_ENTRY"
                    proposal.update({"eligible": False, "result": False,
                                     "status": reason, "reason": reason})
                    proposal["proposal"].update({"eligible": False,
                                                  "status": reason, "reason": reason})
                    transition["type"] = "ENTRY_ALREADY_USED"
                else:
                    if state.get("loss_limit_blocked"):
                        proposal["proposal"]["relevantInputs"]["lossLimitRearmed"] = True
            elif transition_type not in {"CONTINUE", "SPECIAL_SKIP_ARMED", "SPECIAL_SKIP_BET"}:
                state["entry_signal_latched"] = False
            if state.get("loss_limit_blocked"):
                if transition_type != "TRIGGER":
                    reason = "LOSS_LIMIT_WAITING_FOR_NEW_ENTRY"
                    proposal.update({"eligible": False, "result": False,
                                     "status": reason, "reason": reason})
                    proposal["proposal"].update({"eligible": False,
                                                  "status": reason, "reason": reason})
            elif transition_type == "ENTRY_ALREADY_USED":
                pass
            after = proposal["state_snapshot"]["after"]
            for key in ("entry_signal_latched", "loss_limit_losses", "loss_limit_blocked",
                        "loss_limit_reached_at", "loss_limit_reached_round_index"):
                after[key] = deepcopy(state.get(key))
            proposal["proposal"]["privateState"]["after"] = deepcopy(after)
            return proposal
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
            matched = 2.0 < number < 3.0
            reason = "Igikubo cya source kiri hejuru ya 2.00 ariko munsi ya 3.00." if matched else "Igikubo cya source kiri hanze ya (2.00, 3.00)."
        else:
            cycle = state.get("cycle")
            if cycle and cycle.get("active"):
                matched = int(cycle.get("requested_count", 0)) < RULE_CYCLE_MAX_TARGETS
                cycle_id = cycle.get("cycle_id")
                reason = "Cycle ya HH irasaba target ikurikiraho." if matched else "Cycle ya HH yarasabiye targets eshatu."
            else:
                matched = values == ["H", "H"]
                reason = "HH yabonetse; itegeko risaba target ikurikira." if matched else "HH ntiraboneka ku source."
        cycle = state.get("cycle")
        continuing_after_trigger = rid == "AFTER_HH_CONTINUE_MAX_3" and bool(cycle and cycle.get("active"))
        cycle_action = None
        rearmed = False
        if not matched and not continuing_after_trigger:
            state["entry_signal_latched"] = False
            if cycle and cycle.get("active"):
                cycle.update({"active": False, "status": "ENTRY_SIGNAL_ENDED",
                              "ended_at": at, "end_round_index": source["round_index"],
                              "end_reason": "ENTRY_SIGNAL_CLEARED"})
                state["cycle_mode"] = "IDLE"
        else:
            if state.get("loss_limit_blocked"):
                if state.get("entry_signal_latched"):
                    matched = False
                    reason = "LOSS_LIMIT_WAITING_FOR_NEW_ENTRY"
                else:
                    rearmed = True
            if matched:
                maximum = max(1, int(definition["condition"].get("maximum_selections") or 1))
                if cycle and cycle.get("active"):
                    if int(cycle.get("requested_count", 0)) >= maximum:
                        cycle.update({"active": False, "status": "MAX_TARGETS_REACHED",
                                      "ended_at": at, "end_round_index": source["round_index"],
                                      "end_reason": "MAXIMUM_TARGETS_REACHED"})
                        state["cycle_mode"] = "IDLE"
                        matched = False
                        reason = f"Maximum {maximum} target(s) reached; waiting for a fresh entry."
                    else:
                        cycle_action = "CONTINUE"
                        cycle_id = cycle.get("cycle_id")
                        state["entry_signal_latched"] = True
                elif state.get("entry_signal_latched"):
                    matched = False
                    reason = "Entry signal was already used; waiting for it to clear and appear again."
                else:
                    cycle_action = "START"
                    reason = f"Fresh entry signal; start target 1 of {maximum}."
            elif state.get("loss_limit_blocked"):
                reason = "LOSS_LIMIT_WAITING_FOR_NEW_ENTRY"
        inputs = {"history": [{"roundId": item["round_id"], "roundIndex": item["round_index"],
                                "multiplier": item["multiplier"], "outcome": item["outcome"]} for item in tail],
                  "condition": deepcopy(definition["condition"])}
        if rid == "AFTER_HH_CONTINUE_MAX_3":
            inputs["continuationCycle"] = deepcopy(state.get("cycle"))
        if cycle_action:
            inputs["ruleCycle"] = {"action": cycle_action, "cycle_id": cycle_id,
                                    "maximum_targets": int(definition["condition"].get("maximum_selections") or 1)}
        inputs["lossLimit"] = {"losses": int(state.get("loss_limit_losses") or 0),
                                "limit": RULE_LOSS_LIMIT,
                                "blocked": bool(state.get("loss_limit_blocked")),
                                "rearmed": rearmed}
        status = ("LOSS_LIMIT_WAITING_FOR_ENTRY" if reason == "LOSS_LIMIT_WAITING_FOR_NEW_ENTRY"
                  else "WAITING_FOR_NEW_ENTRY" if "waiting for" in reason.lower()
                  else "TRUE" if matched else "FALSE")
        return _result(rid, definition["version"], session, source, expected, matched,
                       status, reason, inputs, before, state, at)

    def _policy_proposal(self, session: dict[str, Any], rid: str, source: dict[str, Any],
                         expected: dict[str, Any], at: str, before: dict[str, Any]) -> dict[str, Any]:
        state = session["engines"][rid]
        outcome = source["outcome"]
        mode = state["mode"]
        matched = False
        reason = "Rule iracyategereje L."
        transition: dict[str, Any] | None = None
        after = deepcopy(state)
        loss_limit_terminal_round_index = state.get("loss_limit_terminal_round_index")
        try:
            is_loss_limit_terminal_round = (loss_limit_terminal_round_index is not None
                and int(source["round_index"]) == int(loss_limit_terminal_round_index))
        except (TypeError, ValueError, OverflowError):
            is_loss_limit_terminal_round = False
        terminal_round_index = state.get("special_skip_terminal_round_index")
        try:
            is_special_loss_terminal_round = (
                rid in SPECIAL_SKIP_POLICY_IDS
                and terminal_round_index is not None
                and int(source["round_index"]) == int(terminal_round_index))
        except (TypeError, ValueError, OverflowError):
            is_special_loss_terminal_round = False
        if is_loss_limit_terminal_round:
            # A target settlement may close a policy on this same verified
            # round. Do not reuse that result as a fresh entry observation.
            after["loss_limit_terminal_round_index"] = None
            reason = "Loss limit closed the cycle; the terminal round is not reprocessed as an entry."
            transition = {"type": "LOSS_LIMIT_TERMINAL_ROUND"}
        elif is_special_loss_terminal_round:
            # Settlement already closed the cycle; ignore this same source as
            # a new WAIT_LOW observation, then consume the one-round marker.
            after["special_skip_terminal_round_index"] = None
            reason = "Special bet LOSS yahagaritse cycle; round imwe ntisubirwamo kuri WAIT_LOW."
            transition = {"type": "SPECIAL_SKIP_LOSS_TERMINAL_ROUND"}
        elif (rid in SPECIAL_SKIP_POLICY_IDS and mode == "STANDARD"
                and state.get("special_skip_active")):
            armed_after = state.get("special_skip_after_round_index")
            try:
                skip_round_is_upcoming = int(source["round_index"]) > int(armed_after)
            except (TypeError, ValueError, OverflowError):
                skip_round_is_upcoming = False
            if not skip_round_is_upcoming:
                reason = "Special skip irategereje round imwe ikurikira."
                transition = {"type": "SPECIAL_SKIP_ARMED"}
            elif float(source["multiplier"]) <= H_THRESHOLD:
                matched = True
                reason = "Round ya special skip ni L (<= 2.00); BET isabwa source+1."
                transition = {"type": "SPECIAL_SKIP_BET"}
            else:
                reason = "Round ya special skip ni H (> 2.00); cycle irangiye isubiye kuri WAIT_LOW."
                after.update({"rule_state": "WAIT_LOW", "mode": "STANDARD",
                              "previous_outcome": None, "failed_cycle_count": 0,
                              "consecutive_bet_wins": 0, "selected_outcomes": [],
                              "special_skip_active": False,
                              "special_skip_after_round_index": None,
                              "special_skip_win_count": 0})
                transition = {"type": "SPECIAL_SKIP_TERMINATE"}
        elif state["rule_state"] == "WAIT_LOW":
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
        if rid in _research_by_id:
            relevant = proposal["proposal"].get("relevantInputs", {})
            cycle_action = (relevant.get("ruleCycle") or {}).get("action")
            if relevant.get("lossLimit", {}).get("rearmed") is True:
                state.update({"loss_limit_blocked": False, "loss_limit_losses": 0,
                              "loss_limit_reached_at": None,
                              "loss_limit_reached_round_index": None})
            cycle = state.get("cycle")
            if cycle_action == "START" or not cycle or not cycle.get("active"):
                cycle = {"cycle_id": str(uuid4()), "active": True, "status": "ACTIVE",
                         "requested_count": 0, "resolved_count": 0, "loss_count": 0,
                         "maximum_targets": int((relevant.get("ruleCycle") or {}).get("maximum_targets")
                                                 or 1),
                         "end_condition": ((relevant.get("ruleCycle") or {}).get("end_condition")
                                           or "Maximum targets resolved or entry signal clears."),
                         "entry_round_id": selection["source_round"]["roundId"],
                         "started_at": selection["selected_at"]}
                state["cycle"] = cycle
                state["cycle_mode"] = ("CONTINUE_MAX_3" if rid == "AFTER_HH_CONTINUE_MAX_3"
                                        else "SIGNAL_CYCLE")
            cycle["requested_count"] += 1
            cycle["last_source_round_id"] = selection["source_round"]["roundId"]
            selection.setdefault("rule_cycle_ids", {})[rid] = cycle["cycle_id"]
            state["entry_signal_latched"] = True
        elif rid in {START_CHECK_POLICY_ID, LEGACY_POLICY_ID, COMBINED_POLICY_ID}:
            relevant = proposal["proposal"].get("relevantInputs", {})
            transition_type = relevant.get("transition", {}).get("type")
            if transition_type in {"TRIGGER", "CONTINUE", "SPECIAL_SKIP_BET"}:
                cycle = state.get("cycle")
                if transition_type == "TRIGGER" or not cycle or not cycle.get("active"):
                    cycle = {"cycle_id": str(uuid4()), "active": True, "status": "ACTIVE",
                             "requested_count": 0, "resolved_count": 0, "loss_count": 0,
                             "maximum_targets": None,
                             "end_condition": "Policy stop transition or two losses; then wait for a fresh entry.",
                             "entry_round_id": selection["source_round"]["roundId"],
                             "started_at": selection["selected_at"]}
                    state["cycle"] = cycle
                    state["cycle_mode"] = "POLICY_CYCLE"
                cycle["requested_count"] = int(cycle.get("requested_count") or 0) + 1
                selection.setdefault("rule_cycle_ids", {})[rid] = cycle["cycle_id"]
            if transition_type == "TRIGGER":
                if relevant.get("lossLimitRearmed") is True:
                    state.update({"loss_limit_blocked": False, "loss_limit_losses": 0,
                                  "loss_limit_reached_at": None,
                                  "loss_limit_reached_round_index": None})
                state["rule_state"] = "PLAYING"
                state["previous_outcome"] = "H" if rid in {START_CHECK_POLICY_ID, LEGACY_POLICY_ID} else None
                state["selected_outcomes"] = []
                state["cycle_number"] = int(state.get("cycle_number", 0)) + 1
                state["entry_signal_latched"] = True
            elif transition_type == "CONTINUE":
                state["rule_state"] = "PLAYING"

    def observe_round(self, event: Mapping[str, Any], *, now: str | None = None,
                      target_already_completed: bool = False,
                      defer_persistence: bool = False,
                      defer_performance_observer: bool = False) -> dict[str, Any]:
        """Consume a verified observed or committed event, preserving its two stages."""
        processing_started_ns = time.perf_counter_ns()
        lock_wait_ms = 0.0
        policy_evaluation_ms = 0.0
        standard_rule_evaluation_ms = 0.0
        selected_result_evaluation_ms = 0.0
        two_bet_wins_transition_ms = 0.0
        target_identity_check_ms = 0.0
        decision_aggregation_ms = 0.0
        persistence_timings: dict[str, float] = {}
        at = _now(now)
        if not isinstance(event, Mapping):
            event = {}
        observed_stage = (event.get("event_type") == "round.observed" and event.get("durability") == "PENDING")
        committed_stage = (event.get("event_type") == "round.persisted" and event.get("durability") == "COMMITTED")
        source_gap_boundary = event.get("continuity_status") == "GAP_BEFORE"
        row = _identity(event, require_sequence=not observed_stage)
        trusted = bool(row and (observed_stage or committed_stage)
                       and event.get("continuity_status") in {"VERIFIED", "GAP_BEFORE"})
        if row is not None and trusted:
            # _identity intentionally keeps only round identity fields. Preserve
            # the verified delivery evidence too, so auto-next can freeze the
            # committed boundary for both window and configured-limit triggers.
            row.update({"event_type": event["event_type"],
                        "durability": event["durability"],
                        "continuity_status": event["continuity_status"]})
        lock_requested_ns = time.perf_counter_ns()
        with self._lock:
            lock_wait_ms = (time.perf_counter_ns() - lock_requested_ns) / 1_000_000.0
            dirty_session_ids: set[str] = set()
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
                    if session.get("status") not in {"RUNNING", "SETTLING", "PAUSED"}:
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
                              "sessions": sessions,
                              "evaluated_at": at}
                self.last_evaluation, self.last_evaluation_at = evaluation, at
                self._persist(session_ids=set())
                return deepcopy(evaluation)
            performance_changed = False
            if (committed_stage and self._performance_observer_enabled
                    and not defer_performance_observer):
                try:
                    performance_changed = self._observe_rule_performance_round(event)
                    self._performance_observer_error = None
                except Exception as exc:
                    # Report-only analytics must never block normal SHADOW
                    # evaluation or delivery. The durable inbox can replay a
                    # missed observer boundary after a restart.
                    self._performance_observer_error = type(exc).__name__
                    performance_changed = True
                    log.exception("rule performance collector observer failed round=%s",
                                  event.get("round_id"))
            if performance_changed:
                # The state writer persists this compact observer alongside
                # normal session updates and acknowledges the durable inbox
                # only after the snapshot commits.
                dirty_session_ids.add(RULE_PERFORMANCE_CHECKPOINT_ID)
            # Auto-next must be based on a PostgreSQL-committed contiguous
            # round. An observed/pending event cannot start another session.
            if committed_stage:
                dirty_session_ids.update(self._spawn_auto_next_sessions(row, at))
            settlement_started_ns = time.perf_counter_ns()
            settlements_by_session: dict[str, list[dict[str, Any]]] = {}
            for session in list(self.sessions.values()):
                if session.get("last_stream_id") != row["stream_id"]:
                    continue
                if committed_stage and not source_gap_boundary:
                    # Commit confirmation is immutable evidence for an already
                    # settled target, including sessions that have since ended.
                    if self._mark_target_persistence_committed(session, row):
                        dirty_session_ids.add(str(session["session_id"]))
                if not self._session_accepts_settlement_events(session):
                    continue
                if observed_stage or not session.get("last_received_round"):
                    session["last_received_round"] = {
                        "stream_id": row["stream_id"], "round_id": row["round_id"],
                        "round_index": row["round_index"], "multiplier": row["multiplier"],
                        "event_type": event.get("event_type"),
                        "durability": event.get("durability"),
                        "observed_at": row.get("observed_at"),
                        "received_at": event.get("api_received_at"),
                    }
                    dirty_session_ids.add(str(session["session_id"]))
                known_round = next((record.get("source_round")
                    for record in (session.get("event_records") or {}).values()
                    if isinstance(record, Mapping)
                    and isinstance(record.get("source_round"), Mapping)
                    and record["source_round"].get("streamId") == row["stream_id"]
                    and record["source_round"].get("roundId") == row["round_id"]), None)
                has_pending_target = not source_gap_boundary and any(
                    selection.get("status") == "PENDING" and self._selection_targets(selection, row)
                    for selection in list(session.get("selections", []))
                    + (list(session.get("adaptive_automatic_baseline_selections") or [])
                       if session.get("adaptive_rule_selection") is True else []))
                # A replay can settle outstanding work only if that target
                # event has not already been consumed. This prevents a late
                # or corrupt selection row from being backdated onto history.
                resolve_started_ns = time.perf_counter_ns()
                settled_items = ([] if known_round is not None or source_gap_boundary
                                 else self._resolve(session, row, at))
                if not source_gap_boundary and self._resolve_deferred_late_targets(
                        session, row, at, observed_stage=observed_stage,
                        source_metadata=event.get("source_metadata")):
                    dirty_session_ids.add(str(session["session_id"]))
                resolve_elapsed_ms = (time.perf_counter_ns() - resolve_started_ns) / 1_000_000.0
                selected_result_evaluation_ms += resolve_elapsed_ms
                if any(state.get("mode") == "TWO_BET_WINS"
                       for state in (session.get("engines") or {}).values()
                       if isinstance(state, Mapping)):
                    two_bet_wins_transition_ms += resolve_elapsed_ms
                if settled_items or has_pending_target:
                    dirty_session_ids.add(str(session["session_id"]))
                visible_settlements = [item for item in settled_items
                                       if item.get("automatic_baseline") is not True]
                if visible_settlements:
                    session["last_settled_target"] = {
                        "stream_id": row["stream_id"], "round_id": row["round_id"],
                        "round_index": row["round_index"], "multiplier": row["multiplier"],
                        "event_type": event.get("event_type"),
                        "durability": event.get("durability"),
                        "observed_at": row.get("observed_at"),
                        "settled_at": at, "settlement_count": len(visible_settlements),
                        "results": deepcopy(visible_settlements),
                    }
                    settlements_by_session[session["session_id"]] = settled_items
            settlement_processing_ms = (time.perf_counter_ns() - settlement_started_ns) / 1_000_000.0
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
                            session["status"] = "PAUSED"
                            self._record_session_stop(session, "ROUND_IDENTITY_CONFLICT", at,
                                "ROUND_IDENTITY_VALIDATION", row,
                                details={"event_key": committed_key})
                            dirty_session_ids.add(str(session["session_id"]))
                        results.append(deepcopy(committed_prior))
                        continue
                    if pending_prior:
                        if pending_prior.get("source_round") != _reference(row):
                            session["status"] = "PAUSED"
                            self._record_session_stop(session, "OBSERVED_COMMIT_IDENTITY_CONFLICT", at,
                                "OBSERVED_COMMIT_IDENTITY_VALIDATION", row,
                                details={"event_key": pending_key})
                            dirty_session_ids.add(str(session["session_id"]))
                            continue
                        if pending_prior.get("gap_recovery_pending"):
                            # The observed event was deliberately held until its
                            # PostgreSQL commit proved the new boundary. Resume
                            # and evaluate that committed round as the first
                            # source in a clean history segment.
                            previous_sequence = int(session.get("last_stream_sequence") or 0)
                            expected_now = int(session.get("last_round_index") or 0) + 1
                            legacy_gap = (session.get("stop_reason") == "ROUND_INDEX_GAP"
                                or (int(session.get("gap_expected_index") or 0) > 0
                                    and int(session.get("gap_received_index") or 0) > 0))
                            manual_pause = (session.get("status") == "PAUSED"
                                and session.get("stop_reason") == "MANUAL_PAUSE")
                            can_recover = (session.get("status") in {"RUNNING", "SETTLING"}
                                           or (legacy_gap and session.get("status") == "PAUSED")
                                           or manual_pause)
                            if (can_recover and int(row["stream_sequence"]) > previous_sequence
                                    and (legacy_gap or int(row["round_index"]) >= expected_now)):
                                if legacy_gap or int(row["round_index"]) > expected_now:
                                    gap_expected = int(session.get("gap_expected_index") or
                                                       pending_prior.get("gap_expected_index") or expected_now)
                                    self._resume_after_round_gap(session, row, at, gap_expected)
                                if (session.get("auto_next_armed") is True
                                        and session.get("auto_next_after_round_id") == row["round_id"]):
                                    self._record_auto_next_boundary(session, row)
                                for selection in session.get("selections", []):
                                    actual_target = selection.get("actual_target") or {}
                                    if (actual_target.get("streamId") == row["stream_id"]
                                            and actual_target.get("roundId") == row["round_id"]):
                                        selection["target_persistence_status"] = "COMMITTED"
                                for selections in (session.get("rule_selections") or {}).values():
                                    for selection in selections:
                                        actual_target = selection.get("actual_target") or {}
                                        if (actual_target.get("streamId") == row["stream_id"]
                                                and actual_target.get("roundId") == row["round_id"]):
                                            selection["target_persistence_status"] = "COMMITTED"
                                session["event_records"].pop(pending_key, None)
                                dirty_session_ids.add(str(session["session_id"]))
                                # Continue below through normal committed
                                # source evaluation instead of merely promoting
                                # the provisional WAIT record.
                            else:
                                pending_prior["persistence_status"] = "COMMITTED"
                                pending_prior["committed_stream_sequence"] = row["stream_sequence"]
                                pending_prior["commit_evidence"] = {
                                    "event_type": event.get("event_type"),
                                    "durability": event.get("durability"),
                                    "continuity_status": event.get("continuity_status"),
                                    "delivery_mode": str(event.get("delivery_mode") or "LIVE").upper(),
                                    "stream_id": row["stream_id"],
                                    "stream_sequence": row["stream_sequence"],
                                }
                                if (session.get("auto_next_armed") is True
                                        and session.get("auto_next_after_round_id") == row["round_id"]):
                                    self._record_auto_next_boundary(session, row)
                                session["event_records"][committed_key] = pending_prior
                                session["event_records"].pop(pending_key, None)
                                session["last_stream_sequence"] = max(previous_sequence,
                                                                       int(row["stream_sequence"]))
                                for selection in session.get("selections", []):
                                    actual_target = selection.get("actual_target") or {}
                                    if (actual_target.get("streamId") == row["stream_id"]
                                            and actual_target.get("roundId") == row["round_id"]):
                                        selection["target_persistence_status"] = "COMMITTED"
                                for selections in (session.get("rule_selections") or {}).values():
                                    for selection in selections:
                                        actual_target = selection.get("actual_target") or {}
                                        if (actual_target.get("streamId") == row["stream_id"]
                                                and actual_target.get("roundId") == row["round_id"]):
                                            selection["target_persistence_status"] = "COMMITTED"
                                dirty_session_ids.add(str(session["session_id"]))
                                results.append(deepcopy(pending_prior))
                                continue
                        else:
                            pending_prior["persistence_status"] = "COMMITTED"
                            pending_prior["committed_stream_sequence"] = row["stream_sequence"]
                            pending_prior["commit_evidence"] = {
                                "event_type": event.get("event_type"),
                                "durability": event.get("durability"),
                                "continuity_status": event.get("continuity_status"),
                                "delivery_mode": str(event.get("delivery_mode") or "LIVE").upper(),
                                "stream_id": row["stream_id"],
                                "stream_sequence": row["stream_sequence"],
                            }
                            if (session.get("auto_next_armed") is True
                                    and session.get("auto_next_after_round_id") == row["round_id"]):
                                self._record_auto_next_boundary(session, row)
                            session["event_records"][committed_key] = pending_prior
                            session["event_records"].pop(pending_key, None)
                            for saved_evaluation in session.get("evaluations", []):
                                if (saved_evaluation.get("source_round") == pending_prior.get("source_round")
                                        and saved_evaluation.get("evaluated_at") == pending_prior.get("evaluated_at")):
                                    saved_evaluation["persistence_status"] = "COMMITTED"
                                    saved_evaluation["committed_stream_sequence"] = row["stream_sequence"]
                            session["last_stream_sequence"] = max(int(session.get("last_stream_sequence", 0)), row["stream_sequence"])
                            for selection in session.get("selections", []):
                                actual_target = selection.get("actual_target") or {}
                                if (actual_target.get("streamId") == row["stream_id"]
                                        and actual_target.get("roundId") == row["round_id"]):
                                    selection["target_persistence_status"] = "COMMITTED"
                            for selections in session.get("rule_selections", {}).values():
                                for selection in selections:
                                    actual_target = selection.get("actual_target") or {}
                                    if (actual_target.get("streamId") == row["stream_id"]
                                            and actual_target.get("roundId") == row["round_id"]):
                                        selection["target_persistence_status"] = "COMMITTED"
                            dirty_session_ids.add(str(session["session_id"]))
                            results.append(deepcopy(pending_prior))
                            continue
                prior = session.get("event_records", {}).get(record_key)
                if prior:
                    if prior.get("source_round") != _reference(row):
                        session["status"] = "PAUSED"
                        self._record_session_stop(session, "ROUND_IDENTITY_CONFLICT", at,
                            "ROUND_IDENTITY_VALIDATION", row, details={"event_key": record_key})
                        dirty_session_ids.add(str(session["session_id"]))
                    results.append(deepcopy(prior))
                    continue
                if observed_stage:
                    committed_prior = next((item for item in session.get("event_records", {}).values()
                        if item.get("persistence_status") == "COMMITTED"
                        and isinstance(item.get("source_round"), Mapping)
                        and item["source_round"].get("streamId") == row["stream_id"]
                        and item["source_round"].get("roundId") == row["round_id"]), None)
                    if committed_prior:
                        if committed_prior.get("source_round") != _reference(row):
                            session["status"] = "PAUSED"
                            self._record_session_stop(session, "ROUND_IDENTITY_CONFLICT", at,
                                "ROUND_IDENTITY_VALIDATION", row,
                                details={"event_key": "committed_round_replay"})
                            dirty_session_ids.add(str(session["session_id"]))
                        results.append(deepcopy(committed_prior))
                        continue
                if self._effective_session_status(session) == "COMPLETED":
                    # Do not keep treating a legacy full-window SETTLING row
                    # as an active observer after its verified quota is met.
                    continue
                if session.get("status") not in {"RUNNING", "SETTLING", "PAUSED", "MANUALLY_STOPPED"}:
                    continue
                legacy_gap_pause = (session["status"] == "PAUSED"
                    and session.get("stop_reason") == "ROUND_INDEX_GAP")
                gap_marker_active = (session.get("stop_reason") == "ROUND_INDEX_GAP"
                    or (int(session.get("gap_expected_index") or 0) > 0
                        and int(session.get("gap_received_index") or 0) > 0))
                manual_pause = (session.get("status") == "PAUSED"
                                and session.get("stop_reason") == "MANUAL_PAUSE")
                can_recover_gap_marker = (session.get("status") in {"RUNNING", "SETTLING"}
                    or legacy_gap_pause or manual_pause)
                if (session["status"] == "PAUSED"
                        and session.get("stop_reason") not in {None, "MANUAL_PAUSE"}
                        and not legacy_gap_pause):
                    continue
                if row["stream_id"] != session["last_stream_id"]:
                    session["status"] = "PAUSED"
                    self._record_session_stop(session, "STREAM_IDENTITY_CHANGED", at,
                        "STREAM_IDENTITY_VALIDATION", row,
                        details={"expected_stream_id": session.get("last_stream_id"),
                                 "received_stream_id": row["stream_id"]})
                    dirty_session_ids.add(str(session["session_id"]))
                    continue
                if not observed_stage and row["stream_sequence"] <= session["last_stream_sequence"]:
                    # A replay older than the persisted cursor is a no-op; no retrospective selections.
                    continue
                expected_index = int(session["last_round_index"]) + 1
                if source_gap_boundary and row["round_index"] >= expected_index:
                    # A GAP_BEFORE round cannot settle a target from the prior
                    # segment or produce a decision. It is still a real,
                    # identified round and must seed the new segment so the
                    # next verified event does not lose one round of context.
                    self._resume_after_round_gap(session, row, at, expected_index,
                                                 skip_boundary_target=True)
                    boundary_context = deepcopy(row)
                    boundary_context["continuity_boundary"] = True
                    session["context"] = [boundary_context]
                    session["observed_rounds"] = int(session.get("observed_rounds") or 0) + 1
                    session.update({"last_stream_sequence": (row["stream_sequence"] if committed_stage
                                                               else session["last_stream_sequence"]),
                                    "last_round_index": row["round_index"],
                                    "last_round_id": row["round_id"],
                                    "last_activity_at": at})
                    record = {"session_id": session["session_id"],
                              "source_round": _reference(row), "target_round": None,
                              "rule_evaluations": [],
                              "decision": {"action": "WAIT",
                                           "reason": "CONTINUITY_GAP_BOUNDARY",
                                           "true_rule_ids": [], "selection_id": None},
                              "evaluated_at": at,
                              "persistence_status": "PENDING" if observed_stage else "COMMITTED",
                              "target_identity_guard": "CONTINUITY_GAP_BOUNDARY"}
                    if committed_stage:
                        record["committed_stream_sequence"] = row["stream_sequence"]
                        record["commit_evidence"] = {
                            "event_type": event.get("event_type"),
                            "durability": event.get("durability"),
                            "continuity_status": event.get("continuity_status"),
                            "delivery_mode": str(event.get("delivery_mode") or "LIVE").upper(),
                            "stream_id": row["stream_id"],
                            "stream_sequence": row["stream_sequence"],
                        }
                    shared_decision = {
                        "decision_id": f"{session['session_id']}:{row['stream_id']}:{row['stream_sequence']}:shared",
                        "session_id": session["session_id"],
                        "source_round": _reference(row), "target_round": None,
                        "action": "NOT_EVALUATED", "reason": "CONTINUITY_GAP_BOUNDARY",
                        "true_rule_ids": [], "selection_id": None, "evaluated_at": at,
                        "persistence_status": record["persistence_status"],
                    }
                    record["shared_decision"] = shared_decision
                    session.setdefault("event_records", {})[record_key] = record
                    session["evaluations"].append(deepcopy(record))
                    session["evaluations"] = session["evaluations"][-500:]
                    session.setdefault("decisions", []).append(deepcopy(shared_decision))
                    session["decisions"] = session["decisions"][-500:]
                    dirty_session_ids.add(str(session["session_id"]))
                    results.append(record)
                    continue
                if gap_marker_active and committed_stage and can_recover_gap_marker:
                    # Older persisted sessions may already have a cursor beyond
                    # the collector's real round index. A newer committed
                    # delivery is the recovery boundary, even when its index
                    # is lower than that poisoned cursor.
                    gap_expected = int(session.get("gap_expected_index") or
                                       int(session.get("last_round_index") or 0) + 1)
                    self._resume_after_round_gap(session, row, at, gap_expected)
                    legacy_gap_pause = False
                    dirty_session_ids.add(str(session["session_id"]))
                expected_index = int(session["last_round_index"]) + 1
                if gap_marker_active and observed_stage:
                    # Hold the event until its canonical commit arrives; then
                    # it can safely become the first source in the new segment.
                    record = {"session_id": session["session_id"],
                              "source_round": _reference(row), "target_round": None,
                              "rule_evaluations": [],
                              "decision": {"action": "WAIT",
                                           "reason": "ROUND_INDEX_GAP_WAITING_FOR_COMMIT"},
                              "evaluated_at": at, "persistence_status": "PENDING",
                              "gap_recovery_pending": can_recover_gap_marker,
                              "gap_expected_index": int(session.get("gap_expected_index") or expected_index)}
                    session.setdefault("event_records", {})[record_key] = record
                    dirty_session_ids.add(str(session["session_id"]))
                    results.append(record)
                    continue
                legacy_gap_received_index = int(session.get("gap_received_index") or 0)
                stale_round = (row["round_index"] < expected_index
                    or (legacy_gap_pause and legacy_gap_received_index
                        and row["round_index"] < legacy_gap_received_index))
                if stale_round:
                    # A late target can still settle above, but it must never
                    # rewind source progress or unpause a gap-paused session.
                    record = {"session_id": session["session_id"],
                              "source_round": _reference(row), "target_round": None,
                              "rule_evaluations": [],
                              "decision": {"action": "WAIT", "reason": "OUT_OF_ORDER_ROUND_IGNORED"},
                              "evaluated_at": at,
                              "persistence_status": "PENDING" if observed_stage else "COMMITTED"}
                    if committed_stage:
                        record["committed_stream_sequence"] = row["stream_sequence"]
                        record["commit_evidence"] = {
                            "event_type": event.get("event_type"),
                            "durability": event.get("durability"),
                            "continuity_status": event.get("continuity_status"),
                            "delivery_mode": str(event.get("delivery_mode") or "LIVE").upper(),
                            "stream_id": row["stream_id"],
                            "stream_sequence": row["stream_sequence"],
                        }
                    session.setdefault("event_records", {})[record_key] = record
                    if committed_stage:
                        session["last_stream_sequence"] = max(
                            int(session.get("last_stream_sequence") or 0), row["stream_sequence"])
                    dirty_session_ids.add(str(session["session_id"]))
                    results.append(record)
                    continue
                if row["round_index"] > expected_index:
                    details = {"expected_index": expected_index,
                               "received_index": int(row["round_index"])}
                    manual_pause = (session.get("status") == "PAUSED"
                                    and session.get("stop_reason") == "MANUAL_PAUSE")
                    can_recover_gap = (session.get("status") in {"RUNNING", "SETTLING"}
                                       or legacy_gap_pause or manual_pause)
                    source_metadata = event.get("source_metadata")
                    fresh_watch_observation = (
                        observed_stage and isinstance(source_metadata, Mapping)
                        and source_metadata.get("source") == "ORBIT_DATA_WATCH_API"
                        and str(event.get("delivery_mode") or "").upper() == "LIVE"
                    )
                    if (observed_stage and not fresh_watch_observation) or not can_recover_gap:
                        # A pending observation cannot move the durable cursor.
                        # Preserve the session and let the matching commit
                        # establish the fresh boundary before evaluation.
                        record = {"session_id": session["session_id"],
                                  "source_round": _reference(row), "target_round": None,
                                  "rule_evaluations": [],
                                  "decision": {"action": "WAIT", "reason": "ROUND_INDEX_GAP",
                                               **details},
                                  "evaluated_at": at,
                                  "persistence_status": "PENDING" if observed_stage else "COMMITTED",
                                  "gap_recovery_pending": bool(observed_stage and can_recover_gap),
                                  "gap_expected_index": expected_index}
                        if committed_stage:
                            record["committed_stream_sequence"] = row["stream_sequence"]
                            record["commit_evidence"] = {
                                "event_type": event.get("event_type"),
                                "durability": event.get("durability"),
                                "continuity_status": event.get("continuity_status"),
                                "delivery_mode": str(event.get("delivery_mode") or "LIVE").upper(),
                                "stream_id": row["stream_id"],
                                "stream_sequence": row["stream_sequence"],
                            }
                        session.setdefault("event_records", {})[record_key] = record
                        dirty_session_ids.add(str(session["session_id"]))
                        results.append(record)
                        continue
                    self._resume_after_round_gap(session, row, at, expected_index)
                    dirty_session_ids.add(str(session["session_id"]))
                unresolved_late_target = False
                for pending_transition in session.get("late_target_transitions") or []:
                    if (not isinstance(pending_transition, Mapping)
                            or pending_transition.get("target_stream_id") != row.get("stream_id")):
                        continue
                    try:
                        unresolved_late_target = (
                            int(pending_transition.get("target_round_index") or 0)
                            <= int(row.get("round_index") or 0))
                    except (TypeError, ValueError, OverflowError):
                        continue
                    if unresolved_late_target:
                        break
                if unresolved_late_target:
                    # Do not allow any rule to bet from policy state that is
                    # waiting for a verified result from an earlier late target.
                    session["context"].append(deepcopy(row))
                    session["context"] = session["context"][-5:]
                    session["observed_rounds"] += 1
                    session.update({"last_round_index": row["round_index"],
                                    "last_round_id": row["round_id"]})
                    record = {"session_id": session["session_id"],
                              "source_round": _reference(row), "target_round": None,
                              "rule_evaluations": [],
                              "decision": {"action": "WAIT",
                                           "reason": "LATE_TARGET_RESULT_UNVERIFIED"},
                              "evaluated_at": at,
                              "persistence_status": "PENDING" if observed_stage else "COMMITTED"}
                    if committed_stage:
                        session["last_stream_sequence"] = row["stream_sequence"]
                        record["committed_stream_sequence"] = row["stream_sequence"]
                    session.setdefault("event_records", {})[record_key] = record
                    dirty_session_ids.add(str(session["session_id"]))
                    results.append(record)
                    continue
                observed_at = _parse_time(row.get("observed_at"))
                started_at = _parse_time(session.get("started_at"))
                auto_next_start_round = session.get("auto_next_start_round_id")
                auto_next_first_contiguous_round = (
                    session.get("auto_next_accept_first_round") is True
                    and int(row.get("round_index") or 0)
                    == int(session.get("last_round_index") or 0) + 1)
                if (observed_stage and observed_at is not None and started_at is not None
                        and observed_at.tzinfo is not None and started_at.tzinfo is not None
                        and observed_at.astimezone(timezone.utc) <= started_at.astimezone(timezone.utc)
                        and not auto_next_first_contiguous_round
                        and row.get("round_id") != auto_next_start_round):
                    # The pending-priority path can reach Rules Engine before
                    # PostgreSQL. Preserve the old session boundary semantics:
                    # a round already visible when the session started may
                    # advance context, but can never produce a selection.
                    session["context"].append(deepcopy(row))
                    session["context"] = session["context"][-5:]
                    session["prestart_skipped_rounds"] = int(
                        session.get("prestart_skipped_rounds", 0)) + 1
                    session.update({"last_round_index": row["round_index"],
                                    "last_round_id": row["round_id"]})
                    record = {"session_id": session["session_id"],
                              "source_round": _reference(row), "target_round": _expected(row),
                              "rule_evaluations": [],
                              "decision": {"action": "WAIT", "reason": "PRESTART_ROUND"},
                              "evaluated_at": at, "persistence_status": "PENDING",
                              "prestart_only": True}
                    session.setdefault("event_records", {})[record_key] = record
                    dirty_session_ids.add(str(session["session_id"]))
                    results.append(record)
                    continue
                if (committed_stage and observed_at is not None and started_at is not None
                        and observed_at.tzinfo is not None and started_at.tzinfo is not None
                        and observed_at.astimezone(timezone.utc) <= started_at.astimezone(timezone.utc)
                        and not auto_next_first_contiguous_round
                        and row.get("round_id") != auto_next_start_round):
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
                    dirty_session_ids.add(str(session["session_id"]))
                    continue
                if session["status"] == "PAUSED" and session.get("stop_reason") == "MANUAL_PAUSE":
                    session["context"].append(deepcopy(row))
                    session["context"] = session["context"][-5:]
                    session["observed_rounds"] += 1
                    session.update({"status": "PAUSED", "last_stream_sequence": row["stream_sequence"],
                                    "last_round_index": row["round_index"], "last_round_id": row["round_id"]})
                    record = {"source_round": _reference(row), "rule_evaluations": [],
                              "decision": {"action": "WAIT", "reason": "MANUALLY_PAUSED"},
                              "evaluated_at": at}
                    session.setdefault("event_records", {})[record_key] = record
                    dirty_session_ids.add(str(session["session_id"]))
                    results.append(record)
                    continue
                history = deepcopy(session["context"]) + [deepcopy(row)]
                session["observed_rounds"] += 1
                session["context"] = history[-5:]
                if session.get("automatic_rule_selection") is True:
                    choice = self._automatic_rule_choice(session, row)
                    session["active_rule_ids"] = choice["rule_ids"]
                    session["automatic_rule_choice"] = choice
                elif session.get("adaptive_rule_selection") is True:
                    choice = self._adaptive_rule_choice(session, row, history, at)
                    session["adaptive_rule_choice"] = choice
                active_rule_ids = set(session["active_rule_ids"] if (
                    session.get("automatic_rule_selection") or session.get("adaptive_rule_selection"))
                    else session["rule_ids"])
                final_window_source = (session.get("window_size") is not None
                                       and session["observed_rounds"] >= session["window_size"])
                active = (session["selection_status"] == "ACTIVE" and session["status"] == "RUNNING"
                          and not final_window_source and not target_already_completed)
                evaluation_rows: list[dict[str, Any]] = []
                proposals: list[dict[str, Any]] = []
                automatic_baseline_session: dict[str, Any] | None = None
                automatic_baseline_choice: dict[str, Any] | None = None
                automatic_baseline_proposals: list[dict[str, Any]] = []
                late_true_rule_ids: set[str] = set()
                for rid in session["rule_ids"]:
                    state = session["engines"][rid]
                    before = deepcopy(state)
                    proposal_started_ns = time.perf_counter_ns()
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
                    proposal_elapsed_ms = (time.perf_counter_ns() - proposal_started_ns) / 1_000_000.0
                    if rid in {COMBINED_POLICY_ID, START_CHECK_POLICY_ID, LEGACY_POLICY_ID}:
                        policy_evaluation_ms += proposal_elapsed_ms
                    else:
                        standard_rule_evaluation_ms += proposal_elapsed_ms
                    # Automatic mode treats the mode's whole policy catalog as
                    # candidates; the legacy enabled toggle applies only to
                    # Manual participation.
                    proposal["enabled"] = True
                    if session.get("automatic_rule_selection") is True:
                        proposal["selected_by_automatic_rule"] = rid in active_rule_ids
                    if session.get("adaptive_rule_selection") is True:
                        proposal["selected_by_adaptive_rule"] = rid in active_rule_ids
                        proposal["adaptive_selection_version"] = session.get("adaptive_selection_version")
                        proposal["behavior_context"] = self._adaptive_context_signature(history)
                    proposal["evaluation_id"] = hashlib.sha256("\0".join((
                        str(session["session_id"]), rid, str(row["stream_id"]),
                        str(row["round_index"]))).encode("utf-8")).hexdigest()
                    if target_already_completed and proposal.get("eligible") is True:
                        late_true_rule_ids.add(rid)
                        proposal["eligible"] = None
                        proposal["result"] = None
                        proposal["status"] = "TARGET_ALREADY_COMPLETED"
                        proposal["reason"] = "Target yamaze kuboneka; nta cyemezo cyasubijwe inyuma."
                        proposal["proposal"].update({"eligible": None, "status": "TARGET_ALREADY_COMPLETED",
                                                     "reason": proposal["reason"]})
                    if final_window_source and proposal.get("eligible") is not None:
                        proposal["eligible"] = None
                        proposal["result"] = None
                        proposal["status"] = "WINDOW_COMPLETED_NO_TARGET"
                        proposal["reason"] = "Idirishya rirangiye; nta target nshya yemewe."
                        proposal["proposal"].update({"eligible": None,
                                                     "status": "WINDOW_COMPLETED_NO_TARGET",
                                                     "reason": proposal["reason"]})
                    if rid in {START_CHECK_POLICY_ID, LEGACY_POLICY_ID, COMBINED_POLICY_ID}:
                        transition = proposal.get("proposal", {}).get("relevantInputs", {}).get("transition") or {}
                        if (transition.get("type") == "SPECIAL_SKIP_TERMINATE"
                                and proposal.get("status") != "TARGET_ALREADY_COMPLETED"):
                            # A skipped H closes only its owning policy cycle;
                            # it is verified source evidence, never a bet result.
                            policy_state = session["engines"][rid]
                            policy_state.update(deepcopy(proposal["state_snapshot"]["after"]))
                            cycle = policy_state.get("cycle")
                            if cycle and cycle.get("active"):
                                cycle.update({"active": False, "status": "COMPLETED",
                                              "ended_at": at, "end_round_index": row["round_index"],
                                              "end_reason": "SPECIAL_SKIP_H_TERMINAL"})
                                policy_state["cycle_mode"] = "IDLE"
                            proposal.setdefault("state_snapshot", {})["after"] = deepcopy(policy_state)
                            proposal.setdefault("proposal", {}).setdefault("privateState", {})["after"] = (
                                deepcopy(policy_state))
                        elif (transition.get("type") == "SPECIAL_SKIP_LOSS_TERMINAL_ROUND"
                              and proposal.get("status") != "TARGET_ALREADY_COMPLETED"):
                            session["engines"][rid]["special_skip_terminal_round_index"] = None
                        elif proposal.get("eligible") is False:
                            # Waiting-state transitions are owned by this policy
                            # and depend only on its verified source, not aggregation.
                            session["engines"][rid]["rule_state"] = proposal["state_snapshot"]["after"].get("rule_state")
                    evaluation_rows.append(proposal)
                    if active and rid in active_rule_ids:
                        proposals.append(proposal)
                if active and session.get("adaptive_rule_selection") is True:
                    (automatic_baseline_session, automatic_baseline_choice,
                     automatic_baseline_proposals) = self._adaptive_automatic_baseline_proposals(
                        session, row, history, at)
                target_identity_guard = ("TARGET_ALREADY_COMPLETED" if target_already_completed
                                          else "NOT_APPLICABLE")
                target_guard_unavailable = False
                late_target_row: dict[str, Any] | None = None
                target_index = int(row["round_index"]) + 1
                checker = getattr(self._persistence, "round_continuity_for_indices", None)
                observation_checker = getattr(self.observation_inbox,
                                              "has_pending_round_index", None)
                pending_target_reader = getattr(self.observation_inbox,
                                                "pending_round_for_index", None)
                target_guard_started_ns = time.perf_counter_ns()
                if active and (any(proposal.get("eligible") is True for proposal in proposals)
                               or any(proposal.get("eligible") is True
                                      for proposal in automatic_baseline_proposals)):
                    if target_already_completed:
                        target_identity_guard = "TARGET_ALREADY_COMPLETED"
                    else:
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
                                late_target_row = self._persisted_late_target(
                                    row, target_index, existing_target[target_index])
                            elif already_observed:
                                target_already_completed = True
                                target_identity_guard = "TARGET_ALREADY_OBSERVED"
                                pending_target = (pending_target_reader(target_index)
                                                  if callable(pending_target_reader) else None)
                                late_target_row = self._observed_late_target(
                                    row, target_index, pending_target)
                            elif callable(checker) or callable(observation_checker):
                                target_identity_guard = "NO_COMPLETED_TARGET_FOUND"
                elif target_already_completed:
                    target_identity_guard = "TARGET_ALREADY_COMPLETED"
                if target_already_completed and late_target_row is None:
                    # A caller may already know the target is late. Recover its
                    # verified result from canonical history or the observation
                    # inbox when available, so private rule state can catch up.
                    try:
                        existing_target = (checker([target_index])
                                           if callable(checker) else {})
                    except Exception:
                        existing_target = {}
                    if existing_target and target_index in existing_target:
                        late_target_row = self._persisted_late_target(
                            row, target_index, existing_target[target_index])
                    if late_target_row is None and callable(pending_target_reader):
                        try:
                            pending_target = pending_target_reader(target_index)
                        except Exception:
                            pending_target = None
                        late_target_row = self._observed_late_target(
                            row, target_index, pending_target)
                target_identity_check_ms += (time.perf_counter_ns() - target_guard_started_ns) / 1_000_000.0
                if target_already_completed or target_guard_unavailable:
                        reason = ("Target yamaze kuboneka; nta cyemezo cyasubijwe inyuma."
                                  if target_already_completed else
                                  "Target ntiyashoboye kugenzurwa; icyemezo kirahagaritswe.")
                        status = ("TARGET_ALREADY_COMPLETED" if target_already_completed
                                  else "UNAVAILABLE_TARGET_IDENTITY")
                        for proposal in evaluation_rows:
                            if proposal.get("eligible") is not True:
                                continue
                            if target_already_completed:
                                late_true_rule_ids.add(str(proposal.get("rule_id") or ""))
                            proposal.update({"eligible": None, "result": None,
                                             "status": status, "reason": reason})
                            proposal.setdefault("proposal", {}).update({
                                "eligible": None, "status": status, "reason": reason,
                            })
                        for proposal in automatic_baseline_proposals:
                            if proposal.get("eligible") is not True:
                                continue
                            proposal.update({"eligible": None, "result": None,
                                             "status": status, "reason": reason})
                            proposal.setdefault("proposal", {}).update({
                                "eligible": None, "status": status, "reason": reason,
                            })
                if target_already_completed and late_true_rule_ids:
                    for proposal in evaluation_rows:
                        if (proposal.get("status") != "TARGET_ALREADY_COMPLETED"
                                or str(proposal.get("rule_id") or "") not in late_true_rule_ids):
                            continue
                        if late_target_row is not None:
                            resolution = self._apply_late_target_to_rule_state(
                                session, proposal, late_target_row, at)
                        else:
                            resolution = None
                        if resolution is None:
                            resolution = {"status": "WAITING_FOR_VERIFIED_TARGET_RESULT",
                                          "target_round_index": target_index,
                                          "actual_bet_placed": False}
                            session.setdefault("late_target_transitions", []).append({
                                "source_round": _reference(row),
                                "target_stream_id": row["stream_id"],
                                "target_round_index": target_index,
                                "rule_id": proposal["rule_id"],
                                "queued_at": at,
                                "proposal": deepcopy(proposal),
                            })
                            proposal["late_target_resolution"] = deepcopy(resolution)
                            proposal.setdefault("proposal", {}).setdefault("relevantInputs", {})[
                                "lateTargetResolution"] = deepcopy(resolution)
                        dirty_session_ids.add(str(session["session_id"]))
                if (session.get("adaptive_rule_selection") is True
                        and automatic_baseline_session is not None
                        and automatic_baseline_choice is not None):
                    self._record_adaptive_automatic_baseline(
                        session, automatic_baseline_session, automatic_baseline_choice,
                        automatic_baseline_proposals, row, at)
                aggregation_started_ns = time.perf_counter_ns()
                if active and session.get("automatic_rule_selection") is True and not active_rule_ids:
                    combined = {"action": "NO_ACTION",
                                "reason": session["automatic_rule_choice"]["reason"],
                                "true_rule_ids": [], "selection_id": None}
                elif active and session.get("adaptive_rule_selection") is True:
                    combined = self._separate(session, row, proposals, at)
                elif active and session["mode"] in {"FIVE_RULE_TRACKER", "ALL_RULES_TOGETHER"}:
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
                decision_aggregation_ms += (time.perf_counter_ns() - aggregation_started_ns) / 1_000_000.0
                if target_guard_unavailable:
                    combined = {"action": "WAIT", "reason": "TARGET_IDENTITY_CHECK_UNAVAILABLE",
                                "true_rule_ids": [], "selection_id": None}
                accepted_rule_ids = (set(combined.get("true_rule_ids") or [])
                                     if combined.get("action") == "SELECT" else set())
                for proposal in evaluation_rows:
                    rule_id = proposal.get("rule_id")
                    if rule_id not in accepted_rule_ids:
                        continue
                    accepted_state = deepcopy(session.get("engines", {}).get(rule_id) or {})
                    proposal.setdefault("state_snapshot", {})["after"] = deepcopy(accepted_state)
                    proposal.setdefault("proposal", {}).setdefault("privateState", {})["after"] = (
                        deepcopy(accepted_state))
                    inputs = proposal.setdefault("proposal", {}).setdefault("relevantInputs", {})
                    cycle = accepted_state.get("cycle") or {}
                    if inputs.get("ruleCycle") is not None:
                        inputs["ruleCycle"].update({
                            "cycle_id": cycle.get("cycle_id"),
                            "active": bool(cycle.get("active")),
                            "requested_targets": int(cycle.get("requested_count") or 0),
                            "resolved_targets": int(cycle.get("resolved_count") or 0),
                            "maximum_targets": cycle.get("maximum_targets"),
                            "end_condition": cycle.get("end_condition"),
                        })
                if (session.get("window_size") is not None
                        and session["observed_rounds"] >= session["window_size"]):
                    session["status"] = "COMPLETED"
                    session["ended_at"] = at
                    self._record_session_stop(session, "WINDOW_COMPLETED", at,
                        "WINDOW_COMPLETION", row)
                    if session["selection_status"] == "ACTIVE":
                        self._stop_selecting(session, "WINDOW_COMPLETED", at, row,
                                             stop_source="WINDOW_COMPLETION")
                    if not session.get("auto_next_session_id") and not session.get("auto_next_armed"):
                        session["auto_next_armed"] = self._auto_next_window_trigger_enabled(session)
                        if session["auto_next_armed"]:
                            session.update({"auto_next_trigger": "WINDOW_COMPLETED",
                                           "auto_next_armed_at": at,
                                           "auto_next_after_round_id": row.get("round_id"),
                                           "auto_next_boundary": None,
                                           "auto_next_context": deepcopy(session.get("context") or []),
                                           "auto_next_block_reason": None})
                            if committed_stage:
                                self._record_auto_next_boundary(session, row)
                if row.get("round_id") == auto_next_start_round:
                    session["auto_next_start_round_id"] = None
                if auto_next_first_contiguous_round:
                    session["auto_next_start_round_id"] = None
                    session["auto_next_accept_first_round"] = False
                session.update({"last_stream_sequence": row["stream_sequence"] if not observed_stage else session["last_stream_sequence"],
                                "last_round_index": row["round_index"], "last_round_id": row["round_id"]})
                self._sync_automatic_candidate_lifecycle(session)
                dirty_session_ids.add(str(session["session_id"]))
                record = {"session_id": session["session_id"],
                          "source_round": _reference(row), "target_round": _expected(row),
                          "rule_evaluations": evaluation_rows, "decision": combined, "evaluated_at": at,
                          "automatic_rule_choice": deepcopy(session.get("automatic_rule_choice"))
                              if session.get("automatic_rule_selection") is True else None,
                          "adaptive_rule_choice": deepcopy(session.get("adaptive_rule_choice"))
                              if session.get("adaptive_rule_selection") is True else None,
                          "adaptive_automatic_baseline_choice": deepcopy(
                              session.get("adaptive_automatic_baseline_choice"))
                              if session.get("adaptive_rule_selection") is True else None,
                          "persistence_status": "PENDING" if observed_stage else "COMMITTED",
                          "target_identity_guard": target_identity_guard,
                          "commit_evidence": {
                              "event_type": event.get("event_type"),
                              "durability": event.get("durability"),
                              "continuity_status": event.get("continuity_status"),
                              "delivery_mode": str(event.get("delivery_mode") or "LIVE").upper(),
                              "stream_id": row["stream_id"],
                              "stream_sequence": row["stream_sequence"],
                          }}
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
            if committed_stage:
                # A full-window trigger has an exact committed boundary. Start
                # its successor here so users do not have to manually start it
                # while waiting for the next round event to arrive.
                dirty_session_ids.update(self._spawn_auto_next_sessions(
                    row, at, start_window_trigger_at_boundary=True))
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
                      "sessions": results,
                      "evaluated_at": at,
                      "target_settlements": [item for rows in settlements_by_session.values()
                                             for item in rows],
                      "event_timings_ms": {
                          "target_settlement_processing_ms": (
                              round(settlement_processing_ms, 3) if settlements_by_session else None),
                          "target_settlement_durable_ms": None,
                      }}
            self.last_evaluation = output
            self.last_evaluation_at = at
            pre_persist_processing_ms = (time.perf_counter_ns() - processing_started_ns) / 1_000_000.0
            for session in self.sessions.values():
                if str(session.get("session_id")) not in dirty_session_ids:
                    continue
                received = session.get("last_received_round") or {}
                if (received.get("stream_id") == row["stream_id"]
                        and received.get("round_id") == row["round_id"]):
                    session["last_event_processing_latency_ms"] = round(pre_persist_processing_ms, 3)
                    if session["session_id"] in settlements_by_session:
                        session["last_target_settlement_processing_latency_ms"] = round(
                            settlement_processing_ms, 3)
            for result in results:
                result["timings_ms"] = {
                    "target_settlement_processing_ms": (
                        round(settlement_processing_ms, 3)
                        if result.get("session_id") in settlements_by_session else None),
                    "rules_engine_processing_ms": round(pre_persist_processing_ms, 3),
                }
            for session_id in dirty_session_ids:
                session = self.sessions.get(str(session_id))
                if session is not None:
                    self._trim_committed_event_records(session, keep=100)
            if defer_persistence:
                # The caller's durable observation inbox is the recovery
                # record. A separate ordered writer checkpoints state after
                # urgent signal dispatch, without holding this worker or the
                # rules lock on PostgreSQL latency.
                persistence_timings = {
                    "snapshot_serialization_ms": None,
                    "database_persistence_ms": None,
                    "persistence_total_ms": None,
                    "persistence_deferred": True,
                }
                self._persistence_generation += 1
                output["_deferred_persistence_session_ids"] = sorted(dirty_session_ids)
            else:
                persistence_timings = self._persist(session_ids=dirty_session_ids) or persistence_timings
            total_processing_ms = (time.perf_counter_ns() - processing_started_ns) / 1_000_000.0
            durable_settlement_ms = ((time.perf_counter_ns() - settlement_started_ns) / 1_000_000.0
                                     if settlements_by_session else None)
            output["event_timings_ms"]["rules_engine_processing_ms"] = round(total_processing_ms, 3)
            output["event_timings_ms"].update({
                "rules_engine_lock_wait_ms": round(lock_wait_ms, 3),
                "user_playing_policy_evaluation_ms": round(policy_evaluation_ms, 3),
                "standard_rule_evaluation_ms": round(standard_rule_evaluation_ms, 3),
                "selected_result_evaluation_ms": round(selected_result_evaluation_ms, 3),
                "two_bet_wins_transition_ms": round(two_bet_wins_transition_ms, 3),
                "target_identity_check_ms": round(target_identity_check_ms, 3),
                "decision_aggregation_ms": round(decision_aggregation_ms, 3),
                **persistence_timings,
            })
            if durable_settlement_ms is not None:
                output["event_timings_ms"]["target_settlement_durable_ms"] = round(durable_settlement_ms, 3)
            event_metric_id = (str(event.get("observation_id") or event.get("round_id") or "")
                               + ":" + str(event.get("event_type") or "unknown"))
            if event_metric_id:
                self._record_delivery_metric("rules_engine_processing_ms", event_metric_id,
                                             total_processing_ms)
                for stage in ("rules_engine_lock_wait_ms", "user_playing_policy_evaluation_ms",
                              "standard_rule_evaluation_ms", "selected_result_evaluation_ms",
                              "two_bet_wins_transition_ms", "target_identity_check_ms",
                              "decision_aggregation_ms", "snapshot_serialization_ms",
                              "database_persistence_ms"):
                    if defer_persistence and stage in {
                            "snapshot_serialization_ms", "database_persistence_ms"}:
                        continue
                    self._record_delivery_metric(
                        stage, event_metric_id,
                        float(output["event_timings_ms"].get(stage) or 0.0))
                if settlements_by_session:
                    self._record_delivery_metric("target_settlement_processing_ms", event_metric_id,
                                                 settlement_processing_ms)
                    self._record_delivery_metric("target_settlement_durable_ms", event_metric_id,
                                                 durable_settlement_ms or 0.0)
            return deepcopy(output)

    def has_round_event_consumers(self) -> bool:
        """Return whether a persisted session can consume or settle a round.

        The report-only committed-round observer is coordinated separately by
        the collector worker, so this predicate still describes SHADOW-session
        consumers and preserves the no-session fast path for observed events.
        """
        with self._lock:
            shadow_consumers = any(
                self._session_accepts_source_events(session)
                or self._session_accepts_settlement_events(session)
                for session in self.sessions.values()
            )
            return shadow_consumers

    def _spawn_auto_next_sessions(self, current: dict[str, Any], at: str, *,
                                  start_window_trigger_at_boundary: bool = False) -> set[str]:
        changed_session_ids: set[str] = set()
        for previous in list(self.sessions.values()):
            if not previous.get("auto_next_armed"):
                continue
            trigger = previous.get("auto_next_trigger")
            completed_window = previous.get("status") == "COMPLETED" and (
                trigger in {None, "WINDOW_COMPLETED"}
                and self._auto_next_window_trigger_enabled(previous))
            stopped_at_limit = (previous.get("status") in {"SETTLING", "COMPLETED"}
                and trigger == "LIMIT_REACHED"
                and previous.get("configuration_snapshot", {}).get("auto_next_on_limit_enabled") is True
                and previous.get("selection_stop_source") in {
                    "PROFIT_TARGET", "NET_OUTCOME_LIMIT", "MAX_SELECTIONS", "AUTO_NEXT_RESULT_LIMIT"}
                and previous.get("selection_stop_reason") in {
                    "PROFIT_TARGET_REACHED", "MAX_SELECTIONS_REACHED",
                    "AUTO_NEXT_RESULT_LIMIT_REACHED",
                    *NET_OUTCOME_LIMIT_REASONS})
            if not (completed_window or stopped_at_limit):
                continue
            active_peer = any(item is not previous
                              and item.get("mode") == previous.get("mode")
                              and item.get("last_stream_id") == current["stream_id"]
                              and item.get("status") in {"RUNNING", "SETTLING", "PAUSED"}
                              and item.get("selection_status") == "ACTIVE"
                              for item in self.sessions.values())
            if active_peer:
                previous["auto_next_armed"] = False
                previous["auto_next_armed_at"] = None
                previous["auto_next_block_reason"] = "SESSION_ALREADY_ACTIVE_FOR_MODE"
                changed_session_ids.add(str(previous["session_id"]))
                continue
            # Capture the exact committed event that completed the old window.
            if current.get("round_id") == previous.get("auto_next_after_round_id"):
                if not (start_window_trigger_at_boundary and completed_window):
                    self._record_auto_next_boundary(previous, current)
                    if isinstance(previous.get("auto_next_boundary"), Mapping):
                        changed_session_ids.add(str(previous["session_id"]))
                    continue
            boundary = previous.get("auto_next_boundary")
            context = previous.get("auto_next_context")
            same_window_completion_boundary = (
                start_window_trigger_at_boundary
                and completed_window
                and current.get("round_id") == previous.get("auto_next_after_round_id"))
            valid_boundary = (isinstance(boundary, Mapping)
                and boundary.get("event_type") == "round.persisted"
                and boundary.get("durability") == "COMMITTED"
                and boundary.get("continuity_status") == "VERIFIED"
                and int(boundary.get("stream_sequence") or 0) > 0
                and boundary.get("round_id") == previous.get("auto_next_after_round_id"))
            if not valid_boundary:
                # If the saved completion boundary was lost, rebase the
                # successor on this later verified commit. Its history starts
                # with this round, so missing older context cannot block it.
                try:
                    current_index = int(current.get("round_index"))
                    previous_index = int(previous.get("last_round_index"))
                    current_sequence = int(current.get("stream_sequence"))
                except (TypeError, ValueError, OverflowError):
                    current_index = previous_index = current_sequence = 0
                can_rebase = (current.get("event_type") == "round.persisted"
                    and current.get("durability") == "COMMITTED"
                    and current.get("continuity_status") == "VERIFIED"
                    and current.get("stream_id") == previous.get("last_stream_id")
                    and current_index > previous_index and current_sequence > 0)
                if can_rebase:
                    previous["auto_next_boundary"] = None
                    previous["auto_next_context"] = [deepcopy(current)]
                    self._record_auto_next_boundary(previous, current)
                    previous["auto_next_block_reason"] = None
                    changed_session_ids.add(str(previous["session_id"]))
                    continue
                previous["auto_next_block_reason"] = "AUTO_NEXT_COMMITTED_BOUNDARY_UNAVAILABLE"
                changed_session_ids.add(str(previous["session_id"]))
                continue
            if (current["stream_id"] != boundary.get("stream_id")
                    or (not same_window_completion_boundary
                        and (current["round_index"] <= boundary.get("round_index", 0)
                             or current["stream_sequence"] <= boundary.get("stream_sequence", 0)))):
                continue
            if (not isinstance(context, list) or not context
                    or context[-1].get("stream_id") != boundary.get("stream_id")
                    or context[-1].get("round_id") != boundary.get("round_id")):
                previous["auto_next_armed"] = False
                previous["auto_next_armed_at"] = None
                previous["auto_next_block_reason"] = "AUTO_NEXT_CONTEXT_UNAVAILABLE"
                changed_session_ids.add(str(previous["session_id"]))
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
                "rule_configuration_keys": deepcopy(previous.get("rule_configuration_keys") or {}),
                "automatic_rule_selection": previous.get("automatic_rule_selection", False),
                "adaptive_rule_selection": previous.get("adaptive_rule_selection", False),
                "active_rule_ids": [], "automatic_rule_choice": None,
                "adaptive_rule_choice": None, "adaptive_decisions": [],
                "adaptive_selection_version": int(previous.get("adaptive_selection_version") or 0),
                "adaptive_automatic_baseline_choice": None,
                "adaptive_automatic_baseline_decisions": [],
                "adaptive_automatic_baseline_selections": [],
                "adaptive_automatic_baseline_engines": ({rid: _fresh_engine(rid) for rid in rule_ids}
                    if previous.get("adaptive_rule_selection") is True else {}),
                "manifest": deepcopy(previous["manifest"]),
                "start_boundary": _reference(boundary) | {"streamSequence": boundary["stream_sequence"]},
                "last_stream_id": boundary["stream_id"], "last_stream_sequence": boundary["stream_sequence"],
                "last_round_index": boundary["round_index"], "last_round_id": boundary["round_id"],
                "context": deepcopy(context[-5:]),
                "engines": {rid: _fresh_engine(rid) for rid in rule_ids}, "observed_rounds": 0,
                "prestart_skipped_rounds": 0,
                "selections": [], "rule_selections": {rid: [] for rid in rule_ids},
                "adaptive_signal_selections": [],
                "evaluations": [], "decisions": [], "total_wins": 0, "total_losses": 0,
                "total_cancelled": 0,
                "net_profit_rwf": 0, "selection_stop_reason": None, "stop_reason": None,
                "auto_next_armed": False, "auto_next_trigger": None,
                "auto_next_armed_at": None, "auto_next_block_reason": None,
                "auto_next_boundary": None, "auto_next_context": [],
                "auto_next_start_round_id": current["round_id"],
                "auto_next_accept_first_round": same_window_completion_boundary,
                }
            previous["auto_next_armed"] = False
            previous["auto_next_started_at"] = at
            previous["auto_next_session_id"] = session_id
            self.sessions[session_id] = clone
            changed_session_ids.update((str(previous["session_id"]), session_id))
        return changed_session_ids

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
            self._stop_selecting(session, "MAX_SELECTIONS_REACHED", at, source,
                                 stop_source="MAX_SELECTIONS")
            return {"action": "WAIT", "reason": "MAX_SELECTIONS_REACHED", "true_rule_ids": [], "selection_id": None}
        selected_rule_ids = [proposal["rule_id"] for proposal in true_rows]
        selection_id = _stable_selection_id(session, source, selected_rule_ids)
        contributors = [{"rule_id": proposal["rule_id"], "rule_version": proposal["rule_version"],
                         "reason": proposal["reason"], "proposal": deepcopy(proposal["proposal"])}
                        for proposal in true_rows]
        selection = {"selection_id": selection_id, "session_id": session["session_id"],
                     "decision_id": selection_id,
                     "source_round": _reference(source), "expected_target": _expected(source),
                     "source_round_id": source["round_id"], "target_round_id": None,
                     "contributing_rule_ids": [row["rule_id"] for row in true_rows],
                     "matched_rule_ids": [row["rule_id"] for row in true_rows],
                     "contributors": contributors, "selected_at": at, "status": "PENDING", "result": "PENDING",
                     "settlement_status": "PENDING",
                     "actual_multiplier": None, "actual_target": None, "resolved_at": None,
                     "settled_at": None, "settlement_evidence": None,
                     "stake_rwf": config.get("fixed_stake_rwf", 100),
                     "payout_multiplier": 2.0, "payout_rwf": None, "profit_loss_rwf": None}
        session["selections"].append(selection)
        for proposal in true_rows:
            self._commit_contributor(session, proposal, selection)
        self._trim_performance_observer_ledger(session)
        max_selections = config.get("max_selections")
        if (session.get("mode") == "ALL_RULES_TOGETHER"
                and isinstance(max_selections, int) and not isinstance(max_selections, bool)
                and len(session["selections"]) >= max_selections):
            self._stop_selecting(session, "MAX_SELECTIONS_REACHED", at, source,
                                 stop_source="MAX_SELECTIONS")
            self._arm_auto_next_after_limit(
                session, session.get("selection_stop_reason"), at, source)
        return {"action": "SELECT", "reason": "ONE_OR_MORE_RULES_TRUE", "selection_id": selection_id,
                "true_rule_ids": list(selection["contributing_rule_ids"]),
                "target_round": deepcopy(selection["expected_target"]),
                "contributor_reasons": [{"rule_id": row["rule_id"], "reason": row["reason"]} for row in true_rows]}

    def _separate(self, session: dict[str, Any], source: dict[str, Any],
                  proposals: list[dict[str, Any]], at: str) -> dict[str, Any]:
        true_rows = [row for row in proposals if row.get("eligible") is True]
        selected: list[str] = []
        selection_ids: list[str] = []
        config = session.get("configuration_snapshot") or {}
        maximum = config.get("max_selections") if session.get("adaptive_rule_selection") is True else None
        if (session.get("mode") == "ALL_RULES_TOGETHER"
                and isinstance(maximum, int) and self._adaptive_session_result_counts(session)[3] >= maximum):
            self._stop_selecting(session, "MAX_SELECTIONS_REACHED", at, source,
                                 stop_source="MAX_SELECTIONS")
            return {"action": "WAIT", "reason": "MAX_SELECTIONS_REACHED",
                    "true_rule_ids": [], "selection_ids": []}
        expected_source, expected_target = _reference(source), _expected(source)
        if any(row.get("proposal", {}).get("sourceRoundRef") != expected_source
               or row.get("proposal", {}).get("expectedTargetRef") != expected_target for row in true_rows):
            return {"action": "BLOCKED", "reason": "TARGET_MISMATCH", "true_rule_ids": [], "selection_id": None}
        for proposal in true_rows:
            rid = proposal["rule_id"]
            selection_id = _stable_selection_id(session, source, [rid])
            selection = {"selection_id": selection_id, "session_id": session["session_id"],
                         "rule_id": rid, "rule_version": proposal["rule_version"],
                         "source_round": _reference(source), "expected_target": _expected(source),
                         "reason": proposal["reason"], "selected_at": at,
                         "adaptive_selection_version": proposal.get("adaptive_selection_version"),
                         "behavior_context": proposal.get("behavior_context"),
                         "status": "PENDING", "result": "PENDING", "actual_multiplier": None,
                         "actual_target": None, "resolved_at": None}
            session["rule_selections"][rid].append(selection)
            self._commit_contributor(session, proposal, selection)
            selected.append(rid)
            selection_ids.append(selection_id)
        signal_selection_id = None
        if selected:
            # The signal contract stores one publication per session/target.
            # Keep the independent rule ledgers above, and add a separate
            # target-level envelope so the signal bridge can publish all TRUE
            # Adaptive contributors as one signal without collapsing outcomes.
            signal_selection_id = str(uuid5(NAMESPACE_URL,
                f"adaptive-signal-envelope:{session['session_id']}:"
                f"{source['stream_id']}:{source['round_id']}:"
                f"{_expected(source).get('roundIndex')}:{','.join(selected)}"))
            envelope = {"selection_id": signal_selection_id,
                "decision_id": signal_selection_id, "session_id": session["session_id"],
                "source_round": _reference(source), "expected_target": _expected(source),
                "contributing_rule_ids": list(selected), "matched_rule_ids": list(selected),
                "selected_at": at, "adaptive_selection_version": session.get("adaptive_selection_version"),
                "status": "PENDING", "result": "PENDING", "signal_envelope": True}
            session.setdefault("adaptive_signal_selections", []).append(envelope)
            session["adaptive_signal_selections"] = session["adaptive_signal_selections"][-500:]
        self._trim_performance_observer_ledger(session)
        if (maximum is not None and selected
                and self._adaptive_session_result_counts(session)[3] >= maximum):
            self._stop_selecting(session, "MAX_SELECTIONS_REACHED", at, source,
                                 stop_source="MAX_SELECTIONS")
            self._arm_auto_next_after_limit(session, session.get("selection_stop_reason"), at, source)
        return {"action": "SELECT" if selected else ("WAIT" if session.get("rule_ids") else "NO_ACTION"),
                "reason": "INDEPENDENT_TRACKER_SELECTIONS" if selected else "NO_TRUE_PROPOSALS",
                "selection_id": signal_selection_id,
                "true_rule_ids": selected, "selection_ids": selection_ids}

    def _start_check_summary(self, session: dict[str, Any]) -> dict[str, Any]:
        rid = START_CHECK_POLICY_ID
        state = session.get("engines", {}).get(rid, {})
        return {"state": state.get("rule_state", "WAIT_LOW"), "mode": state.get("mode", "STANDARD"),
                "failed_cycles": state.get("failed_cycle_count", 0),
                "selected_wins": state.get("selected_wins", 0), "selected_losses": state.get("selected_losses", 0)}

    def session_action(self, session_id: str, action: str, *, now: str | None = None) -> dict[str, Any]:
        at = _now(now)
        if action not in {"pause", "resume", "stop", "reset"}:
            raise ValueError("INVALID_SESSION_ACTION")
        with self._lock:
            session = self.sessions.get(session_id)
            if not session:
                raise KeyError("SESSION_NOT_FOUND")
            already_applied = (
                (action == "pause" and session.get("status") == "PAUSED"
                 and session.get("stop_reason") == "MANUAL_PAUSE")
                or (action == "resume" and session.get("status") == "RUNNING")
                or (action == "stop" and session.get("status") == "MANUALLY_STOPPED"
                    and session.get("stop_reason") == "MANUAL_STOP")
                or (action == "reset" and session.get("status") == "RESET"
                    and session.get("stop_reason") == "RESET")
            )
            if already_applied:
                return self._session_summary(session)

            # Auto-next creates a linked chain of sessions. A manual stop on
            # its current member must also disarm ancestors and stop any
            # already-created successor, otherwise the next round can start
            # another session while the operator is repeatedly clicking Stop.
            lineage = {str(session_id)}
            changed = True
            while changed:
                changed = False
                for candidate_id, candidate in self.sessions.items():
                    parent_id = str(candidate.get("auto_next_session_id") or "")
                    if (str(candidate_id) in lineage and parent_id
                            and parent_id in self.sessions and parent_id not in lineage):
                        lineage.add(parent_id)
                        changed = True
                    if (parent_id in lineage and str(candidate_id) not in lineage):
                        lineage.add(str(candidate_id))
                        changed = True

            affected_ids: set[str] = set()
            for lineage_id in lineage:
                member = self.sessions.get(lineage_id)
                if not member:
                    continue
                if member.get("auto_next_armed"):
                    member.update({"auto_next_armed": False, "auto_next_trigger": None,
                                   "auto_next_armed_at": None,
                                   "auto_next_block_reason": "OPERATOR_SESSION_CONTROL"})
                    affected_ids.add(lineage_id)
                # Only terminal actions propagate through a successor chain;
                # Pause preserves normal pending-result observation behavior.
                if (action in {"stop", "reset"}
                        and member.get("status") in {"RUNNING", "SETTLING", "PAUSED"}):
                    propagated_action = action
                    reason = "RESET" if propagated_action == "reset" else "MANUAL_STOP"
                    prior_integrity_reason = (member.get("integrity_stop_reason")
                        or member.get("selection_stop_reason") or member.get("stop_reason"))
                    if prior_integrity_reason and prior_integrity_reason not in {"MANUAL_PAUSE", reason}:
                        member["integrity_stop_reason"] = prior_integrity_reason
                    member.update({"status": "RESET" if propagated_action == "reset"
                                   else "MANUALLY_STOPPED", "ended_at": at,
                                   "auto_next_armed": False, "auto_next_trigger": None,
                                   "auto_next_armed_at": None})
                    self._record_session_stop(member, reason, at,
                        "MANUAL_RESET_CONTROL" if propagated_action == "reset" else
                        "MANUAL_STOP_CONTROL", stop_selection=True)
                    affected_ids.add(lineage_id)

            if action == "pause" and session["status"] == "RUNNING":
                session.update({"status": "PAUSED", "paused_at": at})
                self._record_session_stop(session, "MANUAL_PAUSE", at, "MANUAL_OPERATOR")
            elif action == "resume" and session["status"] == "PAUSED" and session.get("stop_reason") == "MANUAL_PAUSE":
                session.update({"status": "RUNNING", "stop_reason": None, "resumed_at": at})
            elif action == "stop":
                if session_id not in affected_ids:
                    reason = "MANUAL_STOP"
                    prior_reason = session.get("selection_stop_reason") or session.get("stop_reason")
                    if prior_reason and prior_reason not in {"MANUAL_PAUSE", reason}:
                        session["integrity_stop_reason"] = prior_reason
                    session.update({"status": "MANUALLY_STOPPED", "ended_at": at,
                                    "auto_next_armed": False, "auto_next_trigger": None,
                                    "auto_next_armed_at": None})
                    self._record_session_stop(session, reason, at, "MANUAL_STOP_CONTROL",
                                              stop_selection=True)
            elif action == "reset":
                if session_id not in affected_ids:
                    session.update({"status": "RESET", "ended_at": at,
                                    "auto_next_armed": False, "auto_next_trigger": None,
                                    "auto_next_armed_at": None})
                    self._record_session_stop(session, "RESET", at, "MANUAL_RESET_CONTROL",
                                              stop_selection=True)
            else:
                raise ValueError("INVALID_SESSION_TRANSITION")
            self._persist(session_ids=affected_ids or {str(session_id)})
            # Controls only need the compact authoritative summary. Returning
            # hundreds of evaluations and event records made a simple action
            # response unnecessarily large and delayed the button feedback.
            return self._session_summary(session)

    def close_for_reconfiguration(self, session_id: str, new_session_id: str,
                                  confirmation: str, *, now: str | None = None) -> dict[str, Any]:
        """Archive the old configuration after an explicit operator-confirmed replacement."""
        if confirmation != f"RECONFIGURE SESSION {session_id}":
            raise ValueError("OPERATOR_CONFIRMATION_REQUIRED")
        at = _now(now)
        with self._lock:
            session = self.sessions.get(session_id)
            if session is None:
                raise KeyError("SESSION_NOT_FOUND")
            if session.get("status") not in {"RUNNING", "SETTLING", "PAUSED"}:
                raise ValueError("RULES_SESSION_NOT_ACTIVE")
            session.update({"status": "RECONFIGURED", "ended_at": at,
                            "auto_next_armed": False, "auto_next_trigger": None,
                            "auto_next_armed_at": None,
                            "reconfigured_to_session_id": str(new_session_id)})
            self._record_session_stop(session, "RECONFIGURED", at,
                "OPERATOR_CONFIRMED_RECONFIGURATION", stop_selection=True,
                details={"new_session_id": str(new_session_id),
                         "operator_confirmation": True,
                         "confirmed_at": at})
            self._persist(session_ids={str(session_id)})
            return deepcopy(session)

    def session(self, session_id: str) -> dict[str, Any] | None:
        # Session snapshots are read by HTTP and collector callback threads
        # while observe_round mutates the same ledgers. Copy under the engine
        # lock so a concurrent update cannot produce a torn view or make
        # deepcopy fail with "dictionary changed size during iteration".
        with self._lock:
            value = self.sessions.get(session_id)
            if not value:
                return None
            result = deepcopy(value)
            summary = self._session_summary(result)
            behaviour_rows = [item for item in result.get("selections", [])
                              if isinstance(item, dict)
                              and item.get("source_id") == "BEHAVIOUR_DETECTION"]
            behaviour_wins = sum(item.get("status") == "WIN" for item in behaviour_rows)
            behaviour_losses = sum(item.get("status") == "LOSS" for item in behaviour_rows)
            behaviour_pending = sum(item.get("status") == "PENDING" for item in behaviour_rows)
            behaviour_net = round(sum(float(item.get("profit_loss_rwf") or 0)
                                      for item in behaviour_rows), 2)
            result["source_performance"] = {
                "behaviour_detection": {"source_label": "Behaviour Detection / Rolling HIGH",
                    "selections": len(behaviour_rows), "wins": behaviour_wins,
                    "losses": behaviour_losses, "pending": behaviour_pending,
                    "net_profit_rwf": behaviour_net},
                "other_shadow": {"source_label": "Other SHADOW strategies",
                    "selections": max(0, int(summary["selection_count"]) - len(behaviour_rows)),
                    "wins": max(0, int(summary["results"]["WIN"]) - behaviour_wins),
                    "losses": max(0, int(summary["results"]["LOSS"]) - behaviour_losses),
                    "pending": max(0, int(summary["results"]["PENDING"]) - behaviour_pending),
                    "net_profit_rwf": round(float(result.get("net_profit_rwf") or 0) - behaviour_net, 2)},
            }
            result.update({"total_wins": summary["results"]["WIN"],
                           "total_losses": summary["results"]["LOSS"],
                           "status": summary["status"],
                           "net_outcome": summary["net_outcome"],
                           "results": deepcopy(summary["results"]),
                           "selection_count": summary["selection_count"],
                           "stop_reason": summary["stop_reason"],
                           "stop_reason_source": summary["stop_reason_source"],
                           "stop_reason_at": summary["stop_reason_at"],
                           "stop_reason_round_id": summary["stop_reason_round_id"],
                           "stop_reason_details": deepcopy(summary["stop_reason_details"]),
                           "stop_history": deepcopy(summary["stop_history"])})
            return result

    def record_strategy_eligibility(self, session_id: str, eligibility: Mapping[str, Any],
                                    source: Mapping[str, Any], *,
                                    persist: bool = True,
                                    now: str | None = None) -> dict[str, Any]:
        """Record the adapter's current eligibility without creating a signal."""
        at = _now(now)
        with self._lock:
            session = self.sessions.get(session_id)
            if (session is None or session.get("status") != "RUNNING"
                    or session.get("selection_status") != "ACTIVE"
                    or (session.get("strategy_id") or
                        ("BEHAVIOUR_ROLLING_HIGH" if session.get("behaviour_detection_enabled") else "RULES"))
                        != "BEHAVIOUR_ROLLING_HIGH"):
                raise ValueError("BEHAVIOUR_STRATEGY_NOT_ACTIVE")
            result = {
                "strategy_id": "BEHAVIOUR_ROLLING_HIGH",
                "series_id": eligibility.get("series_id"),
                "part": eligibility.get("part"), "batch_id": eligibility.get("batch_id"),
                "current_dominance_state": str(eligibility.get("current_dominance_state") or "UNKNOWN"),
                "eligible": eligibility.get("eligibility") == "ELIGIBLE",
                "reason": str(eligibility.get("eligibility_reason") or "UNKNOWN"),
                "source_round_identity": str(eligibility.get("decision_round_identity") or ""),
                "source_round_id": str(source.get("round_id") or ""),
                "policy_version": str(eligibility.get("policy_version") or ""),
                "configuration_version": int(eligibility.get("configuration_version") or 0),
                "data_freshness": deepcopy(eligibility.get("verified_data_freshness") or {}),
                "updated_at": at,
            }
            session["last_behaviour_eligibility"] = result
            # Eligibility is a live readout emitted once per round. Persisting a
            # full Rules Engine session snapshot synchronously here delays the
            # collector path, while Behaviour Detection already owns the durable
            # analytical state and retries this idempotent status update.
            if persist:
                self._persist(session_ids={session_id})
            return deepcopy(result)

    def register_strategy_selection(self, session_id: str, signal: Mapping[str, Any],
                                    source: Mapping[str, Any], *,
                                    persist: bool = True,
                                    now: str | None = None) -> dict[str, Any]:
        """Register an adapter-approved selection in the selected SHADOW session.

        The caller must validate the committed source and immediate future target
        against the authoritative round repository before invoking this method.
        The stable signal id makes retries idempotent across worker/backend restarts.
        """
        at = _now(now)
        signal_id = str(signal.get("signal_id") or "")
        target_index = int(signal.get("target_round_index") or 0)
        stream_id = str(signal.get("target_stream_id") or "")
        source_id = str(source.get("round_id") or "")
        source_index = int(source.get("round_index") or 0)
        if (not signal_id or not session_id or not stream_id or not source_id
                or source_index < 1 or target_index != source_index + 1
                or signal.get("direction") != "HIGH"):
            raise ValueError("INVALID_BEHAVIOUR_SHADOW_SELECTION")
        with self._lock:
            session = self.sessions.get(session_id)
            if (session is None or session.get("status") != "RUNNING"
                    or session.get("selection_status") != "ACTIVE"):
                raise ValueError("SHADOW_SESSION_NOT_ACTIVE")
            configuration = session.get("configuration_snapshot") or {}
            if (session.get("behaviour_detection_enabled",
                            configuration.get("behaviour_detection_enabled", False)) is not True):
                raise ValueError("BEHAVIOUR_SOURCE_DISABLED")
            selected_strategy = session.get("strategy_id") or (
                "BEHAVIOUR_ROLLING_HIGH" if session.get("behaviour_detection_enabled") else "RULES")
            if selected_strategy != "BEHAVIOUR_ROLLING_HIGH":
                raise ValueError("BEHAVIOUR_STRATEGY_NOT_SELECTED")
            existing = next((row for row in session.get("selections", [])
                             if str(row.get("selection_id") or "") == signal_id), None)
            if existing is not None:
                if (str(existing.get("source_id") or "") != "BEHAVIOUR_DETECTION"
                        or str(existing.get("source_round_id") or "") != source_id
                        or int((existing.get("expected_target") or {}).get("roundIndex") or 0)
                            != target_index):
                    raise ValueError("BEHAVIOUR_SIGNAL_IDENTITY_CONFLICT")
                return deepcopy(existing)
            for row in session.get("selections", []):
                expected = row.get("expected_target") or {}
                if (row.get("status") == "PENDING"
                        and expected.get("streamId") == stream_id
                        and int(expected.get("roundIndex") or 0) == target_index):
                    raise ValueError("SHADOW_TARGET_CONFLICT")
            selection = {
                "selection_id": signal_id, "decision_id": signal_id,
                "session_id": session_id, "source_id": "BEHAVIOUR_DETECTION",
                "source_label": "Behaviour Detection / Rolling HIGH",
                "strategy_id": str(signal.get("strategy_id") or "ROLLING_HIGH"),
                "series_id": str(signal.get("series_id") or ""),
                "part": str(signal.get("part") or ""),
                "batch_id": str(signal.get("batch_id") or ""),
                "eligibility_reason": str(signal.get("eligibility_reason") or ""),
                "current_dominance_state": str(signal.get("current_dominance_state") or "UNKNOWN"),
                "configuration_version": int(signal.get("configuration_version") or 0),
                "direction": "HIGH", "trigger_horizon": int(signal.get("trigger_horizon") or 5),
                "policy_version": str(signal.get("policy_version") or ""),
                "trigger_round_identity": str(signal.get("trigger_round_identity") or ""),
                "decision_round_identity": str(signal.get("decision_round_identity") or ""),
                "source_round": {"streamId": stream_id, "roundId": source_id,
                    "observationId": str(source.get("observation_id") or source_id),
                    "roundIndex": source_index, "multiplier": source.get("multiplier"),
                    "outcome": source.get("outcome")},
                "source_round_id": source_id,
                "expected_target": {"streamId": stream_id, "roundId": None,
                    "observationId": None, "roundIndex": target_index},
                "target_round_id": None, "contributing_rule_ids": [],
                "matched_rule_ids": [], "selected_at": at,
                "decision_timestamp": str(signal.get("decision_timestamp") or at),
                "status": "PENDING", "result": "PENDING", "settlement_status": "PENDING",
                "actual_multiplier": None, "actual_target": None, "resolved_at": None,
                "settled_at": None, "settlement_evidence": None,
                "stake_rwf": (session.get("configuration_snapshot") or {}).get("fixed_stake_rwf", 100),
                "payout_multiplier": 2.0, "payout_rwf": None, "profit_loss_rwf": None,
            }
            session.setdefault("selections", []).append(selection)
            session["last_behaviour_eligibility"] = {
                "strategy_id": selected_strategy, "series_id": selection["series_id"],
                "part": selection["part"], "batch_id": selection["batch_id"],
                "current_dominance_state": selection["current_dominance_state"],
                "eligible": True, "reason": selection["eligibility_reason"],
                "source_round_identity": selection["decision_round_identity"],
                "source_round_id": source_id, "policy_version": selection["policy_version"],
                "configuration_version": selection["configuration_version"],
                "data_freshness": deepcopy(signal.get("verified_data_freshness") or {}),
                "updated_at": at,
            }
            session["last_behaviour_selection"] = deepcopy(selection)
            if persist:
                self._persist(session_ids={session_id})
            return deepcopy(selection)

    def cancel_strategy_selection(self, session_id: str, selection_id: str,
                                  reason: str, *, now: str | None = None) -> None:
        at = _now(now)
        with self._lock:
            session = self.sessions.get(session_id)
            if session is None:
                return
            selection = next((row for row in session.get("selections", [])
                              if row.get("selection_id") == selection_id
                              and row.get("source_id") == "BEHAVIOUR_DETECTION"), None)
            if selection is None or selection.get("status") != "PENDING":
                return
            selection.update({"status": "CANCELLED", "result": "CANCELLED",
                              "settlement_status": "CANCELLED", "settlement_phase": "CANCELLED",
                              "cancellation_reason": str(reason), "cancelled_at": at})
            session["total_cancelled"] = int(session.get("total_cancelled") or 0) + 1
            self._persist(session_ids={session_id})

    def session_live_detail(self, session_id: str) -> dict[str, Any] | None:
        """Return the live dashboard fields without copying archived round history."""
        with self._lock:
            value = self.sessions.get(str(session_id))
            if not value:
                return None
            history_fields = {"evaluations", "decisions", "event_records",
                              "selections", "rule_selections", "adaptive_decisions",
                              "adaptive_signal_selections",
                              "adaptive_automatic_baseline_decisions",
                              "adaptive_automatic_baseline_selections"}
            result = {key: child for key, child in value.items()
                      if key not in history_fields}
            result["evaluations"] = list(value.get("evaluations") or [])[-12:]
            result["decisions"] = list(value.get("decisions") or [])[-12:]
            # The live panel renders a compact recent selection list. Full
            # finished-session history remains available from session().
            result["selections"] = list(value.get("selections") or [])[-100:]
            result["rule_selections"] = {
                str(rule_id): list(rows or [])[-100:]
                for rule_id, rows in (value.get("rule_selections") or {}).items()
            }
            result["event_records"] = {}
            summary = self._session_status_summary(value)
            result.update(summary)
            result["stop_history"] = list(value.get("stop_history") or [])[-50:]
            result = deepcopy(result)
            result["total_wins"] = summary["results"]["WIN"]
            result["total_losses"] = summary["results"]["LOSS"]
            return result

    def session_dashboard_context(self, session_id: str) -> dict[str, Any] | None:
        """Return the latest decision and summary needed by the live dashboard."""
        with self._lock:
            value = self.sessions.get(str(session_id))
            if not value:
                return None
            summary = self._session_status_summary(value)
            evaluations = value.get("evaluations") or []
            latest = deepcopy(evaluations[-1]) if evaluations else None
            return {**summary, "evaluations": [latest] if latest is not None else [],
                    "rule_ids": list(value.get("rule_ids") or [])}

    def signal_validation_session(self, session_id: str | None = None, *,
                                  allow_saved_selection: bool = False) -> dict[str, Any] | None:
        """Return only the active-session fields needed to validate a signal.

        The public status/detail methods build settlement summaries and deep
        copy the bounded history. Signal validation only needs session identity,
        status, and selected rule IDs, so it should not copy that history on the
        urgent decision path.
        """
        with self._lock:
            # The trusted bridge supplies the session ID from the just-produced
            # engine decision. Resolve that exact session directly instead of
            # sorting all sessions and, more importantly, do not call session()
            # which deep-copies the unbounded round/selection history.
            active = (self.sessions.get(str(session_id))
                      if allow_saved_selection and session_id is not None
                      else self._selected_session())
            if active is None:
                return None
            status = self._effective_session_status(active)
            selection_status = active.get("selection_status")
            is_running = status == "RUNNING" and selection_status == "ACTIVE"
            is_saved_settling = (allow_saved_selection and status == "SETTLING"
                                 and selection_status == "STOPPED")
            if not is_running and not is_saved_settling:
                return None
            active_id = str(active.get("session_id") or "")
            if session_id is not None and active_id != str(session_id):
                return None
            return {"session_id": active_id, "mode": active.get("mode"),
                    "status": status, "selection_status": selection_status,
                    "rule_ids": list(active.get("rule_ids") or []),
                    "selection_stop_reason": active.get("selection_stop_reason"),
                    "selection_stop_source": active.get("selection_stop_source"),
                    "selection_stopped_at_round_id": active.get("selection_stopped_at_round_id"),
                    "last_round_index": active.get("last_round_index")}

    def signal_selection_context(self, session_id: str, selection_id: str) -> dict[str, Any] | None:
        """Copy only the selected decision and identity fields for publication."""
        with self._lock:
            session = self.sessions.get(str(session_id))
            if session is None:
                return None
            selection = next((row for row in session.get("adaptive_signal_selections", [])
                              if isinstance(row, Mapping)
                              and str(row.get("selection_id") or row.get("decision_id") or "")
                                  == str(selection_id)), None)
            if selection is not None:
                return {"session_id": str(session.get("session_id") or ""),
                        "mode": session.get("mode"),
                        "status": self._effective_session_status(session),
                        "selection_status": session.get("selection_status"),
                        "selection_stop_reason": session.get("selection_stop_reason"),
                        "selection_stop_source": session.get("selection_stop_source"),
                        "selection_stopped_at_round_id": session.get("selection_stopped_at_round_id"),
                        "last_round_index": session.get("last_round_index"),
                        "rule_ids": list(session.get("rule_ids") or []),
                        "selection": deepcopy(selection)}
            selection = next((row for row in session.get("selections", [])
                              if isinstance(row, Mapping)
                              and str(row.get("selection_id") or row.get("decision_id") or "")
                                  == str(selection_id)), None)
            if selection is None:
                for rows in (session.get("rule_selections") or {}).values():
                    if not isinstance(rows, list):
                        continue
                    selection = next((row for row in rows if isinstance(row, Mapping)
                                      and str(row.get("selection_id") or row.get("decision_id") or "")
                                          == str(selection_id)), None)
                    if selection is not None:
                        break
            if selection is None:
                return None
            return {"session_id": str(session.get("session_id") or ""),
                    "mode": session.get("mode"),
                    "status": self._effective_session_status(session),
                    "selection_status": session.get("selection_status"),
                    "selection_stop_reason": session.get("selection_stop_reason"),
                    "selection_stop_source": session.get("selection_stop_source"),
                    "selection_stopped_at_round_id": session.get("selection_stopped_at_round_id"),
                    "last_round_index": session.get("last_round_index"),
                    "rule_ids": list(session.get("rule_ids") or []),
                    "selection": deepcopy(selection)}

    def delete_session(self, session_id: str, *, now: str | None = None) -> dict[str, Any]:
        """Delete a finished SHADOW session while retaining its system audit record."""
        at = _now(now)
        with self._lock:
            session = self.sessions.get(session_id)
            if not session:
                raise KeyError("SESSION_NOT_FOUND")
            if self._effective_session_status(session) not in {
                    "COMPLETED", "SETTLING", "PAUSED", "MANUALLY_STOPPED", "RESET", "RECONFIGURED"}:
                raise ValueError("SESSION_NOT_FINISHED")
            if self._has_pending_event_records(session):
                raise ValueError("SESSION_HAS_PENDING_EVENTS")
            if session.get("auto_next_armed"):
                raise ValueError("SESSION_AUTO_NEXT_ARMED")
            selections = list(session.get("selections") or [])
            selections.extend(item for rows in (session.get("rule_selections") or {}).values()
                              for item in rows)
            delete_persisted = getattr(self._persistence, "delete_rules_engine_session", None)
            if not callable(delete_persisted):
                raise ValueError("SESSION_DELETE_PERSISTENCE_UNAVAILABLE")
            deleted = deepcopy(session)
            summary = self._session_summary(session)
            snapshot = self._snapshot()
            del self.sessions[session_id]
            try:
                delete_persisted(session_id, snapshot, at)
            except Exception:
                self.sessions[session_id] = deleted
                raise
            return {"ok": True, "session_id": session_id, "deleted_at": at,
                    "summary": summary}

    def delete_sessions(self, session_ids: list[str], *, now: str | None = None) -> dict[str, Any]:
        """Remove a selected group of inactive sessions using one persistence transaction."""
        if not isinstance(session_ids, list):
            raise ValueError("INVALID_SESSION_IDS")
        unique_ids = list(dict.fromkeys(str(session_id) for session_id in session_ids))
        if not unique_ids or any(not session_id.strip() for session_id in unique_ids):
            raise ValueError("INVALID_SESSION_IDS")

        at = _now(now)
        removable_statuses = {
            "COMPLETED", "SETTLING", "PAUSED", "MANUALLY_STOPPED", "RESET", "RECONFIGURED",
        }
        with self._lock:
            eligible: dict[str, dict[str, Any]] = {}
            failures: list[dict[str, str]] = []
            for session_id in unique_ids:
                session = self.sessions.get(session_id)
                if session is None:
                    failures.append({"session_id": session_id, "reason": "SESSION_NOT_FOUND"})
                    continue
                if self._effective_session_status(session) not in removable_statuses:
                    failures.append({"session_id": session_id, "reason": "SESSION_NOT_FINISHED"})
                    continue
                if session.get("auto_next_armed"):
                    failures.append({"session_id": session_id, "reason": "SESSION_AUTO_NEXT_ARMED"})
                    continue
                eligible[session_id] = deepcopy(session)

            if not eligible:
                return {"ok": not failures, "deleted_session_ids": [], "failures": failures}
            delete_batch = getattr(self._persistence, "delete_rules_engine_sessions", None)
            if not callable(delete_batch):
                raise ValueError("SESSION_DELETE_PERSISTENCE_UNAVAILABLE")

            snapshot = self._snapshot()
            for session_id in eligible:
                self.sessions.pop(session_id, None)
            try:
                persisted = delete_batch(list(eligible), snapshot, at)
            except Exception:
                self.sessions.update(eligible)
                raise

            persisted_deleted = set(persisted.get("deleted_session_ids") or [])
            deleted_ids = [session_id for session_id in eligible if session_id in persisted_deleted]
            failures.extend(persisted.get("failures") or [])
            for session_id, session in eligible.items():
                if session_id not in persisted_deleted:
                    self.sessions[session_id] = session
            return {
                "ok": not failures,
                "deleted_session_ids": deleted_ids,
                "failures": failures,
            }

    def sessions_status(self) -> list[dict[str, Any]]:
        return [self._session_status_summary(item) for item in sorted(self.sessions.values(),
                key=lambda row: row.get("started_at", ""), reverse=True)]

    def _continuous_rule_performance(self, *, max_windows: int = 100) -> dict[str, Any]:
        cap = min(max(int(max_windows), 3), 250)
        with self._lock:
            registry_rows = deepcopy(self.registry().get("rules") or [])
            observers = {
                rule_id: {
                    "history": deepcopy(observer.get("history") or []),
                    "last_event": deepcopy(observer.get("last_event")),
                    "rounds_processed": int(observer.get("rounds_processed") or 0),
                    "manual_reset_at": observer.get("manual_reset_at"),
                    "manual_reset_round_index": observer.get("manual_reset_round_index"),
                    "sessions": [deepcopy(session) for session in observer["engine"].sessions.values()],
                }
                for rule_id, observer in self._performance_observers.items()
            }

        metadata = {str(row.get("rule_id")): {
            "rule_id": str(row.get("rule_id")), "name": row.get("name") or row.get("rule_id"),
            "configured": row.get("configured") is True, "enabled": row.get("enabled") is True,
        } for row in registry_rows if row.get("rule_id")}
        rows = []
        window_total = 0
        last_events = []
        processed_rounds = 0
        manual_resets = []
        for rule_id, info in metadata.items():
            observer = observers.get(rule_id) or {}
            windows = list(observer.get("history") or [])
            windows.extend(self._performance_window(session, rule_id)
                           for session in observer.get("sessions") or [])
            windows.sort(key=lambda item: (item.get("started_at") or "", item.get("session_id") or ""))
            windows = windows[-cap:]
            window_total += len(windows)
            processed_rounds = max(processed_rounds, int(observer.get("rounds_processed") or 0))
            if observer.get("manual_reset_at"):
                manual_resets.append({
                    "at": observer.get("manual_reset_at"),
                    "round_index": observer.get("manual_reset_round_index"),
                })
            last_event = observer.get("last_event")
            if isinstance(last_event, Mapping):
                last_events.append(last_event)

            completed = [item for item in windows if item.get("completed_window") is True
                         and int(item.get("settled") or 0) > 0]
            recent_completed = completed[-3:]
            recent_positive = sum(int(item.get("net") or 0) > 0 for item in recent_completed)
            recent_sample = sum(int(item.get("settled") or 0) for item in recent_completed)
            continuation_status = (
                "INSUFFICIENT_WINDOWS" if len(recent_completed) < 3 or recent_sample < 5
                else "REPEATEDLY_POSITIVE" if recent_positive >= 2
                else "MIXED_OR_NEGATIVE")
            total_wins = sum(int(item.get("wins") or 0) for item in windows)
            total_losses = sum(int(item.get("losses") or 0) for item in windows)
            direct_selections = sum(int(item.get("direct_selections") or 0) for item in windows)
            latest = windows[-1] if windows else None
            if (continuation_status == "REPEATEDLY_POSITIVE"
                    and (latest is None or not latest.get("settled") or latest.get("net", 0) > 0)):
                status = "REPEATEDLY_WINNING"
            elif latest and latest.get("settled") and latest.get("net", 0) > 0:
                status = "WINNING_LATEST_WINDOW"
            elif latest and latest.get("settled") and latest.get("net", 0) < 0:
                status = "LOSING_LATEST_WINDOW"
            elif total_wins + total_losses:
                status = "EVEN_OR_WAITING"
            else:
                status = "NO_SETTLED_RESULTS"
            rows.append({**info, "status": status, "window_count": len(windows),
                "settled_total": total_wins + total_losses, "wins": total_wins,
                "losses": total_losses, "net": total_wins - total_losses,
                "direct_selections": direct_selections, "shared_attributions": 0,
                "win_rate": (total_wins / (total_wins + total_losses))
                    if total_wins + total_losses else None,
                "latest_window": latest, "recent_windows": windows[-5:],
                "continuation": {"status": continuation_status,
                    "completed_windows": len(recent_completed),
                    "positive_windows": recent_positive, "settled_results": recent_sample,
                    "window_rule": "positive means verified WIN count exceeds verified LOSS count"}})

        rows.sort(key=lambda item: (
            item["status"] == "REPEATEDLY_WINNING",
            item["status"] == "WINNING_LATEST_WINDOW",
            item["net"], item["win_rate"] if item["win_rate"] is not None else -1,
        ), reverse=True)
        latest_event = max(last_events, key=lambda item: str(item.get("observed_at")
                            or item.get("api_received_at") or ""), default=None)
        event_at = ((latest_event or {}).get("observed_at")
                    or (latest_event or {}).get("api_received_at"))
        parsed_at = _parse_time(event_at)
        age_seconds = ((datetime.now(timezone.utc) - parsed_at.astimezone(timezone.utc)).total_seconds()
                       if parsed_at is not None and parsed_at.tzinfo is not None else None)
        collector_status = ("WAITING_FOR_COLLECTOR" if latest_event is None else
                            "ACTIVITY_TIME_UNKNOWN" if age_seconds is None else
                            "COLLECTING" if age_seconds <= 180 else "NO_RECENT_ROUND")
        latest_manual_reset = max(manual_resets, key=lambda item: str(item.get("at") or ""),
                                  default=None)
        if self._performance_observer_error:
            collector_status = "ERROR"
        return {
            "source": "winner_predict_committed_collector_observer",
            "window_scope": ("rolling latest 100 verified committed collector rounds per continuity segment; "
                             "segments reset after a verified continuity gap, 300 seconds without feed activity, "
                             "or an operator reset"),
            "windows_scanned": window_total, "rules": rows,
            "observer": {"status": collector_status,
                "rules_monitored": len(metadata), "rounds_processed": processed_rounds,
                "last_round_id": (latest_event or {}).get("round_id"),
                "last_round_index": (latest_event or {}).get("round_index"),
                "last_committed_at": event_at, "age_seconds": age_seconds,
                "window_size": RULE_PERFORMANCE_WINDOW_SIZE,
                "last_manual_reset_at": (latest_manual_reset or {}).get("at"),
                "last_manual_reset_round_index": (latest_manual_reset or {}).get("round_index"),
                "idle_reset_after_seconds": RULE_PERFORMANCE_IDLE_RESET_SECONDS,
                "error": self._performance_observer_error},
            "interpretation": (
                "All registered rules are observed independently from verified committed collector rounds, "
                "including disabled and unconfigured rules. Rule Performance does not require a SHADOW session "
                "and does not send signals or execute actions. Wins and losses are scored only when the "
                "immediately following verified committed round is available; interrupted windows are excluded "
                "from repeated-positive continuation counts. Historical performance does not guarantee future results."),
        }

    def rule_performance(self, *, max_windows: int = 100) -> dict[str, Any]:
        """Summarize verified SHADOW results by rule and completed session window.

        In shared-selection modes, one settled target may be attributed to each
        rule that contributed to that selection. The response keeps that
        attribution explicit so it is not mistaken for independent bets.
        """
        if self._performance_observer_enabled:
            return self._continuous_rule_performance(max_windows=max_windows)
        cap = min(max(int(max_windows), 3), 250)
        with self._lock:
            ordered = sorted(self.sessions.values(), key=lambda row: row.get("started_at", ""))[-cap:]
            sessions = []
            for row in ordered:
                sessions.append({
                    "session_id": row.get("session_id"), "mode": row.get("mode"),
                    "status": self._effective_session_status(row),
                    "started_at": row.get("started_at"), "ended_at": row.get("ended_at"),
                    "observed_rounds": int(row.get("observed_rounds") or 0),
                    "window_size": row.get("window_size"),
                    "rule_ids": list(row.get("rule_ids") or []),
                    "manifest": deepcopy(row.get("manifest") or {}),
                    "evaluations": deepcopy(row.get("evaluations") or []),
                    "selections": deepcopy(row.get("selections") or []),
                    "rule_selections": deepcopy(row.get("rule_selections") or {}),
                })
            registry_rows = deepcopy(self.registry().get("rules") or [])

        metadata = {str(row.get("rule_id")): {
            "rule_id": str(row.get("rule_id")), "name": row.get("name") or row.get("rule_id"),
            "configured": row.get("configured") is True, "enabled": row.get("enabled") is True,
        } for row in registry_rows if row.get("rule_id")}
        by_rule: dict[str, list[dict[str, Any]]] = {}

        for session in sessions:
            ids = {str(rule_id) for rule_id in session["rule_ids"] if rule_id}
            manifest_rules = session["manifest"].get("rules") or []
            if isinstance(manifest_rules, list):
                for rule in manifest_rules:
                    if isinstance(rule, Mapping) and rule.get("rule_id"):
                        rule_id = str(rule["rule_id"])
                        ids.add(rule_id)
                        metadata.setdefault(rule_id, {"rule_id": rule_id,
                            "name": rule.get("name") or rule_id, "configured": True, "enabled": True})
            elif isinstance(manifest_rules, Mapping):
                for rule_id, rule in manifest_rules.items():
                    ids.add(str(rule_id))
                    metadata.setdefault(str(rule_id), {"rule_id": str(rule_id),
                        "name": (rule.get("name") if isinstance(rule, Mapping) else None) or str(rule_id),
                        "configured": True, "enabled": True})

            window_by_rule: dict[str, dict[str, Any]] = {}
            def window_for(rule_id: str) -> dict[str, Any]:
                if rule_id not in window_by_rule:
                    info = metadata.get(rule_id) or {"rule_id": rule_id, "name": rule_id,
                        "configured": True, "enabled": True}
                    metadata.setdefault(rule_id, info)
                    window_by_rule[rule_id] = {
                        "rule_id": rule_id, "name": info["name"],
                        "session_id": session["session_id"], "mode": session["mode"],
                        "session_status": session["status"], "started_at": session["started_at"],
                        "ended_at": session["ended_at"], "observed_rounds": session["observed_rounds"],
                        "window_size": session["window_size"], "wins": 0, "losses": 0,
                        "pending": 0, "eligible_checks": 0, "false_checks": 0,
                        "direct_selections": 0, "shared_attributions": 0,
                        "_selection_ids": set(),
                    }
                return window_by_rule[rule_id]

            for rule_id in ids:
                window_for(rule_id)

            for evaluation in session["evaluations"]:
                if not isinstance(evaluation, Mapping):
                    continue
                for check in evaluation.get("rule_evaluations") or []:
                    if not isinstance(check, Mapping) or not check.get("rule_id"):
                        continue
                    rule_id = str(check["rule_id"])
                    item = window_for(rule_id)
                    eligible = check.get("eligible", check.get("result"))
                    if eligible is True:
                        item["eligible_checks"] += 1
                    elif eligible is False:
                        item["false_checks"] += 1

            def record_selection(rule_id: str, selection: Mapping[str, Any], *, shared: bool) -> None:
                item = window_for(rule_id)
                selection_id = str(selection.get("selection_id") or selection.get("decision_id") or "")
                if selection_id and selection_id in item["_selection_ids"]:
                    return
                if selection_id:
                    item["_selection_ids"].add(selection_id)
                status = str(selection.get("status") or selection.get("result") or "").upper()
                if status == "WIN":
                    item["wins"] += 1
                elif status == "LOSS":
                    item["losses"] += 1
                elif status in {"PENDING", "UNKNOWN", ""}:
                    item["pending"] += 1
                if shared:
                    item["shared_attributions"] += 1
                else:
                    item["direct_selections"] += 1

            for rule_id, selections in (session["rule_selections"] or {}).items():
                ids.add(str(rule_id))
                for selection in selections or []:
                    if isinstance(selection, Mapping):
                        record_selection(str(rule_id), selection, shared=False)

            for selection in session["selections"]:
                if not isinstance(selection, Mapping):
                    continue
                contributors = selection.get("matched_rule_ids") or selection.get("contributing_rule_ids") or []
                if not contributors:
                    contributors = [row.get("rule_id") for row in selection.get("contributors") or []
                                    if isinstance(row, Mapping) and row.get("rule_id")]
                if not contributors and len(session["rule_ids"]) == 1:
                    contributors = session["rule_ids"]
                shared = len(set(str(value) for value in contributors if value)) > 1 or session["mode"] in {
                    "FIVE_RULE_TRACKER", "ALL_RULES_TOGETHER"}
                for rule_id in dict.fromkeys(str(value) for value in contributors if value):
                    record_selection(rule_id, selection, shared=shared)

            for item in window_by_rule.values():
                item["settled"] = item["wins"] + item["losses"]
                item["net"] = item["wins"] - item["losses"]
                item["completed_window"] = (item["session_status"] == "COMPLETED"
                    and item["window_size"] is not None
                    and item["observed_rounds"] >= item["window_size"])
                item.pop("_selection_ids", None)
                by_rule.setdefault(item["rule_id"], []).append(item)

        rows = []
        for rule_id, info in metadata.items():
            windows = sorted(by_rule.get(rule_id, []), key=lambda item: item.get("started_at") or "")
            settled_windows = [item for item in windows if item["completed_window"] and item["settled"] > 0]
            recent_completed = settled_windows[-3:]
            recent_positive = sum(item["net"] > 0 for item in recent_completed)
            recent_sample = sum(item["settled"] for item in recent_completed)
            continuation_status = ("INSUFFICIENT_WINDOWS" if len(recent_completed) < 3 or recent_sample < 5
                else "REPEATEDLY_POSITIVE" if recent_positive >= 2 else "MIXED_OR_NEGATIVE")
            total_wins = sum(item["wins"] for item in windows)
            total_losses = sum(item["losses"] for item in windows)
            direct_selections = sum(item["direct_selections"] for item in windows)
            shared_attributions = sum(item["shared_attributions"] for item in windows)
            latest = windows[-1] if windows else None
            if (continuation_status == "REPEATEDLY_POSITIVE"
                    and (latest is None or latest["settled"] == 0 or latest["net"] > 0)):
                status = "REPEATEDLY_WINNING"
            elif latest and latest["settled"] and latest["net"] > 0:
                status = "WINNING_LATEST_WINDOW"
            elif latest and latest["settled"] and latest["net"] < 0:
                status = "LOSING_LATEST_WINDOW"
            elif total_wins + total_losses:
                status = "EVEN_OR_WAITING"
            else:
                status = "NO_SETTLED_RESULTS"
            rows.append({**info, "status": status,
                "window_count": len(windows), "settled_total": total_wins + total_losses,
                "wins": total_wins, "losses": total_losses, "net": total_wins - total_losses,
                "direct_selections": direct_selections, "shared_attributions": shared_attributions,
                "win_rate": (total_wins / (total_wins + total_losses)) if total_wins + total_losses else None,
                "latest_window": latest,
                "recent_windows": windows[-5:],
                "continuation": {"status": continuation_status, "completed_windows": len(recent_completed),
                    "positive_windows": recent_positive, "settled_results": recent_sample,
                    "window_rule": "positive means verified WIN count exceeds verified LOSS count"}})
        rows.sort(key=lambda item: (
            item["status"] == "REPEATEDLY_WINNING",
            item["status"] == "WINNING_LATEST_WINDOW",
            item["net"], item["win_rate"] if item["win_rate"] is not None else -1,
        ), reverse=True)
        return {"source": "winner_predict_rules_engine_shadow", "window_scope": "one saved Rules Engine session",
                "windows_scanned": len(sessions), "rules": rows,
                "interpretation": "Only settled SHADOW outcomes are scored. Shared selections are attributed to each contributing rule and are not independent samples. Positive past windows do not guarantee future wins; this report does not change rule selection or execute actions."}

    def restart_rule_performance(self, *, now: str | None = None) -> dict[str, Any]:
        """Start a fresh rolling 100-round observer window at the latest verified round."""
        if not self._performance_observer_enabled:
            raise ValueError("RULE_PERFORMANCE_OBSERVER_UNAVAILABLE")
        reset_at = _now(now)
        with self._lock:
            observers = {rule_id: self._performance_observer(rule_id)
                         for rule_id in self._performance_rule_ids()}
            boundary_by_rule = {
                rule_id: observer.get("last_event") or {}
                for rule_id, observer in observers.items()
            }
            identities = set()
            for boundary in boundary_by_rule.values():
                try:
                    round_index = int(boundary.get("round_index") or 0)
                    stream_sequence = int(boundary.get("stream_sequence") or 0)
                except (TypeError, ValueError, OverflowError) as exc:
                    raise ValueError("RULE_PERFORMANCE_OBSERVER_NOT_READY") from exc
                if (not boundary.get("stream_id") or not boundary.get("round_id")
                        or boundary.get("continuity_status") != "VERIFIED"
                        or round_index < 1 or stream_sequence < 1):
                    raise ValueError("RULE_PERFORMANCE_OBSERVER_NOT_READY")
                identities.add((str(boundary["stream_id"]), str(boundary["round_id"]),
                                round_index, stream_sequence))
            if len(identities) != 1:
                raise ValueError("RULE_PERFORMANCE_OBSERVERS_OUT_OF_SYNC")
            boundary = deepcopy(next(iter(boundary_by_rule.values())))
            boundary_index = int(boundary["round_index"])
            reset_window_start = boundary_index + 1

            for rule_id, observer in observers.items():
                self._archive_performance_windows(
                    observer, rule_id, interruption="MANUAL_RESET", force=True)
                observer["engine"].sessions.clear()
                observer["segment_start_round_index"] = reset_window_start
                observer["manual_reset_at"] = reset_at
                observer["manual_reset_round_index"] = boundary_index
                self._start_performance_window(observer, rule_id, boundary)

            active_session_ids: set[str] = set()
            active_adaptive_sessions_updated = 0
            active_automatic_sessions_updated = 0
            for session in self.sessions.values():
                if self._effective_session_status(session) not in {"RUNNING", "SETTLING"}:
                    continue
                if session.get("adaptive_rule_selection") is True:
                    version = int(session.get("adaptive_selection_version") or 0) + 1
                    reset_decision = {"rule_ids": [], "reason": "PERFORMANCE_WINDOW_RESET",
                        "source_round_index": boundary_index, "evaluated_at": reset_at,
                        "context": {"window_rounds": 50, "reset_window_start_round_index": reset_window_start},
                        "selection_version": version,
                        "decision_sequence": len(session.get("adaptive_decisions") or []) + 1,
                        "added": [], "removed": [{"rule_id": rid,
                            "reason": "PERFORMANCE_WINDOW_RESET"}
                            for rid in session.get("active_rule_ids") or []],
                        "candidates": [{"rule_id": rid, "selected": False,
                            "validated": False, "evidence_status": "EXPLORATORY",
                            "reason": "PERFORMANCE_WINDOW_RESET", "score": None}
                            for rid in session.get("rule_ids") or []]}
                    session["active_rule_ids"] = []
                    session["adaptive_selection_version"] = version
                    session["adaptive_rule_choice"] = reset_decision
                    session.setdefault("adaptive_decisions", []).append(deepcopy(reset_decision))
                    session["adaptive_performance_reset_round_index"] = boundary_index
                    session["performance_window_reset_at"] = reset_at
                    session["performance_window_reset_round_index"] = boundary_index
                    active_session_ids.add(str(session["session_id"]))
                    active_adaptive_sessions_updated += 1
                    continue
                if session.get("automatic_rule_selection") is not True:
                    continue
                previous_candidates = {
                    str(item.get("rule_id")): item
                    for item in (session.get("automatic_rule_choice") or {}).get("candidates", [])
                    if isinstance(item, Mapping) and item.get("rule_id")
                }
                candidates = []
                for rule_id in session.get("rule_ids") or []:
                    prior = previous_candidates.get(str(rule_id)) or {}
                    candidate = {key: deepcopy(prior[key]) for key in (
                        "loss_limit_losses", "loss_limit", "loss_limit_blocked",
                        "loss_limit_reached_round_index", "entry_signal_latched",
                        "rule_state", "cycle_status", "cycle_active",
                        "cycle_requested_targets", "cycle_resolved_targets",
                        "cycle_loss_count", "cycle_maximum_targets",
                        "cycle_end_condition", "cycle_end_reason") if key in prior}
                    candidate.update({
                        "rule_id": rule_id, "qualified": False,
                        "reason": "PERFORMANCE_WINDOW_RESET",
                        "settled": 0, "wins": 0, "losses": 0, "net_wins": 0,
                        "window_rounds": 0, "window_size": RULE_PERFORMANCE_WINDOW_SIZE,
                        "window_mode": "ROLLING",
                        "window_start_round_index": reset_window_start,
                        "window_end_round_index": boundary_index,
                        "window_last_round_index": boundary_index,
                        "recovered_rounds": 0,
                        "performance_trend": "INSUFFICIENT",
                        "performance_momentum_pp": None,
                        "recent_outcome_wins": 0, "recent_outcome_losses": 0,
                        "recent_outcome_count": 0, "previous_outcome_wins": 0,
                        "previous_outcome_losses": 0, "previous_outcome_count": 0,
                        "outcome_sample_count": 0,
                        "signal_trend": "UNKNOWN", "signal_momentum_pp": None,
                        "recent_signal_hits": 0, "recent_signal_checks": 0,
                        "previous_signal_hits": 0, "previous_signal_checks": 0,
                        "signal_sample_count": 0,
                    })
                    candidates.append(candidate)
                session["active_rule_ids"] = []
                session["automatic_rule_choice"] = {
                    "rule_ids": [], "reason": "PERFORMANCE_WINDOW_RESET",
                    "source_round_index": boundary_index,
                    "reset_at": reset_at,
                    "window_start_round_index": reset_window_start,
                    "window_size": RULE_PERFORMANCE_WINDOW_SIZE,
                    "candidates": candidates,
                }
                session["performance_window_reset_at"] = reset_at
                session["performance_window_reset_round_index"] = boundary_index
                active_session_ids.add(str(session["session_id"]))
                active_automatic_sessions_updated += 1

            self._persist(session_ids=active_session_ids or set())
            return {
                "ok": True,
                "reset_at": reset_at,
                "reset_at_round_index": boundary_index,
                "window_start_round_index": reset_window_start,
                "window_size": RULE_PERFORMANCE_WINDOW_SIZE,
                "rules_reset": len(observers),
                "active_automatic_sessions_updated": active_automatic_sessions_updated,
                "active_adaptive_sessions_updated": active_adaptive_sessions_updated,
                "history_preserved": True,
                "rule_pause_state_reset": False,
            }

    @staticmethod
    def _adaptive_session_result_counts(session: Mapping[str, Any]) -> tuple[int, int, int, int]:
        targets: dict[tuple[str, int], str] = {}
        for rows in (session.get("rule_selections") or {}).values():
            for item in rows or []:
                target = item.get("actual_target") or item.get("expected_target") or {}
                stream = target.get("streamId", target.get("stream_id"))
                try:
                    target_index = int(target.get("roundIndex", target.get("round_index")) or 0)
                except (TypeError, ValueError, OverflowError):
                    continue
                if not stream or target_index < 1:
                    continue
                status = str(item.get("status") or item.get("result") or "").upper()
                key = (str(stream), target_index)
                if status == "WIN":
                    targets[key] = "WIN"
                elif status == "LOSS" and targets.get(key) != "WIN":
                    targets[key] = "LOSS"
                else:
                    targets.setdefault(key, "PENDING")
        values = list(targets.values())
        return values.count("WIN"), values.count("LOSS"), values.count("PENDING"), len(targets)

    @staticmethod
    def _settled_shared_result_counts(session: Mapping[str, Any]) -> tuple[int, int]:
        """Count each distinct decision once; separate decisions may share a target."""
        wins = losses = 0
        seen_selection_ids: set[str] = set()
        seen_legacy_targets: set[tuple[str, int]] = set()
        for row in session.get("selections", []) or []:
            if not isinstance(row, Mapping):
                continue
            result = row.get("status")
            if result not in {"WIN", "LOSS"}:
                continue
            selection_id = row.get("selection_id")
            if selection_id:
                selection_key = str(selection_id)
                if selection_key in seen_selection_ids:
                    continue
                seen_selection_ids.add(selection_key)
            else:
                target = row.get("expected_target") or {}
                stream_id = target.get("streamId", target.get("stream_id")) if isinstance(target, Mapping) else None
                round_index = target.get("roundIndex", target.get("round_index")) if isinstance(target, Mapping) else None
                target_key = ((stream_id, round_index) if isinstance(stream_id, str)
                              and isinstance(round_index, int) and not isinstance(round_index, bool) else None)
                if target_key is not None and target_key in seen_legacy_targets:
                    continue
                if target_key is not None:
                    seen_legacy_targets.add(target_key)
            if result == "WIN":
                wins += 1
            else:
                losses += 1
        return wins, losses

    @staticmethod
    def _custom_window_limit_reached(wins: int, losses: int,
                                     configuration: Mapping[str, Any]) -> str | None:
        """Return which configured net outcome is currently at its threshold."""
        net_outcome = wins - losses
        win_limit = configuration.get("auto_next_win_count")
        if (configuration.get("auto_next_limits_enabled") is True
                and isinstance(win_limit, int) and not isinstance(win_limit, bool)
                and net_outcome >= win_limit):
            return "WIN"
        loss_limit = configuration.get("auto_next_loss_count")
        if (configuration.get("auto_next_limits_enabled") is True
                and isinstance(loss_limit, int) and not isinstance(loss_limit, bool)
                and -net_outcome >= loss_limit):
            return "LOSS"
        return None

    @classmethod
    def _reconcile_custom_window_result_limit_stop(cls, session: dict[str, Any],
            wins: int, losses: int, at: str, source: Mapping[str, Any]) -> str | None:
        """Keep a custom net stop aligned while already-pending results settle."""
        config = session.get("configuration_snapshot") or {}
        reached = cls._custom_window_limit_reached(wins, losses, config)
        if (session.get("selection_stop_reason") != "AUTO_NEXT_RESULT_LIMIT_REACHED"
                or session.get("selection_stop_source") != "AUTO_NEXT_RESULT_LIMIT"):
            return reached
        if reached is not None:
            details = dict(session.get("selection_stop_details") or {})
            details.update({"reached": reached, "settled_wins": wins,
                "settled_losses": losses, "net_outcome": wins - losses,
                "win_count_limit": config.get("auto_next_win_count"),
                "loss_count_limit": config.get("auto_next_loss_count")})
            session["selection_stop_details"] = details
            return reached
        if session.get("status") not in {"RUNNING", "SETTLING"}:
            return reached

        reason = session.get("selection_stop_reason")
        stopped_at = session.get("selection_stopped_at")
        if stopped_at and not any(
            isinstance(event, Mapping)
            and event.get("reason") == reason
            and event.get("stopped_at") == stopped_at
            and event.get("scope") in {"SELECTION", "SESSION_AND_SELECTION"}
            for event in (session.get("stop_history") or [])
        ):
            cls._append_stop_event(session, reason, stopped_at, "AUTO_NEXT_RESULT_LIMIT",
                cls._source_round(source), "SELECTION",
                session.get("selection_stop_details"))
        session.update({"selection_status": "ACTIVE", "selection_stop_reason": None,
            "selection_stop_source": None, "selection_stopped_at": None,
            "selection_stopped_at_round_id": None, "selection_stop_details": {}})
        if session.get("status") == "SETTLING":
            session["status"] = "RUNNING"
        if (session.get("auto_next_trigger") == "LIMIT_REACHED"
                and session.get("auto_next_armed")
                and not session.get("auto_next_session_id")):
            session.update({"auto_next_armed": False, "auto_next_trigger": None,
                "auto_next_armed_at": None,
                "auto_next_block_reason": "RESULT_LIMIT_NO_LONGER_ACTIVE"})
        return reached

    @staticmethod
    def _pending_shared_result_count(session: Mapping[str, Any]) -> int:
        pending = [row for row in session.get("selections", []) or []
                   if isinstance(row, Mapping) and row.get("status") == "PENDING"]
        identifiers = [str(row.get("selection_id")) for row in pending if row.get("selection_id")]
        return len(set(identifiers)) + sum(1 for row in pending if not row.get("selection_id"))

    @staticmethod
    def _pending_selection_count(session: Mapping[str, Any]) -> int:
        rows = list(session.get("selections") or [])
        rows.extend(selection for group in (session.get("rule_selections") or {}).values()
                    for selection in group)
        rows.extend(session.get("adaptive_automatic_baseline_selections") or [])
        seen: set[str] = set()
        pending = 0
        for row in rows:
            if not isinstance(row, Mapping) or row.get("status") != "PENDING":
                continue
            identifier = str(row.get("selection_id") or "")
            marker = identifier or f"anonymous:{id(row)}"
            if marker in seen:
                continue
            seen.add(marker)
            pending += 1
        return pending

    @classmethod
    def _normalize_legacy_limit_stop(cls, session: dict[str, Any]) -> None:
        """Separate current limit state from historical reasons in old snapshots."""
        config = session.get("configuration_snapshot") or {}
        wins, losses = cls._settled_shared_result_counts(session)
        custom_limits_enabled = config.get("auto_next_limits_enabled") is True
        # A per-window custom net-outcome rule supersedes the legacy net stop.
        # Keep old stop events in history, but do not resume that stop after a
        # restart when its configured threshold was never reached.
        valid_reason = (None if custom_limits_enabled
                        else cls._net_outcome_stop_reason(wins, losses, config))
        for reason_field, source_field, at_field, round_field, details_field, scope in (
            ("selection_stop_reason", "selection_stop_source", "selection_stopped_at",
             "selection_stopped_at_round_id", "selection_stop_details", "SELECTION"),
            ("stop_reason", "stop_source", "stop_at", "stop_round_id", "stop_details", "SESSION"),
        ):
            reason = session.get(reason_field)
            if reason not in NET_OUTCOME_LIMIT_REASONS:
                continue
            stopped_at = session.get(at_field)
            stop_source = session.get(source_field) or "LEGACY_UNVERIFIED"
            round_id = session.get(round_field)
            if valid_reason == reason:
                session[source_field] = stop_source
                session.setdefault("legacy_stop_source_status", "REASON_VALIDATED_PROVENANCE_MISSING")
                # A valid limit stops selections while the session settles
                # pending results and finishes its observation window.
                if session.get("status") in {"RUNNING", "SETTLING"}:
                    session.update({"status": "SETTLING", "selection_status": "STOPPED"})
                    if reason_field == "stop_reason" and not session.get("selection_stop_reason"):
                        session.update({"selection_stop_reason": reason,
                            "selection_stop_source": stop_source,
                            "selection_stopped_at": stopped_at,
                            "selection_stopped_at_round_id": round_id,
                            "selection_stop_details": deepcopy(session.get(details_field) or {})})
                continue

            details = {"original_reason": reason, "settled_wins": wins,
                "settled_losses": losses, "net_outcome": wins - losses,
                "win_limit_enabled": config.get("win_limit_enabled") is True,
                "loss_limit_enabled": config.get("loss_limit_enabled") is True}
            session["legacy_stop_reason"] = reason
            session["legacy_stop_reason_status"] = "UNVERIFIED_DISABLED_OR_UNMET_LIMIT"
            session["legacy_stop_details"] = deepcopy(details)
            if stopped_at is None:
                session["legacy_stop_timestamp_status"] = "UNAVAILABLE"
            else:
                history = session.get("stop_history") or []
                if not any(isinstance(event, Mapping) and event.get("reason") == reason
                           and event.get("stopped_at") == stopped_at for event in history):
                    event = cls._append_stop_event(session, reason, stopped_at, stop_source,
                        {"round_id": round_id} if round_id else None, scope, details)
                    session["last_selection_stop_event" if reason_field == "selection_stop_reason"
                            else "last_stop_event"] = event

            # Keep the original event and legacy metadata, but make sure a
            # disabled/unmet limit is no longer the current stop reason.
            for field in (reason_field, source_field, at_field, round_field, details_field):
                session[field] = None if field != details_field else {}
            if (reason_field == "selection_stop_reason"
                    and session.get("status") in {"RUNNING", "SETTLING"}):
                session["selection_status"] = "ACTIVE"
                if session.get("status") == "SETTLING":
                    session["status"] = "RUNNING"

        if custom_limits_enabled:
            # Old snapshots may already have armed an immediate successor for
            # a legacy net limit or for the former custom-limit behavior. The
            # new rule always waits for the observation window to finish.
            if (session.get("auto_next_trigger") == "LIMIT_REACHED"
                    and session.get("auto_next_session_id") is None):
                session.update({"auto_next_armed": False, "auto_next_trigger": None,
                    "auto_next_armed_at": None, "auto_next_after_round_id": None,
                    "auto_next_boundary": None,
                    "auto_next_block_reason": "CUSTOM_LIMIT_WAITS_FOR_WINDOW"})

            reached = cls._reconcile_custom_window_result_limit_stop(
                session, wins, losses, session.get("updated_at") or session.get("ended_at") or "", {})
            win_limit = config.get("auto_next_win_count")
            loss_limit = config.get("auto_next_loss_count")
            net_outcome = wins - losses
            if (session.get("selection_status") == "ACTIVE" and reached
                    and session.get("status") in {"RUNNING", "SETTLING"}):
                latest_round = next((row for row in reversed(session.get("context") or [])
                                     if isinstance(row, Mapping)), None)
                details = {"reached": reached, "settled_wins": wins,
                    "settled_losses": losses, "win_count_limit": win_limit,
                    "loss_count_limit": loss_limit, "net_outcome": net_outcome}
                session.update({"selection_status": "STOPPED",
                    "selection_stop_reason": "AUTO_NEXT_RESULT_LIMIT_REACHED",
                    "selection_stop_source": "AUTO_NEXT_RESULT_LIMIT",
                    "selection_stopped_at": (session.get("updated_at") or session.get("ended_at")),
                    "selection_stopped_at_round_id": ((latest_round or {}).get("round_id")
                        or (latest_round or {}).get("roundId")),
                    "selection_stop_details": details, "status": "SETTLING"})

    @classmethod
    def _ensure_legacy_stop_provenance(cls, session: dict[str, Any]) -> None:
        """Mark historical stops whose original source or time was not persisted."""
        for reason_field, source_field, at_field, round_field, scope in (
            ("selection_stop_reason", "selection_stop_source", "selection_stopped_at",
             "selection_stopped_at_round_id", "LEGACY_SELECTION_STOP"),
            ("stop_reason", "stop_source", "stop_at", "stop_round_id", "LEGACY_SESSION_STOP"),
        ):
            reason = session.get(reason_field)
            if not isinstance(reason, str) or not reason:
                continue
            if not session.get(source_field):
                session[source_field] = "LEGACY_UNVERIFIED"
            stopped_at = session.get(at_field)
            if not stopped_at:
                session["legacy_stop_timestamp_status"] = "UNAVAILABLE"
                continue
            if not any(isinstance(event, Mapping) and event.get("reason") == reason
                       and event.get("stopped_at") == stopped_at
                       for event in (session.get("stop_history") or [])):
                event = cls._append_stop_event(session, reason, stopped_at, session[source_field],
                    {"round_id": session.get(round_field)} if session.get(round_field) else None,
                    scope, {"provenance_status": "LEGACY_UNVERIFIED"})
                session["last_selection_stop_event" if reason_field == "selection_stop_reason"
                        else "last_stop_event"] = event

    @staticmethod
    def _net_outcome_stop_reason(wins: int, losses: int,
                                 configuration: Mapping[str, Any]) -> str | None:
        net_outcome = wins - losses
        win_count = configuration.get("win_limit_count")
        if (configuration.get("win_limit_enabled") is True
                and isinstance(win_count, int) and not isinstance(win_count, bool)
                and win_count > 0 and net_outcome >= win_count):
            return "WIN_LIMIT_REACHED"
        loss_count = configuration.get("loss_limit_count")
        if (configuration.get("loss_limit_enabled") is True
                and isinstance(loss_count, int) and not isinstance(loss_count, bool)
                and loss_count > 0 and -net_outcome >= loss_count):
            return "LOSS_LIMIT_REACHED"
        return None

    @staticmethod
    def _trim_committed_event_records(session: dict[str, Any], *, keep: int = 100) -> None:
        """Bound the live deduplication snapshot; full history stays in SQL ledgers.

        Pending observed records must remain until their matching committed
        event upgrades them. Older committed records are safely covered by the
        monotonic per-session stream cursor and the durable evaluation ledgers.
        """
        records = session.get("event_records")
        if not isinstance(records, dict) or len(records) <= keep:
            return
        pending = {key: value for key, value in records.items()
                   if isinstance(value, Mapping)
                   and value.get("persistence_status") == "PENDING"}
        committed = [(key, value) for key, value in records.items()
                     if isinstance(value, Mapping)
                     and value.get("persistence_status") != "PENDING"]
        committed.sort(key=lambda item: (
            int((item[1].get("source_round") or {}).get("roundIndex") or 0), item[0]))
        retained = dict(committed[-max(1, int(keep)):])
        retained.update(pending)
        session["event_records"] = retained

    @staticmethod
    def _has_pending_event_records(session: Mapping[str, Any]) -> bool:
        return any(record.get("persistence_status") == "PENDING"
                   for record in (session.get("event_records") or {}).values())

    @staticmethod
    def _effective_session_status(session: Mapping[str, Any]) -> str:
        status = str(session.get("status") or "UNKNOWN")
        observed = session.get("observed_rounds")
        window = session.get("window_size")
        if (status in {"RUNNING", "SETTLING"}
                and isinstance(observed, int) and not isinstance(observed, bool)
                and isinstance(window, int) and not isinstance(window, bool)
                and window > 0 and observed >= window):
            return "COMPLETED"
        return status

    @staticmethod
    def _session_accepts_source_events(session: Mapping[str, Any]) -> bool:
        status = RulesEngine._effective_session_status(session)
        if status in {"RUNNING", "SETTLING"}:
            return True
        if status == "PAUSED" and session.get("stop_reason") in {None, "MANUAL_PAUSE"}:
            return True
        if status == "PAUSED" and session.get("stop_reason") == "ROUND_INDEX_GAP":
            # Replay can carry a legacy gap-paused session to the next fresh
            # boundary, where observe_round rebases and resumes it.
            return True
        if status == "MANUALLY_STOPPED":
            selections = list(session.get("selections") or [])
            selections.extend(item for rows in (session.get("rule_selections") or {}).values()
                              for item in rows)
            return any(item.get("status") == "PENDING" for item in selections)
        return False

    @staticmethod
    def _session_accepts_settlement_events(session: Mapping[str, Any]) -> bool:
        """Allow terminal sessions to settle already-created targets, but honor reset/archive."""
        status = RulesEngine._effective_session_status(session)
        if status in {"RUNNING", "SETTLING", "PAUSED"}:
            return True
        if status not in {"COMPLETED", "MANUALLY_STOPPED", "RECONFIGURED"}:
            return False
        selections = list(session.get("selections") or [])
        selections.extend(item for rows in (session.get("rule_selections") or {}).values()
                          for item in rows)
        return any(item.get("status") == "PENDING" for item in selections)

    def _selected_session(self) -> dict[str, Any] | None:
        rows = sorted(self.sessions.values(),
                      key=lambda row: row.get("started_at", ""), reverse=True)
        running = next((item for item in rows
                        if self._effective_session_status(item) in {"RUNNING", "SETTLING"}), None)
        if running:
            return running
        return next((item for item in rows if item.get("status") == "PAUSED"
                     and item.get("stop_reason") in {None, "MANUAL_PAUSE"}), None)

    @staticmethod
    def _session_status_summary(session: Mapping[str, Any]) -> dict[str, Any]:
        """Build the frequently-polled session view from bounded scalar state.

        Full ledgers remain available from the sessions/detail endpoints. Status
        polling must not count or deepcopy every historical selection because
        an active session can retain a very large settlement ledger.
        """
        configuration = session.get("configuration_snapshot") or {}
        status = RulesEngine._effective_session_status(session)
        if session.get("adaptive_rule_selection") is True:
            selection_count = len(session.get("adaptive_signal_selections") or [])
        elif session.get("mode") == "FIVE_RULE_TRACKER" and not session.get("selections"):
            selection_count = sum(len(rows or []) for rows in
                                  (session.get("rule_selections") or {}).values())
        else:
            selection_count = len(session.get("selections") or [])
        wins = max(0, int(session.get("total_wins") or 0))
        losses = max(0, int(session.get("total_losses") or 0))
        cancelled = max(0, int(session.get("total_cancelled") or 0))
        pending = max(0, selection_count - wins - losses - cancelled)
        evaluations = session.get("evaluations") or []
        last_evaluation = evaluations[-1] if evaluations else {}
        strategy_id = session.get("strategy_id") or (
            "BEHAVIOUR_ROLLING_HIGH" if session.get("behaviour_detection_enabled") else
            "ADAPTIVE" if session.get("adaptive_rule_selection") else
            "AUTOMATIC" if session.get("automatic_rule_selection") else "RULES")
        return {
            "session_id": session.get("session_id"), "mode": session.get("mode"),
            "status": status, "selection_status": session.get("selection_status"),
            "strategy_id": strategy_id,
            "behaviour_strategy": session.get("last_behaviour_eligibility"),
            "last_behaviour_selection": session.get("last_behaviour_selection"),
            "run_until_stopped": configuration.get("run_until_stopped") is True
                or session.get("window_size") is None,
            "started_at": session.get("started_at"), "ended_at": session.get("ended_at"),
            "last_activity_at": session.get("ended_at") or last_evaluation.get("evaluated_at")
                or session.get("started_at"),
            "rule_ids": session.get("rule_ids") or [],
            "automatic_rule_selection": session.get("automatic_rule_selection", False),
            "adaptive_rule_selection": session.get("adaptive_rule_selection", False),
            "active_rule_ids": session.get("active_rule_ids") or [],
            "automatic_rule_choice": session.get("automatic_rule_choice"),
            "adaptive_rule_choice": session.get("adaptive_rule_choice"),
            "adaptive_automatic_baseline_choice": session.get("adaptive_automatic_baseline_choice"),
            "adaptive_selection_version": session.get("adaptive_selection_version", 0),
            "adaptive_decision_count": len(session.get("adaptive_decisions") or []),
            "adaptive_portfolio_performance": session.get("adaptive_portfolio_performance"),
            "adaptive_baseline_comparison": session.get("adaptive_baseline_comparison"),
            "configuration_version": session.get("configuration_version"),
            "observed_rounds": session.get("observed_rounds", 0),
            "window_size": session.get("window_size"),
            "latest_observed_round": session.get("last_received_round"),
            "last_received_round": session.get("last_received_round"),
            "last_settled_target": session.get("last_settled_target"),
            "last_evaluation_at": last_evaluation.get("evaluated_at"),
            "event_processing_latency_ms": session.get("last_event_processing_latency_ms"),
            "settlement_processing_latency_ms": session.get("last_target_settlement_processing_latency_ms"),
            "is_active": status in {"RUNNING", "SETTLING"},
            "prestart_skipped_rounds": session.get("prestart_skipped_rounds", 0),
            "selection_count": selection_count,
            "results": {"WIN": wins, "LOSS": losses, "PENDING": pending},
            "net_outcome": wins - losses,
            "net_outcome_limit_version": configuration.get("net_outcome_limit_version"),
            "win_limit_enabled": configuration.get("win_limit_enabled", False),
            "win_limit_count": configuration.get("win_limit_count"),
            "loss_limit_enabled": configuration.get("loss_limit_enabled", False),
            "loss_limit_count": configuration.get("loss_limit_count"),
            "profit_target_enabled": configuration.get("profit_target_enabled", False),
            "profit_target_rwf": configuration.get("profit_target_rwf"),
            "auto_next_check_enabled": configuration.get("auto_next_check_enabled"),
            "auto_next_on_limit_enabled": configuration.get("auto_next_on_limit_enabled"),
            "auto_next_limits_enabled": configuration.get("auto_next_limits_enabled", False),
            "auto_next_win_count": configuration.get("auto_next_win_count"),
            "auto_next_loss_count": configuration.get("auto_next_loss_count"),
            "net_profit_rwf": session.get("net_profit_rwf", 0),
            "auto_next_armed": session.get("auto_next_armed", False),
            "auto_next_trigger": session.get("auto_next_trigger"),
            "auto_next_armed_at": session.get("auto_next_armed_at"),
            "auto_next_session_id": session.get("auto_next_session_id"),
            "auto_next_block_reason": session.get("auto_next_block_reason"),
            "stop_reason": session.get("selection_stop_reason") or session.get("stop_reason"),
            "stop_reason_source": session.get("selection_stop_source") or session.get("stop_source"),
            "stop_reason_at": session.get("selection_stopped_at") or session.get("stop_at"),
            "stop_reason_round_id": session.get("selection_stopped_at_round_id") or session.get("stop_round_id"),
            "stop_reason_details": session.get("selection_stop_details") or session.get("stop_details") or {},
            "integrity_stop_reason": session.get("integrity_stop_reason"),
            "requested_stake": configuration.get("requested_stake",
                configuration.get("fixed_stake_rwf", configuration.get("stake_rwf"))),
            "requested_stake_currency": configuration.get("stake_currency", "RWF"),
            "max_selections": configuration.get("max_selections"),
        }

    def session_for_strategy(self, strategy_id: str) -> dict[str, Any] | None:
        """Return the active session identity for one selected strategy."""
        with self._lock:
            session = self._selected_session()
            selected_strategy = ((session or {}).get("strategy_id") or
                ("BEHAVIOUR_ROLLING_HIGH" if (session or {}).get("behaviour_detection_enabled") else
                 "ADAPTIVE" if (session or {}).get("adaptive_rule_selection") else
                 "AUTOMATIC" if (session or {}).get("automatic_rule_selection") else "RULES"))
            if (session is None or session.get("status") not in {"RUNNING", "SETTLING"}
                    or session.get("selection_status") != "ACTIVE"
                    or selected_strategy != strategy_id):
                return None
            # Avoid copying the potentially large selection and evaluation ledgers.
            return {"session_id": str(session.get("session_id") or ""),
                    "strategy_id": strategy_id,
                    "status": session.get("status"),
                    "selection_status": session.get("selection_status")}

    def stream_cursor(self, stream_id: str, latest_sequence: int) -> int:
        # Rewind for any durable consumer that still needs committed events:
        # active SHADOW sessions, pending event records, and the independent
        # performance observer. The observer must recover rounds even when no
        # active SHADOW session is eligible to drive the historical replay.
        with self._lock:
            latest = max(0, int(latest_sequence))
            has_successor = any(row.get("last_stream_id") == stream_id
                                and self._effective_session_status(row) in {"RUNNING", "SETTLING"}
                                for row in self.sessions.values())
            cursors = [min(latest, max(0, int(row.get("last_stream_sequence", latest))))
                       for row in self.sessions.values()
                       if row.get("last_stream_id") == stream_id
                       and (self._session_accepts_source_events(row)
                            or self._has_pending_event_records(row))
                       and not (has_successor and row.get("status") == "PAUSED"
                                and row.get("stop_reason") == "ROUND_INDEX_GAP"
                                and not self._has_pending_event_records(row))]

            observer_checkpoints = [observer.get("last_event")
                                    for observer in self._performance_observers.values()
                                    if isinstance(observer, Mapping)
                                    and isinstance(observer.get("last_event"), Mapping)]
            observer_cursors = []
            observer_stream_changed = False
            for checkpoint in observer_checkpoints:
                if str(checkpoint.get("stream_id") or "") != str(stream_id):
                    observer_stream_changed = True
                    continue
                try:
                    sequence = int(checkpoint.get("stream_sequence") or 0)
                except (TypeError, ValueError, OverflowError):
                    sequence = 0
                if sequence > 0:
                    observer_cursors.append(min(latest, sequence))
            if observer_cursors:
                cursors.append(min(observer_cursors))
            if observer_stream_changed:
                # A lagging observer still attached to an earlier stream has
                # no usable cursor for this one. Replay enough real committed
                # events to seed context and fill the window without rescanning
                # the replacement stream's full lifetime.
                cursors.append(max(0, latest - RULE_PERFORMANCE_WINDOW_SIZE - 5))
            return min(cursors) if cursors else latest

    @staticmethod
    def _session_summary(session: Mapping[str, Any]) -> dict[str, Any]:
        # New sessions count each shared target once, regardless of how many
        # independent rules contributed. Keep the old per-rule ledger summary
        # readable for sessions persisted by the previous tracker version.
        rows = session.get("selections", [])
        adaptive_rule_performance: dict[str, Any] = {}
        adaptive_portfolio_performance: dict[str, Any] | None = None
        adaptive_net_profit: float | None = None
        if session.get("adaptive_rule_selection") is True:
            rule_groups = session.get("rule_selections") or {}
            target_groups: dict[tuple[str, int], list[Mapping[str, Any]]] = {}
            for rule_id in session.get("rule_ids") or []:
                selections = list(rule_groups.get(rule_id) or [])
                wins = sum(str(item.get("status") or item.get("result") or "").upper() == "WIN" for item in selections)
                losses = sum(str(item.get("status") or item.get("result") or "").upper() == "LOSS" for item in selections)
                pending = sum(str(item.get("status") or item.get("result") or "").upper()
                              in {"PENDING", "UNKNOWN", ""} for item in selections)
                last_target = None
                for item in selections:
                    target = item.get("actual_target") or item.get("expected_target") or {}
                    stream = target.get("streamId", target.get("stream_id"))
                    index = target.get("roundIndex", target.get("round_index"))
                    if stream and isinstance(index, int) and not isinstance(index, bool):
                        target_groups.setdefault((str(stream), index), []).append(item)
                        last_target = max(index, last_target or 0)
                adaptive_rule_performance[rule_id] = {
                    "wins": wins, "losses": losses, "pending": pending,
                    "net": wins - losses, "targets": len(selections),
                    "target_progress": {"completed": wins + losses, "pending": pending,
                                        "last_target_round_index": last_target},
                }
            grouped_statuses = []
            for items in target_groups.values():
                statuses = {str(item.get("status") or item.get("result") or "").upper()
                            for item in items}
                grouped_statuses.append("WIN" if "WIN" in statuses else
                                        "LOSS" if "LOSS" in statuses else "PENDING")
            adaptive_wins = grouped_statuses.count("WIN")
            adaptive_losses = grouped_statuses.count("LOSS")
            adaptive_pending = grouped_statuses.count("PENDING")
            results = {"WIN": adaptive_wins, "LOSS": adaptive_losses, "PENDING": adaptive_pending}
            rows = [item for selections in rule_groups.values() for item in selections]
            selection_count = len(target_groups)
            net_outcome = adaptive_wins - adaptive_losses
            stake = float((session.get("configuration_snapshot") or {}).get("fixed_stake_rwf") or 100)
            adaptive_net_profit = round((adaptive_wins - adaptive_losses) * stake, 2)
            adaptive_portfolio_performance = {"wins": adaptive_wins, "losses": adaptive_losses,
                "pending": adaptive_pending, "net": net_outcome, "unique_targets": len(target_groups),
                "independent_rule_predictions": len(rows), "net_profit_rwf": adaptive_net_profit}
        elif session.get("mode") == "FIVE_RULE_TRACKER" and not rows:
            legacy_rows = [item for values in session.get("rule_selections", {}).values() for item in values]
            if legacy_rows:
                rows = legacy_rows
                results = {status: sum(item.get("status") == status for item in rows)
                           for status in ("WIN", "LOSS", "PENDING")}
            else:
                results = {"WIN": 0, "LOSS": 0, "PENDING": 0}
        else:
            pending = RulesEngine._pending_shared_result_count(session)
            wins, losses = RulesEngine._settled_shared_result_counts(session)
            results = {"WIN": wins, "LOSS": losses, "PENDING": pending}
        net_outcome = results["WIN"] - results["LOSS"]
        selection_ids = [str(row.get("selection_id")) for row in rows
                         if isinstance(row, Mapping) and row.get("selection_id")]
        if session.get("adaptive_rule_selection") is not True:
            selection_count = len(set(selection_ids)) + sum(
                1 for row in rows if isinstance(row, Mapping) and not row.get("selection_id"))
        configuration = session.get("configuration_snapshot") or {}
        requested_stake = configuration.get("requested_stake")
        if requested_stake is None:
            requested_stake = configuration.get("fixed_stake_rwf", configuration.get("stake_rwf"))
        stop_reason = session.get("selection_stop_reason") or session.get("stop_reason")
        selection_reason = bool(session.get("selection_stop_reason"))
        matching_stop_event = next((event for event in reversed(session.get("stop_history") or [])
            if isinstance(event, Mapping) and event.get("reason") == stop_reason
            and event.get("scope") in ({"SELECTION", "SESSION_AND_SELECTION"} if selection_reason
                                        else {"SESSION", "SESSION_AND_SELECTION"})), None)
        stop_source = (((matching_stop_event or {}).get("source")
                       or session.get("selection_stop_source" if selection_reason else "stop_source"))
                       if stop_reason is not None else None)
        stop_at = (((matching_stop_event or {}).get("stopped_at")
                   or session.get("selection_stopped_at" if selection_reason else "stop_at"))
                   if stop_reason is not None else None)
        stop_round = ((matching_stop_event or {}).get("source_round") or {})
        stop_round_id = ((stop_round.get("round_id")
                         or session.get("selection_stopped_at_round_id" if selection_reason else "stop_round_id"))
                         if stop_reason is not None else None)
        stop_details = (((matching_stop_event or {}).get("details")
                        or session.get("selection_stop_details" if selection_reason else "stop_details") or {})
                        if stop_reason is not None else {})
        evaluations = session.get("evaluations") or []
        last_evaluation_at = (evaluations[-1].get("evaluated_at")
                              if evaluations and isinstance(evaluations[-1], Mapping) else None)
        return {"session_id": session["session_id"], "mode": session["mode"],
                "status": RulesEngine._effective_session_status(session),
                "run_until_stopped": configuration.get("run_until_stopped") is True
                    or session.get("window_size") is None,
                "selection_status": session.get("selection_status"), "started_at": session.get("started_at"),
                "ended_at": session.get("ended_at"),
                "last_activity_at": session.get("ended_at") or last_evaluation_at or session.get("started_at"),
                "rule_ids": list(session.get("rule_ids", [])),
                "strategy_id": session.get("strategy_id") or (
                    "BEHAVIOUR_ROLLING_HIGH" if session.get("behaviour_detection_enabled") else
                    "ADAPTIVE" if session.get("adaptive_rule_selection") else
                    "AUTOMATIC" if session.get("automatic_rule_selection") else "RULES"),
                "behaviour_strategy": deepcopy(session.get("last_behaviour_eligibility")),
                "last_behaviour_selection": deepcopy(session.get("last_behaviour_selection")),
                "automatic_rule_selection": session.get("automatic_rule_selection", False),
                "adaptive_rule_selection": session.get("adaptive_rule_selection", False),
                "active_rule_ids": list(session.get("active_rule_ids") or []),
                "automatic_rule_choice": deepcopy(session.get("automatic_rule_choice")),
                "adaptive_rule_choice": deepcopy(session.get("adaptive_rule_choice")),
                "adaptive_automatic_baseline_choice": deepcopy(
                    session.get("adaptive_automatic_baseline_choice")),
                "adaptive_selection_version": session.get("adaptive_selection_version", 0),
                "adaptive_decision_count": len(session.get("adaptive_decisions") or []),
                "adaptive_rule_performance": adaptive_rule_performance,
                "adaptive_portfolio_performance": adaptive_portfolio_performance,
                "adaptive_baseline_comparison": deepcopy(session.get("adaptive_baseline_comparison")),
                "configuration_version": session.get("configuration_version"),
                "observed_rounds": session.get("observed_rounds", 0), "window_size": session.get("window_size"),
                "latest_observed_round": deepcopy(session.get("last_received_round")),
                "last_received_round": deepcopy(session.get("last_received_round")),
                "last_settled_target": deepcopy(session.get("last_settled_target")),
                "last_evaluation_at": last_evaluation_at,
                "event_processing_latency_ms": session.get("last_event_processing_latency_ms"),
                "settlement_processing_latency_ms": session.get("last_target_settlement_processing_latency_ms"),
                "is_active": RulesEngine._effective_session_status(session) in {"RUNNING", "SETTLING"},
                "prestart_skipped_rounds": session.get("prestart_skipped_rounds", 0),
                "selection_count": selection_count, "results": results,
                "net_outcome": net_outcome,
                "net_outcome_limit_version": configuration.get("net_outcome_limit_version"),
                "win_limit_enabled": configuration.get("win_limit_enabled", False),
                "win_limit_count": configuration.get("win_limit_count"),
                "loss_limit_enabled": configuration.get("loss_limit_enabled", False),
                "loss_limit_count": configuration.get("loss_limit_count"),
                "profit_target_enabled": configuration.get("profit_target_enabled", False),
                "profit_target_rwf": configuration.get("profit_target_rwf"),
                "auto_next_check_enabled": configuration.get("auto_next_check_enabled"),
                "auto_next_on_limit_enabled": configuration.get("auto_next_on_limit_enabled"),
                "auto_next_limits_enabled": configuration.get("auto_next_limits_enabled", False),
                "auto_next_win_count": configuration.get("auto_next_win_count"),
                "auto_next_loss_count": configuration.get("auto_next_loss_count"),
                "net_profit_rwf": (adaptive_net_profit if adaptive_net_profit is not None
                                    else session.get("net_profit_rwf", 0)),
                "auto_next_armed": session.get("auto_next_armed", False),
                "auto_next_trigger": session.get("auto_next_trigger"),
                "auto_next_armed_at": session.get("auto_next_armed_at"),
                "auto_next_session_id": session.get("auto_next_session_id"),
                "auto_next_block_reason": session.get("auto_next_block_reason"),
                "stop_reason": stop_reason, "stop_reason_source": stop_source,
                "stop_reason_at": stop_at, "stop_reason_round_id": stop_round_id,
                "stop_reason_details": deepcopy(dict(stop_details)),
                "legacy_stop_reason": session.get("legacy_stop_reason"),
                "legacy_stop_reason_status": session.get("legacy_stop_reason_status"),
                "legacy_stop_timestamp_status": session.get("legacy_stop_timestamp_status"),
                "stop_history": deepcopy(session.get("stop_history") or []),
                "integrity_stop_reason": session.get("integrity_stop_reason"),
                "selection_results_kind": "SHADOW_ROUND_OUTCOMES",
                "requested_stake": requested_stake,
                "max_selections": configuration.get("max_selections"),
                }

    def evaluate(self, evaluated_round: dict[str, Any] | None = None) -> dict[str, Any]:
        # Compatibility endpoint deliberately does not infer from a loose/latest row.
        # Only committed, ordered round.persisted events can reach observe_round().
        at = _now()
        active_sessions = any(item.get("status") == "RUNNING" and item.get("selection_status") == "ACTIVE"
                              for item in self.sessions.values())
        if evaluated_round is not None and active_sessions:
            result = {"result": False, "true_rule_ids": [], "rule_evaluations": [],
                      "decision": "BLOCKED", "reason": "COMMITTED_EVENT_REQUIRED",
                      "source_round": deepcopy(evaluated_round)}
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
                or (self._effective_session_status(item) == "COMPLETED"
                    and item.get("auto_next_armed") is True)
                for item in self.sessions.values()
            )

    def status(self, *, include_sessions: bool = True) -> dict[str, Any]:
        active = self._selected_session()
        active_summary = self._session_status_summary(active) if active else None
        # The full sessions endpoint is intentionally separate from the
        # frequently-polled status endpoint. Building every historical
        # session summary walks every saved selection and can make a simple
        # status poll take longer than the browser's request timeout as the
        # session ledger grows.
        if include_sessions:
            sessions = self.sessions_status()
            latest_summary = sessions[0] if sessions else None
        else:
            # The active session is normally also the newest session. Avoid
            # walking any historical selection ledger on every status poll.
            if active is not None:
                latest_summary = active_summary
            else:
                latest_session = max(
                    self.sessions.values(),
                    key=lambda row: row.get("started_at", ""),
                    default=None,
                )
                latest_summary = (self._session_status_summary(latest_session)
                                  if latest_session else None)
            sessions = None
        # Metrics remain tied to a concrete settlement scope after a session
        # stops or completes. Keep active_session separate for control/readiness.
        summary = active_summary or latest_summary
        configured = sum(1 for row in self.rule_configuration.values() if row.get("configured"))
        enabled = sum(1 for row in self.rule_configuration.values() if row.get("configured") and row.get("enabled"))
        if active:
            if active.get("automatic_rule_selection") is True:
                configured = sum(self.rule_configuration.get(rid, {}).get("configured") is True
                                 for rid in active.get("rule_ids", []))
                enabled = sum(self.rule_configuration.get(rid, {}).get("configured") is True
                              and self.rule_configuration.get(rid, {}).get("enabled") is True
                              for rid in active.get("rule_ids", []))
            else:
                configured = len(active.get("rule_ids", []))
                enabled = configured
        latest = (self.last_evaluation or {}).get("source_round",
                  (self.last_evaluation or {}).get("evaluated_round"))
        if self._lock.acquire(blocking=False):
            try:
                event_latency_metrics = {stage: self._latency_summary(values)
                                         for stage, values in self._delivery_metrics.items()}
                self._last_status_latency_metrics = event_latency_metrics
            finally:
                self._lock.release()
        else:
            # Status reads are a monitoring path. Never queue them behind a
            # round evaluation; use the last safe latency sample while the
            # single ordered evaluator owns the engine lock.
            event_latency_metrics = self._last_status_latency_metrics
        return {"status": active.get("status") if active else ("READY" if configured else "NOT_CONFIGURED"),
                "rules_configured": configured, "rules_enabled": enabled,
                "configuration_version": self.configuration_version,
                "presets": deepcopy(self.presets),
                "last_rule_evaluation": self.last_evaluation_at or "NONE",
                "last_evaluated_round": latest, "decision": (self.last_evaluation or {}).get("decision", "NO_ACTION"),
                "rule_evaluations": (self.last_evaluation or {}).get("rule_evaluations", []),
                "active_session": active_summary, "latest_session": latest_summary,
                "reporting_session_id": summary.get("session_id") if summary else None,
                "reporting_session_status": summary.get("status") if summary else None,
                **({"sessions": sessions} if sessions is not None else {}),
                "selection_count": summary.get("selection_count", 0) if summary else 0,
                "total_wins": summary.get("results", {}).get("WIN", 0) if summary else 0,
                "total_losses": summary.get("results", {}).get("LOSS", 0) if summary else 0,
                "net_outcome": summary.get("net_outcome", 0) if summary else 0,
                "pending": summary.get("results", {}).get("PENDING", 0) if summary else 0,
                "event_latency_metrics": {"scope": "CURRENT_PROCESS", "stages_ms": event_latency_metrics},
                }
