"""scrap entries — dedicated qty_packed field for internal capture

Revision ID: f4a8c2d97b31
Revises: e3f9b7a2d456
Create Date: 2026-08-06

Internal scrap capture reused qty_machined — a column meant for the
customer's machined total on external rows — to mean "quantity packed".
That conflated two different things under one column, so internal rows get
their own qty_packed column here. Existing internal rows are backfilled
from qty_machined, which is then cleared on those rows since it no longer
applies to them (external rows are untouched).

Idempotent: safe to run even if qty_packed already exists.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

# revision identifiers, used by Alembic.
revision = 'f4a8c2d97b31'
down_revision = 'e3f9b7a2d456'
branch_labels = None
depends_on = None


def _scrap_entries_table():
    return sa.table(
        'scrap_entries',
        sa.column('source', sa.String),
        sa.column('qty_machined', sa.Integer),
        sa.column('qty_packed', sa.Integer),
    )


def upgrade():
    insp = inspect(op.get_bind())
    columns = [c['name'] for c in insp.get_columns('scrap_entries')]
    if 'qty_packed' not in columns:
        op.add_column(
            'scrap_entries',
            sa.Column('qty_packed', sa.Integer(), nullable=True, server_default='0'),
        )

        scrap_entries = _scrap_entries_table()
        op.execute(
            scrap_entries.update()
            .where(scrap_entries.c.source == 'internal')
            .values(qty_packed=scrap_entries.c.qty_machined, qty_machined=None)
        )


def downgrade():
    insp = inspect(op.get_bind())
    columns = [c['name'] for c in insp.get_columns('scrap_entries')]
    if 'qty_packed' in columns:
        scrap_entries = _scrap_entries_table()
        op.execute(
            scrap_entries.update()
            .where(scrap_entries.c.source == 'internal')
            .values(qty_machined=scrap_entries.c.qty_packed)
        )
        op.drop_column('scrap_entries', 'qty_packed')
