"""Tests for admin blueprint: dashboard, audit log."""

import re
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import sqlalchemy as sa
from cryptography.fernet import Fernet

from app.extensions import db
from app.models.audit import AuditLogEntry
from app.models.event import Event, EventStatus
from app.models.master_event import MasterEvent
from app.models.outbox import OutboxEmail
from app.models.role import Role
from app.models.settings import get_settings
from app.models.user import UserAccount
from app.routes.admin import _admin_statistics, _check_db_health
from app.utils import diff_changes
from tests.conftest import _make_user


def _audit(summary, *, actor_id=None, ts=None, entity_type="Event"):
    db.session.add(
        AuditLogEntry(
            action_type="edit",
            entity_type=entity_type,
            entity_id="1",
            summary=summary,
            actor_id=actor_id,
            timestamp=ts or datetime(2026, 1, 15, 12, tzinfo=timezone.utc),
        )
    )
    db.session.commit()


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

        # Configure a real (encrypted) SMTP password in settings
        test_password = "super_secret_smtp_pass_99"
        with app.app_context():
            settings = get_settings()
            key = Fernet.generate_key()
            f = Fernet(key)
            settings.smtp_password_encrypted = f.encrypt(test_password.encode()).decode()
            db.session.commit()

        response = admin_client.get("/admin/settings/")
        assert response.status_code == 200
        # The plaintext password must never appear in the HTML
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


class TestAuditLogFilters:
    @pytest.mark.parametrize(
        ("query", "expected"),
        [
            ("actor_id=system", {"Zápis systému (akce)", "Zápis systému (nadřazená akce)"}),
            ("actor_id=system&entity_type=Event", {"Zápis systému (akce)"}),
            ("actor_id={actor}", {"Zápis uživatele"}),
            ("actor_id={unknown}", set()),
        ],
    )
    def test_actor_filter(self, app, admin_client, query, expected):
        with app.app_context():
            actor = _make_user("auditor@medcover.local", "Vymyšlený Auditor", Role.MEMBER)
            actor_id = actor.id
            _audit("Zápis systému (akce)", entity_type="Event")
            _audit("Zápis systému (nadřazená akce)", entity_type="MasterEvent")
            _audit("Zápis uživatele", actor_id=actor_id)

        response = admin_client.get("/admin/audit-log/?" + query.format(actor=actor_id, unknown=uuid.uuid4()))
        assert response.status_code == 200
        data = response.get_data(as_text=True)
        summaries = ("Zápis systému (akce)", "Zápis systému (nadřazená akce)", "Zápis uživatele")
        assert {s for s in summaries if s in data} == expected

    @pytest.mark.parametrize("bad", ["not-a-uuid", "SYSTEM"])
    def test_invalid_actor_id_is_ignored_and_not_echoed(self, app, admin_client, bad):
        with app.app_context():
            actor = _make_user("auditor@medcover.local", "Vymyšlený Auditor", Role.MEMBER)
            _audit("Systémový zápis")
            _audit("Zápis uživatele", actor_id=actor.id)

        response = admin_client.get(f"/admin/audit-log/?actor_id={bad}")
        assert response.status_code == 200
        assert "Systémový zápis".encode() in response.data
        assert "Zápis uživatele".encode() in response.data
        actor_select = re.search(r'<select name="actor_id".*?</select>', response.get_data(as_text=True), re.S)
        assert actor_select is not None
        assert "selected" not in actor_select.group(0)
        assert "Zrušit filtry".encode() not in response.data

    def test_date_filters_use_local_day_boundaries(self, app, admin_client):
        rows = {
            "Minutu před začátkem dne": datetime(2026, 1, 14, 22, 59, tzinfo=timezone.utc),
            "Přesně na začátku dne": datetime(2026, 1, 14, 23, 0, tzinfo=timezone.utc),
            "Minutu před koncem dne": datetime(2026, 1, 15, 22, 59, tzinfo=timezone.utc),
            "Přesně na začátku dalšího dne": datetime(2026, 1, 15, 23, 0, tzinfo=timezone.utc),
        }
        with app.app_context():
            for summary, ts in rows.items():
                _audit(summary, ts=ts)
        before_start, at_start, before_end, next_day = rows

        def listed(query):
            data = admin_client.get(f"/admin/audit-log/?{query}").get_data(as_text=True)
            return {summary for summary in rows if summary in data}

        assert listed("date_to=2026-01-15") == {before_start, at_start, before_end}
        assert listed("date_from=2026-01-15") == {at_start, before_end, next_day}
        assert listed("date_from=2026-01-15&date_to=2026-01-15") == {at_start, before_end}

        # invalid dates are ignored and echoed back unchanged
        response = admin_client.get("/admin/audit-log/?date_from=garbage&date_to=garbage")
        assert response.status_code == 200
        data = response.get_data(as_text=True)
        assert {summary for summary in rows if summary in data} == set(rows)
        assert data.count('value="garbage"') == 2
        assert b"alert-danger" not in response.data


