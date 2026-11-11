"""Tests for the decoding shared by the signature and profile photo uploads."""

import io
import struct

import pytest
from PIL import Image, ImageOps

from app import image_upload
from app.extensions import db
from app.models.user import UserAccount
from app.profile_photo import ProfilePhotoError, process_profile_photo_upload
from app.signature import SignatureError, process_signature_upload
from tests.test_profile_photo import _user

PIPELINES = [
    pytest.param(process_profile_photo_upload, ProfilePhotoError, id="photo"),
    pytest.param(process_signature_upload, SignatureError, id="signature"),
]


def _truncated_heic_source() -> bytes:
    buf = io.BytesIO()
    Image.effect_noise((400, 300), 50).convert("RGB").save(buf, "HEIF")
    return buf.getvalue()


def _truncated_heic() -> bytes:
    return _truncated_heic_source()[:2000]


def _corrupt_heic_raising_eof() -> bytes:
    """HEIC whose damaged header makes pillow_heif raise EOFError from load()."""
    raw = bytearray(_truncated_heic_source())
    raw[379] = 177
    raw[92] = 161
    return bytes(raw)


def _jpeg() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (400, 300), (30, 120, 200)).save(buf, "JPEG")
    return buf.getvalue()


@pytest.mark.parametrize(("process", "error"), PIPELINES)
class TestDecodeFailures:
    def test_truncated_heic_is_rejected_with_pipeline_error(self, process, error) -> None:
        with pytest.raises(error, match="není platný obrázek"):
            process(_truncated_heic())

    def test_corrupt_heic_header_is_rejected_with_pipeline_error(self, process, error) -> None:
        raw = _corrupt_heic_raising_eof()
        with pytest.raises(EOFError):
            Image.open(io.BytesIO(raw)).load()
        with pytest.raises(error, match="není platný obrázek"):
            process(raw)

    def test_unreadable_exif_is_ignored(self, process, error, monkeypatch: pytest.MonkeyPatch) -> None:
        def broken(*_args, **_kwargs):
            raise struct.error("unpack requires a buffer of 2 bytes")

        monkeypatch.setattr(ImageOps, "exif_transpose", broken)
        assert process(_jpeg())

    def test_unreadable_exif_malformed_in_file(self, process, error) -> None:
        raw = _jpeg_with_bad_exif()
        with pytest.raises(struct.error):
            ImageOps.exif_transpose(Image.open(io.BytesIO(raw)))
        assert process(raw)


class TestUploadRoutesSurviveCorruptImages:
    def test_photo_route_flashes_for_corrupt_heic(self, app, member_client) -> None:
        user = _user(app, "member@test.com")
        resp = member_client.post(
            "/users/profile",
            data={
                "action": "photo_upload",
                "version": str(user.version),
                "photo": (io.BytesIO(_truncated_heic()), "me.heic"),
            },
            content_type="multipart/form-data",
            follow_redirects=True,
        )
        assert resp.status_code == 200
        assert "Soubor není platný obrázek".encode() in resp.data
        with app.app_context():
            assert db.session.get(UserAccount, user.id).profile_photo_token is None

    def test_signature_route_flashes_for_corrupt_heic(self, app, member_client) -> None:
        user = _user(app, "member@test.com")
        resp = member_client.post(
            "/users/profile",
            data={
                "action": "signature_upload",
                "version": str(user.version),
                "signature": (io.BytesIO(_truncated_heic()), "sig.heic"),
            },
            content_type="multipart/form-data",
            follow_redirects=True,
        )
        assert resp.status_code == 200
        assert "Soubor není platný obrázek".encode() in resp.data


@pytest.mark.parametrize(
    ("action", "field"),
    [("photo_upload", "photo"), ("signature_upload", "signature")],
)
def test_routes_flash_for_corrupt_heic_header(app, member_client, action: str, field: str) -> None:
    user = _user(app, "member@test.com")
    resp = member_client.post(
        "/users/profile",
        data={
            "action": action,
            "version": str(user.version),
            field: (io.BytesIO(_corrupt_heic_raising_eof()), "x.heic"),
        },
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert "není platný obrázek" in resp.get_data(as_text=True)


def test_pillow_pixel_limit_is_set_once_by_shared_module() -> None:
    assert Image.MAX_IMAGE_PIXELS == image_upload.MAX_IMAGE_PIXELS


def _jpeg_with_bad_exif() -> bytes:
    """JPEG whose EXIF block makes ImageOps.exif_transpose raise struct.error."""
    exif = Image.Exif()
    exif[0x0112] = 6
    exif[0x010F] = "Cam"
    exif.get_ifd(0x8825)[1] = "N"
    buf = io.BytesIO()
    Image.new("RGB", (400, 300), (1, 2, 3)).save(buf, "JPEG", exif=exif)
    raw = bytearray(buf.getvalue())
    for offset, value in ((41, 32), (117, 76), (63, 22)):
        raw[offset] = value
    return bytes(raw)


@pytest.mark.parametrize(
    ("action", "field"),
    [("photo_upload", "photo"), ("signature_upload", "signature")],
)
def test_routes_survive_malformed_exif(app, member_client, action: str, field: str) -> None:
    user = _user(app, "member@test.com")
    resp = member_client.post(
        "/users/profile",
        data={
            "action": action,
            "version": str(user.version),
            field: (io.BytesIO(_jpeg_with_bad_exif()), "x.jpg"),
        },
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert resp.status_code == 200
