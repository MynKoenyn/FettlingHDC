"""product weight (kg) + product ↔ department many-to-many

Revision ID: e5f0a2b8c319
Revises: d4e9f1c7a205
Create Date: 2026-07-24

Weight is the divisor behind the display-only price-per-kg figure on the
products page; the price itself still comes from the price list.

Idempotent: safe to run even if the app's startup auto-sync has already
added the column or created the table.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

# revision identifiers, used by Alembic.
revision = 'e5f0a2b8c319'
down_revision = 'd4e9f1c7a205'
branch_labels = None
depends_on = None


def _columns(insp, table):
    return {c["name"] for c in insp.get_columns(table)}


def upgrade():
    insp = inspect(op.get_bind())

    if 'weight' not in _columns(insp, 'products'):
        op.add_column('products', sa.Column('weight', sa.Numeric(10, 3), nullable=True))

    if 'product_departments' not in insp.get_table_names():
        op.create_table(
            'product_departments',
            sa.Column('product_id', sa.Integer(), nullable=False),
            sa.Column('department_id', sa.Integer(), nullable=False),
            sa.ForeignKeyConstraint(['product_id'], ['products.id'], ondelete='CASCADE'),
            sa.ForeignKeyConstraint(['department_id'], ['departments.id'], ondelete='CASCADE'),
            sa.PrimaryKeyConstraint('product_id', 'department_id'),
        )


def downgrade():
    insp = inspect(op.get_bind())

    if 'product_departments' in insp.get_table_names():
        op.drop_table('product_departments')

    if 'weight' in _columns(insp, 'products'):
        op.drop_column('products', 'weight')
