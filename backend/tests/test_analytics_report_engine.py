from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.database.repository import Repository
from app.api.analytics import export_report, report as get_report, reports as list_reports
from app.services.analytics_report_engine import (
    AnalyticsReportEngine,
    analyze_rounds,
    build_report,
    compare_summaries,
    completed_daily_windows,
    next_round_blocks,
    past_only_evidence,
    wilson_interval,
)


def round_rows(values, start=None, step=timedelta(seconds=1)):
    start = start or datetime(2026, 1, 1, tzinfo=timezone.utc)
    return [{"round_id": f"round-{i + 1}", "round_index": i + 1,
             "multiplier": value, "timestamp": (start + step * i).isoformat()}
            for i, value in enumerate(values)]


def test_round_boundaries_and_duplicate_ids_are_exact():
    rows = round_rows([1.1] * 2001)
    rows.insert(3, rows[2])
    blocks = next_round_blocks(rows)
    assert [len(block) for _, block in blocks] == [1000, 1000]
    assert blocks[0][1][0]["round_id"] == "round-1"
    assert blocks[1][1][-1]["round_id"] == "round-2000"
    hundred_blocks = next_round_blocks(rows, 100)
    assert [len(block) for _, block in hundred_blocks] == [100] * 20
    assert hundred_blocks[0][1][0]["round_id"] == "round-1"
    assert hundred_blocks[-1][1][-1]["round_id"] == "round-2000"


def test_completed_daily_windows_use_real_timestamps_and_exact_utc_boundary():
    start = datetime(2026, 1, 1, 23, 59, tzinfo=timezone.utc)
    rows = round_rows([1.4, 2.0, 2.5], start=start, step=timedelta(minutes=1))
    windows = completed_daily_windows(rows, datetime(2026, 1, 3, tzinfo=timezone.utc))
    assert [day for day, _, _, _ in windows] == ["2026-01-01", "2026-01-02"]
    assert [len(rows) for _, _, _, rows in windows] == [1, 2]
    assert [day for day, _, _, _ in completed_daily_windows(rows, datetime(2026, 1, 2, tzinfo=timezone.utc))] == ["2026-01-01"]


def test_threshold_bins_streaks_transitions_and_intervals():
    rows = round_rows([1.19, 1.20, 1.49, 1.50, 1.99, 2.00, 2.99, 3.00,
                       4.99, 5.00, 9.99, 10.0, 1.1, 1.2, 1.3, 2.1, 2.2, 2.3])
    result = analyze_rounds(rows)
    assert result["round_count"] == 18
    assert result["under_2x"]["count"] == 8
    assert result["at_least_2x"]["count"] == 10
    assert [item["count"] for item in result["multiplier_histogram"].values()] == [2, 4, 2, 5, 2, 2, 1]
    assert result["transitions"]["conditional_sequences"]["LLL"]["count"] >= 1
    assert result["streaks"]["longest"]["<2x"] == 5
    interval = wilson_interval(10, 10)
    assert 0.72 < interval["lower"] < 0.73
    assert interval["upper"] > 0.999999


def test_current_snapshot_keeps_dashboard_metrics_without_full_report_work(tmp_path):
    repository = Repository(tmp_path / "current-snapshot.sqlite")
    repository.init()
    rows = round_rows([1.2 + (i % 7) for i in range(1100)])
    service = SimpleNamespace(clean_rounds=__import__("pandas").DataFrame(rows), quality={"status": "VALIDATED"})
    snapshot = AnalyticsReportEngine(repository, service).current_snapshot()
    assert snapshot["latest_100"]["round_count"] == 100
    assert snapshot["latest_100"]["data_quality"]["available"] is True
    assert snapshot["historical"]["windows"]["100"]["round_count"] == 100
    assert snapshot["historical"]["under_2x"]["count"] + snapshot["historical"]["at_least_2x"]["count"] == 1100
    assert "rolling_at_least_2x" not in snapshot["historical"]


def test_research_evidence_is_past_only_through_requested_round():
    rows = round_rows([1.1, 1.4, 3.0, 1.2, 4.0])
    evidence = past_only_evidence(rows, "round-2")
    assert evidence["data_available_through_round"] == "round-2"
    assert evidence["data_available_through_index"] == 2
    assert evidence["future_round_included"] is False
    assert evidence["evidence"]["baseline_p_h"] == 0


def test_past_round_browser_pages_back_and_analyzes_through_selected_round(tmp_path):
    repository = Repository(tmp_path / "past-round.sqlite")
    repository.init()
    rows = round_rows([1.1 + (i % 5) for i in range(12)])
    service = SimpleNamespace(clean_rounds=__import__("pandas").DataFrame(rows), quality={})
    engine = AnalyticsReportEngine(repository, service)
    page = engine.round_options(5)
    assert [item["round_index"] for item in page["rounds"]] == [12, 11, 10, 9, 8]
    assert page["has_older"] is True
    earlier = engine.round_options(5, page["next_before_round_index"])
    assert [item["round_index"] for item in earlier["rounds"]] == [7, 6, 5, 4, 3]
    past = engine.past_round_snapshot("round-5")
    assert past["last_round"] == "round-5"
    assert past["round_count"] == 5
    assert past["analysis"]["round_count"] == 5


