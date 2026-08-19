"""
Asset Register Models
=====================
Drop into your Flask app. Requires Flask-SQLAlchemy.
Usage: from models import db, Asset, AssetCategory, ...
"""

from datetime import datetime, date
from decimal import Decimal
from app import db


# ---------------------------------------------------------------------------
# ENUMS / CONSTANTS
# ---------------------------------------------------------------------------

class AssetStatus:
    ACTIVE           = 'active'
    AUC              = 'auc'           # Assets Under Construction
    SPARE            = 'spare'
    DISPOSED         = 'disposed'
    IMPAIRED         = 'impaired'
    FULLY_DEPRECIATED = 'fully_depreciated'

    ALL = [ACTIVE, AUC, SPARE, DISPOSED, IMPAIRED, FULLY_DEPRECIATED]


class DepreciationMethod:
    SLM = 'SLM'   # Straight Line Method
    WDV = 'WDV'   # Written Down Value (Reducing Balance)

    ALL = [SLM, WDV]


class DisposalType:
    SALE      = 'sale'
    WRITE_OFF = 'write_off'
    SCRAP     = 'scrap'
    DONATION  = 'donation'

    ALL = [SALE, WRITE_OFF, SCRAP, DONATION]


# ---------------------------------------------------------------------------
# 1. ASSET CATEGORY
# ---------------------------------------------------------------------------

class AssetCategory(db.Model):
    """
    Lookup table for asset categories.
    Seeded with the 6 standard categories; extend as needed.
    """
    __tablename__ = 'asset_category'

    id   = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False, unique=True)
    code = db.Column(db.String(10),  nullable=False, unique=True)
    # e.g: PM=Plant & Machinery, OE=Office Equipment,
    #       CE=Computer Equipment, MV=Motor Vehicles,
    #       SPR=Spares, AUC=Assets Under Construction

    # Depreciation defaults for this category
    depreciation_method   = db.Column(db.String(5), default=DepreciationMethod.SLM)
    default_useful_life   = db.Column(db.Integer)   # in years; None for AUC/Spares
    default_residual_pct  = db.Column(db.Numeric(5, 2), default=0)  # % of cost

    # Flags
    is_depreciable = db.Column(db.Boolean, default=True)
    # AUC and Spares are NOT depreciable until transferred
    is_active      = db.Column(db.Boolean, default=True)

    created_at = db.Column(db.DateTime, default=datetime.now)

    # Relationships
    assets = db.relationship('Asset', backref='category', lazy='dynamic')

    def __repr__(self):
        return f'<AssetCategory {self.code}: {self.name}>'

    @staticmethod
    def seed_defaults(db_session):
        """Call once during app initialisation to populate standard categories."""
        defaults = [
            dict(name='Plant & Machinery',        code='PM',  default_useful_life=10, default_residual_pct=10, is_depreciable=True),
            dict(name='Office Equipment',          code='OE',  default_useful_life=5,  default_residual_pct=5,  is_depreciable=True),
            dict(name='Computer Equipment',        code='CE',  default_useful_life=3,  default_residual_pct=0,  is_depreciable=True),
            dict(name='Motor Vehicles',            code='MV',  default_useful_life=5,  default_residual_pct=10, is_depreciable=True),
            dict(name='Spares',                    code='SPR', default_useful_life=None, default_residual_pct=0, is_depreciable=False),
            dict(name='Assets Under Construction', code='AUC', default_useful_life=None, default_residual_pct=0, is_depreciable=False),
        ]
        for d in defaults:
            if not AssetCategory.query.filter_by(code=d['code']).first():
                db_session.add(AssetCategory(**d))
        db_session.commit()


# ---------------------------------------------------------------------------
# 2. ASSET  (Core)
# ---------------------------------------------------------------------------

