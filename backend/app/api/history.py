from fastapi import APIRouter, Request

router = APIRouter(prefix="/api/history", tags=["history"])


@router.get("")
def history(request: Request, limit: int = 250):
    wp = request.app.state.wp
    limit = min(max(limit, 1), 5000)
    rows = wp.rounds.tail(limit)
    return {
        "count": int(len(wp.rounds)),
        "returned": int(len(rows)),
        "rounds": [
            {
                "round_index": int(r.round_index),
                "multiplier": float(r.multiplier),
                "timestamp": r.timestamp,
                "target": int(float(r.multiplier) >= wp.settings.target_multiplier),
            }
            for r in rows.itertuples()
        ],
    }

