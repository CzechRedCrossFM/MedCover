from pathlib import Path

import pytest

IMG = Path(__file__).resolve().parent.parent / "app" / "static" / "img"
STAMPED = ["favicon.ico", "apple-touch-icon.png", "logo.webp", "logo_nav.webp"]


def _html(client, url: str) -> str:
    resp = client.get(url, follow_redirects=True)
    assert resp.status_code == 200
    return resp.get_data(as_text=True)


@pytest.fixture
def dev_build(app, monkeypatch):
    monkeypatch.setitem(app.config, "GIT_COMMIT", "dev")


@pytest.fixture
def sha_build(app, monkeypatch):
    monkeypatch.setitem(app.config, "GIT_COMMIT", "abc1234")


def test_login_uses_stamped_images_in_dev_build(client, dev_build):
    html = _html(client, "/auth/login")
    assert "/static/img/dev/favicon.ico" in html
    assert "/static/img/dev/apple-touch-icon.png" in html
    assert "/static/img/dev/logo.webp" in html


def test_nav_uses_stamped_logo_in_dev_build(admin_client, dev_build):
    assert "/static/img/dev/logo_nav.webp" in _html(admin_client, "/")


def test_login_uses_original_images_in_sha_build(client, sha_build):
    html = _html(client, "/auth/login")
    assert "/static/img/favicon.ico" in html
    assert "/static/img/apple-touch-icon.png" in html
    assert "/static/img/logo.webp" in html
    assert "/img/dev/" not in html


def test_nav_uses_original_logo_in_sha_build(admin_client, sha_build):
    html = _html(admin_client, "/")
    assert "/static/img/logo_nav.webp" in html
    assert "/img/dev/" not in html


def test_stamped_files_match_originals():
    assert sorted(p.name for p in (IMG / "dev").iterdir()) == sorted(STAMPED)
    for name in STAMPED:
        assert (IMG / name).read_bytes() != (IMG / "dev" / name).read_bytes()


@pytest.mark.parametrize("name", STAMPED)
def test_dev_static_files_served(client, name):
    assert client.get(f"/static/img/dev/{name}").status_code == 200