class Asset(db.Model):
    """
    Central asset record.
    Depreciation fields are cached here and refreshed by DepreciationService.
    """
    __tablename__ = 'asset'

    id          = db.Column(db.Integer, primary_key=True)
    asset_code  = db.Column(db.String(50), unique=True, nullable=False)
    description = db.Column(db.String(255), nullable=False)
    location    = db.Column(db.String(150))
    serial_no   = db.Column(db.String(100))

    # Category FK
    category_id = db.Column(db.Integer, db.ForeignKey('asset_category.id'), nullable=False)

    # ----- Cost & Valuation -----
    cost              = db.Column(db.Numeric(15, 2), nullable=False)
    residual_value    = db.Column(db.Numeric(15, 2), default=0)
    useful_life_years = db.Column(db.Integer)         # overrides category default
    useful_life_months = db.Column(db.Integer)        # stored: years * 12

    depreciation_method = db.Column(db.String(5), default=DepreciationMethod.SLM)

    # ----- Cached/Computed Depreciation Figures -----
    # These are recalculated by DepreciationService after any event.
    monthly_depreciation     = db.Column(db.Numeric(15, 2), default=0)
    accumulated_depreciation  = db.Column(db.Numeric(15, 2), default=0)
    net_book_value            = db.Column(db.Numeric(15, 2))   # cost - accum_dep

    # ----- Dates -----
    purchase_date      = db.Column(db.Date, nullable=False)
    capitalisation_date = db.Column(db.Date)   # when asset became 'active'
    transfer_date      = db.Column(db.Date)    # AUC/Spare → active (dep starts here)
    last_dep_date      = db.Column(db.Date)    # last period depreciation was run
    disposal_date      = db.Column(db.Date)

    # ----- Status -----
    status = db.Column(db.String(25), default=AssetStatus.ACTIVE)

    # ----- Audit -----
    created_by = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.now)
    updated_at = db.Column(db.DateTime, default=datetime.now, onupdate=datetime.now)

    # ----- Relationships -----
    depreciation_schedules = db.relationship('DepreciationSchedule', backref='asset', lazy='dynamic', cascade='all, delete-orphan')
    transfers              = db.relationship('AssetTransfer',         backref='asset', lazy='dynamic', cascade='all, delete-orphan')
    disposals              = db.relationship('Disposal',              backref='asset', lazy='dynamic', cascade='all, delete-orphan')
    impairments            = db.relationship('Impairment',            backref='asset', lazy='dynamic', cascade='all, delete-orphan')

    # ----- Properties -----

    @property
    def depreciable_amount(self):
        """Cost minus residual value — the amount to be depreciated over useful life."""
        return Decimal(str(self.cost)) - Decimal(str(self.residual_value or 0))

    @property
    def is_fully_depreciated(self):
        return Decimal(str(self.accumulated_depreciation or 0)) >= self.depreciable_amount

    @property
    def depreciation_start_date(self):
        """
        Depreciation begins on:
          - transfer_date  → for AUC and Spares (after transfer to active)
          - capitalisation_date → for standard assets
          - purchase_date  → fallback
        """
        return self.transfer_date or self.capitalisation_date or self.purchase_date

    def recalculate_monthly_depreciation(self):
        """Recalculate and cache monthly_depreciation. Call after impairment/cost changes."""
        if not self.category or not self.category.is_depreciable:
            self.monthly_depreciation = Decimal('0.00')
            return

        if self.status in (AssetStatus.DISPOSED, AssetStatus.AUC, AssetStatus.SPARE):
            self.monthly_depreciation = Decimal('0.00')
            return

        months = self.useful_life_months or ((self.useful_life_years or 1) * 12)

        if self.depreciation_method == DepreciationMethod.SLM:
            if months > 0:
                self.monthly_depreciation = (self.depreciable_amount / months).quantize(Decimal('0.01'))
            else:
                self.monthly_depreciation = Decimal('0.00')
        # WDV is computed period-by-period in DepreciationService; store 0 here.
        elif self.depreciation_method == DepreciationMethod.WDV:
            self.monthly_depreciation = Decimal('0.00')

    def update_nbv(self):
        self.net_book_value = (
            Decimal(str(self.cost)) - Decimal(str(self.accumulated_depreciation or 0))
        ).quantize(Decimal('0.01'))

    def __repr__(self):
        return f'<Asset {self.asset_code}: {self.description}>'


# ---------------------------------------------------------------------------
# 3. DEPRECIATION SCHEDULE  (Monthly Ledger)
# ---------------------------------------------------------------------------

class DepreciationSchedule(db.Model):
    """
    One row per asset per accounting period.
    Provides full audit trail and period reporting.
    """
    __tablename__ = 'depreciation_schedule'

    id       = db.Column(db.Integer, primary_key=True)
    asset_id = db.Column(db.Integer, db.ForeignKey('asset.id'), nullable=False)

    period_year  = db.Column(db.Integer, nullable=False)   # e.g. 2024
    period_month = db.Column(db.Integer, nullable=False)   # 1–12

    opening_nbv          = db.Column(db.Numeric(15, 2), nullable=False)
    depreciation_charge  = db.Column(db.Numeric(15, 2), nullable=False)
    impairment_charge    = db.Column(db.Numeric(15, 2), default=0)   # if impairment in period
    closing_nbv          = db.Column(db.Numeric(15, 2), nullable=False)

    is_posted  = db.Column(db.Boolean, default=False)
    run_date   = db.Column(db.DateTime, default=datetime.now)
    posted_by  = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)

    __table_args__ = (
        db.UniqueConstraint('asset_id', 'period_year', 'period_month',
                            name='uq_dep_asset_period'),
    )

    def __repr__(self):
        return f'<DepSchedule asset={self.asset_id} {self.period_year}/{self.period_month:02d}>'


