"""Tests for scheduler auto-transition functions with retry on concurrent modification."""

import importlib
import logging
import os
import pathlib
import runpy
import sys
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import DEFAULT, MagicMock, call, patch

import pytest
import schedule
import sqlalchemy as sa
from sqlalchemy.orm.exc import StaleDataError

from app.extensions import db
from app.models.audit import AuditLogEntry
from app.models.event import Event, EventStatus
from app.models.master_event import MasterEvent
from app.models.role import Role
from app.models.settings import get_settings
from app.models.user import UserAccount


class _StopLoop(BaseException):
    """Raised from the patched time.sleep to leave the scheduler's endless loop."""


def _make_event(app, status: EventStatus, end_offset_hours: int = -1) -> int:
    """Create a minimal Event with the given status. Returns event id."""
    with app.app_context():
        me = MasterEvent(name="Sched ME")
        db.session.add(me)
        db.session.flush()

        creator_role = db.session.scalar(db.select(Role).where(Role.name == Role.ADMIN))
        creator = UserAccount(email="sched_creator@test.com", name="Sched Creator", is_active=True)
        creator.set_password("testpass123")
        creator.roles = [creator_role]
        db.session.add(creator)
        db.session.flush()

        now = datetime.now(timezone.utc)
        event = Event(
            name="Sched Event",
            master_event_id=me.id,
            start_datetime=now + timedelta(hours=end_offset_hours - 1),
            end_datetime=now + timedelta(hours=end_offset_hours),
            status=status,
            created_by_id=creator.id,
            responsible_person_id=creator.id,
        )
        if status == EventStatus.PUBLISHED:
            event.assignments_open_datetime = now - timedelta(minutes=1)
        db.session.add(event)
        db.session.commit()
        return event.id


@pytest.fixture
def sm(app):
    """Import scheduler.main bound to the test app.

    The module runs ``create_app("production")`` at import time, which would
    bind to ``DATABASE_URL`` instead of this worker's test DB. Patch it and
    reload so a copy imported earlier in the worker is not reused.
    """
    with patch("app.create_app", return_value=app):
        return importlib.reload(importlib.import_module("scheduler.main"))


class TestOpenAssignments:
    def test_transitions_published_event(self, app, sm) -> None:
        event_id = _make_event(app, EventStatus.PUBLISHED)

        sm.open_assignments()

        with app.app_context():
            event = db.session.get(Event, event_id)
            assert event is not None
            assert event.status == EventStatus.ASSIGNMENTS_OPEN
            entry = db.session.scalar(
                sa.select(AuditLogEntry).where(
                    AuditLogEntry.entity_type == "Event",
                    AuditLogEntry.entity_id == str(event_id),
                    AuditLogEntry.action_type == "status_change",
                )
            )
            assert entry is not None
            assert "Přihlašování" in entry.summary

    def test_retries_once_on_stale_version(self, app, sm, caplog) -> None:
        event_id = _make_event(app, EventStatus.PUBLISHED)

        call_count = 0
        original_commit = db.session.commit

        def _flaky_commit():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise StaleDataError("simulated concurrent modification")
            return original_commit()

        with app.app_context(), caplog.at_level(logging.WARNING, logger="scheduler.main"):
            with patch.object(db.session, "commit", side_effect=_flaky_commit):
                sm._apply_event_transition_with_retry(event_id, sm._open_assignments_mutator, "open_assignments")

        assert "retrying" in caplog.text
        with app.app_context():
            event = db.session.get(Event, event_id)
            assert event is not None
            assert event.status == EventStatus.ASSIGNMENTS_OPEN

    def test_skips_after_two_stale_errors(self, app, sm, caplog) -> None:
        event_id = _make_event(app, EventStatus.PUBLISHED)

        def _always_stale():
            raise StaleDataError("simulated concurrent modification")

        with app.app_context(), caplog.at_level(logging.ERROR, logger="scheduler.main"):
            with patch.object(db.session, "commit", side_effect=_always_stale):
                sm._apply_event_transition_with_retry(event_id, sm._open_assignments_mutator, "open_assignments")

        assert "skipping" in caplog.text
        with app.app_context():
            event = db.session.get(Event, event_id)
            assert event is not None
            assert event.status == EventStatus.PUBLISHED


