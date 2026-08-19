"""overtime capture batches

Revision ID: d7b2c5e83f41
Revises: c9e1a4d70b83
Create Date: 2026-07-28

Adds the batch reference shared by every record created in one submit.

A week of overtime raised in one go is ten rows — two periods on each of five
days — and until now they had nothing tying them together, so they could only
be opened, approved and captured one at a time. The batch is what lets the
whole submission be worked as a unit.

Existing rows get NULL, which reads as a batch of one and leaves them exactly
as they were.

Idempotent: safe to run even if the app's startup auto-sync has already
applied the same change.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

# revision identifiers, used by Alembic.
revision = 'd7b2c5e83f41'
down_revision = 'c9e1a4d70b83'
branch_labels = None
depends_on = None


def _columns(insp, table):
    return {c["name"] for c in insp.get_columns(table)}


def _indexes(insp, table):
    return {i["name"] for i in insp.get_indexes(table)}


def upgrade():
    insp = inspect(op.get_bind())
    if 'batch_id' not in _columns(insp, 'overtime_requests'):
        op.add_column('overtime_requests',
                      sa.Column('batch_id', sa.String(length=36), nullable=True))
    if 'ix_overtime_requests_batch_id' not in _indexes(insp, 'overtime_requests'):
        op.create_index('ix_overtime_requests_batch_id',
                        'overtime_requests', ['batch_id'])


def downgrade():
    insp = inspect(op.get_bind())
    if 'ix_overtime_requests_batch_id' in _indexes(insp, 'overtime_requests'):
        op.drop_index('ix_overtime_requests_batch_id', table_name='overtime_requests')
    if 'batch_id' in _columns(insp, 'overtime_requests'):
        op.drop_column('overtime_requests', 'batch_id')
