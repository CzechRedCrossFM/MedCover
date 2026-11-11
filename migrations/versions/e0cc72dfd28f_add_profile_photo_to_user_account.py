"""add profile photo columns to user_account

Revision ID: e0cc72dfd28f
Revises: 6c0d2e3f4051
Create Date: 2026-11-10 00:00:00.000000

Optional per-user profile photo, stored processed (square JPEG). The blob is
deferred in the model; the token is a content hash used for presence checks
and browser cache busting.
"""
from alembic import op
import sqlalchemy as sa

revision = 'e0cc72dfd28f'
down_revision = '6c0d2e3f4051'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        'user_account',
        sa.Column('profile_photo', sa.LargeBinary(), nullable=True),
    )
    op.add_column(
        'user_account',
        sa.Column('profile_photo_token', sa.String(length=16), nullable=True),
    )


def downgrade():
    op.drop_column('user_account', 'profile_photo_token')
    op.drop_column('user_account', 'profile_photo')
