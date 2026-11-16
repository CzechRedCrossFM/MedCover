"""Tests for scheduler auto-transition functions with retry on concurrent modification."""

import importlib
import inspect
import logging
import threading
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
import schedule
import sqlalchemy as sa
from sqlalchemy.orm.exc import StaleDataError

from app.extensions import db
from app.models.audit import AuditLogEntry
from app.models.event import Event, EventStatus
from app.models.master_event import MasterEvent
from app.models.role import Role
from app.models.user import UserAccount


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
def sm(app, monkeypatch):
    """Import scheduler.main bound to the test app.

    The module runs ``create_app("production")`` at import time, which would
    bind to ``DATABASE_URL`` instead of this worker's test DB. Patch it and
    reload so a copy imported earlier in the worker is not reused. The module
    sets ``OTEL_SERVICE_NAME`` on import, so it is set through monkeypatch first
    to have it removed again at teardown.
    """
    monkeypatch.setenv("OTEL_SERVICE_NAME", "medcover-scheduler")
    with patch("app.create_app", return_value=app):
        return importlib.reload(importlib.import_module("scheduler.main"))


class _Stop(Exception):
    pass


@pytest.mark.parametrize(
    ("env", "query_timeout", "watchdog_minutes"),
    [
        ({}, 60, 30),
        ({"SCHEDULER_DB_QUERY_TIMEOUT_SECONDS": "7", "SCHEDULER_WATCHDOG_TIMEOUT_MINUTES": "45"}, 7, 45),
        ({"SCHEDULER_DB_QUERY_TIMEOUT_SECONDS": "0", "SCHEDULER_WATCHDOG_TIMEOUT_MINUTES": "0"}, 0, 0),
    ],
)
def test_env_configures_timeouts(app, monkeypatch, env, query_timeout, watchdog_minutes) -> None:
    for name in ("SCHEDULER_DB_QUERY_TIMEOUT_SECONDS", "SCHEDULER_WATCHDOG_TIMEOUT_MINUTES"):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("OTEL_SERVICE_NAME", "medcover-scheduler")
    with patch("app.create_app", return_value=app) as factory:
        module = importlib.reload(importlib.import_module("scheduler.main"))

    factory.assert_called_with("production", query_timeout=query_timeout)
    assert module.SCHEDULER_DB_QUERY_TIMEOUT_SECONDS == query_timeout
    assert module.SCHEDULER_WATCHDOG_TIMEOUT_MINUTES == watchdog_minutes
    assert "scheduler-watchdog" not in [t.name for t in threading.enumerate()]


def test_watchdog_exits_when_loop_stalls(sm, caplog) -> None:
    progress = sm.LoopProgress(last_pass=0.0, current_job="scheduled_backup")
    exit_process = MagicMock()
    calls = MagicMock()
    calls.attach_mock(exit_process, "exit_process")
    with patch.object(sm, "flush_telemetry") as flush:
        calls.attach_mock(flush, "flush")
        sm.run_watchdog(progress, 60, clock=lambda: 61.0, sleep=lambda _s: None, exit_process=exit_process)

    assert [c[0] for c in calls.mock_calls] == ["flush", "exit_process"]
    exit_process.assert_called_once_with(1)
    critical = [r for r in caplog.records if r.levelno == logging.CRITICAL]
    assert len(critical) == 1
    assert "scheduled_backup" in critical[0].getMessage()


@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
def test_watchdog_exits_even_when_the_report_raises(sm) -> None:
    progress = sm.LoopProgress(last_pass=0.0)
    exit_process = MagicMock()
    with patch.object(sm, "flush_telemetry", side_effect=RuntimeError("flush broke")):
        sm.run_watchdog(progress, 60, clock=lambda: 61.0, sleep=lambda _s: None, exit_process=exit_process)
    exit_process.assert_called_once_with(1)


def test_watchdog_exits_even_when_the_report_thread_cannot_start(sm) -> None:
    progress = sm.LoopProgress(last_pass=0.0)
    exit_process = MagicMock()
    with (
        patch.object(sm.threading, "Thread", side_effect=RuntimeError("can't start new thread")),
        pytest.raises(RuntimeError),
    ):
        sm.run_watchdog(progress, 60, clock=lambda: 61.0, sleep=lambda _s: None, exit_process=exit_process)
    exit_process.assert_called_once_with(1)


def test_watchdog_exits_when_the_report_blocks(sm) -> None:
    progress = sm.LoopProgress(last_pass=0.0)
    exit_process = MagicMock()
    release = threading.Event()
    try:
        with patch.object(sm, "flush_telemetry", side_effect=lambda: release.wait(10)):
            sm.run_watchdog(
                progress,
                60,
                clock=lambda: 61.0,
                sleep=lambda _s: None,
                exit_process=exit_process,
                report_timeout_seconds=0.1,
            )
        exit_process.assert_called_once_with(1)
    finally:
        release.set()