class TestDbHealthCheck:
    def test_failed_probe_rolls_back_and_leaves_session_usable(self, app):
        with app.app_context():
            db.session.execute(db.text("SELECT 1"))
            db.session.connection().invalidate()

            result = _check_db_health()

            assert result["status"] == "error"
            assert result["ms"] is None
            assert db.session.execute(db.text("SELECT 1")).scalar() == 1

    def test_rollback_failure_is_swallowed(self, app, monkeypatch):
        def fail_probe(*args, **kwargs):
            raise RuntimeError("probe failed")

        def fail_rollback():
            raise OSError("rollback failed")

        with app.app_context():
            monkeypatch.setattr(db.session, "execute", fail_probe)
            monkeypatch.setattr(db.session, "rollback", fail_rollback)

            assert _check_db_health()["error"] == "RuntimeError"


_NOW = datetime(2026, 1, 15, 12, tzinfo=timezone.utc)


class TestAdminStatistics:
    def test_empty_database_returns_zero_ints(self, app):
        with app.app_context():
            stats = _admin_statistics(_NOW)

        for key in ("user_total", "user_active", "user_pending", "user_archived", "event_total"):
            assert type(stats[key]) is int
            assert stats[key] == 0
        assert list(stats["event_counts"]) == [s.value for s in EventStatus]
        assert stats["event_counts"] == {s.value: 0 for s in EventStatus}
        assert all(type(v) is int and v == 0 for v in stats["event_counts"].values())
        for key in ("outbox_pending", "outbox_failed", "outbox_sent"):
            assert type(stats[key]) is int
            assert stats[key] == 0
        assert stats["outbox_last_status"] is None
        assert stats["outbox_last_sent"] is None
        assert stats["recent_audit"] == []

    def test_populated_dataset_matches_expected(self, app, admin_client):
        with app.app_context():
            for i, (active, archived) in enumerate(
                [(True, False)] * 2 + [(False, False)] * 2 + [(True, True), (False, True)]
            ):
                db.session.add(
                    UserAccount(
                        email=f"stat{i}@medcover.local",
                        name=f"Vymyšlený Uživatel {i}",
                        password_hash="x",
                        is_active=active,
                        is_archived=archived,
                    )
                )
            me = MasterEvent(name="Testovací nadřazená akce")
            db.session.add(me)
            db.session.flush()
            creator = db.session.scalar(db.select(UserAccount).where(UserAccount.email == "admin@test.com"))
            for status, archived in [
                (EventStatus.DRAFT, False),
                (EventStatus.PUBLISHED, True),
                (EventStatus.COMPLETED, False),
            ]:
                db.session.add(
                    Event(
                        name=f"Testovací akce {status.value}",
                        master_event_id=me.id,
                        start_datetime=_NOW,
                        end_datetime=_NOW + timedelta(hours=2),
                        status=status,
                        archived=archived,
                        created_by_id=creator.id,
                    )
                )
            for status, age, sent_age in [
                ("pending", timedelta(hours=23, minutes=59), None),
                ("pending", timedelta(hours=24), None),
                ("failed", timedelta(hours=1), None),
                ("sent", timedelta(hours=2), timedelta(hours=2)),
                ("sent", timedelta(hours=24, minutes=1), timedelta(hours=24)),
                ("sending", timedelta(hours=1, minutes=30), None),
            ]:
                db.session.add(
                    OutboxEmail(
                        to_email="prijemce@medcover.local",
                        subject="Testovací zpráva",
                        body="Text",
                        status=status,
                        created_at=_NOW - age,
                        sent_at=_NOW - sent_age if sent_age else None,
                    )
                )
            db.session.commit()

            stats = _admin_statistics(_NOW)

        recent_audit = stats.pop("recent_audit")
        assert isinstance(recent_audit, list)
        assert stats.pop("outbox_last_status") == "failed"
        assert stats == {
            # the admin user from the fixture is one more active, non-archived user
            "user_total": 5,
            "user_active": 3,
            "user_pending": 2,
            "user_archived": 2,
            "event_total": 3,
            "event_counts": {
                s.value: 1 if s in (EventStatus.DRAFT, EventStatus.PUBLISHED, EventStatus.COMPLETED) else 0
                for s in EventStatus
            },
            "outbox_pending": 2,
            "outbox_failed": 1,
            "outbox_sent": 1,
            "outbox_last_sent": _NOW - timedelta(hours=2),
            "feedback_count": 0,
        }
        counts = [v for k, v in stats.items() if k not in ("event_counts", "outbox_last_sent")]
        assert all(type(v) is int for v in [*counts, *stats["event_counts"].values()])
        assert admin_client.get("/admin/").status_code == 200

    def test_uses_at_most_seven_select_statements(self, app):
        statements: list[str] = []

        def count(conn, cursor, statement, parameters, context, executemany):
            statements.append(statement)

        with app.app_context():
            sa.event.listen(db.engine, "before_cursor_execute", count)
            try:
                _admin_statistics(_NOW)
            finally:
                sa.event.remove(db.engine, "before_cursor_execute", count)

        assert len(statements) <= 7
