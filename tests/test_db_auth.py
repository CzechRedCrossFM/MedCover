"""Tests for app.db_auth — Azure SQL managed-identity authentication.

Covers the URL rewriting, the token fetch from the identity endpoints, the token
cache and its refresh margin, the pyodbc token struct, and preparing/attaching the
``do_connect`` listener. All network access is mocked (``urlopen`` is patched) and
the listener is attached to a sqlite-backed app with its own ``SQLAlchemy()``
instance, so no Azure identity endpoint or SQL Server connection is needed.
"""

import io
import json
import urllib.parse
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy.dialects.mssql import pyodbc as mssql_pyodbc
from sqlalchemy.engine import URL, make_url

from app import db_auth
from app.db_auth import (
    _EXPIRY_SKEW_SECONDS,
    _extract_client_id,
    _fetch_token,
    _get_token,
    _token_struct,
    attach_msi_token_auth,
    is_msi_url,
    prepare_msi_auth,
    rewrite_msi_url,
)

CLIENT_ID = "712b31b7-ae43-41c9-9c32-69aca6dda993"

# Client id carried as the URL username (the form used in production).
MSI_URL_USERNAME = (
    f"mssql+pyodbc://{CLIENT_ID}@medcover-sql.database.windows.net/MedCover"
    "?driver=ODBC+Driver+18+for+SQL+Server&Authentication=ActiveDirectoryMsi&Encrypt=yes"
)

# Client id carried as a UID query parameter (the form shown in the docs).
MSI_URL_UID_QUERY = (
    "mssql+pyodbc://@medcover-sql.database.windows.net/MedCover"
    "?driver=ODBC+Driver+18+for+SQL+Server&Authentication=ActiveDirectoryMsi"
    f"&UID={CLIENT_ID}&Encrypt=yes"
)

# SQL authentication (local dev / tests) — must be left untouched.
SQL_AUTH_URL = (
    "mssql+pyodbc://SA:DevPassword123!@localhost:1433/medcover_test"
    "?driver=ODBC+Driver+18+for+SQL+Server&Encrypt=no&TrustServerCertificate=yes"
)


IDENTITY_ENDPOINT = "http://127.0.0.1:42356/msi/token"
NOW = 1_800_000_000
TOKEN_EXPIRES_ON = NOW + 3600

# Parametrisation for the two token endpoints: the client_id query parameter is
# sent only when a client id is given.
CLIENT_ID_QUERY_CASES = pytest.mark.parametrize(
    ("client_id", "extra_query"),
    [(CLIENT_ID, {"client_id": [CLIENT_ID]}), (None, {})],
    ids=["with-client-id", "without-client-id"],
)


@pytest.fixture(autouse=True)
def _clear_token_cache():
    db_auth._token_cache.clear()
    yield
    db_auth._token_cache.clear()


def test_is_msi_url_detects_managed_identity() -> None:
    assert is_msi_url(MSI_URL_USERNAME) is True
    assert is_msi_url(MSI_URL_UID_QUERY) is True


def test_is_msi_url_false_for_sql_auth() -> None:
    assert is_msi_url(SQL_AUTH_URL) is False
    assert is_msi_url("") is False


def test_extract_client_id_from_username_and_query() -> None:
    assert _extract_client_id(make_url(MSI_URL_USERNAME)) == CLIENT_ID
    assert _extract_client_id(make_url(MSI_URL_UID_QUERY)) == CLIENT_ID


def test_extract_client_id_prefers_username_over_uid() -> None:
    assert _extract_client_id(make_url(f"mssql+pyodbc://{CLIENT_ID}@h/db?UID=other")) == CLIENT_ID


def test_extract_client_id_from_tuple_uid() -> None:
    assert _extract_client_id(URL.create("mssql+pyodbc", host="h", query={"UID": ("x", "y")})) == "x"
    assert _extract_client_id(URL.create("mssql+pyodbc", host="h", query={"UID": ()})) is None


def test_rewrite_msi_url_returns_client_id() -> None:
    for uri in (MSI_URL_USERNAME, MSI_URL_UID_QUERY):
        _, client_id = rewrite_msi_url(uri)
        assert client_id == CLIENT_ID


def test_rewrite_msi_url_produces_clean_odbc_string() -> None:
    """The rewritten URL must not carry driver-side auth options.

    Checking the actual pyodbc connect string guards against the two failure
    modes we hit in production: a driver ``Trusted_Connection=Yes`` (from an empty
    username) conflicting with Authentication, and the driver attempting its own
    (hanging) MSI token flow.
    """
    clean_url, _ = rewrite_msi_url(MSI_URL_USERNAME)
    cargs, _ = mssql_pyodbc.dialect().create_connect_args(make_url(clean_url))
    odbc = cargs[0]

    assert make_url(clean_url).drivername == "mssql+pyodbc"
    assert "Authentication" not in odbc
    assert "Trusted_Connection" not in odbc
    assert odbc == (
        "Driver={ODBC Driver 18 for SQL Server};Server=tcp:medcover-sql.database.windows.net,1433;"
        "Database=MedCover;Encrypt=yes;TrustServerCertificate=no"
    )


