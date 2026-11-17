"""Tests for equipment inventory CRUD and permissions."""

from datetime import datetime, timedelta, timezone

import pytest

from app.constants import RECORD_MODIFIED_MSG
from app.extensions import db
from app.models.audit import AuditLogEntry
from app.models.equipment import (
    EquipmentItem,
    EquipmentType,
    EventEquipmentPlan,
)
from app.models.event import Event, EventStatus
from app.models.master_event import MasterEvent
from app.models.role import Role
from app.models.user import UserAccount
from app.queries import available_quantity_for_type
from tests.conftest import _make_event_in_status, _make_user, _post_with_stale_commit


def _make_type(app, name: str = "Test Type") -> int:
    with app.app_context():
        et = EquipmentType(name=name)
        db.session.add(et)
        db.session.commit()
        return et.id


def _make_item(app, type_id: int, name: str = "Test Item") -> int:
    with app.app_context():
        item = EquipmentItem(name=name, type_id=type_id)
        db.session.add(item)
        db.session.commit()
        return item.id


def _version(app, item_id: int) -> int:
    with app.app_context():
        return db.session.get(EquipmentItem, item_id).version


def _set_window(app, item_id: int, since: datetime, until: datetime | None = None, reason: str | None = None) -> None:
    with app.app_context():
        item = db.session.get(EquipmentItem, item_id)
        item.unavailability_since = since
        item.unavailability_until = until
        item.unavailability_reason = reason
        db.session.commit()


def _plan_future_event(app, type_id: int, name: str, days: float = 5) -> int:
    start = datetime.now(timezone.utc) + timedelta(days=days)
    event_id = _make_event_in_status(app, name=name, start=start, end=start + timedelta(hours=8))
    with app.app_context():
        db.session.add(EventEquipmentPlan(event_id=event_id, equipment_type_id=type_id, quantity_required=1))
        db.session.commit()
    return event_id


def _audits(app, item_id: int) -> list[AuditLogEntry]:
    with app.app_context():
        return list(
            db.session.scalars(
                db.select(AuditLogEntry).where(
                    AuditLogEntry.entity_type == "EquipmentItem", AuditLogEntry.entity_id == str(item_id)
                )
            )
        )


def _assert_item_untouched(app, item_id: int, version: int) -> None:
    with app.app_context():
        item = db.session.get(EquipmentItem, item_id)
        assert item.unavailability_since is None
        assert item.version == version


class TestEquipmentTypeList:
    def test_list_accessible_for_admin(self, admin_client):
        response = admin_client.get("/equipment/")
        assert response.status_code == 200

    def test_list_accessible_for_member(self, member_client):
        response = member_client.get("/equipment/")
        assert response.status_code == 200

    def test_list_requires_login(self, client):
        response = client.get("/equipment/", follow_redirects=False)
        assert response.status_code == 302

    def test_list_shows_type_icon(self, app, admin_client):
        type_id = _make_type(app, "Icon type")
        with app.app_context():
            equipment_type = db.session.get(EquipmentType, type_id)
            equipment_type.icon = "🩺"
            db.session.commit()

        response = admin_client.get("/equipment/")
        assert '<td class="text-center fs-5" title="🩺">🩺</td>' in response.data.decode()


class TestEquipmentTypeCreate:
    def test_create_page_loads_for_admin(self, admin_client):
        response = admin_client.get("/equipment/types/create")
        assert response.status_code == 200

    def test_create_page_forbidden_for_member(self, member_client):
        response = member_client.get("/equipment/types/create")
        assert response.status_code == 403

    def test_admin_can_create_type(self, app, admin_client):
        response = admin_client.post(
            "/equipment/types/create",
            data={"name": "Defibrilátor", "icon": "🩺", "description": ""},
            follow_redirects=False,
        )
        assert response.status_code == 302
        with app.app_context():
            et = db.session.scalar(db.select(EquipmentType).where(EquipmentType.name == "Defibrilátor"))
            assert et is not None
            assert et.icon == "🩺"

    def test_create_type_missing_name(self, admin_client):
        response = admin_client.post(
            "/equipment/types/create",
            data={"name": ""},
            follow_redirects=True,
        )
        assert response.status_code == 200

    def test_member_cannot_create_type(self, member_client):
        response = member_client.post(
            "/equipment/types/create",
            data={"name": "Test"},
        )
        assert response.status_code == 403


class TestEquipmentTypeEdit:
    def test_admin_can_edit_type(self, app, admin_client):
        type_id = _make_type(app, "Old Name")
        response = admin_client.post(
            f"/equipment/types/{type_id}/edit",
            data={"name": "New Name", "icon": "🎒", "version": "1"},
            follow_redirects=False,
        )
        assert response.status_code == 302
        with app.app_context():
            et = db.session.get(EquipmentType, type_id)
            assert et.name == "New Name"
            assert et.icon == "🎒"

    def test_optimistic_lock_conflict(self, app, admin_client):
        type_id = _make_type(app)
        response = admin_client.post(
            f"/equipment/types/{type_id}/edit",
            data={"name": "New Name", "version": "99"},
            follow_redirects=True,
        )
        assert response.status_code == 200
        assert "mezitím změněn" in response.data.decode("utf-8")


class TestEquipmentTypeDelete:
    def test_admin_can_delete_empty_type(self, app, admin_client):
        type_id = _make_type(app)
        response = admin_client.post(
            f"/equipment/types/{type_id}/delete",
            follow_redirects=False,
        )
        assert response.status_code == 302
        with app.app_context():
            assert db.session.get(EquipmentType, type_id) is None

    def test_cannot_delete_type_with_items(self, app, admin_client):
        type_id = _make_type(app)
        _make_item(app, type_id)
        response = admin_client.post(
            f"/equipment/types/{type_id}/delete",
            follow_redirects=True,
        )
        assert response.status_code == 200
        with app.app_context():
            assert db.session.get(EquipmentType, type_id) is not None


class TestEquipmentItemCreate:
    def test_admin_can_create_item(self, app, admin_client):
        type_id = _make_type(app)
        response = admin_client.post(
            "/equipment/items/create",
            data={"name": "AED Unit 1", "type_id": str(type_id), "serial_number": "SN001"},
            follow_redirects=False,
        )
        assert response.status_code == 302
        with app.app_context():
            item = db.session.scalar(db.select(EquipmentItem).where(EquipmentItem.name == "AED Unit 1"))
            assert item is not None
            assert item.serial_number == "SN001"

    def test_member_cannot_create_item(self, app, member_client):
        type_id = _make_type(app)
        response = member_client.post(
            "/equipment/items/create",
            data={"name": "Test", "type_id": str(type_id)},
        )
        assert response.status_code == 403


