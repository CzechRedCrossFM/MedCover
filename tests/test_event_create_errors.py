"""Tests for the error paths of event creation and cloning."""

from datetime import datetime, timedelta, timezone

import pytest

from app.extensions import db
from app.models.event import Event, EventStatus, StaffingMode
from app.models.role import ROLE_PERMISSIONS, Role
from tests.conftest import _make_event_in_status, _make_event_with_spot, _make_master_event, _make_rp_qual
from tests.test_events import _event_form_data


def _drop_permission(monkeypatch, role: str, code: str) -> None:
    monkeypatch.setitem(ROLE_PERMISSIONS, role, [c for c in ROLE_PERMISSIONS[role] if c != code])


def _event_count(app) -> int:
    with app.app_context():
        return db.session.scalar(db.select(db.func.count(Event.id)))


class TestCreateErrorPaths:
    def test_non_numeric_template_id_400(self, admin_client):
        assert admin_client.get("/events/create?template_id=abc").status_code == 400

    def test_non_numeric_template_id_in_form_400(self, app, admin_client):
        me_id = _make_master_event(app, "Testovací nadřazená akce")
        data = _event_form_data(me_id, "Testovací akce", _make_rp_qual(app)) | {"template_id": "abc"}
        assert admin_client.post("/events/create", data=data).status_code == 400
        assert _event_count(app) == 0

    def test_clone_without_view_permission_403(self, app, coordinator_client, monkeypatch):
        event_id = _make_event_in_status(app, EventStatus.DRAFT)
        _drop_permission(monkeypatch, Role.COORDINATOR, "event.view_draft")
        assert coordinator_client.get(f"/events/create?clone_event_id={event_id}").status_code == 403

    @pytest.mark.parametrize("code", ["event.publish", "event.assignments.open"])
    def test_quick_publish_requires_permissions(self, app, coordinator_client, monkeypatch, code):
        me_id = _make_master_event(app, "Testovací nadřazená akce")
        _drop_permission(monkeypatch, Role.COORDINATOR, code)
        data = _event_form_data(me_id, "Testovací akce", _make_rp_qual(app)) | {"action": "quick_publish"}
        assert coordinator_client.post("/events/create", data=data).status_code == 403
        assert _event_count(app) == 0

    def test_quick_publish_opens_assignments(self, app, admin_client):
        me_id = _make_master_event(app, "Testovací nadřazená akce")
        data = _event_form_data(me_id, "Testovací akce", _make_rp_qual(app)) | {"action": "quick_publish"}
        before = datetime.now(timezone.utc)
        resp = admin_client.post("/events/create", data=data)
        with app.app_context():
            event = db.session.scalars(db.select(Event)).one()
            assert resp.status_code == 302
            assert resp.headers["Location"] == f"/events/{event.id}"
            assert event.status == EventStatus.ASSIGNMENTS_OPEN
            opened = event.assignments_open_datetime.replace(tzinfo=timezone.utc)
            assert before - timedelta(seconds=1) <= opened <= datetime.now(timezone.utc)
        page = admin_client.get(resp.headers["Location"])
        assert "Akce byla vytvořena a přihlášky okamžitě otevřeny.".encode() in page.data

    def test_clone_spots_event_invalid_spot_config(self, app, admin_client):
        source_id, _ = _make_event_with_spot(app, name="Testovací zdroj")
        me_id = _make_master_event(app, "Testovací nadřazená akce")
        data = _event_form_data(me_id, "Testovací klon") | {"spot_total": "1", "spot_desc_0": "Bez kvalifikace"}
        resp = admin_client.post(f"/events/create?clone_event_id={source_id}", data=data)
        assert resp.status_code == 200
        assert (
            "Alespoň jedna povinná pozice musí vyžadovat kvalifikaci umožňující roli zodpovědné osoby.".encode()
            in resp.data
        )
        assert _event_count(app) == 1

    def test_clone_spots_event_builds_spots(self, app, admin_client):
        source_id, _ = _make_event_with_spot(app, name="Testovací zdroj")
        me_id = _make_master_event(app, "Testovací nadřazená akce")
        rp_qual_id = _make_rp_qual(app)
        data = _event_form_data(me_id, "Testovací klon", rp_qual_id)
        resp = admin_client.post(f"/events/create?clone_event_id={source_id}", data=data)
        assert resp.status_code == 302
        with app.app_context():
            clone = db.session.scalar(db.select(Event).where(Event.name == "Testovací klon"))
            assert resp.headers["Location"] == f"/events/{clone.id}"
            assert clone.staffing_mode == StaffingMode.SPOTS
            assert [(s.description, [q.id for q in s.required_qualifications]) for s in clone.spots] == [
                ("Zdravotník", [rp_qual_id])
            ]