def test_rewrite_msi_url_defaults_driver_and_keeps_explicit_port() -> None:
    clean_url, client_id = rewrite_msi_url("mssql+pyodbc://@h:1444/db?Authentication=ActiveDirectoryMsi")

    assert client_id is None
    assert make_url(clean_url).query["odbc_connect"] == (
        "Driver={ODBC Driver 18 for SQL Server};Server=tcp:h,1444;Database=db;Encrypt=yes;TrustServerCertificate=no"
    )


def test_rewrite_msi_url_uses_first_of_repeated_driver() -> None:
    uri = (
        "mssql+pyodbc://@h/db?driver=ODBC+Driver+18+for+SQL+Server&driver=Other+Driver"
        "&Authentication=ActiveDirectoryMsi"
    )

    clean_url, _ = rewrite_msi_url(uri)

    odbc = make_url(clean_url).query["odbc_connect"]
    assert odbc.startswith("Driver={ODBC Driver 18 for SQL Server};")


def _fake_urlopen(payload: dict) -> MagicMock:
    """A patched ``urlopen`` whose response context manager yields ``payload`` as JSON."""
    response = MagicMock()
    response.__enter__.return_value = io.BytesIO(json.dumps(payload).encode())
    return MagicMock(return_value=response)


def _fetch_with_mocked_urlopen(client_id: str | None):
    """Run ``_fetch_token`` against a mocked urlopen; return (result, request, timeout)."""
    urlopen = _fake_urlopen({"access_token": "tok", "expires_on": "1900000000"})
    with patch("app.db_auth.urllib.request.urlopen", urlopen):
        result = _fetch_token(client_id)
    (request,), kwargs = urlopen.call_args
    return result, request, kwargs["timeout"]


def _url_without_query(request) -> str:
    parts = urllib.parse.urlsplit(request.full_url)
    return f"{parts.scheme}://{parts.netloc}{parts.path}"


def _query(request) -> dict[str, list[str]]:
    return urllib.parse.parse_qs(urllib.parse.urlsplit(request.full_url).query, keep_blank_values=True)


@CLIENT_ID_QUERY_CASES
def test_fetch_token_container_apps_endpoint(monkeypatch, client_id, extra_query) -> None:
    monkeypatch.setenv("IDENTITY_ENDPOINT", IDENTITY_ENDPOINT)
    monkeypatch.setenv("IDENTITY_HEADER", "secret-header")

    result, request, timeout = _fetch_with_mocked_urlopen(client_id)

    assert result == ("tok", 1_900_000_000)
    assert timeout == 15
    assert _url_without_query(request) == IDENTITY_ENDPOINT
    assert _query(request) == {
        "resource": ["https://database.windows.net/"],
        "api-version": ["2019-08-01"],
        **extra_query,
    }
    assert request.header_items() == [("X-identity-header", "secret-header")]


def test_fetch_token_container_apps_header_defaults_to_empty(monkeypatch) -> None:
    monkeypatch.setenv("IDENTITY_ENDPOINT", IDENTITY_ENDPOINT)
    monkeypatch.delenv("IDENTITY_HEADER", raising=False)

    _, request, _ = _fetch_with_mocked_urlopen(None)

    assert request.header_items() == [("X-identity-header", "")]


@CLIENT_ID_QUERY_CASES
def test_fetch_token_imds_fallback(monkeypatch, client_id, extra_query) -> None:
    monkeypatch.delenv("IDENTITY_ENDPOINT", raising=False)
    monkeypatch.setenv("IDENTITY_HEADER", "must-not-be-sent")

    result, request, timeout = _fetch_with_mocked_urlopen(client_id)

    assert result == ("tok", 1_900_000_000)
    assert timeout == 15
    assert _url_without_query(request) == "http://169.254.169.254/metadata/identity/oauth2/token"
    assert _query(request) == {
        "resource": ["https://database.windows.net/"],
        "api-version": ["2018-02-01"],
        **extra_query,
    }
    assert request.header_items() == [("Metadata", "true")]


def test_fetch_token_ignores_empty_identity_endpoint(monkeypatch) -> None:
    monkeypatch.setenv("IDENTITY_ENDPOINT", "")

    _, request, _ = _fetch_with_mocked_urlopen(None)

    assert request.full_url.startswith("http://169.254.169.254/")


