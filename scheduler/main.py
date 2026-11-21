import functools
import logging
import os
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import schedule
from sqlalchemy.orm.exc import StaleDataError

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app, flush_telemetry
from app.extensions import db
from app.mail import drain_batched_outbox, drain_one_outbox_email
from app.models.audit import AuditLogEntry
from app.models.event import Event, EventStatus
from app.models.settings import get_settings
from app.scheduler_tasks import (
    cleanup_work_report_files,
    run_admin_digest,
    run_record_metrics,
    run_scheduled_backup,
    run_send_external_opportunities,
    run_send_reminders,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# Cloud role name in App Insights; the web app defaults to "medcover-web".
os.environ.setdefault("OTEL_SERVICE_NAME", "medcover-scheduler")

# Per-statement DB timeout for the scheduler (0 = none). A hung query fails with
# HYT00 instead of freezing the single-threaded loop; the backup job is exempt.
SCHEDULER_DB_QUERY_TIMEOUT_SECONDS: int = int(os.environ.get("SCHEDULER_DB_QUERY_TIMEOUT_SECONDS", "60"))
# The watchdog ends the process when the main loop has made no progress for this
# long (0 = no watchdog); it must exceed the longest legitimate job (the nightly backup).
SCHEDULER_WATCHDOG_TIMEOUT_MINUTES: int = int(os.environ.get("SCHEDULER_WATCHDOG_TIMEOUT_MINUTES", "30"))
WATCHDOG_CHECK_INTERVAL_SECONDS: int = 30
# A stall report blocked on a logging handler lock must not keep the process alive.
WATCHDOG_REPORT_TIMEOUT_SECONDS: int = 10
if SCHEDULER_DB_QUERY_TIMEOUT_SECONDS < 0 or SCHEDULER_WATCHDOG_TIMEOUT_MINUTES < 0:
    raise ValueError("SCHEDULER_DB_QUERY_TIMEOUT_SECONDS and SCHEDULER_WATCHDOG_TIMEOUT_MINUTES must be >= 0")
app = create_app("production", query_timeout=SCHEDULER_DB_QUERY_TIMEOUT_SECONDS)

# Sentinel user ID used in audit log rows written by the scheduler (no human actor)
SCHEDULER_ACTOR_ID = None

INSTANCE_ID: str = os.environ.get("INSTANCE_ID", "")

# How long to wait between individual SMTP sends (seconds).
# 3 s ≈ 20 emails/min — safely under typical relay limits (e.g. MS365: 30/min).
MAIL_QUEUE_INTERVAL_SECONDS: int = int(os.environ.get("MAIL_QUEUE_INTERVAL_SECONDS", "3"))

# How often to write the scheduler heartbeat (DB row + local file), in seconds.
# Kept independent of the main loop's poll interval (below) so that polling more
# often to honor short schedule.every(...) intervals doesn't multiply DB writes.
HEARTBEAT_INTERVAL_SECONDS: int = 5

# A job that keeps failing logs its full traceback at most this often (or when the
# exception type changes), so a 3 s job cannot flood the exception log.
FAILURE_TRACEBACK_INTERVAL_SECONDS: int = 15 * 60


@dataclass
class LoopProgress:
    """Main-loop progress shared with the watchdog thread (single attribute writes only)."""

    last_pass: float = field(default_factory=time.monotonic)
    current_job: str | None = None


_progress = LoopProgress()


def _logged_task(name: str, fn: Callable[[], None]) -> Callable[[], None]:
    """Wrap a scheduled task: DEBUG log on each run, and contain its failures.

    DEBUG, not INFO: tasks fire every minute whether or not there is work, and
    INFO is reserved for lines where something actually happened.

    An exception is logged (throttled, see FAILURE_TRACEBACK_INTERVAL_SECONDS) instead of
    propagating, so one failing job neither ends the process nor skips other jobs,
    and ``schedule`` still advances the job to its next regular run.
    """
    failures = 0
    last_type: type[Exception] | None = None
    last_traceback = 0.0

    @functools.wraps(fn)
    def _wrapper() -> None:
        nonlocal failures, last_type, last_traceback
        log.debug("Task running: %s", name)
        _progress.current_job = name
        try:
            fn()
        except Exception as exc:
            failures += 1
            now = time.monotonic()
            if (
                failures == 1
                or type(exc) is not last_type
                or now - last_traceback >= FAILURE_TRACEBACK_INTERVAL_SECONDS
            ):
                log.exception("Task %s failed (%d consecutive failure(s))", name, failures)
                last_traceback = now
            last_type = type(exc)
            return
        finally:
            _progress.current_job = None
            _progress.last_pass = time.monotonic()
        if failures:
            log.info("Task %s recovered after %d consecutive failure(s)", name, failures)
            failures = 0
            last_type = None

    return _wrapper


def process_email_queue() -> None:
    """Drain the outbox_email queue.

    Priority:
      1. drain_batched_outbox() — one batched email per triggering recipient.
      2. Fall through to drain_one_outbox_email() for user_id=NULL legacy rows.

    Called every MAIL_QUEUE_INTERVAL_SECONDS (default 3 s).  At most one
    SMTP send per tick — the batched drain's early return prevents double sending.
    Errors propagate to the ``_logged_task`` wrapper.
    """
    with app.app_context():
        s = get_settings()
        s.apply_to_app(app)
        # drain_batched_outbox calls external_url_for which needs a request context
        # when SERVER_NAME is not set in ProductionConfig.
        with app.test_request_context("/"):
            if not drain_batched_outbox():
                drain_one_outbox_email()


def _apply_event_transition_with_retry(
    event_id: int,
    mutator: Callable[[Event], None],
    action_desc: str,
    predicate: Callable[[Event], bool] | None = None,
) -> None:
    """Apply a status transition to one Event, retrying once on concurrent modification.

    ``predicate`` is re-checked against the freshly-loaded row before every
    mutation attempt (including after a rollback + reload on retry). If it
    returns False, the transition is skipped — protects against acting on a
    row whose state changed between the initial id scan and the load
    (e.g. an admin cancelled or archived the event, or shifted its transition
    time into the future).
    """
    for attempt in range(2):
        try:
            event = db.session.get(Event, event_id)
            if event is None:
                log.warning(
                    "_apply_event_transition_with_retry: event id=%s not found, skipping",
                    event_id,
                )
                return
            if predicate is not None and not predicate(event):
                log.info(
                    "Event id=%s (%s) no longer eligible after reload, skipping",
                    event_id,
                    action_desc,
                )
                return
            mutator(event)
            db.session.commit()
            return
        except StaleDataError:
            db.session.rollback()
            if attempt == 0:
                log.warning(
                    "Concurrent modification for event id=%s (%s), retrying",
                    event_id,
                    action_desc,
                )
            else:
                log.error(
                    "Concurrent modification for event id=%s (%s) on retry, skipping",
                    event_id,
                    action_desc,
                )


def _open_assignments_predicate(event: Event) -> bool:
    now = datetime.now(timezone.utc)
    return (
        event.status == EventStatus.PUBLISHED
        and event.assignments_open_datetime is not None
        and event.assignments_open_datetime <= now
    )


def _open_assignments_mutator(event: Event) -> None:
    event.status = EventStatus.ASSIGNMENTS_OPEN
    event.version += 1
    db.session.add(
        AuditLogEntry(
            actor_id=SCHEDULER_ACTOR_ID,
            action_type="status_change",
            entity_type="Event",
            entity_id=str(event.id),
            summary=f"[Scheduler] Přihlašování automaticky otevřeno pro akci '{event.name}'",
        )
    )
    log.info("Opened assignments for event id=%s name=%r", event.id, event.name)


def _close_completed_predicate(event: Event) -> bool:
    now = datetime.now(timezone.utc)
    return (
        event.status in (EventStatus.ASSIGNMENTS_OPEN, EventStatus.ASSIGNMENTS_CLOSED)
        and event.end_datetime <= now
        and not event.archived
    )


def _close_completed_mutator(event: Event) -> None:
    event.status = EventStatus.COMPLETED
    event.version += 1
    db.session.add(
        AuditLogEntry(
            actor_id=SCHEDULER_ACTOR_ID,
            action_type="status_change",
            entity_type="Event",
            entity_id=str(event.id),
            summary=f"[Scheduler] Akce '{event.name}' automaticky dokončena po skončení termínu",
        )
    )
    log.info("Completed event id=%s name=%r", event.id, event.name)


def open_assignments() -> None:
    """Auto-transition Events from Published → Assignments Open when assignments_open_datetime has passed."""
    with app.app_context():
        now = datetime.now(timezone.utc)
        event_ids = db.session.scalars(
            db.select(Event.id).where(
                Event.status == EventStatus.PUBLISHED,
                Event.assignments_open_datetime != None,  # noqa: E711
                Event.assignments_open_datetime <= now,
            )
        ).all()

        for event_id in event_ids:
            _apply_event_transition_with_retry(
                event_id,
                _open_assignments_mutator,
                "open_assignments",
                predicate=_open_assignments_predicate,
            )

        if event_ids:
            log.info("open_assignments: processed %d event(s)", len(event_ids))


def close_completed_events() -> None:
    """Auto-transition Events from Assignments Open/Closed → Completed after end_datetime."""
    with app.app_context():
        now = datetime.now(timezone.utc)
        event_ids = db.session.scalars(
            db.select(Event.id).where(
                Event.status.in_([EventStatus.ASSIGNMENTS_OPEN, EventStatus.ASSIGNMENTS_CLOSED]),
                Event.end_datetime <= now,
                Event.archived == False,  # noqa: E712
            )
        ).all()

        for event_id in event_ids:
            _apply_event_transition_with_retry(
                event_id,
                _close_completed_mutator,
                "close_completed_events",
                predicate=_close_completed_predicate,
            )

        if event_ids:
            log.info("close_completed_events: processed %d event(s)", len(event_ids))


def send_reminders() -> None:
    """Send unfilled-spot reminder emails for events whose reminder window has arrived.

    Delegates to app.scheduler_tasks.run_send_reminders for the core logic so
    that it can be tested without importing this module.
    """
    with app.app_context():
        run_send_reminders(db.session)


def send_external_opportunities() -> None:
    with app.app_context():
        run_send_external_opportunities(db.session)


def send_admin_digest_task() -> None:
    """Send admin digest if it is due per DigestSchedule."""
    with app.app_context():
        run_admin_digest(db.session)


def scheduled_backup_task() -> None:
    """Create a daily backup if backup_schedule_enabled is True in AppSettings."""
    with app.app_context():
        run_scheduled_backup(db.session)


def record_metrics() -> None:
    """Record outbox queue depth snapshot every 15 minutes."""
    with app.app_context():
        run_record_metrics(db.session)


def cleanup_work_report() -> None:
    """Remove employee work report xlsx files older than 1 day."""
    with app.app_context():
        cleanup_work_report_files(app.instance_path)


def _report_stall(stalled: float, current_job: str | None) -> None:
    log.critical(
        "Scheduler main loop made no progress for %d s (job in progress: %s); exiting for restart",
        int(stalled),
        current_job or "none",
    )
    for handler in logging.getLogger().handlers:
        handler.flush()
    flush_telemetry()


def run_watchdog(
    progress: LoopProgress,
    timeout_seconds: float,
    check_interval_seconds: float = WATCHDOG_CHECK_INTERVAL_SECONDS,
    *,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    exit_process: Callable[[int], object] = os._exit,
    report_timeout_seconds: float = WATCHDOG_REPORT_TIMEOUT_SECONDS,
) -> None:
    """Exit the process when the main loop stalls, so the container platform restarts it.

    os._exit because a main thread hung in a DB/network call cannot be interrupted.
    The report runs in a daemon thread and is waited for only briefly: a main thread
    blocked inside a logging handler holds its lock, which would block the report forever.
    """
    while True:
        sleep(check_interval_seconds)
        stalled = clock() - progress.last_pass
        if stalled > timeout_seconds:
            try:
                reporter = threading.Thread(
                    target=_report_stall,
                    args=(stalled, progress.current_job),
                    name="scheduler-stall-report",
                    daemon=True,
                )
                reporter.start()
                reporter.join(report_timeout_seconds)
            finally:
                exit_process(1)
            return


def register_jobs() -> None:
    """Register every scheduler job on the default ``schedule`` scheduler, each through ``_logged_task``."""
    schedule.every(MAIL_QUEUE_INTERVAL_SECONDS).seconds.do(_logged_task("process_email_queue", process_email_queue))
    schedule.every(1).minutes.do(_logged_task("open_assignments", open_assignments))
    schedule.every(1).minutes.do(_logged_task("close_completed_events", close_completed_events))
    schedule.every(5).minutes.do(_logged_task("send_reminders", send_reminders))
    schedule.every(5).minutes.do(_logged_task("send_external_opportunities", send_external_opportunities))
    schedule.every(1).hours.do(_logged_task("send_admin_digest", send_admin_digest_task))
    # Poll every minute so an HH:MM schedule can fire on the configured minute.
    # The task itself short-circuits cheaply when the time isn't due yet.
    schedule.every(1).minutes.do(_logged_task("scheduled_backup", scheduled_backup_task))
    schedule.every(15).minutes.do(_logged_task("record_metrics", record_metrics))
    schedule.every(1).hours.do(_logged_task("cleanup_work_report", cleanup_work_report))


if __name__ == "__main__":
    register_jobs()

    log.info(
        "Scheduler started (instance=%s pid=%d mail_queue_interval=%ds db_query_timeout=%ds watchdog=%dmin)",
        INSTANCE_ID or "?",
        os.getpid(),
        MAIL_QUEUE_INTERVAL_SECONDS,
        SCHEDULER_DB_QUERY_TIMEOUT_SECONDS,
        SCHEDULER_WATCHDOG_TIMEOUT_MINUTES,
    )

    _progress.last_pass = time.monotonic()
    if SCHEDULER_WATCHDOG_TIMEOUT_MINUTES:
        threading.Thread(
            target=run_watchdog,
            args=(_progress, SCHEDULER_WATCHDOG_TIMEOUT_MINUTES * 60),
            name="scheduler-watchdog",
            daemon=True,
        ).start()

    _last_alive_log: float = 0.0  # monotonic timestamp of last hourly alive message
    _last_heartbeat: float = 0.0  # monotonic timestamp of last heartbeat write

    while True:
        schedule.run_pending()

        # Hourly "still alive" heartbeat in the log for easy monitoring.
        _now_mono = time.monotonic()
        if _now_mono - _last_alive_log >= 3600:
            log.info(
                "Scheduler alive (instance=%s pid=%d)",
                INSTANCE_ID or "?",
                os.getpid(),
            )
            _last_alive_log = _now_mono

        if _now_mono - _last_heartbeat >= HEARTBEAT_INTERVAL_SECONDS:
            # Write heartbeat so the admin dashboard can confirm the scheduler is alive
            try:
                with app.app_context():
                    s = get_settings()
                    s.scheduler_last_seen = datetime.now(timezone.utc)
                    db.session.commit()
            except Exception as exc:  # noqa: BLE001
                log.warning("Heartbeat write failed: %s", exc)
            # Also touch a local file so Docker healthcheck can verify without a DB query
            try:
                Path("/tmp/scheduler_heartbeat").touch()
            except Exception as exc:  # noqa: BLE001
                log.warning("Heartbeat file touch failed: %s", exc)
            _last_heartbeat = _now_mono

        # Poll interval must stay below MAIL_QUEUE_INTERVAL_SECONDS (and every other
        # schedule.every(...) interval) — schedule.run_pending() only fires a job once
        # this loop notices it's due, so a coarser sleep here would silently cap how
        # often the mail queue (and any other job) can actually run.
        time.sleep(1)
        _progress.last_pass = time.monotonic()
