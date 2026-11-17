"""Outbox payload fallbacks, enqueue error handling and fail-safe gates in app.mail."""

import json
import logging

import pytest
from flask import g

import app.mail as mail_module
from app.extensions import db
from app.mail import (
    _EVENT_CHANGED_CHANGE_TYPE,
    _base_context,
    _enqueue,
    _is_notify_enabled,
    _is_test_notification,
    _row_to_entry,
    user_can_receive_notification,
)
from app.models.outbox import OutboxEmail
from app.models.role import Role
from app.models.settings import get_settings
from app.utils import external_url_for
from tests.conftest import _make_user

BAD_PAYLOAD_LOG = "Missing/bad change_value on outbox row id=None"
MISSING_ASSIGNMENT_LOG = "debriefing outbox row id=None missing assignment_id"
RECIPIENT = "prijemce@example.com"


def _row(notification_type, change_value=None, html_body=None, change_type=None) -> OutboxEmail:
    """Unsaved row (id=None) for direct _row_to_entry calls."""
    return OutboxEmail(
        to_email=RECIPIENT,
        subject="Předmět",
        body="tělo",
        html_body=html_body,
        notification_type=notification_type,
        change_type=change_type,
        change_value=change_value,
    )


@pytest.fixture
def warnings_log(caplog):
    caplog.set_level(logging.WARNING, logger="app.mail")
    return caplog


class TestRowToEntry:
    def test_event_changed_bad_json(self, app, warnings_log):
        row = _row("event_changed", "not json", change_type=_EVENT_CHANGED_CHANGE_TYPE)
        with app.test_request_context("/"):
            entry = _row_to_entry(row)
        assert entry == {"type": "event_changed", "changes": []}
        assert [r.getMessage() for r in warnings_log.records] == ["Bad event_changed payload on outbox row id=None"]

    @pytest.mark.parametrize("ntype", ["assignment_confirmed", "assignment_released"])
    @pytest.mark.parametrize(
        "change_value,description,warned",
        [
            (None, "", False),
            ("", "", False),
            ("garbage", "", True),
            ('{"spot_description": null}', "", False),
            (json.dumps({"spot_description": "Zdravotník"}, ensure_ascii=False), "Zdravotník", False),
        ],
        ids=["none", "empty", "garbage", "null-description", "valid"],
    )
    def test_assignment_payload(self, app, warnings_log, ntype, change_value, description, warned):
        with app.test_request_context("/"):
            entry = _row_to_entry(_row(ntype, change_value))
        assert entry == {"type": ntype, "spot_description": description}
        expected = [f"{BAD_PAYLOAD_LOG} (type={ntype})"] if warned else []
        assert [r.getMessage() for r in warnings_log.records] == expected

    @pytest.mark.parametrize(
        "change_value,count",
        [("garbage", 0), ('{"unfilled_count": "x"}', 0), ('{"unfilled_count": "3"}', 3)],
        ids=["garbage", "non-numeric", "numeric-string"],
    )
    def test_unfilled_reminder_count(self, app, change_value, count):
        with app.test_request_context("/"):
            entry = _row_to_entry(_row("unfilled_reminder", change_value))
        assert entry == {"type": "unfilled_reminder", "unfilled_count": count}

    def test_unfilled_reminder_garbage_warns(self, app, warnings_log):
        with app.test_request_context("/"):
            _row_to_entry(_row("unfilled_reminder", "garbage"))
        assert [r.getMessage() for r in warnings_log.records] == [f"{BAD_PAYLOAD_LOG} (unfilled_reminder)"]

    @pytest.mark.parametrize(
        "change_value,expected_warnings",
        [
            ("garbage", [f"{BAD_PAYLOAD_LOG} (debriefing_invitation)", MISSING_ASSIGNMENT_LOG]),
            ("{}", [MISSING_ASSIGNMENT_LOG]),
        ],
        ids=["garbage", "missing-key"],
    )
    def test_debriefing_missing_id(self, app, warnings_log, change_value, expected_warnings):
        with app.test_request_context("/"):
            entry = _row_to_entry(_row("debriefing_invitation", change_value))
        assert entry == {"type": "debriefing_invitation", "debriefing_url": ""}
        assert [r.getMessage() for r in warnings_log.records] == expected_warnings

    def test_debriefing_valid_id(self, app, warnings_log):
        with app.test_request_context("/"):
            entry = _row_to_entry(_row("debriefing_invitation", json.dumps({"assignment_id": 42})))
            expected_url = external_url_for("debriefing.submit", assignment_id=42)
        assert entry == {"type": "debriefing_invitation", "debriefing_url": expected_url}
        assert expected_url.endswith("/42")
        assert not warnings_log.records

    @pytest.mark.parametrize(
        "html_body,expected",
        [("<p>stará</p>", "<p>stará</p>"), (None, "")],
        ids=["html", "no-html"],
    )
    def test_unknown_type_legacy(self, app, warnings_log, html_body, expected):
        with app.test_request_context("/"):
            entry = _row_to_entry(_row("neznamy_typ", html_body=html_body, change_type="x"))
        assert entry == {"type": "legacy", "legacy_html": expected}
        assert [r.getMessage() for r in warnings_log.records] == [
            "Unknown notification_type='neznamy_typ' change_type='x' for outbox row id=None"
            " — using legacy html_body fallback"
        ]


