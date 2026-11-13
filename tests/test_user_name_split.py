import importlib.util
import json
import re
import zipfile
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import select, text

from app.backup import _upgrade_legacy_row, downloaded_backup, export_to_zip, restore_from_zip
from app.extensions import db
from app.models.assignment import Assignment
from app.models.audit import AuditLogEntry
from app.models.event import Event, EventStatus
from app.models.invite import RegistrationInvite
from app.models.role import Role
from app.models.settings import get_settings
from app.models.user import (
    NAME_PART_MAX_LENGTH,
    NAME_PLACEHOLDER,
    UserAccount,
    display_name,
    parse_name_parts,
    split_full_name,
)
from app.queries import active_users_list, user_name_matches
from app.routes.import_events import _name_lookup
from app.utils import person_sort_key
from tests.conftest import _get_csrf, _make_event_in_status, _make_event_with_spot, _make_master_event, _make_user
from tests.test_import_events import _minimal_event, _post_confirm

MIGRATION_FILE = "7e2f4a9c1b35_split_user_name_into_surname_and_first_name.py"


def _load_migration():
    path = Path("migrations/versions") / MIGRATION_FILE
    spec = importlib.util.spec_from_file_location("split_name_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _migration():
    module = _load_migration()
    module.op = Operations(MigrationContext.configure(db.session.connection()))
    return module


class TestHelpers:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("", (NAME_PLACEHOLDER, NAME_PLACEHOLDER)),
            ("   ", (NAME_PLACEHOLDER, NAME_PLACEHOLDER)),
            (None, (NAME_PLACEHOLDER, NAME_PLACEHOLDER)),
            ("Madonna", ("Madonna", NAME_PLACEHOLDER)),
            ("Testovací Rudolf", ("Testovací", "Rudolf")),
            ("Testovacíková Marie Anna", ("Testovacíková", "Marie Anna")),
            ("  Testovacíková   Marie  Anna ", ("Testovacíková", "Marie Anna")),
            (
                "\tSmyšlený\u00a0Oldřich",
                ("Smyšlený", "Oldřich"),
            ),  # tab and NBSP (written as an escape, it is invisible) are whitespace in legacy data
        ],
    )
    def test_split_full_name(self, raw, expected):
        assert split_full_name(raw) == expected

    def test_display_name(self):
        assert display_name("Testovací", "Rudolf") == "Testovací Rudolf"
        assert display_name("Testovací", "") == "Testovací"
        assert display_name(None, None) == ""

    def test_parse_trims_and_collapses(self):
        assert parse_name_parts("  van   der Berg ", " Marie  Anna ") == ("van der Berg", "Marie Anna")

    def test_parse_errors_in_field_order(self):
        with pytest.raises(ValueError, match="Příjmení nesmí být prázdné"):
            parse_name_parts("", "")
        with pytest.raises(ValueError, match="Jméno nesmí být prázdné"):
            parse_name_parts("Testovací", "   ")

    def test_parse_length_limits(self):
        assert parse_name_parts("a" * NAME_PART_MAX_LENGTH, "Rudolf")[0] == "a" * NAME_PART_MAX_LENGTH
        with pytest.raises(ValueError, match="Příjmení je příliš dlouhé"):
            parse_name_parts("a" * (NAME_PART_MAX_LENGTH + 1), "Rudolf")
        with pytest.raises(ValueError, match="Jméno je příliš dlouhé"):
            parse_name_parts("Testovací", "a" * (NAME_PART_MAX_LENGTH + 1))

    def test_parse_rejects_byte_overflow(self):
        with pytest.raises(ValueError) as exc:
            parse_name_parts("Testovací", "😀" * NAME_PART_MAX_LENGTH)
        assert str(exc.value) == "Jméno obsahuje příliš mnoho speciálních znaků (např. emoji), zkraťte ho."

    def test_parse_rejects_surname_byte_overflow(self):
        with pytest.raises(ValueError, match="^Příjmení obsahuje příliš mnoho speciálních znaků"):
            parse_name_parts("😀" * 64, "Rudolf")

    def test_length_message_states_character_limit(self):
        with pytest.raises(ValueError) as exc:
            parse_name_parts("Testovací", "a" * (NAME_PART_MAX_LENGTH + 1))
        assert str(exc.value) == f"Jméno je příliš dlouhé (nejvýše {NAME_PART_MAX_LENGTH} znaků)."

    def test_initials(self):
        assert UserAccount(surname="Testovací", first_name="Rudolf").initials == "RT"
        assert UserAccount(surname="Čarodějný", first_name="Zdeněk").initials == "ZČ"
        assert UserAccount(surname="Testovací", first_name=NAME_PLACEHOLDER).initials == "T"
        assert UserAccount(surname="-", first_name="").initials == "?"

    def test_person_sort_key_orders_surname_before_first_name(self):
        people = [
            ("Testovacíková", "Božena"),
            ("Testovací", "Rudolf"),
            ("Testovací", "Albert"),
            ("Čarodějný", "Zdeněk"),
            ("Cestovní", "Bohumil"),
        ]
        ordered = sorted(people, key=lambda p: person_sort_key(*p))
        assert ordered == [
            ("Cestovní", "Bohumil"),
            ("Čarodějný", "Zdeněk"),
            ("Testovací", "Albert"),
            ("Testovací", "Rudolf"),
            ("Testovacíková", "Božena"),
        ]

    def test_migration_placeholder_matches_model(self):
        assert _load_migration().PLACEHOLDER == NAME_PLACEHOLDER