class TestEquipmentItemIssue:
    def test_admin_can_issue_item(self, app, admin_client):
        type_id = _make_type(app)
        item_id = _make_item(app, type_id)
        # get user id

        with app.app_context():
            user = db.session.scalar(db.select(UserAccount).where(UserAccount.email == "admin@test.com"))
            user_id = str(user.id)
        response = admin_client.post(
            f"/equipment/items/{item_id}/issue",
            data={"user_id": user_id},
            follow_redirects=False,
        )
        assert response.status_code == 302
        with app.app_context():
            item = db.session.get(EquipmentItem, item_id)
            assert item.issued_to_id is not None

    def test_admin_can_return_item(self, app, admin_client):
        type_id = _make_type(app)
        item_id = _make_item(app, type_id)

        with app.app_context():
            user = db.session.scalar(db.select(UserAccount).where(UserAccount.email == "admin@test.com"))
            item = db.session.get(EquipmentItem, item_id)
            item.issued_to_id = user.id
            item.issued_at = datetime.now(timezone.utc)
            db.session.commit()
        response = admin_client.post(
            f"/equipment/items/{item_id}/return",
            follow_redirects=False,
        )
        assert response.status_code == 302
        with app.app_context():
            item = db.session.get(EquipmentItem, item_id)
            assert item.issued_to_id is None


class TestEventEquipmentPlan:
    def test_admin_can_add_plan_entry(self, app, admin_client):
        type_id = _make_type(app)
        _make_item(app, type_id, "Plan Item 1")
        _make_item(app, type_id, "Plan Item 2")
        event_id = _make_event_in_status(app)
        response = admin_client.post(
            f"/events/{event_id}/equipment/plan",
            data={"type_id": str(type_id), "quantity": "2"},
            follow_redirects=False,
        )
        assert response.status_code == 302

        with app.app_context():
            plan = db.session.get(EventEquipmentPlan, (event_id, type_id))
            assert plan is not None
            assert plan.quantity_required == 2

    def test_member_cannot_add_plan_entry(self, app, member_client):
        type_id = _make_type(app)
        event_id = _make_event_in_status(app)
        response = member_client.post(
            f"/events/{event_id}/equipment/plan",
            data={"type_id": str(type_id), "quantity": "1"},
        )
        assert response.status_code == 403


# ── Type create: validation edge cases ───────────────────────────────────────


class TestEquipmentTypeCreateExtended:
    def test_icon_is_required(self, admin_client):
        response = admin_client.post(
            "/equipment/types/create",
            data={"name": "Type without icon"},
            follow_redirects=True,
        )
        assert response.status_code == 200
        assert "Ikona typu vybavení je povinná" in response.data.decode()

    def test_duplicate_name_flashes(self, app, admin_client):
        _make_type(app, "Duplicate Type")
        response = admin_client.post(
            "/equipment/types/create",
            data={"name": "Duplicate Type"},
            follow_redirects=True,
        )
        assert response.status_code == 200
        assert "existuje" in response.data.decode()


# ── Type edit: extended ───────────────────────────────────────────────────────


class TestEquipmentTypeEditExtended:
    def test_get_returns_200(self, app, admin_client):
        type_id = _make_type(app)
        response = admin_client.get(f"/equipment/types/{type_id}/edit")
        assert response.status_code == 200

    def test_404_for_missing_type(self, admin_client):
        response = admin_client.get("/equipment/types/999999/edit")
        assert response.status_code == 404

    def test_edit_duplicate_name_flashes(self, app, admin_client):
        type_id = _make_type(app, "Type A")
        _make_type(app, "Type B")
        with app.app_context():
            et = db.session.get(EquipmentType, type_id)
            version = et.version
        response = admin_client.post(
            f"/equipment/types/{type_id}/edit",
            data={"name": "Type B", "version": str(version)},
            follow_redirects=True,
        )
        assert response.status_code == 200
        assert "existuje" in response.data.decode()

    def test_edit_empty_name_flashes(self, app, admin_client):
        type_id = _make_type(app)
        with app.app_context():
            version = db.session.get(EquipmentType, type_id).version
        response = admin_client.post(
            f"/equipment/types/{type_id}/edit",
            data={"name": "", "version": str(version)},
            follow_redirects=True,
        )
        assert response.status_code == 200
        assert "povinný" in response.data.decode()

    def test_edit_member_forbidden(self, app, member_client):
        type_id = _make_type(app)
        response = member_client.post(f"/equipment/types/{type_id}/edit", data={})
        assert response.status_code == 403

    def test_null_description_renders_empty_not_none(self, app, admin_client):
        """Regression for #84: NULL description must render as '' not 'None'."""
        type_id = _make_type(app)  # description is NULL by default
        response = admin_client.get(f"/equipment/types/{type_id}/edit")
        body = response.data.decode()
        assert "None" not in body

    def test_edit_icon_too_long_flashes(self, app, admin_client):
        type_id = _make_type(app, "Icon Type")
        with app.app_context():
            et = db.session.get(EquipmentType, type_id)
            et.icon = "🩺"
            db.session.commit()
            version = et.version
        response = admin_client.post(
            f"/equipment/types/{type_id}/edit",
            data={"name": "Changed", "icon": "x" * 17, "version": str(version)},
        )
        assert response.status_code == 200
        assert "Ikona typu vybavení je povinná a může mít nejvýše 16 znaků.".encode() in response.data
        with app.app_context():
            et = db.session.get(EquipmentType, type_id)
            assert (et.name, et.icon, et.version) == ("Icon Type", "🩺", version)

    def test_edit_stale_commit(self, app, admin_client):
        type_id = _make_type(app, "Stale Type")
        with app.app_context():
            version = db.session.get(EquipmentType, type_id).version
        response = _post_with_stale_commit(
            admin_client,
            f"/equipment/types/{type_id}/edit",
            data={"name": "Changed", "icon": "🎒", "version": str(version)},
        )
        assert response.status_code == 302
        assert response.headers["Location"] == "/equipment/"
        assert RECORD_MODIFIED_MSG.encode() in admin_client.get(response.headers["Location"]).data
        with app.app_context():
            et = db.session.get(EquipmentType, type_id)
            assert (et.name, et.version) == ("Stale Type", version)


# ── Type delete: extended ─────────────────────────────────────────────────────


class TestEquipmentTypeDeleteExtended:
    def test_delete_404_for_missing(self, admin_client):
        response = admin_client.post("/equipment/types/999999/delete")
        assert response.status_code == 404

    def test_delete_member_forbidden(self, app, member_client):
        type_id = _make_type(app)
        response = member_client.post(f"/equipment/types/{type_id}/delete")
        assert response.status_code == 403


# ── Items list: filters ───────────────────────────────────────────────────────


class TestEquipmentItemsList:
    def test_list_returns_200(self, admin_client):
        response = admin_client.get("/equipment/items/")
        assert response.status_code == 200

    def test_filter_by_type_returns_200(self, app, admin_client):
        type_id = _make_type(app)
        response = admin_client.get(f"/equipment/items/?type_id={type_id}")
        assert response.status_code == 200

    def test_filter_issued_yes(self, admin_client):
        response = admin_client.get("/equipment/items/?issued=yes")
        assert response.status_code == 200

    def test_filter_issued_no(self, admin_client):
        response = admin_client.get("/equipment/items/?issued=no")
        assert response.status_code == 200

    def test_list_member_forbidden(self, member_client):
        """Member has equipment.view, so 200 is correct — test that access works."""
        response = member_client.get("/equipment/items/")
        assert response.status_code == 200

    def test_list_renders_shared_issue_modal_when_permitted(self, admin_client):
        """Admin can issue personal equipment, so the shared 'Vydat' modal should render."""
        body = admin_client.get("/equipment/items/").data.decode()
        assert 'id="issueItemModal"' in body
        assert 'id="issueItemForm"' in body
        assert 'id="issueItemCfg"' in body

    def test_list_hides_shared_issue_modal_without_permission(self, viewer_client):
        """Viewer lacks equipment_item.issue_personal, so the modal must not render."""
        body = viewer_client.get("/equipment/items/").data.decode()
        assert 'id="issueItemModal"' not in body


