"""Per-statement query timeout for SQL Server connections.

gunicorn's ``gthread`` worker keeps heartbeating while a request thread is stuck in
a hung query, so ``--timeout`` no longer frees it. The Dockerfile ``CMD`` sets
``DB_QUERY_TIMEOUT_SECONDS`` for the gunicorn process only; migrations in the
entrypoint and the scheduler run without a timeout. Setting the variable in the
container environment does not change the web timeout; it would apply to the
entrypoint migrations (and a compose-run scheduler) instead.
"""

from typing import Any

from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import event


def attach_query_timeout(app: Flask, db: SQLAlchemy) -> None:
    """Set pyodbc's ``Connection.timeout`` on every new connection; 0 disables it.

    A statement that runs longer fails with ``HYT00`` (``OperationalError``).
    Must be called after ``db.init_app(app)`` and before the first DB access.
    """
    seconds = app.config["DB_QUERY_TIMEOUT_SECONDS"]
    if seconds <= 0:
        return
    with app.app_context():
        engine = db.engine

    # insert=True: run before the dialect's first-connect initialization queries,
    # which SQLAlchemy registers when the engine is created.
    @event.listens_for(engine, "connect", insert=True)
    def _set_query_timeout(dbapi_connection: Any, _connection_record: Any) -> None:
        dbapi_connection.timeout = seconds