class TestHybridName:
    def test_python_and_sql_agree(self, app):
        with app.app_context():
            user = _make_user("rudolf@example.com", "Testovací Rudolf", Role.MEMBER)
            assert user.name == "Testovací Rudolf"
            assert db.session.scalar(select(UserAccount.name).where(UserAccount.id == user.id)) == "Testovací Rudolf"

    def test_name_is_read_only(self):
        user = UserAccount(surname="Testovací", first_name="Rudolf")
        with pytest.raises(AttributeError):
            user.name = "Rudolf Testovací"
        with pytest.raises(AttributeError):
            UserAccount(name="Testovací Rudolf")

    def test_search_matches_both_orders(self, app):
        with app.app_context():
            user = _make_user("rudolf@example.com", "Testovací Rudolf", Role.MEMBER)
            for query in ("Testovací", "Rudolf", "Testovací Rudolf", "Rudolf Testovací", "testovací rudolf"):
                found = db.session.scalars(select(UserAccount).where(user_name_matches(query))).all()
                assert user in found, query
            assert not db.session.scalars(select(UserAccount).where(user_name_matches("Rudolf X"))).all()

    def test_active_users_sorted_by_surname_then_first_name(self, app):
        with app.app_context():
            for i, name in enumerate(
                ["Testovacíková Božena", "Testovací Rudolf", "Testovací Albert", "Čarodějný Zdeněk", "Cestovní Bohumil"]
            ):
                _make_user(f"u{i}@example.com", name, Role.MEMBER)
            assert [u.name for u in active_users_list()] == [
                "Cestovní Bohumil",
                "Čarodějný Zdeněk",
                "Testovací Albert",
                "Testovací Rudolf",
                "Testovacíková Božena",
            ]


class TestMigration:
    LEGACY = {
        "a@example.com": "Testovací Rudolf",
        "b@example.com": "Procházková Jana",
        "c@example.com": "Testovacíková  Marie Anna ",
        "d@example.com": "Madonna",
        # tab and NBSP (written as an escape, it is invisible) in legacy data
        "e@example.com": "\tSmyšlený\u00a0Oldřich",
        "f@example.com": "",
    }

    @staticmethod
    def _run(direction):
        getattr(_migration(), direction)()
        db.session.commit()

    def test_round_trip_matches_python_split(self, app):
        with app.app_context():
            for email in self.LEGACY:
                _make_user(email, "Testovací Rudolf", Role.MEMBER)
            count = db.session.scalar(text("SELECT COUNT(*) FROM user_account"))
            self._run("downgrade")
            try:
                for email, legacy in self.LEGACY.items():
                    db.session.execute(
                        text("UPDATE user_account SET name = :n WHERE email = :e"), {"n": legacy, "e": email}
                    )
                db.session.commit()
                self._run("upgrade")
                rows = {
                    r.email: (r.surname, r.first_name)
                    for r in db.session.execute(text("SELECT email, surname, first_name FROM user_account"))
                }
                assert len(rows) == count
                for email, legacy in self.LEGACY.items():
                    assert rows[email] == split_full_name(legacy), legacy
                self._run("downgrade")
                names = {r.email: r.name for r in db.session.execute(text("SELECT email, name FROM user_account"))}
                assert names["a@example.com"] == "Testovací Rudolf"
                assert names["c@example.com"] == "Testovacíková Marie Anna"
                assert names["d@example.com"] == "Madonna"
            finally:
                db.session.rollback()
                has_surname = db.session.scalar(
                    text(
                        "SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS "
                        "WHERE TABLE_NAME = 'user_account' AND COLUMN_NAME = 'surname'"
                    )
                )
                if not has_surname:
                    self._run("upgrade")

    def test_downgrade_refuses_names_that_do_not_fit(self, app):
        with app.app_context():
            user = _make_user("long@example.com", "Testovací Rudolf", Role.MEMBER)
            user.surname = "Ž" * 99
            user.first_name = "Ž" * 99
            db.session.commit()
            with pytest.raises(RuntimeError, match="Cannot downgrade"):
                _migration().downgrade()


class TestBackupCompatibility:
    def test_upgrade_legacy_row(self):
        assert _upgrade_legacy_row("user_account", {"name": "Testovací Rudolf"}) == {
            "name": "Testovací Rudolf",
            "surname": "Testovací",
            "first_name": "Rudolf",
        }
        assert _upgrade_legacy_row("user_account", {"name": "Madonna"})["first_name"] == NAME_PLACEHOLDER
        current = {"surname": "Testovací", "first_name": "Rudolf"}
        assert _upgrade_legacy_row("user_account", current) is current
        other = {"name": "Akce"}
        assert _upgrade_legacy_row("event", other) is other

    def test_old_backup_is_split_on_restore(self, app, tmp_path):
        with app.app_context():
            _make_user("rudolf@example.com", "Testovací Rudolf", Role.MEMBER)
            _make_user("madonna@example.com", "Testovací Jiří", Role.MEMBER)
            blob = export_to_zip()
            with downloaded_backup(blob) as path:
                with zipfile.ZipFile(path) as zf:
                    payload = json.loads(zf.read("backup.json"))
            for row in payload["tables"]["user_account"]:
                row["name"] = "Madonna" if row["email"].startswith("madonna") else "Testovací Rudolf"
                del row["surname"], row["first_name"]
            legacy = tmp_path / "legacy.zip"
            with zipfile.ZipFile(legacy, "w") as zf:
                zf.writestr("backup.json", json.dumps(payload))
            restore_from_zip(legacy)
            db.session.remove()
            users = {u.email: u for u in db.session.scalars(select(UserAccount))}
            assert users["rudolf@example.com"].name == "Testovací Rudolf"
            assert users["madonna@example.com"].surname == "Madonna"
            assert users["madonna@example.com"].first_name == NAME_PLACEHOLDER

    def test_new_backup_round_trips(self, app):
        with app.app_context():
            _make_user("rudolf@example.com", "Testovací Rudolf", Role.MEMBER)
            blob = export_to_zip()
            with downloaded_backup(blob) as path:
                restore_from_zip(path)
            db.session.remove()
            user = db.session.scalar(select(UserAccount).where(UserAccount.email == "rudolf@example.com"))
            assert (user.surname, user.first_name) == ("Testovací", "Rudolf")