# ── Item create: extended validation ─────────────────────────────────────────


class TestEquipmentItemCreateExtended:
    def test_get_returns_200(self, admin_client):
        response = admin_client.get("/equipment/items/create")
        assert response.status_code == 200

    def test_empty_name_flashes(self, app, admin_client):
        type_id = _make_type(app)
        response = admin_client.post(
            "/equipment/items/create",
            data={"name": "", "type_id": str(type_id)},
            follow_redirects=True,
        )
        assert response.status_code == 200
        assert "povinný" in response.data.decode()

    def test_missing_type_flashes(self, admin_client):
        response = admin_client.post(
            "/equipment/items/create",
            data={"name": "Item", "type_id": ""},
            follow_redirects=True,
        )
        assert response.status_code == 200
        assert "povinný" in response.data.decode() or "Typ" in response.data.decode()

    def test_invalid_type_flashes(self, admin_client):
        response = admin_client.post(
            "/equipment/items/create",
            data={"name": "Item", "type_id": "999999"},
            follow_redirects=True,
        )
        assert response.status_code == 200
        assert "Neplatný" in response.data.decode() or "typ" in response.data.decode()


# ── Item edit ─────────────────────────────────────────────────────────────────


class TestEquipmentItemEdit:
    def test_get_returns_200(self, app, admin_client):
        type_id = _make_type(app)
        item_id = _make_item(app, type_id)
        response = admin_client.get(f"/equipment/items/{item_id}/edit")
        assert response.status_code == 200

    def test_404_for_missing_item(self, admin_client):
        response = admin_client.get("/equipment/items/999999/edit")
        assert response.status_code == 404

    def test_stale_version_flashes(self, app, admin_client):
        type_id = _make_type(app)
        item_id = _make_item(app, type_id)
        response = admin_client.post(
            f"/equipment/items/{item_id}/edit",
            data={"name": "New", "type_id": str(type_id), "version": "999"},
            follow_redirects=True,
        )
        assert response.status_code == 200
        assert "mezitím" in response.data.decode()

    def test_empty_name_flashes(self, app, admin_client):
        type_id = _make_type(app)
        item_id = _make_item(app, type_id)
        with app.app_context():
            version = db.session.get(EquipmentItem, item_id).version
        response = admin_client.post(
            f"/equipment/items/{item_id}/edit",
            data={"name": "", "type_id": str(type_id), "version": str(version)},
            follow_redirects=True,
        )
        assert response.status_code == 200
        assert "povinný" in response.data.decode()

    def test_successful_edit_redirects(self, app, admin_client):
        type_id = _make_type(app)
        item_id = _make_item(app, type_id)
        with app.app_context():
            version = db.session.get(EquipmentItem, item_id).version
        response = admin_client.post(
            f"/equipment/items/{item_id}/edit",
            data={"name": "Renamed Item", "type_id": str(type_id), "version": str(version)},
            follow_redirects=False,
        )
        assert response.status_code == 302
        with app.app_context():
            assert db.session.get(EquipmentItem, item_id).name == "Renamed Item"

    def test_member_forbidden(self, app, member_client):
        type_id = _make_type(app)
        item_id = _make_item(app, type_id)
        response = member_client.post(f"/equipment/items/{item_id}/edit", data={})
        assert response.status_code == 403

    def test_null_optional_fields_render_empty_not_none(self, app, admin_client):
        """Regression for #84: NULL optional fields must render as '' not 'None'."""
        type_id = _make_type(app)
        item_id = _make_item(app, type_id)  # serial_number/home_location/notes all NULL
        response = admin_client.get(f"/equipment/items/{item_id}/edit")
        body = response.data.decode()
        assert "None" not in body

    def test_missing_type_flashes(self, app, admin_client):
        type_id = _make_type(app)
        item_id = _make_item(app, type_id, "Keep Name")
        version = _version(app, item_id)
        response = admin_client.post(
            f"/equipment/items/{item_id}/edit",
            data={"name": "Changed", "type_id": "", "version": str(version)},
        )
        assert response.status_code == 200
        assert "Typ vybavení je povinný.".encode() in response.data
        with app.app_context():
            item = db.session.get(EquipmentItem, item_id)
            assert (item.name, item.type_id, item.version) == ("Keep Name", type_id, version)

    @pytest.mark.parametrize(
        ("window", "message"),
        [
            ({"unavailability_since": "zz"}, "Neplatný formát data servisního okna."),
            (
                {"unavailability_since": "2030-02-01T10:00", "unavailability_until": "zz"},
                "Neplatný formát data servisního okna.",
            ),
            (
                {"unavailability_since": "2030-02-02T10:00", "unavailability_until": "2030-02-01T10:00"},
                "Konec servisního okna musí být po jeho začátku.",
            ),
        ],
    )
    def test_invalid_window_flashes(self, app, admin_client, window, message):
        type_id = _make_type(app)
        item_id = _make_item(app, type_id, "Keep Name")
        version = _version(app, item_id)
        response = admin_client.post(
            f"/equipment/items/{item_id}/edit",
            data={"name": "Changed", "type_id": str(type_id), "version": str(version), **window},
        )
        assert response.status_code == 200
        assert message.encode() in response.data
        with app.app_context():
            item = db.session.get(EquipmentItem, item_id)
            assert (item.name, item.unavailability_since, item.unavailability_until) == ("Keep Name", None, None)
            assert item.version == version

    def test_stale_commit(self, app, admin_client):
        type_id = _make_type(app)
        item_id = _make_item(app, type_id, "Keep Name")
        version = _version(app, item_id)
        response = _post_with_stale_commit(
            admin_client,
            f"/equipment/items/{item_id}/edit",
            data={"name": "Changed", "type_id": str(type_id), "version": str(version)},
        )
        assert response.status_code == 302
        assert response.headers["Location"] == "/equipment/items/"
        assert RECORD_MODIFIED_MSG.encode() in admin_client.get(response.headers["Location"]).data
        with app.app_context():
            item = db.session.get(EquipmentItem, item_id)
            assert (item.name, item.version) == ("Keep Name", version)


# ── Item delete: extended ─────────────────────────────────────────────────────


