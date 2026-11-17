"""Tests for app.db_timeout — per-statement query timeout on SQL Server."""

import os
import shutil
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError

from app import create_app, db_timeout
from app.db_timeout import attach_query_timeout, query_timeout_override
from app.extensions import db

GUNICORN_SOFTWARE = "gunicorn/99.0"


def _make_app(db_url: str) -> tuple[Flask, SQLAlchemy]:
    flask_app = Flask(__name__)
    flask_app.config["SQLALCHEMY_DATABASE_URI"] = db_url
    sa = SQLAlchemy()
    sa.init_app(flask_app)
    attach_query_timeout(flask_app, sa)
    return flask_app, sa


@pytest.fixture
def timeout_app(app: Flask, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[Flask, SQLAlchemy]]:
    """A separate app on the test database, built as if running under gunicorn with a 1 s timeout."""
    monkeypatch.setenv("SERVER_SOFTWARE", GUNICORN_SOFTWARE)
    monkeypatch.setattr(db_timeout, "QUERY_TIMEOUT_SECONDS", 1)
    flask_app, sa = _make_app(app.config["SQLALCHEMY_DATABASE_URI"])
    with flask_app.app_context():
        try:
            yield flask_app, sa
        finally:
            sa.session.remove()
            sa.engine.dispose()


def _dbapi_timeout(sa: SQLAlchemy) -> int:
    dbapi_connection = sa.session.connection().connection.dbapi_connection
    assert dbapi_connection is not None
    return int(dbapi_connection.timeout)


def test_slow_statement_is_cancelled_and_session_recovers(timeout_app: tuple[Flask, SQLAlchemy]) -> None:
    _, sa = timeout_app
    # The engine's very first connection (which also runs the dialect's
    # initialization queries) already carries the timeout.
    assert _dbapi_timeout(sa) == 1

    started = time.monotonic()
    with pytest.raises(OperationalError, match="HYT00") as excinfo:
        sa.session.execute(text("WAITFOR DELAY '00:00:05'"))
    assert time.monotonic() - started < 4
    # SQLAlchemy treats HYT00 as a disconnect: the connection is discarded, not reused.
    assert excinfo.value.connection_invalidated

    # After the rollback a request does at teardown, the session works again on a
    # fresh connection, which also gets the timeout.
    sa.session.rollback()
    assert sa.session.execute(text("SELECT 1")).scalar() == 1
    assert _dbapi_timeout(sa) == 1


@pytest.mark.parametrize("server_software", [None, "Werkzeug/3.1", "notgunicorn/1.0"])
def test_timeout_not_installed_outside_gunicorn(
    app: Flask, monkeypatch: pytest.MonkeyPatch, server_software: str | None
) -> None:
    if server_software is None:
        monkeypatch.delenv("SERVER_SOFTWARE", raising=False)
    else:
        monkeypatch.setenv("SERVER_SOFTWARE", server_software)
    monkeypatch.setattr(db_timeout, "QUERY_TIMEOUT_SECONDS", 1)
    flask_app, sa = _make_app(app.config["SQLALCHEMY_DATABASE_URI"])
    with flask_app.app_context():
        try:
            assert "_set_query_timeout" not in _connect_listener_names(sa.engine)
            assert _dbapi_timeout(sa) == 0
            sa.session.execute(text("WAITFOR DELAY '00:00:02'"))
        finally:
            sa.session.remove()
            sa.engine.dispose()


