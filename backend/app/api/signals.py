from fastapi import APIRouter, Request

router = APIRouter(prefix="/api/signal", tags=["signals"])


@router.get("/current")
def current_signal(request: Request):
    analysis = request.app.state.wp.current_analysis()
    return {"analysis": analysis, "formatted": request.app.state.wp.signal_engine().formatted(analysis)}


@router.get("/history")
def signal_history(request: Request, limit: int = 100):
    limit = min(max(limit, 1), 500)
    return {"signals": request.app.state.wp.repository.list_signals(limit)}