class TestEquipmentItemDeleteExtended:
    def test_delete_success(self, app, admin_client):
        type_id = _make_type(app)
        item_id = _make_item(app, type_id)
        response = admin_client.post(f"/equipment/items/{item_id}/delete", follow_redirects=False)
        assert response.status_code == 302
        with app.app_context():
            assert db.session.get(EquipmentItem, item_id) is None

    def test_delete_404_for_missing(self, admin_client):
        response = admin_client.post("/equipment/items/999999/delete")
        assert response.status_code == 404

    def test_delete_issued_item_flashes(self, app, admin_client):

        type_id = _make_type(app)
        item_id = _make_item(app, type_id)
        with app.app_context():
            admin = db.session.scalar(db.select(UserAccount).where(UserAccount.email == "admin@test.com"))
            item = db.session.get(EquipmentItem, item_id)
            item.issued_to_id = admin.id
            item.issued_at = datetime.now(timezone.utc)
            db.session.commit()
        response = admin_client.post(f"/equipment/items/{item_id}/delete", follow_redirects=True)
        assert response.status_code == 200
        assert "vydána" in response.data.decode() or "nelze" in response.data.decode()

    def test_delete_member_forbidden(self, app, member_client):
        type_id = _make_type(app)
        item_id = _make_item(app, type_id)
        response = member_client.post(f"/equipment/items/{item_id}/delete")
        assert response.status_code == 403

    def test_delete_blocked_when_future_event_would_be_short(self, app, admin_client):
        """Deleting the only item of a type must be blocked if a future event plans that type."""

        type_id = _make_type(app, "Del Guard Type")
        item_id = _make_item(app, type_id, "Del Guard Item")
        event_id = _make_event_in_status(app, EventStatus.PUBLISHED)
        future_start = datetime.now(timezone.utc) + timedelta(days=3)
        future_end = future_start + timedelta(hours=8)
        with app.app_context():
            db.session.add(
                EventEquipmentPlan(
                    event_id=event_id,
                    equipment_type_id=type_id,
                    quantity_required=1,
                )
            )
            event = db.session.get(Event, event_id)
            event.start_datetime = future_start
            event.end_datetime = future_end
            db.session.commit()

        resp = admin_client.post(f"/equipment/items/{item_id}/delete", follow_redirects=True)
        assert resp.status_code == 200
        assert "Nelze smazat" in resp.data.decode()
        with app.app_context():
            assert db.session.get(EquipmentItem, item_id) is not None

    @pytest.mark.parametrize(
        ("until_days", "deleted"),
        [(None, True), (1, False), (10, True)],
        ids=["indefinite_maintenance", "maintenance_ends_before_event", "event_inside_maintenance"],
    )
    def test_delete_under_maintenance(self, app, admin_client, until_days, deleted):
        type_id = _make_type(app, "Maint Type")
        item_id = _make_item(app, type_id, "Maint Item")
        _plan_future_event(app, type_id, "Maint Event")
        now = datetime.now(timezone.utc)
        until = now + timedelta(days=until_days) if until_days else None
        _set_window(app, item_id, now - timedelta(hours=1), until)

        resp = admin_client.post(f"/equipment/items/{item_id}/delete", follow_redirects=True)
        body = resp.data.decode()
        with app.app_context():
            exists = db.session.get(EquipmentItem, item_id) is not None
        if deleted:
            assert "Položka vybavení „Maint Item“ byla smazána." in body
            assert "Nelze smazat" not in body
            assert not exists
        else:
            assert "Nelze smazat" in body
            assert "Maint Event" in body
            assert exists


# ── Item issue/return: extended ───────────────────────────────────────────────


class TestEquipmentItemIssueExtended:
    def test_already_issued_flashes(self, app, admin_client):

        type_id = _make_type(app)
        item_id = _make_item(app, type_id)
        with app.app_context():
            admin = db.session.scalar(db.select(UserAccount).where(UserAccount.email == "admin@test.com"))
            item = db.session.get(EquipmentItem, item_id)
            item.issued_to_id = admin.id
            item.issued_at = datetime.now(timezone.utc)
            db.session.commit()
            user_id = str(admin.id)
        response = admin_client.post(
            f"/equipment/items/{item_id}/issue",
            data={"user_id": user_id},
            follow_redirects=True,
        )
        assert response.status_code == 200
        assert "vydána" in response.data.decode() or "již" in response.data.decode()

    def test_take_already_issued_flashes(self, app, member_client):
        type_id = _make_type(app)
        item_id = _make_item(app, type_id, "Taken Item")
        with app.app_context():
            holder = _make_user("holder@medcover.local", "Testovací Držitel", Role.MEMBER)
            item = db.session.get(EquipmentItem, item_id)
            item.issued_to_id = holder.id
            item.issued_at = datetime.now(timezone.utc)
            db.session.commit()
            holder_id = holder.id
        response = member_client.post(f"/equipment/items/{item_id}/take", follow_redirects=True)
        assert "Položka je již vydána.".encode() in response.data
        with app.app_context():
            assert db.session.get(EquipmentItem, item_id).issued_to_id == holder_id

    def test_no_user_id_flashes(self, app, admin_client):
        type_id = _make_type(app)
        item_id = _make_item(app, type_id)
        response = admin_client.post(f"/equipment/items/{item_id}/issue", data={}, follow_redirects=True)
        assert response.status_code == 200
        assert "povinný" in response.data.decode() or "uživatel" in response.data.decode().lower()

    def test_user_not_found_flashes(self, app, admin_client):
        type_id = _make_type(app)
        item_id = _make_item(app, type_id)
        response = admin_client.post(
            f"/equipment/items/{item_id}/issue",
            data={"user_id": "00000000-0000-0000-0000-000000000000"},
            follow_redirects=True,
        )
        assert response.status_code == 200
        assert "nalezen" in response.data.decode() or "uživatel" in response.data.decode().lower()


class TestEquipmentItemReturnExtended:
    def test_not_issued_flashes(self, app, admin_client):
        type_id = _make_type(app)
        item_id = _make_item(app, type_id)
        response = admin_client.post(f"/equipment/items/{item_id}/return", follow_redirects=True)
        assert response.status_code == 200
        assert "vydána" in response.data.decode() or "není" in response.data.decode()


# ── Availability ──────────────────────────────────────────────────────────────


class TestEquipmentItemAvailabilityModel:
    def test_is_available_default(self, app):
        type_id = _make_type(app)
        item_id = _make_item(app, type_id)
        with app.app_context():
            item = db.session.get(EquipmentItem, item_id)
            assert item.is_available is True
            assert item.unavailability_since is None

    def test_is_available_false_when_unavailable(self, app):
        type_id = _make_type(app)
        item_id = _make_item(app, type_id)
        with app.app_context():
            item = db.session.get(EquipmentItem, item_id)
            item.unavailability_since = datetime.now(timezone.utc) - timedelta(minutes=1)
            item.unavailability_reason = "Čeká na opravu"
            db.session.commit()
        with app.app_context():
            item = db.session.get(EquipmentItem, item_id)
            assert item.is_available is False


class TestEquipmentItemAvailabilityEdit:
    def test_set_unavailable_via_edit(self, app, admin_client):
        type_id = _make_type(app)
        item_id = _make_item(app, type_id, name="AED Test")
        with app.app_context():
            item = db.session.get(EquipmentItem, item_id)
            version = item.version

        response = admin_client.post(
            f"/equipment/items/{item_id}/edit",
            data={
                "name": "AED Test",
                "type_id": type_id,
                "version": version,
                "unavailability_reason": "Baterie potřebuje výměnu",
                "unavailability_since": "2030-01-01T10:00",
            },
            follow_redirects=True,
        )
        assert response.status_code == 200
        with app.app_context():
            item = db.session.get(EquipmentItem, item_id)
            assert item.unavailability_since is not None
            assert item.unavailability_reason == "Baterie potřebuje výměnu"

    def test_set_available_clears_reason(self, app, admin_client):
        type_id = _make_type(app)
        item_id = _make_item(app, type_id, name="AED Clr")
        with app.app_context():
            item = db.session.get(EquipmentItem, item_id)
            item.unavailability_since = datetime.now(timezone.utc) - timedelta(minutes=1)
            item.unavailability_reason = "Stará závada"
            db.session.commit()
            version = item.version

        admin_client.post(
            f"/equipment/items/{item_id}/edit",
            data={
                "name": "AED Clr",
                "type_id": type_id,
                "version": version,
            },
            follow_redirects=True,
        )
        with app.app_context():
            item = db.session.get(EquipmentItem, item_id)
            assert item.is_available is True
            assert item.unavailability_reason is None
            assert item.unavailability_since is None