class TestForms:
    def test_profile_and_create_forms_have_two_inputs(self, app, admin_client):
        for url in ("/users/profile", "/users/create"):
            html = admin_client.get(url).data.decode()
            assert html.index('name="surname"') < html.index('name="first_name"')
            assert "Celé jméno" not in html

    def test_admin_edit_form_has_two_prefilled_inputs(self, app, admin_client):
        with app.app_context():
            user = _make_user("rudolf@example.com", "Testovací Rudolf", Role.MEMBER)
            user_id = user.id
        html = admin_client.get(f"/users/{user_id}").data.decode()
        assert 'id="edit_surname"' in html and 'value="Testovací"' in html
        assert 'id="edit_first_name"' in html and 'value="Rudolf"' in html

    def test_register_form_and_validation_repopulate(self, app, client):
        with app.app_context():
            admin = _make_user("admin@example.com", "Testovací Admin", Role.ADMIN)
            invite = RegistrationInvite(email="new@example.com", created_by_id=admin.id)
            db.session.add(invite)
            db.session.commit()
            token = invite.token
        html = client.get(f"/auth/register/{token}").data.decode()
        assert 'name="surname"' in html and 'name="first_name"' in html
        resp = client.post(
            f"/auth/register/{token}",
            data={"surname": "Testovací", "first_name": " ", "password": "securepass99", "password2": "securepass99"},
        )
        assert resp.status_code == 200
        assert "Jméno nesmí být prázdné" in resp.data.decode()
        assert 'value="Testovací"' in resp.data.decode()
        with app.app_context():
            assert db.session.scalar(select(UserAccount).where(UserAccount.email == "new@example.com")) is None

    def test_register_creates_user_with_both_parts(self, app, client):
        with app.app_context():
            admin = _make_user("admin@example.com", "Testovací Admin", Role.ADMIN)
            invite = RegistrationInvite(email="new@example.com", created_by_id=admin.id)
            db.session.add(invite)
            db.session.commit()
            token = invite.token
        client.post(
            f"/auth/register/{token}",
            data={
                "surname": "Vymyšlený",
                "first_name": "Jiří",
                "password": "securepass99",
                "password2": "securepass99",
            },
        )
        with app.app_context():
            user = db.session.scalar(select(UserAccount).where(UserAccount.email == "new@example.com"))
            assert (user.surname, user.first_name) == ("Vymyšlený", "Jiří")

    def test_setup_creates_admin_from_two_fields(self, app, client):
        with app.app_context():
            get_settings().setup_complete = False
            db.session.commit()
        try:
            html = client.get("/setup/step3").data.decode()
            assert html.index('name="surname"') < html.index('name="first_name"')
            resp = client.post(
                "/setup/step3",
                data={
                    "surname": "Testovací",
                    "first_name": "",
                    "email": "root@example.com",
                    "password": "securepass99",
                    "password2": "securepass99",
                },
            )
            assert resp.status_code == 200
            assert "Jméno nesmí být prázdné" in resp.data.decode()
            assert 'value="root@example.com"' in resp.data.decode()
            client.post(
                "/setup/step3",
                data={
                    "surname": "Testovací",
                    "first_name": "Rudolf",
                    "email": "root@example.com",
                    "password": "securepass99",
                    "password2": "securepass99",
                },
            )
            with app.app_context():
                user = db.session.scalar(select(UserAccount).where(UserAccount.email == "root@example.com"))
                assert (user.surname, user.first_name) == ("Testovací", "Rudolf")
        finally:
            with app.app_context():
                get_settings().setup_complete = True
                db.session.commit()

    def test_overlong_surname_on_create_is_rejected_without_error(self, app, admin_client):
        resp = admin_client.post(
            "/users/create",
            data={"surname": "😀" * NAME_PART_MAX_LENGTH, "first_name": "Rudolf", "email": "emoji@example.com"},
        )
        assert resp.status_code == 200
        assert "Příjmení obsahuje příliš mnoho speciálních znaků" in resp.data.decode()

    def test_templates_have_no_single_name_input(self):
        for template in Path("app/templates").rglob("*.html"):
            text_ = template.read_text(encoding="utf-8")
            assert "Celé jméno" not in text_, template
            assert "full_name" not in text_, template


def test_verify_schema_passes(app):
    result = app.test_cli_runner().invoke(args=["verify-schema"])
    assert result.exit_code == 0, result.output
    assert "Schema OK" in result.output


def test_no_name_writes_in_product_code():
    for root in ("app", "scripts"):
        for source in Path(root).rglob("*.py"):
            assert "UserAccount(name=" not in source.read_text(encoding="utf-8"), source


def _member(app, email="rudolf@example.com", name="Testovací Rudolf"):
    with app.app_context():
        user = _make_user(email, name, Role.MEMBER)
        return str(user.id), user.version


