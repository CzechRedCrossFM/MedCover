"""Template filters, globals and the context processor registered in ``app/__init__.py``."""

import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from flask import url_for

import app as app_module
from app.extensions import db
from app.models.assignment import Assignment
from app.models.event import Event, EventSpot
from app.models.settings import get_settings
from tests.conftest import _make_event_with_spot, _make_user
from tests.test_calendar import _assign_user


class TestAuditEntityUrl:
    @pytest.mark.parametrize(
        ("entity_type", "entity_id", "endpoint", "kwargs"),
        [
            ("Event", "5", "events.detail", {"event_id": 5}),
            ("MasterEvent", "3", "master_events.detail", {"me_id": 3}),
            ("EquipmentItem", "2", "equipment.item_edit", {"item_id": 2}),
            ("EquipmentType", "4", "equipment.type_edit", {"type_id": 4}),
            ("EventTemplate", "6", "templates.detail", {"template_id": 6}),
            ("AppSettings", "1", "app_settings.index", {}),
            ("Qualification", "x", "qualifications.index", {}),
            ("RegistrationInvite", "9", "users.invites", {}),
        ],
        ids=[
            "event",
            "master-event",
            "equipment-item",
            "equipment-type",
            "event-template",
            "app-settings",
            "qualification-non-numeric-id",
            "registration-invite",
        ],
    )
    def test_maps_entity_to_page(self, app, entity_type, entity_id, endpoint, kwargs):
        with app.test_request_context("/"):
            result = app.jinja_env.globals["audit_entity_url"](entity_type, entity_id)
            expected = url_for(endpoint, **kwargs)

        assert result == expected

    def test_user_account_maps_to_detail(self, app):
        user_id = uuid.uuid4()
        with app.test_request_context("/"):
            result = app.jinja_env.globals["audit_entity_url"]("UserAccount", str(user_id))

        assert result == f"/users/{user_id}"

    @pytest.mark.parametrize(
        ("entity_type", "entity_id"),
        [("UserAccount", ""), ("Event", "abc"), ("Event", None), ("Nonsense", "1")],
        ids=["user-without-id", "event-non-numeric-id", "event-none-id", "unknown-type"],
    )
    def test_no_page_returns_none(self, app, entity_type, entity_id):
        with app.test_request_context("/"):
            assert app.jinja_env.globals["audit_entity_url"](entity_type, entity_id) is None

    def test_assignment_maps_to_its_event(self, app):
        _make_event_with_spot(app, name="Akce bez přiřazení")
        event_id, spot_id = _make_event_with_spot(app, name="Akce s přiřazením")
        with app.app_context():
            second_spot = EventSpot(event_id=event_id)
            db.session.add(second_spot)
            db.session.commit()
            second_spot_id = second_spot.id
            first = _make_user("prirazeny@medcover.local", "Testovací Rudolf", "Member")
            second = _make_user("druhy@medcover.local", "Vymyšlený Jiří", "Member")
            first_id, second_id = first.id, second.id
        _assign_user(app, first_id, spot_id)
        _assign_user(app, second_id, second_spot_id)
        with app.app_context():
            assignment_ids = db.session.scalars(db.select(Assignment.id)).all()
        assert len(assignment_ids) == 2
        with app.test_request_context("/"):
            results = [app.jinja_env.globals["audit_entity_url"]("Assignment", str(i)) for i in assignment_ids]
            expected = url_for("events.detail", event_id=event_id)

        assert results == [expected, expected]

    def test_assignment_missing_returns_none(self, app):
        _, spot_id = _make_event_with_spot(app, name="Akce se smazaným přiřazením")
        with app.app_context():
            member = _make_user("smazany@medcover.local", "Testovací Rudolf", "Member")
            assignment = Assignment(user_id=member.id, spot_id=spot_id)
            db.session.add(assignment)
            db.session.commit()
            deleted_id = assignment.id
            db.session.delete(assignment)
            db.session.commit()
        with app.test_request_context("/"):
            assert app.jinja_env.globals["audit_entity_url"]("Assignment", str(deleted_id)) is None


