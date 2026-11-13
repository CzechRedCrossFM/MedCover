import enum
import secrets
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from flask_login import UserMixin
from sqlalchemy import ColumnElement
from sqlalchemy.ext.hybrid import hybrid_property
from sqlalchemy.orm import Mapped, deferred
from werkzeug.security import check_password_hash, generate_password_hash

from app.extensions import db
from app.models.role import ROLE_PERMISSIONS, Role

if TYPE_CHECKING:
    from app.models.assignment import Assignment, DebriefingRecord
    from app.models.audit import AuditLogEntry
    from app.models.equipment import EquipmentItem
    from app.models.event import Event
    from app.models.feedback import UserFeedback
    from app.models.invite import RegistrationInvite
    from app.models.master_event import MasterEvent
    from app.models.qualification import Qualification


NAME_PLACEHOLDER = "(doplnit)"
NAME_PART_MAX_LENGTH = 100
_NAME_COLUMN_MAX_BYTES = 255


def display_name(surname: str | None, first_name: str | None) -> str:
    """Join the name parts into the displayed "Příjmení Jméno" form."""
    return " ".join(part for part in (surname, first_name) if part)


def split_full_name(full: str | None) -> tuple[str, str]:
    """Split a one-string "Příjmení Jméno" name at its first whitespace.

    Mirrors the SQL used by the migration that introduced the separate columns.
    A single word becomes the surname and the first name gets the placeholder.
    """
    words = (full or "").split()
    if not words:
        return NAME_PLACEHOLDER, NAME_PLACEHOLDER
    if len(words) == 1:
        return words[0], NAME_PLACEHOLDER
    return words[0], " ".join(words[1:])


def is_name_placeholder(value: str) -> bool:
    """True for the placeholder in any letter case and spacing, e.g. "( Doplnit )"."""
    return "".join(value.split()).casefold() == NAME_PLACEHOLDER.replace(" ", "").casefold()


def _name_part_length_error(label: str, value: str) -> str | None:
    """Return a Czech error message if *value* exceeds the character or column byte limit."""
    if len(value) > NAME_PART_MAX_LENGTH:
        return f"{label} je příliš dlouhé (nejvýše {NAME_PART_MAX_LENGTH} znaků)."
    if len(value.encode("utf-8")) > _NAME_COLUMN_MAX_BYTES:
        return f"{label} obsahuje příliš mnoho speciálních znaků (např. emoji), zkraťte ho."
    return None


def parse_name_parts(
    surname_raw: str | None,
    first_name_raw: str | None,
    allow_placeholder_first_name: bool = False,
) -> tuple[str, str]:
    """Normalise and validate submitted surname and first name.

    The placeholder is reserved for migrated legacy rows, so it is rejected as input
    (the event import may keep it as first name of a sheet name that has a single word).
    Raises ``ValueError`` with a Czech message suitable for flashing.
    """
    surname = " ".join((surname_raw or "").split())
    first_name = " ".join((first_name_raw or "").split())
    if not surname:
        raise ValueError("Příjmení nesmí být prázdné.")
    if not first_name:
        raise ValueError("Jméno nesmí být prázdné.")
    if is_name_placeholder(surname):
        raise ValueError(f"Příjmení nesmí být „{NAME_PLACEHOLDER}“.")
    if is_name_placeholder(first_name):
        if not allow_placeholder_first_name:
            raise ValueError(f"Jméno nesmí být „{NAME_PLACEHOLDER}“.")
        # store the canonical form, so the surname-only lookup of placeholder users finds it
        first_name = NAME_PLACEHOLDER
    for label, value in (("Příjmení", surname), ("Jméno", first_name)):
        error = _name_part_length_error(label, value)
        if error:
            raise ValueError(error)
    return surname, first_name


class CalendarView(str, enum.Enum):
    MONTH = "month"
    WEEK = "week"
    DAY = "day"
    LIST = "list"


class EventTimeFormat(enum.IntEnum):
    """How an event's time window is shown to the user; stored as its number."""

    START_DURATION = 1  # čt 20:00 (5,5 h)
    START_END = 2  # čt 20:00–01:30


