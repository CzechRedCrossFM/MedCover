"""Tests for scripts/seed_dev.py: relative dates, status/date consistency, idempotency."""

import importlib.util
import random
from collections import Counter
from datetime import date, timedelta
from pathlib import Path

import pytest

from app.extensions import db
from app.models.assignment import Assignment, DebriefingRecord
from app.models.event import Event, EventSpot, EventStatus, EventTemplate, StaffingMode
from app.models.qualification import Qualification
from app.models.role import ROLE_PERMISSIONS
from app.models.settings import AppSettings
from app.models.user import UserAccount
from app.routes.dev import DEV_ACCOUNTS
from app.staffing import evaluate_staffing, validate_condition_plan

_spec = importlib.util.spec_from_file_location("seed_dev", Path(__file__).parent.parent / "scripts" / "seed_dev.py")
assert _spec and _spec.loader
seed_dev = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(seed_dev)

TODAY = date(2026, 10, 28)
ANCHORS = [TODAY + timedelta(days=d) for d in range(7)] + [TODAY + timedelta(days=365)]
FUTURE_STATUSES = {
    EventStatus.DRAFT,
    EventStatus.PUBLISHED,
    EventStatus.ASSIGNMENTS_OPEN,
    EventStatus.ASSIGNMENTS_CLOSED,
}


@pytest.mark.parametrize("today", ANCHORS)
def test_plan_is_relative_to_the_anchor(today):
    plan = seed_dev.plan_events(today, random.Random(seed_dev.SEED))
    anchor = seed_dev._anchor_dt(today)
    past = sum(1 for p in plan if p["start"] < anchor)
    assert 150 <= len(plan) <= 260
    assert 0.15 <= past / len(plan) <= 0.40


@pytest.mark.parametrize("today", ANCHORS)
def test_plan_status_matches_dates(today):
    plan = seed_dev.plan_events(today, random.Random(seed_dev.SEED))
    anchor = seed_dev._anchor_dt(today)
    for p in plan:
        assert p["end"] > p["start"]
        if p["status"] is EventStatus.COMPLETED:
            assert p["end"] <= anchor
        elif p["status"] in FUTURE_STATUSES:
            assert p["start"] > anchor
    assert {p["status"] for p in plan} == set(EventStatus)
    assert sum(p["archived"] for p in plan) == 3


def test_seed_is_idempotent_and_consistent(app):
    with app.app_context():
        settings = db.session.get(AppSettings, 1)
        db.session.delete(settings)
        db.session.commit()

        seed_dev.seed_db(today=TODAY)
        anchor = seed_dev._anchor_dt(TODAY)
        counts = {m: db.session.query(m).count() for m in (UserAccount, Event, Assignment, DebriefingRecord)}
        assert counts[Event] >= 150
        assert counts[Assignment] > 0
        assert counts[DebriefingRecord] > 0

        seed_dev.seed_db(today=TODAY)
        assert counts == {m: db.session.query(m).count() for m in counts}

        assert db.session.get(AppSettings, 1).dev_email_block is True
        events = db.session.scalars(db.select(Event)).all()
        assert Counter(e.status for e in events).keys() == set(EventStatus)
        for e in events:
            if e.status is EventStatus.COMPLETED:
                assert e.end_datetime <= anchor
            elif e.status in FUTURE_STATUSES:
                assert e.start_datetime > anchor
        for a in db.session.scalars(db.select(Assignment)):
            assert a.assigned_at <= anchor
            if a.debriefing:
                assert a.event.status is EventStatus.COMPLETED
                assert a.debriefing.submitted_at <= anchor

        users = db.session.scalars(db.select(UserAccount)).all()
        assert all(u.email.endswith("@medcover.local") for u in users)
        for account in DEV_ACCOUNTS:
            user = db.session.scalar(db.select(UserAccount).where(UserAccount.email == account["email"]))
            assert user and user.check_password("devpassword")
        assert len(users) <= 12
        assert {r.name for u in users for r in u.roles} == set(ROLE_PERMISSIONS)
        held = {q.id for u in users for q in u.qualifications}
        assert held == {q.id for q in db.session.scalars(db.select(Qualification))}
        assert {e.staffing_mode for e in events} == {StaffingMode.CONDITIONS}
        assert db.session.query(EventSpot).count() == 0
        for e in events:
            assert len({a.user_id for a in e.assignments}) == len(e.assignments)
            assert len(e.assignments) <= e.maximum_participants
        synthetic = next(u for u in users if u.email not in {a["email"] for a in DEV_ACCOUNTS})
        assert synthetic.check_password("devpassword")

        templates = db.session.scalars(db.select(EventTemplate)).all()
        assert templates
        for t in templates:
            reqs = [(r.qualification_id, r.minimum_count) for r in t.qualification_requirements]
            validate_condition_plan(t.minimum_participants, t.maximum_participants, reqs)
        for e in events:
            reqs = [(r.qualification_id, r.minimum_count) for r in e.qualification_requirements]
            validate_condition_plan(
                e.minimum_participants, e.maximum_participants, reqs, participant_count=len(e.assignments)
            )
            summary = evaluate_staffing(e)
            if e.responsible_person_id:
                assert summary.rp_valid, e.name
            if e.status in (EventStatus.ASSIGNMENTS_CLOSED, EventStatus.COMPLETED):
                assert summary.is_staffing_sufficient, e.name
