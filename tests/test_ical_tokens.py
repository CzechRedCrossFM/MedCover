"""iCal tokens are required on every user and the profile GET never writes."""

import importlib.util
import re
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import event, inspect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.extensions import db
from app.models.role import Role
from app.models.user import UserAccount
from tests.conftest import _make_user

HEX64 = re.compile(r"[0-9a-f]{64}")
TOKEN_COLUMNS = ("ical_token", "ical_all_token")


def load_migration():
    path = Path("migrations/versions/8e2f40516273_require_ical_tokens.py")
    spec = importlib.util.spec_from_file_location("require_ical_tokens_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.op = Operations(MigrationContext.configure(db.session.connection()))
    return module


def _tokens(user_id):
    row = db.session.execute(
        sa.text("SELECT ical_token, ical_all_token, version FROM user_account WHERE id = :id"), {"id": str(user_id)}
    ).one()
    return tuple(row)


def _assert_required_and_unique_indexes():
    inspector = inspect(db.session.connection())
    columns = {c["name"]: c for c in inspector.get_columns("user_account")}
    indexes = {i["name"]: i for i in inspector.get_indexes("user_account")}
    for col in TOKEN_COLUMNS:
        assert columns[col]["nullable"] is False
        assert indexes[f"ix_user_account_{col}"]["unique"]


def test_model_declares_tokens_required_and_generated():
    for col in TOKEN_COLUMNS:
        column = UserAccount.__table__.c[col]
        assert column.nullable is False
        assert column.unique
        assert column.index
        assert column.default is not None


def test_new_user_gets_distinct_hex_tokens(app):
    with app.app_context():
        user = _make_user("tokens@test.com", "Testovací Tokenová", Role.MEMBER)
        assert HEX64.fullmatch(user.ical_token)
        assert HEX64.fullmatch(user.ical_all_token)
        assert user.ical_token != user.ical_all_token


@pytest.mark.parametrize("column", TOKEN_COLUMNS)
def test_database_rejects_null_token(app, column):
    with app.app_context():
        user = _make_user("nulltoken@test.com", "Testovací Nulová", Role.MEMBER)
        with pytest.raises(IntegrityError):
            db.session.execute(sa.text(f"UPDATE user_account SET {column} = NULL WHERE id = :id"), {"id": str(user.id)})
        db.session.rollback()


def test_migration_backfills_null_tokens_and_requires_them(app):
    with app.app_context():
        user_a = _make_user("a@test.com", "Testovací Alfa", Role.MEMBER)
        user_b = _make_user("b@test.com", "Testovací Beta", Role.MEMBER)
        user_c = _make_user("c@test.com", "Testovací Gama", Role.MEMBER)
        ids = [user_a.id, user_b.id, user_c.id]
        before = {uid: _tokens(uid) for uid in ids}
        migration = load_migration()
        migration.downgrade()
        downgraded = True
        try:
            db.session.execute(sa.text("UPDATE user_account SET ical_token = NULL WHERE id = :id"), {"id": str(ids[0])})
            db.session.execute(
                sa.text("UPDATE user_account SET ical_all_token = NULL WHERE id = :id"), {"id": str(ids[1])}
            )
            migration.upgrade()
            downgraded = False
            after = {uid: _tokens(uid) for uid in ids}
            _assert_required_and_unique_indexes()
        finally:
            try:
                if downgraded:
                    migration.upgrade()
                db.session.commit()
            except Exception:
                db.session.rollback()  # DDL and fixtures share the transaction; restore the original schema.
                raise

        a_token, a_all, a_version = after[ids[0]]
        b_token, b_all, b_version = after[ids[1]]
        assert HEX64.fullmatch(a_token)
        assert HEX64.fullmatch(b_all)
        assert a_token != b_all
        assert a_token != before[ids[0]][0]
        assert a_all == before[ids[0]][1]
        assert b_token == before[ids[1]][0]
        assert after[ids[2]] == before[ids[2]]
        assert (a_version, b_version) == (before[ids[0]][2], before[ids[1]][2])


def test_migration_round_trip_keeps_tokens(app):
    with app.app_context():
        user = _make_user("roundtrip@test.com", "Testovací Cyklus", Role.MEMBER)
        user_id = user.id
        before = _tokens(user_id)
        migration = load_migration()
        migration.downgrade()
        try:
            inspector = inspect(db.session.connection())
            columns = {c["name"]: c for c in inspector.get_columns("user_account")}
            indexes = {i["name"]: i for i in inspector.get_indexes("user_account")}
            for col in TOKEN_COLUMNS:
                assert columns[col]["nullable"] is True
                assert indexes[f"ix_user_account_{col}"]["unique"]
        finally:
            migration.upgrade()
            db.session.commit()
        _assert_required_and_unique_indexes()
        assert _tokens(user_id) == before


def test_profile_get_does_not_write(app, member_client, monkeypatch):
    with app.app_context():
        user = db.session.scalar(db.select(UserAccount).where(UserAccount.email == "member@test.com"))
        user_id = user.id
        # the GET must also work after another writer has bumped the version
        db.session.execute(
            sa.text("UPDATE user_account SET version = version + 1 WHERE id = :id"), {"id": str(user_id)}
        )
        db.session.commit()
        before = _tokens(user_id)

    def _no_commit():
        raise AssertionError("profile GET must not commit")

    def _no_user_writes(session, flush_context, instances):
        written = [
            obj
            for obj in (*session.new, *session.deleted, *(o for o in session.dirty if session.is_modified(o)))
            if isinstance(obj, UserAccount)
        ]
        if written:
            raise AssertionError(f"profile GET must not flush user changes: {written}")

    monkeypatch.setattr(db.session, "commit", _no_commit)
    event.listen(Session, "before_flush", _no_user_writes)
    try:
        resp = member_client.get("/users/profile")
    finally:
        event.remove(Session, "before_flush", _no_user_writes)
        monkeypatch.undo()

    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert f"/calendar/{before[0]}.ics" in body
    assert f"/calendar/all/{before[1]}.ics" in body
    with app.app_context():
        assert _tokens(user_id) == before
