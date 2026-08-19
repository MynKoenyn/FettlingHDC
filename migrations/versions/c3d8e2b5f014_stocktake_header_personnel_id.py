"""add personnel_id to stocktake_headers

Revision ID: c3d8e2b5f014
Revises: b2f7c1a9d3e4
Create Date: 2026-07-23

The column exists on StocktakeHeader but was never created in the database,
which broke every stocktake query with UndefinedColumn. It is added nullable
because existing rows have no personnel to backfill and the "open stocktake"
form does not capture one.

Idempotent: safe to run even if the column is already present.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

# revision identifiers, used by Alembic.
revision = 'c3d8e2b5f014'
down_revision = 'b2f7c1a9d3e4'
branch_labels = None
depends_on = None


def _columns(insp, table):
    return {c["name"] for c in insp.get_columns(table)}


def upgrade():
    insp = inspect(op.get_bind())

    if 'personnel_id' not in _columns(insp, 'stocktake_headers'):
        op.add_column(
            'stocktake_headers',
            sa.Column('personnel_id', sa.Integer(), nullable=True)
        )
        op.create_foreign_key(
            'fk_stocktake_headers_personnel',
            'stocktake_headers', 'personnel',
            ['personnel_id'], ['id']
        )


def downgrade():
    insp = inspect(op.get_bind())

    if 'personnel_id' in _columns(insp, 'stocktake_headers'):
        op.drop_constraint(
            'fk_stocktake_headers_personnel',
            'stocktake_headers', type_='foreignkey'
        )
        op.drop_column('stocktake_headers', 'personnel_id')