class TestAdminSaveValidation:
    @pytest.mark.parametrize(
        ("surname", "first_name", "message"),
        [
            ("", "Rudolf", "Příjmení nesmí být prázdné"),
            ("   ", "Rudolf", "Příjmení nesmí být prázdné"),
            ("Testovací", "", "Jméno nesmí být prázdné"),
            ("Testovací", "  \t ", "Jméno nesmí být prázdné"),
            ("a" * (NAME_PART_MAX_LENGTH + 1), "Rudolf", "Příjmení je příliš dlouhé"),
            ("Testovací", "😀" * NAME_PART_MAX_LENGTH, "Jméno obsahuje příliš mnoho speciálních znaků"),
        ],
    )
    def test_invalid_parts_save_nothing(self, app, admin_client, surname, first_name, message):
        user_id, version = _member(app)
        resp = admin_client.post(
            f"/users/{user_id}/save",
            data={
                "surname": surname,
                "first_name": first_name,
                "email": "rudolf@example.com",
                "phone": "",
                "version": str(version),
            },
            follow_redirects=True,
        )
        assert resp.status_code == 200
        assert message in resp.data.decode()
        with app.app_context():
            user = db.session.get(UserAccount, user_id)
            assert (user.surname, user.first_name, user.version) == ("Testovací", "Rudolf", version)

    def test_successful_change_of_both_parts_bumps_version_once(self, app, admin_client):
        user_id, version = _member(app)
        resp = admin_client.post(
            f"/users/{user_id}/save",
            data={
                "surname": "Vymyšlený",
                "first_name": "Jiří",
                "email": "rudolf@example.com",
                "phone": "",
                "version": str(version),
            },
        )
        assert resp.status_code == 302
        with app.app_context():
            user = db.session.get(UserAccount, user_id)
            assert (user.surname, user.first_name) == ("Vymyšlený", "Jiří")
            assert user.version == version + 1
            entries = db.session.scalars(db.select(AuditLogEntry).where(AuditLogEntry.entity_id == user_id)).all()
            entry = next(e for e in entries if "surname" in (e.changes_json or {}))
            assert {"surname", "first_name"} <= set(entry.changes_json)
            assert "name" not in entry.changes_json


class TestProfileLimits:
    def test_overlong_parts_rejected_without_error(self, app, coordinator_client):
        with app.app_context():
            user = db.session.scalar(select(UserAccount).where(UserAccount.email == "coordinator@test.com"))
            before = (user.surname, user.first_name, user.version)
        for surname, first_name, message in (
            ("a" * 101, "Rudolf", "nejvýše"),
            ("Testovací", "😀" * 100, "příliš mnoho speciálních znaků"),
        ):
            resp = coordinator_client.post(
                "/users/profile",
                data={"action": "profile", "surname": surname, "first_name": first_name, "version": str(before[2])},
                follow_redirects=True,
            )
            assert resp.status_code == 200
            assert message in resp.data.decode()
        with app.app_context():
            user = db.session.scalar(select(UserAccount).where(UserAccount.email == "coordinator@test.com"))
            assert (user.surname, user.first_name, user.version) == before


class TestRegisterAndSetupLimits:
    def test_register_rejects_overlong_and_empty_surname(self, app, client):
        with app.app_context():
            admin = _make_user("admin@example.com", "Testovací Admin", Role.ADMIN)
            invite = RegistrationInvite(email="new@example.com", created_by_id=admin.id)
            db.session.add(invite)
            db.session.commit()
            token = invite.token
        passwords = {"password": "securepass99", "password2": "securepass99"}
        resp = client.post(f"/auth/register/{token}", data={"surname": "", "first_name": "Rudolf", **passwords})
        assert resp.status_code == 200 and "Příjmení nesmí být prázdné" in resp.data.decode()
        resp = client.post(
            f"/auth/register/{token}", data={"surname": "Testovací", "first_name": "x" * 101, **passwords}
        )
        assert resp.status_code == 200 and "Jméno je příliš dlouhé" in resp.data.decode()
        with app.app_context():
            assert db.session.scalar(select(UserAccount).where(UserAccount.email == "new@example.com")) is None

    def test_setup_rejects_empty_surname_and_overlong_first_name(self, app, client):
        with app.app_context():
            get_settings().setup_complete = False
            db.session.commit()
        try:
            base = {"email": "root@example.com", "password": "securepass99", "password2": "securepass99"}
            resp = client.post("/setup/step3", data={"surname": " ", "first_name": "Rudolf", **base})
            assert resp.status_code == 200 and "Příjmení nesmí být prázdné" in resp.data.decode()
            resp = client.post("/setup/step3", data={"surname": "Testovací", "first_name": "x" * 101, **base})
            assert resp.status_code == 200 and "Jméno je příliš dlouhé" in resp.data.decode()
            with app.app_context():
                assert db.session.scalar(select(UserAccount).where(UserAccount.email == "root@example.com")) is None
        finally:
            with app.app_context():
                get_settings().setup_complete = True
                db.session.commit()

    def test_create_rejects_empty_parts(self, app, admin_client):
        for surname, first_name, message in (("", "Rudolf", "Příjmení"), ("Testovací", " ", "Jméno")):
            resp = admin_client.post(
                "/users/create", data={"surname": surname, "first_name": first_name, "email": "empty@example.com"}
            )
            assert resp.status_code == 200
            assert f"{message} nesmí být prázdné" in resp.data.decode()
        with app.app_context():
            assert db.session.scalar(select(UserAccount).where(UserAccount.email == "empty@example.com")) is None


