"""Per-statement query timeout for SQL Server connections of the gunicorn web process.

gunicorn's ``gthread`` worker keeps heartbeating while a request thread is stuck in
a hung query, so ``--timeout`` no longer frees it. The timeout is therefore set on
the database connection itself.

It is installed only when ``SERVER_SOFTWARE`` starts with ``gunicorn/``: the gunicorn
master sets that variable in its own environment before loading the application (with
or without ``--preload``), so the workers inherit it. Migrations, the scheduler, the
``flask`` CLI and ``flask run`` do not have it and run without a timeout.
"""

import os
from typing import Any

from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import event

QUERY_TIMEOUT_SECONDS = 30


def attach_query_timeout(app: Flask, db: SQLAlchemy) -> None:
    """Set pyodbc's ``Connection.timeout`` on every new connection under gunicorn.

    A statement that runs longer fails with ``HYT00`` (``OperationalError``).
    Must be called after ``db.init_app(app)`` and before the first DB access.
    """
    if not os.environ.get("SERVER_SOFTWARE", "").startswith("gunicorn/"):
        return
    with app.app_context():
        engine = db.engine

    # insert=True: run before the dialect's first-connect initialization queries,
    # which SQLAlchemy registers when the engine is created.
    @event.listens_for(engine, "connect", insert=True)
    def _set_query_timeout(dbapi_connection: Any, _connection_record: Any) -> None:
        dbapi_connection.timeout = QUERY_TIMEOUT_SECONDS