class TestFutureMaintenanceCancellation:
    """Cancelling a future (scheduled) maintenance window via mark-available route."""

    def test_mark_available_clears_future_window(self, app, admin_client):
        """item_mark_available must clear a future window even though is_available=True."""
        type_id = _make_type(app, "Future Cancel Type")
        item_id = _make_item(app, type_id, "Future Cancel Item")
        future = datetime.now(timezone.utc) + timedelta(days=7)
        with app.app_context():
            item = db.session.get(EquipmentItem, item_id)
            item.unavailability_since = future
            item.unavailability_reason = "Plánovaný servis"
            db.session.commit()
            version = item.version
            # Confirm item is still available (future window)
            assert item.is_available is True

        resp = admin_client.post(
            f"/equipment/items/{item_id}/mark-available",
            data={"version": str(version)},
            follow_redirects=True,
        )
        assert resp.status_code == 200
        body = resp.data.decode()
        assert "zrušen" in body
        with app.app_context():
            item = db.session.get(EquipmentItem, item_id)
            assert item.unavailability_since is None
            assert item.unavailability_reason is None
            assert item.is_available is True

    def test_mark_available_without_window_flashes_warning(self, app, admin_client):
        """Posting to mark-available when no maintenance window exists flashes a warning."""
        type_id = _make_type(app, "No Window Type")
        item_id = _make_item(app, type_id, "No Window Item")
        with app.app_context():
            version = db.session.get(EquipmentItem, item_id).version

        resp = admin_client.post(
            f"/equipment/items/{item_id}/mark-available",
            data={"version": str(version)},
            follow_redirects=True,
        )
        assert resp.status_code == 200
        assert "žádný" in resp.data.decode()
        with app.app_context():
            # Item unchanged
            item = db.session.get(EquipmentItem, item_id)
            assert item.unavailability_since is None

    def test_mark_available_stale_form_version(self, app, admin_client):
        type_id = _make_type(app)
        item_id = _make_item(app, type_id)
        past = datetime.now(timezone.utc) - timedelta(hours=1)
        _set_window(app, item_id, past, None, "Oprava")
        version = _version(app, item_id)
        resp = admin_client.post(
            f"/equipment/items/{item_id}/mark-available", data={"version": str(version - 1)}, follow_redirects=True
        )
        assert "Položka byla mezitím změněna jiným uživatelem.".encode() in resp.data
        with app.app_context():
            item = db.session.get(EquipmentItem, item_id)
            assert (item.unavailability_reason, item.version) == ("Oprava", version)
            assert item.is_available is False

    def test_mark_available_active_window(self, app, admin_client):
        type_id = _make_type(app)
        item_id = _make_item(app, type_id, "Back Item")
        _set_window(app, item_id, datetime.now(timezone.utc) - timedelta(hours=1), None, "Oprava")
        version = _version(app, item_id)
        resp = admin_client.post(
            f"/equipment/items/{item_id}/mark-available", data={"version": str(version)}, follow_redirects=True
        )
        assert "Položka „Back Item“ je opět dostupná.".encode() in resp.data
        with app.app_context():
            item = db.session.get(EquipmentItem, item_id)
            assert (item.unavailability_since, item.unavailability_until, item.unavailability_reason) == (None,) * 3
            assert item.version == version + 1
        (entry,) = _audits(app, item_id)
        assert entry.action_type == "edit"
        assert "vrácena na sklad (dostupná)" in entry.summary

    def test_mark_available_stale_commit(self, app, admin_client):
        type_id = _make_type(app)
        item_id = _make_item(app, type_id)
        _set_window(app, item_id, datetime.now(timezone.utc) - timedelta(hours=1), None, "Oprava")
        version = _version(app, item_id)
        resp = _post_with_stale_commit(
            admin_client, f"/equipment/items/{item_id}/mark-available", data={"version": str(version)}
        )
        assert resp.status_code == 302
        assert resp.headers["Location"] == "/equipment/items/"
        assert RECORD_MODIFIED_MSG.encode() in admin_client.get(resp.headers["Location"]).data
        with app.app_context():
            item = db.session.get(EquipmentItem, item_id)
            assert (item.unavailability_reason, item.version) == ("Oprava", version)


