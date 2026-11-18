from sqlalchemy.orm import Session

from behaviour_detection.db import make_engine, migrate
from behaviour_detection.models import SystemHealth


def test_worker_survives_failed_pass_and_retries_pending_rounds(monkeypatch, tmp_path):
    database_file = tmp_path / "worker.sqlite3"
    engine = make_engine(f"sqlite:///{database_file}")
    migrate(engine)
    monkeypatch.setattr("behaviour_detection.db.make_engine", lambda _url: engine)
    monkeypatch.setattr("behaviour_detection.worker.time.sleep", lambda _seconds: None)
    calls = 0

    def fail_then_interrupt(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("transient processing failure")
        raise KeyboardInterrupt

    monkeypatch.setattr("behaviour_detection.worker.analyse_pending", fail_then_interrupt)
    from behaviour_detection.worker import run_worker

    try:
        run_worker("sqlite:///fixture.sqlite3", poll_seconds=.25)
    except KeyboardInterrupt:
        pass
    else:
        raise AssertionError("worker did not remain in its retry loop")

    check_engine = make_engine(f"sqlite:///{database_file}")
    with Session(check_engine) as session:
        health = session.get(SystemHealth, "analysis_worker")
        assert calls == 2
        assert health.state == "ERROR"
        assert health.details == {"exception_type": "RuntimeError", "retrying": True}
    check_engine.dispose()
