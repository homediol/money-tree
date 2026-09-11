from fastapi import APIRouter, Request

router = APIRouter(prefix="/api/analysis", tags=["analysis"])


@router.get("/current")
def current_analysis(request: Request):
    return request.app.state.wp.current_analysis()


@router.post("/recalculate")
def recalculate(request: Request):
    request.app.state.wp.reload()
    return request.app.state.wp.current_analysis()

