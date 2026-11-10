"""Split user_account.name into surname and first_name.

Existing names follow the "Příjmení Jméno" convention: the first word becomes
the surname and the rest the first name. A single word becomes the surname and
the first name gets the placeholder '(doplnit)'. Downgrade joins the parts back.

Revision ID: 7e2f4a9c1b35
Revises: e0cc72dfd28f
"""
from alembic import op
import sqlalchemy as sa

revision = '7e2f4a9c1b35'
down_revision = 'e0cc72dfd28f'
branch_labels = None
depends_on = None

PLACEHOLDER = '(doplnit)'


def upgrade():
    op.add_column('user_account', sa.Column('surname', sa.String(length=255), nullable=True))
    op.add_column('user_account', sa.Column('first_name', sa.String(length=255), nullable=True))

    # surname temporarily holds the whitespace-normalised full name
    op.execute(
        "UPDATE user_account SET surname = LTRIM(RTRIM("
        "REPLACE(REPLACE(REPLACE(REPLACE(name, CHAR(9), ' '), CHAR(10), ' '), CHAR(13), ' '), NCHAR(160), ' ')))"
    )
    op.execute(
        "WHILE EXISTS (SELECT 1 FROM user_account WHERE CHARINDEX('  ', surname) > 0) "
        "UPDATE user_account SET surname = REPLACE(surname, '  ', ' ') WHERE CHARINDEX('  ', surname) > 0"
    )
    op.execute(
        "UPDATE user_account SET first_name = CASE WHEN CHARINDEX(' ', surname) > 0 "
        f"THEN STUFF(surname, 1, CHARINDEX(' ', surname), '') ELSE '{PLACEHOLDER}' END"
    )
    op.execute(
        "UPDATE user_account SET surname = LEFT(surname, CHARINDEX(' ', surname) - 1) WHERE CHARINDEX(' ', surname) > 0"
    )
    op.execute(f"UPDATE user_account SET surname = '{PLACEHOLDER}' WHERE surname = ''")

    op.alter_column('user_account', 'surname', existing_type=sa.String(length=255), nullable=False)
    op.alter_column('user_account', 'first_name', existing_type=sa.String(length=255), nullable=False)
    op.drop_column('user_account', 'name')


def downgrade():
    connection = op.get_bind()
    too_long = connection.scalar(
        sa.text(
            f"SELECT COUNT(*) FROM user_account WHERE first_name <> '{PLACEHOLDER}' "
            "AND DATALENGTH(surname) + 1 + DATALENGTH(first_name) > 255"
        )
    )
    if too_long:
        raise RuntimeError('Cannot downgrade: some user names are longer than 255 bytes when joined; shorten them first')

    op.add_column('user_account', sa.Column('name', sa.String(length=255), nullable=True))
    op.execute(
        f"UPDATE user_account SET name = CASE WHEN first_name = '{PLACEHOLDER}' THEN surname "
        "ELSE surname + ' ' + first_name END"
    )
    op.alter_column('user_account', 'name', existing_type=sa.String(length=255), nullable=False)
    op.drop_column('user_account', 'first_name')
    op.drop_column('user_account', 'surname')
