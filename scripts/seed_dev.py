"""
Seed script for the development environment.

Creates roles, dev user accounts, qualifications and equipment types (reference
data, get-or-create on every run), then - only when the database has no events -
a synthetic dataset: five extra members (the dev accounts plus these cover every role and
qualification), master events, templates, ~200 rule-based events spread around today (about
a third in the past), assignments, equipment and debriefings. Every event and template has a
valid condition plan, each responsible person is an eligible assignee, and closed/completed
events are fully staffed. Event dates are relative to the run date and the random generator
is seeded, so runs are reproducible except for the date anchor.
Everything is fictional; e-mail addresses use the undeliverable @medcover.local.

Usage:
    python scripts/seed_dev.py

Or from within the Docker web container:
    docker compose exec web python scripts/seed_dev.py

The bulk dataset is generated only into an empty database (no events). To
regenerate it, recreate the database first.
"""

import os
import random
import sys
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import sqlalchemy as sa

# Allow running from repo root
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# python-dotenv is a dev-only dependency; the Docker image sets env vars itself
try:
    from dotenv import load_dotenv
except ImportError:

    def load_dotenv(*_args: Any, **_kwargs: Any) -> bool:
        return False


from app import create_app
from app.extensions import db
from app.models.assignment import Assignment, DebriefingRecord
from app.models.equipment import EquipmentItem, EquipmentType, EventEquipmentPlan, EventTemplateEquipmentPlan
from app.models.event import (
    Event,
    EventQualificationRequirement,
    EventStatus,
    EventTemplate,
    EventTemplateQualificationRequirement,
    EventType,
    StaffingMode,
)
from app.models.master_event import MasterEvent
from app.models.qualification import Qualification
from app.models.role import ROLE_PERMISSIONS, Role
from app.models.settings import AppSettings
from app.models.user import UserAccount
from app.routes.dev import DEV_ACCOUNTS

SEED = 20260101
DEV_PASSWORD = "devpassword"
PRAGUE = ZoneInfo("Europe/Prague")
PAST_DAYS = 120
FUTURE_DAYS = 240

# Expected events per day by weekday (Monday first)
WEEKDAY_WEIGHTS = (0.15, 0.4, 0.3, 0.65, 0.8, 1.4, 0.35)
SERIES_SHARE = 0.4
START_HOUR_WEIGHTS = {7: 5, 8: 5, 9: 2, 10: 2, 14: 3, 15: 4, 16: 4, 17: 3}

MALE_NAMES = (
    "Jan Petr Martin Tomáš Jakub Lukáš Michal David Ondřej Pavel Jiří Filip Marek Adam Daniel Vojtěch Radek "
    "Štěpán Matěj Karel"
).split()
FEMALE_NAMES = (
    "Jana Marie Eva Lucie Petra Tereza Kateřina Lenka Veronika Anna Hana Barbora Klára Markéta Michaela Nikola "
    "Zuzana Monika Simona Alena"
).split()
SURNAMES = (
    ("Dvořák", "Dvořáková"),
    ("Černý", "Černá"),
    ("Procházka", "Procházková"),
    ("Kučera", "Kučerová"),
    ("Veselý", "Veselá"),
    ("Horák", "Horáková"),
    ("Němec", "Němcová"),
    ("Pospíšil", "Pospíšilová"),
    ("Vymyšlený", "Vymyšlená"),
    ("Hájek", "Hájková"),
    ("Král", "Králová"),
    ("Jelínek", "Jelínková"),
    ("Růžička", "Růžičková"),
    ("Beneš", "Benešová"),
    ("Fiala", "Fialová"),
    ("Sedláček", "Sedláčková"),
    ("Doležal", "Doležalová"),
    ("Zeman", "Zemanová"),
    ("Kolář", "Kolářová"),
    ("Navrátil", "Navrátilová"),
)

