"""External role/invitation migration compatibility on MSSQL."""

import importlib.util
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import inspect, text

from app.extensions import db
from app.models.invite import RegistrationInvite
from app.models.role import Role
from tests.conftest import _make_user


def load_migration(filename):
    path = Path("migrations/versions") / filename
    spec = importlib.util.spec_from_file_location("external_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.op = Operations(MigrationContext.configure(db.session.connection()))
    return module


def test_invite_migration_preserves_rows_and_defaults(app):
    with app.app_context():
        admin = _make_user("admin@test.com", "Admin", Role.ADMIN)
        invite = RegistrationInvite(email="legacy@test.com", created_by_id=admin.id)
        db.session.add(invite)
        db.session.commit()
        invite_id = invite.id
        migration = load_migration("4a8b0c1d2e3f_external_collaborator_invites.py")
        migration.downgrade()
        try:
            assert "is_external" not in {
                c["name"] for c in inspect(db.session.connection()).get_columns("registration_invite")
            }
        finally:
            migration.upgrade()
            db.session.commit()
        assert (
            db.session.scalar(text("SELECT is_external FROM registration_invite WHERE id=:id"), {"id": invite_id}) == 0
        )
        assert db.session.scalar(text("SELECT COUNT(*) FROM role WHERE name='External'")) == 1


def test_role_downgrade_refuses_to_remove_assigned_external_role(app):
    with app.app_context():
        _make_user("external@test.com", "External", Role.EXTERNAL)
        migration = load_migration("4a8b0c1d2e3f_external_collaborator_invites.py")
        with pytest.raises(RuntimeError, match="External role"):
            migration.downgrade()
