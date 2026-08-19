"""product stock-item and active flags

Revision ID: f6a1b3d24e87
Revises: e5f0a2b8c319
Create Date: 2026-07-24

is_stock_item — is the product held as stock, or made to order?
active        — deactivated products drop off the product pickers but keep
                every record that already references them.

Existing rows backfill to TRUE, so nothing disappears from a picker on
upgrade. Idempotent: safe to run even if the app's startup auto-sync has
already added the columns.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

# revision identifiers, used by Alembic.
revision = 'f6a1b3d24e87'
down_revision = 'e5f0a2b8c319'
branch_labels = None
depends_on = None

FLAGS = ('is_stock_item', 'active')


def _columns(insp, table):
    return {c["name"] for c in insp.get_columns(table)}


def upgrade():
    insp = inspect(op.get_bind())
    existing = _columns(insp, 'products')

    for name in FLAGS:
        if name not in existing:
            op.add_column('products', sa.Column(
                name,
                sa.Boolean(),
                nullable=False,
                server_default=sa.text('true'),
            ))


def downgrade():
    insp = inspect(op.get_bind())
    existing = _columns(insp, 'products')

    for name in FLAGS:
        if name in existing:
            op.drop_column('products', name)