# (title, venue, weight): recurring series, titles get the date appended
SERIES = (
    ("Házená Zbyslavice - domácí zápas", "Sportovní hala Zbyslavice", 23),
    ("Florbal Hrádek - domácí zápas", "Hala Hrádek", 8),
    ("Stavba obchvatu Krásná Voda - zdravotní dozor", "Areál stavby Krásná Voda", 14),
    ("Plavecké závody Modrá - kolo", "Městské koupaliště Modrá", 7),
    ("Svařovací soutěž Železná - den", "Výstaviště Železná", 6),
    ("Běžecká liga Bukovec", "Náměstí Bukovec", 6),
    ("Fotbal Lipová - domácí zápas", "Stadion Lipová", 7),
)
ORGANISERS = ("Obec", "Město", "Základní škola", "Sportovní klub", "TJ Sokol", "Technické služby", "Dobrovolní hasiči")
PLACES = ("Horní Lhota", "Dolní Újezdec", "Březová", "Kamenná Lhotka", "Podolany", "Rybníky", "Vlčice", "Dubina")
ACTIVITIES = (
    "dětský den",
    "sportovní odpoledne",
    "závod v běhu do vrchu",
    "slavnosti města",
    "den bezpečnosti",
    "hasičská soutěž",
    "koncert na náměstí",
    "charitativní běh",
    "adventní trhy",
    "turnaj v malé kopané",
)
VENUE_TYPES = ("Sportovní hala", "Zimní stadion", "Městské koupaliště", "Náměstí", "Základní škola", "Areál")

QUALIFICATION_NAMES = {
    "Zdravotník": "Zdravotník zotavovacích akcí",
    "Zelenáč": "Zelenáč",
    "Záchranář": "Záchranář",
    "Lékař": "Lékař",
    "Řidič": "Řidič sanitky",
    "Lektor": "Lektor",
}
# (name, e-mail, qualifications): the dev accounts cover every role, these cover the remaining qualifications
SYNTHETIC_MEMBERS = (
    ("Kučera Tomáš", "tomas.kucera@medcover.local", ("Zelenáč",)),
    ("Veselá Eva", "eva.vesela@medcover.local", ("Zdravotník",)),
    ("Vymyšlený Jiří", "jiri.vymysleny@medcover.local", ("Záchranář",)),
    ("Hájková Lucie", "lucie.hajkova@medcover.local", ("Lékař",)),
    ("Fiala Adam", "adam.fiala@medcover.local", ("Lektor", "Zelenáč")),
)

EQUIPMENT_TYPES = (
    ("AED", "🫀", "AED"),
    ("Batoh malý", "🎒", "malá lékárnička"),
    ("Batoh velký", "🎒", "velká lékárnička"),
    ("KPR figurína adolescent", "🧍", ""),
    ("KPR figurína dospělá standardní", "🧍", ""),
    ("KPR figurína dospělá tlustá", "🧍", ""),
    ("KPR figurína mimino", "👶", ""),
    ("Mikina", "🧥", ""),
    ("Nosítka", "🛏️", "Skládací transportní nosítka."),
    ("Sanitka", "🚑", ""),
    ("Stan", "⛺", ""),
    ("Uniforma blůza", "👕", ""),
    ("Uniforma kalhoty", "👖", ""),
    ("VR sada", "🥽", ""),
    ("Tréninkový defibrilátor", "⚡", ""),
    ("KPR figurína novorozenec", "👶", ""),
    ("KPR figurína senior", "🧓", ""),
    ("Kyslíková láhev 2 l", "🫁", ""),
    ("Kyslíková láhev 5 l", "🫁", ""),
    ("Vakuová dlaha", "🦴", ""),
    ("Krční límec", "🩹", ""),
    ("Páteřní deska", "🛏️", ""),
    ("Lopatkové nosítka", "🛏️", ""),
    ("Termofólie", "🌡️", ""),
    ("Chladicí box", "🧊", ""),
    ("Reflexní vesta", "🦺", ""),
    ("Vysílačka", "📻", ""),
    ("Notebook", "💻", ""),
    ("Projektor", "📽️", ""),
)
ITEMS_PER_TYPE = tuple([2] * 18 + [1] * 11)  # 47 items over 29 types
HOME_LOCATIONS = (("Sklad A", 30), ("Sklad B", 10), ("spolek", 6), ("Sanitka", 1))

