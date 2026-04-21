"""
Asset Register — WTForms
========================
pip install flask-wtf wtforms

Import into your routes:
    from asset.forms import AssetForm, DepreciationRunForm, TransferForm, DisposalForm, ImpairmentForm
"""

from flask_wtf import FlaskForm
from wtforms import (
    StringField, SelectField, DecimalField, IntegerField,
    DateField, TextAreaField, HiddenField, BooleanField, FloatField
)
from wtforms.validators import (
    DataRequired, Optional, NumberRange, Length,
    ValidationError
)
from datetime import date


# ── Shared choices ────────────────────────────────────────────────────────────

DEPRECIATION_METHOD_CHOICES = [
    ('SLM', 'Straight Line Method (SLM)'),
    ('WDV', 'Written Down Value (WDV)'),
]

STATUS_CHOICES = [
    ('active', 'Active'),
    ('auc',    'Asset Under Construction (AUC)'),
    ('spare',  'Spare'),
]

DISPOSAL_TYPE_CHOICES = [
    ('sale',      'Sale'),
    ('write_off', 'Write-Off'),
    ('scrap',     'Scrap'),
    ('donation',  'Donation'),
]


def category_choices():
    """Dynamically load categories from DB. Call inside request context."""
    from asset.models import AssetCategory
    cats = AssetCategory.query.filter_by(is_active=True).order_by(AssetCategory.name).all()
    return [('', '— Select Category —')] + [(str(c.id), f'{c.code} — {c.name}') for c in cats]


def depreciable_category_choices():
    """Only categories that are depreciable (for transfer target)."""
    from asset.models import AssetCategory
    cats = AssetCategory.query.filter_by(is_active=True, is_depreciable=True).order_by(AssetCategory.name).all()
    return [('', '— Select Target Category —')] + [(str(c.id), f'{c.code} — {c.name}') for c in cats]


# ── 1. CREATE / EDIT ASSET ────────────────────────────────────────────────────

