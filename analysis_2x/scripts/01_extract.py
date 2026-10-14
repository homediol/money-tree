#!/usr/bin/env python3
"""01_extract.py - Extract the authoritative aviator_rounds snapshot from PostgreSQL.

Preserves the source exactly: no cleaning here. Records an extraction manifest with
row counts, SHA-256 of the exported CSV, extraction query boundary and wall-clock time.
"""
import hashlib
import json
import os
import sys
from datetime import datetime, timezone

import psycopg2
import psycopg2.extras

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ART = os.path.join(ROOT, "analysis_2x", "artifacts")
os.makedirs(ART, exist_ok=True)

DSN = "dbname=winner_predict host=/var/run/postgresql"
OUT_CSV = os.path.join(ART, "raw_rounds.csv")
OUT_MANIFEST = os.path.join(ART, "extraction_manifest.json")

COLS = [
    "id", "round_id", "round_index", "multiplier", "timestamp",
    "platform_timestamp", "observed_at", "stored_at", "source",
    "round_identity_type", "round_index_source", "continuity_verified",
    "gap_before", "continuity_proof", "platform_round_id",
    "platform_round_index", "local_round_index", "identity_confidence",
]


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> None:
    conn = psycopg2.connect(DSN)
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    # Extraction boundary: full table, deterministic order by round_index then id.
    query = (
        "SELECT " + ", ".join(COLS) +
        " FROM aviator_rounds ORDER BY round_index, id"
    )
    cur.execute(query)
    rows = cur.fetchall()

    # Table-level provenance counts (independent of the extracted row set).
    cur.execute("SELECT source, COUNT(*) FROM aviator_rounds GROUP BY source ORDER BY source")
    source_counts = {r["source"]: r["count"] for r in cur.fetchall()}
    cur.execute("SELECT COUNT(*) FROM aviator_rounds")
    table_count = cur.fetchone()["count"]
    cur.execute("SELECT pg_database_size(current_database())")
    db_size = cur.fetchone()["pg_database_size"]
    conn.close()

    assert len(rows) == table_count, f"extracted {len(rows)} != table {table_count}"

    import csv
    with open(OUT_CSV, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(COLS)
        for r in rows:
            w.writerow([
                r[c].isoformat() if isinstance(r[c], datetime) else
                ("" if r[c] is None else r[c])
                for c in COLS
            ])

    manifest = {
        "extracted_at_utc": datetime.now(timezone.utc).isoformat(),
        "database": "winner_predict (local socket /var/run/postgresql)",
        "table": "public.aviator_rounds",
        "extraction_query": query,
        "extraction_boundary": "entire table, all sources, no WHERE filter",
        "table_row_count": table_count,
        "extracted_row_count": len(rows),
        "source_counts": source_counts,
        "database_size_bytes": db_size,
        "raw_rounds_csv_sha256": sha256_file(OUT_CSV),
        "columns": COLS,
        "notes": [
            "Snapshot is immutable evidence; cleaning happens downstream in 02_audit_clean.py.",
            "round_index is a local collector sequence (round_index_source=LOCAL_SEQUENCE),",
            "not a verified platform identifier for legacy_import rows.",
        ],
    }
    with open(OUT_MANIFEST, "w") as f:
        json.dump(manifest, f, indent=2)

    print(json.dumps({k: manifest[k] for k in (
        "table_row_count", "extracted_row_count", "source_counts",
        "raw_rounds_csv_sha256")}, indent=2))


if __name__ == "__main__":
    sys.exit(main())
