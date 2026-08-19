"""furnace module — furnaces, metal grades, entries, tap times, spectro results, tin/copper calcs

Revision ID: 9c2f6b1e8a45
Revises: e8c3f6a91d52
Create Date: 2026-07-31

Brings in the furnace-tracking domain merged from the standalone FurnaceTracker
app: six new tables plus a furnace_role classifier on personnel so melting
staff can be told apart (Melt Technician / Furnace Operator). melt_technician_id
and furnace_operator_id on furnace_entries FK the existing personnel table
rather than a separate one.

Idempotent: safe to run even if a table/column already exists.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

# revision identifiers, used by Alembic.
revision = '9c2f6b1e8a45'
down_revision = 'e8c3f6a91d52'
branch_labels = None
depends_on = None


def _tables(insp):
    return set(insp.get_table_names())


def _columns(insp, table):
    return {c["name"] for c in insp.get_columns(table)}


def upgrade():
    insp = inspect(op.get_bind())
    existing = _tables(insp)

    if 'furnace_role' not in _columns(insp, 'personnel'):
        op.add_column('personnel', sa.Column('furnace_role', sa.String(length=30), nullable=True))

    if 'furnaces' not in existing:
        op.create_table(
            'furnaces',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column('name', sa.String(length=100), nullable=False),
            sa.Column('capacity', sa.Float(), nullable=True),
            sa.Column('capacity_unit', sa.String(length=20), server_default='tons'),
            sa.Column('current_lining_number', sa.Integer(), server_default='1'),
            sa.Column('status', sa.String(length=50), server_default='Active'),
            sa.Column('created_at', sa.DateTime(), nullable=True),
            sa.UniqueConstraint('name', name='uq_furnaces_name'),
        )

    if 'metal_grades' not in existing:
        op.create_table(
            'metal_grades',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column('name', sa.String(length=100), nullable=False),
            sa.Column('description', sa.Text(), nullable=True),
            sa.Column('notes', sa.Text(), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=True),
            sa.UniqueConstraint('name', name='uq_metal_grades_name'),
        )

    if 'furnace_entries' not in existing:
        op.create_table(
            'furnace_entries',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column('date', sa.Date(), nullable=False),
            sa.Column('heat_number', sa.String(length=50), nullable=True),
            sa.Column('furnace_id', sa.Integer(), sa.ForeignKey('furnaces.id'), nullable=True),
            sa.Column('metal_grade_id', sa.Integer(), sa.ForeignKey('metal_grades.id'), nullable=True),
            sa.Column('melt_technician_id', sa.Integer(), sa.ForeignKey('personnel.id'), nullable=True),
            sa.Column('furnace_operator_id', sa.Integer(), sa.ForeignKey('personnel.id'), nullable=True),
            sa.Column('lining_number', sa.Integer(), nullable=True),
            sa.Column('cast_iron', sa.Float(), server_default='0'),
            sa.Column('steel_scrap', sa.Float(), server_default='0'),
            sa.Column('pig_iron', sa.Float(), nullable=True),
            sa.Column('recarb', sa.Float(), nullable=True),
            sa.Column('ferrosilicon', sa.Float(), nullable=True),
            sa.Column('ferromanganese', sa.Float(), nullable=True),
            sa.Column('iron_sulfide', sa.Float(), nullable=True),
            sa.Column('additional_recarb', sa.Float(), nullable=True),
            sa.Column('additional_fesi', sa.Float(), nullable=True),
            sa.Column('additional_femn', sa.Float(), nullable=True),
            sa.Column('additional_iron_sulfide', sa.Float(), nullable=True),
            sa.Column('tin', sa.Float(), nullable=True),
            sa.Column('copper', sa.Float(), nullable=True),
            sa.Column('melt_temperature', sa.Float(), nullable=True),
            sa.Column('inoculate_used', sa.String(length=10), nullable=True),
            sa.Column('remarks', sa.Text(), nullable=True),
            sa.Column('start_charging_time', sa.DateTime(), nullable=True),
            sa.Column('additions_added_time', sa.DateTime(), nullable=True),
            sa.Column('tap_times', sa.Text(), nullable=True),
            sa.Column('end_melt_time', sa.DateTime(), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=True),
            sa.Column('updated_at', sa.DateTime(), nullable=True),
            sa.Column('last_activity_at', sa.DateTime(), nullable=True),
            sa.Column('completed_at', sa.DateTime(), nullable=True),
            sa.Column('status', sa.String(length=15), server_default='In Progress'),
        )

    if 'furnace_tap_times' not in existing:
        op.create_table(
            'furnace_tap_times',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column('entry_id', sa.Integer(),
                      sa.ForeignKey('furnace_entries.id', ondelete='CASCADE'), nullable=False),
            sa.Column('tap_time', sa.DateTime(), nullable=False),
            sa.Column('temperature', sa.Float(), nullable=True),
            sa.Column('innoculate', sa.String(length=20), nullable=True),
            sa.Column('department', sa.String(length=30), nullable=True),
        )

    if 'spectro_results' not in existing:
        op.create_table(
            'spectro_results',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column('entry_id', sa.Integer(),
                      sa.ForeignKey('furnace_entries.id', ondelete='CASCADE'), nullable=True),
            sa.Column('measure_date', sa.Date(), nullable=False),
            sa.Column('measure_time', sa.Time(), nullable=False),
            sa.Column('method_name', sa.Text(), nullable=True),
            sa.Column('calc_mode', sa.Text(), nullable=True),
            sa.Column('melt_technician', sa.Text(), nullable=True),
            sa.Column('grade_id', sa.Text(), nullable=True),
            sa.Column('heat_number', sa.Text(), nullable=True),
            sa.Column('plant', sa.Text(), nullable=True),
            sa.Column('furnace', sa.Text(), nullable=True),
            sa.Column('sample_type', sa.Text(), nullable=True),
            sa.Column('pot_number', sa.Text(), nullable=True),
            sa.Column('metal_grade', sa.Text(), nullable=True),
            sa.Column('cu_addition', sa.Numeric(10, 3), nullable=True),
            sa.Column('sn_addition', sa.Numeric(10, 3), nullable=True),
            sa.Column('ele_c', sa.Numeric(10, 4), nullable=True),
            sa.Column('ele_si', sa.Numeric(10, 4), nullable=True),
            sa.Column('ele_mn', sa.Numeric(10, 4), nullable=True),
            sa.Column('ele_p', sa.Numeric(10, 4), nullable=True),
            sa.Column('ele_s', sa.Numeric(10, 4), nullable=True),
            sa.Column('ele_cr', sa.Numeric(10, 4), nullable=True),
            sa.Column('ele_mo', sa.Numeric(10, 4), nullable=True),
            sa.Column('ele_ni', sa.Numeric(10, 4), nullable=True),
            sa.Column('ele_al', sa.Numeric(10, 4), nullable=True),
            sa.Column('ele_co', sa.Numeric(10, 4), nullable=True),
            sa.Column('ele_cu', sa.Numeric(10, 4), nullable=True),
            sa.Column('ele_nb', sa.Numeric(10, 4), nullable=True),
            sa.Column('ele_ti', sa.Numeric(10, 4), nullable=True),
            sa.Column('ele_v', sa.Numeric(10, 4), nullable=True),
            sa.Column('ele_w', sa.Numeric(10, 4), nullable=True),
            sa.Column('ele_pb', sa.Numeric(10, 4), nullable=True),
            sa.Column('ele_sn', sa.Numeric(10, 4), nullable=True),
            sa.Column('ele_mg', sa.Numeric(10, 4), nullable=True),
            sa.Column('ele_as', sa.Numeric(10, 4), nullable=True),
            sa.Column('ele_zr', sa.Numeric(10, 4), nullable=True),
            sa.Column('ele_bi', sa.Numeric(10, 4), nullable=True),
            sa.Column('ele_ce', sa.Numeric(10, 4), nullable=True),
            sa.Column('ele_sb', sa.Numeric(10, 4), nullable=True),
            sa.Column('ele_se', sa.Numeric(10, 4), nullable=True),
            sa.Column('ele_te', sa.Numeric(10, 4), nullable=True),
            sa.Column('ele_b', sa.Numeric(10, 4), nullable=True),
            sa.Column('ele_zn', sa.Numeric(10, 4), nullable=True),
            sa.Column('ele_la', sa.Numeric(10, 4), nullable=True),
            sa.Column('ele_n', sa.Numeric(10, 4), nullable=True),
            sa.Column('ele_fe', sa.Numeric(10, 4), nullable=True),
            sa.Column('created_at', sa.DateTime(), server_default=sa.func.now()),
        )

    if 'tin_copper_calculations' not in existing:
        op.create_table(
            'tin_copper_calculations',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column('date', sa.Date(), nullable=False),
            sa.Column('heat_number', sa.String(length=50), nullable=True),
            sa.Column('operator_id', sa.Integer(), sa.ForeignKey('personnel.id'), nullable=True),
            sa.Column('furnace_id', sa.Integer(), sa.ForeignKey('furnaces.id'), nullable=True),
            sa.Column('metal_grade_id', sa.Integer(), sa.ForeignKey('metal_grades.id'), nullable=True),
            sa.Column('weight', sa.Integer(), nullable=False),
            sa.Column('base_tin', sa.Float(), nullable=True),
            sa.Column('tin_to_be_added', sa.Float(), nullable=True),
            sa.Column('tin_added', sa.Float(), nullable=True),
            sa.Column('base_copper', sa.Float(), nullable=True),
            sa.Column('copper_to_be_added', sa.Float(), nullable=True),
            sa.Column('copper_added', sa.Float(), nullable=True),
            sa.Column('starting_tin', sa.Float(), nullable=True),
            sa.Column('starting_copper', sa.Float(), nullable=True),
            sa.Column('tin_issued', sa.Float(), nullable=True),
            sa.Column('copper_issued', sa.Float(), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=True),
            sa.Column('updated_at', sa.DateTime(), nullable=True),
        )


def downgrade():
    insp = inspect(op.get_bind())
    existing = _tables(insp)

    for table in ('tin_copper_calculations', 'spectro_results', 'furnace_tap_times',
                  'furnace_entries', 'metal_grades', 'furnaces'):
        if table in existing:
            op.drop_table(table)

    if 'furnace_role' in _columns(insp, 'personnel'):
        op.drop_column('personnel', 'furnace_role')