class TestCloseCompletedEvents:
    def test_transitions_open_event_to_completed(self, app, sm) -> None:
        event_id = _make_event(app, EventStatus.ASSIGNMENTS_OPEN, end_offset_hours=-1)

        sm.close_completed_events()

        with app.app_context():
            event = db.session.get(Event, event_id)
            assert event is not None
            assert event.status == EventStatus.COMPLETED

    def test_retries_once_on_stale_version(self, app, sm, caplog) -> None:
        event_id = _make_event(app, EventStatus.ASSIGNMENTS_OPEN, end_offset_hours=-1)

        call_count = 0
        original_commit = db.session.commit

        def _flaky_commit():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise StaleDataError("simulated concurrent modification")
            return original_commit()

        with app.app_context(), caplog.at_level(logging.WARNING, logger="scheduler.main"):
            with patch.object(db.session, "commit", side_effect=_flaky_commit):
                sm._apply_event_transition_with_retry(event_id, sm._close_completed_mutator, "close_completed_events")

        assert "retrying" in caplog.text
        with app.app_context():
            event = db.session.get(Event, event_id)
            assert event is not None
            assert event.status == EventStatus.COMPLETED

    def test_skips_after_two_stale_errors(self, app, sm, caplog) -> None:
        event_id = _make_event(app, EventStatus.ASSIGNMENTS_OPEN, end_offset_hours=-1)

        def _always_stale():
            raise StaleDataError("simulated concurrent modification")

        with (
            app.app_context(),
            caplog.at_level(logging.ERROR, logger="scheduler.main"),
        ):
            with patch.object(db.session, "commit", side_effect=_always_stale):
                sm._apply_event_transition_with_retry(event_id, sm._close_completed_mutator, "close_completed_events")

        assert "skipping" in caplog.text
        with app.app_context():
            event = db.session.get(Event, event_id)
            assert event is not None
            assert event.status == EventStatus.ASSIGNMENTS_OPEN

    def test_skips_missing_event(self, app, sm, caplog) -> None:
        with app.app_context(), caplog.at_level(logging.WARNING, logger="scheduler.main"):
            sm._apply_event_transition_with_retry(999999, sm._close_completed_mutator, "close_completed_events")

        assert "not found" in caplog.text


class TestPredicateRevalidation:
    """After the ID scan and before the mutation, another writer may have changed
    the row so it no longer qualifies for the transition. The retry helper must
    skip such rows instead of blindly applying the mutator."""

    def test_open_skips_cancelled_event(self, app, sm, caplog) -> None:
        event_id = _make_event(app, EventStatus.PUBLISHED)

        # Simulate a concurrent user cancelling the event between id scan and load.
        with app.app_context():
            event = db.session.get(Event, event_id)
            event.status = EventStatus.CANCELLED
            event.version += 1
            db.session.commit()

        with app.app_context(), caplog.at_level(logging.INFO, logger="scheduler.main"):
            sm._apply_event_transition_with_retry(
                event_id,
                sm._open_assignments_mutator,
                "open_assignments",
                predicate=sm._open_assignments_predicate,
            )

        assert "no longer eligible" in caplog.text
        with app.app_context():
            event = db.session.get(Event, event_id)
            assert event is not None
            assert event.status == EventStatus.CANCELLED
            # No audit row for the would-be transition
            entry = db.session.scalar(
                sa.select(AuditLogEntry).where(
                    AuditLogEntry.entity_type == "Event",
                    AuditLogEntry.entity_id == str(event_id),
                    AuditLogEntry.action_type == "status_change",
                )
            )
            assert entry is None

    def test_open_skips_when_open_datetime_moved_to_future(self, app, sm, caplog) -> None:
        event_id = _make_event(app, EventStatus.PUBLISHED)

        with app.app_context():
            event = db.session.get(Event, event_id)
            event.assignments_open_datetime = datetime.now(timezone.utc) + timedelta(hours=2)
            event.version += 1
            db.session.commit()

        with app.app_context(), caplog.at_level(logging.INFO, logger="scheduler.main"):
            sm._apply_event_transition_with_retry(
                event_id,
                sm._open_assignments_mutator,
                "open_assignments",
                predicate=sm._open_assignments_predicate,
            )

        assert "no longer eligible" in caplog.text
        with app.app_context():
            event = db.session.get(Event, event_id)
            assert event is not None
            assert event.status == EventStatus.PUBLISHED

    def test_close_skips_archived_event(self, app, sm, caplog) -> None:
        event_id = _make_event(app, EventStatus.ASSIGNMENTS_OPEN, end_offset_hours=-1)

        with app.app_context():
            event = db.session.get(Event, event_id)
            event.archived = True
            event.version += 1
            db.session.commit()

        with app.app_context(), caplog.at_level(logging.INFO, logger="scheduler.main"):
            sm._apply_event_transition_with_retry(
                event_id,
                sm._close_completed_mutator,
                "close_completed_events",
                predicate=sm._close_completed_predicate,
            )

        assert "no longer eligible" in caplog.text
        with app.app_context():
            event = db.session.get(Event, event_id)
            assert event is not None
            assert event.status == EventStatus.ASSIGNMENTS_OPEN
            assert event.archived is True

    def test_close_skips_when_status_reverted(self, app, sm, caplog) -> None:
        event_id = _make_event(app, EventStatus.ASSIGNMENTS_OPEN, end_offset_hours=-1)

        # Somebody cancelled the event before scheduler's close pass.
        with app.app_context():
            event = db.session.get(Event, event_id)
            event.status = EventStatus.CANCELLED
            event.version += 1
            db.session.commit()

        with app.app_context(), caplog.at_level(logging.INFO, logger="scheduler.main"):
            sm._apply_event_transition_with_retry(
                event_id,
                sm._close_completed_mutator,
                "close_completed_events",
                predicate=sm._close_completed_predicate,
            )

        assert "no longer eligible" in caplog.text
        with app.app_context():
            event = db.session.get(Event, event_id)
            assert event is not None
            assert event.status == EventStatus.CANCELLED


