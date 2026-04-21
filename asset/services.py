"""
Depreciation Service
====================
Core business logic for running depreciation, handling transfers,
disposals, and impairments. Keep all financial calculations here —
never in routes or models directly.

Usage:
    from services import DepreciationService, DisposalService, ImpairmentService, TransferService
"""

from datetime import date, datetime
from decimal import Decimal, ROUND_HALF_UP
from dateutil.relativedelta import relativedelta

from asset.models import (
    db, Asset, AssetCategory, DepreciationSchedule,
    AssetTransfer, Disposal, Impairment,
    AssetStatus, DepreciationMethod, DisposalType
)
from models import User

# ---------------------------------------------------------------------------
# HELPERS
# ---------------------------------------------------------------------------

def _to_decimal(value, places=2):
    """Safely convert any numeric value to Decimal."""
    return Decimal(str(value or 0)).quantize(Decimal(f'0.{"0" * places}'), rounding=ROUND_HALF_UP)


def _periods_between(start: date, end: date) -> int:
    """Return number of full calendar months between two dates."""
    delta = relativedelta(end, start)
    return delta.years * 12 + delta.months


# ---------------------------------------------------------------------------
# 1. DEPRECIATION SERVICE
# ---------------------------------------------------------------------------

class DepreciationService:
    """
    Calculates and posts monthly depreciation charges.

    Supported methods:
        SLM — Straight Line Method
        WDV — Written Down Value (Reducing Balance)

    Rules enforced:
        - AUC and Spares are skipped (not depreciable until transferred)
        - Disposed / fully depreciated assets are skipped
        - Depreciation never takes NBV below residual_value
        - Duplicate period entries are blocked (unique constraint)
    """

    @staticmethod
    def run_period(year: int, month: int, user_id: int = None) -> dict:
        """
        Run depreciation for ALL eligible assets for a given period.

        Returns:
            {
                'posted': [asset_ids],
                'skipped': [(asset_id, reason)],
                'errors':  [(asset_id, error_msg)],
                'total_charge': Decimal
            }
        """
        result = {'posted': [], 'skipped': [], 'errors': [], 'total_charge': Decimal('0.00')}

        # Only fetch active assets in depreciable categories
        assets = (
            Asset.query
            .join(AssetCategory)
            .filter(
                AssetCategory.is_depreciable == True,
                Asset.status == AssetStatus.ACTIVE
            )
            .all()
        )

        for asset in assets:
            try:
                outcome = DepreciationService._post_period(asset, year, month, user_id)
                if outcome['posted']:
                    result['posted'].append(asset.id)
                    result['total_charge'] += outcome['charge']
                else:
                    result['skipped'].append((asset.id, outcome['reason']))
            except Exception as e:
                db.session.rollback()
                result['errors'].append((asset.id, str(e)))

        db.session.commit()
        return result

    @staticmethod
    def run_for_asset(asset_id: int, year: int, month: int, user_id: int = None) -> dict:
        """Run depreciation for a single asset."""
        asset = Asset.query.get_or_404(asset_id)
        outcome = DepreciationService._post_period(asset, year, month, user_id)
        db.session.commit()
        return outcome

    @staticmethod
    def _post_period(asset: Asset, year: int, month: int, user_id: int) -> dict:
        """Internal: calculate and post one period for one asset."""

        # --- Guard: already posted this period? ---
        existing = DepreciationSchedule.query.filter_by(
            asset_id=asset.id, period_year=year, period_month=month
        ).first()
        if existing:
            return {'posted': False, 'reason': 'Already posted for this period'}

        # --- Guard: depreciation not started yet? ---
        dep_start = asset.depreciation_start_date
        period_date = date(1,month, year)
        if dep_start and period_date < dep_start:
            return {'posted': False, 'reason': 'Depreciation not yet started'}

        # --- Guard: fully depreciated ---
        if asset.is_fully_depreciated:
            asset.status = AssetStatus.FULLY_DEPRECIATED
            return {'posted': False, 'reason': 'Asset fully depreciated'}

        # --- Calculate charge ---
        opening_nbv = _to_decimal(asset.net_book_value or asset.cost)
        residual    = _to_decimal(asset.residual_value)
        depreciable_remaining = opening_nbv - residual

        if depreciable_remaining <= 0:
            return {'posted': False, 'reason': 'No depreciable amount remaining'}

        if asset.depreciation_method == DepreciationMethod.SLM:
            charge = _to_decimal(asset.monthly_depreciation)
        elif asset.depreciation_method == DepreciationMethod.WDV:
            # WDV: annual rate applied monthly to opening NBV
            annual_rate = DepreciationService._wdv_rate(asset)
            charge = (opening_nbv * annual_rate / 12).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        else:
            charge = Decimal('0.00')

        # Never go below residual value
        charge = min(charge, depreciable_remaining)

        closing_nbv = (opening_nbv - charge).quantize(Decimal('0.01'))

        # --- Write DepreciationSchedule row ---
        schedule = DepreciationSchedule(
            asset_id           = asset.id,
            period_year        = year,
            period_month       = month,
            opening_nbv        = opening_nbv,
            depreciation_charge = charge,
            closing_nbv        = closing_nbv,
            is_posted          = True,
            run_date           = datetime.utcnow(),
            posted_by          = user_id
        )
        db.session.add(schedule)

        # --- Update asset cached figures ---
        asset.accumulated_depreciation = (
            _to_decimal(asset.accumulated_depreciation) + charge
        )
        asset.net_book_value  = closing_nbv
        asset.last_dep_date   = date(1, month, year)

        if asset.is_fully_depreciated:
            asset.status = AssetStatus.FULLY_DEPRECIATED

        return {'posted': True, 'charge': charge, 'closing_nbv': closing_nbv}

    @staticmethod
    def _wdv_rate(asset: Asset) -> Decimal:
        """Derive annual WDV rate from useful life. Rate = 1 - (S/C)^(1/n)"""
        import math
        cost      = float(asset.cost)
        residual  = float(asset.residual_value or 0)
        years     = (asset.useful_life_months or ((asset.useful_life_years or 1) * 12)) / 12
        if cost <= 0 or years <= 0:
            return Decimal('0')
        salvage_ratio = max(residual / cost, 0.01)   # avoid log(0)
        rate = 1 - (salvage_ratio ** (1 / years))
        return Decimal(str(round(rate, 6)))

    @staticmethod
    def get_schedule(asset_id: int):
        """Return full depreciation schedule for an asset, ordered by period."""
        return (
            DepreciationSchedule.query
            .filter_by(asset_id=asset_id)
            .order_by(DepreciationSchedule.period_year, DepreciationSchedule.period_month)
            .all()
        )

    @staticmethod
    def get_period_summary(year: int, month: int) -> list:
        """Return all posted depreciation entries for a given period."""
        return (
            DepreciationSchedule.query
            .filter_by(period_year=year, period_month=month, is_posted=True)
            .join(Asset)
            .all()
        )


