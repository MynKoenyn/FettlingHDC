"""product supplier fields (code, supplier code/description, price) and product<->customer many-to-many

Revision ID: b2f7c1a9d3e4
Revises: 1bbd4a2e6727
Create Date: 2026-07-23

Idempotent: safe to run even if the app's startup auto-sync has already
added the columns / created the association table.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

# revision identifiers, used by Alembic.
revision = 'b2f7c1a9d3e4'
down_revision = '1bbd4a2e6727'
branch_labels = None
depends_on = None


def _columns(insp, table):
    return {c["name"] for c in insp.get_columns(table)}


def upgrade():
    insp = inspect(op.get_bind())

    existing = _columns(insp, 'products')
    new_cols = {
        'product_code':         sa.String(length=50),
        'supplier_code':        sa.String(length=50),
        'supplier_description': sa.String(length=255),
        'price':                sa.Numeric(precision=10, scale=2),
    }
    for name, type_ in new_cols.items():
        if name not in existing:
            op.add_column('products', sa.Column(name, type_, nullable=True))

    if not insp.has_table('product_customers'):
        op.create_table(
            'product_customers',
            sa.Column('product_id', sa.Integer(), nullable=False),
            sa.Column('customer_id', sa.Integer(), nullable=False),
            sa.ForeignKeyConstraint(['product_id'], ['products.id'], ondelete='CASCADE'),
            sa.ForeignKeyConstraint(['customer_id'], ['customers.id'], ondelete='CASCADE'),
            sa.PrimaryKeyConstraint('product_id', 'customer_id'),
        )


def downgrade():
    insp = inspect(op.get_bind())

    if insp.has_table('product_customers'):
        op.drop_table('product_customers')

    existing = _columns(insp, 'products')
    for name in ['price', 'supplier_description', 'supplier_code', 'product_code']:
        if name in existing:
            op.drop_column('products', name)
