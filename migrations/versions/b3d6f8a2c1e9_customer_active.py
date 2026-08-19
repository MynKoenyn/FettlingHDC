"""customer active flag

Revision ID: b3d6f8a2c1e9
Revises: a1f4d7c92b63
Create Date: 2026-08-03

active — is this customer currently trading? Deactivating a customer keeps
every record that already references them (products, price lists) intact —
it only hides them from "add new" pickers going forward.

Existing rows backfill to TRUE, so nothing disappears on upgrade. Idempotent:
safe to run even if the app's startup auto-sync has already added the column.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

# revision identifiers, used by Alembic.
revision = 'b3d6f8a2c1e9'
down_revision = 'a1f4d7c92b63'
branch_labels = None
depends_on = None


def _columns(insp, table):
    return {c["name"] for c in insp.get_columns(table)}


def upgrade():
    insp = inspect(op.get_bind())
    existing = _columns(insp, 'customers')

    if 'active' not in existing:
        op.add_column('customers', sa.Column(
            'active',
            sa.Boolean(),
            nullable=False,
            server_default=sa.text('true'),
        ))


def downgrade():
    insp = inspect(op.get_bind())
    existing = _columns(insp, 'customers')

    if 'active' in existing:
        op.drop_column('customers', 'active')