class TestGetToken:
    @pytest.fixture
    def fetch(self, monkeypatch):
        """Patch the clock to NOW and ``_fetch_token`` to return successive tokens."""
        monkeypatch.setattr(db_auth, "time", SimpleNamespace(time=lambda: NOW))
        mock = MagicMock(side_effect=[(f"tok{n}", TOKEN_EXPIRES_ON) for n in range(1, 4)])
        monkeypatch.setattr(db_auth, "_fetch_token", mock)
        return mock

    def test_caches_token(self, fetch) -> None:
        assert _get_token(None) == "tok1"
        assert _get_token(None) == "tok1"

        fetch.assert_called_once_with(None)
        assert db_auth._token_cache == {"": ("tok1", TOKEN_EXPIRES_ON)}

    @pytest.mark.parametrize(
        ("expires_in", "refetched"),
        [
            (_EXPIRY_SKEW_SECONDS - 1, True),
            (_EXPIRY_SKEW_SECONDS, True),
            (_EXPIRY_SKEW_SECONDS + 1, False),
        ],
        ids=["inside-margin", "exactly-at-margin", "just-outside-margin"],
    )
    def test_refresh_margin_boundary(self, fetch, expires_in, refetched) -> None:
        db_auth._token_cache[""] = ("old", NOW + expires_in)

        token = _get_token(None)

        assert token == ("tok1" if refetched else "old")
        assert fetch.call_count == (1 if refetched else 0)

    def test_refresh_replaces_cached_entry(self, fetch) -> None:
        db_auth._token_cache[""] = ("old", NOW)

        assert _get_token(None) == "tok1"

        assert db_auth._token_cache == {"": ("tok1", TOKEN_EXPIRES_ON)}

    def test_cache_is_per_client(self, fetch) -> None:
        assert _get_token(None) == "tok1"
        assert _get_token("abc") == "tok2"
        assert _get_token(None) == "tok1"
        assert _get_token("abc") == "tok2"

        assert [call.args for call in fetch.call_args_list] == [(None,), ("abc",)]
        assert db_auth._token_cache == {"": ("tok1", TOKEN_EXPIRES_ON), "abc": ("tok2", TOKEN_EXPIRES_ON)}


def test_token_struct_is_length_prefixed_utf16(monkeypatch) -> None:
    get_token = MagicMock(return_value="ab")
    monkeypatch.setattr(db_auth, "_get_token", get_token)

    assert _token_struct("abc") == b"\x04\x00\x00\x00a\x00b\x00"
    get_token.assert_called_once_with("abc")


def test_prepare_msi_auth_leaves_sql_auth_url_untouched() -> None:
    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = SQL_AUTH_URL

    assert prepare_msi_auth(app) is False

    assert app.config["SQLALCHEMY_DATABASE_URI"] == SQL_AUTH_URL
    assert "_MSI_AUTH" not in app.config
    assert "_MSI_CLIENT_ID" not in app.config


def test_prepare_msi_auth_rewrites_msi_url() -> None:
    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = MSI_URL_USERNAME

    assert prepare_msi_auth(app) is True

    assert app.config["SQLALCHEMY_DATABASE_URI"] == rewrite_msi_url(MSI_URL_USERNAME)[0]
    assert make_url(app.config["SQLALCHEMY_DATABASE_URI"]).drivername == "mssql+pyodbc"
    assert app.config["_MSI_AUTH"] is True
    assert app.config["_MSI_CLIENT_ID"] == CLIENT_ID


class TestAttachMsiTokenAuth:
    @staticmethod
    def _sqlite_app() -> tuple[Flask, SQLAlchemy]:
        app = Flask(__name__)
        app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite://"
        database = SQLAlchemy()
        database.init_app(app)
        return app, database

    @staticmethod
    def _listeners(app: Flask, database: SQLAlchemy) -> list:
        with app.app_context():
            return list(database.engine.dialect.dispatch.do_connect)

    def test_noop_without_msi(self) -> None:
        app, database = self._sqlite_app()

        attach_msi_token_auth(app, database)

        assert self._listeners(app, database) == []

    def test_listener_injects_token_struct(self, monkeypatch) -> None:
        app, database = self._sqlite_app()
        app.config["_MSI_AUTH"] = True
        app.config["_MSI_CLIENT_ID"] = "abc"
        token_struct = MagicMock(return_value=b"S")
        monkeypatch.setattr(db_auth, "_token_struct", token_struct)

        attach_msi_token_auth(app, database)

        (listener,) = self._listeners(app, database)
        cparams = {"existing": 1}
        with app.app_context():
            listener(database.engine.dialect, None, [], cparams)
        assert cparams == {"existing": 1, "attrs_before": {1256: b"S"}}
        token_struct.assert_called_once_with("abc")
