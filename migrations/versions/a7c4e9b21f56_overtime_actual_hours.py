"""overtime actual hours + request/actual discriminator

Revision ID: a7c4e9b21f56
Revises: f6a1b3d24e87
Create Date: 2026-07-27

Adds the *actual* side of an overtime record alongside the existing
*requested* side, and an entry_type discriminator so a standalone actual
(overtime worked with no prior request) can exist without the request-side
columns. Those request columns are relaxed to nullable to allow it.

Idempotent: safe to run even if the app's startup auto-sync has already
applied the same changes.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

# revision identifiers, used by Alembic.
revision = 'a7c4e9b21f56'
down_revision = 'f6a1b3d24e87'
branch_labels = None
depends_on = None


NEW_COLUMNS = [
    ('entry_type',               sa.String(length=10), {'nullable': False, 'server_default': 'request'}),
    ('actual_start_time',        sa.Time(),            {'nullable': True}),
    ('actual_end_time',          sa.Time(),            {'nullable': True}),
    ('actual_hours',             sa.Numeric(5, 2),     {'nullable': True}),
    ('actual_multiplier',        sa.Numeric(4, 2),     {'nullable': True}),
    ('actual_amount',            sa.Numeric(10, 2),    {'nullable': True}),
    ('actual_amount_overridden', sa.Boolean(),         {'nullable': False, 'server_default': sa.text('false')}),
    ('actual_notes',             sa.Text(),            {'nullable': True}),
    ('actual_captured_by',       sa.Integer(),         {'nullable': True}),
    ('actual_captured_at',       sa.DateTime(),        {'nullable': True}),
]

RELAX_NOT_NULL = ['requested_by', 'start_time', 'end_time', 'hours', 'overtime_amount']


def _columns(insp, table):
    return {c["name"] for c in insp.get_columns(table)}


def upgrade():
    bind = op.get_bind()
    insp = inspect(bind)
    existing = _columns(insp, 'overtime_requests')

    for name, type_, kwargs in NEW_COLUMNS:
        if name not in existing:
            op.add_column('overtime_requests', sa.Column(name, type_, **kwargs))

    # SQLite can't ALTER a column's nullability; it is also not used in prod.
    if bind.dialect.name != 'sqlite':
        cols = {c["name"]: c for c in insp.get_columns('overtime_requests')}
        for name in RELAX_NOT_NULL:
            if name in cols and not cols[name]["nullable"]:
                op.alter_column('overtime_requests', name, nullable=True)


def downgrade():
    bind = op.get_bind()
    insp = inspect(bind)
    existing = _columns(insp, 'overtime_requests')

    for name, _type, _kwargs in reversed(NEW_COLUMNS):
        if name in existing:
            op.drop_column('overtime_requests', name)

    # Not restoring the NOT NULL constraints: rows created as standalone
    # actuals would violate them, so leaving the columns nullable is correct.