DEBRIEFING_SENTENCES = (
    "Akce proběhla bez komplikací.",
    "Spolupráce s pořadateli byla výborná.",
    "Stanoviště bylo dobře vybavené.",
    "Chyběl nám dostatek vody pro posádku.",
    "Příště doporučuji posílit obsazení o jednoho člena.",
    "Komunikace s kolegy fungovala bez problémů.",
)
COVER_TYPES = ("zdravotní dozor", "zdravotní zabezpečení")
VEHICLES = ("sanitka", "stan", "sanitka + stan")


def prague_today() -> date:
    return datetime.now(PRAGUE).date()


def _anchor_dt(today: date) -> datetime:
    return datetime.combine(today, time(0, 0), tzinfo=PRAGUE).astimezone(timezone.utc)


def _fake_phone(rng: random.Random) -> str:
    # The 000 prefix is not allocated, so these numbers cannot reach anyone
    return f"+420 000 {rng.randint(100, 999)} {rng.randint(100, 999)}"


def _fake_name(rng: random.Random) -> str:
    if rng.random() < 0.5:
        return f"{rng.choice(SURNAMES)[0]} {rng.choice(MALE_NAMES)}"
    return f"{rng.choice(SURNAMES)[1]} {rng.choice(FEMALE_NAMES)}"


def _get_or_create(model: Any, name: str, **kwargs: object) -> Any:
    obj = db.session.scalar(db.select(model).where(model.name == name))
    if not obj:
        obj = model(name=name, **kwargs)
        db.session.add(obj)
        db.session.flush()
    return obj


