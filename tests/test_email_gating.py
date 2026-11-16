"""Sender gates, deferred merge and drain guards in app.mail."""

import logging
from datetime import datetime, timedelta, timezone

import pytest

import app.mail as mail_module
from app.extensions import db
from app.mail import (
    drain_batched_outbox,
    drain_one_outbox_email,
    enqueue_deferred,
    send_assignment_confirmed,
    send_assignment_released,
    send_assignments_opened,
    send_debriefing_invitation,
    send_event_archived,
    send_event_cancelled,
    send_event_published,
    send_event_unarchived,
    send_external_opportunity,
    send_unfilled_spots_reminder,
)
from app.models.assignment import Assignment
from app.models.event import Event, EventSpot
from app.models.outbox import OutboxEmail
from app.models.role import Role
from app.models.settings import get_settings
from tests.test_email import _make_batched_row, _make_ed_event
from tests.test_external_events import make_opportunity
from tests.test_external_notifications import external_user

RECIPIENT = "prijemce@example.com"
INSTANCE_HEADER = "X-MedCover-Instance"


def _raiser(message):
    def _raise(*args, **kwargs):
        raise RuntimeError(message)

    return _raise


def _call_sender(sender, user, event):
    if sender is send_debriefing_invitation:
        spot = EventSpot(event_id=event.id)
        db.session.add(spot)
        db.session.flush()
        assignment = Assignment(spot_id=spot.id, user_id=user.id)
        db.session.add(assignment)
        db.session.flush()
        return sender(assignment, event)
    if sender is send_unfilled_spots_reminder:
        return sender(user, event, [object(), object()])
    return sender(user, event)


def _run_sender(app, sender, settings_field, *, enabled, role_name):
    """Send as a user holding only *role_name*; return the user's email and the (type, to_email) of every outbox row."""
    with app.app_context():
        user, event = _make_ed_event(delta_hours=72)
        user.roles = [db.session.scalar(db.select(Role).where(Role.name == role_name))]
        setattr(get_settings(), settings_field, enabled)
        db.session.commit()
        _call_sender(sender, user, event)
        db.session.commit()
        rows = [(r.notification_type, r.to_email) for r in db.session.scalars(db.select(OutboxEmail))]
        return user.email, rows


SENDER_CASES = [
    pytest.param(send_assignment_released, "notify_assignment", "assignment_released", id="assignment_released"),
    pytest.param(send_event_published, "notify_event_published", "event_published", id="event_published"),
    pytest.param(send_assignments_opened, "notify_assignments_opened", "assignments_opened", id="assignments_opened"),
    pytest.param(send_event_cancelled, "notify_event_cancelled", "event_cancelled", id="event_cancelled"),
    pytest.param(send_event_archived, "notify_event_archived", "event_archived", id="event_archived"),
    pytest.param(send_event_unarchived, "notify_event_unarchived", "event_unarchived", id="event_unarchived"),
    pytest.param(send_unfilled_spots_reminder, "notify_unfilled_reminder", "unfilled_reminder", id="unfilled_reminder"),
    pytest.param(send_debriefing_invitation, "notify_debriefing", "debriefing_invitation", id="debriefing_invitation"),
]


@pytest.mark.parametrize("sender,settings_field,ntype", SENDER_CASES)
class TestSenderGates:
    def test_enabled_and_allowed_enqueues(self, app, sender, settings_field, ntype):
        email, rows = _run_sender(app, sender, settings_field, enabled=True, role_name=Role.MEMBER)
        assert rows == [(ntype, email)]

    def test_disabled_setting_blocks(self, app, sender, settings_field, ntype):
        assert _run_sender(app, sender, settings_field, enabled=False, role_name=Role.MEMBER)[1] == []

    def test_role_excluded_blocks(self, app, sender, settings_field, ntype):
        assert _run_sender(app, sender, settings_field, enabled=True, role_name=Role.ADMIN)[1] == []


class TestExternalOpportunityGates:
    @pytest.mark.parametrize(
        "reason,expected_rows",
        [("eligible", 1), ("disabled", 0), ("not_external", 0), ("already_assigned", 0)],
        ids=["eligible", "disabled", "not_external", "already_assigned"],
    )
    def test_gates(self, app, external_client, reason, expected_rows):
        event_id, spot_id = make_opportunity(app)
        with app.app_context():
            user = external_user()
            event = db.session.get(Event, event_id)
            if reason == "disabled":
                get_settings().notify_external_opportunities = False
            elif reason == "not_external":
                user.roles = [db.session.scalar(db.select(Role).where(Role.name == Role.MEMBER))]
            elif reason == "already_assigned":
                # a second open spot keeps the event fillable, so only the per-user check can block
                db.session.add(EventSpot(event_id=event_id))
                event.assignments.append(Assignment(user=user, spot_id=spot_id))
            db.session.commit()
            send_external_opportunity(user, event)
            db.session.commit()
            rows = db.session.scalars(db.select(OutboxEmail)).all()
            assert [(r.notification_type, r.to_email) for r in rows] == [("external_opportunity", user.email)] * (
                expected_rows
            )