class TestProcessEmailQueueFailure:
    def test_drain_failure_is_logged_not_raised(self, sm, caplog) -> None:
        with (
            patch.object(sm, "drain_batched_outbox", side_effect=RuntimeError("smtp down")),
            patch.object(sm, "drain_one_outbox_email") as legacy,
            patch.object(sm, "get_settings"),
            caplog.at_level(logging.ERROR, logger="scheduler.main"),
        ):
            sm.process_email_queue()

        legacy.assert_not_called()
        (record,) = (r for r in caplog.records if r.name == "scheduler.main")
        assert record.exc_info is not None
        assert str(record.exc_info[1]) == "smtp down"
        assert [(r.levelno, r.getMessage()) for r in caplog.records if r.name == "scheduler.main"] == [
            (logging.ERROR, "process_email_queue: drain failed: smtp down")
        ]


HEARTBEAT_FILE = pathlib.Path("/tmp/scheduler_heartbeat")


def _run_scheduler_main(
    monkeypatch, app, *, sleeps, monotonic, env=None, get_settings_mock=None, touch_error=None
) -> SimpleNamespace:
    """Execute scheduler/main.py as ``__main__`` until the patched sleep raises _StopLoop.

    Returns the touch/run_pending/sleep mocks after the global patches are gone; jobs stay
    registered until the TestMainBlock fixture clears them. Nothing real sleeps or loops.
    """
    for name in ("MAIL_QUEUE_INTERVAL_SECONDS", "INSTANCE_ID"):
        monkeypatch.delenv(name, raising=False)
    for name, value in (env or {}).items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("OTEL_SERVICE_NAME", "medcover-scheduler")
    # The module prepends the repo root to sys.path on every execution.
    monkeypatch.setattr(sys, "path", list(sys.path))
    # Absent from sys.modules, runpy does not warn about re-executing an imported module;
    # monkeypatch puts the original back afterwards.
    monkeypatch.delitem(sys.modules, "scheduler.main", raising=False)
    settings_patch = (
        patch("app.models.settings.get_settings", get_settings_mock) if get_settings_mock is not None else nullcontext()
    )
    with (
        patch("app.create_app", return_value=app),
        patch.object(pathlib.Path, "touch", autospec=True, side_effect=touch_error) as touch,
        patch("schedule.run_pending") as run_pending,
        patch("time.sleep", side_effect=sleeps) as sleep,
        patch("time.monotonic", side_effect=monotonic),
        settings_patch,
        pytest.raises(_StopLoop),
    ):
        runpy.run_module("scheduler.main", run_name="__main__")
    return SimpleNamespace(touch=touch, run_pending=run_pending, sleep=sleep)