# ---------------------------------------------------------------------------
# 2. TRANSFER SERVICE  (AUC / Spares → Active)
# ---------------------------------------------------------------------------

class TransferService:
    """
    Handles transfer of Assets Under Construction and Spares
    into a depreciable category.  Depreciation begins from transfer_date.
    """

    @staticmethod
    def transfer(asset_id: int, to_category_code: str, transfer_date: date,
                 useful_life_years: int, residual_value: float,
                 depreciation_method: str = DepreciationMethod.SLM,
                 user_id: int = None, notes: str = '') -> AssetTransfer:
        """
        Transfer an AUC or Spare asset to an active depreciable category.

        Raises:
            ValueError if asset is not in AUC/Spare status.
        """
        asset = Asset.query.get_or_404(asset_id)

        if asset.status not in (AssetStatus.AUC, AssetStatus.SPARE):
            raise ValueError(f'Asset {asset.asset_code} is not in AUC or Spare status.')

        to_category = AssetCategory.query.filter_by(code=to_category_code).first()
        if not to_category:
            raise ValueError(f'Category code "{to_category_code}" not found.')

        if not to_category.is_depreciable:
            raise ValueError(f'Target category "{to_category_code}" is not depreciable.')

        from_category_id = asset.category_id

        # Record the transfer
        transfer = AssetTransfer(
            asset_id         = asset.id,
            from_category_id = from_category_id,
            to_category_id   = to_category.id,
            transfer_date    = transfer_date,
            transferred_cost = asset.cost,
            useful_life_years   = useful_life_years,
            residual_value      = _to_decimal(residual_value),
            depreciation_method = depreciation_method,
            notes      = notes,
            created_by = user_id
        )
        db.session.add(transfer)

        # Update asset
        asset.category_id           = to_category.id
        asset.transfer_date         = transfer_date
        asset.capitalisation_date   = transfer_date
        asset.status                = AssetStatus.ACTIVE
        asset.useful_life_years     = useful_life_years
        asset.useful_life_months    = useful_life_years * 12
        asset.residual_value        = _to_decimal(residual_value)
        asset.depreciation_method   = depreciation_method
        asset.accumulated_depreciation = Decimal('0.00')

        asset.recalculate_monthly_depreciation()
        asset.update_nbv()

        db.session.commit()
        return transfer