class TestDeferredMerge:
    def test_merge_keeps_czech_text_literal(self, app):
        with app.app_context():
            user, event = _make_ed_event(delta_hours=72)
            send_assignment_confirmed(user, event, "Zdravotník A")
            send_assignment_confirmed(user, event, "Řidič č. 2")
            db.session.commit()
            row = db.session.scalar(db.select(OutboxEmail))
            assert db.session.scalar(db.select(db.func.count(OutboxEmail.id))) == 1
            assert row.change_value == '{"action": "confirmed", "spot_description": "Řidič č. 2"}'
            assert row.change_type == "assignment"

    def test_merge_sorts_payload_keys(self, app):
        with app.app_context():
            user, event = _make_ed_event(delta_hours=72)
            for payload in ({"zeta": "ž", "alfa": "č"}, {"zeta": "ť", "alfa": "ď"}):
                enqueue_deferred(user, event, "assignment_confirmed", "S", "B", "x", payload)
            db.session.commit()
            assert db.session.scalar(db.select(OutboxEmail.change_value)) == '{"alfa": "ď", "zeta": "ť"}'


class TestDrainGuards:
    def test_failure_audit_error_logged(self, app, monkeypatch, caplog):
        caplog.set_level(logging.WARNING, logger="app.mail")

        monkeypatch.setattr("flask_mail.Mail.send", _raiser("smtp down"))
        monkeypatch.setattr(mail_module, "AuditLogEntry", _raiser("audit down"))
        with app.app_context():
            row = OutboxEmail(to_email=RECIPIENT, subject="S", body="B", retry_count=OutboxEmail.MAX_RETRIES - 1)
            db.session.add(row)
            db.session.commit()
            row_id = row.id
            assert drain_one_outbox_email() is True
        with app.app_context():
            row = db.session.get(OutboxEmail, row_id)
            assert (row.status, row.retry_count, row.last_error) == ("failed", OutboxEmail.MAX_RETRIES, "smtp down")
        assert [r.getMessage() for r in caplog.records if r.name == "app.mail"] == [
            f"Mail permanently failed: id={row_id} to={RECIPIENT} — smtp down "
            f"(after {OutboxEmail.MAX_RETRIES} retries)",
            f"Failed to write failure audit log for outbox id={row_id} — audit down",
        ]

    @pytest.mark.parametrize(
        "instance_id,html_body,expected_headers",
        [
            ("test-instance", "<p>Ahoj</p>", {INSTANCE_HEADER: "test-instance"}),
            ("", None, None),
        ],
        ids=["html-with-instance", "plain-no-instance"],
    )
    def test_legacy_send_html_and_instance_header(self, app, monkeypatch, instance_id, html_body, expected_headers):
        sent = []
        monkeypatch.setattr("flask_mail.Mail.send", lambda self, message: sent.append(message))
        monkeypatch.setattr(mail_module, "_INSTANCE_ID", instance_id)
        with app.app_context():
            row = OutboxEmail(to_email=RECIPIENT, subject="S", body="B", html_body=html_body)
            db.session.add(row)
            db.session.commit()
            row_id = row.id
            assert drain_one_outbox_email() is True
        assert len(sent) == 1
        assert (sent[0].html, sent[0].extra_headers, sent[0].recipients) == (
            html_body,
            expected_headers,
            [RECIPIENT],
        )
        with app.app_context():
            assert db.session.get(OutboxEmail, row_id).status == "sent"

    def test_batch_failure_audit_error_logged(self, app, monkeypatch, caplog):
        caplog.set_level(logging.WARNING, logger="app.mail")

        monkeypatch.setattr("flask_mail.Mail.send", _raiser("permanent"))
        monkeypatch.setattr(mail_module, "AuditLogEntry", _raiser("audit down"))
        with app.app_context():
            user, event = _make_ed_event(delta_hours=72)
            user_id, user_email = user.id, user.email
            past = datetime.now(timezone.utc) - timedelta(minutes=1)
            rows = [
                _make_batched_row(user, event, "event_published", send_after=past),
                _make_batched_row(user, event, "event_cancelled", send_after=past),
            ]
            for row in rows:
                row.retry_count = OutboxEmail.MAX_RETRIES - 1
            db.session.commit()
            ids = [row.id for row in rows]
            assert drain_batched_outbox() is True
        with app.app_context():
            assert [db.session.get(OutboxEmail, i).status for i in ids] == ["failed", "failed"]
        assert [r.getMessage() for r in caplog.records if r.name == "app.mail"] == [
            "Failed to write batch failure audit — audit down",
            f"Batch mail failed (user_id={user_id} to={user_email} rows=2) — permanent",
        ]

    @pytest.mark.parametrize(
        "instance_id,expected_headers",
        [("test-instance", {INSTANCE_HEADER: "test-instance"}), ("", None)],
        ids=["with-instance", "no-instance"],
    )
    def test_batch_instance_header(self, app, monkeypatch, instance_id, expected_headers):
        sent = []
        monkeypatch.setattr("flask_mail.Mail.send", lambda self, message: sent.append(message))
        monkeypatch.setattr(mail_module, "_INSTANCE_ID", instance_id)
        with app.app_context():
            user, event = _make_ed_event(delta_hours=72)
            past = datetime.now(timezone.utc) - timedelta(minutes=1)
            row = _make_batched_row(user, event, "event_published", send_after=past)
            db.session.commit()
            row_id = row.id
            assert drain_batched_outbox() is True
        assert len(sent) == 1
        assert sent[0].extra_headers == expected_headers
        with app.app_context():
            assert db.session.get(OutboxEmail, row_id).status == "sent"