class TestItemMarkUnavailable:
    """POST /equipment/items/<id>/mark-unavailable."""

    @staticmethod
    def _url(item_id: int) -> str:
        return f"/equipment/items/{item_id}/mark-unavailable"

    def test_member_forbidden(self, app, member_client):
        item_id = _make_item(app, _make_type(app))
        version = _version(app, item_id)
        assert member_client.post(self._url(item_id), data={"version": str(version)}).status_code == 403
        _assert_item_untouched(app, item_id, version)

    @pytest.mark.parametrize(
        ("since", "until", "since_utc", "until_utc", "dates"),
        [
            (
                "2030-01-15T00:30",
                "2030-01-20T12:00",
                datetime(2030, 1, 14, 23, 30),
                datetime(2030, 1, 20, 11, 0),
                "od 15.01.2030 do 20.01.2030",
            ),
            (
                "2030-07-15T01:30",
                "2030-07-20T12:00",
                datetime(2030, 7, 14, 23, 30),
                datetime(2030, 7, 20, 10, 0),
                "od 15.07.2030 do 20.07.2030",
            ),
        ],
        ids=["winter", "summer"],
    )
    def test_marks_window_and_audits(self, app, admin_client, since, until, since_utc, until_utc, dates):
        item_id = _make_item(app, _make_type(app), "Window Item")
        version = _version(app, item_id)
        resp = admin_client.post(
            self._url(item_id),
            data={"version": str(version), "reason": "Revize", "since": since, "until": until},
        )
        assert resp.status_code == 302
        assert resp.headers["Location"] == "/equipment/items/"
        assert (
            "Položka „Window Item“ byla označena jako nedostupná.".encode()
            in admin_client.get("/equipment/items/").data
        )
        with app.app_context():
            item = db.session.get(EquipmentItem, item_id)
            assert item.unavailability_since.replace(tzinfo=None) == since_utc
            assert item.unavailability_until.replace(tzinfo=None) == until_utc
            assert item.unavailability_reason == "Revize"
            assert item.version == version + 1
        (entry,) = _audits(app, item_id)
        assert entry.action_type == "edit"
        assert "Window Item" in entry.summary
        assert dates in entry.summary
        assert "Revize" in entry.summary

    def test_defaults_to_now_indefinite(self, app, admin_client):
        item_id = _make_item(app, _make_type(app), "Default Item")
        version = _version(app, item_id)
        admin_client.post(self._url(item_id), data={"version": str(version)})
        with app.app_context():
            item = db.session.get(EquipmentItem, item_id)
            now = datetime.now(timezone.utc).replace(tzinfo=None)
            assert abs(item.unavailability_since.replace(tzinfo=None) - now) < timedelta(seconds=60)
            assert item.unavailability_until is None
            assert item.unavailability_reason is None
            assert item.is_available is False
        (entry,) = _audits(app, item_id)
        assert "do neurčito" in entry.summary
        assert entry.summary.endswith(": —")

    def test_stale_form_version(self, app, admin_client):
        item_id = _make_item(app, _make_type(app))
        version = _version(app, item_id)
        resp = admin_client.post(self._url(item_id), data={"version": str(version - 1)}, follow_redirects=True)
        assert "Položka byla mezitím změněna jiným uživatelem.".encode() in resp.data
        _assert_item_untouched(app, item_id, version)

    def test_already_unavailable(self, app, admin_client):
        item_id = _make_item(app, _make_type(app))
        since = datetime.now(timezone.utc) - timedelta(hours=1)
        until = datetime.now(timezone.utc) + timedelta(days=1)
        _set_window(app, item_id, since, until, "Původní")
        version = _version(app, item_id)
        resp = admin_client.post(
            self._url(item_id), data={"version": str(version), "reason": "Nový"}, follow_redirects=True
        )
        assert "Položka je již označena jako nedostupná.".encode() in resp.data
        with app.app_context():
            item = db.session.get(EquipmentItem, item_id)
            assert item.unavailability_since.replace(tzinfo=None) == since.replace(tzinfo=None)
            assert item.unavailability_until.replace(tzinfo=None) == until.replace(tzinfo=None)
            assert (item.unavailability_reason, item.version) == ("Původní", version)

    @pytest.mark.parametrize(
        "window",
        [{"since": "not-a-date"}, {"until": "2030-13-45T99:99"}],
        ids=["bad_since", "bad_until"],
    )
    def test_invalid_dates(self, app, admin_client, window):
        item_id = _make_item(app, _make_type(app))
        version = _version(app, item_id)
        resp = admin_client.post(self._url(item_id), data={"version": str(version), **window}, follow_redirects=True)
        assert "Neplatný formát data servisního okna.".encode() in resp.data
        _assert_item_untouched(app, item_id, version)

    @pytest.mark.parametrize("until", ["2030-03-01T10:00", "2030-02-01T10:00"], ids=["equal", "earlier"])
    def test_until_not_after_since(self, app, admin_client, until):
        item_id = _make_item(app, _make_type(app))
        version = _version(app, item_id)
        resp = admin_client.post(
            self._url(item_id),
            data={"version": str(version), "since": "2030-03-01T10:00", "until": until},
            follow_redirects=True,
        )
        assert "Konec servisního okna musí být po jeho začátku.".encode() in resp.data
        _assert_item_untouched(app, item_id, version)

    def test_new_shortage_warns(self, app, admin_client):
        type_id = _make_type(app)
        item_id = _make_item(app, type_id)
        _plan_future_event(app, type_id, "Short Event")
        resp = admin_client.post(
            self._url(item_id), data={"version": str(_version(app, item_id))}, follow_redirects=True
        )
        body = resp.data.decode()
        assert "Upozornění: servisní okno způsobí nedostatek vybavení pro 1 akci/akce/akcí:" in body
        assert "Short Event" in body

    def test_preexisting_shortage_not_rewarned(self, app, admin_client):
        type_id = _make_type(app)
        item_id = _make_item(app, type_id, "Pre Item")
        _plan_future_event(app, type_id, "Short Event")
        _set_window(app, item_id, datetime.now(timezone.utc) + timedelta(days=4))
        resp = admin_client.post(
            self._url(item_id), data={"version": str(_version(app, item_id))}, follow_redirects=True
        )
        body = resp.data.decode()
        assert "Položka „Pre Item“ byla označena jako nedostupná." in body
        assert "Upozornění" not in body

    def test_stale_commit_rolls_back(self, app, admin_client):
        item_id = _make_item(app, _make_type(app))
        version = _version(app, item_id)
        resp = _post_with_stale_commit(admin_client, self._url(item_id), data={"version": str(version)})
        assert resp.status_code == 302
        assert resp.headers["Location"] == "/equipment/items/"
        assert RECORD_MODIFIED_MSG.encode() in admin_client.get(resp.headers["Location"]).data
        _assert_item_untouched(app, item_id, version)
        assert _audits(app, item_id) == []


# ── Type-level availability check ─────────────────────────────────────────────


class TestAvailableQuantityForType:
    """Unit tests for the available_quantity_for_type helper."""

    def _make_event_with_plan(self, app, type_id, qty, start, end, status=EventStatus.PUBLISHED):
        with app.app_context():
            me = MasterEvent(name=f"AQ ME {start}")
            db.session.add(me)
            db.session.flush()
            event = Event(
                name=f"AQ Event {start}",
                master_event_id=me.id,
                status=status,
                start_datetime=start,
                end_datetime=end,
            )
            db.session.add(event)
            db.session.flush()
            db.session.add(EventEquipmentPlan(event_id=event.id, equipment_type_id=type_id, quantity_required=qty))
            db.session.commit()
            return event.id

    def test_single_available_item_no_overlap(self, app):
        type_id = _make_type(app, "AQ Type A")
        _make_item(app, type_id, "AQ Item 1")
        start = datetime(2034, 1, 1, 10, tzinfo=timezone.utc)
        end = datetime(2034, 1, 1, 18, tzinfo=timezone.utc)
        with app.app_context():
            result = available_quantity_for_type(type_id, start, end)
        assert result == 1

    def test_overlapping_event_reduces_availability(self, app):
        type_id = _make_type(app, "AQ Type B")
        _make_item(app, type_id, "AQ Item B1")
        _make_item(app, type_id, "AQ Item B2")
        s = datetime(2034, 2, 1, 10, tzinfo=timezone.utc)
        e = datetime(2034, 2, 1, 18, tzinfo=timezone.utc)
        self._make_event_with_plan(app, type_id, 1, s, e)
        with app.app_context():
            result = available_quantity_for_type(type_id, s, e)
        assert result == 1  # 2 items - 1 committed

    def test_cancelled_event_not_counted(self, app):
        type_id = _make_type(app, "AQ Type C")
        _make_item(app, type_id, "AQ Item C")
        s = datetime(2034, 3, 1, 10, tzinfo=timezone.utc)
        e = datetime(2034, 3, 1, 18, tzinfo=timezone.utc)
        self._make_event_with_plan(app, type_id, 1, s, e, status=EventStatus.CANCELLED)
        with app.app_context():
            result = available_quantity_for_type(type_id, s, e)
        assert result == 1  # cancelled event doesn't consume the item

    def test_completed_event_not_counted(self, app):
        type_id = _make_type(app, "AQ Type D")
        _make_item(app, type_id, "AQ Item D")
        s = datetime(2034, 4, 1, 10, tzinfo=timezone.utc)
        e = datetime(2034, 4, 1, 18, tzinfo=timezone.utc)
        self._make_event_with_plan(app, type_id, 1, s, e, status=EventStatus.COMPLETED)
        with app.app_context():
            result = available_quantity_for_type(type_id, s, e)
        assert result == 1

    def test_unavailable_item_excluded_from_pool(self, app):
        type_id = _make_type(app, "AQ Type E")
        item_id = _make_item(app, type_id, "AQ Item E")
        with app.app_context():
            item = db.session.get(EquipmentItem, item_id)
            item.unavailability_since = datetime.now(timezone.utc) - timedelta(hours=1)
            db.session.commit()
        s = datetime(2034, 5, 1, 10, tzinfo=timezone.utc)
        e = datetime(2034, 5, 1, 18, tzinfo=timezone.utc)
        with app.app_context():
            result = available_quantity_for_type(type_id, s, e)
        assert result == 0

    def test_issued_item_included_in_pool(self, app, admin_client):
        type_id = _make_type(app, "AQ Type F")
        item_id = _make_item(app, type_id, "AQ Item F")
        with app.app_context():
            u = db.session.scalar(db.select(UserAccount).limit(1))
            item = db.session.get(EquipmentItem, item_id)
            item.issued_to_id = u.id
            db.session.commit()
        s = datetime(2034, 6, 1, 10, tzinfo=timezone.utc)
        e = datetime(2034, 6, 1, 18, tzinfo=timezone.utc)
        with app.app_context():
            result = available_quantity_for_type(type_id, s, e)
        assert result == 1  # issued items are still in the pool (person may bring it to the event)

    def test_exclude_event_id_frees_own_quantity(self, app):
        type_id = _make_type(app, "AQ Type G")
        _make_item(app, type_id, "AQ Item G")
        s = datetime(2034, 7, 1, 10, tzinfo=timezone.utc)
        e = datetime(2034, 7, 1, 18, tzinfo=timezone.utc)
        event_id = self._make_event_with_plan(app, type_id, 1, s, e)
        with app.app_context():
            without_exclude = available_quantity_for_type(type_id, s, e)
            with_exclude = available_quantity_for_type(type_id, s, e, exclude_event_id=event_id)
        assert without_exclude == 0
        assert with_exclude == 1


