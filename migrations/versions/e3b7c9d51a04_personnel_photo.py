"""personnel profile photo

Revision ID: e3b7c9d51a04
Revises: d2a6f4b18c93
Create Date: 2026-08-05

Adds a profile photo to personnel — a stored filename under
static/images/personnel/, following the same pattern as ProductImage
(generated name, never the original upload's).

Idempotent: safe to run even if the app's startup auto-sync has already
added the column.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

# revision identifiers, used by Alembic.
revision = 'e3b7c9d51a04'
down_revision = 'd2a6f4b18c93'
branch_labels = None
depends_on = None


def _columns(insp, table):
    return {c["name"] for c in insp.get_columns(table)}


def upgrade():
    insp = inspect(op.get_bind())
    if 'photo' not in _columns(insp, 'personnel'):
        op.add_column('personnel', sa.Column('photo', sa.String(length=255), nullable=True))


def downgrade():
    insp = inspect(op.get_bind())
    if 'photo' in _columns(insp, 'personnel'):
        op.drop_column('personnel', 'photo')
