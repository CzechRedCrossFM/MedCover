"""CLI commands and SMTP startup wiring in ``app/__init__.py``."""

from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from click.testing import CliRunner
from sqlalchemy import Column, Integer, MetaData, Table

import app as app_module
from app import create_app
from app.extensions import db
from app.extensions import mail as flask_mail
from app.models.settings import get_settings

SMTP_HOST = "smtp.example.com"
SMTP_PORT = 2525
SMTP_USER = "odesilatel@example.com"
SMTP_SENDER = "medcover@example.com"
RECIPIENT = "prijemce@example.com"


def _invoke(app, command: str, args: list[str] | None = None):
    with app.app_context():
        return CliRunner().invoke(app.cli.commands[command], args or [], catch_exceptions=False)


def _configure_smtp(app, use_tls: bool = True) -> None:
    with app.app_context():
        settings = get_settings()
        settings.smtp_server = SMTP_HOST
        settings.smtp_port = SMTP_PORT
        settings.smtp_username = SMTP_USER
        settings.set_smtp_password("tajne-heslo")
        settings.smtp_default_sender = SMTP_SENDER
        settings.smtp_use_tls = use_tls
        db.session.commit()


class TestVerifySchema:
    def test_ok_counts_every_model_table(self, app):
        result = _invoke(app, "verify-schema")

        with app.app_context():
            expected = len(db.metadata.tables)
        assert result.exit_code == 0
        assert result.output == f"Schema OK — verified {expected} tables, all columns present.\n"

    def test_missing_table_and_column_are_listed(self, app):
        with app.app_context():
            tables = {name: [c.name for c in t.columns] for name, t in db.metadata.tables.items()}
        missing_table = "digest_block"
        assert missing_table in tables
        assert "color" in tables["event"]
        inspector = Mock()
        inspector.get_table_names.return_value = [t for t in tables if t != missing_table]
        present = {
            t: [c for c in cols if (t, c) != ("event", "color")] for t, cols in tables.items() if t != missing_table
        }
        inspector.get_columns.side_effect = lambda t: [{"name": c} for c in present[t]]

        with patch("app.sa_inspect", return_value=inspector):
            result = _invoke(app, "verify-schema")

        lines = result.output.splitlines()
        assert result.exit_code == 1
        assert lines[0] == "Schema verification FAILED — the following objects are missing from the DB:"
        assert sorted(lines[1:3]) == ["  ✘ MISSING COLUMN: event.color", "  ✘ MISSING TABLE: digest_block"]
        assert lines[3:] == [
            "",
            "Possible causes: migration was stamped but not applied ('flask db stamp'), "
            "DB restored from incomplete backup, or a migration script had an error.",
            "Fix: drop alembic_version and re-run 'flask db upgrade'.",
        ]

    def test_alembic_version_is_not_verified(self, app):
        metadata = MetaData()
        Table("alembic_version", metadata, Column("version_num", Integer))
        Table("event", metadata, Column("id", Integer))
        inspector = Mock()
        inspector.get_table_names.return_value = ["event"]
        inspector.get_columns.return_value = [{"name": "id"}]
        fake_db = SimpleNamespace(engine=object(), metadata=metadata)

        with patch("app.sa_inspect", return_value=inspector), patch("app.db", fake_db):
            result = _invoke(app, "verify-schema")

        assert result.exit_code == 0
        assert result.output == "Schema OK — verified 1 tables, all columns present.\n"


