"""Tests for the event form helpers: field validation, spots and equipment plans."""

import uuid
from datetime import datetime, timezone

import pytest
from flask import get_flashed_messages
from werkzeug.datastructures import MultiDict

from app.extensions import db
from app.models.equipment import EquipmentType, EventEquipmentPlan
from app.models.event import Event, EventSpot, EventType
from app.models.qualification import Qualification
from app.models.role import Role
from app.routes.events._helpers import (
    _parse_form_fields,
    _validate_event_fields,
    apply_equipment_plans,
    build_spots,
    copy_equipment,
    parse_equipment_plans_from_form,
    update_spots,
)
from tests.conftest import _make_event_in_status, _make_event_with_spot, _make_rp_qual, _make_user
from tests.test_equipment import _make_type
from tests.test_events import _event_form_data


def _make_deleted_type(app) -> int:
    type_id = _make_type(app, "Testovací zapůjčený stan")
    with app.app_context():
        db.session.delete(db.session.get(EquipmentType, type_id))
        db.session.commit()
    return type_id


def _validate(**overrides):
    form = _event_form_data(1, name="Testovací akce") | overrides
    return _validate_event_fields(_parse_form_fields(MultiDict(form)))


class TestValidateEventFields:
    @pytest.mark.parametrize(
        ("overrides", "expected_error"),
        [
            (
                {"event_type": "TRAINING", "planned_participants_count": "-1"},
                "Plánovaný počet účastníků musí být nezáporné číslo.",
            ),
            (
                {"event_type": "TRAINING", "planned_participants_count": "abc"},
                "Plánovaný počet účastníků musí být celé číslo.",
            ),
            (
                {"event_type": "TRAINING", "planned_participants_count": "1.5"},
                "Plánovaný počet účastníků musí být celé číslo.",
            ),
            ({"name": ""}, "Název akce je povinný."),
            ({"master_event_id": ""}, "Nadřazená akce je povinná."),
            ({"end_datetime": ""}, "Datum a čas začátku i konce jsou povinné."),
            ({"start_datetime": ""}, "Datum a čas začátku i konce jsou povinné."),
            ({"start_datetime": "x"}, "Neplatný formát data a času."),
            ({"end_datetime": "2030-06-01T09:00"}, "Konec akce musí být po začátku."),
            ({"end_datetime": "2030-06-01T10:00"}, "Konec akce musí být po začátku."),
            ({"assignments_open_datetime": "zz"}, "Neplatný formát data otevření přihlášek."),
        ],
        ids=[
            "negative_count",
            "text_count",
            "fractional_count",
            "blank_name",
            "blank_master_event",
            "blank_end",
            "blank_start",
            "bad_start",
            "end_before_start",
            "end_equal_start",
            "bad_assignments_open",
        ],
    )
    def test_errors(self, app, overrides, expected_error):
        with app.app_context():
            result = _validate(**overrides)
        assert result[0] == expected_error
        assert result[2:] == (None, None, None, None)

    def test_viewer_rp_rejected(self, app):
        with app.app_context():
            viewer = _make_user("pozorovatel@medcover.local", "Testovací Pozorovatel", Role.VIEWER)
            error = _validate(responsible_person_id=str(viewer.id))[0]
        assert error == (
            "Uživatel Testovací Pozorovatel má pouze roli Pozorovatel a nemůže být "
            "odpovědnou osobou. Jako OP je potřeba mít roli Člen nebo vyšší."
        )

    def test_member_rp_accepted(self, app):
        with app.app_context():
            member = _make_user("clen@medcover.local", "Testovací Člen", Role.MEMBER)
            assert _validate(responsible_person_id=str(member.id))[0] is None

    def test_unknown_rp_not_rejected_here(self, app):
        with app.app_context():
            assert _validate(responsible_person_id=str(uuid.uuid4()))[0] is None

    def test_non_training_ignores_count(self, app):
        with app.app_context():
            result = _validate(event_type="MEDICAL_COVER", planned_participants_count="-1")
        assert result[:3] == (None, EventType.MEDICAL_COVER, None)

    def test_training_keeps_valid_count(self, app):
        with app.app_context():
            result = _validate(event_type="TRAINING", planned_participants_count="12")
        assert result[:3] == (None, EventType.TRAINING, 12)

    @pytest.mark.parametrize(
        ("local_start", "utc_start", "utc_end"),
        [
            (
                "2030-01-15T10:00",
                datetime(2030, 1, 15, 9, 0, tzinfo=timezone.utc),
                datetime(2030, 1, 15, 17, 0, tzinfo=timezone.utc),
            ),
            (
                "2030-07-15T10:00",
                datetime(2030, 7, 15, 8, 0, tzinfo=timezone.utc),
                datetime(2030, 7, 15, 16, 0, tzinfo=timezone.utc),
            ),
        ],
        ids=["winter", "summer"],
    )
    def test_success_converts_local_to_utc(self, app, local_start, utc_start, utc_end):
        local_end = local_start.replace("10:00", "18:00")
        with app.app_context():
            result = _validate(start_datetime=local_start, end_datetime=local_end)
        assert result == (None, EventType.MEDICAL_COVER, None, utc_start, utc_end, None)