class TestMainBlock:
    @pytest.fixture(autouse=True)
    def _clean_schedule(self):
        schedule.clear()
        yield
        schedule.clear()

    def test_registers_jobs_and_writes_heartbeat(self, app, monkeypatch, caplog) -> None:
        with app.app_context():
            assert get_settings().scheduler_last_seen is None
        task_names = [
            "run_send_reminders",
            "run_send_external_opportunities",
            "run_admin_digest",
            "run_scheduled_backup",
            "run_record_metrics",
            "cleanup_work_report_files",
        ]

        with (
            caplog.at_level(logging.DEBUG),
            patch.multiple("app.scheduler_tasks", **{n: DEFAULT for n in task_names}) as tasks,
        ):
            ran = _run_scheduler_main(monkeypatch, app, sleeps=[_StopLoop], monotonic=[10_000.0])
            jobs = [(j.job_func.__name__, j.interval, j.unit) for j in schedule.get_jobs()]
            startup_log = [r.getMessage() for r in caplog.records if r.name == "__main__"]
            caplog.clear()
            for job in schedule.get_jobs():
                if job.job_func.__name__ != "process_email_queue":
                    job.job_func()
            fired_log = [r.getMessage() for r in caplog.records if r.name == "__main__"]

        assert jobs == [
            ("process_email_queue", 3, "seconds"),
            ("open_assignments", 1, "minutes"),
            ("close_completed_events", 1, "minutes"),
            ("send_reminders", 5, "minutes"),
            ("send_external_opportunities", 5, "minutes"),
            ("send_admin_digest_task", 1, "hours"),
            ("scheduled_backup_task", 1, "minutes"),
            ("record_metrics", 15, "minutes"),
            ("cleanup_work_report", 1, "hours"),
        ]
        assert startup_log == [
            f"Scheduler started (instance=? pid={os.getpid()} mail_queue_interval=3s)",
            f"Scheduler alive (instance=? pid={os.getpid()})",
        ]
        assert fired_log == [
            "Task running: open_assignments",
            "Task running: close_completed_events",
            "Task running: send_reminders",
            "Task running: send_external_opportunities",
            "Task running: send_admin_digest",
            "Task running: scheduled_backup",
            "Task running: record_metrics",
            "Task running: cleanup_work_report",
        ]
        for name in task_names[:-1]:
            tasks[name].assert_called_once_with(db.session)
        tasks["cleanup_work_report_files"].assert_called_once_with(app.instance_path)
        ran.run_pending.assert_called_once_with()
        ran.sleep.assert_called_once_with(1)
        ran.touch.assert_called_once_with(HEARTBEAT_FILE)
        with app.app_context():
            seen = get_settings().scheduler_last_seen
        assert seen is not None
        assert abs(datetime.now(timezone.utc) - seen.replace(tzinfo=timezone.utc)) < timedelta(seconds=60)

    def test_env_overrides_interval_and_instance_id(self, app, monkeypatch, caplog) -> None:
        with caplog.at_level(logging.INFO):
            _run_scheduler_main(
                monkeypatch,
                app,
                sleeps=[_StopLoop],
                monotonic=[10_000.0],
                env={"MAIL_QUEUE_INTERVAL_SECONDS": "7", "INSTANCE_ID": "inst-x"},
                get_settings_mock=MagicMock(),
            )

        (mail_job,) = (j for j in schedule.get_jobs() if j.job_func.__name__ == "process_email_queue")
        assert (mail_job.interval, mail_job.unit) == (7, "seconds")
        assert [r.getMessage() for r in caplog.records if r.name == "__main__"] == [
            f"Scheduler started (instance=inst-x pid={os.getpid()} mail_queue_interval=7s)",
            f"Scheduler alive (instance=inst-x pid={os.getpid()})",
        ]

    def test_heartbeat_failures_are_logged_once_per_interval(self, app, monkeypatch, caplog) -> None:
        get_settings_mock = MagicMock(side_effect=RuntimeError("db down"))
        with caplog.at_level(logging.WARNING):
            ran = _run_scheduler_main(
                monkeypatch,
                app,
                sleeps=[None, _StopLoop],
                monotonic=[10_000.0, 10_001.0],
                get_settings_mock=get_settings_mock,
                touch_error=OSError("ro fs"),
            )

        assert [(r.levelno, r.getMessage()) for r in caplog.records if r.name == "__main__"] == [
            (logging.WARNING, "Heartbeat write failed: db down"),
            (logging.WARNING, "Heartbeat file touch failed: ro fs"),
        ]
        assert get_settings_mock.call_count == 1
        ran.touch.assert_called_once_with(HEARTBEAT_FILE)
        assert ran.run_pending.call_count == 2

    @pytest.mark.parametrize(
        ("second_tick", "writes", "alive_logs"),
        [
            (10_004.9, 1, 1),
            (10_005.0, 2, 1),
            (13_599.9, 2, 1),
            (13_600.0, 2, 2),
        ],
        ids=["before-heartbeat-interval", "at-heartbeat-interval", "before-alive-interval", "at-alive-interval"],
    )
    def test_heartbeat_and_alive_intervals(self, app, monkeypatch, caplog, second_tick, writes, alive_logs) -> None:
        get_settings_mock = MagicMock()
        with caplog.at_level(logging.INFO):
            ran = _run_scheduler_main(
                monkeypatch,
                app,
                sleeps=[None, _StopLoop],
                monotonic=[10_000.0, second_tick],
                get_settings_mock=get_settings_mock,
            )

        alive = [r for r in caplog.records if r.name == "__main__" and r.getMessage().startswith("Scheduler alive")]
        assert len(alive) == alive_logs
        assert ran.touch.call_args_list == [call(HEARTBEAT_FILE)] * writes
        assert get_settings_mock.call_count == writes
        assert ran.run_pending.call_count == 2
        assert ran.sleep.call_args_list == [call(1), call(1)]