# ── Plan add rejects when stock is insufficient ───────────────────────────────


class TestEquipmentPlanAvailabilityEnforcement:
    """The plan-add route must reject quantities exceeding available stock."""

    def test_plan_add_rejected_when_no_stock(self, app, admin_client):
        type_id = _make_type(app, "Enf Type A")
        # No items of this type → available = 0
        event_id = _make_event_in_status(app, EventStatus.PUBLISHED)
        resp = admin_client.post(
            f"/events/{event_id}/equipment/plan",
            data={"type_id": str(type_id), "quantity": "1"},
            follow_redirects=True,
        )
        assert resp.status_code == 200
        assert "Nedostatek" in resp.data.decode() or "vybavení" in resp.data.decode()
        with app.app_context():
            plan = db.session.get(EventEquipmentPlan, (event_id, type_id))
            assert plan is None

    def test_plan_add_succeeds_when_stock_sufficient(self, app, admin_client):
        type_id = _make_type(app, "Enf Type B")
        _make_item(app, type_id, "Enf Item B")
        event_id = _make_event_in_status(app, EventStatus.PUBLISHED)
        resp = admin_client.post(
            f"/events/{event_id}/equipment/plan",
            data={"type_id": str(type_id), "quantity": "1"},
            follow_redirects=True,
        )
        assert resp.status_code == 200
        with app.app_context():
            plan = db.session.get(EventEquipmentPlan, (event_id, type_id))
            assert plan is not None
            assert plan.quantity_required == 1

    def test_plan_update_cannot_exceed_total_pool(self, app, admin_client):
        """Updating an existing plan row cannot request more items than exist."""
        type_id = _make_type(app, "Upd Type")
        _make_item(app, type_id, "Upd Item 1")  # only 1 item in pool
        event_id = _make_event_in_status(app, EventStatus.PUBLISHED)
        # First add: qty=1 succeeds (pool = 1, requesting 1)
        admin_client.post(
            f"/events/{event_id}/equipment/plan",
            data={"type_id": str(type_id), "quantity": "1"},
        )
        # Update attempt: qty=2 must be rejected (only 1 item exists)
        resp = admin_client.post(
            f"/events/{event_id}/equipment/plan",
            data={"type_id": str(type_id), "quantity": "2"},
            follow_redirects=True,
        )
        assert "Nedostatek" in resp.data.decode()
        with app.app_context():
            plan = db.session.get(EventEquipmentPlan, (event_id, type_id))
            assert plan.quantity_required == 1  # unchanged


# ── Unavailability warning for future events ──────────────────────────────────


