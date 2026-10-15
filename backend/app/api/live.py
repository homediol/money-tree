from __future__ import annotations

import asyncio
import re
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

router = APIRouter(prefix="/api/live", tags=["live activation"])
LIVE_MANUAL_SETUP_CONFIGURATION_VERSION = 1


def live(request: Request):
    return request.app.state.live


def _live_readiness_configuration(modes) -> dict:
    """Read only the operator-selected executor panel; stake is platform-owned."""
    read_configuration = getattr(modes, "live_configuration", None)
    configuration = read_configuration() if callable(read_configuration) else None
    if not isinstance(configuration, dict) or configuration.get("configuration_valid") is not True:
        return {}
    return {
        "execution_panel": configuration.get("execution_panel"),
        "configuration_source": configuration.get("persistence_source"),
    }


@router.post("/readiness")
async def readiness(request: Request):
    modes = request.app.state.betting_mode
    observation = await modes._read_platform_balance()
    return await live(request).production_readiness_check(
        observation, _live_readiness_configuration(modes))


@router.get("/readiness")
async def readiness_read_only(request: Request):
    """Fresh read-only readiness check; does not authorize or start a session."""
    modes = request.app.state.betting_mode
    observation = await modes._read_platform_balance()
    return await live(request).production_readiness_check(
        observation, _live_readiness_configuration(modes))


@router.get("/balance")
async def balance(request: Request):
    """Read current platform balance from the already-owned CDP context."""
    try:
        observation = await request.app.state.betting_mode._read_platform_balance()
    except asyncio.CancelledError:
        task = asyncio.current_task()
        if task is not None and task.cancelling():
            raise
        observation = {
            "verified": False, "balance": None, "currency": None,
            "session_identity": None, "observed_at": datetime.now(timezone.utc).isoformat(),
            "source": "existing_cdp_page_read_only", "error": "browser_balance_snapshot_cancelled",
        }
    except Exception as exc:
        observation = {
            "verified": False, "balance": None, "currency": None,
            "session_identity": None, "observed_at": datetime.now(timezone.utc).isoformat(),
            "source": "existing_cdp_page_read_only", "error": str(exc) or "balance_read_failed",
        }
    if not isinstance(observation, dict) or observation.get("verified") is not True:
        value = observation if isinstance(observation, dict) else {
            "verified": False, "balance": None, "currency": None,
            "error": "balance_observation_unavailable",
        }
        return JSONResponse(status_code=503, content={"ok": False, "platform_balance": value})
    return {"ok": True, "platform_balance": observation}


