"""Edge cases of batch role actions, account state guards and invites."""

import re
from datetime import datetime, timezone

import pytest
import sqlalchemy as sa

from app.extensions import db
from app.models.audit import AuditLogEntry
from app.models.invite import RegistrationInvite
from app.models.outbox import OutboxEmail
from app.models.role import ROLE_PERMISSIONS, Role
from app.models.user import UserAccount
from tests.conftest import (
    TARGET_EMAIL,
    TARGET_NAME,
    _alerts,
    _assert_redirect_flash,
    _make_target,
    _make_user,
    _snapshot,
)
from tests.test_external_roles import role_ids
from tests.test_profile_photo import _audit_rows, _user

USED_INVITE_MSG = "Tato pozvánka již byla použita."


class TestBatchActionEdgeCases:
    @staticmethod
    def _post(client, user_ids: list[str], action: str, role_id: object):
        return client.post("/users/batch", data={"user_ids": user_ids, "action": action, "role_id": str(role_id)})

    @staticmethod
    def _add_role(app, user_id: str, role_name: str) -> None:
        with app.app_context():
            user = db.session.get(UserAccount, user_id)
            user.roles = [*user.roles, db.session.scalar(db.select(Role).where(Role.name == role_name))]
            db.session.commit()

    @pytest.mark.parametrize("role_id", ["", "abc"], ids=["empty", "non-numeric"])
    def test_missing_role_id(self, app, admin_client, role_id: str) -> None:
        uid = _make_target(app)
        before = _snapshot(app, uid)

        resp = self._post(admin_client, [uid], "add_role", role_id)

        _assert_redirect_flash(admin_client, resp, "/users/", [("warning", "Vyberte platnou roli.")])
        assert _snapshot(app, uid) == before

    def test_unknown_role_id(self, app, admin_client) -> None:
        uid = _make_target(app)
        before = _snapshot(app, uid)
        with app.app_context():
            unknown_id = db.session.scalar(sa.select(sa.func.max(Role.id))) + 1000

        resp = self._post(admin_client, [uid], "add_role", unknown_id)

        _assert_redirect_flash(admin_client, resp, "/users/", [("danger", "Role nebyla nalezena.")])
        assert _snapshot(app, uid) == before

    def test_all_user_ids_malformed(self, app, admin_client) -> None:
        uid = _make_target(app)
        before = _snapshot(app, uid)
        with app.app_context():
            (role_id,) = role_ids(Role.COORDINATOR)

        resp = self._post(admin_client, [uid[:-1], "123"], "add_role", role_id)

        _assert_redirect_flash(admin_client, resp, "/users/", [("warning", "Žádní platní uživatelé nevybrání.")])
        assert _snapshot(app, uid) == before
        assert _audit_rows(app, uid) == []

    @staticmethod
    def _batch_flash(client, resp, verb: str, changed: int, skipped: int, category: str) -> None:
        """Assert the redirect and the count flash, leaving the quoted role name unasserted."""
        assert resp.status_code == 302
        assert resp.headers["Location"] == "/users/"
        ((actual_category, message),) = _alerts(client.get("/users/").data)
        assert actual_category == category
        assert re.fullmatch(
            rf"Role .Coordinator. byla {verb} u {changed} uživatel\(ů\)\. "
            rf"{skipped} přeskočeno \(role již byla / nebyla přiřazena\)\.",
            message,
        )

    def test_remove_role_skips_users_without_it(self, app, admin_client) -> None:
        holder = _make_target(app)
        self._add_role(app, holder, Role.COORDINATOR)
        bystander = _make_target(app, "druhy@medcover.local", "Testovací Druhý")
        holder_before = _snapshot(app, holder)
        bystander_before = _snapshot(app, bystander)
        with app.app_context():
            (role_id,) = role_ids(Role.COORDINATOR)

        resp = self._post(admin_client, [holder, bystander], "remove_role", role_id)

        self._batch_flash(admin_client, resp, "odebrána", 1, 1, "success")
        assert _snapshot(app, holder) == {
            **holder_before,
            "roles": [Role.MEMBER],
            "version": holder_before["version"] + 1,
        }
        assert _snapshot(app, bystander) == bystander_before
        (entry,) = _audit_rows(app, holder)
        assert re.fullmatch(rf"Hromadná akce: odebrána role .Coordinator. uživateli {TARGET_NAME}", entry.summary)
        assert _audit_rows(app, bystander) == []

    def test_add_role_skips_users_already_having_it(self, app, admin_client) -> None:
        holder = _make_target(app)
        self._add_role(app, holder, Role.COORDINATOR)
        other = _make_target(app, "druhy@medcover.local", "Testovací Druhý")
        holder_before = _snapshot(app, holder)
        other_before = _snapshot(app, other)
        with app.app_context():
            (role_id,) = role_ids(Role.COORDINATOR)

        resp = self._post(admin_client, [holder, other], "add_role", role_id)

        self._batch_flash(admin_client, resp, "přidána", 1, 1, "success")
        assert _snapshot(app, holder) == holder_before
        assert _audit_rows(app, holder) == []
        assert _snapshot(app, other) == {
            **other_before,
            "roles": [Role.COORDINATOR, Role.MEMBER],
            "version": other_before["version"] + 1,
        }
        (entry,) = _audit_rows(app, other)
        assert re.fullmatch(r"Hromadná akce: přidána role .Coordinator. uživateli Testovací Druhý", entry.summary)

    def test_nothing_changed_flashes_info(self, app, admin_client) -> None:
        uid = _make_target(app)
        before = _snapshot(app, uid)
        with app.app_context():
            (role_id,) = role_ids(Role.COORDINATOR)

        resp = self._post(admin_client, [uid], "remove_role", role_id)

        self._batch_flash(admin_client, resp, "odebrána", 0, 1, "info")
        assert _snapshot(app, uid) == before
        assert _audit_rows(app, uid) == []