def seed_reference() -> None:
    """Settings, roles, dev accounts, qualifications and equipment types. Get-or-create."""
    # ── AppSettings ───────────────────────────────────────────────────────
    settings = db.session.get(AppSettings, 1)
    if not settings:
        print("Seeding AppSettings...")
        db.session.add(AppSettings(id=1, setup_complete=True, org_name="Czech Red Cross (Dev)", dev_email_block=True))
        db.session.flush()
    elif not settings.setup_complete:
        print("Marking AppSettings setup_complete=True...")
        settings.setup_complete = True
        settings.dev_email_block = True
        db.session.flush()

    # ── Roles ─────────────────────────────────────────────────────────────
    print("Seeding roles...")
    for role_name in ROLE_PERMISSIONS:
        if not db.session.scalar(db.select(Role).where(Role.name == role_name)):
            db.session.add(Role(name=role_name))
    db.session.flush()

    # ── Dev user accounts ─────────────────────────────────────────────────
    print("Seeding dev user accounts...")
    role_map = {
        "admin": Role.ADMIN,
        "coordinator": Role.COORDINATOR,
        "member": Role.MEMBER,
        "viewer": Role.VIEWER,
        "debrief_manager": Role.DEBRIEFING_MANAGER,
        "inactive": Role.MEMBER,
    }
    for account_def in DEV_ACCOUNTS:
        email = account_def["email"]
        if db.session.scalar(db.select(UserAccount).where(UserAccount.email == email)):
            print(f"  {email} already exists, skipping.")
            continue
        user = UserAccount(
            email=email,
            name=account_def["name"],
            is_active=(account_def["role"] != "inactive"),
        )
        user.set_password(DEV_PASSWORD)
        role_name = role_map[account_def["role"]]
        role_obj = db.session.scalar(db.select(Role).where(Role.name == role_name))
        if role_obj:
            user.roles.append(role_obj)
        db.session.add(user)
        status = "inactive" if account_def["role"] == "inactive" else "active"
        print(f"  Created {email} ({status})")
    db.session.commit()

    # ── Qualifications ────────────────────────────────────────────────────
    print("Seeding qualifications...")
    c_zdravotnik = _get_or_create(
        Qualification,
        QUALIFICATION_NAMES["Zdravotník"],
        description="Základní zdravotnická způsobilost pro zotavovací akce.",
    )
    c_zelenac = _get_or_create(Qualification, "Zelenáč", description="Začínající člen bez zdravotnické kvalifikace.")
    c_zachranar = _get_or_create(Qualification, "Záchranář", description="Zdravotní záchranář — rozšířená způsobilost.")
    c_lekar = _get_or_create(
        Qualification, "Lékař", description="Absolvent lékařské fakulty, způsobilý k výkonu povolání lékaře."
    )
    c_ridic = _get_or_create(
        Qualification, "Řidič sanitky", description="Oprávnění řídit sanitní vozidlo, řidičský průkaz sk. B+."
    )
    c_lektor = _get_or_create(Qualification, "Lektor", description="Lektor první pomoci.")
    # Hierarchy: Zdravotník can fill a Zelenáč requirement; Záchranář a Zdravotník one; Lékař a Záchranář one
    for child, parent in ((c_zelenac, c_zdravotnik), (c_zdravotnik, c_zachranar), (c_zachranar, c_lekar)):
        if parent not in child.parents:
            child.parents.append(parent)
    for qual in (c_zdravotnik, c_zachranar, c_lekar, c_lektor):
        qual.can_be_rp = True
    db.session.flush()

    coordinator_user = _dev_user("coordinator")
    member_user = _dev_user("member")
    if c_zachranar not in coordinator_user.qualifications:
        coordinator_user.qualifications.append(c_zachranar)
    for qual in (c_zdravotnik, c_ridic):
        if qual not in member_user.qualifications:
            member_user.qualifications.append(qual)

    # ── Equipment types ───────────────────────────────────────────────────
    print("Seeding equipment types...")
    for name, icon, description in EQUIPMENT_TYPES:
        _get_or_create(EquipmentType, name, icon=icon, description=description)
    db.session.commit()


def _dev_user(role: str) -> UserAccount:
    email = next(a["email"] for a in DEV_ACCOUNTS if a["role"] == role)
    return db.session.execute(db.select(UserAccount).where(UserAccount.email == email)).scalar_one()


def _qualifications() -> dict[str, Qualification]:
    return {
        short: db.session.execute(db.select(Qualification).where(Qualification.name == name)).scalar_one()
        for short, name in QUALIFICATION_NAMES.items()
    }


def _local_start(day: date, rng: random.Random) -> datetime:
    hours = list(range(6, 21))
    hour = rng.choices(hours, weights=[START_HOUR_WEIGHTS.get(h, 0.5) for h in hours])[0]
    minute = rng.choice((0, 0, 0, 30))
    return datetime.combine(day, time(hour, minute), tzinfo=PRAGUE).astimezone(timezone.utc)


def _duration_hours(rng: random.Random) -> int:
    r = rng.random()
    if r < 0.56:
        return rng.randint(1, 5)
    if r < 0.90:
        return rng.randint(6, 12)
    if r < 0.95:
        return rng.randint(13, 16)
    return 24 if r < 0.99 else 48


def _status_for(k: int, rng: random.Random) -> EventStatus:
    weighted: tuple[tuple[EventStatus, int], ...]
    if k < 0:
        return EventStatus.CANCELLED if rng.random() < 0.04 else EventStatus.COMPLETED
    if rng.random() < 0.03:
        return EventStatus.CANCELLED
    if k <= 7:
        weighted = ((EventStatus.ASSIGNMENTS_CLOSED, 60), (EventStatus.ASSIGNMENTS_OPEN, 40))
    elif k <= 45:
        weighted = ((EventStatus.ASSIGNMENTS_OPEN, 85), (EventStatus.PUBLISHED, 15))
    elif k <= 120:
        weighted = ((EventStatus.PUBLISHED, 40), (EventStatus.ASSIGNMENTS_OPEN, 30), (EventStatus.DRAFT, 30))
    else:
        weighted = ((EventStatus.DRAFT, 60), (EventStatus.PUBLISHED, 40))
    return rng.choices([s for s, _ in weighted], weights=[w for _, w in weighted])[0]


