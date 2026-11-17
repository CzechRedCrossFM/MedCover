from unittest.mock import patch

import pytest
import sqlalchemy as sa
from flask import url_for

from app.constants import MIN_PASSWORD_LENGTH
from app.extensions import db
from app.models.master_event import MasterEvent
from app.models.role import ROLE_PERMISSIONS, Role
from app.models.settings import get_settings
from app.models.user import UserAccount
from app.routes.setup import _ensure_general_me, _ensure_roles
from tests.conftest import _make_user

_ADMIN_PASSWORD = "x" * MIN_PASSWORD_LENGTH
_ADMIN_FORM = {
    "full_name": "  Testovací Rudolf  ",
    "email": "  Testovaci.Admin@Example.com  ",
    "password": _ADMIN_PASSWORD,
    "password2": _ADMIN_PASSWORD,
}

_SMTP_FORM = {
    "smtp_server": "smtp.example.com",
    "smtp_port": "2525",
    "smtp_use_tls": "on",
    "smtp_username": "odesilatel@example.com",
    "smtp_password": "tajne-heslo",
    "smtp_default_sender": "MedCover <odesilatel@example.com>",
}


@pytest.fixture
def setup_mode(app):
    with app.app_context():
        get_settings().setup_complete = False
        db.session.commit()


def _user_count(app) -> int:
    with app.app_context():
        return db.session.scalar(db.select(db.func.count()).select_from(UserAccount))


def _setup_complete(app) -> bool:
    with app.app_context():
        return get_settings().setup_complete


@pytest.mark.parametrize("method", ["get", "post"])
@pytest.mark.parametrize("path", ["/setup/", "/setup/step1", "/setup/step2", "/setup/step3"])
def test_completed_setup_redirects_to_dashboard(app, client, path, method):
    with app.test_request_context():
        dashboard = url_for("main.dashboard")
    resp = getattr(client, method)(path)
    assert resp.status_code == 302
    assert resp.headers["Location"] == dashboard


def test_incomplete_setup_redirects_app_pages(client, setup_mode):
    resp = client.get("/events/")
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/setup/step1")


@pytest.mark.parametrize("path", ["/setup/", "/setup/step1"])
def test_step1_get_renders_org_name(client, setup_mode, path):
    resp = client.get(path)
    assert resp.status_code == 200
    assert "Test Org" in resp.get_data(as_text=True)


@pytest.mark.parametrize(
    ("path", "field"),
    [("/setup/step2", 'name="smtp_server"'), ("/setup/step3", 'name="full_name"')],
)
def test_step2_and_step3_get_render_forms(client, setup_mode, path, field):
    resp = client.get(path)
    assert resp.status_code == 200
    assert field in resp.get_data(as_text=True)


@pytest.mark.parametrize(
    ("form", "message"),
    [
        ({"org_name": ""}, "Zadejte název organizace."),
        ({"org_name": "Testovací spolek", "timezone": "Mars/Base"}, "Neplatná časová zóna."),
    ],
)
def test_step1_validation(app, client, setup_mode, form, message):
    resp = client.post("/setup/step1", data=form)
    assert resp.status_code == 200
    assert message in resp.get_data(as_text=True)
    with app.app_context():
        assert get_settings().org_name == "Test Org"


def test_step1_saves_and_advances(app, client, setup_mode):
    resp = client.post("/setup/step1", data={"org_name": "  Testovací spolek ", "timezone": "Europe/Vienna"})
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/setup/step2")
    with app.app_context():
        s = get_settings()
        assert (s.org_name, s.timezone) == ("Testovací spolek", "Europe/Vienna")


@pytest.mark.usefixtures("preserve_mail_config")
def test_step2_saves_all_fields(app, client, setup_mode):
    resp = client.post("/setup/step2", data=_SMTP_FORM)
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/setup/step3")
    with app.app_context():
        s = get_settings()
        assert (
            s.smtp_server,
            s.smtp_port,
            s.smtp_use_tls,
            s.smtp_username,
            s.smtp_default_sender,
            s.get_smtp_password(),
        ) == (
            "smtp.example.com",
            2525,
            True,
            "odesilatel@example.com",
            "MedCover <odesilatel@example.com>",
            "tajne-heslo",
        )
        assert s.smtp_password_enc != "tajne-heslo"


