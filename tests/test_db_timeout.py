"""Tests for app.db_timeout — per-statement query timeout on SQL Server."""

import importlib.util
import json
import os
import re
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType

import pytest
from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError

from app import create_app
from app.config import TestingConfig
from app.db_timeout import attach_query_timeout
from app.extensions import db

REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_config_module(monkeypatch: pytest.MonkeyPatch, raw: str | None) -> ModuleType:
    """Import a fresh copy of app/config.py with DB_QUERY_TIMEOUT_SECONDS set to ``raw``."""
    if raw is None:
        monkeypatch.delenv("DB_QUERY_TIMEOUT_SECONDS", raising=False)
    else:
        monkeypatch.setenv("DB_QUERY_TIMEOUT_SECONDS", raw)
    spec = importlib.util.spec_from_file_location("_config_probe", REPO_ROOT / "app" / "config.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(("raw", "expected"), [(None, 0), ("0", 0), ("30", 30)])
def test_config_reads_timeout_from_env(monkeypatch: pytest.MonkeyPatch, raw: str | None, expected: int) -> None:
    config = _load_config_module(monkeypatch, raw)
    for name in ("development", "testing", "production"):
        assert config.config_by_name[name].DB_QUERY_TIMEOUT_SECONDS == expected


def test_config_rejects_invalid_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValueError):
        _load_config_module(monkeypatch, "abc")


def _make_app(db_url: str, seconds: int) -> tuple[Flask, SQLAlchemy]:
    flask_app = Flask(__name__)
    flask_app.config["SQLALCHEMY_DATABASE_URI"] = db_url
    flask_app.config["DB_QUERY_TIMEOUT_SECONDS"] = seconds
    sa = SQLAlchemy()
    sa.init_app(flask_app)
    attach_query_timeout(flask_app, sa)
    return flask_app, sa


@pytest.fixture
def timeout_app(app: Flask, request: pytest.FixtureRequest) -> Iterator[tuple[Flask, SQLAlchemy]]:
    """A separate app on the test database with the timeout given by ``request.param``."""
    flask_app, sa = _make_app(app.config["SQLALCHEMY_DATABASE_URI"], request.param)
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


@pytest.mark.parametrize("timeout_app", [1], indirect=True)
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


@pytest.mark.parametrize("timeout_app", [0, -1], indirect=True)
def test_zero_or_negative_timeout_is_disabled(timeout_app: tuple[Flask, SQLAlchemy]) -> None:
    _, sa = timeout_app
    assert "_set_query_timeout" not in _connect_listener_names(sa.engine)
    assert _dbapi_timeout(sa) == 0
    sa.session.execute(text("WAITFOR DELAY '00:00:01'"))


def test_timeout_listener_runs_before_dialect_initialization() -> None:
    url = "mssql+pyodbc://u:p@localhost/db?driver=ODBC+Driver+18+for+SQL+Server"
    flask_app, sa = _make_app(url, 5)
    with flask_app.app_context():
        listeners = _connect_listener_names(sa.engine)
    assert listeners[0] == "_set_query_timeout"
    assert listeners.count("_set_query_timeout") == 1


def _connect_listener_names(engine: Engine) -> list[str]:
    return [getattr(fn, "__name__", "") for fn in engine.pool.dispatch.connect]


@pytest.mark.parametrize(("seconds", "installed"), [(0, False), (5, True)])
def test_create_app_attaches_timeout_from_config(
    app: Flask, monkeypatch: pytest.MonkeyPatch, seconds: int, installed: bool
) -> None:
    monkeypatch.setattr(TestingConfig, "DB_QUERY_TIMEOUT_SECONDS", seconds)
    flask_app = create_app("testing", db_url=app.config["SQLALCHEMY_DATABASE_URI"])
    with flask_app.app_context():
        names = _connect_listener_names(db.engine)
        db.engine.dispose()
    assert (names[:1] == ["_set_query_timeout"]) is installed
    assert names.count("_set_query_timeout") == (1 if installed else 0)


def _dockerfile_cmd() -> list[str]:
    match = re.search(r"^CMD (\[.*\])$", (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8"), re.MULTILINE)
    assert match is not None
    cmd: list[str] = json.loads(match.group(1))
    return cmd


def test_timeout_applies_to_gunicorn_but_not_entrypoint_migrations(tmp_path: Path) -> None:
    """Run the real entrypoint and Dockerfile CMD with stub ``flask`` and ``gunicorn``."""
    seen = tmp_path / "seen"
    for name in ("flask", "gunicorn"):
        stub = tmp_path / name
        stub.write_text(f'#!/bin/sh\necho "{name} $1 timeout=${{DB_QUERY_TIMEOUT_SECONDS:-unset}}" >> "{seen}"\n')
        stub.chmod(0o755)
    env = {k: v for k, v in os.environ.items() if k != "DB_QUERY_TIMEOUT_SECONDS"}
    env["PATH"] = f"{tmp_path}{os.pathsep}{env['PATH']}"

    subprocess.run(["sh", str(REPO_ROOT / "docker-entrypoint.sh"), *_dockerfile_cmd()], env=env, check=True)

    assert seen.read_text(encoding="utf-8").splitlines() == [
        "flask db timeout=unset",
        "flask verify-schema timeout=unset",
        "gunicorn -w timeout=30",
    ]
