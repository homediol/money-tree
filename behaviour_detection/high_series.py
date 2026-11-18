"""Deterministic, serializable HIGH-triggered Part 1 / Part 2 lifecycle."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
from uuid import uuid4

POLICY_VERSION = "high-series-v1"
DEFAULTS = {"trigger_horizon": 5, "pause_limit_verified_rounds": 10,
            "new_series_cooldown_rounds": 10, "cooldown_verified_rounds": 10,
            "pause_timeout_seconds": None, "strategy_mode": "PART1_AND_PART2",
            "part1": {"enabled": True, "max_counted_rounds": 5,
                      "net_win_limit_enabled": True, "net_win_target": 2},
            "part2": {"enabled": True, "max_counted_rounds_per_batch": 5,
                      "net_win_limit_enabled": True, "net_win_target": 3,
                      "pause_limit_verified_rounds": 10},
            "on_win_limit_reached": "WAIT_FOR_NEXT_FRESH_TRIGGER",
            "shadow_only": True, "configuration_version": 1}
TERMINAL = {"STOPPED_LOW_DOMINANT", "STOPPED_PAUSE_LIMIT", "STOPPED_TIME_LIMIT",
            "STOPPED_PART1_NO_HIGH", "PART1_WIN_TARGET_REACHED",
            "PART2_WIN_TARGET_REACHED", "STOPPED_PART1_COMPLETED",
            "STOPPED_HIGH_DOMINANCE_EXIT"}


def new_state(config: dict | None = None) -> dict:
    return {"policy_version": POLICY_VERSION, "state": "IDLE",
            "config": validate_config(config or DEFAULTS), "pending_config": None,
            "pending_configuration_version": None, "series": None,
            "completed_series": [],
            "cooldown_progress": 0, "cooldown_started_at": None,
            "fresh_high_exit_seen": False,
            "locked_signals": {}, "settlement_receipts": {},
            "last_processed_identity": None, "last_processed_position": 0,
            "last_dominance_state": "INSUFFICIENT_DATA", "events": []}


def validate_config(value: dict) -> dict:
    value = value or {}
    config = {**DEFAULTS, **value}
    config["part1"] = {**DEFAULTS["part1"], **(value.get("part1") or {})}
    config["part2"] = {**DEFAULTS["part2"], **(value.get("part2") or {})}
    if "cooldown_verified_rounds" in value:
        config["new_series_cooldown_rounds"] = value["cooldown_verified_rounds"]
    elif "new_series_cooldown_rounds" in value:
        config["cooldown_verified_rounds"] = value["new_series_cooldown_rounds"]
    else:
        config["cooldown_verified_rounds"] = config["new_series_cooldown_rounds"]
    if "part2_pause_limit_verified_rounds" in value:
        config["part2"]["pause_limit_verified_rounds"] = value["part2_pause_limit_verified_rounds"]
    if "pause_limit_verified_rounds" in value:
        config["part2"]["pause_limit_verified_rounds"] = value["pause_limit_verified_rounds"]
    config["pause_limit_verified_rounds"] = config["part2"]["pause_limit_verified_rounds"]

    if config.get("strategy_mode") not in {"HIGH_DOMINANCE_ONLY", "PART1_ONLY", "PART1_AND_PART2"}:
        raise ValueError("strategy_mode must be HIGH_DOMINANCE_ONLY, PART1_ONLY, or PART1_AND_PART2")
    if config.get("on_win_limit_reached") != "WAIT_FOR_NEXT_FRESH_TRIGGER":
        raise ValueError("on_win_limit_reached must be WAIT_FOR_NEXT_FRESH_TRIGGER")
    if config.get("shadow_only") is not True:
        raise ValueError("shadow_only must remain true")

    def integer(raw, key: str, low: int, high: int) -> int:
        if isinstance(raw, bool):
            raise ValueError(f"{key} must be an integer")
        try:
            parsed = int(raw)
        except (TypeError, ValueError):
            raise ValueError(f"{key} must be an integer") from None
        if str(raw).strip() not in {str(parsed), f"+{parsed}"} and not isinstance(raw, int):
            raise ValueError(f"{key} must be an integer")
        if parsed < low or parsed > high:
            raise ValueError(f"{key} must be between {low} and {high}")
        return parsed

    for key, low, high in (("trigger_horizon", 5, 5),
                           ("pause_limit_verified_rounds", 1, 10000),
                           ("new_series_cooldown_rounds", 1, 10000)):
        config[key] = integer(config[key], key, low, high)
    config["cooldown_verified_rounds"] = config["new_series_cooldown_rounds"]
    config["part2"]["pause_limit_verified_rounds"] = config["pause_limit_verified_rounds"]
    for key in ("enabled", "net_win_limit_enabled"):
        for part in ("part1", "part2"):
            if not isinstance(config[part].get(key), bool):
                raise ValueError(f"{part}.{key} must be a boolean")
    for part, maximum_key in (("part1", "max_counted_rounds"),
                              ("part2", "max_counted_rounds_per_batch")):
        maximum = integer(config[part].get(maximum_key), f"{part}.{maximum_key}", 1, 1000)
        target = integer(config[part].get("net_win_target"), f"{part}.net_win_target", 1, 1000)
        if target > maximum:
            raise ValueError(f"{part}.net_win_target must not exceed {maximum_key}")
        config[part][maximum_key] = maximum
        config[part]["net_win_target"] = target
    if config["strategy_mode"] in {"PART1_ONLY", "PART1_AND_PART2"} and not config["part1"]["enabled"]:
        raise ValueError("part1 must be enabled for this strategy mode")
    config["configuration_version"] = integer(config.get("configuration_version", 1),
                                                "configuration_version", 1, 2_147_483_647)
    timeout = config.get("pause_timeout_seconds")
    if timeout in (None, "", 0, "0"):
        config["pause_timeout_seconds"] = None
    else:
        try:
            timeout = int(timeout)
        except (TypeError, ValueError):
            raise ValueError("pause_timeout_seconds must be a positive integer or disabled") from None
        if timeout < 1 or timeout > 31536000:
            raise ValueError("pause_timeout_seconds must be between 1 and 31536000")
        config["pause_timeout_seconds"] = timeout
    return config


def _event(state: dict, name: str, row: dict, now: str) -> None:
    state["events"].append({"type": name, "round_identity": row.get("identity_key"),
                            "round_id": row.get("round_id"), "position": row.get("position"),
                            "timestamp": now})
    state["events"] = state["events"][-500:]


def _finish(state: dict, reason: str, row: dict, now: str) -> None:
    if not state.get("series") or state["series"].get("stop_reason"):
        return
    series = state["series"]
    series["stop_reason"] = reason
    series["terminal_state"] = reason
    part2 = series.get("part2") or {}
    if part2.get("pause_started_at"):
        series["last_pause_reason"] = part2.get("pause_reason")
        part2.setdefault("pause_intervals", []).append({
            "reason": part2.get("pause_reason"),
            "start_round_identity": part2.get("pause_started_round_identity"),
            "start_timestamp": part2.get("pause_started_at"),
            "end_round_identity": row.get("identity_key"), "end_timestamp": now,
            "verified_rounds": part2.get("pause_count", 0), "terminal_reason": reason})
        part2["pause_started_at"] = None
    series["terminal_round_identity"] = row.get("identity_key")
    series["terminal_at"] = now
    series["current_part"] = series.get("current_part") or (
        "PART2" if series.get("part2") else "PART1")
    state["state"] = "COOLDOWN"
    state["cooldown_progress"] = 0
    state["cooldown_started_at"] = now
    state["fresh_high_exit_seen"] = False
    _event(state, reason, row, now)
    _event(state, "COOLDOWN_STARTED", row, now)


def _new_counter(config: dict, part: str) -> dict:
    part_config = config[part]
    limit_key = ("net_win_target" if part == "part1" else "net_win_target")
    return {"rounds": [], "wins": 0, "losses": 0, "win_count": 0,
            "loss_count": 0, "confirmed_win_count": 0, "counted_rounds": 0,
            "net_profit": 0.0, "remaining_win_target": part_config[limit_key],
            "target": part_config[limit_key]}


def _settle(container: dict, row: dict, *, signal_executed: bool = False,
            signal_settlement: dict | None = None) -> dict | None:
    """Count only a settled SHADOW signal; observation-only rounds are not results."""
    container.setdefault("rounds", [])
    container.setdefault("wins", 0)
    container.setdefault("losses", 0)
    container.setdefault("win_count", int(container.get("wins") or 0))
    container.setdefault("loss_count", int(container.get("losses") or 0))
    container.setdefault("confirmed_win_count", int(container.get("wins") or 0))
    container.setdefault("counted_rounds", len(container["rounds"]))
    container.setdefault("net_profit", 0.0)
    receipt = signal_settlement if isinstance(signal_settlement, dict) else {}
    result = str(receipt.get("result") or "").upper()
    if (not signal_executed or result not in {"WIN", "LOSS"}
            or receipt.get("settlement_status") != "COMMITTED"
            or receipt.get("signal_lock_status") not in {"DURABLY_LOCKED", "EXISTING_DURABLE_SIGNAL"}
            or not receipt.get("signal_id")
            or str(receipt.get("target_round_identity") or "") != str(row.get("identity_key") or "")):
        return None
    item = {"identity_key": row["identity_key"], "round_id": row.get("round_id"),
            "position": row.get("position"), "multiplier": str(row["multiplier"]),
            "outcome": row.get("outcome"), "result": result,
            "signal_id": receipt.get("signal_id"),
            "settlement_status": receipt.get("settlement_status"),
            "execution_status": "LOCKED_SIGNAL_SETTLED",
            "net_profit": receipt.get("net_profit"),
            "timestamp": row.get("timestamp")}
    item["counted_round"] = len(container["rounds"]) + 1
    container["rounds"].append(item)
    container["wins"] += int(signal_executed and result == "WIN")
    container["losses"] += int(signal_executed and result == "LOSS")
    container["win_count"] = container["wins"]
    container["loss_count"] = container["losses"]
    container["confirmed_win_count"] = container["wins"]
    container["counted_rounds"] = len(container["rounds"])
    if item["net_profit"] is not None:
        try:
            container["net_profit"] = float(Decimal(str(container.get("net_profit") or 0))
                                             + Decimal(str(item["net_profit"])))
        except Exception:
            item["net_profit"] = None
    return item


def _series_config(state: dict, series: dict | None) -> dict:
    config = (series or {}).get("configuration_snapshot")
    if isinstance(config, dict):
        return validate_config(config)
    return validate_config(state.get("config") or DEFAULTS)


def _activate_pending_config(state: dict) -> None:
    pending = state.get("pending_config")
    if isinstance(pending, dict):
        state["config"] = validate_config(pending)
        state["pending_config"] = None
        state["pending_configuration_version"] = None


def _start_series(state: dict, row: dict, position: int, stamp: str, *, mode: str) -> None:
    _activate_pending_config(state)
    config = validate_config(state.get("config") or DEFAULTS)
    version = int(config.get("configuration_version") or 1)
    series = {"series_id": str(uuid4()), "strategy_mode": mode,
        "configuration_version": version, "configuration_snapshot": deepcopy(config),
        "trigger_round_identity": row.get("identity_key"),
        "trigger_round_id": row.get("round_id"), "trigger_position": position,
        "trigger_timestamp": stamp, "trigger_horizon": config["trigger_horizon"],
        "current_part": "HIGH_DOMINANCE_ONLY" if mode == "HIGH_DOMINANCE_ONLY" else "PART1",
        "part1": _new_counter(config, "part1"), "part2": None,
        "high_dominance_only": _new_counter(config, "part1")}
    state["series"] = series
    state["state"] = ("HIGH_DOMINANCE_ONLY_ACTIVE" if mode == "HIGH_DOMINANCE_ONLY"
                       else "PART1_COLLECTING")
    state["cooldown_progress"] = 0
    state["fresh_high_exit_seen"] = False
    _event(state, "HIGH_EMERGENCE_DETECTED" if mode == "HIGH_DOMINANCE_ONLY"
           else "PART1_STARTED", row, stamp)


def _apply_win_target(state: dict, part: str, counter: dict, row: dict, stamp: str) -> bool:
    series = state.get("series") or {}
    config = _series_config(state, series)
    part_config = config[part]
    target = int(part_config["net_win_target"])
    counter["target"] = target
    counter["remaining_win_target"] = max(0, target - int(counter.get("wins") or 0))
    if not part_config["net_win_limit_enabled"] or counter["wins"] < target:
        return False
    reason = "PART1_WIN_TARGET_REACHED" if part == "part1" else "PART2_WIN_TARGET_REACHED"
    _finish(state, reason, row, stamp)
    return True


def process_round(saved: dict | None, *, row: dict, previous_state: str,
                  current_state: str, emergence: bool, now: datetime | None = None,
                  data_fresh: bool = True, signal_executed: bool = False,
                  signal_settlement: dict | None = None,
                  settlement_pending: bool = False,
                  pending_signal: dict | None = None,
                  allow_trigger: bool = True,
                  count_cooldown: bool | None = None) -> dict:
    """Apply one committed verified round. Eligibility is locked before state update."""
    state = deepcopy(saved or new_state())
    state["config"] = validate_config(state.get("config") or DEFAULTS)
    state.setdefault("pending_config", None)
    state.setdefault("fresh_high_exit_seen", False)
    state.setdefault("cooldown_progress", 0)
    # Existing active series retain their old rules until completion. Applying
    # newly selected defaults to a pre-feature series would change it in place.
    legacy_series = state.get("series")
    if legacy_series and "configuration_snapshot" not in legacy_series:
        legacy_config = deepcopy(state["config"])
        legacy_config["part1"]["net_win_limit_enabled"] = False
        legacy_config["part2"]["net_win_limit_enabled"] = False
        legacy_series["configuration_snapshot"] = legacy_config
        legacy_series["configuration_version"] = 0
        legacy_series.setdefault("strategy_mode", "PART1_AND_PART2")
    _repair_completed_current_batch(state)
    identity = row.get("identity_key")
    position = int(row.get("position") or 0)
    if not identity or (state.get("last_processed_identity") == identity
                        or position <= int(state.get("last_processed_position") or 0)):
        return state
    now = now or datetime.now(timezone.utc)
    stamp = now.isoformat()
    series = state.get("series")
    state["data_health"] = "HEALTHY" if data_fresh else "DATA_STALE"
    if not data_fresh:
        return state

    # State at entry determines eligibility; this round can only affect future rounds.
    entry_state = state["state"]
    config = _series_config(state, series)
    valid_settlement = (signal_executed and isinstance(signal_settlement, dict)
            and signal_settlement.get("settlement_status") == "COMMITTED"
            and signal_settlement.get("signal_lock_status") in {
                "DURABLY_LOCKED", "EXISTING_DURABLE_SIGNAL"}
            and str(signal_settlement.get("result") or "").upper() in {"WIN", "LOSS"}
            and signal_settlement.get("signal_id")
            and str(signal_settlement.get("target_round_identity") or "") == str(identity))
    if valid_settlement:
        receipts = state.setdefault("settlement_receipts", {})
        signal_id = str(signal_settlement["signal_id"])
        prior_receipt = receipts.get(signal_id)
        if prior_receipt:
            if prior_receipt.get("target_round_identity") != identity:
                state["state"] = "UNSAFE_STATE"
                state["data_health"] = "DATA_STALE"
                state["recovery_quarantine_reason"] = "SHADOW_SETTLEMENT_IDENTITY_CONFLICT"
            return state
        receipts = state.setdefault("settlement_receipts", {})
        receipts[signal_id] = {
            "result": str(signal_settlement.get("result") or "").upper(),
            "series_id": (series or {}).get("series_id"),
            "part": (series or {}).get("current_part"),
            "target_round_identity": identity,
            "received_at": stamp,
        }
        if len(receipts) > 10000:
            for key in list(receipts)[:len(receipts) - 8000]:
                receipts.pop(key, None)
    if entry_state == "WAITING_FOR_SHADOW_SETTLEMENT":
        deferred = state.setdefault("deferred_verified_rounds", [])
        if len(deferred) >= 20000:
            state["state"] = "UNSAFE_STATE"
            state["data_health"] = "DATA_STALE"
            state["recovery_quarantine_reason"] = "SHADOW_SETTLEMENT_DEFERRED_BACKLOG_LIMIT"
            return state
        deferred.append({"row": deepcopy(row), "previous_state": previous_state,
                         "current_state": current_state,
                         "emergence": bool(emergence), "timestamp": stamp})
        state["deferred_verified_rounds"] = deferred
        state["last_dominance_state"] = current_state
        state["last_processed_identity"] = identity
        state["last_processed_position"] = position
        state["data_health"] = "WAITING_FOR_SHADOW_SETTLEMENT"
        return state
    if settlement_pending and entry_state in {
            "PART1_COLLECTING", "PART2_ACTIVE", "HIGH_DOMINANCE_ONLY_ACTIVE"}:
        state["settlement_resume_state"] = entry_state
        state["settlement_wait"] = {"identity_key": identity, "round_id": row.get("round_id"),
            "row": deepcopy(row), "previous_state": previous_state,
            "emergence": bool(emergence),
            "position": position, "outcome": row.get("outcome"),
            "current_state": current_state, "timestamp": stamp,
            "signal_id": str((pending_signal or {}).get("signal_id") or ""),
            "previous_identity": state.get("last_processed_identity"),
            "previous_position": int(state.get("last_processed_position") or 0)}
        state["state"] = "WAITING_FOR_SHADOW_SETTLEMENT"
        state["last_dominance_state"] = current_state
        state["last_processed_identity"] = identity
        state["last_processed_position"] = position
        state["data_health"] = "WAITING_FOR_SHADOW_SETTLEMENT"
        return state

    if entry_state == "PART1_COLLECTING" and series:
        part1_config = config["part1"]
        counted = _settle(series["part1"], row, signal_executed=signal_executed,
                          signal_settlement=signal_settlement)
        part1 = series["part1"]
        part1["remaining_win_target"] = max(0, int(part1_config["net_win_target"]) - part1["wins"])
        target_reached = counted and _apply_win_target(state, "part1", part1, row, stamp)
        if not target_reached and part1["counted_rounds"] >= int(part1_config["max_counted_rounds"]):
            series["part1"]["completed_at"] = stamp
            series["part1"]["state_after_r5"] = current_state
            _event(state, "PART1_COMPLETED", row, stamp)
            can_enter_part2 = (config["strategy_mode"] == "PART1_AND_PART2"
                               and config["part2"]["enabled"])
            if current_state == "HIGH_DOMINANT" and can_enter_part2:
                state["state"] = "PART1_COMPLETED"
                series["current_part"] = "PART2"
                series["part2"] = {"batch_number": 1, "batches": [], "current_batch":
                    _new_counter(config, "part2"), "pause_count": 0,
                    "pause_reason": None, "pause_started_round_identity": None,
                    "pause_started_at": None, "pause_intervals": [], "total_pause_rounds": 0,
                    "resume_count": 0}
                state["state"] = "PART2_ACTIVE"
                _event(state, "PART2_STARTED", row, stamp)
                # Dominance at R5 decides entry. The same completed R5 result
                # then gates the first Part 2 target; a LOW R5 enters Part 2
                # paused without counting R5 as a Part 2 settlement.
                if str(row.get("outcome") or "") != "H":
                    _pause(series, "LOW_RESULT", row, stamp, state)
            elif config["strategy_mode"] == "PART1_ONLY" or not config["part2"]["enabled"]:
                _finish(state, "STOPPED_PART1_COMPLETED", row, stamp)
            elif current_state in {"LOW_DOMINANT", "BALANCED", "TRANSITION"}:
                _finish(state, "STOPPED_PART1_NO_HIGH", row, stamp)
            else:
                state["state"] = "PART1_COMPLETED"
                series["part1"]["completion_pending"] = True
                series["part1"]["completion_status"] = "DATA_INCOMPLETE"
                state["data_health"] = "DATA_STALE"
    elif entry_state == "PART1_COMPLETED" and series and series["part1"].get("completion_pending"):
        # Do not substitute a later rolling state for the unavailable R5 state.
        state["data_health"] = "DATA_STALE"
    elif entry_state == "PART2_ACTIVE" and series:
        # This outcome was eligible under the previously active decision.
        batch = series["part2"]["current_batch"]
        part2_config = config["part2"]
        counted = _settle(batch, row, signal_executed=signal_executed,
                          signal_settlement=signal_settlement)
        batch["remaining_win_target"] = max(0, int(part2_config["net_win_target"]) - batch["wins"])
        target_reached = counted and _apply_win_target(state, "part2", batch, row, stamp)
        if not target_reached:
            # Part 2 is gated by the actual result of the latest completed round.
            # Dominance remains relevant only for the Part 1 R5 entry decision.
            last_was_high = str(row.get("outcome") or "") == "H"
            if batch["counted_rounds"] >= int(part2_config["max_counted_rounds_per_batch"]):
                batch["completed_at"] = stamp
                batch["state_after_r5"] = current_state
                series["part2"]["batches"].append(batch)
                _event(state, "PART2_BATCH_COMPLETED", row, stamp)
                series["part2"]["batch_number"] += 1
                series["part2"]["current_batch"] = None
                if last_was_high:
                    series["part2"]["current_batch"] = _new_counter(config, "part2")
                else:
                    # Close this batch; the next numbered batch opens only when resumed.
                    _pause(series, "LOW_RESULT", row, stamp, state)
            elif not last_was_high:
                _pause(series, "LOW_RESULT", row, stamp, state)
    elif entry_state in {"PART2_PAUSED_LOW_DOMINANT", "PART2_PAUSED_NON_HIGH"} and series:
        part2 = series["part2"]
        part2["pause_count"] += 1
        part2["total_pause_rounds"] += 1
        # On the exact limit, HIGH return is evaluated before expiration.
        if str(row.get("outcome") or "") == "H":
            part2["pause_intervals"].append({"reason": part2["pause_reason"],
                "start_round_identity": part2["pause_started_round_identity"],
                "start_timestamp": part2["pause_started_at"],
                "end_round_identity": identity, "end_timestamp": stamp,
                "verified_rounds": part2["pause_count"]})
            part2["resume_count"] += 1
            part2["pause_count"] = 0
            part2["pause_reason"] = None
            part2["pause_started_round_identity"] = None
            part2["pause_started_at"] = None
            if part2.get("current_batch") is None:
                part2["current_batch"] = _new_counter(config, "part2")
            state["state"] = "PART2_ACTIVE"
            _event(state, "PART2_RESUMED_HIGH", row, stamp)
        elif part2["pause_count"] >= config["pause_limit_verified_rounds"]:
            _finish(state, "STOPPED_PAUSE_LIMIT", row, stamp)
    elif entry_state == "HIGH_DOMINANCE_ONLY_ACTIVE" and series:
        _settle(series["high_dominance_only"], row,
                signal_executed=signal_executed,
                signal_settlement=signal_settlement)
        if current_state != "HIGH_DOMINANT":
            _finish(state, "STOPPED_HIGH_DOMINANCE_EXIT", row, stamp)
    elif entry_state == "COOLDOWN":
        cooldown_observed = allow_trigger if count_cooldown is None else count_cooldown
        if cooldown_observed:
            state["cooldown_progress"] += 1
        cooldown = int(config["cooldown_verified_rounds"])
        if state["cooldown_progress"] >= cooldown:
            state["cooldown_progress"] = cooldown
            state["state"] = "WAITING_FOR_FRESH_TRIGGER"
            state["fresh_high_exit_seen"] = current_state != "HIGH_DOMINANT"
            if state.get("series"):
                archived = deepcopy(state["series"])
                archived["archived_at"] = stamp
                state.setdefault("completed_series", []).append(archived)
            state["series"] = None
            _activate_pending_config(state)
            _event(state, "COOLDOWN_COMPLETED", row, stamp)
    elif entry_state in {"IDLE", "ARMED_FOR_HIGH", "WAITING_FOR_FRESH_TRIGGER"}:
        if current_state != "HIGH_DOMINANT":
            state["fresh_high_exit_seen"] = True
            if entry_state == "IDLE":
                state["state"] = "WAITING_FOR_FRESH_TRIGGER"
        elif (allow_trigger and emergence and
              (entry_state in {"IDLE", "ARMED_FOR_HIGH"}
               or state.get("fresh_high_exit_seen", False))):
            mode = config["strategy_mode"]
            _start_series(state, row, position, stamp, mode=mode)
    state["last_dominance_state"] = current_state
    state["last_processed_identity"] = identity
    state["last_processed_position"] = position
    return state


def prepare_strategy_eligibility(machine: dict, row: dict, *, decision_timestamp: str | None = None) -> dict | None:
    """Expose current eligibility; persist an event only when a HIGH is eligible."""
    if not row.get("identity_key") or not row.get("round_id"):
        return None
    series = machine.get("series") or {}
    state_name = str(machine.get("state") or "IDLE")
    part = ("PART1" if state_name == "PART1_COLLECTING" else
            "PART2" if state_name == "PART2_ACTIVE" else
            "HIGH_DOMINANCE_ONLY" if state_name == "HIGH_DOMINANCE_ONLY_ACTIVE" else None)
    part2 = series.get("part2") or {}
    batch_id = ("PART1-001" if part == "PART1" else
                f"PART2-{int(part2.get('batch_number') or 1):03d}" if part == "PART2" else
                "HIGH-001" if part == "HIGH_DOMINANCE_ONLY" else None)
    decision_identity = str(row["identity_key"])
    eligible = (state_name in {"PART1_COLLECTING", "PART2_ACTIVE", "HIGH_DOMINANCE_ONLY_ACTIVE"}
                and machine.get("data_health") not in {"DATA_STALE", "WAITING_FOR_SHADOW_SETTLEMENT"}
                and (part != "HIGH_DOMINANCE_ONLY" or
                     machine.get("last_dominance_state") == "HIGH_DOMINANT"))
    if machine.get("data_health") == "WAITING_FOR_SHADOW_SETTLEMENT" or state_name == "WAITING_FOR_SHADOW_SETTLEMENT":
        reason = "SHADOW_SETTLEMENT_PENDING"
    elif machine.get("data_health") == "DATA_STALE":
        reason = "BEHAVIOUR_DATA_STALE"
    elif state_name == "PART2_PAUSED_LOW_DOMINANT":
        reason = "PART2_PAUSED_LOW_RESULT"
    elif state_name == "PART2_PAUSED_PAUSE_LIMIT":
        reason = "PART2_PAUSE_LIMIT_REACHED"
    elif eligible and part == "PART1":
        reason = "PART1_ROUND_REMAINS"
    elif eligible and part == "PART2":
        reason = "PART2_LAST_VERIFIED_RESULT_HIGH"
    elif eligible:
        reason = "HIGH_DOMINANCE_CURRENTLY_QUALIFIES"
    elif not series:
        reason = "NO_ACTIVE_SERIES"
    else:
        reason = str(series.get("stop_reason") or state_name)
    eligibility_id = "behaviour-eligibility-" + hashlib.sha256("\0".join((
        str(series.get("series_id") or ""), str(part or ""), str(batch_id or ""), decision_identity)).encode()).hexdigest()
    previous = machine.get("latest_strategy_eligibility")
    if (isinstance(previous, dict) and previous.get("eligibility_id") == eligibility_id
            and previous.get("eligibility") == ("ELIGIBLE" if eligible else "INELIGIBLE")
            and previous.get("eligibility_reason") == reason):
        return previous
    stamp = decision_timestamp or datetime.now(timezone.utc).isoformat()
    item = {
        "eligibility_id": eligibility_id, "strategy_id": "ROLLING_HIGH",
        "source_id": "BEHAVIOUR_DETECTION", "series_id": str(series.get("series_id") or "") or None,
        "part": part, "batch_id": batch_id,
        "configuration_version": int(series.get("configuration_version") or
                                      (machine.get("config") or {}).get("configuration_version") or 1),
        "trigger_round_identity": str(series.get("trigger_round_identity") or "") or None,
        "decision_round_identity": decision_identity,
        "source_round_id": str(row["round_id"]),
        "target_sequence_boundary": {"after_identity": decision_identity,
                                     "cohort_position": int(row.get("position") or 0) + 1},
        "direction": "HIGH", "decision_timestamp": stamp,
        "trigger_horizon": int(series.get("trigger_horizon") or
                                (machine.get("config") or {}).get("trigger_horizon") or 5),
        "policy_version": str(machine.get("policy_version") or POLICY_VERSION),
        "verified_data_freshness": {"verified": True, "data_health": machine.get("data_health", "HEALTHY")},
        "current_dominance_state": machine.get("last_dominance_state"),
        "eligibility_reason": reason,
        "eligibility": "ELIGIBLE" if eligible else "INELIGIBLE",
        "status": "ELIGIBLE" if eligible else "UNPUBLISHED",
        "created_at": stamp,
        "attempt_count": 0, "response": None,
    }
    machine["latest_strategy_eligibility"] = deepcopy(item)
    if eligible:
        machine.setdefault("events", []).append({
            "type": "HIGH_STRATEGY_ELIGIBLE", "round_identity": decision_identity,
            "round_id": row.get("round_id"), "position": row.get("position"),
            "timestamp": item["created_at"], "eligibility_id": eligibility_id,
        })
        machine["events"] = machine["events"][-500:]
    return item


def _repair_completed_current_batch(state: dict) -> None:
    """Normalize snapshots made by the earlier full-batch pause transition."""
    series = state.get("series") or {}
    part2 = series.get("part2") or {}
    current = part2.get("current_batch") or {}
    if not current.get("completed_at"):
        return
    current_ids = tuple(item.get("identity_key") for item in current.get("rounds", []))
    completed_batches = {tuple(item.get("identity_key") for item in batch.get("rounds", []))
                         for batch in part2.get("batches", [])}
    if current_ids and current_ids in completed_batches:
        part2["current_batch"] = None


def _pause(series: dict, reason: str, row: dict, stamp: str, state: dict) -> None:
    part2 = series["part2"]
    part2["pause_reason"] = reason
    part2["pause_started_round_identity"] = row.get("identity_key")
    part2["pause_started_at"] = stamp
    part2["pause_count"] = 0
    state["state"] = ("PART2_PAUSED_NON_HIGH" if reason == "LOW_RESULT"
                       else "PART2_PAUSED_LOW_DOMINANT" if reason == "LOW_DOMINANT"
                       else "PART2_PAUSED_NON_HIGH")
    _event(state, state["state"], row, stamp)

