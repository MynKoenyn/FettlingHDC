"""price list periods + entries, and customer division link

Revision ID: c3a8e2b41d76
Revises: b2f7c1a9d3e4
Create Date: 2026-07-23

Idempotent: safe to run even if the app's startup auto-sync has already
added the column / created the tables.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

# revision identifiers, used by Alembic.
revision = 'c3a8e2b41d76'
down_revision = 'b2f7c1a9d3e4'
branch_labels = None
depends_on = None


def _columns(insp, table):
    return {c["name"] for c in insp.get_columns(table)}


def _fk_names(insp, table):
    return {fk.get("name") for fk in insp.get_foreign_keys(table)}


def upgrade():
    insp = inspect(op.get_bind())

    # ---- customers.division_id ----
    if 'division_id' not in _columns(insp, 'customers'):
        op.add_column('customers', sa.Column('division_id', sa.Integer(), nullable=True))
    if 'fk_customers_division_id' not in _fk_names(insp, 'customers'):
        op.create_foreign_key(
            'fk_customers_division_id', 'customers', 'divisions', ['division_id'], ['id']
        )

    # ---- price_list_periods ----
    if not insp.has_table('price_list_periods'):
        op.create_table(
            'price_list_periods',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('division_id', sa.Integer(), nullable=False),
            sa.Column('label', sa.String(length=100), nullable=False),
            sa.Column('start_date', sa.Date(), nullable=False),
            sa.Column('end_date', sa.Date(), nullable=False),
            sa.Column('created_at', sa.DateTime(), nullable=True),
            sa.ForeignKeyConstraint(['division_id'], ['divisions.id']),
            sa.PrimaryKeyConstraint('id'),
            sa.UniqueConstraint('division_id', 'start_date', name='uq_price_period_division_start'),
        )

    # ---- price_list_entries ----
    if not insp.has_table('price_list_entries'):
        op.create_table(
            'price_list_entries',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('period_id', sa.Integer(), nullable=False),
            sa.Column('customer_id', sa.Integer(), nullable=False),
            sa.Column('product_id', sa.Integer(), nullable=False),
            sa.Column('price', sa.Numeric(precision=10, scale=2), nullable=False),
            sa.ForeignKeyConstraint(['period_id'], ['price_list_periods.id'], ondelete='CASCADE'),
            sa.ForeignKeyConstraint(['customer_id'], ['customers.id']),
            sa.ForeignKeyConstraint(['product_id'], ['products.id']),
            sa.PrimaryKeyConstraint('id'),
            sa.UniqueConstraint('period_id', 'customer_id', 'product_id', name='uq_price_entry'),
        )


def downgrade():
    insp = inspect(op.get_bind())

    if insp.has_table('price_list_entries'):
        op.drop_table('price_list_entries')
    if insp.has_table('price_list_periods'):
        op.drop_table('price_list_periods')

    if 'fk_customers_division_id' in _fk_names(insp, 'customers'):
        op.drop_constraint('fk_customers_division_id', 'customers', type_='foreignkey')
    if 'division_id' in _columns(insp, 'customers'):
        op.drop_column('customers', 'division_id')
