"""HDA bin stock — stocktake_bin_lines and bin_rates

Revision ID: a1f4d7c92b63
Revises: 9c2f6b1e8a45
Create Date: 2026-08-03

Adds HDA bin-stock counting to the stocktake module: stocktake_bin_lines
(one row per bin type per session, e.g. "27 bins of Unpacked & Fettling")
and bin_rates (a manually re-entered R/KG rate history, applied to bin
weight to compute value — see stocktake/models.py BIN_TYPE_META).

Idempotent: safe to run even if the tables already exist.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

# revision identifiers, used by Alembic.
revision = 'a1f4d7c92b63'
down_revision = '9c2f6b1e8a45'
branch_labels = None
depends_on = None


def upgrade():
    insp = inspect(op.get_bind())

    if not insp.has_table('stocktake_bin_lines'):
        op.create_table(
            'stocktake_bin_lines',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('header_id', sa.Integer(), nullable=False),
            sa.Column('bin_type', sa.Enum('unpacked_fettling', 'castbin', name='bintypeenum'), nullable=False),
            sa.Column('bin_count', sa.Integer(), nullable=False),
            sa.Column('notes', sa.String(length=255), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=True),
            sa.ForeignKeyConstraint(['header_id'], ['stocktake_headers.id']),
            sa.PrimaryKeyConstraint('id'),
            sa.UniqueConstraint('header_id', 'bin_type', name='uq_bin_line_header_type'),
        )

    if not insp.has_table('bin_rates'):
        op.create_table(
            'bin_rates',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('effective_date', sa.Date(), nullable=False),
            sa.Column('rate_per_kg', sa.Numeric(precision=10, scale=2), nullable=False),
            sa.Column('created_at', sa.DateTime(), nullable=True),
            sa.Column('created_by', sa.Integer(), nullable=True),
            sa.ForeignKeyConstraint(['created_by'], ['users.id']),
            sa.PrimaryKeyConstraint('id'),
            sa.UniqueConstraint('effective_date'),
        )


def downgrade():
    insp = inspect(op.get_bind())

    if insp.has_table('bin_rates'):
        op.drop_table('bin_rates')
    if insp.has_table('stocktake_bin_lines'):
        op.drop_table('stocktake_bin_lines')

    bind = op.get_bind()
    sa.Enum(name='bintypeenum').drop(bind, checkfirst=True)
