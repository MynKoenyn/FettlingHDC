"""scrap dispatches — packed stock leaving on a truck

Revision ID: e3f9b7a2d456
Revises: d2a6f4b18c93
Create Date: 2026-08-05

One row per dispatch — one part, one date, one qty. Nets against
ScrapEntry.qty_machined (source=internal) to give the running "packed but
not yet dispatched" balance per product; the balance itself is never
stored, always computed live (see scrap/routes.py _packed_balance).

Idempotent: safe to run even if the table already exists.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

# revision identifiers, used by Alembic.
revision = 'e3f9b7a2d456'
down_revision = 'd2a6f4b18c93'
branch_labels = None
depends_on = None


def upgrade():
    insp = inspect(op.get_bind())
    if not insp.has_table('scrap_dispatches'):
        op.create_table(
            'scrap_dispatches',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('dispatch_date', sa.Date(), nullable=False),
            sa.Column('customer_id', sa.Integer(), nullable=True),
            sa.Column('product_id', sa.Integer(), nullable=True),
            sa.Column('qty_dispatched', sa.Integer(), nullable=False),
            sa.Column('notes', sa.String(length=255), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=True),
            sa.Column('created_by', sa.Integer(), nullable=True),
            sa.ForeignKeyConstraint(['customer_id'], ['customers.id']),
            sa.ForeignKeyConstraint(['product_id'], ['products.id']),
            sa.ForeignKeyConstraint(['created_by'], ['users.id']),
            sa.PrimaryKeyConstraint('id'),
        )
        op.create_index('ix_scrap_dispatches_dispatch_date', 'scrap_dispatches', ['dispatch_date'])
        op.create_index('ix_scrap_dispatches_customer_id', 'scrap_dispatches', ['customer_id'])
        op.create_index('ix_scrap_dispatches_product_id', 'scrap_dispatches', ['product_id'])


def downgrade():
    insp = inspect(op.get_bind())
    if insp.has_table('scrap_dispatches'):
        op.drop_table('scrap_dispatches')