# Many-to-many: UserAccount ↔ Role
user_roles = db.Table(
    "user_roles",
    db.Column("user_id", db.Uuid, db.ForeignKey("user_account.id"), primary_key=True),
    db.Column("role_id", db.Integer, db.ForeignKey("role.id"), primary_key=True),
)


class UserAccount(UserMixin, db.Model):  # type: ignore[misc]
    __tablename__ = "user_account"

    id = db.Column(db.Uuid, primary_key=True, default=uuid.uuid4)
    email = db.Column(db.String(255), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    surname = db.Column(db.String(255), nullable=False)
    first_name = db.Column(db.String(255), nullable=False)
    phone = db.Column(db.String(50), nullable=True)
    is_active = db.Column(db.Boolean, default=False, nullable=False)
    is_archived = db.Column(db.Boolean, default=False, nullable=False, server_default="false")
    preferred_calendar_view = db.Column(
        db.Enum(CalendarView, name="calendar_view_enum"),
        default=CalendarView.LIST,
        nullable=False,
    )
    dashboard_horizon_days = db.Column(db.Integer, default=30, nullable=False, server_default="30")
    dark_mode = db.Column(db.Boolean, default=False, nullable=False, server_default="false")
    event_time_format = db.Column(
        db.SmallInteger, default=EventTimeFormat.START_DURATION, nullable=False, server_default="1"
    )
    # Handwritten-signature image (PNG bytes, mode L, ≤ 50 KB) embedded into the
    # user's work-report xlsx. Deferred so it never rides along with normal
    # UserAccount selects; loaded only when explicitly accessed.
    signature_image = deferred(db.Column(db.LargeBinary, nullable=True))
    signature_mimetype = db.Column(db.String(50), nullable=True)
    # Profile photo: 320x320 JPEG (<= 64 KiB), metadata stripped. The blob is deferred so it
    # never rides along with UserAccount selects (event detail lists many users). The token is
    # a short content hash: non-NULL iff a photo exists; it busts browser caches (?v=) and is
    # the ETag. The three columns are NULL together or set together.
    profile_photo = deferred(db.Column(db.LargeBinary, nullable=True))
    profile_photo_token = db.Column(db.String(16), nullable=True)
    # Optimistic-lock counter enforced via SQLAlchemy version_id_col.
    # Callers must bump before commit; stale committed values raise StaleDataError.
    version = db.Column(db.Integer, default=1, nullable=False)

    __mapper_args__ = {"version_id_col": version, "version_id_generator": False}
    # Single-use password reset: nonce is set when a reset link is issued and
    # cleared after successful password change. Old links become invalid immediately.
    password_reset_nonce = db.Column(db.String(64), nullable=True)
    # iCal subscription tokens — 64-char hex, public but unguessable.
    # Generated automatically on account creation; regenerating invalidates
    # any existing calendar subscriptions using the old token.
    ical_token = db.Column(db.String(64), nullable=True, unique=True, index=True, default=lambda: secrets.token_hex(32))
    # Separate token for the all-events iCal feed.
    ical_all_token = db.Column(
        db.String(64), nullable=True, unique=True, index=True, default=lambda: secrets.token_hex(32)
    )
    # Brute-force protection: track consecutive failed logins per account.
    # After LOGIN_MAX_ATTEMPTS failures the account is locked for LOGIN_LOCKOUT_MINUTES.
    failed_login_attempts = db.Column(db.Integer, default=0, nullable=False, server_default="0")
    login_locked_until = db.Column(db.DateTime(timezone=True), nullable=True)
    last_login_at = db.Column(db.DateTime(timezone=True), nullable=True)
    created_at = db.Column(
        db.DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    updated_at = db.Column(
        db.DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    roles: Mapped[list[Role]] = db.relationship(
        "Role",
        secondary=user_roles,
        back_populates="users",
        lazy="selectin",
    )

    qualifications: Mapped[list[Qualification]] = db.relationship(
        "Qualification",
        secondary="user_qualifications",
        back_populates="holders",
        lazy="selectin",
    )

    # ── Inverse relationships (back_populates) ────────────────────────────────
    assignments: Mapped[list[Assignment]] = db.relationship(
        "Assignment",
        foreign_keys="Assignment.user_id",
        back_populates="user",
        lazy="select",
    )
    assignments_made: Mapped[list[Assignment]] = db.relationship(
        "Assignment",
        foreign_keys="Assignment.assigned_by_id",
        back_populates="assigned_by",
        lazy="select",
    )
    submitted_debriefings: Mapped[list[DebriefingRecord]] = db.relationship(
        "DebriefingRecord",
        foreign_keys="DebriefingRecord.submitted_by_id",
        back_populates="submitted_by",
        lazy="select",
    )
    audit_entries: Mapped[list[AuditLogEntry]] = db.relationship(
        "AuditLogEntry",
        foreign_keys="AuditLogEntry.actor_id",
        back_populates="actor",
        lazy="select",
    )
    issued_equipment: Mapped[list[EquipmentItem]] = db.relationship(
        "EquipmentItem",
        foreign_keys="EquipmentItem.issued_to_id",
        back_populates="issued_to",
        lazy="select",
    )
    feedback_entries: Mapped[list[UserFeedback]] = db.relationship(
        "UserFeedback",
        foreign_keys="UserFeedback.user_id",
        back_populates="user",
        lazy="select",
    )
    created_invites: Mapped[list[RegistrationInvite]] = db.relationship(
        "RegistrationInvite",
        foreign_keys="RegistrationInvite.created_by_id",
        back_populates="created_by",
        lazy="select",
    )
    rp_events: Mapped[list[Event]] = db.relationship(
        "Event",
        foreign_keys="Event.responsible_person_id",
        back_populates="responsible_person",
        lazy="select",
    )
    created_events: Mapped[list[Event]] = db.relationship(
        "Event",
        foreign_keys="Event.created_by_id",
        back_populates="created_by",
        lazy="select",
    )
    coordinated_master_events: Mapped[list[MasterEvent]] = db.relationship(
        "MasterEvent",
        foreign_keys="MasterEvent.coordinator_id",
        back_populates="coordinator",
        lazy="select",
    )

    @hybrid_property
    def name(self) -> str:
        """Displayed name "Příjmení Jméno"; read-only, usable in SQL as well."""
        return display_name(self.surname, self.first_name)

    @name.inplace.expression
    @classmethod
    def _name_expression(cls) -> ColumnElement[str]:
        return cls.surname + " " + cls.first_name

    @property
    def initials(self) -> str:
        """First-name initial followed by surname initial, upper-cased."""
        first = "" if self.first_name == NAME_PLACEHOLDER else self.first_name
        letters = [next((ch for ch in part or "" if ch.isalnum()), "") for part in (first, self.surname)]
        return "".join(letters).upper() or "?"

    def regenerate_ical_token(self) -> str:
        """Generate a new iCal subscription token, invalidating the previous one."""
        self.ical_token = secrets.token_hex(32)
        return self.ical_token

    def regenerate_ical_all_token(self) -> str:
        """Generate a new all-events iCal token, invalidating the previous one."""
        self.ical_all_token = secrets.token_hex(32)
        return self.ical_all_token

    def set_password(self, password: str) -> None:
        self.password_hash = generate_password_hash(password)

    def check_password(self, password: str) -> bool:
        return check_password_hash(self.password_hash, password)

    def has_permission(self, code: str) -> bool:
        return any(code in ROLE_PERMISSIONS.get(role.name, []) for role in self.roles)

    def has_any_permission(self, *codes: str) -> bool:
        return any(self.has_permission(code) for code in codes)

    @property
    def has_profile_photo(self) -> bool:
        return self.profile_photo_token is not None

    @property
    def is_external(self) -> bool:
        """Whether the account has the exclusive External role."""
        return any(role.name == Role.EXTERNAL for role in self.roles)

    def is_rp_eligible(self) -> bool:
        """Return True if the user can be a responsible person.

        Requires both a qualification with can_be_rp=True and the
        event.assign_own permission (excludes Viewers and inactive roles).
        """
        return (
            self.is_active
            and not self.is_archived
            and self.has_permission("event.assign_own")
            and any(q.can_be_rp and not q.is_deleted for q in self.qualifications)
        )

    # Flask-Login: use str(uuid) as session token
    def get_id(self) -> str:
        return str(self.id)

    def __repr__(self) -> str:
        return f"<UserAccount {self.email}>"