def _readback_age_seconds(value: dict | None, *, now: datetime) -> float | None:
    if not isinstance(value, dict) or not value.get("observed_at"):
        return None
    try:
        stamp = datetime.fromisoformat(str(value["observed_at"]).replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            return None
        return (now - stamp.astimezone(timezone.utc)).total_seconds()
    except (TypeError, ValueError, OverflowError):
        return None


def _manual_setup_configuration(request: Request) -> dict:
    """Resolve only the Executor target panel. Stake/cashout remain operator-owned."""
    modes = getattr(request.app.state, "betting_mode", None)
    read_configuration = getattr(modes, "live_configuration", None)
    saved = read_configuration() if callable(read_configuration) else None
    panel = saved.get("execution_panel") if isinstance(saved, dict) else None
    if type(panel) is not int or panel not in (0, 1):
        return {"status": "BLOCKED", "reason": "EXECUTION_PANEL_NOT_SELECTED",
                "session_id": None, "execution_panel": None,
                "configuration_source": "LIVE_EXECUTOR_CONFIGURATION",
                "automatic_execution": "OFF"}
    return {
        "status": "CONFIGURED", "reason": None, "session_id": None,
        "execution_panel": panel,
        "configuration_source": "LIVE_EXECUTOR_CONFIGURATION",
        "automatic_execution": "OFF",
    }


def _manual_setup_observation(snapshot, configuration: dict, previous: dict | None = None) -> tuple[dict, dict]:
    from app.betting.cashout import inspect_cashout_configuration
    from app.live import MANUAL_SETUP_READBACK_MAX_AGE_SECONDS

    current_time = datetime.now(timezone.utc)
    observed_at = current_time.isoformat()
    panel_slot = configuration.get("execution_panel")
    panel_states = {row.get("slot"): row for row in (snapshot.bet_panel_states if snapshot and snapshot.ok else [])}
    frame_identity = next((row.get("frame_identity") for row in panel_states.values()
                          if row.get("frame_identity")), None)
    auth_ok = bool(snapshot and snapshot.authentication_state == "AUTHENTICATED")
    page_ok = bool(snapshot and snapshot.page_role == "SHARED_AVIATOR"
                   and snapshot.browser_topology_verified is True)
    if (not auth_ok or not page_ok or not frame_identity
            or not isinstance(previous, dict)
            or previous.get("readback_version") != 2
            or previous.get("frame_identity") != frame_identity):
        previous = {}
    else:
        previous = dict(previous)

    panel = next((row for row in snapshot.bet_panel_states
                  if row.get("slot") == panel_slot), None) if snapshot and snapshot.ok else None
    stake_input = next((row for row in snapshot.stake_inputs
                        if row.get("slot") == panel_slot), None) if snapshot and snapshot.ok else None
    button_amount = None
    button_text = panel.get("button_text") if panel else None
    if button_text:
        match = re.search(r"^Bet\s+([\d,]+(?:\.\d+)?)", str(button_text), re.IGNORECASE)
        if match:
            try:
                button_amount = float(match.group(1).replace(",", ""))
            except ValueError:
                button_amount = None
    expected_stake = None
    expected_currency = None
    observed_stake = None
    if stake_input and stake_input.get("value") not in (None, ""):
        try:
            observed_stake = float(stake_input["value"])
        except (TypeError, ValueError):
            observed_stake = None
    observed_currency = (str(panel.get("currency_hint") or snapshot.balance_currency or "").upper()
                         if panel and snapshot else "") or None
    account_currency = str(snapshot.balance_currency or "").upper() if snapshot else ""
    pending_states = [row.get("pending_bet_state", "UNKNOWN") for row in panel_states.values()]
    all_clear = len(pending_states) == 2 and all(state == "CLEAR" for state in pending_states)
    # A currently visible stake input and its associated button label are
    # sufficient read-only evidence even when Auto is the selected tab. Do
    # not require the operator to switch tabs just to verify the value.
    panel_evidence = panel.get("evidence") if isinstance(panel, dict) else {}
    button_visible = (isinstance(panel, dict) and panel.get("button_visible") is True
                      or isinstance(panel_evidence, dict)
                      and panel_evidence.get("button_visible") is True)
    direct_stake_read = bool(auth_ok and page_ok and panel
                             and button_visible
                             and stake_input and stake_input.get("visible") is True and all_clear)
    cache_matches = (previous.get("execution_panel") == panel_slot
                     and previous.get("account_currency") == account_currency)
    cached_stake = previous.get("stake") if cache_matches else None
    cached_stake_age = _readback_age_seconds(cached_stake, now=current_time)
    if direct_stake_read:
        if (observed_stake is None or button_amount is None or not account_currency or not observed_currency):
            stake_status = "UNAVAILABLE"
        elif (observed_stake.is_integer() and observed_stake == button_amount
              and observed_currency == account_currency == "BIF"):
            stake_status = "STAKE_VERIFIED"
        else:
            stake_status = "STAKE_MISMATCH"
        stake_readback = {
            "status": stake_status, "observed_stake": observed_stake,
            "displayed_button_stake": button_amount, "button_text": panel.get("button_text"),
            "observed_currency": observed_currency,
            "minimum_stake": stake_input.get("minAttribute"), "observed_at": observed_at,
            "frame_identity": frame_identity, "source": "SHARED_AVIATOR_FRAME_READ_ONLY",
        }
    elif (all_clear and panel_slot in (0, 1) and cached_stake_age is not None
          and -5 <= cached_stake_age <= MANUAL_SETUP_READBACK_MAX_AGE_SECONDS):
        stake_readback = {**cached_stake, "readback_age_seconds": round(max(0.0, cached_stake_age), 3),
                          "source": "CACHED_READ_ONLY_PLATFORM_READBACK"}
        stake_status = stake_readback["status"]
        observed_stake = stake_readback.get("observed_stake")
        button_amount = stake_readback.get("displayed_button_stake")
        button_text = stake_readback.get("button_text")
        observed_currency = stake_readback.get("observed_currency")
    else:
        stake_readback = None
        stake_status = "UNAVAILABLE"

    next_readbacks = {
        "readback_version": 2, "frame_identity": frame_identity, "execution_panel": panel_slot,
        "account_currency": account_currency, "cashout_panels": {},
        "stake": stake_readback if direct_stake_read else cached_stake if stake_readback else None,
    }

    controls = {row.get("slot"): row for row in (snapshot.cashout_controls if snapshot and snapshot.ok else [])}
    inspected_panels = []
    for slot in (0, 1):
        control = controls.get(slot)
        panel_state = panel_states.get(slot) or {}
        inspected = inspect_cashout_configuration(control)
        auto_bet_off = bool(control and control.get("autoBetEnabled") is False)
        direct_cashout_read = bool(auth_ok and page_ok and control and control.get("autoTabSelected") is True)
        # Auto-tab values are a read-only configuration observation. A queued
        # or active bet blocks execution readiness, but it must not erase a
        # valid settings readback; that separate blocker is reported below in
        # the pending-bet checklist and panel state.
        if direct_cashout_read:
            if (control.get("inputVisible") is not True or inspected.get("enabled") is None
                    or control.get("autoBetEnabled") is None or inspected.get("displayed_value") is None):
                status = "CASHOUT_UNAVAILABLE"
            elif inspected.get("configuration_valid") and auto_bet_off:
                status = "CASHOUT_VERIFIED"
            else:
                status = "CASHOUT_MISMATCH"
            cashout_readback = {
                "panel_id": f"PANEL_{slot + 1}", "status": status,
                "auto_tab_selected": control.get("autoTabSelected"),
                "auto_bet": "OFF" if auto_bet_off else "ON" if control.get("autoBetEnabled") is True else "UNAVAILABLE",
                "auto_cashout": "ON" if inspected.get("enabled") is True else "OFF" if inspected.get("enabled") is False else "UNAVAILABLE",
                "multiplier": inspected.get("displayed_value"),
                "platform_acknowledged": inspected.get("platform_acknowledged"),
                "observed_at": observed_at, "frame_identity": frame_identity,
                "source": "SHARED_AVIATOR_FRAME_READ_ONLY",
            }
        else:
            cached = (previous.get("cashout_panels") or {}).get(str(slot))
            cached_age = _readback_age_seconds(cached, now=current_time)
            if (cached_age is not None and -5 <= cached_age <= MANUAL_SETUP_READBACK_MAX_AGE_SECONDS
                    and cached.get("frame_identity") == frame_identity):
                cashout_readback = {**cached, "readback_age_seconds": round(max(0.0, cached_age), 3),
                                    "source": "CACHED_READ_ONLY_PLATFORM_READBACK"}
                status = cashout_readback["status"]
            else:
                cashout_readback = None
                status = "CASHOUT_UNAVAILABLE"
        if not direct_cashout_read and cashout_readback:
            next_readbacks["cashout_panels"][str(slot)] = cached
        elif direct_cashout_read and cashout_readback:
            next_readbacks["cashout_panels"][str(slot)] = cashout_readback
        if cashout_readback:
            panel_row = {**cashout_readback,
                         "pending_bet_state": panel_state.get("pending_bet_state", "UNKNOWN"),
                         "failures": [] if status == "CASHOUT_VERIFIED" else list(inspected.get("failures", []))}
        else:
            panel_row = {
                "panel_id": f"PANEL_{slot + 1}", "status": status,
                "auto_tab_selected": control.get("autoTabSelected") if control else None,
                "auto_bet": "OFF" if auto_bet_off else "ON" if control and control.get("autoBetEnabled") is True else "UNAVAILABLE",
                "auto_cashout": "ON" if inspected.get("enabled") is True else "OFF" if inspected.get("enabled") is False else "UNAVAILABLE",
                "multiplier": inspected.get("displayed_value"), "platform_acknowledged": inspected.get("platform_acknowledged"),
                "observed_at": observed_at, "frame_identity": frame_identity,
                "source": "SHARED_AVIATOR_FRAME_READ_ONLY",
                "pending_bet_state": panel_state.get("pending_bet_state", "UNKNOWN"),
                "failures": (list(inspected.get("failures", []))
                    + ([] if panel_state.get("pending_bet_state") == "CLEAR"
                       else ["ACTIVE_OR_PENDING_BET" if panel_state.get("pending_bet_state") == "PENDING"
                             else "PENDING_BET_STATE_UNAVAILABLE"])),
            }
        inspected_panels.append(panel_row)

    balance_age = _readback_age_seconds(
        {"observed_at": getattr(snapshot, "balance_observed_at", None)} if snapshot else None,
        now=current_time)
    account_evidence_fresh = bool(
        snapshot and auth_ok and page_ok
        and balance_age is not None and 0 <= balance_age <= 20
        and getattr(snapshot, "provider_identity", None)
        and getattr(snapshot, "session_identity", None))
    currency_ok = bool(account_evidence_fresh and getattr(snapshot, "balance_currency", None))
    balance_ok = bool(account_evidence_fresh and getattr(snapshot, "balance", None) is not None
                      and getattr(snapshot, "balance_currency", None))
    # LIVE readiness requires both platform cashout panels configured. Make
    # the ten-item manual checklist use the same scope so it cannot report a
    # fully verified setup while the other panel is still unsafe.
    selected_cashouts = inspected_panels

    def all_panel_status(field: str, expected: str) -> str:
        values = [row.get(field) for row in selected_cashouts]
        if not values:
            return "UNAVAILABLE"
        if any(value not in (expected, "UNAVAILABLE", None) for value in values):
            return "MISMATCH"
        return "VERIFIED" if all(value == expected for value in values) else "UNAVAILABLE"

    cashout_enabled_status = all_panel_status("auto_cashout", "ON")
    multiplier_values = [row.get("multiplier") for row in selected_cashouts]
    multiplier_status = ("MISMATCH" if any(value is not None and value != "2.00" for value in multiplier_values)
                         else "VERIFIED" if multiplier_values and all(value == "2.00" for value in multiplier_values)
                         else "UNAVAILABLE")
    auto_bet_status = all_panel_status("auto_bet", "OFF")
    checklist = [
        {"item": "Browser authenticated", "status": "VERIFIED" if auth_ok else "UNAVAILABLE"},
        {"item": "Aviator game opened", "status": "VERIFIED" if page_ok else "UNAVAILABLE"},
        {"item": "Execution panel selected", "status": "VERIFIED" if panel_slot in (0, 1) else "REQUIRED"},
        {"item": "Stake manually entered", "status": stake_status},
        {"item": "Auto Cashout ON for both panels", "status": cashout_enabled_status},
        {"item": "Cashout multiplier 2.00x on both panels", "status": multiplier_status},
        {"item": "Auto Bet OFF on both panels", "status": auto_bet_status},
        {"item": "Account currency verified", "status": "VERIFIED" if currency_ok else "UNAVAILABLE"},
        {"item": "Balance verified", "status": "VERIFIED" if balance_ok else "UNAVAILABLE"},
        {"item": "No active or pending bet", "status": "VERIFIED" if all_clear else "BLOCKED" if "PENDING" in pending_states else "UNAVAILABLE"},
    ]
    return {
        "status": "VERIFIED" if all(item["status"] in {"VERIFIED", "STAKE_VERIFIED"}
                                     for item in checklist) else "REVIEW_REQUIRED",
        "observed_at": observed_at, "source": "SHARED_AVIATOR_FRAME_READ_ONLY",
        "authentication_state": snapshot.authentication_state if snapshot else "UNKNOWN",
        "page_role": snapshot.page_role if snapshot else "UNKNOWN",
        "browser_topology_verified": snapshot.browser_topology_verified is True if snapshot else False,
        "selected_panel": panel_slot + 1 if panel_slot in (0, 1) else None,
        "configuration_source": configuration.get("configuration_source"),
        "expected_stake": None, "expected_currency": None,
        "observed_stake": observed_stake, "displayed_button_stake": button_amount,
        "button_text": button_text, "observed_currency": observed_currency,
        "stake_readback_observed_at": stake_readback.get("observed_at") if stake_readback else None,
        "stake_readback_source": stake_readback.get("source") if stake_readback else None,
        "minimum_stake": stake_readback.get("minimum_stake") if stake_readback else stake_input.get("minAttribute") if stake_input else None,
        "account_balance": snapshot.balance if snapshot else None,
        "account_currency": snapshot.balance_currency if snapshot else None,
        "balance_observed_at": getattr(snapshot, "balance_observed_at", None) if snapshot else None,
        "balance_age_seconds": round(balance_age, 3) if balance_age is not None else None,
        "pending_bet_states": pending_states,
        "cashout_panels": inspected_panels, "checklist": checklist,
        "betting_controls": {
            "status": "READY" if (direct_stake_read and panel is not None
                                     and stake_status == "STAKE_VERIFIED" and all_clear) else
                "BLOCKED" if direct_stake_read and stake_status == "STAKE_MISMATCH" else "UNKNOWN",
            "panels": snapshot.bet_panel_states if snapshot else [],
            "stake_inputs": snapshot.stake_inputs if snapshot else [],
            "bet_buttons": snapshot.place_bet_buttons if snapshot else [],
            "betting_window": snapshot.betting_window if snapshot else {"state": "UNKNOWN"},
            "observed_at": observed_at,
        },
        "platform_acknowledged": None,
        "financial_action_submitted": False,
    }, next_readbacks


def _observe_manual_setup(request: Request, snapshot, configuration: dict) -> dict:
    live_manager = live(request)
    observation, readbacks = _manual_setup_observation(
        snapshot, configuration, getattr(live_manager, "manual_setup_readbacks", None))
    live_manager.manual_setup_readbacks = readbacks
    return observation


@router.get("/manual-setup")
async def manual_setup(request: Request):
    from app.betting.browser_client import AviatorBrowserClient

    configuration = _manual_setup_configuration(request)
    client = AviatorBrowserClient(request.app.state.betting.settings)
    try:
        snapshot = await client.snapshot()
        return {"ok": True, "manual_setup": _observe_manual_setup(request, snapshot, configuration)}
    except Exception as exc:
        return {"ok": False, "manual_setup": _observe_manual_setup(request, None, configuration),
                "error": f"READ_ONLY_OBSERVATION_UNAVAILABLE:{type(exc).__name__}"}
    finally:
        await client.close()


async def _read_cashout_verification(request: Request):
    from app.betting.browser_client import AviatorBrowserClient

    configuration = _manual_setup_configuration(request)
    client = AviatorBrowserClient(request.app.state.betting.settings)
    try:
        snapshot = await client.snapshot()
        observation = _observe_manual_setup(request, snapshot, configuration)
        panels = [{**row, "panel": int(row["panel_id"][-1])} for row in observation["cashout_panels"]]
        panel_observation_times = []
        for row in panels:
            try:
                stamp = datetime.fromisoformat(str(row.get("observed_at") or "").replace("Z", "+00:00"))
                if stamp.tzinfo is not None:
                    panel_observation_times.append(stamp.astimezone(timezone.utc))
            except (TypeError, ValueError, OverflowError):
                continue
        cashout_observed_at = min(panel_observation_times).isoformat() if panel_observation_times else observation["observed_at"]
        status = "CASHOUT_VERIFIED" if (len(panels) == 2
                                          and all(row["status"] == "CASHOUT_VERIFIED" for row in panels)) else \
            "CASHOUT_MISMATCH" if any(row["status"] == "CASHOUT_MISMATCH" for row in panels) else "CASHOUT_UNAVAILABLE"
        result = {"status": status, "observed_at": cashout_observed_at,
                  "source": "SHARED_AVIATOR_FRAME_READBACK", "target": "2.00", "panels": panels,
                  "selected_panel": observation["selected_panel"],
                  "page_role": observation["page_role"],
                  "browser_topology_verified": observation["browser_topology_verified"],
                  "betting_controls": observation["betting_controls"],
                  "platform_acknowledged": None, "financial_action_submitted": False}
        request.app.state.live.last_cashout_refresh = result
        return {"ok": True, "cashout_verification": result, "manual_setup": observation}
    finally:
        await client.close()


@router.get("/cashout/verification")
async def cashout_verification(request: Request):
    return await _read_cashout_verification(request)


@router.post("/cashout/refresh")
async def refresh_cashout(request: Request):
    """Deprecated compatibility route; refreshes read-only evidence only."""
    return await _read_cashout_verification(request)


@router.get("/risk")
async def risk_status(request: Request):
    risk = getattr(request.app.state, "risk", None)
    if risk is None:
        return {"ok": True, "execution_integrity": {
            "emergency_stop": None, "state": "BLOCKED",
            "reason": "EMERGENCY_STOP_STATE_UNAVAILABLE"}}
    return {"ok": True, "execution_integrity": {
        "emergency_stop": bool(risk.emergency_latched),
        "state": "BLOCKED" if risk.emergency_latched else "NOT_AUTHORIZED",
        "reason": "EMERGENCY_STOP_LATCHED" if risk.emergency_latched else
                  "REAL_MONEY_EXECUTION_DISABLED"}}


@router.post("/start")
async def start(request: Request, body: dict):
    return {"ok": False, "error": "backend_mode_switch_required",
            "message": "Review LIVE settings and switch through POST /api/betting-mode"}


@router.post("/pause")
async def pause(request: Request, body: dict | None = None):
    reason = (body or {}).get("reason", "manual_pause")
    modes = request.app.state.betting_mode
    return {"ok": True, "status": await modes.force_safe(reason)}


@router.post("/resume")
async def resume(request: Request, body: dict):
    return {"ok": False, "error": "fresh_live_review_required",
            "message": "Resume is disabled. Start a fresh backend reviewed LIVE mode switch."}


@router.post("/stop")
async def stop(request: Request):
    return {"ok": True, "status": await request.app.state.betting_mode.force_safe("manual_stop")}


@router.post("/emergency-stop")
async def emergency_stop(request: Request):
    await request.app.state.risk.emergency_stop()
    return {"ok": True, "status": await request.app.state.betting_mode.force_safe("emergency_stop")}


@router.get("/status")
async def status(request: Request):
    return {"ok": True, **live(request).status()}


@router.get("/current")
async def current(request: Request):
    current_session = live(request).status().get("session")
    betting_status = request.app.state.betting.status()
    open_executions = request.app.state.wp.repository.open_executions()
    return {"ok": True, "current": current_session,
            "current_bet": betting_status.get("current_bet"),
            "current_execution": open_executions[0] if open_executions else None}


@router.get("/session")
async def session(request: Request):
    return {"ok": True, "session": live(request).status().get("session")}


@router.get("/metrics")
async def metrics(request: Request):
    s = live(request).status().get("session") or {}
    return {"ok": True, "mode": live(request).mode, "session_id": s.get("session_id"),
            "executions": request.app.state.wp.repository.list_executions(100),
            "ledger": request.app.state.wp.repository.list_ledger(100)}


@router.get("/executions")
async def executions(request: Request, limit: int = 100):
    return {"ok": True, "executions": request.app.state.wp.repository.list_executions(limit)}


@router.get("/reconciliation")
async def reconciliation(request: Request, limit: int = 100):
    return {"ok": True, "records": request.app.state.wp.repository.list_reconciliations(limit)}


@router.get("/audit")
async def audit(request: Request, limit: int = 100):
    return {"ok": True, "events": request.app.state.wp.repository.list_audit_log(limit)}
