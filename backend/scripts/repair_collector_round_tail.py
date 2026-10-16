"""Move one verified local collector session to PostgreSQL's canonical tail.

Use only when collector rows were written with restarted local indexes after a
temporary PostgreSQL load failure. Round IDs, multipliers, and timestamps stay
unchanged. The first moved row opens a continuity boundary because its order
relative to the previous PostgreSQL tail was not proven.
"""
from __future__ import annotations

import argparse
import fcntl
import json
from pathlib import Path
import sys

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.core.config import get_settings
from app.database.repository import Repository


ROOT = Path(__file__).resolve().parents[2]
STATUS_PATH = ROOT / "data" / "bot" / "status.json"
COLLECTOR_LOCK_PATH = ROOT / "data" / "bot" / "collector.lock"


def _require_collector_stopped():
    handle = COLLECTOR_LOCK_PATH.open("a+")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        handle.close()
        raise RuntimeError("collector OS lock is held; stop the collector before repairing indexes") from exc
    return handle


def _read_collector_status() -> dict:
    try:
        value = json.loads(STATUS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"could not read collector status: {type(exc).__name__}") from exc
    if not isinstance(value, dict) or not value.get("lastRoundId"):
        raise RuntimeError("collector status has no latest persisted round ID")
    if value.get("lastObservedRoundId") != value.get("lastRoundId"):
        raise RuntimeError("collector has an observed round that is not yet persisted")
    return value


def _candidate_session(conn, status: dict) -> tuple[dict, dict, list[dict]]:
    tail = conn.execute(
        """SELECT round_id,round_index,stored_at FROM aviator_rounds
           ORDER BY round_index DESC LIMIT 1"""
    ).fetchone()
    if not tail:
        raise RuntimeError("PostgreSQL round history is empty")
    current = conn.execute(
        """SELECT round_id,round_index,local_round_index,round_index_source,
                  platform_round_id,platform_round_index,source,continuity_verified,
                  gap_before,continuity_proof,stored_at,timestamp
           FROM aviator_rounds WHERE round_id=?""",
        (str(status["lastRoundId"]),),
    ).fetchone()
    if not current:
        raise RuntimeError("collector latest round is absent from PostgreSQL")
    end_local = int(current["local_round_index"] or 0)
    if (end_local < 1 or int(current["round_index"]) != end_local
            or int(tail["round_index"]) <= int(current["round_index"])):
        raise RuntimeError("collector latest row does not match the expected out-of-order local sequence")

    fetched = conn.execute(
        """SELECT round_id,round_index,local_round_index,round_index_source,
                  platform_round_id,platform_round_index,source,continuity_verified,
                  gap_before,continuity_proof,stored_at,timestamp
           FROM aviator_rounds WHERE round_index BETWEEN 1 AND ? ORDER BY round_index ASC""",
        (end_local,),
    ).fetchall()
    rows = [dict(row) for row in fetched]
    if len(rows) != end_local:
        raise RuntimeError(f"expected local indexes 1..{end_local}, found {len(rows)} rows")
    for expected, row in enumerate(rows, 1):
        if (int(row["round_index"]) != expected
                or int(row["local_round_index"] or 0) != expected
                or row["source"] != "collector"
                or row["round_index_source"] != "LOCAL_SEQUENCE"
                or row["platform_round_id"] is not None
                or row["platform_round_index"] is not None
                or not row["continuity_verified"]
                or (expected > 1 and row["gap_before"])):
            raise RuntimeError(f"row at local index {expected} does not match the verified session pattern")
    if str(rows[-1]["round_id"]) != str(status["lastRoundId"]):
        raise RuntimeError("collector status round is not the end of the local-index sequence")
    if rows[0]["stored_at"] <= tail["stored_at"]:
        raise RuntimeError("local session does not start after the current PostgreSQL tail")

    start_index = int(tail["round_index"]) + 1
    end_index = int(tail["round_index"]) + end_local
    conflict = conn.execute(
        "SELECT round_index FROM aviator_rounds WHERE round_index BETWEEN ? AND ? LIMIT 1",
        (start_index, end_index),
    ).fetchone()
    if conflict:
        raise RuntimeError(f"canonical destination index {conflict['round_index']} is already occupied")
    return dict(tail), dict(current), rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="commit the guarded index repair")
    parser.parse_args()
    parser.error("PERSISTENT_ROUND_HISTORY_REPAIR_DISABLED")

    collector_lock = _require_collector_stopped()
    try:
        status = _read_collector_status()
        settings = get_settings()
        repository = Repository(settings.database_path, database_url=settings.database_url, require_postgres=True)
        with repository.connect() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(hashtext(?))", ("aviator_rounds",))
            tail, current, rows = _candidate_session(conn, status)
            offset = int(tail["round_index"])
            print(json.dumps({
                "mode": "APPLY" if args.apply else "DRY_RUN",
                "round_count": len(rows),
                "collector_latest_round_id": current["round_id"],
                "previous_postgres_tail_round_id": tail["round_id"],
                "previous_postgres_tail_index": int(tail["round_index"]),
                "new_canonical_range": [offset + 1, offset + len(rows)],
                "first_round_opens_continuity_boundary": True,
                "round_ids_and_multipliers_preserved": True,
            }, indent=2))
            if not args.apply:
                return 0

            for row in rows:
                local_index = int(row["local_round_index"])
                if local_index == 1:
                    conn.execute(
                        """UPDATE aviator_rounds
                           SET round_index=?,gap_before=TRUE,
                               continuity_proof='LOCAL_SEQUENCE_REBASED_AT_POSTGRES_TAIL'
                           WHERE round_id=? AND round_index=?""",
                        (offset + local_index, row["round_id"], local_index),
                    )
                else:
                    conn.execute(
                        "UPDATE aviator_rounds SET round_index=? WHERE round_id=? AND round_index=?",
                        (offset + local_index, row["round_id"], local_index),
                    )

            total = int(conn.execute("SELECT COUNT(*) AS n FROM aviator_rounds").fetchone()["n"])
            state = conn.execute(
                "SELECT collector_session_id FROM collector_state WHERE singleton=1"
            ).fetchone()
            contiguous = len(rows)
            warmup_status = "READY" if contiguous >= 100 else "WARMING_UP"
            conn.execute(
                """INSERT INTO collector_state(singleton,collector_state,last_processed_round_id,
                       last_round_timestamp,contiguous_rounds,total_history,warmup_status,
                       collector_session_id,updated_at)
                   VALUES(1,'COLLECTING',?,?,?,?,?,?,clock_timestamp())
                   ON CONFLICT(singleton) DO UPDATE SET collector_state=EXCLUDED.collector_state,
                       last_processed_round_id=EXCLUDED.last_processed_round_id,
                       last_round_timestamp=EXCLUDED.last_round_timestamp,
                       contiguous_rounds=EXCLUDED.contiguous_rounds,
                       total_history=EXCLUDED.total_history,warmup_status=EXCLUDED.warmup_status,
                       collector_session_id=EXCLUDED.collector_session_id,updated_at=clock_timestamp()""",
                (current["round_id"], current["timestamp"], contiguous, total, warmup_status,
                 state["collector_session_id"] if state else None),
            )
            print(json.dumps({"committed": True, "latest_round_index": offset + len(rows),
                              "contiguous_rounds": contiguous, "total_history": total}))
    finally:
        collector_lock.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
