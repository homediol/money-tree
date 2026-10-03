from __future__ import annotations

import csv
import io
import json

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import Response

router = APIRouter(prefix="/api/analytics", tags=["analytics-reports"])


@router.get("/current")
def current(request: Request):
    return request.app.state.wp.analytics_report_engine.current_snapshot()


@router.get("/progress")
def report_progress(request: Request):
    return request.app.state.wp.analytics_report_engine.report_progress_snapshot()


@router.get("/reports")
def reports(request: Request, report_type: str | None = Query(None, pattern="^(DAILY_REPORT|ROUND_100_REPORT|ROUND_REPORT)$"),
            limit: int = 100, offset: int = 0):
    limit = min(max(limit, 1), 1000)
    offset = max(offset, 0)
    items = request.app.state.wp.repository.list_analytics_reports(report_type, limit, offset)
    has_more = len(items) == limit
    return {"reports": items, "count": len(items),
            "next_offset": offset + len(items) if has_more else None,
            "has_more": has_more}


@router.get("/reports/{report_id}")
def report(report_id: str, request: Request):
    item = request.app.state.wp.repository.get_analytics_report(report_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Analytics report not found")
    return item


@router.get("/reports/{report_id}/export")
def export_report(report_id: str, request: Request, format: str = Query("json", pattern="^(json|csv)$")):
    item = request.app.state.wp.repository.get_analytics_report(report_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Analytics report not found")
    if format == "json":
        body = json.dumps(item, ensure_ascii=False, indent=2)
        return Response(body, media_type="application/json",
                        headers={"Content-Disposition": f'attachment; filename="{report_id}.json"'})
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(["section", "metric", "value", "sample_size", "ci95_lower", "ci95_upper"])
    analysis = item["analysis"]
    writer.writerow(["report", "report_id", item["report_id"], "", "", ""])
    writer.writerow(["report", "report_type", item["report_type"], "", "", ""])
    writer.writerow(["report", "start_time", item["start_time"], "", "", ""])
    writer.writerow(["report", "end_time", item["end_time"], "", "", ""])
    writer.writerow(["report", "first_round", item["first_round"], "", "", ""])
    writer.writerow(["report", "last_round", item["last_round"], "", "", ""])
    writer.writerow(["report", "round_count", item["round_count"], "", "", ""])
    for metric, key in (("<2x", "under_2x"), (">=2x", "at_least_2x")):
        result = analysis[key]
        ci = result["confidence_95"]
        writer.writerow(["threshold", metric, result["percentage"], result["count"], ci["lower"], ci["upper"]])
    for label, result in analysis.get("multiplier_histogram", {}).items():
        writer.writerow(["multiplier_histogram", label, result["percentage"], analysis["round_count"], "", ""])
    for sequence, result in analysis.get("transitions", {}).get("conditional_sequences", {}).items():
        ci = result["confidence_95"]
        writer.writerow(["conditional_sequence", f"after_{sequence}_P(H)", result["percentage"], result["count"], ci["lower"], ci["upper"]])
    return Response(output.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{report_id}.csv"'})


@router.get("/research-features/past-only")
def research_features(request: Request, through_round_id: str):
    try:
        return request.app.state.wp.analytics_report_engine.research_features_through(through_round_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/rounds")
def round_options(request: Request, limit: int = 100, before_round_index: int | None = None):
    return request.app.state.wp.analytics_report_engine.round_options(limit, before_round_index)


@router.get("/past-round")
def past_round(request: Request, through_round_id: str):
    try:
        return request.app.state.wp.analytics_report_engine.past_round_snapshot(through_round_id)
    except KeyError as exc:
        raise HTTPException(status_code=404,
                            detail=f"Round {through_round_id!r} is not present in validated stored history. Refresh round history or choose an available round.") from exc