def _plan_one(day: date, k: int, anchor: datetime, rng: random.Random) -> dict[str, Any]:
    series = None
    me = "general"
    if rng.random() < SERIES_SHARE:
        series = rng.choices(SERIES, weights=[s[2] for s in SERIES])[0]
        me = "league" if series is SERIES[0] else "general"
    r = rng.random()
    event_type = EventType.TRAINING if r < 0.009 else EventType.PRESENTATION if r < 0.013 else EventType.MEDICAL_COVER
    place = rng.choice(PLACES)
    if series:
        title = f"{series[0]} {day.day}.{day.month}."
        venue = series[1]
    elif event_type is EventType.TRAINING:
        title, venue = f"Školení první pomoci - {rng.choice(ORGANISERS)} {place}", f"Základní škola {place}"
    elif event_type is EventType.PRESENTATION:
        title, venue = f"Prezentace Červeného kříže - {place}", f"Náměstí {place}"
    else:
        title = f"{rng.choice(ORGANISERS)} {place} - {rng.choice(ACTIVITIES)}"
        venue = f"{rng.choice(VENUE_TYPES)} {place}"

    start = _local_start(day, rng)
    end = start + timedelta(hours=_duration_hours(rng))
    if k < 0 and end > anchor:
        # A long event on the last past day must not run into the future
        start = anchor - timedelta(hours=rng.randint(4, 8))
        end = start + timedelta(hours=rng.randint(1, 3))
    status = _status_for(k, rng)
    return {
        "name": title,
        "start": start,
        "end": end,
        "status": status,
        "k": k,
        "me": me,
        "event_type": event_type,
        "paid": rng.random() < 0.72,
        "venue": venue,
        "archived": False,
    }


def _festival_plans(today: date) -> list[dict[str, Any]]:
    k = 55
    while (today + timedelta(days=k)).weekday() != 4:
        k += 1
    plans = []
    for offset, day_name in enumerate(("pátek", "sobota", "neděle")):
        start = datetime.combine(today + timedelta(days=k + offset), time(10, 0), tzinfo=PRAGUE).astimezone(
            timezone.utc
        )
        plans.append(
            {
                "name": f"Hudební festival Zelený břeh - {day_name}",
                "start": start,
                "end": start + timedelta(hours=12),
                "status": EventStatus.ASSIGNMENTS_OPEN,
                "k": k + offset,
                "me": "festival",
                "event_type": EventType.MEDICAL_COVER,
                "paid": True,
                "venue": "Areál Zelený břeh",
                "archived": False,
            }
        )
    return plans


def plan_events(today: date, rng: random.Random) -> list[dict[str, Any]]:
    """Plan every event relative to *today*. Pure: no database, no clock."""
    anchor = _anchor_dt(today)
    plans: list[dict[str, Any]] = []
    for k in [*range(-PAST_DAYS, 0), *range(1, FUTURE_DAYS + 1)]:
        day = today + timedelta(days=k)
        expected = WEEKDAY_WEIGHTS[day.weekday()]
        count = int(expected) + (rng.random() < expected - int(expected))
        plans.extend(_plan_one(day, k, anchor, rng) for _ in range(count))
    plans.extend(_festival_plans(today))
    archived_me = [p for p in plans if p["k"] < -30 and p["me"] == "general" and p["status"] is EventStatus.COMPLETED]
    for p in archived_me[:5]:
        p["me"] = "archive"
    # Deterministic coverage of the rarer states, independent of the random draws
    open_soon = [
        p
        for p in plans
        if 0 < p["k"] <= 45 and p["status"] in (EventStatus.ASSIGNMENTS_OPEN, EventStatus.ASSIGNMENTS_CLOSED)
    ]
    for p in open_soon[:2]:
        p["status"] = EventStatus.ASSIGNMENTS_CLOSED
    old_completed = [p for p in plans if p["k"] < -90 and p["status"] is EventStatus.COMPLETED]
    for p in old_completed[:3]:
        p["archived"] = True
    plans.sort(key=lambda p: p["start"])
    return plans


