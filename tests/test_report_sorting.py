"""Report templates expose typed sort values for every sortable column."""

import json
from datetime import datetime, timezone
from decimal import Decimal
from html.parser import HTMLParser
from pathlib import Path
from types import SimpleNamespace

import pytest
from jinja2 import ChoiceLoader, DictLoader, Environment, FileSystemLoader, StrictUndefined

from app.models.event import EventStatus, EventType
from app.routes.reports import UserStats, WorkSummaryGroup, WorkSummaryRow, WorkSummaryTotal


class SortTables(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tables = []
        self.current = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "table":
            self.current = {"headers": [], "rows": [], "attrs": attrs}
            self.tables.append(self.current)
        elif self.current is not None:
            if tag == "th" and "data-col" in attrs:
                self.current["headers"].append(attrs["data-col"])
                assert attrs["aria-sort"] == "none"
            elif tag == "tr" and "data-sv" in attrs:
                self.current["rows"].append(json.loads(attrs["data-sv"]))

    def handle_endtag(self, tag):
        if tag == "table":
            self.current = None


@pytest.mark.parametrize(
    "template,table_count", [("user_report", 1), ("me_report", 2), ("date_range", 2), ("work_summary", 1)]
)
def test_report_sort_values_match_headers(template, table_count):
    env = Environment(
        loader=ChoiceLoader(
            [
                DictLoader({"base.html": "{% block content %}{% endblock %}{% block extra_scripts %}{% endblock %}"}),
                FileSystemLoader(Path(__file__).resolve().parent.parent / "app/templates"),
            ]
        ),
        autoescape=True,
        undefined=StrictUndefined,
    )
    env.filters["localdt"] = lambda value, fmt: value.strftime(fmt)
    env.filters["cznum"] = lambda value: str(value).replace(".", ",")
    now = datetime(2026, 2, 1, tzinfo=timezone.utc)
    user = SimpleNamespace(id="user", name="Čeněk", email="user@test.com", qualifications=[])
    master = SimpleNamespace(id=1, name="Master", coordinator=None)
    event = SimpleNamespace(
        id=1,
        name="2. Akce <test> 'quoted'",
        start_datetime=now,
        status=EventStatus.COMPLETED,
        event_type=EventType.MEDICAL_COVER,
        staffing_mode="SPOTS",
        filled_spots=1,
        total_spots=2,
        actual_hours=Decimal("2.5"),
        post_event_count=3,
    )
    stats = UserStats(shifts_served=1, hours_served=Decimal("2.5"), last_shift=now)
    row = SimpleNamespace(
        event=event,
        planned_hours=Decimal("10"),
        actual_hours=None,
        filled_spots=1,
        total_spots=2,
        worked_hours=Decimal("2.5"),
        patients=3,
    )
    total = WorkSummaryTotal(user_name=user.name, hours_served=Decimal("2.5"))
    work_row = WorkSummaryRow(
        user_name=user.name,
        event_name=event.name,
        start_datetime=now,
        status_label=event.status.value,
        incomplete=False,
        hours_served=Decimal("2.5"),
        hours_planned=Decimal("0"),
        hours_paid=Decimal("0"),
        hours_free=Decimal("2.5"),
    )
    html = env.get_template(f"reports/{template}.html").render(
        report_user=user,
        master_event=master,
        stats=stats,
        rows=[row],
        user_stat_rows=[(user, stats)],
        grand_filled_spots=1,
        grand_total_spots=2,
        grand_worked_hours=Decimal("2.5"),
        grand_patients=3,
        status_counts={},
        date_error=None,
        quick_ranges=[],
        from_date="2026-01-01",
        to_date="2026-03-01",
        results=SimpleNamespace(
            total_events=1,
            filled_spots=1,
            total_spots=2,
            total_worked_hours=Decimal("2.5"),
            total_patients=3,
            status_counts={},
            me_groups=[SimpleNamespace(master_event=master, events=[event])],
            user_stat_rows=[(user, stats)],
        ),
        groups=[WorkSummaryGroup(total=total, rows=[work_row])],
        grand_total=total,
        current_user=SimpleNamespace(has_permission=lambda code: True),
        static_ver="test",
        url_for=lambda endpoint, **kwargs: "/static/" + kwargs["filename"] if endpoint == "static" else "/" + endpoint,
        event_type_row_class=lambda kind: "",
    )
    parser = SortTables()
    parser.feed(html)
    assert len(parser.tables) == table_count
    assert "/static/js/table-sort.js" in html
    for table in parser.tables:
        assert table["attrs"]["id"]
        assert table["attrs"]["data-sort-initial"] == "none"
        assert table["headers"]
        assert table["rows"]
        for values in table["rows"]:
            assert set(table["headers"]) <= values.keys()
            for key in table["headers"]:
                if key not in ("name", "user", "status"):
                    assert values[key] is None or isinstance(values[key], (int, float)), (key, values[key])