class TestTemplateFilters:
    @pytest.fixture(autouse=True)
    def _request_context(self, app):
        with app.test_request_context("/"):
            yield

    def test_localdate_iso_none_is_empty(self, app):
        assert app.jinja_env.filters["localdate_iso"](None) == ""

    def test_localdate_iso_uses_local_day(self, app):
        late_utc = datetime(2030, 6, 1, 22, 30, tzinfo=timezone.utc)

        assert app.jinja_env.filters["localdate_iso"](late_utc) == "2030-06-02"

    def test_localdt_none_is_dash(self, app):
        assert app.jinja_env.filters["localdt"](None) == "—"

    def test_localdt_and_localdate_convert_to_local_time(self, app):
        late_utc = datetime(2030, 6, 1, 22, 30, tzinfo=timezone.utc)

        assert app.jinja_env.filters["localdt"](late_utc) == "02.06.2030 00:30"
        assert app.jinja_env.filters["localdate"](late_utc) == "02.06.2030"

    def test_midpoint_iso_non_event_is_empty(self, app):
        assert app.jinja_env.filters["midpoint_iso"]("not an event") == ""

    @pytest.mark.parametrize("tzinfo", [None, timezone.utc], ids=["naive-as-utc", "aware-utc"])
    def test_midpoint_iso_is_local_midpoint(self, app, tzinfo):
        event = Event(
            start_datetime=datetime(2030, 6, 1, 10, 0, tzinfo=tzinfo),
            end_datetime=datetime(2030, 6, 1, 12, 0, tzinfo=tzinfo),
        )

        assert app.jinja_env.filters["midpoint_iso"](event) == "2030-06-01T13:00"

    def test_czechday_none_is_empty(self, app):
        assert app.jinja_env.filters["czechday"](None) == ""

    def test_czechday_is_local_weekday(self, app):
        monday_late = datetime(2030, 7, 1, 22, 30, tzinfo=timezone.utc)

        assert app.jinja_env.filters["czechday"](monday_late) == "út"

    def test_groupby_safe_groups_none_with_empty_string(self, app):
        items = [
            SimpleNamespace(k="b", v=1),
            SimpleNamespace(k=None, v=2),
            SimpleNamespace(k="a", v=3),
            SimpleNamespace(k="b", v=4),
            SimpleNamespace(k=None, v=5),
        ]

        grouped = app.jinja_env.filters["groupby_safe"](items, "k")

        assert [(key, [i.v for i in group]) for key, group in grouped] == [("", [2, 5]), ("a", [3]), ("b", [1, 4])]

    @pytest.mark.parametrize(
        ("event_type", "kind", "expected"),
        [
            (None, "table", ""),
            (SimpleNamespace(name="TRAINING"), "table", "table-info"),
            (SimpleNamespace(name="TRAINING"), "list", "list-group-item-info"),
            (SimpleNamespace(name="PRESENTATION"), "table", "table-warning"),
            (SimpleNamespace(name="PRESENTATION"), "list", "list-group-item-warning"),
            (SimpleNamespace(name="MEDICAL_COVER"), "table", ""),
        ],
        ids=["none", "training-table", "training-list", "presentation-table", "presentation-list", "medical-cover"],
    )
    def test_event_type_row_class(self, app, event_type, kind, expected):
        assert app.jinja_env.globals["event_type_row_class"](event_type, kind) == expected

    def test_event_type_row_class_defaults_to_table(self, app):
        assert app.jinja_env.globals["event_type_row_class"](SimpleNamespace(name="TRAINING")) == "table-info"


class TestContextProcessor:
    def _context(self, app) -> dict:
        with app.test_request_context("/"):
            ctx: dict = {}
            app.update_template_context(ctx)
            return ctx

    @pytest.mark.parametrize("enabled", [True, False], ids=["enabled", "disabled"])
    def test_feedback_flag_follows_settings(self, app, enabled):
        with app.app_context():
            get_settings().feedback_enabled = enabled
            db.session.commit()

        assert self._context(app)["feedback_enabled"] is enabled

    def test_feedback_flag_defaults_to_enabled_when_settings_unavailable(self, app):
        with app.app_context():
            get_settings().feedback_enabled = False
            db.session.commit()

        with patch("app.get_settings", side_effect=RuntimeError):
            assert self._context(app)["feedback_enabled"] is True

    @pytest.mark.parametrize(
        ("git_commit", "expected"),
        [("abc1234", "abc1234"), ("dev", app_module._STARTUP_TS), ("", app_module._STARTUP_TS)],
        ids=["commit-hash", "dev", "empty"],
    )
    def test_static_version(self, app, monkeypatch, git_commit, expected):
        monkeypatch.setitem(app.config, "GIT_COMMIT", git_commit)

        assert self._context(app)["static_ver"] == expected