class TestBuildSpots:
    @pytest.mark.parametrize("form", [{}, {"spot_total": "abc"}, {"spot_total": ""}], ids=["missing", "text", "empty"])
    def test_no_total_builds_nothing(self, app, form):
        event_id = _make_event_in_status(app)
        with app.test_request_context("/"):
            build_spots(db.session.get(Event, event_id), MultiDict(form))
            db.session.commit()
            assert db.session.scalars(db.select(EventSpot).where(EventSpot.event_id == event_id)).all() == []

    def test_builds_rows(self, app):
        event_id = _make_event_in_status(app)
        rp_qual_id = _make_rp_qual(app)
        with app.test_request_context("/"):
            deleted = Qualification(name="Smazaná kvalifikace", is_deleted=True)
            db.session.add(deleted)
            db.session.commit()
            form = MultiDict(
                [
                    ("spot_total", "2"),
                    ("spot_desc_0", "  Zdravotník  "),
                    ("spot_optional_0", "1"),
                    ("spot_cred_0", str(rp_qual_id)),
                    ("spot_cred_0", str(deleted.id)),
                    ("spot_cred_0", "x"),
                    ("spot_desc_1", "  "),
                ]
            )
            build_spots(db.session.get(Event, event_id), form)
            db.session.commit()
            spots = db.session.scalars(
                db.select(EventSpot).where(EventSpot.event_id == event_id).order_by(EventSpot.id)
            ).all()
            assert [(s.description, s.is_optional, [q.id for q in s.required_qualifications]) for s in spots] == [
                ("Zdravotník", True, [rp_qual_id]),
                (None, False, []),
            ]