@pytest.mark.usefixtures("preserve_mail_config")
class TestSendTestEmail:
    @pytest.mark.parametrize(("use_tls", "body_tls"), [(True, "ano"), (False, "ne")], ids=["tls", "no-tls"])
    def test_success(self, app, use_tls, body_tls):
        _configure_smtp(app, use_tls=use_tls)
        assert app.config.get("MAIL_SERVER") != SMTP_HOST
        fake_time = SimpleNamespace(monotonic=Mock(side_effect=[0.0, 0.012, 1.0, 1.034]))

        with (
            patch("app.socket") as fake_socket,
            patch.object(flask_mail, "send") as send,
            patch.object(app_module, "_time", fake_time),
        ):
            result = _invoke(app, "send-test-email", [RECIPIENT])

        assert result.exit_code == 0
        assert result.output.splitlines() == [
            f"SMTP server : {SMTP_HOST}:{SMTP_PORT}",
            f"Username    : {SMTP_USER}",
            f"TLS         : {use_tls}",
            f"Sender      : {SMTP_SENDER}",
            f"Recipient   : {RECIPIENT}",
            "",
            f"1/2  Checking TCP connectivity to {SMTP_HOST}:{SMTP_PORT} … OK (12 ms)",
            "2/2  Sending test email via SMTP … OK (34 ms)",
            "",
            f"✔ Test email successfully sent to {RECIPIENT}",
        ]
        fake_socket.create_connection.assert_called_once_with((SMTP_HOST, SMTP_PORT), timeout=5)
        send.assert_called_once()
        (msg,) = send.call_args.args
        assert msg.subject == "MedCover — Test SMTP"
        assert msg.recipients == [RECIPIENT]
        assert msg.sender == SMTP_SENDER
        assert msg.body == (
            "Tento e-mail byl odeslán jako test konfigurace SMTP.\n\n"
            f"Server:   {SMTP_HOST}:{SMTP_PORT}\n"
            f"Odesílatel: {SMTP_SENDER}\n"
            f"TLS: {body_tls}\n\n"
            "Pokud jste tento e-mail obdrželi, konfigurace SMTP je správná."
        )
        assert app.config["MAIL_SERVER"] == SMTP_HOST
        assert app.config["MAIL_PORT"] == SMTP_PORT

    @pytest.mark.parametrize(
        "error",
        [OSError("refused"), TimeoutError("timed out")],
        ids=["refused", "timeout"],
    )
    def test_connect_failure_exits_before_sending(self, app, error):
        _configure_smtp(app)

        with (
            patch("app.socket") as fake_socket,
            patch.object(flask_mail, "send") as send,
            patch.object(app_module, "_time", SimpleNamespace(monotonic=Mock(return_value=0.0))),
        ):
            fake_socket.create_connection.side_effect = error
            result = _invoke(app, "send-test-email", [RECIPIENT])

        assert result.exit_code == 1
        assert result.output.splitlines()[-1] == (
            f"1/2  Checking TCP connectivity to {SMTP_HOST}:{SMTP_PORT} … FAILED — {error}"
        )
        send.assert_not_called()

    def test_send_failure_reports_error(self, app):
        _configure_smtp(app)
        fake_time = SimpleNamespace(monotonic=Mock(side_effect=[0.0, 0.012, 1.0, 1.034]))

        with (
            patch("app.socket"),
            patch.object(flask_mail, "send", side_effect=Exception("relay odmítl")),
            patch.object(app_module, "_time", fake_time),
        ):
            result = _invoke(app, "send-test-email", [RECIPIENT])

        assert result.exit_code == 1
        assert result.output.splitlines()[-2:] == [
            f"1/2  Checking TCP connectivity to {SMTP_HOST}:{SMTP_PORT} … OK (12 ms)",
            "2/2  Sending test email via SMTP … FAILED (34 ms) — relay odmítl",
        ]


@pytest.mark.usefixtures("preserve_mail_config")
class TestCreateAppFactory:
    @pytest.fixture
    def second_app_factory(self, app):
        created = []

        def build():
            new = create_app(db_url=app.config["SQLALCHEMY_DATABASE_URI"])
            created.append(new)
            return new

        yield build
        for new in created:
            with new.app_context():
                db.engine.dispose()

    @pytest.mark.parametrize(
        ("flask_env", "testing", "debug"),
        [("testing", True, False), ("development", False, True), (None, False, True)],
        ids=["testing", "development", "unset"],
    )
    def test_config_name_comes_from_flask_env(self, monkeypatch, second_app_factory, flask_env, testing, debug):
        if flask_env is None:
            monkeypatch.delenv("FLASK_ENV", raising=False)
        else:
            monkeypatch.setenv("FLASK_ENV", flask_env)
        monkeypatch.setenv("SECRET_KEY", "test-secret-not-for-production")

        new = second_app_factory()

        assert (new.config["TESTING"], new.config["DEBUG"]) == (testing, debug)

    def test_smtp_from_database_applied_at_startup(self, app, monkeypatch, second_app_factory):
        _configure_smtp(app)
        monkeypatch.setenv("FLASK_ENV", "testing")

        new = second_app_factory()

        assert new.config["MAIL_SERVER"] == SMTP_HOST
        assert new.config["MAIL_PORT"] == SMTP_PORT

    def test_unconfigured_smtp_is_not_applied_at_startup(self, app, monkeypatch, second_app_factory):
        with app.app_context():
            settings = get_settings()
            settings.smtp_server = SMTP_HOST
            settings.smtp_username = None
            assert not settings.smtp_configured
            db.session.commit()
        monkeypatch.setenv("FLASK_ENV", "testing")

        new = second_app_factory()

        assert "MAIL_SERVER" not in new.config
