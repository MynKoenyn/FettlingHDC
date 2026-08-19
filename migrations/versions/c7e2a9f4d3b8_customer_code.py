"""customer code

Revision ID: c7e2a9f4d3b8
Revises: b3d6f8a2c1e9
Create Date: 2026-08-03

code — the customer's own reference/account number, distinct from our
internal customer id. Optional, free text.

Idempotent: safe to run even if the app's startup auto-sync has already
added the column.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

# revision identifiers, used by Alembic.
revision = 'c7e2a9f4d3b8'
down_revision = 'b3d6f8a2c1e9'
branch_labels = None
depends_on = None


def _columns(insp, table):
    return {c["name"] for c in insp.get_columns(table)}


def upgrade():
    insp = inspect(op.get_bind())
    existing = _columns(insp, 'customers')

    if 'code' not in existing:
        op.add_column('customers', sa.Column('code', sa.String(length=50)))


def downgrade():
    insp = inspect(op.get_bind())
    existing = _columns(insp, 'customers')

    if 'code' in existing:
        op.drop_column('customers', 'code')
