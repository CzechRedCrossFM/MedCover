"""Edge cases of the own-profile page and the user list."""

import pytest

from app.constants import RECORD_MODIFIED_MSG
from app.extensions import db
from app.models.role import Role
from app.models.user import CalendarView, UserAccount
from tests.conftest import _assert_redirect_flash, _make_user, _post_with_stale_commit
from tests.test_profile_photo import _audit_rows, _set_photo, _user


class TestProfileEdgeCases:
    @pytest.mark.parametrize(
        ("posted_view", "posted_days", "view", "days"),
        [
            ("zzz", "abc", CalendarView.LIST, 30),
            ("week", "0", CalendarView.WEEK, 1),
            ("week", "9999", CalendarView.WEEK, 365),
        ],
        ids=["invalid-values", "days-below-min", "days-above-max"],
    )
    def test_preferences_invalid_values(
        self, app, member_client, posted_view: str, posted_days: str, view: CalendarView, days: int
    ) -> None:
        with app.app_context():
            user = db.session.scalar(db.select(UserAccount).where(UserAccount.email == "member@test.com"))
            user.preferred_calendar_view = CalendarView.MONTH
            user.dashboard_horizon_days = 90
            db.session.commit()
            version = user.version

        resp = member_client.post(
            "/users/profile",
            data={
                "action": "preferences",
                "preferred_calendar_view": posted_view,
                "dashboard_horizon_days": posted_days,
                "version": str(version),
            },
        )

        _assert_redirect_flash(member_client, resp, "/users/profile", [("success", "Nastavení bylo uloženo.")])
        saved = _user(app, "member@test.com")
        assert (saved.preferred_calendar_view, saved.dashboard_horizon_days) == (view, days)
        assert saved.version == version + 1
        assert len(_audit_rows(app, saved.id)) == 1

    def test_unknown_action_renders_profile_unchanged(self, app, member_client) -> None:
        before = _user(app, "member@test.com")

        resp = member_client.post("/users/profile", data={"action": "zzz", "version": str(before.version)})

        assert resp.status_code == 200
        assert "Můj profil" in resp.data.decode()
        assert _user(app, "member@test.com").version == before.version

    def test_profile_change_stale_commit(self, app, member_client) -> None:
        before = _user(app, "member@test.com")

        resp = _post_with_stale_commit(
            member_client,
            "/users/profile",
            data={"action": "profile", "phone": "+420 777 111 222", "version": str(before.version)},
        )

        _assert_redirect_flash(member_client, resp, "/users/profile", [("danger", RECORD_MODIFIED_MSG)])
        after = _user(app, "member@test.com")
        assert (after.phone, after.version) == (before.phone, before.version)

    def test_password_change_stale_commit(self, app, member_client) -> None:
        before = _user(app, "member@test.com")

        resp = _post_with_stale_commit(
            member_client,
            "/users/profile",
            data={
                "action": "password",
                "current_password": "testpass123",
                "new_password": "nove-heslo-456",
                "confirm_password": "nove-heslo-456",
                "version": str(before.version),
            },
        )

        _assert_redirect_flash(member_client, resp, "/users/profile", [("danger", RECORD_MODIFIED_MSG)])
        with app.app_context():
            user = db.session.get(UserAccount, before.id)
            assert user.check_password("testpass123")
            assert user.version == before.version

    def test_signature_remove_without_signature(self, app, member_client) -> None:
        before = _user(app, "member@test.com")

        resp = member_client.post("/users/profile", data={"action": "signature_remove", "version": str(before.version)})

        _assert_redirect_flash(member_client, resp, "/users/profile", [])
        assert _user(app, "member@test.com").version == before.version
        assert _audit_rows(app, before.id) == []

    def test_signature_remove_stale_commit(self, app, member_client) -> None:
        with app.app_context():
            user = db.session.scalar(db.select(UserAccount).where(UserAccount.email == "member@test.com"))
            user.signature_image = b"\x89PNG\r\n"
            user.signature_mimetype = "image/png"
            db.session.commit()
            version = user.version

        resp = _post_with_stale_commit(
            member_client, "/users/profile", data={"action": "signature_remove", "version": str(version)}
        )

        _assert_redirect_flash(member_client, resp, "/users/profile", [("danger", RECORD_MODIFIED_MSG)])
        with app.app_context():
            user = db.session.scalar(db.select(UserAccount).where(UserAccount.email == "member@test.com"))
            assert (user.signature_image, user.signature_mimetype, user.version) == (
                b"\x89PNG\r\n",
                "image/png",
                version,
            )

    def test_photo_remove_stale_commit(self, app, member_client) -> None:
        user_id = _user(app, "member@test.com").id
        token = _set_photo(app, user_id)
        version = _user(app, "member@test.com").version

        resp = _post_with_stale_commit(
            member_client, "/users/profile", data={"action": "photo_remove", "version": str(version)}
        )

        _assert_redirect_flash(member_client, resp, "/users/profile", [("danger", RECORD_MODIFIED_MSG)])
        after = _user(app, "member@test.com")
        assert (after.profile_photo_token, after.version) == (token, version)

    def test_ical_guide_google(self, member_client) -> None:
        resp = member_client.get("/users/profile/ical-guide/google")

        assert resp.status_code == 200
        assert "Přidání do Google Kalendáře" in resp.data.decode()


class TestUserListFallbacks:
    def test_invalid_sort_and_direction_fall_back_to_name_ascending(self, app, admin_client) -> None:
        # Created order and e-mail order are both the reverse of name order, so only a name sort puts Adam first.
        with app.app_context():
            _make_user("aaa@medcover.local", "Testovací Zbyněk", Role.MEMBER)
            _make_user("zzz@medcover.local", "Testovací Adam", Role.MEMBER)

        body = admin_client.get("/users/?sort=zzz&dir=zzz").data.decode()

        assert body.index("Testovací Adam") < body.index("Testovací Zbyněk")