class TestListSortAndSearch:
    PEOPLE = (
        ("Testovací", "Rudolf"),
        ("Testovací", "Albert"),
        ("Testovacíková", "Božena"),
        ("Čarodějný", "Zdeněk"),
        ("Cestovní", "Bohumil"),
    )

    def _seed(self, app):
        with app.app_context():
            for i, (surname, first_name) in enumerate(self.PEOPLE):
                user = UserAccount(email=f"p{i}@example.com", surname=surname, first_name=first_name, is_active=True)
                user.set_password("pass1234")
                db.session.add(user)
            db.session.commit()

    def _order(self, html):
        names = [f"{s} {f}" for s, f in self.PEOPLE]
        return sorted(names, key=html.index)

    def test_sort_by_surname_then_first_name(self, app, admin_client):
        self._seed(app)
        expected = [
            "Cestovní Bohumil",
            "Čarodějný Zdeněk",
            "Testovací Albert",
            "Testovací Rudolf",
            "Testovacíková Božena",
        ]
        html = admin_client.get("/users/?sort=name&dir=asc").data.decode()
        assert self._order(html) == expected
        html = admin_client.get("/users/?sort=name&dir=desc").data.decode()
        assert self._order(html) == expected[::-1]

    @pytest.mark.parametrize(
        "query", ["Testovací", "testovací rudolf", "Rudolf", "Testovací Rudolf", "Rudolf Testovací", "RUDOLF TESTOVACÍ"]
    )
    def test_search_finds_user_in_either_order(self, app, admin_client, query):
        self._seed(app)
        html = admin_client.get("/users/", query_string={"q": query}).data.decode()
        assert "p0@example.com" in html

    def test_search_does_not_match_unrelated_combination(self, app, admin_client):
        self._seed(app)
        html = admin_client.get("/users/", query_string={"q": "Rudolf Čarodějný"}).data.decode()
        assert "p0@example.com" not in html and "p3@example.com" not in html

    def test_search_still_respects_archived_filter(self, app, admin_client):
        self._seed(app)
        with app.app_context():
            user = db.session.scalar(select(UserAccount).where(UserAccount.email == "p0@example.com"))
            user.is_archived = True
            db.session.commit()
        html = admin_client.get("/users/", query_string={"q": "Testovací Rudolf"}).data.decode()
        assert "p0@example.com" not in html


class TestDisplayName:
    def test_pages_show_surname_first_display_name(self, app, admin_client):
        with app.app_context():
            rp = _make_user("rp@example.com", "Testovací Rudolf", Role.MEMBER)
            rp_id = str(rp.id)
        event_id, _ = _make_event_with_spot(app, name="Zkušební akce")
        with app.app_context():
            db.session.get(Event, event_id).responsible_person_id = rp_id
            db.session.commit()
        for url in ("/users/", f"/users/{rp_id}", f"/events/{event_id}", "/users/profile"):
            resp = admin_client.get(url)
            assert resp.status_code == 200, url
        assert "Testovací Rudolf" in admin_client.get("/users/").data.decode()
        detail = admin_client.get(f"/users/{rp_id}").data.decode()
        assert "Testovací Rudolf" in detail and "Rudolf Testovací" not in detail
        assert "Testovací Rudolf" in admin_client.get(f"/events/{event_id}").data.decode()
        assert "Test Admin" in admin_client.get("/users/profile").data.decode()

    def test_profile_prefills_both_parts_and_disables_without_permission(self, app, member_client):
        html = member_client.get("/users/profile").data.decode()
        assert html.index("Příjmení") < html.index("Jméno")
        assert 'value="Test"' in html and 'value="Member"' in html
        assert 'name="surname"' not in html and 'name="first_name"' not in html


class TestImportNewUsers:
    def _preview(self, admin_client, name):
        payload = {
            "version": 2,
            "users": [{"gs_name": name, "name": name, "email": "novy@example.com", "phone": None}],
            "events": [],
        }
        return admin_client.post(
            "/import/events/preview",
            data={"json_data": json.dumps(payload), "csrf_token": _get_csrf(admin_client, "/import/events/")},
        )

    def test_preview_offers_split_editable_inputs(self, app, admin_client):
        html = self._preview(admin_client, "Příklad Jiří Ondřej").data.decode()
        assert 'name="user_0_surname"' in html and 'value="Příklad"' in html
        assert 'name="user_0_first_name"' in html and 'value="Jiří Ondřej"' in html
        assert html.index("user_0_surname") < html.index("user_0_first_name")
        assert 'name="user_0_name"' not in html

    def test_preview_submits_name_as_sheet_key_without_gs_name(self, app, admin_client):
        payload = {"version": 2, "users": [{"name": "Příklad Jiří", "email": "novy@example.com"}], "events": []}
        html = admin_client.post(
            "/import/events/preview",
            data={"json_data": json.dumps(payload), "csrf_token": _get_csrf(admin_client, "/import/events/")},
        ).data.decode()
        assert '<input type="hidden" name="user_0_gs_name" value="Příklad Jiří">' in html

    def _confirm(self, app, admin_client, **parts):
        me_id = _make_master_event(app)
        return _post_confirm(
            app,
            admin_client,
            events=[],
            users=[{"gs_name": "Příklad Jiří", "email": "novy@example.com", **parts}],
            master_event_id=me_id,
        )

    def test_confirm_creates_user_from_edited_parts(self, app, admin_client):
        resp = self._confirm(app, admin_client, surname="Upravený", first_name="Ondřej")
        assert resp.status_code == 302
        with app.app_context():
            user = db.session.scalar(select(UserAccount).where(UserAccount.email == "novy@example.com"))
            assert (user.surname, user.first_name) == ("Upravený", "Ondřej")

    @pytest.mark.parametrize(("surname", "first_name"), [("Příklad", ""), ("", "Jiří"), ("a" * 101, "Jiří")])
    def test_confirm_skips_row_with_missing_or_overlong_part(self, app, admin_client, surname, first_name):
        resp = self._confirm(app, admin_client, surname=surname, first_name=first_name)
        assert resp.status_code == 302
        with app.app_context():
            assert db.session.scalar(select(UserAccount).where(UserAccount.email == "novy@example.com")) is None

    def test_confirm_reports_rejected_row(self, app, admin_client):
        self._confirm(app, admin_client, surname="Příklad", first_name="")
        flashes = admin_client.get("/events/").data.decode()
        assert "Tito uživatelé nebyli importováni: „Příklad Jiří“ (Jméno nesmí být prázdné.)" in flashes