@pytest.mark.usefixtures("preserve_mail_config")
def test_step2_blank_fields_use_defaults_and_keep_password(app, client, setup_mode):
    with app.app_context():
        s = get_settings()
        s.smtp_server = "smtp.example.com"
        s.smtp_port = 2525
        s.smtp_username = "odesilatel@example.com"
        s.set_smtp_password("puvodni-heslo")
        db.session.commit()

    resp = client.post(
        "/setup/step2",
        data={"smtp_server": "", "smtp_port": "", "smtp_username": "", "smtp_password": "", "smtp_default_sender": ""},
    )
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/setup/step3")
    with app.app_context():
        s = get_settings()
        assert (s.smtp_server, s.smtp_port, s.smtp_use_tls, s.smtp_username, s.smtp_default_sender) == (
            None,
            587,
            False,
            None,
            None,
        )
        assert s.get_smtp_password() == "puvodni-heslo"


@pytest.mark.usefixtures("preserve_mail_config")
class TestStep2TestEmail:
    def test_unconfigured_warns_without_sending(self, client, setup_mode):
        with patch("app.routes.setup.mail.send") as send:
            resp = client.post("/setup/step2", data={"action": "test"})
        assert resp.status_code == 200
        assert "Před odesláním testovacího e-mailu vyplňte SMTP nastavení." in resp.get_data(as_text=True)
        send.assert_not_called()

    def test_sends_to_smtp_username(self, client, setup_mode):
        with patch("app.routes.setup.mail.send") as send:
            resp = client.post("/setup/step2", data={**_SMTP_FORM, "action": "test"})
        assert resp.status_code == 200
        assert "Testovací e-mail byl odeslán." in resp.get_data(as_text=True)
        send.assert_called_once()
        msg = send.call_args.args[0]
        assert msg.recipients == ["odesilatel@example.com"]
        assert msg.subject == "MedCover — testovací e-mail"

    def test_send_failure_is_flashed(self, client, setup_mode):
        with patch("app.routes.setup.mail.send", side_effect=Exception("boom")):
            resp = client.post("/setup/step2", data={**_SMTP_FORM, "action": "test"})
        assert resp.status_code == 200
        body = resp.get_data(as_text=True)
        assert "Odeslání se nezdařilo: boom" in body
        assert "Testovací e-mail byl odeslán." not in body


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"full_name": ""}, "Zadejte celé jméno."),
        ({"email": ""}, "Zadejte e-mail."),
        (
            {"password": _ADMIN_PASSWORD[:-1], "password2": _ADMIN_PASSWORD[:-1]},
            f"Heslo musí mít alespoň {MIN_PASSWORD_LENGTH} znaků.",
        ),
        ({"password2": "jine-heslo-456"}, "Hesla se neshodují."),
        ({"email": "existujici@example.com"}, "Účet s tímto e-mailem již existuje."),
    ],
)
def test_step3_validation(app, client, setup_mode, overrides, message):
    with app.app_context():
        _make_user("existujici@example.com", "Existující Karel", Role.MEMBER)

    resp = client.post("/setup/step3", data={**_ADMIN_FORM, **overrides})
    assert resp.status_code == 200
    assert message in resp.get_data(as_text=True)
    assert _user_count(app) == 1
    assert not _setup_complete(app)


def test_step3_creates_admin_and_logs_in(app, client, setup_mode):
    with app.app_context():
        db.session.execute(db.delete(Role))
        db.session.commit()

    resp = client.post("/setup/step3", data=_ADMIN_FORM)
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/setup/done")

    with app.app_context():
        user = db.session.scalar(db.select(UserAccount))
        assert (user.email, user.name, user.is_active) == ("testovaci.admin@example.com", "Testovací Rudolf", True)
        assert user.check_password(_ADMIN_PASSWORD)
        assert [r.name for r in user.roles] == [Role.ADMIN]
        assert {r.name for r in db.session.scalars(db.select(Role))} == set(ROLE_PERMISSIONS)
        general = db.session.scalars(db.select(MasterEvent).where(MasterEvent.is_general == sa.true())).all()
        assert [m.name for m in general] == ["Obecné"]
        assert get_settings().setup_complete

    done = client.get("/setup/done")
    assert done.status_code == 200
    assert "Nastavení dokončeno. Vítejte v MedCover!" in done.get_data(as_text=True)
    assert client.get("/users/profile").status_code == 200


def test_ensure_helpers_idempotent(app):
    with app.app_context():
        db.session.execute(db.delete(Role))
        db.session.commit()
        for _ in range(2):
            _ensure_roles()
            _ensure_general_me()
            db.session.commit()
        assert sorted(r.name for r in db.session.scalars(db.select(Role))) == sorted(ROLE_PERMISSIONS)
        general = db.session.scalars(db.select(MasterEvent).where(MasterEvent.is_general == sa.true())).all()
        assert [m.name for m in general] == ["Obecné"]
