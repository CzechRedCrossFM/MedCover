"""Per-statement query timeout for SQL Server connections.

gunicorn's ``gthread`` worker keeps heartbeating while a request thread is stuck in
a hung query, so ``--timeout`` no longer frees it. The timeout is therefore set on
the database connection itself.

It covers the gunicorn web process (30 s, gated on ``SERVER_SOFTWARE`` starting with
``gunicorn/``, which the master sets before loading the application) and the scheduler,
which passes its own limit through ``create_app(query_timeout=...)``. Migrations, the
``flask`` CLI and ``flask run`` have no timeout.

``query_timeout_override`` lifts or changes the limit for one block (the scheduled
backup). The value is reapplied on every checkout and reset on checkin.
"""

import os
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from contextvars import ContextVar
from typing import Any

import pyodbc
from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import event
from sqlalchemy.exc import DisconnectionError

QUERY_TIMEOUT_SECONDS = 30

_timeout_override: ContextVar[int | None] = ContextVar("query_timeout_override", default=None)


def attach_query_timeout(app: Flask, db: SQLAlchemy, seconds: int | None = None) -> None:
    """Set pyodbc's ``Connection.timeout`` on every connection, under gunicorn or with ``seconds`` given.

    A statement that runs longer fails with ``HYT00`` (``OperationalError``).
    ``seconds=None`` means the default limit under gunicorn and none elsewhere; ``0`` means none.
    Must be called after ``db.init_app(app)`` and before the first DB access.
    """
    if seconds is None:
        if not os.environ.get("SERVER_SOFTWARE", "").startswith("gunicorn/"):
            return
        seconds = QUERY_TIMEOUT_SECONDS
    if not seconds:
        return
    with app.app_context():
        engine = db.engine

    def _set_query_timeout(dbapi_connection: Any, *_args: Any) -> None:
        override = _timeout_override.get()
        dbapi_connection.timeout = seconds if override is None else override

    # insert=True: run before the dialect's first-connect initialization queries,
    # which SQLAlchemy registers when the engine is created.
    event.listen(engine, "connect", _set_query_timeout, insert=True)

    @event.listens_for(engine, "checkout")
    def _apply_query_timeout(dbapi_connection: Any, *_args: Any) -> None:
        try:
            _set_query_timeout(dbapi_connection)
        except pyodbc.Error as exc:
            # A pooled connection the server or driver has closed: the pool
            # discards it and retries the checkout with a fresh connection.
            raise DisconnectionError(str(exc)) from exc

    @event.listens_for(engine, "checkin")
    def _reset_query_timeout(dbapi_connection: Any, _connection_record: Any) -> None:
        # None after the connection was invalidated
        if dbapi_connection is None:
            return
        # Raising here would keep the connection out of the pool; a closed
        # connection is discarded on its next checkout anyway.
        with suppress(pyodbc.Error):
            dbapi_connection.timeout = seconds


@contextmanager
def query_timeout_override(seconds: int) -> Iterator[None]:
    """Use ``seconds`` (0 = no limit) as the statement timeout for connections checked out inside the block.

    Applies to the current thread only. Enter it before the session's first query;
    no-op where no timeout is installed.
    """
    token = _timeout_override.set(seconds)
    try:
        yield
    finally:
        _timeout_override.reset(token)
