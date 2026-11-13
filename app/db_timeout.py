"""Per-statement query timeout for SQL Server connections.

gunicorn's ``gthread`` worker keeps heartbeating from its main loop while request
threads run, so the arbiter's ``--timeout`` no longer kills a worker whose thread
is stuck in a hung query (the ``sync`` worker was killed after 30 s). Without a
query timeout, a few hung statements would hold request threads indefinitely.

``DB_QUERY_TIMEOUT_SECONDS`` sets pyodbc's ``Connection.timeout`` on every new
DBAPI connection; pyodbc applies it as ``SQL_ATTR_QUERY_TIMEOUT`` to each cursor,
and a statement that runs longer fails with ``HYT00`` (``OperationalError``),
which the request-level rollback (#519) already handles.

The value is 0 (no timeout) unless set. The Dockerfile ``CMD`` sets it only for
the gunicorn web process, from ``WEB_DB_QUERY_TIMEOUT_SECONDS`` (default 30), so
migrations in the entrypoint and the scheduler, which can legitimately run long
statements, are not affected. Configure the web timeout with
``WEB_DB_QUERY_TIMEOUT_SECONDS`` (0 disables it). The gunicorn command always
overrides ``DB_QUERY_TIMEOUT_SECONDS``, so setting that in the container
environment does not change the web timeout; it applies only to the
entrypoint migrations and the scheduler.
"""

import os
from typing import Any

from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import event
from sqlalchemy.engine import Engine


def query_timeout_from_env() -> int:
    """Seconds from ``DB_QUERY_TIMEOUT_SECONDS``; 0 (disabled) if unset or invalid."""
    raw = os.environ.get("DB_QUERY_TIMEOUT_SECONDS", "").strip()
    try:
        seconds = int(raw)
    except ValueError:
        return 0
    return max(seconds, 0)


def install_query_timeout(engine: Engine, seconds: int) -> bool:
    """Set the query timeout on each new pyodbc connection of ``engine``.

    Returns True when the listener was installed. No-op for ``seconds <= 0`` or a
    non-pyodbc driver.
    """
    if seconds <= 0 or engine.dialect.driver != "pyodbc":
        return False

    # insert=True: run before the dialect's first-connect initialization queries,
    # which SQLAlchemy registers when the engine is created.
    @event.listens_for(engine, "connect", insert=True)
    def _set_query_timeout(dbapi_connection: Any, connection_record: Any) -> None:
        dbapi_connection.timeout = seconds

    return True


def attach_query_timeout(app: Flask, db: SQLAlchemy) -> bool:
    """Install the query timeout on the app's engine from ``DB_QUERY_TIMEOUT_SECONDS``.

    Must be called after ``db.init_app(app)`` and before the first DB access.
    """
    seconds = int(app.config.get("DB_QUERY_TIMEOUT_SECONDS") or 0)
    if seconds <= 0:
        return False
    with app.app_context():
        engine = db.engine
    return install_query_timeout(engine, seconds)
