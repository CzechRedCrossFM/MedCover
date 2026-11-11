"""Tests for the profile photo upload/remove/serve feature and its display."""

import io
import random
import re
import uuid
from pathlib import Path

import pytest
from PIL import Image
from sqlalchemy import event as sa_event
from sqlalchemy import inspect, text

from app import profile_photo as profile_photo_module
from app.backup import export_to_zip
from app.constants import RECORD_MODIFIED_MSG
from app.extensions import db
from app.models.assignment import Assignment
from app.models.audit import AuditLogEntry
from app.models.event import Event, EventSpot, EventStatus, StaffingMode
from app.models.role import Role
from app.models.user import UserAccount
from app.profile_photo import (
    MAX_STORED_BYTES,
    PHOTO_EDGE_PX,
    ProfilePhotoError,
    photo_token,
    process_profile_photo_upload,
)
from app.utils import name_initials, tel_href
from tests.conftest import _login, _make_event_with_spot, _make_user
from tests.test_backup import _restore
from tests.test_external_schema import load_migration

SIGNATURE_PHOTO_FIXTURE = Path(__file__).parent / "fixtures" / "signature_photo.jpeg"


def _png(w: int = 600, h: int = 400, mode: str = "RGB", color: object = (200, 30, 30)) -> bytes:
    buf = io.BytesIO()
    Image.new(mode, (w, h), color).save(buf, "PNG")
    return buf.getvalue()


def _jpeg(w: int = 600, h: int = 400, orientation: int | None = None, color: object = (30, 120, 200)) -> bytes:
    img = Image.new("RGB", (w, h), color)
    exif = Image.Exif()
    if orientation is not None:
        exif[0x0112] = orientation
    # GPS IFD so the strip check has something to strip.
    exif.get_ifd(0x8825)[1] = "N"
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=90, exif=exif)
    return buf.getvalue()


def _fmt(fmt: str, w: int = 300, h: int = 300) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (10, 200, 10)).save(buf, fmt)
    return buf.getvalue()


def _upload(client, url: str, data: bytes, version: int | str, action: str | None = None, **kwargs):
    form = {"photo": (io.BytesIO(data), kwargs.pop("filename", "me.png")), "version": str(version)}
    if action:
        form["action"] = action
    return client.post(url, data=form, content_type="multipart/form-data", follow_redirects=kwargs.pop("follow", False))


def _self_upload(client, data: bytes, version: int | str, **kwargs):
    return _upload(client, "/users/profile", data, version, action="photo_upload", **kwargs)


def _self_remove(client, version: int | str, follow: bool = False):
    return client.post(
        "/users/profile", data={"action": "photo_remove", "version": str(version)}, follow_redirects=follow
    )


def _user(app, email: str) -> UserAccount:
    with app.app_context():
        user = db.session.scalar(db.select(UserAccount).where(UserAccount.email == email))
        db.session.expunge(user)
        return user


def _set_photo(app, user_id, data: bytes | None = None) -> str:
    data = data or process_profile_photo_upload(_png())
    with app.app_context():
        user = db.session.get(UserAccount, user_id)
        user.profile_photo = data
        user.profile_photo_token = photo_token(data)
        user.version += 1
        db.session.commit()
        return user.profile_photo_token


def _audit_rows(app, user_id):
    with app.app_context():
        return db.session.scalars(
            db.select(AuditLogEntry).where(AuditLogEntry.entity_id == str(user_id)).order_by(AuditLogEntry.id)
        ).all()


# ── Pipeline ─────────────────────────────────────────────────────────────────