class TestPlaceholderRejected:
    @pytest.mark.parametrize(("surname", "first_name"), [(NAME_PLACEHOLDER, "Rudolf"), ("Testovací", NAME_PLACEHOLDER)])
    def test_parse_rejects_literal_placeholder(self, surname, first_name):
        with pytest.raises(ValueError, match="„\\(doplnit\\)“"):
            parse_name_parts(surname, first_name)

    @pytest.mark.parametrize("variant", ["(Doplnit)", "( doplnit )", "(DOPLNIT)", "(  doplnit)"])
    def test_parse_rejects_placeholder_case_and_spacing_variants(self, variant):
        with pytest.raises(ValueError, match="„\\(doplnit\\)“"):
            parse_name_parts(variant, "Rudolf")
        with pytest.raises(ValueError, match="„\\(doplnit\\)“"):
            parse_name_parts("Testovací", variant)

    def test_parse_can_allow_placeholder_first_name(self):
        assert parse_name_parts("Testovací", NAME_PLACEHOLDER, allow_placeholder_first_name=True) == (
            "Testovací",
            NAME_PLACEHOLDER,
        )

    @pytest.mark.parametrize("variant", ["(Doplnit)", "( doplnit )", "(DOPLNIT)"])
    def test_allowed_placeholder_variant_is_canonicalised(self, variant):
        assert parse_name_parts("Testovací", variant, allow_placeholder_first_name=True) == (
            "Testovací",
            NAME_PLACEHOLDER,
        )

    def test_profile_form_rejects_placeholder(self, app, coordinator_client):
        resp = coordinator_client.post(
            "/users/profile",
            data={"action": "profile", "surname": "Testovací", "first_name": NAME_PLACEHOLDER, "version": "1"},
            follow_redirects=True,
        )
        assert "Jméno nesmí být „(doplnit)“." in resp.data.decode()


class TestImportSignupsResolve:
    """Signups and responsible persons resolve to the user their preview row became."""

    def _import(self, app, admin_client, users, signups, rp=None):
        me_id = _make_master_event(app)
        ev = _minimal_event(name="Importovaná akce")
        ev["signups"] = signups
        ev["responsible_person_id"] = rp
        resp = _post_confirm(app, admin_client, events=[ev], users=users, master_event_id=me_id)
        assert resp.status_code == 302
        return resp

    def _assigned_emails(self, app):
        with app.app_context():
            event = db.session.scalar(select(Event).where(Event.name == "Importovaná akce"))
            rows = db.session.scalars(select(Assignment).where(Assignment.spot_id.in_([s.id for s in event.spots])))
            return sorted(a.user.email for a in rows)

    def test_single_word_sheet_name(self, app, admin_client):
        self._import(
            app,
            admin_client,
            users=[{"gs_name": "Pepa", "name": "Pepa", "email": "pepa@example.com"}],
            signups=["Pepa"],
        )
        with app.app_context():
            user = db.session.scalar(select(UserAccount).where(UserAccount.email == "pepa@example.com"))
            assert (user.surname, user.first_name) == ("Pepa", NAME_PLACEHOLDER)
        assert self._assigned_emails(app) == ["pepa@example.com"]

    def test_extra_whitespace_in_sheet_name(self, app, admin_client):
        self._import(
            app,
            admin_client,
            users=[{"gs_name": "Testovací  Rudolf", "name": "Testovací  Rudolf", "email": "jan@example.com"}],
            signups=["Testovací  Rudolf"],
        )
        assert self._assigned_emails(app) == ["jan@example.com"]

    def test_edited_parts_differ_from_sheet_string(self, app, admin_client):
        self._import(
            app,
            admin_client,
            users=[
                {"gs_name": "Příklad Jiří", "surname": "Upravený", "first_name": "Ondřej", "email": "u@example.com"}
            ],
            signups=["Příklad Jiří"],
        )
        assert self._assigned_emails(app) == ["u@example.com"]

    def test_responsible_person_resolves_to_edited_user(self, app, admin_client):
        self._import(
            app,
            admin_client,
            users=[
                {"gs_name": "Příklad Jiří", "surname": "Upravený", "first_name": "Ondřej", "email": "u@example.com"}
            ],
            signups=[],
            rp="name:Příklad  Jiří",
        )
        assert self._assigned_emails(app) == ["u@example.com"]

    def test_existing_placeholder_user_matched_by_surname(self, app, admin_client):
        with app.app_context():
            user = _make_user("madonna@example.com", "Madonna", Role.MEMBER)
            assert user.first_name == NAME_PLACEHOLDER
        self._import(app, admin_client, users=[], signups=["Madonna"])
        assert self._assigned_emails(app) == ["madonna@example.com"]

    def test_existing_user_row_maps_sheet_name_to_db_id(self, app, admin_client):
        with app.app_context():
            user_id = str(_make_user("existing@example.com", "Skutečný Oldřich", Role.MEMBER).id)
        self._import(
            app,
            admin_client,
            users=[{"gs_name": "Jiný Zápis", "db_id": user_id, "name": "Skutečný Oldřich"}],
            signups=["Jiný Zápis"],
        )
        assert self._assigned_emails(app) == ["existing@example.com"]

    def test_two_sheet_names_for_one_account_assign_it_once(self, app, admin_client):
        # The second row matches the first new user by email, so both sheet names map to one account.
        self._import(
            app,
            admin_client,
            users=[
                {"gs_name": "Příklad Jiří", "name": "Příklad Jiří", "email": "u@example.com"},
                {"gs_name": "Příklad J.", "name": "Příklad J.", "email": "u@example.com"},
            ],
            signups=["Příklad Jiří", "Příklad J."],
        )
        assert self._assigned_emails(app) == ["u@example.com"]

    def test_duplicate_signup_does_not_push_out_a_later_one(self, app, admin_client):
        # A, A, B without RP: three names but two people, so both fit the two Zelenáč spots.
        self._import(
            app,
            admin_client,
            users=[
                {"gs_name": "Příklad Jiří", "name": "Příklad Jiří", "email": "a@example.com"},
                {"gs_name": "Příklad J.", "name": "Příklad J.", "email": "a@example.com"},
                {"gs_name": "Ukázková Eva", "name": "Ukázková Eva", "email": "b@example.com"},
            ],
            signups=["Příklad Jiří", "Příklad J.", "Ukázková Eva"],
        )
        assert self._assigned_emails(app) == ["a@example.com", "b@example.com"]

    def test_three_distinct_signups_without_rp_are_all_assigned(self, app, admin_client):
        # A, A, B, C without RP: three distinct people need three Zelenáč spots.
        self._import(
            app,
            admin_client,
            users=[
                {"gs_name": "Příklad Jiří", "name": "Příklad Jiří", "email": "a@example.com"},
                {"gs_name": "Příklad J.", "name": "Příklad J.", "email": "a@example.com"},
                {"gs_name": "Ukázková Eva", "name": "Ukázková Eva", "email": "b@example.com"},
                {"gs_name": "Vymyšlený Jiří", "name": "Vymyšlený Jiří", "email": "c@example.com"},
            ],
            signups=["Příklad Jiří", "Příklad J.", "Ukázková Eva", "Vymyšlený Jiří"],
        )
        assert self._assigned_emails(app) == ["a@example.com", "b@example.com", "c@example.com"]

    def test_single_word_placeholder_variant_is_matched_on_reimport(self, app, admin_client):
        self._import(
            app,
            admin_client,
            users=[{"gs_name": "Pepa", "surname": "Pepa", "first_name": "( Doplnit )", "email": "pepa@example.com"}],
            signups=[],
        )
        with app.app_context():
            user = db.session.scalar(select(UserAccount).where(UserAccount.email == "pepa@example.com"))
            assert user.first_name == NAME_PLACEHOLDER
            assert _name_lookup([user])["pepa"] is user

    def test_responsible_person_listed_as_signup_is_assigned_once(self, app, admin_client):
        self._import(
            app,
            admin_client,
            users=[{"gs_name": "Příklad Jiří", "name": "Příklad Jiří", "email": "u@example.com"}],
            signups=["Příklad Jiří"],
            rp="name:Příklad Jiří",
        )
        assert self._assigned_emails(app) == ["u@example.com"]

    def test_unresolved_signup_is_reported(self, app, admin_client):
        resp = self._import(app, admin_client, users=[], signups=["Neznámý Nikdo"])
        flashes = admin_client.get("/events/").data.decode()
        assert "Nepodařilo se přiřadit k existujícímu uživateli: „Neznámý Nikdo“." in flashes
        assert resp.status_code == 302
        assert self._assigned_emails(app) == []

    def test_empty_sheet_name_is_skipped(self, app, admin_client):
        self._import(app, admin_client, users=[{"gs_name": "", "name": "", "email": "prazdny@example.com"}], signups=[])
        with app.app_context():
            assert db.session.scalar(select(UserAccount).where(UserAccount.email == "prazdny@example.com")) is None

    def test_preview_matches_placeholder_user_and_blank_name_has_empty_inputs(self, app, admin_client):
        with app.app_context():
            _make_user("madonna@example.com", "Madonna", Role.MEMBER)
        html = TestImportNewUsers._preview(None, admin_client, "Madonna").data.decode()
        assert "madonna@example.com" in html
        html = TestImportNewUsers._preview(None, admin_client, "").data.decode()
        assert NAME_PLACEHOLDER not in html