def test_period_comparison_and_drift_report_descriptive_changes():
    previous_rows = round_rows([1.2] * 100)
    current_rows = round_rows([2.2] * 100)
    previous = build_report("ROUND_REPORT", previous_rows, start_time=previous_rows[0]["timestamp"],
                            end_time=previous_rows[-1]["timestamp"], data_source="test", period_key="prev")
    current = analyze_rounds(current_rows)
    comparison = compare_summaries(current, previous)
    assert comparison["classification"] == "CHANGING"
    assert comparison["previous_report_id"] == previous["report_id"]
    assert any(item["metric"] == "patterns_appeared_disappeared" for item in comparison["changes"])
    drift_rows = round_rows([2.2] * 250 + [1.2] * 250)
    assert analyze_rounds(drift_rows)["drift"]["status"] == "CHANGING"


def test_report_snapshots_are_persisted_immutably_and_idempotently(tmp_path):
    repository = Repository(tmp_path / "reports.sqlite")
    repository.init()
    rows = round_rows([1.2, 2.4, 1.8])
    report = build_report("ROUND_REPORT", rows, start_time=rows[0]["timestamp"],
                          end_time=rows[-1]["timestamp"], data_source="repository.validated_rounds",
                          period_key="1:1-3")
    assert repository.save_analytics_report(report) is True
    edited = {**report, "round_count": 999, "analysis": {"changed": True}}
    assert repository.save_analytics_report(edited) is False
    restored = Repository(tmp_path / "reports.sqlite")
    assert restored.get_analytics_report(report["report_id"]) == report


def test_report_api_listing_detail_and_json_csv_exports(tmp_path):
    repository = Repository(tmp_path / "api.sqlite")
    repository.init()
    rows = round_rows([1.1, 2.0, 3.0])
    stored = build_report("ROUND_REPORT", rows, start_time=rows[0]["timestamp"],
                          end_time=rows[-1]["timestamp"], data_source="repository.validated_rounds",
                          period_key="1:1-3")
    repository.save_analytics_report(stored)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(wp=SimpleNamespace(repository=repository))))
    assert list_reports(request, "ROUND_REPORT", 10)["count"] == 1
    assert get_report(stored["report_id"], request)["round_count"] == 3
    assert export_report(stored["report_id"], request, "json").media_type == "application/json"
    csv_export = export_report(stored["report_id"], request, "csv")
    assert csv_export.media_type.startswith("text/csv")
    assert b">=2x" in csv_export.body


def test_scheduler_creates_exact_round_and_completed_day_reports_once(tmp_path):
    repository = Repository(tmp_path / "scheduler.sqlite")
    repository.init()
    start = datetime(2026, 1, 1, 0, tzinfo=timezone.utc)
    rows = round_rows([1.5 + (i % 2) for i in range(2000)], start, timedelta(minutes=1))
    service = SimpleNamespace(clean_rounds=__import__("pandas").DataFrame(rows), quality={"valid_rounds": len(rows)})
    engine = AnalyticsReportEngine(repository, service)
    now = start + timedelta(days=3)
    first = engine.generate_due_reports(now)
    recovered_repository = Repository(tmp_path / "scheduler.sqlite")
    recovered_repository.init()
    second = AnalyticsReportEngine(recovered_repository, service).generate_due_reports(now)
    assert len(first["generated"]) == 24  # twenty 100s, two 1,000s, and two UTC days
    assert second["generated"] == []
    assert len(recovered_repository.list_analytics_reports("ROUND_REPORT")) == 2
    hundred_round_reports = recovered_repository.list_analytics_reports("ROUND_100_REPORT")
    assert len(hundred_round_reports) == 20
    assert all(report["round_count"] == 100 for report in hundred_round_reports)
    assert all(report["analysis"]["round_count"] == 100 for report in hundred_round_reports)
    assert len(recovered_repository.list_analytics_reports("DAILY_REPORT")) == 2

    recovered_engine = AnalyticsReportEngine(recovered_repository, service)
    progress = recovered_engine.report_progress_snapshot()
    assert progress["valid_round_count"] == 2000
    assert progress["every_100"]["rounds_collected"] == 0
    assert progress["every_100"]["rounds_remaining"] == 100
    assert progress["every_100"]["latest_report"]["last_round"] == "round-2000"
    assert progress["every_1000"]["rounds_collected"] == 0
    assert progress["every_1000"]["rounds_remaining"] == 1000

    service.clean_rounds = __import__("pandas").DataFrame(
        round_rows([1.5 + (i % 2) for i in range(2034)], start, timedelta(minutes=1)))
    partial = recovered_engine.report_progress_snapshot()
    assert partial["valid_round_count"] == 2034
    assert partial["every_100"]["rounds_collected"] == 34
    assert partial["every_100"]["rounds_remaining"] == 66
    assert partial["every_100"]["latest_report"]["last_round"] == "round-2000"
    assert partial["every_1000"]["rounds_collected"] == 34
    assert partial["every_1000"]["rounds_remaining"] == 966


def test_large_history_analysis_completes_and_keeps_source_count():
    rows = round_rows([1.15 + (i % 113) / 31 for i in range(20_000)])
    result = analyze_rounds(rows)
    assert result["round_count"] == 20_000
    assert len(result["block_persistence"]["complete_1000_round_blocks"]) == 20
    assert result["sequence_pattern_persistence"]["complete_1000_round_blocks"] == 20
    assert result["sequence_pattern_persistence"]["sequences"]["L"]["status"] == "STABLE"