class TestAccountStateGuards:
    @pytest.mark.parametrize(
        ("action", "message"),
        [("deactivate", "Nelze deaktivovat vlastní účet."), ("archive", "Nelze archivovat vlastní účet.")],
        ids=["deactivate", "archive"],
    )
    def test_cannot_change_own_account(self, app, admin_client, action: str, message: str) -> None:
        before = _user(app, "admin@test.com")

        resp = admin_client.post(f"/users/{before.id}/{action}")

        _assert_redirect_flash(admin_client, resp, f"/users/{before.id}", [("danger", message)])
        after = _user(app, "admin@test.com")
        assert (after.is_active, after.is_archived, after.version) == (True, False, before.version)
        assert _audit_rows(app, str(before.id)) == []

    @pytest.mark.parametrize(
        ("action", "archived", "message"),
        [
            ("archive", True, f"Účet {TARGET_NAME} je již archivován."),
            ("unarchive", False, f"Účet {TARGET_NAME} není archivován."),
        ],
        ids=["archive-archived", "unarchive-active"],
    )
    def test_archive_state_mismatch(self, app, admin_client, action: str, archived: bool, message: str) -> None:
        with app.app_context():
            target = _make_user(TARGET_EMAIL, TARGET_NAME, Role.MEMBER)
            target.is_archived = archived
            db.session.commit()
            uid = target.id
        before = _user(app, TARGET_EMAIL)

        resp = admin_client.post(f"/users/{uid}/{action}")

        _assert_redirect_flash(admin_client, resp, f"/users/{uid}", [("warning", message)])
        after = _user(app, TARGET_EMAIL)
        assert (after.is_archived, after.is_active, after.version) == (archived, True, before.version)
        assert _audit_rows(app, str(uid)) == []


class TestInviteEdgeCases:
    @staticmethod
    def _make_invite(app, *, used: bool = False, cancelled: bool = False) -> int:
        now = datetime.now(timezone.utc)
        with app.app_context():
            creator = db.session.scalar(db.select(UserAccount).where(UserAccount.email == "admin@test.com"))
            invite = RegistrationInvite(
                email="pozvany@medcover.local",
                created_by_id=creator.id,
                used_at=now if used else None,
                cancelled_at=now if cancelled else None,
            )
            db.session.add(invite)
            db.session.commit()
            return invite.id

    @staticmethod
    def _invite_count(app) -> int:
        with app.app_context():
            return db.session.scalar(sa.select(sa.func.count()).select_from(RegistrationInvite))

    def test_subject_of_max_length_is_accepted(self, app, admin_client) -> None:
        resp = admin_client.post(
            "/users/invites/create", data={"email": "pozvany@medcover.local", "custom_subject": "x" * 255}
        )

        _assert_redirect_flash(
            admin_client,
            resp,
            "/users/invites",
            [("success", "Pozvánka zařazena do fronty odchozích zpráv pro pozvany@medcover.local.")],
        )
        with app.app_context():
            invite = db.session.scalar(db.select(RegistrationInvite))
            assert invite.custom_subject == "x" * 255

    def test_too_long_subject_is_rejected(self, app, admin_client) -> None:
        resp = admin_client.post(
            "/users/invites/create", data={"email": "pozvany@medcover.local", "custom_subject": "x" * 256}
        )

        assert resp.status_code == 200
        assert _alerts(resp.data) == [("danger", "Předmět e-mailu může mít nejvýše 255 znaků.")]
        assert self._invite_count(app) == 0

    def test_external_invite_requires_assign_role(
        self, app, coordinator_client, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setitem(ROLE_PERMISSIONS, Role.COORDINATOR, [*ROLE_PERMISSIONS[Role.COORDINATOR], "invite.create"])

        resp = coordinator_client.post(
            "/users/invites/create", data={"email": "pozvany@medcover.local", "is_external": "on"}
        )

        assert resp.status_code == 403
        assert self._invite_count(app) == 0

    def test_resend_used_invite(self, app, admin_client) -> None:
        invite_id = self._make_invite(app, used=True)
        with app.app_context():
            outbox_before = db.session.scalar(sa.select(sa.func.count()).select_from(OutboxEmail))

        resp = admin_client.post(f"/users/invites/{invite_id}/resend")

        _assert_redirect_flash(admin_client, resp, "/users/invites", [("warning", USED_INVITE_MSG)])
        with app.app_context():
            assert db.session.scalar(sa.select(sa.func.count()).select_from(OutboxEmail)) == outbox_before
            assert db.session.get(RegistrationInvite, invite_id).outbox_email_id is None

    @pytest.mark.parametrize(
        ("used", "cancelled", "message"),
        [
            (True, False, "Tato pozvánka již byla použita — nelze zrušit."),
            (False, True, "Tato pozvánka již byla zrušena."),
        ],
        ids=["used", "already-cancelled"],
    )
    def test_cancel_unusable_invite(self, app, admin_client, used: bool, cancelled: bool, message: str) -> None:
        invite_id = self._make_invite(app, used=used, cancelled=cancelled)
        with app.app_context():
            cancelled_before = db.session.get(RegistrationInvite, invite_id).cancelled_at

        resp = admin_client.post(f"/users/invites/{invite_id}/cancel")

        _assert_redirect_flash(admin_client, resp, "/users/invites", [("warning", message)])
        with app.app_context():
            assert db.session.get(RegistrationInvite, invite_id).cancelled_at == cancelled_before
            assert (
                db.session.scalar(
                    sa.select(sa.func.count()).select_from(AuditLogEntry).where(AuditLogEntry.action_type == "cancel")
                )
                == 0
            )
