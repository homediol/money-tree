from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request

router = APIRouter(prefix="/api/live", tags=["live activation"])


def live(request: Request):
    return request.app.state.live


@router.post("/readiness")
async def readiness(request: Request):
    modes = request.app.state.betting_mode
    observation = await modes._read_platform_balance()
    return await live(request).production_readiness_check(observation)


@router.get("/readiness")
async def readiness_read_only(request: Request):
    """Fresh read-only readiness check; does not authorize or start a session."""
    modes = request.app.state.betting_mode
    observation = await modes._read_platform_balance()
    return await live(request).production_readiness_check(observation)


@router.get("/balance")
async def balance(request: Request):
    """Read current platform balance from the already-owned CDP context."""
    observation = await request.app.state.betting_mode._read_platform_balance()
    return {"ok": True, "platform_balance": observation}


@router.post("/cashout/refresh")
async def refresh_cashout(request: Request):
    """Refresh both Auto Cash Out controls on the one owned Aviator frame.

    This operation only changes the two settings panels. It never submits a
    bet, and refuses to proceed if either panel has an active/unknown bet or
    either Auto Bet switch is not verified OFF.
    """
    live_manager = request.app.state.live
    mode_manager = request.app.state.betting_mode
    if live_manager.live_active or mode_manager.mode == "LIVE_REAL":
        raise HTTPException(status_code=409, detail={
            "reason": "CASHOUT_REFRESH_REQUIRES_LIVE_SESSION_STOPPED",
            "message": "Stop LIVE_REAL before changing platform controls.",
        })

    from app.betting.browser_client import AviatorBrowserClient, BrowserUiError
    from app.betting.cashout import inspect_cashout_configuration

    client = AviatorBrowserClient(request.app.state.betting.settings)
    try:
        before = await client.snapshot()
        client._require_clear_two_panel_state(before)
        client._require_auto_bet_off_two_panel_state(before)

        for panel in (0, 1):
            await client.set_auto_cashout(target=2.00, panel=panel)

        final_snapshot = await client.snapshot()
        client._require_clear_two_panel_state(final_snapshot)
        client._require_auto_bet_off_two_panel_state(final_snapshot)
        controls = {item.get("slot"): item for item in final_snapshot.cashout_controls}
        panels: list[dict] = []
        for panel in (0, 1):
            control = controls.get(panel)
            inspected = inspect_cashout_configuration(control)
            auto_bet_off = bool(control and control.get("autoBetEnabled") is False)
            ready = bool(inspected.get("configuration_valid") and auto_bet_off)
            panels.append({
                "panel": panel + 1,
                "status": "READY" if ready else "FAILED",
                "auto_tab_selected": control.get("autoTabSelected") if control else None,
                "auto_bet": "OFF" if auto_bet_off else "UNVERIFIED",
                "auto_cashout": "ON" if inspected.get("enabled") is True else
                    "OFF" if inspected.get("enabled") is False else "UNVERIFIED",
                "multiplier": inspected.get("displayed_value"),
                "active_or_pending_bet": "CLEAR",
                "platform_acknowledged": inspected.get("platform_acknowledged"),
                "failures": inspected.get("failures", []),
            })
        observed_at = datetime.now(timezone.utc).isoformat()
        betting_controls = {
            "status": "READY" if len(final_snapshot.bet_panel_states) == 2
                and len(final_snapshot.stake_inputs) == 2
                and len(final_snapshot.place_bet_buttons) == 2
                and all(row.get("pending_bet_state") == "CLEAR"
                        and row.get("button_visible") is True
                        and row.get("stake_input_visible") is True
                        for row in final_snapshot.bet_panel_states)
                else "UNAVAILABLE",
            "panels": final_snapshot.bet_panel_states,
            "stake_inputs": final_snapshot.stake_inputs,
            "bet_buttons": final_snapshot.place_bet_buttons,
            "betting_window": final_snapshot.betting_window,
            "observed_at": observed_at,
        }
        status = "READY" if all(panel["status"] == "READY" for panel in panels) else "FAILED"
        readback = {
            "status": status,
            "observed_at": observed_at,
            "source": "SHARED_AVIATOR_FRAME_READBACK",
            "page_role": final_snapshot.page_role,
            "authentication_state": final_snapshot.authentication_state,
            "browser_topology_verified": final_snapshot.browser_topology_verified,
            "target": "2.00",
            "auto_bet": "OFF" if status == "READY" else "UNVERIFIED",
            "panels": panels,
            "betting_controls": betting_controls,
            "platform_acknowledged": None,
            "financial_action_submitted": False,
        }
        live_manager.last_cashout_refresh = readback
        if status != "READY":
            raise HTTPException(status_code=409, detail={
                "reason": "CASHOUT_CONFIGURATION_READBACK_FAILED",
                "cashout_refresh": readback,
            })
        return {"ok": True, "cashout_refresh": readback}
    except HTTPException:
        raise
    except BrowserUiError as exc:
        failure = {
            "status": "FAILED", "observed_at": datetime.now(timezone.utc).isoformat(),
            "source": "SHARED_AVIATOR_FRAME_READBACK", "reason": str(exc),
            "platform_acknowledged": None, "financial_action_submitted": False,
        }
        live_manager.last_cashout_refresh = failure
        raise HTTPException(status_code=409, detail=failure) from exc
    except Exception as exc:
        failure = {
            "status": "FAILED", "observed_at": datetime.now(timezone.utc).isoformat(),
            "source": "SHARED_AVIATOR_FRAME_READBACK",
            "reason": f"CASHOUT_REFRESH_FAILED:{type(exc).__name__}",
            "platform_acknowledged": None, "financial_action_submitted": False,
        }
        live_manager.last_cashout_refresh = failure
        raise HTTPException(status_code=503, detail=failure) from exc
    finally:
        await client.close()


@router.get("/risk")
async def risk_status(request: Request):
    betting = request.app.state.betting
    return {"ok": True, "status": request.app.state.risk.status(betting.status())}


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