class TestPlaceholderInForms:
    """The migration placeholder is never accepted as typed input and legacy users can replace it."""

    def test_admin_edit_rejects_placeholder(self, app, admin_client):
        user_id, version = _member(app)
        for surname, first_name, message in (
            (NAME_PLACEHOLDER, "Rudolf", "Příjmení nesmí být „(doplnit)“."),
            ("Testovací", NAME_PLACEHOLDER, "Jméno nesmí být „(doplnit)“."),
        ):
            resp = admin_client.post(
                f"/users/{user_id}/save",
                data={
                    "surname": surname,
                    "first_name": first_name,
                    "email": "rudolf@example.com",
                    "phone": "",
                    "version": str(version),
                },
                follow_redirects=True,
            )
            assert message in resp.data.decode()
        with app.app_context():
            user = db.session.get(UserAccount, user_id)
            assert (user.surname, user.first_name, user.version) == ("Testovací", "Rudolf", version)

    def test_create_rejects_placeholder(self, app, admin_client):
        for surname, first_name in ((NAME_PLACEHOLDER, "Rudolf"), ("Testovací", NAME_PLACEHOLDER)):
            resp = admin_client.post(
                "/users/create", data={"surname": surname, "first_name": first_name, "email": "ph@example.com"}
            )
            assert resp.status_code == 200
            assert "nesmí být „(doplnit)“" in resp.data.decode()
        with app.app_context():
            assert db.session.scalar(select(UserAccount).where(UserAccount.email == "ph@example.com")) is None

    def test_register_rejects_placeholder(self, app, client):
        with app.app_context():
            admin = _make_user("admin@example.com", "Testovací Admin", Role.ADMIN)
            invite = RegistrationInvite(email="ph-reg@example.com", created_by_id=admin.id)
            db.session.add(invite)
            db.session.commit()
            token = invite.token
        resp = client.post(
            f"/auth/register/{token}",
            data={
                "surname": "Vymyšlený",
                "first_name": NAME_PLACEHOLDER,
                "password": "securepass99",
                "password2": "securepass99",
            },
        )
        assert resp.status_code == 200
        assert "Jméno nesmí být „(doplnit)“." in resp.data.decode()
        with app.app_context():
            assert db.session.scalar(select(UserAccount).where(UserAccount.email == "ph-reg@example.com")) is None

    def test_setup_rejects_placeholder(self, app, client):
        with app.app_context():
            get_settings().setup_complete = False
            db.session.commit()
        try:
            resp = client.post(
                "/setup/step3",
                data={
                    "surname": NAME_PLACEHOLDER,
                    "first_name": "Rudolf",
                    "email": "root-ph@example.com",
                    "password": "securepass99",
                    "password2": "securepass99",
                },
            )
            assert resp.status_code == 200
            assert "Příjmení nesmí být „(doplnit)“." in resp.data.decode()
            with app.app_context():
                assert db.session.scalar(select(UserAccount).where(UserAccount.email == "root-ph@example.com")) is None
        finally:
            with app.app_context():
                get_settings().setup_complete = True
                db.session.commit()

    def _legacy_coordinator(self, app):
        with app.app_context():
            user = db.session.scalar(select(UserAccount).where(UserAccount.email == "coordinator@test.com"))
            user.surname = "Madonna"
            user.first_name = NAME_PLACEHOLDER
            db.session.commit()
            return user.version

    def test_legacy_user_prefilled_and_can_replace_placeholder(self, app, coordinator_client):
        version = self._legacy_coordinator(app)
        html = coordinator_client.get("/users/profile").data.decode()
        assert 'value="Madonna"' in html and 'value="(doplnit)"' not in html
        first_name_input = re.search(r'<input[^>]*id="first_name"[^>]*>', html).group(0)
        assert 'value=""' in first_name_input and 'placeholder="(doplnit)"' in first_name_input
        assert "required" in first_name_input
        resp = coordinator_client.post(
            "/users/profile",
            data={"action": "profile", "surname": "Madonna", "first_name": "Louise", "version": str(version)},
            follow_redirects=True,
        )
        assert "Profil byl uložen." in resp.data.decode()
        with app.app_context():
            user = db.session.scalar(select(UserAccount).where(UserAccount.email == "coordinator@test.com"))
            assert (user.surname, user.first_name) == ("Madonna", "Louise")
            assert user.name == "Madonna Louise"

    def test_legacy_user_keeping_placeholder_gets_clear_error_and_nothing_saved(self, app, coordinator_client):
        version = self._legacy_coordinator(app)
        resp = coordinator_client.post(
            "/users/profile",
            data={
                "action": "profile",
                "surname": "Madonna",
                "first_name": NAME_PLACEHOLDER,
                "phone": "+420123456789",
                "version": str(version),
            },
            follow_redirects=True,
        )
        assert "Jméno nesmí být „(doplnit)“." in resp.data.decode()
        with app.app_context():
            user = db.session.scalar(select(UserAccount).where(UserAccount.email == "coordinator@test.com"))
            assert (user.first_name, user.phone, user.version) == (NAME_PLACEHOLDER, None, version)

    def test_member_without_name_permission_can_save_profile_with_placeholder(self, app, member_client):
        with app.app_context():
            user = db.session.scalar(select(UserAccount).where(UserAccount.email == "member@test.com"))
            user.surname = "Madonna"
            user.first_name = NAME_PLACEHOLDER
            db.session.commit()
            version = user.version
        html = member_client.get("/users/profile").data.decode()
        assert 'id="first_name"' in html and 'value="(doplnit)"' in html
        resp = member_client.post(
            "/users/profile",
            data={"action": "profile", "phone": "+420123456789", "version": str(version)},
            follow_redirects=True,
        )
        assert "Profil byl uložen." in resp.data.decode()
        with app.app_context():
            user = db.session.scalar(select(UserAccount).where(UserAccount.email == "member@test.com"))
            assert (user.surname, user.first_name, user.phone) == ("Madonna", NAME_PLACEHOLDER, "+420123456789")

    def test_admin_edit_form_blanks_placeholder_name_part(self, app, admin_client):
        with app.app_context():
            user = UserAccount(email="blank-ph@example.com", surname="Madonna", first_name=NAME_PLACEHOLDER)
            user.set_password("pass")
            db.session.add(user)
            db.session.commit()
            user_id = user.id
        html = admin_client.get(f"/users/{user_id}").data.decode()
        first_name_input = re.search(r'<input[^>]*id="edit_first_name"[^>]*>', html).group(0)
        assert 'value=""' in first_name_input and 'placeholder="(doplnit)"' in first_name_input
        assert "required" in first_name_input


class TestEventListSortByResponsiblePerson:
    @pytest.mark.parametrize(("direction", "expected"), [("asc", ["B", "A", "C"]), ("desc", ["A", "B", "C"])])
    def test_orders_by_surname_then_first_name_with_missing_rp_last(self, app, admin_client, direction, expected):
        with app.app_context():
            people = {
                "A": _make_user("rp-a@example.com", "Testovací Zdeněk", Role.COORDINATOR),
                "B": _make_user("rp-b@example.com", "Testovací Adam", Role.COORDINATOR),
            }
            rp_ids = {key: person.id for key, person in people.items()}
        me_id = _make_master_event(app)
        for key in ("A", "B", "C"):
            event_id = _make_event_in_status(app, status=EventStatus.DRAFT, name=f"Řazení RP {key}", me_id=me_id)
            if key in rp_ids:
                with app.app_context():
                    db.session.get(Event, event_id).responsible_person_id = rp_ids[key]
                    db.session.commit()
        html = admin_client.get(f"/events/?sort=rp&dir={direction}&statuses=DRAFT").data.decode()
        positions = {key: html.index(f"Řazení RP {key}") for key in ("A", "B", "C")}
        assert sorted(positions, key=positions.__getitem__) == expected
