"""product grade (GG30 / G25 / SG50 / SG40 / SG60)

Revision ID: d4e9f1c7a205
Revises: c3d8e2b5f014, c3a8e2b41d76
Create Date: 2026-07-23

Also merges the two existing heads (stocktake personnel_id and price lists),
so `flask db upgrade head` resolves to a single revision again.

Idempotent: safe to run even if the app's startup auto-sync has already
added the column.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

# revision identifiers, used by Alembic.
revision = 'd4e9f1c7a205'
down_revision = ('c3d8e2b5f014', 'c3a8e2b41d76')
branch_labels = None
depends_on = None


def _columns(insp, table):
    return {c["name"] for c in insp.get_columns(table)}


def upgrade():
    insp = inspect(op.get_bind())

    if 'grade' not in _columns(insp, 'products'):
        op.add_column('products', sa.Column('grade', sa.String(length=10), nullable=True))


def downgrade():
    insp = inspect(op.get_bind())

    if 'grade' in _columns(insp, 'products'):
        op.drop_column('products', 'grade')
