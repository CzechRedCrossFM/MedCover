"""Event index sorting/filters, stale edit commit and archive guards."""

import html
import json
import re
from datetime import datetime, timedelta, timezone

import pytest

from app.constants import RECORD_MODIFIED_MSG
from app.extensions import db
from app.models.event import Event, EventSpot, EventStatus
from app.models.qualification import Qualification
from tests.conftest import (
    _make_event_in_status,
    _make_master_event,
    _post_with_stale_commit,
)
from tests.test_events import ARCHIVED_MSG, _event_form_data
from tests.test_external_events import make_opportunity

ALFA = "Testovací akce Alfa"
BETA = "Testovací akce Beta"
GAMA = "Testovací akce Gama"
START = datetime(2030, 6, 1, 10, 0, tzinfo=timezone.utc)


def _names_in_order(html_text: str, names: list[str]) -> list[str]:
    return sorted(names, key=html_text.index)


def _make_event(app, status: EventStatus, name: str, **kwargs) -> int:
    # A neutral master event name keeps the event name out of the master-event filter dropdown.
    me_id = kwargs.pop("me_id", None) or _make_master_event(app, f"Nadřazená {name[-4:]}")
    return _make_event_in_status(app, status, name, me_id=me_id, **kwargs)


def _add_spots(app, event_id: int, mandatory: int = 0, optional: int = 0) -> list[int]:
    with app.app_context():
        spots = [EventSpot(event_id=event_id, description="Pozice") for _ in range(mandatory)]
        spots += [EventSpot(event_id=event_id, description="Volitelná", is_optional=True) for _ in range(optional)]
        db.session.add_all(spots)
        db.session.commit()
        return [s.id for s in spots]


class TestIndexSorting:
    def test_invalid_sort_falls_back_to_latest_start_first(self, app, admin_client):
        for offset, name in enumerate((ALFA, BETA, GAMA)):
            _make_event(app, EventStatus.PUBLISHED, name, start=START + timedelta(days=offset))

        body = admin_client.get("/events/?sort=zzz&dir=zzz&statuses=PUBLISHED").data.decode()

        assert _names_in_order(body, [ALFA, BETA, GAMA]) == [GAMA, BETA, ALFA]
        assert 'Čas <span class="sort-icon">↓</span>' in body
        assert "sort=zzz" not in body

    def test_sort_by_status_reverses_and_ignores_start_time(self, app, admin_client):
        # Start times put the middle-starting event first alphabetically and by lifecycle,
        # so neither status direction coincides with start order.
        starts = {EventStatus.PUBLISHED: 0, EventStatus.ASSIGNMENTS_OPEN: 1, EventStatus.DRAFT: 2}
        names = {EventStatus.PUBLISHED: ALFA, EventStatus.ASSIGNMENTS_OPEN: BETA, EventStatus.DRAFT: GAMA}
        for status, name in names.items():
            _make_event(app, status, name, start=START + timedelta(days=starts[status]))
        by_start = [names[s] for s in sorted(starts, key=starts.__getitem__)]
        url = "/events/?sort=status&dir={}&statuses=DRAFT,PUBLISHED,ASSIGNMENTS_OPEN"

        asc = _names_in_order(admin_client.get(url.format("asc")).data.decode(), list(names.values()))
        desc = _names_in_order(admin_client.get(url.format("desc")).data.decode(), list(names.values()))

        assert desc == asc[::-1]
        assert asc not in (by_start, by_start[::-1])

    @pytest.mark.parametrize("direction", ["asc", "desc"])
    def test_sort_by_total_counts_only_mandatory_spots(self, app, admin_client, direction):
        # Alfa has the most spots overall, but only optional ones. Start times contradict both directions.
        for name, mandatory, optional, days in ((ALFA, 0, 3, 1), (BETA, 1, 0, 0), (GAMA, 2, 0, 2)):
            event_id = _make_event(app, EventStatus.PUBLISHED, name, start=START + timedelta(days=days))
            _add_spots(app, event_id, mandatory, optional)

        body = admin_client.get(f"/events/?sort=total&dir={direction}&statuses=PUBLISHED").data.decode()

        expected = [ALFA, BETA, GAMA] if direction == "asc" else [GAMA, BETA, ALFA]
        assert _names_in_order(body, [ALFA, BETA, GAMA]) == expected


