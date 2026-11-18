from __future__ import annotations

import argparse
import json
import logging
import os
import secrets
import sys
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parent.parent
PACKAGE = ROOT / "behaviour_detection"
ENV_FILE = PACKAGE / ".env"
RESEARCH_ROOT = ROOT / "research_phase1_2026-11-13"


def _read_env_file() -> dict[str, str]:
    from dotenv import dotenv_values
    return {k: str(v) for k, v in dotenv_values(ENV_FILE).items() if k and v is not None}


def _ensure_private_environment() -> None:
    if not ENV_FILE.exists():
        template = (PACKAGE / ".env.example").read_text(encoding="utf-8")
        password = secrets.token_urlsafe(36)
        contents = template.replace("REPLACE_WITH_PRIVATE_PASSWORD", password)
        descriptor = os.open(ENV_FILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            output.write(contents)
    else:
        os.chmod(ENV_FILE, 0o600)
    values = _read_env_file()
    password = values.get("BEHAVIOUR_DB_PASSWORD", "")
    database_url = values.get("BEHAVIOUR_DATABASE_URL", "").strip()
    component_configured = bool(
        values.get("BEHAVIOUR_DB_USER") and password and values.get("BEHAVIOUR_DB_NAME")
        and "REPLACE_WITH_PRIVATE_PASSWORD" not in password
    )
    if not component_configured and not database_url:
        raise RuntimeError(
            "behaviour_detection/.env needs a dedicated database URL or complete local PostgreSQL credentials; "
            "no Winner Predict credential will be used"
        )


def _load_settings():
    from . import settings
    problems = settings.validation_errors()
    if any("BEHAVIOUR_DATABASE_URL" in item for item in problems):
        raise RuntimeError("; ".join(problems))
    return settings


def _engine():
    from .db import make_engine
    settings = _load_settings()
    return make_engine(settings.DATABASE_URL)


def command_setup(_args) -> int:
    _ensure_private_environment()
    from .db import make_engine, migrate
    settings = _load_settings()
    engine = make_engine(settings.DATABASE_URL)
    try:
        with engine.connect() as connection:
            connection.exec_driver_sql("SELECT 1")
        migrate(engine)
    except Exception as exc:
        raise RuntimeError(
            "Could not connect to the configured Behaviour database. Start the local PostgreSQL service "
            "and ensure behaviour_detection/.env points to its dedicated database, or configure a local SQLite URL. "
            f"({type(exc).__name__})"
        ) from None
    finally:
        engine.dispose()
    print("Behaviour database is ready and its schema is migrated.")
    print("Next: npm run behaviour:import-history, then npm run behaviour:reconcile, then npm start.")
    return 0


def command_env_check(_args) -> int:
    _ensure_private_environment()
    settings = _load_settings()
    issues = settings.validation_errors()
    if issues:
        for issue in issues:
            print(f"configuration: {issue}")
        return 2
    values = _read_env_file()
    print("Behaviour database configuration: present (value hidden)")
    print(f"Winner Predict PostgreSQL source: {'configured' if settings.WINNER_PREDICT_DATABASE_URL else 'not configured'} (read-only)")
    if settings.ORBIT_BASE_URL and settings.ORBIT_API_KEY:
        print("Orbit credentials: present; Orbit is not used by the active live-round sync")
    print(f"Phase 1 research package: {'present' if RESEARCH_ROOT.is_dir() else 'missing'}")
    return 0 if RESEARCH_ROOT.is_dir() else 2


def command_migrate(_args) -> int:
    from .db import migrate
    engine = _engine()
    migrate(engine)
    engine.dispose()
    print("Behaviour schema migrations are current.")
    return 0


def command_import_history(args) -> int:
    from .db import migrate
    from .importer import DEFAULT_RESEARCH_ROOT, import_phase1_and_enrich
    engine = _engine()
    try:
        migrate(engine)
        root = Path(args.research_root or os.getenv("BEHAVIOUR_RESEARCH_ROOT") or DEFAULT_RESEARCH_ROOT)
        result = import_phase1_and_enrich(
            engine, root, batch_size=_load_settings().IMPORT_BATCH_SIZE)
    finally:
        engine.dispose()
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def _reconcile(engine) -> dict:
    from sqlalchemy import func, select
    from sqlalchemy.orm import Session
    from .analysis import validate_expanding_baseline
    from .importer import SNAPSHOT_ID
    from .models import (CanonicalRound, CumulativeStats, DiscoveredMotif,
                         ForecastReferenceBlock, MotifResearchEvidence,
                         ResearchImport, RoundSourceMapping, SourceRecord, TimeGroup,
                         WorkerCheckpoint)

    with Session(engine) as session:
        imported = session.get(ResearchImport, SNAPSHOT_ID)
        if imported is None:
            raise RuntimeError("Phase 1 snapshot has not been imported")
        counts = {
            "raw_source_records": session.scalar(select(func.count()).select_from(SourceRecord).where(
                SourceRecord.snapshot_id == SNAPSHOT_ID)) or 0,
            "canonical_rounds": session.scalar(select(func.count()).select_from(CanonicalRound).where(
                CanonicalRound.import_snapshot_id == SNAPSHOT_ID)) or 0,
            "source_mappings": session.scalar(select(func.count()).select_from(RoundSourceMapping).where(
                RoundSourceMapping.snapshot_id == SNAPSHOT_ID)) or 0,
            "time_groups": session.scalar(select(func.count()).select_from(TimeGroup)) or 0,
            "forecast_reference_blocks": session.scalar(select(func.count()).select_from(ForecastReferenceBlock).where(
                ForecastReferenceBlock.source == "phase1_frozen_disjoint_100")) or 0,
            "discovered_motifs": session.scalar(select(func.count()).select_from(DiscoveredMotif).where(
                DiscoveredMotif.research_snapshot_id == SNAPSHOT_ID)) or 0,
            "motif_evidence_records": session.scalar(select(func.count()).select_from(MotifResearchEvidence).where(
                MotifResearchEvidence.research_snapshot_id == SNAPSHOT_ID)) or 0,
        }
        stats = session.get(CumulativeStats, "global")
        classes = {symbol: session.scalar(select(func.count()).select_from(CanonicalRound).where(
            CanonicalRound.import_snapshot_id == SNAPSHOT_ID, CanonicalRound.outcome == symbol)) or 0
                   for symbol in ("H", "L", "E")}
        total_rounds = session.scalar(select(func.count()).select_from(CanonicalRound)) or 0
        total_classes = {symbol: session.scalar(select(func.count()).select_from(CanonicalRound).where(
            CanonicalRound.outcome == symbol)) or 0 for symbol in ("H", "L", "E")}
        latest_position = session.scalar(select(func.max(CanonicalRound.analysis_position)) ) or 0
        live_rounds = session.scalar(select(func.count()).select_from(CanonicalRound).where(
            CanonicalRound.import_snapshot_id.is_(None))) or 0
        cross_source_unknown = session.scalar(select(func.count()).select_from(CanonicalRound).where(
            CanonicalRound.order_quality == "verified_orbit_delta_cross_source_overlap_unknown")) or 0
        checkpoint = session.get(WorkerCheckpoint, "analysis")
        checkpoint_position = checkpoint.last_position if checkpoint else 0
        analysis_pending = max(0, latest_position - checkpoint_position)
        symbols = session.scalars(select(CanonicalRound.outcome).where(
            CanonicalRound.import_snapshot_id == SNAPSHOT_ID).order_by(CanonicalRound.analysis_position)).all()
        baseline = validate_expanding_baseline(symbols)
        expected = {"raw_source_records": 171378, "canonical_rounds": 50123,
                    "source_mappings": 171378, "time_groups": 427,
                    "forecast_reference_blocks": 499}
        base_counts = {key: counts[key] for key in expected}
        if base_counts != expected or classes != {"H": 24339, "L": 25651, "E": 133}:
            raise RuntimeError(f"Phase 1 database reconciliation mismatch: counts={counts}, classes={classes}")
        if counts["discovered_motifs"] != counts["motif_evidence_records"]:
            raise RuntimeError(f"motif detail reconciliation mismatch: counts={counts}")
        if not baseline.get("matches_phase1_tolerance"):
            raise RuntimeError(f"Phase 1 expanding-baseline replay mismatch: {baseline}")
        from .analysis import disjoint_reference_blocks
        expected_blocks = disjoint_reference_blocks(symbols)
        stored_blocks = session.scalars(select(ForecastReferenceBlock).where(
            ForecastReferenceBlock.source == "phase1_frozen_disjoint_100"
        ).order_by(ForecastReferenceBlock.block_number)).all()
        expected_positions = [(b["first_position"], b["last_position"]) for b in expected_blocks]
        stored_positions = [(b.first_position, b.last_position) for b in stored_blocks]
        if stored_positions != expected_positions:
            raise RuntimeError("Phase 1 next-100 reference positions do not match the source sequence")
        if imported.input_files_sha256 is None:
            raise RuntimeError("Phase 1 input file bytes have not been attested; rerun behaviour:import-history")
        if latest_position != total_rounds:
            raise RuntimeError("canonical analysis positions are not a gap-free sequence")
        summary_matches = bool(stats and checkpoint_position == latest_position
                               and (stats.round_count, stats.high_count, stats.low_count,
                                    stats.exact_count) ==
                               (total_rounds, total_classes["H"], total_classes["L"],
                                total_classes["E"]))
        if analysis_pending == 0 and not summary_matches:
            raise RuntimeError("incremental class totals do not match fully processed canonical rounds")
        return {"status": "reconciled", "snapshot_id": imported.snapshot_id,
                "source_sha256": imported.source_sha256,
                "input_files_sha256": imported.input_files_sha256,
                **counts, "classes": classes,
                "total_canonical_rounds": total_rounds,
                "live_rounds": live_rounds,
                "total_classes": total_classes,
                "cross_source_order_unknown_rounds": cross_source_unknown,
                "analysis_checkpoint_position": checkpoint_position,
                "analysis_pending_rounds": analysis_pending,
                "incremental_summary_matches": summary_matches,
                "source_rows_accounted": imported.source_records,
                "baseline_validation": baseline}


def command_reconcile(_args) -> int:
    engine = _engine()
    result = _reconcile(engine)
    engine.dispose()
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def _orbit_client(settings):
    if not settings.ORBIT_BASE_URL or not settings.ORBIT_API_KEY:
        raise RuntimeError("Set ORBIT_API_BASE_URL (or BEHAVIOUR_ORBIT_API_BASE_URL) and an independent Orbit API key")
    from .orbit_client import OrbitDataWatchClient
    return OrbitDataWatchClient(settings.ORBIT_BASE_URL, settings.ORBIT_API_KEY,
                                connect_timeout=settings.ORBIT_CONNECT_TIMEOUT_SECONDS,
                                request_timeout=settings.ORBIT_REQUEST_TIMEOUT_SECONDS)


def command_orbit_test(_args) -> int:
    from .collector import _visible_dom, _watch_source_health
    from .orbit_client import (parse_reading, select_watch, validate_history_page,
                               validate_latest_snapshot)
    settings = _load_settings()
    client = _orbit_client(settings)
    try:
        watch = select_watch(client.list_watches())
        latest = client.latest(watch["id"])
        cursor, raw = validate_latest_snapshot(latest, watch)
        history = validate_history_page(
            client.history(watch["id"], "0", settings.ORBIT_PAGE_SIZE), watch["id"], "0")
        latest_in_history_page = None
        if raw is not None and int(cursor) <= int(history["nextCursor"]):
            history_latest = next((item for item in history["readings"]
                                   if str(item.get("id", "")) == cursor), None)
            if history_latest is None:
                raise RuntimeError("Orbit latest reading is missing from the history page that covers its cursor")
            if history_latest != raw:
                raise RuntimeError("Orbit latest and history endpoints disagree for the same reading")
            latest_in_history_page = True
        parsed = parse_reading(raw) if raw else None
        context = _visible_dom(parsed) if parsed else []
        context_count = len(context)
        source_state, source_details = _watch_source_health(watch)
        print(json.dumps({"status": "connected", "watch_id": watch["id"],
                          "selector": watch["selector"], "latest_context_rounds": context_count,
                          "source_visible_items": sum(item.visible for item in parsed.items) if parsed else 0,
                          "identical_sequence_halves_coalesced": any(
                              len(item.get("source_item_indices", [])) > 1 for item in context),
                          "source_health_state": source_state,
                          "source_collector_status": source_details.get("upstream_collector_status"),
                          "source_error_code": source_details.get("upstream_error_code"),
                          "latest_reading_id": parsed.reading_id if parsed else None,
                          "history_readings": len(history["readings"]),
                          "history_next_cursor": history["nextCursor"],
                          "history_has_more": history["hasMore"],
                          "latest_in_history_page": latest_in_history_page,
                          "reading_checked_at": parsed.checked_at.isoformat() if parsed else None,
                          "context_at_least_50": context_count >= 50,
                          "note": "checkedAt is page observation time; round event timestamps are unavailable on this contract."}, indent=2))
    finally:
        client.close()
    return 0


def command_backfill_orbit(_args) -> int:
    from .collector import collect_once
    from .db import migrate
    from .orbit_client import select_watch
    settings = _load_settings()
    engine = _engine()
    migrate(engine)
    client = _orbit_client(settings)
    try:
        from sqlalchemy import select
        from sqlalchemy.orm import Session
        from .models import OrbitWatchState
        with Session(engine) as session:
            state = session.get(OrbitWatchState, 1)
        preferred_watch_id = state.watch_id if state else settings.ORBIT_WATCH_ID
        watch = select_watch(client.list_watches(), preferred_watch_id,
                             allow_fallback=state is None)
        result = collect_once(engine, client, watch, page_size=settings.ORBIT_PAGE_SIZE)
    finally:
        client.close()
        engine.dispose()
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def command_collector_run(_args) -> int:
    from .collector import run_collector
    settings = _load_settings()
    run_collector(settings.DATABASE_URL, settings.ORBIT_BASE_URL, settings.ORBIT_API_KEY,
                  poll_seconds=settings.ORBIT_POLL_SECONDS,
                  page_size=settings.ORBIT_PAGE_SIZE,
                  connect_timeout=settings.ORBIT_CONNECT_TIMEOUT_SECONDS,
                  request_timeout=settings.ORBIT_REQUEST_TIMEOUT_SECONDS)
    return 0


def command_winner_sync_once(_args) -> int:
    from .winner_source import sync_once
    settings = _load_settings()
    engine = _engine()
    try:
        from .db import migrate
        migrate(engine)
        result = sync_once(engine)
    finally:
        engine.dispose()
    print(json.dumps(result, indent=2, sort_keys=True, default=str))
    return 0 if result.get("status") != "degraded" else 2


def command_winner_sync_run(_args) -> int:
    from .winner_source import run_source_collector
    settings = _load_settings()
    if not settings.WINNER_PREDICT_DATABASE_URL:
        raise RuntimeError("Winner Predict DATABASE_URL is not configured")
    run_source_collector(settings.DATABASE_URL,
                         poll_seconds=settings.WINNER_PREDICT_SYNC_SECONDS)
    return 0


def command_worker_run(_args) -> int:
    from .worker import run_worker
    settings = _load_settings()
    run_worker(settings.DATABASE_URL, poll_seconds=0.25,
               state_confirm_rounds=settings.STATE_CONFIRM_ROUNDS,
               min_state_rounds=settings.MIN_STATE_ROUNDS,
               gap_seconds=settings.TIME_GROUP_GAP_SECONDS)
    return 0


def command_start(_args) -> int:
    os.execvp("node", ["node", str(ROOT / "scripts" / "behaviour-runtime.js")])
    return 0


def command_stop(_args) -> int:
    os.execvp("node", ["node", str(ROOT / "scripts" / "behaviour-runtime.js"), "--stop"])
    return 0


def command_status(_args) -> int:
    os.execvp("node", ["node", str(ROOT / "scripts" / "behaviour-runtime.js"), "--status"])
    return 0


def command_health(_args) -> int:
    base = os.getenv("BEHAVIOUR_LOCAL_API_URL", "http://127.0.0.1:8100")
    results = {}
    for path in ("/health/live", "/health/ready", "/api/v1/behaviour/health"):
        try:
            with urlopen(base + path, timeout=15) as response:
                payload = json.loads(response.read())
                results[path] = {"http_status": response.status, "payload": payload,
                                 "ok": payload.get("ok", True) is True}
        except (OSError, URLError, TimeoutError) as exc:
            results[path] = {"error": type(exc).__name__}
    print(json.dumps(results, indent=2, sort_keys=True))
    return 0 if all("error" not in result and result.get("ok", True) for result in results.values()) else 2


def command_logs(args) -> int:
    os.execvp("node", ["node", str(ROOT / "scripts" / "behaviour-runtime.js"), "--logs"])
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="behaviour", description="Independent deterministic Behaviour Detection runtime")
    commands = parser.add_subparsers(dest="command", required=True)
    for name, callback in (("setup", command_setup), ("env-check", command_env_check),
                           ("migrate", command_migrate), ("reconcile", command_reconcile),
                           ("orbit-test", command_orbit_test), ("backfill-orbit", command_backfill_orbit),
                           ("collector-run", command_collector_run), ("winner-sync-once", command_winner_sync_once),
                           ("winner-sync-run", command_winner_sync_run), ("worker-run", command_worker_run),
                           ("start", command_start), ("stop", command_stop),
                           ("status", command_status), ("health", command_health)):
        sub = commands.add_parser(name)
        sub.set_defaults(callback=callback)
    importer = commands.add_parser("import-history")
    importer.add_argument("--research-root", default=None)
    importer.set_defaults(callback=command_import_history)
    logs = commands.add_parser("logs")
    logs.add_argument("--tail", type=int, default=100)
    logs.add_argument("--follow", action="store_true")
    logs.add_argument("services", nargs="*")
    logs.set_defaults(callback=command_logs)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(level=os.getenv("BEHAVIOUR_LOG_LEVEL", "INFO").upper(),
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        return args.callback(args)
    except KeyboardInterrupt:
        print("Interrupted; the independent process can be restarted safely.", file=sys.stderr)
        return 130
    except Exception as exc:
        # Runtime diagnostics include a code/type and never print credential-bearing settings.
        message = str(exc)
        try:
            values = _read_env_file()
            for secret_name in ("BEHAVIOUR_DB_PASSWORD", "BEHAVIOUR_ORBIT_API_KEY"):
                secret = values.get(secret_name)
                if secret:
                    message = message.replace(secret, "[redacted]")
        except Exception:
            message = type(exc).__name__
        print(f"behaviour command failed: {type(exc).__name__}: {message}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
