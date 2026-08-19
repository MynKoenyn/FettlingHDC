"""personnel supervisor, manager grants, demographics

Revision ID: 1bbd4a2e6727
Revises:
Create Date: 2026-07-17 10:43:11.077301

"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = '1bbd4a2e6727'
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('personnel', schema=None) as batch_op:
        batch_op.add_column(sa.Column('gender', sa.String(length=10), nullable=True))
        batch_op.add_column(sa.Column('race', sa.String(length=20), nullable=True))
        batch_op.add_column(sa.Column('job_description', sa.String(length=150), nullable=True))
        batch_op.add_column(sa.Column('supervisor_id', sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            'fk_personnel_supervisor', 'personnel', ['supervisor_id'], ['id']
        )


def downgrade():
    with op.batch_alter_table('personnel', schema=None) as batch_op:
        batch_op.drop_constraint('fk_personnel_supervisor', type_='foreignkey')
        batch_op.drop_column('supervisor_id')
        batch_op.drop_column('job_description')
        batch_op.drop_column('race')
        batch_op.drop_column('gender')
