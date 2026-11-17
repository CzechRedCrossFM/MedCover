"""Tests for the app-local day helpers."""

from datetime import timedelta

import pytest

from app.extensions import db
from app.models.settings import get_settings
from app.utils import local_day_start_utc, local_day_start_utc_or_none

_INVALID = ["abc", "2026-13-01", "2026-02-30", "15.1.2026", "0001-01-01"]


@pytest.mark.parametrize(
    ("value", "next_day", "expected"),
    [
        ("2026-01-15", False, "2026-01-14T23:00:00+00:00"),
        ("2026-01-15", True, "2026-01-15T23:00:00+00:00"),
        ("2026-07-15", False, "2026-07-14T22:00:00+00:00"),
        # spring-forward day is 23 h long: the next day starts at 22:00 UTC, not 23:00
        ("2026-03-29", False, "2026-03-28T23:00:00+00:00"),
        ("2026-03-29", True, "2026-03-29T22:00:00+00:00"),
        # fall-back day is 25 h long
        ("2026-10-25", False, "2026-10-24T22:00:00+00:00"),
        ("2026-10-25", True, "2026-10-25T23:00:00+00:00"),
        ("2026-12-31", True, "2026-12-31T23:00:00+00:00"),
        ("2024-02-29", True, "2024-02-29T23:00:00+00:00"),
    ],
)
def test_converts_local_day_to_utc_instant(app, value, next_day, expected):
    with app.app_context():
        result = local_day_start_utc(value, next_day=next_day)
        assert result.isoformat() == expected
        assert result.utcoffset() == timedelta(0)
        assert local_day_start_utc_or_none(value, next_day=next_day) == result


@pytest.mark.parametrize(
    ("value", "next_day"),
    [("", False)] + [(v, False) for v in _INVALID] + [("9999-12-31", True)],
)
def test_invalid_input(app, value, next_day):
    with app.app_context():
        with pytest.raises(ValueError):
            local_day_start_utc(value, next_day=next_day)
        assert local_day_start_utc_or_none(value, next_day=next_day) is None


def test_uses_configured_app_timezone(app):
    with app.app_context():
        get_settings().timezone = "UTC"
        db.session.commit()
        assert local_day_start_utc("2026-01-15").isoformat() == "2026-01-15T00:00:00+00:00"
