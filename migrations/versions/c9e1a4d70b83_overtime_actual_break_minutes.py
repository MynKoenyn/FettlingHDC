"""overtime actual unpaid lunch break

Revision ID: c9e1a4d70b83
Revises: b8d5f3a1c927
Create Date: 2026-07-28

Adds the unpaid lunch/break total that comes off an actual's hours.

Only the minutes are kept, not the lunch times themselves — the capture form
takes a start and an end and works out how much of it falls inside each range,
but once that is done the times have no further use. The total does have to be
stored: actual_hours is recomputed from the times whenever an actual is edited,
so without it the deduction would quietly be handed back.

Existing rows get 0, which leaves their hours exactly as they were.

Idempotent: safe to run even if the app's startup auto-sync has already
applied the same change.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

# revision identifiers, used by Alembic.
revision = 'c9e1a4d70b83'
down_revision = 'b8d5f3a1c927'
branch_labels = None
depends_on = None


def _columns(insp, table):
    return {c["name"] for c in insp.get_columns(table)}


def upgrade():
    insp = inspect(op.get_bind())
    if 'actual_break_minutes' not in _columns(insp, 'overtime_requests'):
        op.add_column('overtime_requests',
                      sa.Column('actual_break_minutes', sa.Integer(),
                                nullable=False, server_default=sa.text('0')))


def downgrade():
    insp = inspect(op.get_bind())
    if 'actual_break_minutes' in _columns(insp, 'overtime_requests'):
        op.drop_column('overtime_requests', 'actual_break_minutes')