# ---------------------------------------------------------------------------
# 3. DISPOSAL SERVICE
# ---------------------------------------------------------------------------

class DisposalService:
    """
    Handles full asset disposals (sale, write-off, scrap, donation).
    Stops depreciation and calculates gain/loss.
    """

    @staticmethod
    def dispose(asset_id: int, disposal_date: date, disposal_type: str,
                proceeds: float = 0, notes: str = '',
                approved_by: int = None) -> Disposal:
        """
        Dispose an asset.

        Raises:
            ValueError if asset is already disposed.
        """
        asset = Asset.query.get_or_404(asset_id)

        if asset.status == AssetStatus.DISPOSED:
            raise ValueError(f'Asset {asset.asset_code} is already disposed.')

        if disposal_type not in DisposalType.ALL:
            raise ValueError(f'Invalid disposal type: {disposal_type}')

        nbv = _to_decimal(asset.net_book_value or asset.cost)
        proceeds_dec = _to_decimal(proceeds)

        disposal = Disposal(
            asset_id              = asset.id,
            disposal_date         = disposal_date,
            disposal_type         = disposal_type,
            proceeds              = proceeds_dec,
            nbv_at_disposal       = nbv,
            cost_at_disposal      = _to_decimal(asset.cost),
            accum_dep_at_disposal = _to_decimal(asset.accumulated_depreciation),
            notes                 = notes,
            approved_by           = approved_by
        )
        disposal.calculate_gain_loss()
        db.session.add(disposal)

        # Freeze the asset
        asset.status        = AssetStatus.DISPOSED
        asset.disposal_date = disposal_date
        asset.monthly_depreciation = Decimal('0.00')

        db.session.commit()
        return disposal


# ---------------------------------------------------------------------------
# 4. IMPAIRMENT SERVICE
# ---------------------------------------------------------------------------

class ImpairmentService:
    """
    Records impairment charges and recalculates future depreciation
    based on the post-impairment NBV.
    """

    @staticmethod
    def impair(asset_id: int, impairment_date: date, impairment_amount: float,
               reason: str, revised_useful_life_months: int = None,
               revised_residual_value: float = None,
               approved_by: int = None) -> Impairment:
        """
        Apply an impairment charge to an asset.

        Raises:
            ValueError if impairment_amount > current NBV - residual_value.
        """
        asset = Asset.query.get_or_404(asset_id)

        nbv = _to_decimal(asset.net_book_value or asset.cost)
        residual = _to_decimal(asset.residual_value)
        imp_amount = _to_decimal(impairment_amount)

        if imp_amount > (nbv - residual):
            raise ValueError(
                f'Impairment amount ({imp_amount}) exceeds depreciable carrying amount ({nbv - residual}).'
            )

        impairment = Impairment(
            asset_id           = asset.id,
            impairment_date    = impairment_date,
            pre_impairment_nbv = nbv,
            impairment_amount  = imp_amount,
            revised_useful_life_months = revised_useful_life_months,
            revised_residual_value     = _to_decimal(revised_residual_value) if revised_residual_value else None,
            reason      = reason,
            approved_by = approved_by
        )
        impairment.calculate_post_impairment_nbv()
        db.session.add(impairment)

        # Update asset
        asset.accumulated_depreciation = (
            _to_decimal(asset.accumulated_depreciation) + imp_amount
        )
        asset.net_book_value = impairment.post_impairment_nbv
        asset.status = AssetStatus.IMPAIRED

        # Apply revised parameters if provided
        if revised_useful_life_months:
            asset.useful_life_months = revised_useful_life_months
        if revised_residual_value is not None:
            asset.residual_value = _to_decimal(revised_residual_value)

        # Recalculate monthly depreciation on new NBV
        # For SLM after impairment: new charge = (new NBV - revised residual) / remaining months
        remaining_months = ImpairmentService._remaining_months(asset, impairment_date)
        if remaining_months > 0:
            new_depreciable = (
                _to_decimal(asset.net_book_value) - _to_decimal(asset.residual_value)
            )
            asset.monthly_depreciation = (
                new_depreciable / remaining_months
            ).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        else:
            asset.monthly_depreciation = Decimal('0.00')

        db.session.commit()
        return impairment

    @staticmethod
    def reverse(impairment_id: int, reversal_date: date, reversal_amount: float,
                reversal_reason: str, approved_by: int = None) -> Impairment:
        """Reverse a previously posted impairment (partial or full)."""
        impairment = Impairment.query.get_or_404(impairment_id)

        if impairment.is_reversed:
            raise ValueError('This impairment has already been reversed.')

        rev_amount = _to_decimal(reversal_amount)
        asset = impairment.asset

        impairment.is_reversed    = True
        impairment.reversal_date   = reversal_date
        impairment.reversal_amount = rev_amount
        impairment.reversal_reason = reversal_reason

        # Reduce accumulated depreciation by reversal amount
        asset.accumulated_depreciation = (
            _to_decimal(asset.accumulated_depreciation) - rev_amount
        )
        asset.update_nbv()
        asset.recalculate_monthly_depreciation()

        if asset.status == AssetStatus.IMPAIRED:
            asset.status = AssetStatus.ACTIVE

        db.session.commit()
        return impairment

    @staticmethod
    def _remaining_months(asset: Asset, from_date: date) -> int:
        dep_start = asset.depreciation_start_date
        total_months = asset.useful_life_months or ((asset.useful_life_years or 1) * 12)
        elapsed = _periods_between(dep_start, from_date) if dep_start else 0
        return max(0, total_months - elapsed)