def test_watchdog_waits_while_loop_progresses(sm, caplog) -> None:
    now = [0.0]
    progress = sm.LoopProgress(last_pass=0.0)
    exit_process = MagicMock()
    checks = [0]

    def sleep(interval: float) -> None:
        checks[0] += 1
        if checks[0] > 20:
            raise _Stop
        now[0] += interval
        progress.last_pass = now[0] - 50  # a 50 s job under a 60 s timeout

    with pytest.raises(_Stop):
        sm.run_watchdog(progress, 60, 30, clock=lambda: now[0], sleep=sleep, exit_process=exit_process)
    exit_process.assert_not_called()
    assert not [r for r in caplog.records if r.levelno == logging.CRITICAL]


def test_watchdog_ignores_sequential_jobs_that_each_finish_within_the_limit(sm, monkeypatch) -> None:
    now = [1000.0]
    monkeypatch.setattr(sm, "time", SimpleNamespace(monotonic=lambda: now[0]))
    progress = sm.LoopProgress(last_pass=now[0])
    monkeypatch.setattr(sm, "_progress", progress)
    exit_process = MagicMock()

    def check_watchdog() -> None:
        calls = [0]

        def sleep(_s: float) -> None:
            calls[0] += 1
            if calls[0] > 1:
                raise _Stop

        with pytest.raises(_Stop):
            sm.run_watchdog(progress, 30 * 60, clock=lambda: now[0], sleep=sleep, exit_process=exit_process)

    def job() -> None:
        now[0] += 20 * 60  # each job is within the limit; together they are not
        check_watchdog()

    for _ in range(3):
        sm._logged_task("job", job)()
    exit_process.assert_not_called()


def _due(job: schedule.Job) -> None:
    job.next_run = datetime.now() - timedelta(seconds=1)


class TestLoggedTask:
    def test_failing_job_is_rescheduled_and_others_still_run(self, sm) -> None:
        scheduler = schedule.Scheduler()
        failing = MagicMock(side_effect=RuntimeError("boom"), __name__="failing")
        healthy = MagicMock(__name__="healthy")
        failing_job = scheduler.every(1).minutes.do(sm._logged_task("failing", failing))
        healthy_job = scheduler.every(1).minutes.do(sm._logged_task("healthy", healthy))
        _due(failing_job)
        _due(healthy_job)

        scheduler.run_pending()

        healthy.assert_called_once()
        assert failing_job.next_run > datetime.now()
        scheduler.run_pending()
        failing.assert_called_once()

    def test_failure_logging_is_throttled_and_recovery_logged(self, sm, caplog, monkeypatch) -> None:
        clock = [1000.0]
        monkeypatch.setattr(sm, "time", SimpleNamespace(monotonic=lambda: clock[0]))
        steps: list[BaseException | None] = [
            RuntimeError("one"),
            RuntimeError("two"),
            RuntimeError("three"),
            ValueError("four"),
            None,
            None,
        ]
        fn = MagicMock(side_effect=steps, __name__="flaky")
        task = sm._logged_task("flaky", fn)
        caplog.set_level(logging.DEBUG, logger="scheduler.main")

        def run(advance: float = 0.0) -> list[logging.LogRecord]:
            clock[0] += advance
            caplog.clear()
            task()
            return [r for r in caplog.records if r.levelno > logging.DEBUG]

        first = run()
        assert [r.levelno for r in first] == [logging.ERROR]
        assert first[0].exc_info is not None
        assert run(10) == []
        assert [r.levelno for r in run(16 * 60)] == [logging.ERROR]
        type_change = run(10)
        assert [r.levelno for r in type_change] == [logging.ERROR]
        assert type_change[0].exc_info is not None
        recovered = run()
        assert [r.levelno for r in recovered] == [logging.INFO]
        assert "recovered after 4 consecutive" in recovered[0].getMessage()
        assert run() == []

        other = sm._logged_task("other", MagicMock(side_effect=[RuntimeError("x")], __name__="other"))
        caplog.clear()
        other()
        assert [r.levelno for r in caplog.records] == [logging.DEBUG, logging.ERROR]

    @pytest.mark.parametrize("exc", [KeyboardInterrupt, SystemExit])
    def test_base_exceptions_propagate(self, sm, exc) -> None:
        task = sm._logged_task("stop", MagicMock(side_effect=exc, __name__="stop"))
        with pytest.raises(exc):
            task()

    def test_all_jobs_are_registered_through_the_wrapper(self, sm) -> None:
        schedule.clear()
        try:
            sm.register_jobs()
            jobs = list(schedule.get_jobs())
            assert len(jobs) == 9
            unwrapped = []
            for job in jobs:
                wrapper = job.job_func.func
                target = inspect.unwrap(wrapper)
                assert wrapper is not target
                assert inspect.getmodule(target) is sm
                unwrapped.append(target)
            assert sm.process_email_queue in unwrapped
        finally:
            schedule.clear()

    def test_email_queue_settings_failure_is_contained(self, sm, caplog) -> None:
        task = sm._logged_task("process_email_queue", sm.process_email_queue)
        with patch.object(sm, "get_settings", side_effect=RuntimeError("settings down")):
            task()
        errors = [r for r in caplog.records if r.levelno == logging.ERROR]
        assert len(errors) == 1
        assert errors[0].exc_info is not None


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
