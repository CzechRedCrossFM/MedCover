import socket
import time
import uuid
from datetime import datetime, timedelta, timezone

import sqlalchemy as sa
from flask import Blueprint, Response, render_template, request
from flask_login import login_required
from sqlalchemy import collate

from app.extensions import db
from app.models.audit import AuditLogEntry
from app.models.event import Event, EventStatus
from app.models.feedback import UserFeedback
from app.models.outbox import OutboxEmail
from app.models.role import ALL_PERMISSIONS, ROLE_PERMISSIONS
from app.models.settings import AppSettings, get_settings
from app.models.user import UserAccount
from app.utils import (
    CS_COLLATION,
    get_or_404,
    last_page_redirect,
    local_day_start_utc_or_none,
    page_arg,
    require_permission,
)

admin_bp = Blueprint("admin", __name__, url_prefix="/admin")

_PAGE_SIZE = 50

# Response-time thresholds
_DB_WARN_MS = 200  # warn above 200 ms
_SMTP_WARN_MS = 1000  # warn above 1 s


def _check_db_health() -> dict:
    """Probe DB and return {status, ms, error}."""
    try:
        t0 = time.monotonic()
        db.session.execute(db.text("SELECT 1"))
        ms = int((time.monotonic() - t0) * 1000)
        status = "warning" if ms > _DB_WARN_MS else "ok"
        return {"status": status, "ms": ms, "error": None}
    except Exception as exc:
        try:
            db.session.rollback()
        except Exception:
            # a dead connection can fail the rollback too; the probe error is what we report
            pass
        return {"status": "error", "ms": None, "error": type(exc).__name__}


def _check_scheduler(settings: AppSettings, now: datetime) -> dict:
    """Check scheduler heartbeat and return {status, last, age_s}."""
    sched_last = settings.scheduler_last_seen
    if sched_last is None:
        return {"status": "unknown", "last": None, "age_s": None}
    age_s = int((now - sched_last).total_seconds())
    if age_s < 30:
        status = "ok"
    elif age_s < 300:
        status = "warning"
    else:
        status = "error"
    return {"status": status, "last": sched_last, "age_s": age_s}


def _check_smtp(settings: AppSettings) -> dict:
    """Probe SMTP and return {configured, status, ms, error}."""
    if not settings.smtp_configured:
        return {"configured": False, "status": "unconfigured", "ms": None, "error": None}
    try:
        t0 = time.monotonic()
        with socket.create_connection((settings.smtp_server, settings.smtp_port), timeout=3):
            pass
        ms = int((time.monotonic() - t0) * 1000)
        status = "warning" if ms > _SMTP_WARN_MS else "ok"
        return {"configured": True, "status": status, "ms": ms, "error": None}
    except TimeoutError:
        return {
            "configured": True,
            "status": "error",
            "ms": None,
            "error": f"Spojení na {settings.smtp_server}:{settings.smtp_port} vypršelo (timeout 3 s)",
        }
    except OSError as exc:
        return {"configured": True, "status": "error", "ms": None, "error": str(exc)}


def _admin_statistics(now: datetime) -> dict:
    """Collect user/event/outbox/audit statistics for the admin dashboard."""
    # SQL Server cannot use a bare bit column as a CASE predicate, hence the explicit comparisons
    not_archived = UserAccount.is_archived == sa.false()
    user_total, user_active, user_archived = db.session.execute(
        db.select(
            db.func.count(sa.case((not_archived, 1))),
            db.func.count(sa.case((sa.and_(not_archived, UserAccount.is_active == sa.true()), 1))),
            db.func.count(sa.case((UserAccount.is_archived == sa.true(), 1))),
        )
    ).one()

    event_rows = db.session.execute(db.select(Event.status, db.func.count()).group_by(Event.status)).all()
    counts_by_status = {status: count for status, count in event_rows}
    event_counts = {s.value: counts_by_status.get(s, 0) for s in EventStatus}

    cutoff_24h = now - timedelta(hours=24)
    outbox_rows = db.session.execute(
        db.select(OutboxEmail.status, db.func.count())
        .where(OutboxEmail.created_at >= cutoff_24h, OutboxEmail.status.in_(("pending", "failed", "sent")))
        .group_by(OutboxEmail.status)
    ).all()
    outbox_by_status = {status: count for status, count in outbox_rows}

    outbox_last = db.session.scalar(db.select(OutboxEmail).order_by(OutboxEmail.created_at.desc()).limit(1))
    outbox_last_sent = db.session.scalar(
        db.select(OutboxEmail.sent_at).where(OutboxEmail.status == "sent").order_by(OutboxEmail.sent_at.desc()).limit(1)
    )

    recent_audit = db.session.scalars(db.select(AuditLogEntry).order_by(AuditLogEntry.timestamp.desc()).limit(8)).all()

    feedback_count = db.session.scalar(db.select(db.func.count()).select_from(UserFeedback))

    return {
        "user_total": user_total,
        "user_active": user_active,
        "user_pending": user_total - user_active,
        "user_archived": user_archived,
        "event_total": sum(event_counts.values()),
        "event_counts": event_counts,
        "outbox_pending": outbox_by_status.get("pending", 0),
        "outbox_failed": outbox_by_status.get("failed", 0),
        "outbox_sent": outbox_by_status.get("sent", 0),
        "outbox_last_status": outbox_last.status if outbox_last else None,
        "outbox_last_sent": outbox_last_sent,
        "recent_audit": recent_audit,
        "feedback_count": feedback_count,
    }