def test_timeout_listener_runs_before_dialect_initialization(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SERVER_SOFTWARE", GUNICORN_SOFTWARE)
    url = "mssql+pyodbc://u:p@localhost/db?driver=ODBC+Driver+18+for+SQL+Server"
    flask_app, sa = _make_app(url)
    with flask_app.app_context():
        listeners = _connect_listener_names(sa.engine)
    assert listeners[0] == "_set_query_timeout"
    assert listeners.count("_set_query_timeout") == 1


def _connect_listener_names(engine: Engine) -> list[str]:
    return [getattr(fn, "__name__", "") for fn in engine.pool.dispatch.connect]


@pytest.mark.parametrize(
    ("server_software", "query_timeout", "expected"),
    [(None, None, 0), (GUNICORN_SOFTWARE, None, 30), (None, 7, 7), (None, 0, 0)],
)
def test_create_app_query_timeout(
    app: Flask, monkeypatch: pytest.MonkeyPatch, server_software: str | None, query_timeout: int | None, expected: int
) -> None:
    if server_software is None:
        monkeypatch.delenv("SERVER_SOFTWARE", raising=False)
    else:
        monkeypatch.setenv("SERVER_SOFTWARE", server_software)
    flask_app = create_app("testing", db_url=app.config["SQLALCHEMY_DATABASE_URI"], query_timeout=query_timeout)
    with flask_app.app_context():
        try:
            names = _connect_listener_names(db.engine)
            assert (names[:1] == ["_set_query_timeout"]) is bool(expected)
            assert names.count("_set_query_timeout") == (1 if expected else 0)
            with db.engine.connect() as connection:
                assert connection.connection.dbapi_connection.timeout == expected
        finally:
            db.engine.dispose()


def test_override_lifts_timeout_and_is_restored(timeout_app: tuple[Flask, SQLAlchemy]) -> None:
    _, sa = timeout_app
    with pytest.raises(RuntimeError), query_timeout_override(0):
        sa.session.execute(text("WAITFOR DELAY '00:00:01.500'"))
        sa.session.commit()
        # re-checkout after the commit keeps the override
        assert _dbapi_timeout(sa) == 0
        raise RuntimeError("job failed")
    sa.session.remove()
    # before any checkout reapplies the value: only the checkin reset can have restored it
    assert sa.engine.pool.checkedin() == 1
    pooled = sa.engine.pool._pool.queue[0].dbapi_connection
    assert pooled.timeout == 1
    assert _dbapi_timeout(sa) == 1


def test_closed_pooled_connection_is_replaced_on_checkout(timeout_app: tuple[Flask, SQLAlchemy]) -> None:
    _, sa = timeout_app
    assert _dbapi_timeout(sa) == 1
    sa.session.remove()
    pooled = sa.engine.pool._pool.queue[0].dbapi_connection
    pooled.close()  # closed underneath the pool, e.g. by the driver after a network drop
    # Checkin must not raise either, or the connection would never return to the pool.
    for listener in sa.engine.pool.dispatch.checkin:
        listener(pooled, None)

    assert _dbapi_timeout(sa) == 1
    assert sa.session.connection().connection.dbapi_connection is not pooled


PROBE_APP = """\
import os

with open(os.environ["PROBE_OUT"], "w", encoding="utf-8") as fh:
    fh.write(os.environ.get("SERVER_SOFTWARE", ""))


def application(environ, start_response):
    start_response("200 OK", [("Content-Type", "text/plain")])
    return [b"ok"]
"""


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port: int = sock.getsockname()[1]
        return port


@pytest.mark.parametrize("preload", [False, True])
def test_gunicorn_sets_server_software_before_app_load(tmp_path: Path, preload: bool) -> None:
    """Guards against a gunicorn upgrade that stops exporting SERVER_SOFTWARE before app load."""
    gunicorn = shutil.which("gunicorn", path=str(Path(sys.executable).parent))
    assert gunicorn is not None
    (tmp_path / "probe_app.py").write_text(PROBE_APP, encoding="utf-8")
    out = tmp_path / "server_software"
    env = {k: v for k, v in os.environ.items() if k != "SERVER_SOFTWARE"}
    env["PROBE_OUT"] = str(out)
    cmd = [gunicorn, "-k", "gthread", "-w", "1", "-b", f"127.0.0.1:{_free_port()}", "--chdir", str(tmp_path)]
    if preload:
        cmd.append("--preload")
    cmd.append("probe_app:application")

    proc = subprocess.Popen(cmd, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.monotonic() + 20
        while not out.exists() and proc.poll() is None and time.monotonic() < deadline:
            time.sleep(0.1)
        assert out.exists(), "gunicorn did not load the probe app"
        assert out.read_text(encoding="utf-8").startswith("gunicorn/")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