class TestParseEquipmentPlans:
    def test_bad_total(self, app):
        type_id = _make_type(app, "Testovací batoh")
        with app.test_request_context("/"):
            form = MultiDict({"eq_total": "abc", "eq_type_id_0": str(type_id), "eq_qty_0": "2"})
            assert parse_equipment_plans_from_form(form) == []

    def test_skips_blank_and_non_digit(self, app):
        type_id = _make_type(app, "Testovací batoh")
        with app.test_request_context("/"):
            form = MultiDict(
                {
                    "eq_total": "3",
                    "eq_type_id_0": "",
                    "eq_type_id_1": "x",
                    "eq_type_id_2": str(type_id),
                    "eq_qty_2": "4",
                }
            )
            assert parse_equipment_plans_from_form(form) == [(type_id, 4)]
            assert get_flashed_messages() == []

    def test_duplicate_type_keeps_first(self, app):
        type_id = _make_type(app, "Testovací batoh")
        with app.test_request_context("/"):
            form = MultiDict(
                {
                    "eq_total": "2",
                    "eq_type_id_0": str(type_id),
                    "eq_qty_0": "2",
                    "eq_type_id_1": str(type_id),
                    "eq_qty_1": "5",
                }
            )
            assert parse_equipment_plans_from_form(form) == [(type_id, 2)]
            assert get_flashed_messages(with_categories=True) == [
                ("warning", "Typ vybavení „Testovací batoh“ byl zadán vícekrát — zachován první výskyt.")
            ]

    def test_duplicate_unknown_type_uses_id(self, app):
        type_id = _make_deleted_type(app)
        with app.test_request_context("/"):
            form = MultiDict({"eq_total": "2", "eq_type_id_0": str(type_id), "eq_type_id_1": str(type_id)})
            assert parse_equipment_plans_from_form(form) == [(type_id, 1)]
            assert get_flashed_messages(with_categories=True) == [
                ("warning", f"Typ vybavení „{type_id}“ byl zadán vícekrát — zachován první výskyt.")
            ]

    @pytest.mark.parametrize("qty", ["0", "-3"])
    def test_qty_clamped_to_one(self, app, qty):
        type_id = _make_type(app, "Testovací batoh")
        with app.test_request_context("/"):
            form = MultiDict({"eq_total": "1", "eq_type_id_0": str(type_id), "eq_qty_0": qty})
            assert parse_equipment_plans_from_form(form) == [(type_id, 1)]
            assert get_flashed_messages() == []

    def test_qty_unparseable(self, app):
        type_id = _make_type(app, "Testovací batoh")
        with app.test_request_context("/"):
            form = MultiDict({"eq_total": "1", "eq_type_id_0": str(type_id), "eq_qty_0": "x"})
            assert parse_equipment_plans_from_form(form) == [(type_id, 1)]
            assert get_flashed_messages(with_categories=True) == [
                ("warning", "Množství pro typ vybavení „Testovací batoh“ nebylo možné načíst — použita hodnota 1.")
            ]

    def test_qty_unparseable_unknown_type_uses_id(self, app):
        type_id = _make_deleted_type(app)
        with app.test_request_context("/"):
            form = MultiDict({"eq_total": "1", "eq_type_id_0": str(type_id), "eq_qty_0": "x"})
            assert parse_equipment_plans_from_form(form) == [(type_id, 1)]
            assert get_flashed_messages(with_categories=True) == [
                ("warning", f"Množství pro typ vybavení „{type_id}“ nebylo možné načíst — použita hodnota 1.")
            ]


class TestEquipmentPlanHelpers:
    def test_apply_upserts_and_removes(self, app):
        event_id = _make_event_in_status(app)
        type_a = _make_type(app, "Testovací A")
        type_b = _make_type(app, "Testovací B")
        type_c = _make_type(app, "Testovací C")
        with app.app_context():
            db.session.add_all(
                [
                    EventEquipmentPlan(event_id=event_id, equipment_type_id=type_a, quantity_required=1),
                    EventEquipmentPlan(event_id=event_id, equipment_type_id=type_b, quantity_required=1),
                ]
            )
            db.session.commit()
            apply_equipment_plans(db.session.get(Event, event_id), [(type_a, 3), (type_c, 2)])
            db.session.commit()
            rows = db.session.scalars(
                db.select(EventEquipmentPlan).where(EventEquipmentPlan.event_id == event_id)
            ).all()
            assert {(p.equipment_type_id, p.quantity_required) for p in rows} == {(type_a, 3), (type_c, 2)}

    def test_copy_equipment(self, app):
        source_id = _make_event_in_status(app, name="Testovací zdroj")
        target_id = _make_event_in_status(app, name="Testovací cíl")
        type_a = _make_type(app, "Testovací A")
        with app.app_context():
            db.session.add(EventEquipmentPlan(event_id=source_id, equipment_type_id=type_a, quantity_required=4))
            db.session.commit()
            copy_equipment(db.session.get(Event, source_id), db.session.get(Event, target_id))
            db.session.commit()
            rows = db.session.scalars(
                db.select(EventEquipmentPlan).where(EventEquipmentPlan.event_id == target_id)
            ).all()
            assert [(p.equipment_type_id, p.quantity_required) for p in rows] == [(type_a, 4)]

    def test_update_spots_negative_total(self, app):
        event_id, _ = _make_event_with_spot(app)
        with app.app_context(), pytest.raises(ValueError) as excinfo:
            update_spots(db.session.get(Event, event_id), MultiDict({"spot_total": "-1"}))
        assert str(excinfo.value) == "Neplatné údaje pozic. Obnovte formulář a zkuste to znovu."