@admin_bp.route("/")
@login_required
def index() -> str:
    require_permission("admin.view")

    settings = get_settings()
    now = datetime.now(timezone.utc)

    db_health = _check_db_health()
    sched = _check_scheduler(settings, now)
    smtp = _check_smtp(settings)
    stats = _admin_statistics(now)

    return render_template(
        "admin/index.html",
        db_status=db_health["status"],
        db_ms=db_health["ms"],
        db_error=db_health["error"],
        sched_status=sched["status"],
        sched_last=sched["last"],
        sched_age_s=sched["age_s"],
        smtp_configured=smtp["configured"],
        smtp_status=smtp["status"],
        smtp_ms=smtp["ms"],
        smtp_error=smtp["error"],
        settings=settings,
        now=now,
        **stats,
    )


# ── Permission matrix ─────────────────────────────────────────────────────────


@admin_bp.route("/permissions")
@login_required
def permissions() -> str:
    require_permission("admin.view")

    role_names = list(ROLE_PERMISSIONS.keys())
    return render_template(
        "admin/permissions.html",
        all_permissions=ALL_PERMISSIONS,
        role_names=role_names,
        role_permissions=ROLE_PERMISSIONS,
    )


# ── Audit log ─────────────────────────────────────────────────────────────────


@admin_bp.route("/audit-log/")
@login_required
def audit_log_list() -> str | Response:
    require_permission("admin.view")

    page = page_arg()
    f_entity_type = request.args.get("entity_type", "").strip()
    f_actor_id = request.args.get("actor_id", "").strip()
    f_action_type = request.args.get("action_type", "").strip()
    f_date_from = request.args.get("date_from", "").strip()
    f_date_to = request.args.get("date_to", "").strip()
    f_q = request.args.get("q", "").strip()

    query = db.select(AuditLogEntry).order_by(AuditLogEntry.timestamp.desc())

    if f_entity_type:
        query = query.where(AuditLogEntry.entity_type == f_entity_type)
    if f_actor_id == "system":
        # "system" entries have no actor (actor_id IS NULL)
        query = query.where(AuditLogEntry.actor_id.is_(None))
    elif f_actor_id:
        try:
            actor_uuid = uuid.UUID(f_actor_id)
        except ValueError:
            f_actor_id = ""
        else:
            query = query.where(AuditLogEntry.actor_id == actor_uuid)
    if f_action_type:
        query = query.where(AuditLogEntry.action_type == f_action_type)
    dt_from = local_day_start_utc_or_none(f_date_from)
    if dt_from is not None:
        query = query.where(AuditLogEntry.timestamp >= dt_from)
    # the "to" day is inclusive: bound by the start of the next local day
    dt_to = local_day_start_utc_or_none(f_date_to, next_day=True)
    if dt_to is not None:
        query = query.where(AuditLogEntry.timestamp < dt_to)
    if f_q:
        # Column collation (Czech_100_CI_AS_SC_UTF8) already gives us
        # case-insensitive matching, so a bare LIKE keeps the column reference
        # sargable — .ilike() on MSSQL compiles to LOWER(col) LIKE LOWER(?)
        # which hides the column from any index.
        query = query.where(AuditLogEntry.summary.like(f"%{f_q}%"))

    # Paginate manually: count + offset/limit
    total = db.session.scalar(db.select(db.func.count()).select_from(query.subquery()))
    total_pages = max(1, (total + _PAGE_SIZE - 1) // _PAGE_SIZE)
    if redirect_resp := last_page_redirect(page, total_pages):
        return redirect_resp
    entries = db.session.scalars(query.offset((page - 1) * _PAGE_SIZE).limit(_PAGE_SIZE)).all()

    # Distinct values for filter dropdowns
    entity_types = db.session.scalars(
        db.select(AuditLogEntry.entity_type).distinct().order_by(AuditLogEntry.entity_type)
    ).all()
    action_types = db.session.scalars(
        db.select(AuditLogEntry.action_type).distinct().order_by(AuditLogEntry.action_type)
    ).all()
    actors = db.session.scalars(
        db.select(UserAccount)
        .where(
            UserAccount.id.in_(db.select(AuditLogEntry.actor_id).where(AuditLogEntry.actor_id.isnot(None)).distinct())
        )
        .order_by(collate(UserAccount.name, CS_COLLATION))
    ).all()

    return render_template(
        "admin/audit_log_list.html",
        entries=entries,
        page=page,
        total=total,
        total_pages=total_pages,
        entity_types=entity_types,
        action_types=action_types,
        actors=actors,
        f_entity_type=f_entity_type,
        f_actor_id=f_actor_id,
        f_action_type=f_action_type,
        f_date_from=f_date_from,
        f_date_to=f_date_to,
        f_q=f_q,
    )


@admin_bp.route("/audit-log/<int:entry_id>")
@login_required
def audit_log_detail(entry_id: int) -> str:
    require_permission("admin.view")

    entry = get_or_404(AuditLogEntry, entry_id)

    return render_template("admin/audit_log_detail.html", entry=entry)