class TestProfilePhotoPipeline:
    def test_output_is_square_jpeg_without_exif(self) -> None:
        out = process_profile_photo_upload(_jpeg(orientation=6))
        img = Image.open(io.BytesIO(out))
        assert img.size == (PHOTO_EDGE_PX, PHOTO_EDGE_PX)
        assert img.format == "JPEG"
        assert not img.getexif()
        assert "exif" not in img.info
        assert len(out) <= MAX_STORED_BYTES

    @pytest.mark.parametrize("raw", [_png(), _jpeg()], ids=["png", "jpeg"])
    def test_png_and_jpeg_accepted(self, raw: bytes) -> None:
        assert Image.open(io.BytesIO(process_profile_photo_upload(raw))).format == "JPEG"

    def test_heic_input_accepted(self) -> None:
        buf = io.BytesIO()
        Image.new("RGB", (300, 300), (10, 20, 200)).save(buf, "HEIF")
        out = process_profile_photo_upload(buf.getvalue())
        assert Image.open(io.BytesIO(out)).size == (PHOTO_EDGE_PX, PHOTO_EDGE_PX)

    def test_mpo_container_is_accepted(self) -> None:
        out = process_profile_photo_upload(SIGNATURE_PHOTO_FIXTURE.read_bytes())
        assert Image.open(io.BytesIO(out)).format == "JPEG"

    @pytest.mark.parametrize(
        "raw",
        [
            _fmt("GIF"),
            _fmt("WEBP"),
            _fmt("BMP"),
            b'<svg xmlns="http://www.w3.org/2000/svg" width="300" height="300"></svg>',
            b"MZ\x90\x00\x03\x00\x00\x00 not an image",
            b"just some text pretending to be a jpg",
            b"",
            _jpeg(1000, 1000)[:600],
        ],
        ids=["gif", "webp", "bmp", "svg", "exe", "text", "empty", "truncated"],
    )
    def test_rejects_disallowed_input(self, raw: bytes) -> None:
        with pytest.raises(ProfilePhotoError):
            process_profile_photo_upload(raw)

    def test_rejects_oversized_by_header(self, monkeypatch: pytest.MonkeyPatch) -> None:
        raw = _png(300, 300)
        monkeypatch.setattr(profile_photo_module, "MAX_IMAGE_PIXELS", 10_000)

        def fail_load(self, *args, **kwargs):
            raise AssertionError("pixels must not be decoded")

        monkeypatch.setattr(Image.Image, "load", fail_load)
        with pytest.raises(ProfilePhotoError, match="příliš velký"):
            process_profile_photo_upload(raw)

    def test_rejects_below_min_edge(self) -> None:
        with pytest.raises(ProfilePhotoError, match="příliš malý"):
            process_profile_photo_upload(_png(100, 600))

    def test_extreme_aspect_is_center_cropped(self) -> None:
        out = process_profile_photo_upload(_png(3000, 200))
        assert Image.open(io.BytesIO(out)).size == (PHOTO_EDGE_PX, PHOTO_EDGE_PX)

    def test_stored_cap_exceeded_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(profile_photo_module, "MAX_STORED_BYTES", 100)
        with pytest.raises(ProfilePhotoError, match="přesahuje limit"):
            process_profile_photo_upload(_png())

    def test_quality_ladder_fits_noise_under_cap(self) -> None:
        rng = random.Random(1)
        noise = Image.frombytes("RGB", (1000, 1000), rng.randbytes(3_000_000))
        buf = io.BytesIO()
        noise.save(buf, "PNG")
        assert len(process_profile_photo_upload(buf.getvalue())) <= MAX_STORED_BYTES

    def test_exif_orientation_is_applied(self) -> None:
        img = Image.new("RGB", (400, 200), (0, 0, 255))
        img.paste((255, 0, 0), (0, 0, 200, 200))
        exif = Image.Exif()
        exif[0x0112] = 6  # rotate 90 degrees clockwise on display: left half ends up on top
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=95, exif=exif)
        out = Image.open(io.BytesIO(process_profile_photo_upload(buf.getvalue())))
        top = out.getpixel((PHOTO_EDGE_PX // 2, 20))
        bottom = out.getpixel((PHOTO_EDGE_PX // 2, PHOTO_EDGE_PX - 20))
        assert top[0] > 200 and top[2] < 80
        assert bottom[2] > 200 and bottom[0] < 80

    def test_transparent_png_composited_on_white(self) -> None:
        out = Image.open(io.BytesIO(process_profile_photo_upload(_png(mode="RGBA", color=(0, 0, 0, 0)))))
        assert all(channel > 200 for channel in out.getpixel((5, 5)))

    def test_transparent_palette_png_composited_on_white(self) -> None:
        img = Image.new("P", (300, 300), 0)
        img.putpalette([0, 0, 0] * 256)
        img.info["transparency"] = 0
        buf = io.BytesIO()
        img.save(buf, "PNG", transparency=0)
        out = Image.open(io.BytesIO(process_profile_photo_upload(buf.getvalue())))
        assert all(channel > 200 for channel in out.getpixel((5, 5)))

    def test_token_is_stable_short_hash(self) -> None:
        assert photo_token(b"abc") == photo_token(b"abc") != photo_token(b"abd")
        assert len(photo_token(b"abc")) == 16


class TestFilters:
    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            ("Testovací Rudolf", "TR"),
            ("Vymyšlený", "V"),
            ("testovací  rudolf", "TR"),
            ("„Pepa“ Vymyšlený", "PV"),
            ("", "?"),
            (None, "?"),
            ("  ", "?"),
            ("Testovací Rudolf Druhý", "TD"),
        ],
    )
    def test_name_initials(self, name: str | None, expected: str) -> None:
        assert name_initials(name) == expected

    @pytest.mark.parametrize(
        ("phone", "expected"),
        [
            ("+420 123 456 789", "+420123456789"),
            ("123 456\t789", "123456789"),  # tab
            ("123\u00a0456\u00a0789", "123456789"),  # non-breaking spaces; escaped because the literal is invisible
            ("123-456-789", "123456789"),
            ("abc", ""),
            ("  ", ""),
            (None, ""),
            ("javascript:alert(1)", "1"),
            ("+420+123", "+420123"),
            ("420+123", "420123"),
            ("++420", "+420"),
            ("12345678\uff19", "123456789"),  # full-width 9; escaped because it looks like an ASCII 9
            ("\uff11\uff12\uff13\uff14\uff15\uff16\uff17\uff18\uff19", "123456789"),
            ("+420 \u0661\u0662\u0663 456 789", "+420123456789"),  # Arabic-Indic digits
        ],
    )
    def test_tel_href(self, phone: str | None, expected: str) -> None:
        assert tel_href(phone) == expected


# ── Self-service routes ──────────────────────────────────────────────────────


class TestProfilePhotoSelfService:
    def test_profile_shows_initials_and_upload_form(self, member_client) -> None:
        html = member_client.get("/users/profile").data.decode()
        assert 'name="photo"' in html
        assert "TM" in html
        assert "/photo?v=" not in html

    @pytest.mark.parametrize("raw", [_png(), _jpeg()], ids=["png", "jpeg"])
    def test_upload_stores_square_jpeg(self, app, member_client, raw: bytes) -> None:
        user = _user(app, "member@test.com")
        resp = _self_upload(member_client, raw, user.version, follow=True)
        assert "Profilová fotka byla uložena".encode() in resp.data
        with app.app_context():
            stored = db.session.get(UserAccount, user.id)
            assert Image.open(io.BytesIO(stored.profile_photo)).size == (PHOTO_EDGE_PX, PHOTO_EDGE_PX)
            assert stored.version == user.version + 1

    def test_upload_sets_server_mimetype_and_token(self, app, member_client) -> None:
        user = _user(app, "member@test.com")
        form = {
            "action": "photo_upload",
            "version": str(user.version),
            "photo": (io.BytesIO(_png()), "me.png", "image/gif"),
        }
        member_client.post("/users/profile", data=form, content_type="multipart/form-data")
        with app.app_context():
            stored = db.session.get(UserAccount, user.id)
            assert stored.profile_photo_token == photo_token(stored.profile_photo)

    def test_upload_rejected_format_flashes_and_keeps_existing_photo(self, app, member_client) -> None:
        user = _user(app, "member@test.com")
        token = _set_photo(app, user.id)
        version = _user(app, "member@test.com").version
        resp = _self_upload(member_client, _fmt("GIF"), version, follow=True)
        assert "Nepodporovaný formát".encode() in resp.data
        after = _user(app, "member@test.com")
        assert after.profile_photo_token == token
        assert after.version == version

    def test_upload_without_file_flashes(self, app, member_client) -> None:
        user = _user(app, "member@test.com")
        resp = member_client.post(
            "/users/profile",
            data={"action": "photo_upload", "version": str(user.version)},
            content_type="multipart/form-data",
            follow_redirects=True,
        )
        assert "Nebyl vybrán žádný soubor".encode() in resp.data

    def test_rejects_oversized_content_length(self, app, member_client, monkeypatch: pytest.MonkeyPatch) -> None:
        user = _user(app, "member@test.com")
        monkeypatch.setattr("app.routes.users.PHOTO_MAX_UPLOAD_BYTES", 100)
        resp = _self_upload(member_client, b"x" * 2048, user.version, follow=True)
        assert "příliš velký".encode() in resp.data
        assert _user(app, "member@test.com").profile_photo_token is None

    def test_rejects_oversized_body_bytes(self, app, member_client, monkeypatch: pytest.MonkeyPatch) -> None:
        user = _user(app, "member@test.com")
        monkeypatch.setattr("app.routes.users.PHOTO_MAX_UPLOAD_BYTES", 1024)
        monkeypatch.setattr(
            type(member_client.application).request_class, "content_length", property(lambda self: None), raising=False
        )
        resp = _self_upload(member_client, b"x" * 2048, user.version, follow=True)
        assert "příliš velký".encode() in resp.data
        assert _user(app, "member@test.com").profile_photo_token is None

    def test_upload_and_remove_write_one_audit_row_each(self, app, member_client) -> None:
        user = _user(app, "member@test.com")
        _self_upload(member_client, _png(), user.version)
        rows = _audit_rows(app, user.id)
        assert len(rows) == 1
        assert rows[0].changes_json == {"profile_photo": [None, "set"]}
        assert "nahrál profilovou fotku" in rows[0].summary
        assert "\\xff\\xd8" not in repr(rows[0].changes_json) and "\xff\xd8" not in repr(rows[0].changes_json)
        _self_remove(member_client, _user(app, "member@test.com").version)
        rows = _audit_rows(app, user.id)
        assert len(rows) == 2
        assert rows[1].changes_json == {"profile_photo": ["set", None]}
        assert "smazal profilovou fotku" in rows[1].summary
        cleared = _user(app, "member@test.com")
        assert cleared.profile_photo_token is None

    def test_replace_audit_records_previous_photo(self, app, member_client) -> None:
        user = _user(app, "member@test.com")
        _self_upload(member_client, _png(), user.version)
        _self_upload(member_client, _jpeg(), _user(app, "member@test.com").version)
        assert _audit_rows(app, user.id)[1].changes_json == {"profile_photo": ["set", "set"]}

    def test_remove_without_photo_is_silent_noop(self, app, member_client) -> None:
        user = _user(app, "member@test.com")
        resp = _self_remove(member_client, user.version)
        assert resp.status_code == 302
        assert resp.headers["Location"].endswith("/users/profile")
        assert _audit_rows(app, user.id) == []
        assert _user(app, "member@test.com").version == user.version

    @pytest.mark.parametrize("action", ["photo_upload", "photo_remove"])
    def test_stale_version_changes_nothing(self, app, member_client, action: str) -> None:
        user = _user(app, "member@test.com")
        token = _set_photo(app, user.id)
        current = _user(app, "member@test.com").version
        if action == "photo_upload":
            resp = _self_upload(member_client, _jpeg(), current - 1, follow=True)
        else:
            resp = _self_remove(member_client, current - 1, follow=True)
        assert RECORD_MODIFIED_MSG in resp.data.decode()
        after = _user(app, "member@test.com")
        assert after.profile_photo_token == token and after.version == current

    def test_concurrent_change_between_check_and_commit_is_caught(
        self, app, member_client, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        user = _user(app, "member@test.com")
        real = process_profile_photo_upload

        def process_and_race(raw: bytes) -> bytes:
            with db.engine.begin() as conn:
                conn.execute(
                    text("UPDATE user_account SET version = version + 1 WHERE id = :id"),
                    {"id": user.id},
                )
            return real(raw)

        monkeypatch.setattr("app.routes.users.process_profile_photo_upload", process_and_race)
        resp = _self_upload(member_client, _png(), user.version, follow=True)
        assert RECORD_MODIFIED_MSG in resp.data.decode()
        assert _user(app, "member@test.com").profile_photo_token is None

    def test_member_can_upload_own_photo_via_profile(self, app, member_client) -> None:
        user = _user(app, "member@test.com")
        _self_upload(member_client, _png(), user.version)
        assert _user(app, "member@test.com").profile_photo_token is not None


# ── Admin routes ─────────────────────────────────────────────────────────────


class TestProfilePhotoAdmin:
    def test_admin_uploads_and_removes_other_users_photo(self, app, admin_client) -> None:
        with app.app_context():
            target = _make_user("target@test.com", "Vymyšlený Jiří", Role.MEMBER)
            target_id, version = target.id, target.version
        resp = _upload(admin_client, f"/users/{target_id}/photo/upload", _png(), version)
        assert resp.status_code == 302
        assert resp.headers["Location"].endswith(f"/users/{target_id}")
        stored = _user(app, "target@test.com")
        assert stored.profile_photo_token is not None
        summary = _audit_rows(app, target_id)[0].summary
        assert "Test Admin" in summary and "Vymyšlený Jiří" in summary
        resp = admin_client.post(f"/users/{target_id}/photo/remove", data={"version": str(stored.version)})
        assert resp.status_code == 302
        assert _user(app, "target@test.com").profile_photo_token is None
        removal = _audit_rows(app, target_id)[1].summary
        assert "Test Admin" in removal and "Vymyšlený Jiří" in removal

    @pytest.mark.parametrize("client_name", ["member_client", "coordinator_client", "viewer_client", "external_client"])
    def test_non_admin_gets_403(self, app, request: pytest.FixtureRequest, client_name: str) -> None:
        client = request.getfixturevalue(client_name)
        with app.app_context():
            target = _make_user("target@test.com", "Vymyšlený Jiří", Role.MEMBER)
            target_id, version = target.id, target.version
        token = _set_photo(app, target_id)
        version = _user(app, "target@test.com").version
        assert _upload(client, f"/users/{target_id}/photo/upload", _png(), version).status_code == 403
        assert client.post(f"/users/{target_id}/photo/remove", data={"version": str(version)}).status_code == 403
        assert _user(app, "target@test.com").profile_photo_token == token

    def test_admin_unknown_user_is_404(self, admin_client) -> None:
        assert _upload(admin_client, f"/users/{uuid.uuid4()}/photo/upload", _png(), 1).status_code == 404


# ── Serving route ────────────────────────────────────────────────────────────


class TestProfilePhotoServe:
    def test_anonymous_redirected_to_login(self, app, client) -> None:
        with app.app_context():
            user = _make_user("target@test.com", "Vymyšlený Jiří", Role.MEMBER)
            user_id = user.id
        _set_photo(app, user_id)
        resp = client.get(f"/users/{user_id}/photo")
        assert resp.status_code == 302
        assert not resp.data.startswith(b"\xff\xd8")

    def test_serves_photo_with_headers(self, app, member_client) -> None:
        with app.app_context():
            user_id = _make_user("target@test.com", "Vymyšlený Jiří", Role.MEMBER).id
        token = _set_photo(app, user_id)
        resp = member_client.get(f"/users/{user_id}/photo?v={token}")
        assert resp.status_code == 200
        assert resp.mimetype == "image/jpeg"
        assert resp.data.startswith(b"\xff\xd8")
        assert "private" in resp.headers["Cache-Control"] and "max-age=86400" in resp.headers["Cache-Control"]
        assert resp.headers["ETag"] == f'"{token}"'
        assert resp.headers["X-Content-Type-Options"] == "nosniff"

    def test_weak_etag_from_proxy_returns_304(self, app, member_client) -> None:
        with app.app_context():
            user_id = _make_user("target@test.com", "Vymyšlený Jiří", Role.MEMBER).id
        token = _set_photo(app, user_id)
        resp = member_client.get(f"/users/{user_id}/photo", headers={"If-None-Match": f'W/"{token}"'})
        assert resp.status_code == 304

    def test_conditional_get_returns_304(self, app, member_client) -> None:
        with app.app_context():
            user_id = _make_user("target@test.com", "Vymyšlený Jiří", Role.MEMBER).id
        token = _set_photo(app, user_id)
        resp = member_client.get(f"/users/{user_id}/photo", headers={"If-None-Match": f'"{token}"'})
        assert resp.status_code == 304

    def test_not_modified_does_not_select_blob(self, app, member_client) -> None:
        with app.app_context():
            user_id = _make_user("target@test.com", "Vymyšlený Jiří", Role.MEMBER).id
        token = _set_photo(app, user_id)
        statements: list[str] = []

        def _before(_conn, _cursor, statement, _parameters, _context, _executemany):
            statements.append(statement)

        with app.app_context():
            engine = db.engine
        sa_event.listen(engine, "before_cursor_execute", _before)
        try:
            resp = member_client.get(f"/users/{user_id}/photo", headers={"If-None-Match": f'"{token}"'})
        finally:
            sa_event.remove(engine, "before_cursor_execute", _before)
        assert resp.status_code == 304
        assert not [s for s in statements if re.search(r"profile_photo(?!_)", s, re.I)]

    def test_404_unknown_user(self, member_client) -> None:
        assert member_client.get(f"/users/{uuid.uuid4()}/photo").status_code == 404

    def test_404_user_without_photo(self, app, member_client) -> None:
        with app.app_context():
            user_id = _make_user("target@test.com", "Vymyšlený Jiří", Role.MEMBER).id
        assert member_client.get(f"/users/{user_id}/photo").status_code == 404

    def test_debriefing_manager_403_for_others_but_200_for_own(self, app, client) -> None:
        with app.app_context():
            manager_id = _make_user("manager@test.com", "Testovací Správce", Role.DEBRIEFING_MANAGER).id
            other_id = _make_user("target@test.com", "Vymyšlený Jiří", Role.MEMBER).id
        _login(client, "manager@test.com")
        _set_photo(app, manager_id)
        _set_photo(app, other_id)
        assert client.get(f"/users/{other_id}/photo").status_code == 403
        assert client.get(f"/users/{manager_id}/photo").status_code == 200

    def test_archived_user_photo_still_served(self, app, member_client) -> None:
        with app.app_context():
            user = _make_user("target@test.com", "Vymyšlený Jiří", Role.MEMBER)
            user.is_active = False
            user.is_archived = True
            db.session.commit()
            user_id = user.id
        _set_photo(app, user_id)
        assert member_client.get(f"/users/{user_id}/photo").status_code == 200


# ── User detail ──────────────────────────────────────────────────────────────


class TestUserDetailPhoto:
    def _target(self, app) -> uuid.UUID:
        with app.app_context():
            return _make_user("target@test.com", "Vymyšlený Jiří", Role.MEMBER).id

    def test_detail_shows_photo_img(self, app, admin_client) -> None:
        user_id = self._target(app)
        token = _set_photo(app, user_id)
        html = admin_client.get(f"/users/{user_id}").data.decode()
        assert f"/users/{user_id}/photo?v={token}" in html

    def test_detail_shows_initials_without_photo(self, app, admin_client) -> None:
        user_id = self._target(app)
        html = admin_client.get(f"/users/{user_id}").data.decode()
        assert "/photo?v=" not in html
        assert "avatar-initials" in html

    def test_detail_photo_forms_for_edit_any(self, app, admin_client) -> None:
        user_id = self._target(app)
        assert "photo/upload" in admin_client.get(f"/users/{user_id}").data.decode()

    def test_detail_photo_forms_hidden_without_edit_any(self, app, viewer_client) -> None:
        user_id = self._target(app)
        _set_photo(app, user_id)
        html = viewer_client.get(f"/users/{user_id}").data.decode()
        assert "avatar" in html and "photo/upload" not in html and "photo/remove" not in html

    def test_photo_forms_are_outside_save_form(self, app, admin_client) -> None:
        user_id = self._target(app)
        _set_photo(app, user_id)
        html = admin_client.get(f"/users/{user_id}").data.decode()
        upload = html.index("photo/upload")
        remove = html.index("photo/remove")
        save_form = html.index("/save")
        form_close_after_upload = html.index("</form>", upload)
        form_close_after_remove = html.index("</form>", remove)
        assert form_close_after_upload < save_form
        assert form_close_after_remove < save_form


# ── Event detail ─────────────────────────────────────────────────────────────


def _assign(app, event_id: int, spot_id: int | None, email: str, name: str, phone: str | None, photo: bool = False):
    with app.app_context():
        role = Role.EXTERNAL if email.startswith("ext") else Role.MEMBER
        user = _make_user(email, name, role)
        user.phone = phone
        db.session.add(Assignment(event_id=event_id, spot_id=spot_id, user_id=user.id))
        db.session.commit()
        user_id = user.id
    token = _set_photo(app, user_id) if photo else None
    return user_id, token


def _event_html(client, event_id: int) -> str:
    resp = client.get(f"/events/{event_id}")
    assert resp.status_code == 200
    return resp.data.decode()


def _row_for(html: str, name: str) -> str:
    start = html.rindex("<tr", 0, html.index(name))
    return html[start : html.index("</tr>", start)]


class TestEventDetailAssignees:
    @pytest.mark.parametrize(
        ("phone", "href"),
        [
            ("+420 123 456 789", "tel:+420123456789"),
            ("123 456 789", "tel:123456789"),
            ("00420123456789", "tel:00420123456789"),
        ],
    )
    def test_tel_link_strips_whitespace(self, app, admin_client, phone: str, href: str) -> None:
        event_id, spot_id = _make_event_with_spot(app)
        _assign(app, event_id, spot_id, "a@test.com", "Vymyšlený Jiří", phone)
        row = _row_for(_event_html(admin_client, event_id), "Vymyšlený Jiří")
        assert f'href="{href}"' in row
        assert f"📞</span> {phone}</a>)</span>" in row
        assert f'(<a href="{href}"' in row
        assert row.index("Vymyšlený Jiří") < row.index("tel:")

    def test_no_phone_no_tel_anchor(self, app, admin_client) -> None:
        event_id, spot_id = _make_event_with_spot(app)
        _assign(app, event_id, spot_id, "a@test.com", "Vymyšlený Jiří", None)
        assert "tel:" not in _row_for(_event_html(admin_client, event_id), "Vymyšlený Jiří")

    def test_thumbnail_img_with_lazy_and_alt(self, app, admin_client) -> None:
        event_id, spot_id = _make_event_with_spot(app)
        user_id, token = _assign(app, event_id, spot_id, "a@test.com", "Vymyšlený Jiří", None, photo=True)
        row = _row_for(_event_html(admin_client, event_id), "Vymyšlený Jiří")
        assert f"/users/{user_id}/photo?v={token}" in row
        assert 'loading="lazy"' in row and 'alt=""' in row and 'width="32"' in row

    def test_initials_without_photo(self, app, admin_client) -> None:
        event_id, spot_id = _make_event_with_spot(app)
        _assign(app, event_id, spot_id, "a@test.com", "Vymyšlený Jiří", None)
        row = _row_for(_event_html(admin_client, event_id), "Vymyšlený Jiří")
        assert "<img" not in row and "VJ" in row

    def test_external_viewer_sees_photo_and_phone_but_no_user_link(self, app, external_client) -> None:
        event_id, spot_id = _make_event_with_spot(app)
        with app.app_context():
            spot2 = EventSpot(event_id=event_id)
            db.session.add(spot2)
            db.session.commit()
            spot2_id = spot2.id
        external = _user(app, "external@test.com")
        with app.app_context():
            db.session.add(Assignment(event_id=event_id, spot_id=spot_id, user_id=external.id))
            db.session.commit()
        other_id, token = _assign(
            app, event_id, spot2_id, "a@test.com", "Vymyšlený Jiří", "+420 123 456 789", photo=True
        )
        html = _event_html(external_client, event_id)
        assert f'href="/users/{other_id}"' not in html
        assert f"/users/{other_id}/photo?v={token}" in html
        assert 'href="tel:+420123456789"' in html

    def test_external_cannot_view_hidden_event_403(self, app, external_client) -> None:
        event_id, spot_id = _make_event_with_spot(app, status=EventStatus.DRAFT)
        _assign(app, event_id, spot_id, "a@test.com", "Vymyšlený Jiří", "+420 123 456 789")
        resp = external_client.get(f"/events/{event_id}")
        assert resp.status_code == 403
        assert b"tel:" not in resp.data

    def test_free_spot_shows_volne_without_avatar_or_phone(self, app, admin_client) -> None:
        event_id, _ = _make_event_with_spot(app)
        html = _event_html(admin_client, event_id)
        assert "Volné" in html
        assert "avatar" not in html and "tel:" not in html

    def test_inactive_and_archived_assignees_still_render(self, app, admin_client) -> None:
        event_id, spot_id = _make_event_with_spot(app)
        with app.app_context():
            spot2 = EventSpot(event_id=event_id)
            db.session.add(spot2)
            db.session.commit()
            spot2_id = spot2.id
        first_id, _ = _assign(app, event_id, spot_id, "a@test.com", "Vymyšlený Jiří", "123 456 789", photo=True)
        second_id, _ = _assign(app, event_id, spot2_id, "b@test.com", "Testovací Rudolf", "987 654 321", photo=True)
        with app.app_context():
            db.session.get(UserAccount, first_id).is_active = False
            archived = db.session.get(UserAccount, second_id)
            archived.is_active = False
            archived.is_archived = True
            db.session.commit()
        html = _event_html(admin_client, event_id)
        for user_id, phone in ((first_id, "tel:123456789"), (second_id, "tel:987654321")):
            assert f"/users/{user_id}/photo?v=" in html and phone in html

    def test_replacing_photo_changes_token_and_url(self, app, admin_client) -> None:
        event_id, spot_id = _make_event_with_spot(app)
        user_id, first = _assign(app, event_id, spot_id, "a@test.com", "Vymyšlený Jiří", None, photo=True)
        before = _event_html(admin_client, event_id)
        second = _set_photo(app, user_id, process_profile_photo_upload(_jpeg(color=(1, 2, 3))))
        assert first != second
        after = _event_html(admin_client, event_id)
        assert f"photo?v={first}" in before and f"photo?v={second}" in after and f"v={first}" not in after

    def test_conditions_participant_list_shows_avatar_and_phone(self, app, admin_client) -> None:
        event_id, _ = _make_event_with_spot(app)
        with app.app_context():
            event = db.session.get(Event, event_id)
            event.spots.clear()
            event.staffing_mode = StaffingMode.CONDITIONS
            event.minimum_participants, event.maximum_participants = 1, 2
            db.session.commit()
        user_id, token = _assign(app, event_id, None, "a@test.com", "Vymyšlený Jiří", "+420 123 456 789", photo=True)
        html = _event_html(admin_client, event_id)
        assert f"/users/{user_id}/photo?v={token}" in html
        assert 'href="tel:+420123456789"' in html
        assert f'href="/users/{user_id}"' in html


class TestEventDetailQueries:
    def _render_statements(self, app, client, assignees: int) -> list[str]:
        event_id, spot_id = _make_event_with_spot(app, name=f"Query Event {assignees}")
        with app.app_context():
            spot_ids = [spot_id]
            for _ in range(assignees - 1):
                spot = EventSpot(event_id=event_id)
                db.session.add(spot)
                db.session.flush()
                spot_ids.append(spot.id)
            db.session.commit()
        for index, current_spot in enumerate(spot_ids):
            _assign(
                app, event_id, current_spot, f"q{assignees}_{index}@test.com", f"Vymyšlený {index}", "123 456 789", True
            )
        captured: list[str] = []

        def _before(_conn, _cursor, statement, _parameters, _context, _executemany):
            captured.append(statement)

        with app.app_context():
            engine = db.engine
        sa_event.listen(engine, "before_cursor_execute", _before)
        try:
            assert client.get(f"/events/{event_id}").status_code == 200
        finally:
            sa_event.remove(engine, "before_cursor_execute", _before)
        return captured

    def test_query_count_constant_and_blob_never_selected(self, app, admin_client) -> None:
        self._render_statements(app, admin_client, 2)  # warm-up: first render runs one-off queries
        one = self._render_statements(app, admin_client, 1)
        five = self._render_statements(app, admin_client, 5)
        assert len(one) == len(five)
        blob_column = re.compile(r"profile_photo(?!_)", re.I)
        assert not [s for s in one + five if blob_column.search(s)]


# ── Schema and backup ────────────────────────────────────────────────────────


def test_profile_photo_migration_round_trip(app) -> None:
    with app.app_context():
        user = _make_user("roundtrip@test.com", "Testovací Rudolf", Role.MEMBER)
        user_id = user.id
        migration = load_migration("e0cc72dfd28f_add_profile_photo_to_user_account.py")
        migration.downgrade()
        try:
            columns = {c["name"] for c in inspect(db.session.connection()).get_columns("user_account")}
            assert not columns & {"profile_photo", "profile_photo_token"}
        finally:
            migration.upgrade()
            db.session.commit()
        columns = {c["name"] for c in inspect(db.session.connection()).get_columns("user_account")}
        assert {"profile_photo", "profile_photo_token"} <= columns
        assert db.session.scalar(text("SELECT COUNT(*) FROM user_account WHERE id = :id"), {"id": user_id}) == 1


def test_profile_photo_survives_backup_restore(app) -> None:
    with app.app_context():
        user = _make_user("backup@test.com", "Testovací Rudolf", Role.MEMBER)
        user_id = user.id
        data = process_profile_photo_upload(_png())
        user.profile_photo = data
        user.profile_photo_token = photo_token(data)
        db.session.commit()
        name = export_to_zip()
        user.profile_photo = None
        user.profile_photo_token = None
        db.session.commit()
        _restore(name)
        db.session.expire_all()
        restored = db.session.get(UserAccount, user_id)
        assert restored.profile_photo == data
        assert restored.profile_photo_token == photo_token(data)


# ── Boundary and exploratory cases ───────────────────────────────────────────


class TestProfilePhotoBoundaries:
    def test_real_oversized_pixel_count_rejected_before_decode(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # 5001 x 5001 solid PNG: tiny on disk, 25.01 MP in the header.
        raw = _png(5001, 5001)

        def fail_load(self, *args, **kwargs):
            raise AssertionError("pixels must not be decoded")

        monkeypatch.setattr(Image.Image, "load", fail_load)
        with pytest.warns(Image.DecompressionBombWarning), pytest.raises(ProfilePhotoError, match="příliš velký"):
            process_profile_photo_upload(raw)

    def test_minimum_edge_boundary(self) -> None:
        out = process_profile_photo_upload(_png(profile_photo_module.MIN_SOURCE_EDGE_PX, 500))
        assert Image.open(io.BytesIO(out)).size == (PHOTO_EDGE_PX, PHOTO_EDGE_PX)
        with pytest.raises(ProfilePhotoError, match="příliš malý"):
            process_profile_photo_upload(_png(profile_photo_module.MIN_SOURCE_EDGE_PX - 1, 500))

    def test_transparent_heic_composited_on_white(self) -> None:
        buf = io.BytesIO()
        Image.new("RGBA", (300, 300), (0, 0, 0, 0)).save(buf, "HEIF")
        out = Image.open(io.BytesIO(process_profile_photo_upload(buf.getvalue())))
        assert min(out.getpixel((160, 160))) > 240

    def test_animated_png_is_flattened_to_single_frame(self) -> None:
        frames = [Image.new("RGB", (300, 300), (i * 60, 0, 0)) for i in range(3)]
        buf = io.BytesIO()
        frames[0].save(buf, "PNG", save_all=True, append_images=frames[1:])
        out = Image.open(io.BytesIO(process_profile_photo_upload(buf.getvalue())))
        assert out.format == "JPEG" and getattr(out, "n_frames", 1) == 1

    def test_route_upload_of_heic_stores_jpeg(self, app, member_client) -> None:
        buf = io.BytesIO()
        Image.new("RGB", (300, 300), (10, 20, 200)).save(buf, "HEIF")
        user = _user(app, "member@test.com")
        resp = _self_upload(member_client, buf.getvalue(), user.version, filename="IMG_0001.HEIC", follow=True)
        assert "Profilová fotka byla uložena".encode() in resp.data
        with app.app_context():
            stored = db.session.get(UserAccount, user.id)
            assert stored.profile_photo.startswith(b"\xff\xd8")

    @pytest.mark.parametrize(
        "raw",
        [_fmt("GIF"), b"<svg xmlns='http://www.w3.org/2000/svg'/>", b"not an image", _jpeg(1000, 1000)[:600]],
        ids=["gif", "svg", "text", "truncated"],
    )
    def test_route_rejection_keeps_existing_photo(self, app, member_client, raw: bytes) -> None:
        user = _user(app, "member@test.com")
        token = _set_photo(app, user.id)
        version = _user(app, "member@test.com").version
        resp = _self_upload(member_client, raw, version, filename="x.jpg", follow=True)
        assert resp.status_code == 200
        stored = _user(app, "member@test.com")
        assert stored.profile_photo_token == token and stored.version == version
        assert len(_audit_rows(app, user.id)) == 0

    def test_cancelled_event_still_shows_avatar_and_phone(self, app, admin_client) -> None:
        event_id, spot_id = _make_event_with_spot(app, name="Zrušená akce")
        with app.app_context():
            event = db.session.get(Event, event_id)
            event.status = EventStatus.CANCELLED
            db.session.commit()
        _, token = _assign(app, event_id, spot_id, "cancelled@test.com", "Vymyšlený Zrušený", "+420 111 222 333", True)
        row = _row_for(_event_html(admin_client, event_id), "Vymyšlený Zrušený")
        assert f"?v={token}" in row and 'href="tel:+420111222333"' in row

    def test_user_without_phone_or_photo_renders_cleanly(self, app, member_client) -> None:
        event_id, spot_id = _make_event_with_spot(app, name="Prázdná akce")
        _assign(app, event_id, spot_id, "bare@test.com", "Vymyšlený Holý", None, False)
        row = _row_for(_event_html(member_client, event_id), "Vymyšlený Holý")
        assert "tel:" not in row and "<img" not in row and "VH" in row
