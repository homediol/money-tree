import json
import sqlite3
from contextlib import contextmanager

from app.database.repository import Repository


def test_target_guard_uses_current_canonical_rounds(tmp_path):
    path = tmp_path / "rounds.sqlite"
    connection = sqlite3.connect(path)
    connection.executescript("""
        CREATE TABLE round_distribution_meta (singleton INTEGER, stream_id TEXT);
        CREATE TABLE aviator_rounds (round_id TEXT, round_index INTEGER);
        CREATE TABLE round_distribution_events
            (stream_id TEXT, round_id TEXT, round_index INTEGER,
             stream_sequence INTEGER, payload TEXT);
        INSERT INTO round_distribution_meta VALUES (1, 'stream');
        INSERT INTO round_distribution_events VALUES
            ('stream', 'obsolete', 101, 1, '{"round_id":"obsolete"}');
    """)
    connection.commit()
    connection.close()

    repository = Repository.__new__(Repository)
    repository.database_url = "postgresql://test"

    @contextmanager
    def connect():
        db = sqlite3.connect(path)
        db.row_factory = lambda cursor, row: dict(zip((col[0] for col in cursor.description), row))
        try:
            yield db
            db.commit()
        finally:
            db.close()

    repository.connect = connect
    assert not repository.has_committed_round_distribution_index("stream", 101)
    assert repository.committed_round_distribution_event_by_index("stream", 101) is None

    with connect() as db:
        db.execute("INSERT INTO aviator_rounds VALUES (?, ?)", ("current", 101))
    # The target is complete even if its distribution event has not arrived yet.
    assert repository.has_committed_round_distribution_index("stream", 101)
    assert not repository.has_committed_round_distribution_index("other-stream", 101)
    assert repository.committed_round_distribution_event_by_index("stream", 101) is None

    with connect() as db:
        db.execute("INSERT INTO round_distribution_events VALUES (?, ?, ?, ?, ?)",
                   ("stream", "current", 101, 2, json.dumps({"round_id": "current"})))
    assert repository.committed_round_distribution_event_by_index("stream", 101)["round_id"] == "current"
