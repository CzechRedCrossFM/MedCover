"""Require iCal tokens on every user account.

Revision ID: 8e2f40516273
Revises: 6c0d2e3f4051
"""
import secrets

from alembic import op
import sqlalchemy as sa

revision = '8e2f40516273'
down_revision = '6c0d2e3f4051'
branch_labels = None
depends_on = None

TOKEN_COLUMNS = ('ical_token', 'ical_all_token')


def upgrade():
    conn = op.get_bind()
    for col in TOKEN_COLUMNS:
        ids = conn.execute(sa.text(f'SELECT id FROM user_account WHERE {col} IS NULL')).scalars().all()
        for user_id in ids:
            conn.execute(
                sa.text(f'UPDATE user_account SET {col} = :token WHERE id = :id AND {col} IS NULL'),
                {'token': secrets.token_hex(32), 'id': user_id},
            )
    for col in TOKEN_COLUMNS:
        op.drop_index(f'ix_user_account_{col}', table_name='user_account')
        op.alter_column('user_account', col, existing_type=sa.String(64), nullable=False)
        op.create_index(f'ix_user_account_{col}', 'user_account', [col], unique=True)


def downgrade():
    for col in TOKEN_COLUMNS:
        op.drop_index(f'ix_user_account_{col}', table_name='user_account')
        op.alter_column('user_account', col, existing_type=sa.String(64), nullable=True)
        op.create_index(f'ix_user_account_{col}', 'user_account', [col], unique=True)