# ---------------------------------------------------------------------------
# 4. ASSET TRANSFER  (AUC / Spares → Active)
# ---------------------------------------------------------------------------

class AssetTransfer(db.Model):
    """
    Records movement of AUC or Spares into a depreciable category.
    Triggers depreciation to begin from transfer_date.
    """
    __tablename__ = 'asset_transfer'

    id       = db.Column(db.Integer, primary_key=True)
    asset_id = db.Column(db.Integer, db.ForeignKey('asset.id'), nullable=False)

    from_category_id = db.Column(db.Integer, db.ForeignKey('asset_category.id'), nullable=False)
    to_category_id   = db.Column(db.Integer, db.ForeignKey('asset_category.id'), nullable=False)

    transfer_date    = db.Column(db.Date, nullable=False)
    transferred_cost = db.Column(db.Numeric(15, 2))   # cost at time of transfer

    # New depreciation parameters applied after transfer
    useful_life_years   = db.Column(db.Integer)
    residual_value      = db.Column(db.Numeric(15, 2))
    depreciation_method = db.Column(db.String(5))

    notes      = db.Column(db.Text)
    created_by = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.now)

    from_category = db.relationship('AssetCategory', foreign_keys=[from_category_id])
    to_category   = db.relationship('AssetCategory', foreign_keys=[to_category_id])

    def __repr__(self):
        return f'<AssetTransfer asset={self.asset_id} on {self.transfer_date}>'


# ---------------------------------------------------------------------------
# 5. DISPOSAL
# ---------------------------------------------------------------------------

class Disposal(db.Model):
    """
    Records full or partial disposal of an asset.
    Sets asset.status = 'disposed' and stops depreciation.
    """
    __tablename__ = 'disposal'

    id       = db.Column(db.Integer, primary_key=True)
    asset_id = db.Column(db.Integer, db.ForeignKey('asset.id'), nullable=False)

    disposal_date = db.Column(db.Date, nullable=False)
    disposal_type = db.Column(db.String(20), nullable=False)   # DisposalType.*

    proceeds         = db.Column(db.Numeric(15, 2), default=0)
    nbv_at_disposal  = db.Column(db.Numeric(15, 2))   # net book value on disposal date
    gain_loss        = db.Column(db.Numeric(15, 2))    # proceeds - nbv_at_disposal
    # Positive = profit on disposal; Negative = loss on disposal

    cost_at_disposal          = db.Column(db.Numeric(15, 2))
    accum_dep_at_disposal     = db.Column(db.Numeric(15, 2))

    notes       = db.Column(db.Text)
    approved_by = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    created_at  = db.Column(db.DateTime, default=datetime.now)

    def calculate_gain_loss(self):
        nbv = Decimal(str(self.nbv_at_disposal or 0))
        proceeds = Decimal(str(self.proceeds or 0))
        self.gain_loss = (proceeds - nbv).quantize(Decimal('0.01'))

    def __repr__(self):
        return f'<Disposal asset={self.asset_id} on {self.disposal_date} type={self.disposal_type}>'


# ---------------------------------------------------------------------------
# 6. IMPAIRMENT
# ---------------------------------------------------------------------------

class Impairment(db.Model):
    """
    Records a downward revaluation of an asset's carrying amount.
    After posting, monthly_depreciation is recalculated on the new NBV.
    May be reversed (partial or full) in a later period.
    """
    __tablename__ = 'impairment'

    id       = db.Column(db.Integer, primary_key=True)
    asset_id = db.Column(db.Integer, db.ForeignKey('asset.id'), nullable=False)

    impairment_date    = db.Column(db.Date, nullable=False)
    pre_impairment_nbv = db.Column(db.Numeric(15, 2), nullable=False)
    impairment_amount  = db.Column(db.Numeric(15, 2), nullable=False)
    post_impairment_nbv = db.Column(db.Numeric(15, 2))   # pre - impairment_amount

    # Remaining useful life AFTER impairment (may be revised)
    revised_useful_life_months = db.Column(db.Integer)
    revised_residual_value     = db.Column(db.Numeric(15, 2))

    reason      = db.Column(db.Text, nullable=False)
    is_reversed = db.Column(db.Boolean, default=False)

    # Reversal fields (populated when impairment is reversed)
    reversal_date   = db.Column(db.Date)
    reversal_amount = db.Column(db.Numeric(15, 2))
    reversal_reason = db.Column(db.Text)

    approved_by = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    created_at  = db.Column(db.DateTime, default=datetime.now)

    def calculate_post_impairment_nbv(self):
        pre = Decimal(str(self.pre_impairment_nbv))
        amt = Decimal(str(self.impairment_amount))
        self.post_impairment_nbv = (pre - amt).quantize(Decimal('0.01'))

    def __repr__(self):
        return f'<Impairment asset={self.asset_id} amount={self.impairment_amount} on {self.impairment_date}>'


