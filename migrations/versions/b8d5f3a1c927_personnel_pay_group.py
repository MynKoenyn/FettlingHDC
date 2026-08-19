"""personnel pay group (Wages / Salary)

Revision ID: b8d5f3a1c927
Revises: a7c4e9b21f56
Create Date: 2026-07-28

Adds a payroll-group classifier to personnel so Wages and Salary staff can
be told apart for grouping and reporting.

Idempotent: safe to run even if the app's startup auto-sync has already
added the column.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

# revision identifiers, used by Alembic.
revision = 'b8d5f3a1c927'
down_revision = 'a7c4e9b21f56'
branch_labels = None
depends_on = None


def _columns(insp, table):
    return {c["name"] for c in insp.get_columns(table)}


def upgrade():
    insp = inspect(op.get_bind())
    if 'pay_group' not in _columns(insp, 'personnel'):
        op.add_column('personnel', sa.Column('pay_group', sa.String(length=20), nullable=True))


def downgrade():
    insp = inspect(op.get_bind())
    if 'pay_group' in _columns(insp, 'personnel'):
        op.drop_column('personnel', 'pay_group')
