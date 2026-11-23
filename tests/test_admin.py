"""Tests for admin blueprint: dashboard, health checks, permissions page, audit log."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

import pytest

import app.routes.admin as admin_routes
from app.extensions import db
from app.models.audit import AuditLogEntry
from app.models.role import ALL_PERMISSIONS, ROLE_PERMISSIONS, Role
from app.models.settings import get_settings
from app.utils import diff_changes
from tests.conftest import _make_user
from tests.test_event_log import _entry

SMTP_HOST = "smtp.example.com"
SMTP_PORT = 2525
SMTP_REFUSED = "refused"
SCHEDULER_NOW = datetime(2030, 5, 10, 12, 0, tzinfo=timezone.utc)


class TestDiffChanges:
    def test_returns_only_changed_fields(self):
        before = {"name": "A", "desc": "same"}
        after = {"name": "B", "desc": "same"}
        result = diff_changes(before, after)
        assert result == {"name": ["A", "B"]}
        assert "desc" not in result

    def test_empty_when_nothing_changed(self):
        before = {"x": 1, "y": 2}
        after = {"x": 1, "y": 2}
        assert diff_changes(before, after) == {}

    def test_all_fields_changed(self):
        before = {"a": "old", "b": "old2"}
        after = {"a": "new", "b": "new2"}
        result = diff_changes(before, after)
        assert result == {"a": ["old", "new"], "b": ["old2", "new2"]}

    def test_new_keys_in_after_included(self):
        before = {"a": "x"}
        after = {"a": "x", "b": "y"}
        result = diff_changes(before, after)
        assert "b" in result
        assert result["b"] == [None, "y"]

    def test_missing_keys_in_after(self):
        before = {"a": "x", "b": "y"}
        after = {"a": "x"}
        result = diff_changes(before, after)
        assert "b" in result
        assert result["b"] == ["y", None]


class TestAdminDashboard:
    def test_admin_dashboard_requires_login(self, client):
        response = client.get("/admin/", follow_redirects=False)
        assert response.status_code == 302

    def test_member_cannot_access_admin_dashboard(self, member_client):
        response = member_client.get("/admin/")
        assert response.status_code == 403

    def test_admin_can_access_dashboard(self, admin_client):
        response = admin_client.get("/admin/")
        assert response.status_code == 200

    def test_admin_dashboard_shows_service_status(self, admin_client):
        response = admin_client.get("/admin/")
        assert response.status_code == 200
        # Dashboard should contain DB status info
        assert b"DB" in response.data or "Databáze".encode() in response.data

    def test_admin_dashboard_has_audit_log_link(self, admin_client):
        response = admin_client.get("/admin/")
        assert b"audit-log" in response.data


class TestAppSettings:
    def test_settings_page_requires_admin(self, member_client):
        response = member_client.get("/admin/settings/")
        assert response.status_code == 403

    def test_settings_page_loads_for_admin(self, admin_client):
        response = admin_client.get("/admin/settings/")
        assert response.status_code == 200

    def test_settings_page_does_not_expose_smtp_password(self, app, admin_client):
        """The plaintext SMTP password must never appear in the settings page HTML."""

        test_password = "super_secret_smtp_pass_99"
        with app.app_context():
            settings = get_settings()
            settings.set_smtp_password(test_password)
            db.session.commit()

        response = admin_client.get("/admin/settings/")
        assert response.status_code == 200
        # positive control: the page knows a password is stored
        assert "(beze změny)".encode() in response.data
        assert test_password.encode() not in response.data


class TestAuditLogUI:
    def test_audit_log_list_requires_login(self, client):
        response = client.get("/admin/audit-log/", follow_redirects=False)
        assert response.status_code == 302

    def test_member_cannot_access_audit_log(self, member_client):
        response = member_client.get("/admin/audit-log/")
        assert response.status_code == 403

    def test_admin_can_access_audit_log(self, admin_client):
        response = admin_client.get("/admin/audit-log/")
        assert response.status_code == 200

    def test_audit_log_shows_entries(self, app, admin_client):
        with app.app_context():
            entry = AuditLogEntry(
                action_type="create",
                entity_type="Event",
                entity_id="42",
                summary="Vytvořena akce 'Test'",
            )
            db.session.add(entry)
            db.session.commit()

        response = admin_client.get("/admin/audit-log/")
        assert "Vytvořena".encode() in response.data

    def test_audit_log_filter_by_entity_type(self, app, admin_client):
        with app.app_context():
            db.session.add(
                AuditLogEntry(
                    action_type="create",
                    entity_type="Event",
                    entity_id="1",
                    summary="Akce vytvořena",
                )
            )
            db.session.add(
                AuditLogEntry(
                    action_type="edit",
                    entity_type="MasterEvent",
                    entity_id="2",
                    summary="ME upravena",
                )
            )
            db.session.commit()

        response = admin_client.get("/admin/audit-log/?entity_type=Event")
        assert response.status_code == 200
        assert "Akce vytvořena".encode() in response.data
        assert b"ME upravena" not in response.data

    def test_audit_log_search_matches_summary_case_insensitively(self, app, admin_client):
        """?q=... on /admin/audit-log/ matches summaries case-insensitively."""
        with app.app_context():
            db.session.add(
                AuditLogEntry(
                    action_type="create",
                    entity_type="Event",
                    entity_id="99",
                    summary="Unikátní záznam AuditSearchToken",
                )
            )
            db.session.add(
                AuditLogEntry(
                    action_type="create",
                    entity_type="Event",
                    entity_id="100",
                    summary="Jiná událost bez tokenu",
                )
            )
            db.session.commit()

        # Lowercase query must still find mixed-case summary.
        response = admin_client.get("/admin/audit-log/?q=auditsearchtoken")
        assert response.status_code == 200
        assert b"AuditSearchToken" in response.data
        assert "Jiná událost".encode() not in response.data

        # Uppercase query must still find mixed-case summary.
        response = admin_client.get("/admin/audit-log/?q=AUDITSEARCHTOKEN")
        assert response.status_code == 200
        assert b"AuditSearchToken" in response.data

    def test_audit_log_detail_requires_admin(self, member_client):
        response = member_client.get("/admin/audit-log/1")
        assert response.status_code == 403

    def test_audit_log_detail_404_for_missing(self, admin_client):
        response = admin_client.get("/admin/audit-log/999999")
        assert response.status_code == 404

    def test_audit_log_detail_shows_diff(self, app, admin_client):
        with app.app_context():
            entry = AuditLogEntry(
                action_type="edit",
                entity_type="Event",
                entity_id="10",
                summary="Upravena akce",
                changes_json={"name": ["Starý název", "Nový název"]},
            )
            db.session.add(entry)
            db.session.commit()
            entry_id = entry.id

        response = admin_client.get(f"/admin/audit-log/{entry_id}")
        assert response.status_code == 200
        assert "Starý název".encode() in response.data
        assert "Nový název".encode() in response.data


def _clock(*readings: float) -> SimpleNamespace:
    return SimpleNamespace(monotonic=Mock(side_effect=list(readings)))


def _smtp_settings() -> SimpleNamespace:
    return SimpleNamespace(smtp_configured=True, smtp_server=SMTP_HOST, smtp_port=SMTP_PORT)


class TestHealthChecks:
    def test_db_error(self, app, monkeypatch):
        monkeypatch.setattr(db.session, "execute", Mock(side_effect=RuntimeError("db down")))
        with app.app_context():
            assert admin_routes._check_db_health() == {"status": "error", "ms": None, "error": "RuntimeError"}

    @pytest.mark.parametrize(
        ("elapsed", "expected"),
        [
            (0.01, {"status": "ok", "ms": 10, "error": None}),
            (0.2005, {"status": "ok", "ms": 200, "error": None}),
            (0.2015, {"status": "warning", "ms": 201, "error": None}),
            (0.25, {"status": "warning", "ms": 250, "error": None}),
        ],
        ids=["fast", "at-limit", "just-over-limit", "slow"],
    )
    def test_db_latency(self, app, monkeypatch, elapsed, expected):
        monkeypatch.setattr(admin_routes, "time", _clock(0.0, elapsed))
        with app.app_context():
            assert admin_routes._check_db_health() == expected

    def test_scheduler_never_seen(self):
        settings = SimpleNamespace(scheduler_last_seen=None)
        assert admin_routes._check_scheduler(settings, SCHEDULER_NOW) == {
            "status": "unknown",
            "last": None,
            "age_s": None,
        }

    @pytest.mark.parametrize(
        ("age", "status"),
        [(5, "ok"), (29, "ok"), (30, "warning"), (299, "warning"), (300, "error"), (400, "error")],
        ids=["5s", "29s", "30s", "299s", "300s", "400s"],
    )
    def test_scheduler_age(self, age, status):
        last = SCHEDULER_NOW - timedelta(seconds=age)
        settings = SimpleNamespace(scheduler_last_seen=last)
        assert admin_routes._check_scheduler(settings, SCHEDULER_NOW) == {
            "status": status,
            "last": last,
            "age_s": age,
        }

    def test_smtp_unconfigured(self, monkeypatch):
        connect = MagicMock()
        monkeypatch.setattr(admin_routes, "socket", SimpleNamespace(create_connection=connect))
        result = admin_routes._check_smtp(SimpleNamespace(smtp_configured=False))
        assert result == {"configured": False, "status": "unconfigured", "ms": None, "error": None}
        connect.assert_not_called()

    @pytest.mark.parametrize(
        ("elapsed", "expected"),
        [
            (0.005, {"status": "ok", "ms": 5}),
            (1.0, {"status": "ok", "ms": 1000}),
            (1.0015, {"status": "warning", "ms": 1001}),
            (1.5, {"status": "warning", "ms": 1500}),
        ],
        ids=["fast", "at-limit", "just-over-limit", "slow"],
    )
    def test_smtp_latency(self, monkeypatch, elapsed, expected):
        connect = MagicMock()
        monkeypatch.setattr(admin_routes, "time", _clock(0.0, elapsed))
        monkeypatch.setattr(admin_routes, "socket", SimpleNamespace(create_connection=connect))
        assert admin_routes._check_smtp(_smtp_settings()) == {"configured": True, "error": None, **expected}
        connect.assert_called_once_with((SMTP_HOST, SMTP_PORT), timeout=3)

    @pytest.mark.parametrize(
        ("raised", "error"),
        [
            (TimeoutError(), f"Spojení na {SMTP_HOST}:{SMTP_PORT} vypršelo (timeout 3 s)"),
            (OSError(SMTP_REFUSED), SMTP_REFUSED),
        ],
        ids=["timeout", "os-error"],
    )
    def test_smtp_connect_failure(self, monkeypatch, raised, error):
        connect = Mock(side_effect=raised)
        monkeypatch.setattr(admin_routes, "socket", SimpleNamespace(create_connection=connect))
        assert admin_routes._check_smtp(_smtp_settings()) == {
            "configured": True,
            "status": "error",
            "ms": None,
            "error": error,
        }

    @pytest.mark.parametrize("fails", [True, False], ids=["connect-fails", "connect-ok"])
    def test_dashboard_renders_smtp_state(self, app, admin_client, monkeypatch, fails):
        with app.app_context():
            settings = get_settings()
            settings.smtp_server = SMTP_HOST
            settings.smtp_port = SMTP_PORT
            settings.smtp_username = "mailer@example.com"
            settings.set_smtp_password("somepass")
            db.session.commit()
        connect = Mock(side_effect=OSError(SMTP_REFUSED)) if fails else MagicMock()
        monkeypatch.setattr(admin_routes, "socket", SimpleNamespace(create_connection=connect))

        response = admin_client.get("/admin/")

        assert response.status_code == 200
        connect.assert_called_once()
        body = response.data.decode()
        if fails:
            assert SMTP_REFUSED in body
            assert f"{SMTP_HOST}:{SMTP_PORT} — " not in body
        else:
            assert SMTP_REFUSED not in body
            assert f"{SMTP_HOST}:{SMTP_PORT} — " in body


class TestPermissionsPage:
    def test_admin_sees_matrix(self, admin_client):
        response = admin_client.get("/admin/permissions")
        assert response.status_code == 200
        body = response.data.decode()
        for role in ROLE_PERMISSIONS:
            assert f'<th class="text-center minw-110">{role}</th>' in body
        for perm in ALL_PERMISSIONS:
            assert f'<code class="text-muted font-78">{perm["code"]}</code>' in body
        granted = sum(1 for perms in ROLE_PERMISSIONS.values() for p in ALL_PERMISSIONS if p["code"] in perms)
        denied = len(ALL_PERMISSIONS) * len(ROLE_PERMISSIONS) - granted
        assert granted > 0
        assert denied > 0
        assert body.count("✔") == granted
        assert body.count("✘") == denied

    def test_member_forbidden(self, member_client):
        assert member_client.get("/admin/permissions").status_code == 403

    def test_anonymous_redirected_to_login(self, client):
        response = client.get("/admin/permissions")
        assert response.status_code == 302
        assert "/auth/login" in response.headers["Location"]


def _day(day, hour):
    return datetime(2030, 5, day, hour, 0, tzinfo=timezone.utc)


class TestAuditLogFilters:
    ALL = {"AUD-A-CREATE", "AUD-A-EDIT", "AUD-B-CREATE", "AUD-B-EDIT"}

    @pytest.fixture
    def seeded(self, app, admin_client):
        with app.app_context():
            a = _make_user("actor-a@medcover.local", "Testovací Aneta", Role.COORDINATOR)
            b = _make_user("actor-b@medcover.local", "Vymyšlený Bohumil", Role.COORDINATOR)
            _entry("Event", 1, "AUD-A-CREATE", actor=a.id, timestamp=_day(10, 8), action="create")
            _entry("Event", 1, "AUD-A-EDIT", actor=a.id, timestamp=_day(11, 8))
            _entry("Event", 1, "AUD-B-CREATE", actor=b.id, timestamp=_day(12, 8), action="create")
            _entry("Event", 1, "AUD-B-EDIT", actor=b.id, timestamp=_day(13, 8))
            db.session.commit()
            return {"a": str(a.id), "b": str(b.id)}

    def _markers(self, client, query, universe=None):
        response = client.get(f"/admin/audit-log/?{query}")
        assert response.status_code == 200
        body = response.data.decode()
        return {m for m in universe or self.ALL if m in body}

    def test_unfiltered_lists_everything(self, admin_client, seeded):
        assert self._markers(admin_client, "") == self.ALL

    def test_actor_filter(self, admin_client, seeded):
        assert self._markers(admin_client, f"actor_id={seeded['a']}") == {"AUD-A-CREATE", "AUD-A-EDIT"}

    def test_action_type_filter(self, admin_client, seeded):
        assert self._markers(admin_client, "action_type=edit") == {"AUD-A-EDIT", "AUD-B-EDIT"}

    def test_date_range_inclusive(self, app, admin_client):
        with app.app_context():
            # midnight UTC instants fall on the same day in UTC and in Prague
            _entry("Event", 1, "AUD-D-BEFORE", timestamp=_day(9, 12))
            _entry("Event", 1, "AUD-D-START", timestamp=_day(10, 0))
            _entry("Event", 1, "AUD-D-LAST", timestamp=_day(12, 20))
            _entry("Event", 1, "AUD-D-NEXT", timestamp=_day(13, 0))
            _entry("Event", 1, "AUD-D-AFTER", timestamp=_day(13, 12))
            db.session.commit()
        universe = {"AUD-D-BEFORE", "AUD-D-START", "AUD-D-LAST", "AUD-D-NEXT", "AUD-D-AFTER"}
        assert self._markers(admin_client, "date_from=2030-05-10&date_to=2030-05-12", universe) == {
            "AUD-D-START",
            "AUD-D-LAST",
        }
        assert self._markers(admin_client, "date_from=2030-05-10", universe) == universe - {"AUD-D-BEFORE"}
        assert self._markers(admin_client, "date_to=2030-05-12", universe) == {
            "AUD-D-BEFORE",
            "AUD-D-START",
            "AUD-D-LAST",
        }

    @pytest.mark.parametrize("query", ["date_from=zz", "date_to=2030-99-99"], ids=["bad-from", "bad-to"])
    def test_invalid_dates_ignored(self, admin_client, seeded, query):
        assert self._markers(admin_client, query) == self.ALL

    def test_combined_filters(self, admin_client, seeded):
        query = f"actor_id={seeded['b']}&action_type=edit&date_from=2030-05-12&date_to=2030-05-13"
        assert self._markers(admin_client, query) == {"AUD-B-EDIT"}
        # swapping the actor drops B's entry, proving the actor term is applied
        assert self._markers(admin_client, query.replace(seeded["b"], seeded["a"])) == set()
