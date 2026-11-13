"""Tests for app.db_timeout — per-statement query timeout on SQL Server."""

import time

import pytest
from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError

from app.db_timeout import attach_query_timeout, install_query_timeout, query_timeout_from_env


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(None, 0), ("", 0), ("30", 30), (" 15 ", 15), ("0", 0), ("-5", 0), ("abc", 0)],
)
def test_query_timeout_from_env(monkeypatch: pytest.MonkeyPatch, raw: str | None, expected: int) -> None:
    if raw is None:
        monkeypatch.delenv("DB_QUERY_TIMEOUT_SECONDS", raising=False)
    else:
        monkeypatch.setenv("DB_QUERY_TIMEOUT_SECONDS", raw)
    assert query_timeout_from_env() == expected


def test_install_is_noop_when_disabled() -> None:
    engine = create_engine("sqlite://")
    assert install_query_timeout(engine, 0) is False


def test_install_is_noop_for_non_pyodbc_driver() -> None:
    engine = create_engine("sqlite://")
    assert install_query_timeout(engine, 5) is False


MSSQL_URL = "mssql+pyodbc://u:p@localhost/db?driver=ODBC+Driver+18+for+SQL+Server"


def _timeout_listeners(engine: Engine) -> list:
    return [fn for fn in engine.pool.dispatch.connect if getattr(fn, "__name__", "") == "_set_query_timeout"]


@pytest.mark.parametrize(("seconds", "installed"), [(0, False), (None, False), (7, True)])
def test_attach_query_timeout_follows_app_config(seconds: int | None, installed: bool) -> None:
    flask_app = Flask(__name__)
    flask_app.config["SQLALCHEMY_DATABASE_URI"] = MSSQL_URL
    flask_app.config["DB_QUERY_TIMEOUT_SECONDS"] = seconds
    sa = SQLAlchemy()
    sa.init_app(flask_app)
    assert attach_query_timeout(flask_app, sa) is installed
    with flask_app.app_context():
        assert len(_timeout_listeners(sa.engine)) == (1 if installed else 0)


def test_timeout_listener_runs_before_dialect_initialization() -> None:
    engine = create_engine(MSSQL_URL)
    install_query_timeout(engine, 5)
    first = next(iter(engine.pool.dispatch.connect))
    assert getattr(first, "__name__", "") == "_set_query_timeout"

    class FakeConnection:
        timeout = 0

    conn = FakeConnection()
    first(conn, None)
    assert conn.timeout == 5


def test_slow_statement_is_cancelled(app: Flask) -> None:
    engine = create_engine(app.config["SQLALCHEMY_DATABASE_URI"])
    try:
        assert install_query_timeout(engine, 1) is True
        with engine.connect() as conn:
            assert conn.execute(text("SELECT 1")).scalar() == 1
            started = time.monotonic()
            with pytest.raises(OperationalError, match="HYT00|timeout"):
                conn.execute(text("WAITFOR DELAY '00:00:05'"))
            assert time.monotonic() - started < 4
    finally:
        engine.dispose()


def test_fast_statement_unaffected_without_timeout(app: Flask) -> None:
    engine = create_engine(app.config["SQLALCHEMY_DATABASE_URI"])
    try:
        with engine.connect() as conn:
            conn.execute(text("WAITFOR DELAY '00:00:01'"))
            assert conn.execute(text("SELECT 1")).scalar() == 1
    finally:
        engine.dispose()