def _generate_users(password_hash: str, roles: dict[str, Role], quals: dict[str, Qualification]) -> list[UserAccount]:
    users = []
    for name, email, qual_names in SYNTHETIC_MEMBERS:
        user = UserAccount(email=email, name=name, is_active=True)
        user.password_hash = password_hash
        user.roles = [roles["Member"]]
        user.qualifications = [quals[n] for n in qual_names]
        users.append(user)
    return users


class _Staffing:
    """Picks assignees: active users who can fill a qualification, skewed towards a few busy ones."""

    def __init__(
        self,
        rng: random.Random,
        users: list[UserAccount],
        favourites: list[UserAccount],
        quals: dict[str, Qualification],
    ) -> None:
        self.rng = rng
        self.weights = {u.id: min(rng.paretovariate(1.5), 10.0) for u in users}
        for u in favourites:
            self.weights[u.id] = 8.0
        self.users = [u for u in users if u.is_active and not u.is_archived]
        # Built up front: lazy loads during event construction would autoflush half-built events
        self.pools = {
            q.name: [u for u in self.users if any(q.can_be_filled_by(uq) for uq in u.qualifications)]
            for q in quals.values()
        }

    def pick(self, qual: Qualification, taken: set[Any]) -> UserAccount | None:
        candidates = [u for u in self.pools[qual.name] if u.id not in taken]
        if not candidates:
            return None
        user = self.rng.choices(candidates, weights=[self.weights[u.id] for u in candidates])[0]
        taken.add(user.id)
        return user


def _seed_templates(quals: dict[str, Qualification], types: dict[str, EquipmentType]) -> None:
    for name, event_type, paid, minimum, maximum, qual, plan in (
        (
            "Placený zdravotní dozor - 1 hlídka",
            EventType.MEDICAL_COVER,
            True,
            2,
            3,
            "Zdravotník",
            (("Batoh malý", 1), ("AED", 1)),
        ),
        ("Sportovní utkání", EventType.MEDICAL_COVER, False, 1, 2, "Zdravotník", (("Batoh velký", 1),)),
        ("Školení první pomoci", EventType.TRAINING, False, 1, 1, "Lektor", ()),
    ):
        if db.session.scalar(db.select(EventTemplate).where(EventTemplate.name == name)):
            continue
        template = EventTemplate(
            name=name,
            event_type=event_type,
            paid=paid,
            description=f"Šablona: {name}.",
            minimum_participants=minimum,
            maximum_participants=maximum,
        )
        template.qualification_requirements.append(
            EventTemplateQualificationRequirement(qualification=quals[qual], minimum_count=1)
        )
        for type_name, quantity in plan:
            template.equipment_plans.append(
                EventTemplateEquipmentPlan(equipment_type=types[type_name], quantity_required=quantity)
            )
        db.session.add(template)


def _seed_equipment(rng: random.Random, anchor: datetime, user: UserAccount) -> None:
    locations = [loc for loc, _ in HOME_LOCATIONS]
    location_weights = [w for _, w in HOME_LOCATIONS]
    index = 0
    for (type_name, _icon, _description), count in zip(EQUIPMENT_TYPES, ITEMS_PER_TYPE, strict=True):
        equipment_type = db.session.scalar(db.select(EquipmentType).where(EquipmentType.name == type_name))
        for n in range(1, count + 1):
            index += 1
            item = EquipmentItem(
                name=f"{type_name} {n}",
                equipment_type=equipment_type,
                serial_number=f"SN-{rng.randint(1000, 9999)}" if index <= 41 else None,
                home_location=rng.choices(locations, weights=location_weights)[0],
                notes="Pravidelná kontrola v pořádku." if index <= 40 else None,
            )
            if index == 7:
                item.issued_to = user
                item.issued_at = anchor - timedelta(days=5)
            db.session.add(item)


