"""overtime late in / early out minutes

Revision ID: e8c3f6a91d52
Revises: d7b2c5e83f41
Create Date: 2026-07-28

Adds the second unpaid deduction on a captured actual: minutes lost to
clocking in late or leaving early.

Kept apart from the lunch break rather than added to it, because the two mean
different things — a break is time off, late in or early out is time that was
meant to be worked and was not — and management wants them told apart.

Existing rows get 0, which leaves their hours exactly as they were.

Idempotent: safe to run even if the app's startup auto-sync has already
applied the same change.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

# revision identifiers, used by Alembic.
revision = 'e8c3f6a91d52'
down_revision = 'd7b2c5e83f41'
branch_labels = None
depends_on = None


def _columns(insp, table):
    return {c["name"] for c in insp.get_columns(table)}


def upgrade():
    insp = inspect(op.get_bind())
    if 'actual_late_minutes' not in _columns(insp, 'overtime_requests'):
        op.add_column('overtime_requests',
                      sa.Column('actual_late_minutes', sa.Integer(),
                                nullable=False, server_default=sa.text('0')))


def downgrade():
    insp = inspect(op.get_bind())
    if 'actual_late_minutes' in _columns(insp, 'overtime_requests'):
        op.drop_column('overtime_requests', 'actual_late_minutes')
