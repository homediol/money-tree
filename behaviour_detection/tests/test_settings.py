from sqlalchemy.engine import make_url

from behaviour_detection import settings


def test_database_url_escapes_reserved_password_characters(monkeypatch):
    monkeypatch.delenv("BEHAVIOUR_DATABASE_URL", raising=False)
    monkeypatch.setenv("BEHAVIOUR_DB_USER", "behaviour_user")
    monkeypatch.setenv("BEHAVIOUR_DB_PASSWORD", "p@ss:/% word")
    monkeypatch.setenv("BEHAVIOUR_DB_NAME", "behaviour_detection")
    monkeypatch.setenv("BEHAVIOUR_DB_HOST", "127.0.0.1")
    monkeypatch.setenv("BEHAVIOUR_DB_PORT", "55432")
    url = make_url(settings._database_url())
    assert url.password == "p@ss:/% word"
    assert url.host == "127.0.0.1"
    assert url.database == "behaviour_detection"