class TestUnavailabilityFutureEventWarning:
    """Marking an item unavailable shows a warning if future events would be short."""

    def test_warning_shown_when_future_event_becomes_short(self, app, admin_client):

        type_id = _make_type(app, "Warn Type A")
        item_id = _make_item(app, type_id, "Warn Item A")
        # One item available; create a future event that needs 1
        future_start = datetime.now(timezone.utc) + timedelta(days=5)
        future_end = future_start + timedelta(hours=8)
        event_id = _make_event_in_status(app)
        with app.app_context():
            db.session.add(
                EventEquipmentPlan(
                    event_id=event_id,
                    equipment_type_id=type_id,
                    quantity_required=1,
                )
            )
            event = db.session.get(Event, event_id)
            event.start_datetime = future_start
            event.end_datetime = future_end
            db.session.commit()

        with app.app_context():
            version = db.session.get(EquipmentItem, item_id).version
        resp = admin_client.post(
            f"/equipment/items/{item_id}/edit",
            data={
                "name": "Warn Item A",
                "type_id": str(type_id),
                "version": str(version),
                "unavailability_reason": "Oprava",
                "unavailability_since": "2026-01-01",
            },
            follow_redirects=True,
        )
        assert resp.status_code == 200
        assert "archivováno" not in resp.data.decode()
        assert "Upozornění" in resp.data.decode() or "nedostatek" in resp.data.decode()

    def test_no_warning_when_enough_stock_remains(self, app, admin_client):

        type_id = _make_type(app, "Warn Type B")
        item1_id = _make_item(app, type_id, "Warn Item B1")
        _make_item(app, type_id, "Warn Item B2")  # second item keeps pool at 1
        future_start = datetime.now(timezone.utc) + timedelta(days=5)
        future_end = future_start + timedelta(hours=8)
        event_id = _make_event_in_status(app)
        with app.app_context():
            db.session.add(
                EventEquipmentPlan(
                    event_id=event_id,
                    equipment_type_id=type_id,
                    quantity_required=1,
                )
            )
            event = db.session.get(Event, event_id)
            event.start_datetime = future_start
            event.end_datetime = future_end
            db.session.commit()

        with app.app_context():
            version = db.session.get(EquipmentItem, item1_id).version
        resp = admin_client.post(
            f"/equipment/items/{item1_id}/edit",
            data={
                "name": "Warn Item B1",
                "type_id": str(type_id),
                "version": str(version),
                "unavailability_reason": "Oprava",
                "unavailability_since": "2026-01-01",
            },
            follow_redirects=True,
        )
        # 2 items → marking 1 unavailable still leaves 1 available → no shortage
        assert resp.status_code == 200
        body = resp.data.decode()
        assert "Upozornění" not in body
        assert "nedostatek" not in body.lower()

    # ── Diff-based warning tests ────────────────────────────────────

    def test_extending_active_window_warns_only_for_newly_covered_events(self, app, admin_client):
        """Case 2: item already in maintenance; edit extends until to cover a new event.
        Warning fires only for the newly covered event, not the one already short."""
        type_id = _make_type(app, "ExtWin Type")
        item_id = _make_item(app, type_id, "ExtWin Item")

        # E1 starts in 5 days — will be inside old AND new window
        # E2 starts in 15 days — will be outside old window (until=+10d) but inside new (until=+20d)
        _plan_future_event(app, type_id, "Warn Event 1", days=5)
        event2_id = _plan_future_event(app, type_id, "Warn Event 2", days=15)

        # Seed old maintenance window: since = yesterday, until = +10 days
        with app.app_context():
            now = datetime.now(timezone.utc)
            item = db.session.get(EquipmentItem, item_id)
            item.unavailability_since = now - timedelta(days=1)
            item.unavailability_until = now + timedelta(days=10)
            db.session.commit()
            version = item.version

        # Edit: extend until to +20 days (now covers E2 too)
        resp = admin_client.post(
            f"/equipment/items/{item_id}/edit",
            data={
                "name": "ExtWin Item",
                "type_id": str(type_id),
                "version": str(version),
                "unavailability_reason": "Oprava",
                "unavailability_since": (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d"),
                "unavailability_until": (datetime.now(timezone.utc) + timedelta(days=20)).strftime("%Y-%m-%d"),
            },
            follow_redirects=True,
        )
        assert resp.status_code == 200
        body = resp.data.decode()
        # A warning must fire (E2 is newly short)
        assert "Upozornění" in body or "nedostatek" in body.lower()

        with app.app_context():
            ev2 = db.session.get(Event, event2_id)
            # The warning should mention E2's name
            assert ev2.name in body

    def test_shortening_window_no_new_warning(self, app, admin_client):
        """Case 6: edit shortens the maintenance window, freeing future events.
        No NEW warning should fire because no new events became short."""
        type_id = _make_type(app, "ShortenWin Type")
        item_id = _make_item(app, type_id, "ShortenWin Item")

        # Two future events — both initially within the window
        _plan_future_event(app, type_id, "Warn Event 1", days=5)
        _plan_future_event(app, type_id, "Warn Event 2", days=15)

        # Seed window covering both events: since=yesterday, until=+20d
        with app.app_context():
            now = datetime.now(timezone.utc)
            item = db.session.get(EquipmentItem, item_id)
            item.unavailability_since = now - timedelta(days=1)
            item.unavailability_until = now + timedelta(days=20)
            db.session.commit()
            version = item.version

        # Edit: shorten until to +10 days (E2 at +15d is now freed)
        resp = admin_client.post(
            f"/equipment/items/{item_id}/edit",
            data={
                "name": "ShortenWin Item",
                "type_id": str(type_id),
                "version": str(version),
                "unavailability_reason": "Oprava",
                "unavailability_since": (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d"),
                "unavailability_until": (datetime.now(timezone.utc) + timedelta(days=10)).strftime("%Y-%m-%d"),
            },
            follow_redirects=True,
        )
        assert resp.status_code == 200
        body = resp.data.decode()
        # No NEW warning — no new events became short
        assert "Upozornění" not in body
        assert "nedostatek" not in body.lower()

    def test_reason_only_edit_no_warning(self, app, admin_client):
        """Case 7: only the reason text changes; no window dates are set.
        No warning should fire."""
        type_id = _make_type(app, "ReasonOnly Type")
        item_id = _make_item(app, type_id, "ReasonOnly Item")
        _plan_future_event(app, type_id, "Warn Event 1")

        with app.app_context():
            version = db.session.get(EquipmentItem, item_id).version

        resp = admin_client.post(
            f"/equipment/items/{item_id}/edit",
            data={
                "name": "ReasonOnly Item",
                "type_id": str(type_id),
                "version": str(version),
                # No unavailability_since / until → window stays cleared
            },
            follow_redirects=True,
        )
        assert resp.status_code == 200
        body = resp.data.decode()
        assert "Upozornění" not in body
        assert "nedostatek" not in body.lower()

    def test_rescheduling_expired_window_to_future_warns(self, app, admin_client):
        """Case 5: expired window is rescheduled to cover a future event.
        Warning should fire because the future event is newly short."""
        type_id = _make_type(app, "Reschedule Type")
        item_id = _make_item(app, type_id, "Reschedule Item")
        _plan_future_event(app, type_id, "Warn Event 1")

        # Seed an already-expired window (both dates in the past)
        with app.app_context():
            now = datetime.now(timezone.utc)
            item = db.session.get(EquipmentItem, item_id)
            item.unavailability_since = now - timedelta(days=60)
            item.unavailability_until = now - timedelta(days=1)
            db.session.commit()
            version = item.version

        # Reschedule to a future window that covers the event
        resp = admin_client.post(
            f"/equipment/items/{item_id}/edit",
            data={
                "name": "Reschedule Item",
                "type_id": str(type_id),
                "version": str(version),
                "unavailability_reason": "Oprava",
                "unavailability_since": (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%d"),
                "unavailability_until": (datetime.now(timezone.utc) + timedelta(days=30)).strftime("%Y-%m-%d"),
            },
            follow_redirects=True,
        )
        assert resp.status_code == 200
        body = resp.data.decode()
        assert "Upozornění" in body or "nedostatek" in body.lower()


# ── Plan-add conflict message includes conflicting event ──────────────────────


class TestPlanAddConflictMessage:
    """equipment_plan_add flash must link to the conflicting event."""

    def test_conflict_flash_contains_conflicting_event_name(self, app, admin_client):
        type_id = _make_type(app, "Conflict Msg Type")
        _make_item(app, type_id, "Conflict Msg Item")

        future_start = datetime.now(timezone.utc) + timedelta(days=3)
        future_end = future_start + timedelta(hours=8)

        # Event A occupies the only item in its window
        event_a_id = _make_event_in_status(app, EventStatus.PUBLISHED, name="Conflict Event A")
        with app.app_context():
            ev_a = db.session.get(Event, event_a_id)
            ev_a.name = "Blocking Event"
            ev_a.start_datetime = future_start
            ev_a.end_datetime = future_end
            db.session.add(EventEquipmentPlan(event_id=event_a_id, equipment_type_id=type_id, quantity_required=1))
            db.session.commit()

        # Event B overlaps — trying to add the same type should fail and name Event A
        event_b_id = _make_event_in_status(app, EventStatus.PUBLISHED, name="Conflict Event B")
        with app.app_context():
            ev_b = db.session.get(Event, event_b_id)
            ev_b.start_datetime = future_start
            ev_b.end_datetime = future_end
            db.session.commit()

        resp = admin_client.post(
            f"/events/{event_b_id}/equipment/plan",
            data={"type_id": str(type_id), "quantity": "1"},
            follow_redirects=True,
        )
        assert resp.status_code == 200
        body = resp.data.decode()
        assert "Nedostatek" in body
        assert "Blocking Event" in body