def seed_bulk(today: date) -> None:
    """Generate the synthetic dataset. Does nothing when events already exist."""
    if db.session.scalar(db.select(sa.func.count()).select_from(Event)):
        print("Events exist, skipping bulk data.")
        return
    print("Seeding synthetic dataset...")
    rng = random.Random(SEED)
    anchor = _anchor_dt(today)

    roles = {r.name: r for r in db.session.scalars(db.select(Role))}
    quals = _qualifications()
    coordinator = _dev_user("coordinator")
    member = _dev_user("member")

    hasher = UserAccount(email="hash@medcover.local", name="hash")
    hasher.set_password(DEV_PASSWORD)
    users = _generate_users(hasher.password_hash, roles, quals)
    db.session.add_all(users)
    staffing = _Staffing(rng, list(db.session.scalars(db.select(UserAccount))), [member, coordinator], quals)

    mes = {
        "general": _get_or_create(
            MasterEvent,
            "Obecné",
            description="Výchozí nadřazená akce pro akce bez specifického zařazení.",
            is_general=True,
        ),
        "festival": _get_or_create(
            MasterEvent,
            "Letní festival",
            description="Zdravotní zabezpečení letního hudebního festivalu.",
            coordinator_id=coordinator.id,
        ),
        "league": _get_or_create(MasterEvent, "Házenkářská liga", description="Domácí zápasy ligové sezony."),
        "archive": _get_or_create(
            MasterEvent, "Ukončená sezona", description="Starší akce, které již nejsou aktuální.", archived=True
        ),
    }
    types = {t.name: t for t in db.session.scalars(db.select(EquipmentType))}
    _seed_templates(quals, types)
    _seed_equipment(rng, anchor, rng.choice(users))

    future_equipment = 0
    for plan in plan_events(today, random.Random(SEED)):
        status: EventStatus = plan["status"]
        festival = plan["me"] == "festival"
        event = Event(
            name=plan["name"],
            master_event=mes[plan["me"]],
            event_type=plan["event_type"],
            status=status,
            archived=plan["archived"],
            start_datetime=plan["start"],
            end_datetime=plan["end"],
            paid=plan["paid"],
            staffing_mode=StaffingMode.CONDITIONS,
            created_by=coordinator,
            address=plan["venue"] if rng.random() < 0.84 else None,
            contact_person=(
                (f"{_fake_name(rng)} {_fake_phone(rng)}" if rng.random() < 0.5 else _fake_name(rng))
                if rng.random() < 0.69
                else None
            ),
            color="#ffe0b2" if rng.random() < 0.01 else None,
        )
        if rng.random() < 0.92:
            event.description = (
                f"Typ: {rng.choice(COVER_TYPES)} | Vozidlo/stan: {rng.choice(VEHICLES)} | "
                f"Kontakt pořadatel: {_fake_name(rng)} {_fake_phone(rng)}"
            )
        if status in (EventStatus.ASSIGNMENTS_OPEN, EventStatus.ASSIGNMENTS_CLOSED):
            event.assignments_open_datetime = anchor - timedelta(days=rng.randint(1, 30))
        if status is EventStatus.COMPLETED and rng.random() < 0.66:
            event.actual_start_datetime = plan["start"] + timedelta(minutes=rng.randint(-30, 30))
            event.actual_end_datetime = min(plan["end"] + timedelta(minutes=rng.randint(-30, 30)), anchor)
        if plan["event_type"] is EventType.TRAINING:
            event.planned_participants_count = 12
            if status is EventStatus.COMPLETED:
                event.post_event_count = 10

        # Every plan needs an RP-capable requirement; the minimum must cover the requirement sum
        if plan["event_type"] is EventType.TRAINING:
            required = ["Lektor"]
        else:
            required = ["Zdravotník", "Zelenáč"][: rng.randint(1, 2)]
        minimum = len(required) + rng.randint(0, 1)
        event.minimum_participants = minimum
        event.maximum_participants = rng.randint(minimum, 3)
        for name in required:
            event.qualification_requirements.append(
                EventQualificationRequirement(qualification=quals[name], minimum_count=1)
            )
        if status is EventStatus.COMPLETED:
            wanted = rng.randint(minimum, event.maximum_participants)
        elif status is EventStatus.ASSIGNMENTS_CLOSED:
            wanted = minimum
        elif status is EventStatus.ASSIGNMENTS_OPEN:
            wanted = rng.randint(0, minimum)
        else:
            wanted = 0
        taken: set[Any] = set()
        assigned: list[UserAccount] = []
        for i in range(wanted):
            user = staffing.pick(quals[required[i] if i < len(required) else "Zelenáč"], taken)
            if user:
                assigned.append(user)

        eligible = [u for u in assigned if u.is_rp_eligible()]
        if eligible and (status in (EventStatus.ASSIGNMENTS_CLOSED, EventStatus.COMPLETED) or rng.random() < 0.73):
            event.responsible_person = rng.choice(eligible)

        for user in assigned:
            assigned_at = min(
                plan["start"] - timedelta(days=rng.randint(3, 60)), anchor - timedelta(hours=rng.randint(0, 72))
            )
            assignment = Assignment(
                event=event,
                user=user,
                assigned_at=assigned_at,
                debriefing_email_sent=status is EventStatus.COMPLETED,
            )
            if status is EventStatus.COMPLETED and rng.random() < 0.85:
                note_status = rng.choices((0, 1, 2, 3), weights=(10, 60, 25, 5))[0]
                DebriefingRecord(
                    assignment=assignment,
                    submitted_by=user,
                    event_note_status=note_status,
                    feedback_event=(
                        " ".join(rng.sample(DEBRIEFING_SENTENCES, rng.randint(1, 3))) if note_status == 2 else None
                    ),
                    submitted_at=min(plan["end"] + timedelta(hours=rng.randint(1, 72)), anchor),
                )

        db.session.add(event)
        if festival:
            for type_name, quantity in (("Stan", 1), ("AED", 1)):
                plan_row = EventEquipmentPlan(equipment_type=types[type_name], quantity_required=quantity)
                event.equipment_plans.append(plan_row)
        elif future_equipment < 2 and status is EventStatus.ASSIGNMENTS_OPEN and plan["k"] > 0:
            future_equipment += 1
            for type_name, quantity in (("Batoh malý", 2), ("Nosítka", 1)):
                plan_row = EventEquipmentPlan(equipment_type=types[type_name], quantity_required=quantity)
                event.equipment_plans.append(plan_row)

    db.session.commit()
    for model in (UserAccount, MasterEvent, EventTemplate, Event, Assignment, DebriefingRecord):
        print(f"  {model.__tablename__}: {db.session.scalar(db.select(sa.func.count()).select_from(model))}")


def seed_db(today: date | None = None) -> None:
    seed_reference()
    seed_bulk(today or prague_today())


def main() -> None:
    # Load .env so DATABASE_URL / SECRET_KEY are available when running outside Docker
    load_dotenv()
    app = create_app("development")

    with app.app_context():
        if not app.config.get("DEV_LOGIN_ENABLED"):
            print("ERROR: DEV_LOGIN_ENABLED is not set. Refusing to seed.")
            print("Add DEV_LOGIN_ENABLED=true to your .env file.")
            sys.exit(1)

        seed_db()

        print("\nDone. Dev accounts (password: devpassword):")
        for a in DEV_ACCOUNTS:
            print(f"  {a['role']:12s}  {a['email']}")


if __name__ == "__main__":
    main()