class TestIndexFilters:
    @pytest.mark.parametrize("query", ["statuses=", "statuses=PUBLISHED&types="], ids=["empty-statuses", "empty-types"])
    def test_empty_filter_lists_nothing(self, app, admin_client, query):
        _make_event(app, EventStatus.PUBLISHED, ALFA)
        assert ALFA in admin_client.get("/events/?statuses=PUBLISHED").data.decode()

        assert ALFA not in admin_client.get(f"/events/?{query}").data.decode()

    def test_active_master_event_filter_lists_only_its_events(self, app, admin_client):
        first_me = _make_master_event(app, "Nadřazená první")
        _make_event(app, EventStatus.PUBLISHED, ALFA, me_id=first_me)
        _make_event(app, EventStatus.PUBLISHED, BETA)

        body = admin_client.get(f"/events/?me_id={first_me}&statuses=PUBLISHED").data.decode()

        assert ALFA in body
        assert BETA not in body

    def test_archived_master_event_filter_is_ignored(self, app, admin_client):
        archived_me = _make_master_event(app, "Archivovaná nadřazená akce", archived=True)
        _make_event(app, EventStatus.PUBLISHED, ALFA, me_id=archived_me)
        _make_event(app, EventStatus.PUBLISHED, BETA)

        body = admin_client.get(f"/events/?me_id={archived_me}&statuses=PUBLISHED").data.decode()

        assert ALFA in body
        assert BETA in body

    def test_external_filter_for_master_event_without_visible_events_is_ignored(self, app, external_client):
        hidden_me = _make_master_event(app, "Nadřazená akce bez viditelných akcí")
        _make_event(app, EventStatus.DRAFT, ALFA, me_id=hidden_me)
        opportunity_id, _ = make_opportunity(app, name=BETA)
        with app.app_context():
            # Keeps the event name out of the master-event filter dropdown, so a match means a listed row.
            db.session.get(Event, opportunity_id).master_event.name = "Nadřazená viditelná akce"
            db.session.commit()

        response = external_client.get(f"/events/?me_id={hidden_me}&statuses=ASSIGNMENTS_OPEN")

        assert response.status_code == 200
        assert BETA in response.data.decode()


class TestEventGuards:
    def test_edit_stale_commit_redirects_with_flash(self, app, admin_client):
        event_id = _make_event(app, EventStatus.DRAFT, ALFA)
        with app.app_context():
            event = db.session.get(Event, event_id)
            me_id, version = event.master_event_id, event.version
        data = {**_event_form_data(me_id, name=BETA), "version": str(version)}

        response = _post_with_stale_commit(admin_client, f"/events/{event_id}/edit", data=data)

        assert response.status_code == 302
        assert response.headers["Location"] == f"/events/{event_id}/edit"
        assert RECORD_MODIFIED_MSG in admin_client.get(response.headers["Location"]).data.decode()
        with app.app_context():
            event = db.session.get(Event, event_id)
            assert (event.name, event.version) == (ALFA, version)

    def _archived_event(self, app) -> tuple[int, int]:
        event_id = _make_event(app, EventStatus.PUBLISHED, ALFA)
        with app.app_context():
            event = db.session.get(Event, event_id)
            event.archived = True
            db.session.commit()
            return event_id, event.version

    def test_archive_already_archived_ajax(self, app, admin_client):
        event_id, version = self._archived_event(app)

        response = admin_client.post(
            f"/events/{event_id}/archive", headers={"X-CSRFToken": "t", "Accept": "application/json"}
        )

        assert response.status_code == 400
        assert response.get_json() == {"ok": False, "error": ARCHIVED_MSG}
        with app.app_context():
            assert db.session.get(Event, event_id).version == version

    def test_detail_with_qualification_cycle(self, app, admin_client):
        event_id = _make_event(app, EventStatus.PUBLISHED, ALFA)
        with app.app_context():
            first, second = Qualification(name="Kvalifikace A"), Qualification(name="Kvalifikace B")
            first.parents, second.parents = [second], [first]
            db.session.add_all([first, second])
            db.session.commit()
            ids = sorted([first.id, second.id])

        response = admin_client.get(f"/events/{event_id}")

        assert response.status_code == 200
        match = re.search(r"data-fillers-map='([^']*)'", response.data.decode())
        assert match
        raw = match.group(1)
        fillers = {key: sorted(value) for key, value in json.loads(html.unescape(raw)).items()}
        assert fillers == {str(ids[0]): ids, str(ids[1]): ids}
