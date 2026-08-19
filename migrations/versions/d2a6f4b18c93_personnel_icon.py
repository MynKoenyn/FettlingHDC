"""personnel icon tile + colour

Revision ID: d2a6f4b18c93
Revises: c7e2a9f4d3b8
Create Date: 2026-08-05

Adds an icon tile to personnel: `icon` (any Bootstrap Icons class name,
picked from a "browse more icons" search over the full ~2000-icon set) and
`icon_color` (the tile's background, picked from a fixed swatch list).

Existing rows can only have been saved with one of the 15 dashboard
quick-pick icons (the only choice before the free-icon search was added),
so their colour is backfilled from that fixed pairing.

Idempotent: safe to run even if the app's startup auto-sync has already
added the columns / done the backfill.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

# revision identifiers, used by Alembic.
revision = 'd2a6f4b18c93'
down_revision = 'c7e2a9f4d3b8'
branch_labels = None
depends_on = None

_LEGACY_QUICK_PICKS = [
    ("bi-tools",          "#2563eb"),
    ("bi-clipboard-data", "#0891b2"),
    ("bi-boxes",          "#7c3aed"),
    ("bi-fire",           "#b45309"),
    ("bi-trash3",         "#dc2626"),
    ("bi-clock-history",  "#d97706"),
    ("bi-hdd-stack",      "#059669"),
    ("bi-stopwatch",      "#0369a1"),
    ("bi-people",         "#db2777"),
    ("bi-box-seam",       "#4f46e5"),
    ("bi-tags",           "#9333ea"),
    ("bi-person-gear",    "#475569"),
    ("bi-shield-lock",    "#0f766e"),
    ("bi-person-vcard",   "#0d9488"),
    ("bi-diagram-3",      "#ea580c"),
]


def _columns(insp, table):
    return {c["name"] for c in insp.get_columns(table)}


def upgrade():
    insp = inspect(op.get_bind())
    if 'icon' not in _columns(insp, 'personnel'):
        op.add_column('personnel', sa.Column('icon', sa.String(length=40), nullable=True))
    if 'icon_color' not in _columns(insp, 'personnel'):
        op.add_column('personnel', sa.Column('icon_color', sa.String(length=7), nullable=True))

    conn = op.get_bind()
    personnel = sa.table('personnel', sa.column('icon', sa.String), sa.column('icon_color', sa.String))
    for icon, color in _LEGACY_QUICK_PICKS:
        conn.execute(
            personnel.update()
            .where(personnel.c.icon == icon)
            .where(personnel.c.icon_color.is_(None))
            .values(icon_color=color)
        )


def downgrade():
    insp = inspect(op.get_bind())
    if 'icon_color' in _columns(insp, 'personnel'):
        op.drop_column('personnel', 'icon_color')
    if 'icon' in _columns(insp, 'personnel'):
        op.drop_column('personnel', 'icon')