# ---------------------------------------------------------------------------
# 5. ASSET CODE GENERATOR
# ---------------------------------------------------------------------------

class AssetCodeGenerator:
    """Auto-generate sequential asset codes like PM-00001, AUC-00003."""

    @staticmethod
    def generate(category: AssetCategory) -> str:
        count = Asset.query.filter_by(category_id=category.id).count()
        return f'{category.code}-{(count + 1):05d}'


# ---------------------------------------------------------------------------
# 6. REPORTING HELPERS
# ---------------------------------------------------------------------------

class AssetReportService:
    """Utility queries for common register reports."""

    @staticmethod
    def asset_register(category_code: str = None) -> list:
        """Full asset register, optionally filtered by category."""
        q = Asset.query.join(AssetCategory)
        if category_code:
            q = q.filter(AssetCategory.code == category_code)
        return q.order_by(AssetCategory.name, Asset.asset_code).all()

    @staticmethod
    def depreciation_summary(year: int, month: int) -> dict:
        """Aggregate depreciation charges by category for a period."""
        from sqlalchemy import func
        rows = (
            db.session.query(
                AssetCategory.name,
                func.count(DepreciationSchedule.id).label('asset_count'),
                func.sum(DepreciationSchedule.depreciation_charge).label('total_charge')
            )
            .join(Asset, Asset.id == DepreciationSchedule.asset_id)
            .join(AssetCategory, AssetCategory.id == Asset.category_id)
            .filter(
                DepreciationSchedule.period_year  == year,
                DepreciationSchedule.period_month == month,
                DepreciationSchedule.is_posted    == True
            )
            .group_by(AssetCategory.name)
            .all()
        )
        return [
            {'category': r.name, 'asset_count': r.asset_count, 'total_charge': r.total_charge}
            for r in rows
        ]

    @staticmethod
    def disposal_report(from_date: date, to_date: date) -> list:
        return (
            Disposal.query
            .filter(Disposal.disposal_date.between(from_date, to_date))
            .join(Asset)
            .order_by(Disposal.disposal_date)
            .all()
        )

    @staticmethod
    def impairment_report(from_date: date, to_date: date) -> list:
        return (
            Impairment.query
            .filter(Impairment.impairment_date.between(from_date, to_date))
            .join(Asset)
            .order_by(Impairment.impairment_date)
            .all()
        )

    @staticmethod
    def auc_and_spares() -> dict:
        """All assets still in AUC or Spare status."""
        auc    = Asset.query.filter_by(status=AssetStatus.AUC).all()
        spares = Asset.query.filter_by(status=AssetStatus.SPARE).all()
        return {'auc': auc, 'spares': spares}