class TestEnqueue:
    def test_test_notification_override(self, app):
        with app.test_request_context("/"):
            g._test_notification_email = "tester@example.com"
            _enqueue("puvodni@example.com", "Předmět", "Tělo")
            db.session.commit()
        with app.app_context():
            assert db.session.scalars(db.select(OutboxEmail.to_email)).all() == ["tester@example.com"]

    def test_without_override_keeps_recipient(self, app):
        with app.test_request_context("/"):
            _enqueue("puvodni@example.com", "Předmět", "Tělo")
            db.session.commit()
        with app.app_context():
            assert db.session.scalars(db.select(OutboxEmail.to_email)).all() == ["puvodni@example.com"]

    def test_integrity_error_logged_not_raised(self, app, warnings_log):
        with app.test_request_context("/"):
            _enqueue(None, "Předmět", "Tělo")
            db.session.commit()
        assert len(warnings_log.records) == 1
        assert (
            warnings_log.records[0]
            .getMessage()
            .startswith("Failed to enqueue mail to None — IntegrityError, skipping:")
        )
        with app.app_context():
            assert db.session.scalar(db.select(db.func.count(OutboxEmail.id))) == 0


def _raise_settings_error():
    raise RuntimeError("settings down")


class TestGatesFailSafe:
    def test_notify_enabled_fails_open(self, app, monkeypatch):
        with app.app_context():
            get_settings().notify_assignment = False
            db.session.commit()
            assert _is_notify_enabled("notify_assignment") is False
            monkeypatch.setattr(mail_module, "get_settings", _raise_settings_error)
            assert _is_notify_enabled("notify_assignment") is True

    def test_base_context_fallback_org_name(self, app, monkeypatch):
        with app.app_context():
            assert _base_context()["org_name"] == "Test Org"
            monkeypatch.setattr(mail_module, "get_settings", _raise_settings_error)
            ctx = _base_context()
        assert ctx["org_name"] == "MedCover"
        assert ctx["url_for_external"] is external_url_for

    def test_is_test_notification_without_context(self, app):
        assert _is_test_notification() is False
        with app.app_context():
            assert _is_test_notification() is False
            g._test_notification_email = "tester@example.com"
            assert _is_test_notification() is True

    def test_auth_notification_allowed_for_viewer(self, app):
        with app.app_context():
            viewer = _make_user("viewer-gate@medcover.local", "Testovací Divák", Role.VIEWER)
            assert user_can_receive_notification(viewer, "auth") is True