class AssetForm(FlaskForm):
    """Used for both creating and editing an asset."""

    # Identity
    description = StringField(
        'Asset Description',
        validators=[DataRequired(), Length(min=3, max=255)],
        render_kw={'placeholder': 'e.g. Forklift #3 — 3-Tonne Hyster'}
    )
    serial_no = StringField(
        'Serial / Tag Number',
        validators=[Optional(), Length(max=100)],
        render_kw={'placeholder': 'e.g. HY-2024-003'}
    )
    location = StringField(
        'Location',
        validators=[Optional(), Length(max=150)],
        render_kw={'placeholder': 'e.g. Warehouse A — Bay 4'}
    )

    # Category & Status
    category_id = SelectField(
        'Asset Category',
        validators=[DataRequired(message='Please select a category.')],
        choices=[]      # populated in __init__
    )
    status = SelectField(
        'Initial Status',
        choices=STATUS_CHOICES,
        default='active'
    )
    depreciation_method = SelectField(
        'Depreciation Method',
        choices=DEPRECIATION_METHOD_CHOICES,
        default='SLM'
    )

    # Financial
    cost = DecimalField(
        'Cost (R)',
        validators=[DataRequired(), NumberRange(min=0.01, message='Cost must be greater than zero.')],
        places=2,
        render_kw={'placeholder': '0.00'}
    )
    residual_value = DecimalField(
        'Residual / Scrap Value (R)',
        validators=[Optional(), NumberRange(min=0)],
        places=2,
        default=0,
        render_kw={'placeholder': '0.00'}
    )
    useful_life_years = IntegerField(
        'Useful Life (Years)',
        validators=[Optional(), NumberRange(min=1, max=99)],
        render_kw={'placeholder': 'e.g. 5'}
    )

    # Dates
    purchase_date = DateField(
        'Date of Purchase',
        validators=[DataRequired()],
        format='%Y-%m-%d'
    )
    capitalisation_date = DateField(
        'Capitalisation Date',
        validators=[Optional()],
        format='%Y-%m-%d',
        render_kw={'placeholder': 'Leave blank if same as purchase date'}
    )
    transfer_date = DateField(
        'Transfer Date',
        validators=[Optional()],
        format='%Y-%m-%d',
        description='Required for AUC and Spares — date transferred to active use.'
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.category_id.choices = category_choices()

    def validate_residual_value(self, field):
        if field.data and self.cost.data:
            if field.data >= self.cost.data:
                raise ValidationError('Residual value must be less than cost.')

    def validate_useful_life_years(self, field):
        """Not required for AUC and Spares."""
        if self.status.data == 'active' and not field.data:
            raise ValidationError('Useful life is required for active assets.')

    def validate_transfer_date(self, field):
        if self.status.data in ('auc', 'spare') and not field.data:
            # Transfer date not required at creation for AUC/Spares
            pass
        if field.data and self.purchase_date.data:
            if field.data < self.purchase_date.data:
                raise ValidationError('Transfer date cannot be before purchase date.')


# ── 2. DEPRECIATION RUN ───────────────────────────────────────────────────────

MONTH_CHOICES = [
    (1, 'January'),   (2, 'February'), (3, 'March'),
    (4, 'April'),     (5, 'May'),      (6, 'June'),
    (7, 'July'),      (8, 'August'),   (9, 'September'),
    (10, 'October'),  (11, 'November'),(12, 'December'),
]

class DepreciationRunForm(FlaskForm):
    """Trigger a manual depreciation run for a specific period."""

    year = IntegerField(
        'Year',
        validators=[DataRequired(), NumberRange(min=2000, max=2099)],
        default=lambda: date.today().year
    )
    month = SelectField(
        'Month',
        choices=[(str(v), l) for v, l in MONTH_CHOICES],
        coerce=int,
        default=lambda: date.today().month
    )
    asset_id = HiddenField()   # Optional — if set, runs for single asset only

    def validate_year(self, field):
        today = date.today()
        if field.data > today.year or (
            field.data == today.year and
            int(self.month.data) > today.month
        ):
            raise ValidationError('Cannot run depreciation for a future period.')


# ── 3. CATCHUP DEPRECIATION (calculate accumulated dep without running each month) ──

class CatchUpDepreciationForm(FlaskForm):
    """
    Calculate accumulated depreciation 'as at' a specific date without
    needing to have run each individual period.
    Used for assets added to the register after the fact.
    """
    asset_id   = HiddenField(validators=[DataRequired()])
    as_at_date = DateField(
        'Calculate Accumulated Depreciation As At',
        validators=[DataRequired()],
        format='%Y-%m-%d',
        default=date.today
    )
    post_to_ledger = BooleanField(
        'Post calculated amounts to depreciation schedule',
        default=True,
        description='If unchecked, this is a preview only — nothing is saved.'
    )


# ── 4. TRANSFER (AUC / Spares → Active) ──────────────────────────────────────

class TransferForm(FlaskForm):
    """Transfer an AUC or Spare asset into a depreciable category."""

    asset_id = HiddenField(validators=[DataRequired()])

    to_category_id = SelectField(
        'Transfer To Category',
        validators=[DataRequired()],
        choices=[]      # populated in __init__
    )
    transfer_date = DateField(
        'Transfer / Commissioning Date',
        validators=[DataRequired()],
        format='%Y-%m-%d',
        default=date.today
    )
    useful_life_years = IntegerField(
        'Useful Life (Years)',
        validators=[DataRequired(), NumberRange(min=1, max=99)]
    )
    residual_value = DecimalField(
        'Residual Value (R)',
        validators=[Optional(), NumberRange(min=0)],
        places=2,
        default=0
    )
    depreciation_method = SelectField(
        'Depreciation Method',
        choices=DEPRECIATION_METHOD_CHOICES,
        default='SLM'
    )
    notes = TextAreaField(
        'Notes',
        validators=[Optional(), Length(max=500)],
        render_kw={'rows': 3, 'placeholder': 'e.g. Commissioning certificate received'}
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.to_category_id.choices = depreciable_category_choices()


# ── 5. DISPOSAL ───────────────────────────────────────────────────────────────

class DisposalForm(FlaskForm):
    """Record a full asset disposal."""

    asset_id = HiddenField(validators=[DataRequired()])

    disposal_date = DateField(
        'Disposal Date',
        validators=[DataRequired()],
        format='%Y-%m-%d',
        default=date.today
    )
    disposal_type = SelectField(
        'Disposal Type',
        choices=DISPOSAL_TYPE_CHOICES,
        validators=[DataRequired()]
    )
    proceeds = DecimalField(
        'Disposal Proceeds (R)',
        validators=[Optional(), NumberRange(min=0)],
        places=2,
        default=0,
        render_kw={'placeholder': '0.00'},
        description='Leave as 0 for write-offs and scraps.'
    )
    notes = TextAreaField(
        'Notes / Reference',
        validators=[Optional(), Length(max=500)],
        render_kw={'rows': 3, 'placeholder': 'e.g. Invoice #1234, buyer: ABC Ltd'}
    )


# ── 6. IMPAIRMENT ─────────────────────────────────────────────────────────────

class ImpairmentForm(FlaskForm):
    """Record an impairment charge against an asset."""

    asset_id = HiddenField(validators=[DataRequired()])

    impairment_date = DateField(
        'Impairment Date',
        validators=[DataRequired()],
        format='%Y-%m-%d',
        default=date.today
    )
    impairment_amount = DecimalField(
        'Impairment Amount (R)',
        validators=[DataRequired(), NumberRange(min=0.01)],
        places=2,
        render_kw={'placeholder': '0.00'}
    )
    reason = TextAreaField(
        'Reason for Impairment',
        validators=[DataRequired(), Length(min=10, max=1000)],
        render_kw={'rows': 4, 'placeholder': 'e.g. Flood damage — recoverable amount assessed at R70,000 by independent valuator on 2024-09-15'}
    )
    revised_useful_life_months = IntegerField(
        'Revised Remaining Useful Life (Months)',
        validators=[Optional(), NumberRange(min=1)],
        render_kw={'placeholder': 'Leave blank to keep current useful life'}
    )
    revised_residual_value = DecimalField(
        'Revised Residual Value (R)',
        validators=[Optional(), NumberRange(min=0)],
        places=2,
        render_kw={'placeholder': 'Leave blank to keep current residual value'}
    )


# ── 7. IMPAIRMENT REVERSAL ────────────────────────────────────────────────────

class ImpairmentReversalForm(FlaskForm):
    impairment_id   = HiddenField(validators=[DataRequired()])
    reversal_date   = DateField('Reversal Date',   validators=[DataRequired()], format='%Y-%m-%d', default=date.today)
    reversal_amount = DecimalField('Reversal Amount (R)', validators=[DataRequired(), NumberRange(min=0.01)], places=2)
    reversal_reason = TextAreaField('Reason for Reversal', validators=[DataRequired(), Length(min=5)], render_kw={'rows': 3})


# ── 8. REPORT FILTER ──────────────────────────────────────────────────────────

class ReportFilterForm(FlaskForm):
    """Filter form for the depreciation register report."""

    as_at_date = DateField(
        'As At Date',
        validators=[Optional()],
        format='%Y-%m-%d',
        default=date.today,
        description='Show accumulated depreciation as at this date.'
    )
    category_id = SelectField(
        'Category',
        choices=[],
        validators=[Optional()]
    )
    status = SelectField(
        'Status',
        choices=[
            ('', 'All Statuses'),
            ('active',            'Active'),
            ('auc',               'AUC'),
            ('spare',             'Spare'),
            ('disposed',          'Disposed'),
            ('impaired',          'Impaired'),
            ('fully_depreciated', 'Fully Depreciated'),
        ],
        validators=[Optional()]
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.category_id.choices = [('', 'All Categories')] + [
            (str(c.id), c.name)
            for c in __import__('asset.models', fromlist=['AssetCategory']).AssetCategory.query.filter_by(is_active=True).all()
        ]