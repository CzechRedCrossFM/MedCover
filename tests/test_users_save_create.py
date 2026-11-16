"""Edge cases of admin user save and manual creation."""

import pytest
import sqlalchemy as sa

from app.constants import MIN_PASSWORD_LENGTH, RECORD_MODIFIED_MSG
from app.extensions import db
from app.models.qualification import Qualification
from app.models.role import ROLE_PERMISSIONS, Role
from app.models.user import UserAccount
from tests.conftest import (
    TARGET_NAME,
    _alerts,
    _assert_redirect_flash,
    _make_target,
    _post_with_stale_commit,
    _snapshot,
)
from tests.test_external_roles import role_ids
from tests.test_profile_photo import _audit_rows

INVALID_PHONE_MSG = "Neplatný formát telefonního čísla."


def _save_form(app, user_id: str, **overrides) -> dict:
    """Form the detail page would submit for the unchanged user, with *overrides* applied."""
    current = _snapshot(app, user_id)
    with app.app_context():
        current_role_ids = [str(role_id) for role_id in role_ids(*current["roles"])]
    form = {
        "name": current["name"],
        "email": current["email"],
        "phone": current["phone"] or "",
        "version": str(current["version"]),
        "role_ids": current_role_ids,
    }
    form.update(overrides)
    return form


class TestSaveUserEdgeCases:
    def test_empty_email(self, app, admin_client) -> None:
        uid = _make_target(app)
        before = _snapshot(app, uid)

        resp = admin_client.post(f"/users/{uid}/save", data=_save_form(app, uid, name="Změněné Jméno", email=" "))

        _assert_redirect_flash(admin_client, resp, f"/users/{uid}", [("danger", "E-mail nesmí být prázdný.")])
        assert _snapshot(app, uid) == before
        assert _audit_rows(app, uid) == []

    def test_short_new_password_discards_all_changes(self, app, admin_client) -> None:
        uid = _make_target(app)
        before = _snapshot(app, uid)

        resp = admin_client.post(
            f"/users/{uid}/save",
            data=_save_form(app, uid, name="Změněné Jméno", new_password="x" * (MIN_PASSWORD_LENGTH - 1)),
        )

        _assert_redirect_flash(
            admin_client,
            resp,
            f"/users/{uid}",
            [("warning", f"Heslo musí mít alespoň {MIN_PASSWORD_LENGTH} znaků.")],
        )
        assert _snapshot(app, uid) == before
        with app.app_context():
            assert db.session.get(UserAccount, uid).check_password("testpass123")

    def test_admin_sets_password(self, app, admin_client) -> None:
        uid = _make_target(app)
        before = _snapshot(app, uid)

        resp = admin_client.post(
            f"/users/{uid}/save", data=_save_form(app, uid, new_password="n" * MIN_PASSWORD_LENGTH)
        )

        _assert_redirect_flash(admin_client, resp, f"/users/{uid}", [("success", "Uživatel byl uložen.")])
        assert _snapshot(app, uid) == {**before, "version": before["version"] + 1}
        with app.app_context():
            user = db.session.get(UserAccount, uid)
            assert user.check_password("n" * MIN_PASSWORD_LENGTH)
            assert not user.check_password("testpass123")
        (entry,) = _audit_rows(app, uid)
        assert entry.summary == f"Admin nastavil heslo pro uživatele {TARGET_NAME}"
        assert entry.changes_json == {}

    def test_stale_commit(self, app, admin_client) -> None:
        uid = _make_target(app)
        before = _snapshot(app, uid)

        resp = _post_with_stale_commit(
            admin_client, f"/users/{uid}/save", data=_save_form(app, uid, name="Změněné Jméno")
        )

        _assert_redirect_flash(admin_client, resp, f"/users/{uid}", [("danger", RECORD_MODIFIED_MSG)])
        assert _snapshot(app, uid) == before

    def test_without_assign_permissions_roles_and_qualifications_are_kept(
        self, app, coordinator_client, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setitem(ROLE_PERMISSIONS, Role.COORDINATOR, [*ROLE_PERMISSIONS[Role.COORDINATOR], "user.edit_any"])
        uid = _make_target(app)
        with app.app_context():
            qualification = Qualification(name="Testovací kvalifikace")
            db.session.add(qualification)
            db.session.commit()
            qualification_id = qualification.id
            (admin_role_id,) = role_ids(Role.ADMIN)
        before = _snapshot(app, uid)

        resp = coordinator_client.post(
            f"/users/{uid}/save",
            data=_save_form(
                app,
                uid,
                name="Změněné Jméno",
                role_ids=[str(admin_role_id)],
                qualification_ids=[str(qualification_id)],
            ),
        )

        _assert_redirect_flash(coordinator_client, resp, f"/users/{uid}", [("success", "Uživatel byl uložen.")])
        assert _snapshot(app, uid) == {**before, "name": "Změněné Jméno", "version": before["version"] + 1}


class TestCreateUserEdgeCases:
    @staticmethod
    def _user_count(app) -> int:
        with app.app_context():
            return db.session.scalar(sa.select(sa.func.count()).select_from(UserAccount))

    @pytest.mark.parametrize("email", ["", "bez-zavinace"], ids=["empty", "no-at-sign"])
    def test_invalid_email(self, app, admin_client, email: str) -> None:
        users_before = self._user_count(app)

        resp = admin_client.post("/users/create", data={"name": "Testovací Nový", "email": email, "phone": ""})

        assert resp.status_code == 200
        assert _alerts(resp.data) == [("danger", "Zadejte platný e-mail.")]
        assert self._user_count(app) == users_before

    def test_invalid_phone(self, app, admin_client) -> None:
        users_before = self._user_count(app)

        resp = admin_client.post(
            "/users/create", data={"name": "Testovací Nový", "email": "novy@medcover.local", "phone": "abc"}
        )

        assert resp.status_code == 200
        assert _alerts(resp.data) == [("danger", INVALID_PHONE_MSG)]
        assert self._user_count(app) == users_before

    def test_without_assign_permissions_forged_roles_and_qualifications_are_ignored(
        self, app, coordinator_client, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setitem(ROLE_PERMISSIONS, Role.COORDINATOR, [*ROLE_PERMISSIONS[Role.COORDINATOR], "user.create"])
        with app.app_context():
            qualification = Qualification(name="Testovací kvalifikace")
            db.session.add(qualification)
            db.session.commit()
            qualification_id = qualification.id
            (admin_role_id,) = role_ids(Role.ADMIN)

        resp = coordinator_client.post(
            "/users/create",
            data={
                "name": "Testovací Nový",
                "email": "novy@medcover.local",
                "phone": "",
                "role_ids": [str(admin_role_id)],
                "qualification_ids": [str(qualification_id)],
            },
        )

        with app.app_context():
            created = db.session.scalar(db.select(UserAccount).where(UserAccount.email == "novy@medcover.local"))
            assert created is not None
            created_id = created.id
            assert [r.name for r in created.roles] == []
            assert [q.name for q in created.qualifications] == []
        assert resp.status_code == 302
        assert resp.headers["Location"] == f"/users/{created_id}"
        ((category, message),) = _alerts(coordinator_client.get(resp.headers["Location"]).data)
        assert category == "success"
        assert message.startswith("Uživatel Testovací Nový byl vytvořen.")
        (entry,) = _audit_rows(app, created_id)
        assert entry.action_type == "create"
        assert entry.summary == "Manuálně vytvořen účet Testovací Nový (novy@medcover.local)"
