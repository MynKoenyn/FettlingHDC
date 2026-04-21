"""
Asset Register Routes (Blueprint)
==================================
Register this blueprint in your Flask app factory:

    from routes import asset_bp
    app.register_blueprint(asset_bp, url_prefix='/assets')

All endpoints return JSON. Wire up your own templates/frontend as needed.
"""

from flask import render_template, request, redirect, url_for, session, flash, Blueprint,jsonify, abort
from flask_login import login_user, logout_user, current_user, login_required
from app import app , db
from asset.models import *
from models import *
from asset.services import *
from asset.forms import *
from datetime import date
import json

asset_bp = Blueprint('asset', __name__, url_prefix='/asset')


# ── Helpers ──────────────────────────────────────────────────────────────────

def _parse_date(value: str):
    if not value:
        return None
    try:
        return date.fromisoformat(value)   # expects 'YYYY-MM-DD'
    except ValueError:
        abort(400, f'Invalid date format: {value}. Use DD-MM-YYYY.')


def _asset_to_dict(a: Asset) -> dict:
    return {
        'id':                      a.id,
        'asset_code':              a.asset_code,
        'description':             a.description,
        'category':                a.category.name if a.category else None,
        'category_code':           a.category.code if a.category else None,
        'status':                  a.status,
        'cost':                    float(a.cost),
        'residual_value':          float(a.residual_value or 0),
        'useful_life_years':       a.useful_life_years,
        'useful_life_months':      a.useful_life_months,
        'depreciation_method':     a.depreciation_method,
        'monthly_depreciation':    float(a.monthly_depreciation or 0),
        'accumulated_depreciation': float(a.accumulated_depreciation or 0),
        'net_book_value':          float(a.net_book_value or a.cost),
        'purchase_date':           str(a.purchase_date) if a.purchase_date else None,
        'transfer_date':           str(a.transfer_date) if a.transfer_date else None,
        'capitalisation_date':     str(a.capitalisation_date) if a.capitalisation_date else None,
        'disposal_date':           str(a.disposal_date) if a.disposal_date else None,
        'last_dep_date':           str(a.last_dep_date) if a.last_dep_date else None,
        'location':                a.location,
        'serial_no':               a.serial_no,
    }


# ─────────────────────────────────────────────────────────────────────────────
# DASHBOARD
# ─────────────────────────────────────────────────────────────────────────────
 
@asset_bp.route('/dashboard')
def dashboard():
    from sqlalchemy import func
 
    today = date.today()
 
    # Summary cards
    total_assets   = Asset.query.filter(Asset.status != AssetStatus.DISPOSED).count()
    total_cost     = db.session.query(func.sum(Asset.cost)).filter(Asset.status != AssetStatus.DISPOSED).scalar() or 0
    total_accum    = db.session.query(func.sum(Asset.accumulated_depreciation)).filter(Asset.status != AssetStatus.DISPOSED).scalar() or 0
    total_nbv      = db.session.query(func.sum(Asset.net_book_value)).filter(Asset.status != AssetStatus.DISPOSED).scalar() or 0
    auc_count      = Asset.query.filter_by(status=AssetStatus.AUC).count()
    spares_count   = Asset.query.filter_by(status=AssetStatus.SPARE).count()
    disposed_count = Asset.query.filter_by(status=AssetStatus.DISPOSED).count()
    fully_dep      = Asset.query.filter_by(status=AssetStatus.FULLY_DEPRECIATED).count()
    impaired_count = Asset.query.filter_by(status=AssetStatus.IMPAIRED).count()
 
    # By category (for chart)
    category_summary = (
        db.session.query(
            AssetCategory.name,
            AssetCategory.code,
            func.count(Asset.id).label('count'),
            func.sum(Asset.cost).label('total_cost'),
            func.sum(Asset.net_book_value).label('total_nbv'),
        )
        .join(Asset, Asset.category_id == AssetCategory.id)
        .filter(Asset.status != AssetStatus.DISPOSED)
        .group_by(AssetCategory.id)
        .all()
    )
 
    # Last depreciation run
    last_run = (
        DepreciationSchedule.query
        .order_by(DepreciationSchedule.period_year.desc(), DepreciationSchedule.period_month.desc())
        .first()
    )
 
    # Recent activity (last 10 events across disposals/impairments/transfers)
    recent_disposals    = Disposal.query.order_by(Disposal.created_at.desc()).limit(5).all()
    recent_impairments  = Impairment.query.order_by(Impairment.created_at.desc()).limit(5).all()
    recent_transfers    = AssetTransfer.query.order_by(AssetTransfer.created_at.desc()).limit(5).all()
 
    dep_run_form = DepreciationRunForm()
 
    return render_template(
        'asset/dashboard.html',
        today=today,
        total_assets=total_assets,
        total_cost=total_cost,
        total_accum=total_accum,
        total_nbv=total_nbv,
        auc_count=auc_count,
        spares_count=spares_count,
        disposed_count=disposed_count,
        fully_dep=fully_dep,
        impaired_count=impaired_count,
        category_summary=category_summary,
        last_run=last_run,
        recent_disposals=recent_disposals,
        recent_impairments=recent_impairments,
        recent_transfers=recent_transfers,
        dep_run_form=dep_run_form,
    )
 
 
# ─────────────────────────────────────────────────────────────────────────────
# ASSET LIST
# ─────────────────────────────────────────────────────────────────────────────
 
@asset_bp.route('/')
def list_assets():
    category_code = request.args.get('category', '')
    status        = request.args.get('status', '')
    search        = request.args.get('q', '').strip()
    page          = request.args.get('page', 1, type=int)
 
    q = Asset.query.join(AssetCategory)
 
    if category_code:
        q = q.filter(AssetCategory.code == category_code)
    if status:
        q = q.filter(Asset.status == status)
    if search:
        q = q.filter(
            db.or_(
                Asset.description.ilike(f'%{search}%'),
                Asset.asset_code.ilike(f'%{search}%'),
                Asset.serial_no.ilike(f'%{search}%'),
            )
        )
 
    assets = q.order_by(AssetCategory.name, Asset.asset_code).paginate(page=page, per_page=25)
    categories = AssetCategory.query.filter_by(is_active=True).order_by(AssetCategory.name).all()
 
    return render_template(
        'asset/list.html',
        assets=assets,
        categories=categories,
        current_category=category_code,
        current_status=status,
        search=search,
    )
 
 
# ─────────────────────────────────────────────────────────────────────────────
# ASSET DETAIL
# ─────────────────────────────────────────────────────────────────────────────
 
@asset_bp.route('/<int:asset_id>')
def asset_detail(asset_id):
    asset    = Asset.query.get_or_404(asset_id)
    schedule = DepreciationService.get_schedule(asset_id)
    transfer_form    = TransferForm(asset_id=asset_id)
    disposal_form    = DisposalForm(asset_id=asset_id)
    impairment_form  = ImpairmentForm(asset_id=asset_id)
    catchup_form     = CatchUpDepreciationForm(asset_id=asset_id)
    dep_run_form     = DepreciationRunForm(asset_id=asset_id)
 
    # Calculate "as-at" values using schedule
    as_at = request.args.get('as_at')
    as_at_date = None
    as_at_accum = None
    as_at_nbv   = None
 
    if as_at:
        try:
            as_at_date = date.fromisoformat(as_at)
            as_at_accum, as_at_nbv = _calculate_as_at(asset, as_at_date, schedule)
        except ValueError:
            pass
 
    return render_template(
        'asset/detail.html',
        asset=asset,
        schedule=schedule,
        transfer_form=transfer_form,
        disposal_form=disposal_form,
        impairment_form=impairment_form,
        catchup_form=catchup_form,
        dep_run_form=dep_run_form,
        as_at_date=as_at_date,
        as_at_accum=as_at_accum,
        as_at_nbv=as_at_nbv,
    )
 
 
def _calculate_as_at(asset, as_at_date, schedule):
    """
    Calculate accumulated depreciation and NBV as at a specific date.
 
    Priority:
      1. Sum posted DepreciationSchedule rows up to as_at_date
      2. If no rows exist (catch-up scenario), calculate mathematically from SLM formula
    """
    year  = as_at_date.year
    month = as_at_date.month
 
    # Try from posted schedule
    posted = [
        s for s in schedule
        if (s.period_year, s.period_month) <= (year, month) and s.is_posted
    ]
 
    if posted:
        total_dep = sum(Decimal(str(s.depreciation_charge)) for s in posted)
        accum     = total_dep
        nbv       = Decimal(str(asset.cost)) - accum
        return accum, nbv
 
    # Fallback: mathematical SLM calculation
    dep_start = asset.depreciation_start_date
    if not dep_start or not asset.useful_life_months:
        return Decimal('0'), Decimal(str(asset.cost))
 
    if as_at_date < dep_start:
        return Decimal('0'), Decimal(str(asset.cost))
 
    delta   = relativedelta(as_at_date, dep_start)
    months_elapsed = delta.years * 12 + delta.months + 1  # inclusive of current month
    months_elapsed = min(months_elapsed, asset.useful_life_months)
 
    monthly = Decimal(str(asset.monthly_depreciation or 0))
    accum   = (monthly * months_elapsed).quantize(Decimal('0.01'))
    depreciable = Decimal(str(asset.cost)) - Decimal(str(asset.residual_value or 0))
    accum   = min(accum, depreciable)
    nbv     = Decimal(str(asset.cost)) - accum
 
    return accum, nbv
 
 
# ─────────────────────────────────────────────────────────────────────────────
# CREATE ASSET
# ─────────────────────────────────────────────────────────────────────────────
 
@asset_bp.route('/new', methods=['GET', 'POST'])
def create_asset():
    form = AssetForm()
    if form.validate_on_submit():
        category = AssetCategory.query.get(int(form.category_id.data))
        if not category:
            flash('Invalid category selected.', 'danger')
            return render_template('asset/form.html', form=form, title='New Asset')
 
        status = form.status.data
        ul_years  = form.useful_life_years.data or category.default_useful_life or 1
        ul_months = ul_years * 12
        cost      = float(form.cost.data)
        residual  = float(form.residual_value.data or 0)
 
        if status in (AssetStatus.AUC, AssetStatus.SPARE):
            monthly_dep = 0
        else:
            monthly_dep = (cost - residual) / ul_months if ul_months else 0
 
        asset = Asset(
            asset_code           = AssetCodeGenerator.generate(category),
            description          = form.description.data,
            category_id          = category.id,
            cost                 = form.cost.data,
            residual_value       = form.residual_value.data or 0,
            useful_life_years    = ul_years,
            useful_life_months   = ul_months,
            depreciation_method  = form.depreciation_method.data,
            monthly_depreciation = monthly_dep,
            accumulated_depreciation = 0,
            net_book_value       = cost,
            purchase_date        = form.purchase_date.data,
            capitalisation_date  = form.capitalisation_date.data,
            transfer_date        = form.transfer_date.data,
            status               = status,
            location             = form.location.data,
            serial_no            = form.serial_no.data,
        )
        db.session.add(asset)
        db.session.commit()
        flash(f'Asset {asset.asset_code} created successfully.', 'success')
        return redirect(url_for('asset.asset_detail', asset_id=asset.id))
 
    return render_template('asset/form.html', form=form, title='Add New Asset')
 
 
# ─────────────────────────────────────────────────────────────────────────────
# EDIT ASSET
# ─────────────────────────────────────────────────────────────────────────────
 
@asset_bp.route('/<int:asset_id>/edit', methods=['GET', 'POST'])
def edit_asset(asset_id):
    asset = Asset.query.get_or_404(asset_id)
    form  = AssetForm(obj=asset)
    form.category_id.data = str(asset.category_id)
 
    if form.validate_on_submit():
        asset.description = form.description.data
        asset.location    = form.location.data
        asset.serial_no   = form.serial_no.data
        db.session.commit()
        flash('Asset updated.', 'success')
        return redirect(url_for('asset.asset_detail', asset_id=asset.id))
 
    return render_template('asset/form.html', form=form, title='Edit Asset', asset=asset)
 
 
# ─────────────────────────────────────────────────────────────────────────────
# DEPRECIATION — MANUAL PERIOD RUN
# ─────────────────────────────────────────────────────────────────────────────
 
@asset_bp.route('/depreciation/run', methods=['POST'])
def run_depreciation():
    form = DepreciationRunForm()
    if form.validate_on_submit():
        year     = form.year.data
        month    = int(form.month.data)
        asset_id = form.asset_id.data or None
 
        if asset_id:
            outcome = DepreciationService.run_for_asset(int(asset_id), year, month)
            if outcome.get('posted'):
                flash(
                    f'Depreciation posted for period {year}/{month:02d}. '
                    f'Charge: R{float(outcome["charge"]):,.2f}',
                    'success'
                )
            else:
                flash(f'Not posted: {outcome.get("reason")}', 'warning')
            return redirect(url_for('asset.asset_detail', asset_id=asset_id))
        else:
            result = DepreciationService.run_period(year, month)
            flash(
                f'Depreciation run complete for {year}/{month:02d}. '
                f'Posted: {len(result["posted"])} assets. '
                f'Total charge: R{float(result["total_charge"]):,.2f}. '
                f'Skipped: {len(result["skipped"])}.',
                'success' if result["posted"] else 'warning'
            )
            return redirect(url_for('asset.dashboard'))
 
    flash('Invalid depreciation run parameters.', 'danger')
    return redirect(url_for('asset.dashboard'))
 
 
# ─────────────────────────────────────────────────────────────────────────────
# DEPRECIATION — CATCH-UP (as-at calculation)
# ─────────────────────────────────────────────────────────────────────────────
 
@asset_bp.route('/<int:asset_id>/catchup', methods=['POST'])
def catchup_depreciation(asset_id):
    """
    Calculate and optionally post accumulated depreciation for all months
    between depreciation_start_date and as_at_date in one go.
    This solves the 'I have assets added today but they were purchased in 2020'
    problem without manually running each month.
    """
    form = CatchUpDepreciationForm()
    if form.validate_on_submit():
        asset      = Asset.query.get_or_404(asset_id)
        as_at_date = form.as_at_date.data
        post       = form.post_to_ledger.data
 
        dep_start = asset.depreciation_start_date
        if not dep_start:
            flash('Asset has no depreciation start date.', 'warning')
            return redirect(url_for('asset.asset_detail', asset_id=asset_id))
 
        # Calculate all months to post
        current = dep_start.replace(day=1)
        end     = as_at_date.replace(day=1)
        months_to_post = []
 
        while current <= end:
            exists = DepreciationSchedule.query.filter_by(
                asset_id=asset.id,
                period_year=current.year,
                period_month=current.month
            ).first()
            if not exists:
                months_to_post.append((current.year, current.month))
            current = (current + relativedelta(months=1))
 
        if not months_to_post:
            flash('All periods up to that date already posted.', 'info')
            return redirect(url_for('asset.asset_detail', asset_id=asset_id))
 
        if post:
            posted_count = 0
            for yr, mo in months_to_post:
                outcome = DepreciationService.run_for_asset(asset_id, yr, mo)
                if outcome.get('posted'):
                    posted_count += 1
            flash(
                f'Catch-up complete. Posted {posted_count} period(s) '
                f'up to {as_at_date.strftime("%d %B %Y")}.',
                'success'
            )
        else:
            # Preview only — pass as_at to detail page
            flash(
                f'Preview: {len(months_to_post)} period(s) would be posted '
                f'up to {as_at_date.strftime("%d %B %Y")}.',
                'info'
            )
 
    return redirect(url_for('asset.asset_detail', asset_id=asset_id,
                             as_at=form.as_at_date.data.isoformat() if form.as_at_date.data else ''))
 
 
# ─────────────────────────────────────────────────────────────────────────────
# TRANSFER
# ─────────────────────────────────────────────────────────────────────────────
 
@asset_bp.route('/<int:asset_id>/transfer', methods=['POST'])
def transfer_asset(asset_id):
    form = TransferForm()
    if form.validate_on_submit():
        try:
            category = AssetCategory.query.get(int(form.to_category_id.data))
            TransferService.transfer(
                asset_id           = asset_id,
                to_category_code   = category.code,
                transfer_date      = form.transfer_date.data,
                useful_life_years  = form.useful_life_years.data,
                residual_value     = float(form.residual_value.data or 0),
                depreciation_method = form.depreciation_method.data,
                notes              = form.notes.data,
            )
            flash('Asset successfully transferred to active use. Depreciation will begin from the transfer date.', 'success')
        except ValueError as e:
            flash(str(e), 'danger')
    else:
        for field, errors in form.errors.items():
            for error in errors:
                flash(f'{field}: {error}', 'danger')
 
    return redirect(url_for('asset.asset_detail', asset_id=asset_id))
 
 
# ─────────────────────────────────────────────────────────────────────────────
# DISPOSAL
# ─────────────────────────────────────────────────────────────────────────────
 
@asset_bp.route('/<int:asset_id>/dispose', methods=['POST'])
def dispose_asset(asset_id):
    form = DisposalForm()
    if form.validate_on_submit():
        try:
            disposal = DisposalService.dispose(
                asset_id      = asset_id,
                disposal_date = form.disposal_date.data,
                disposal_type = form.disposal_type.data,
                proceeds      = float(form.proceeds.data or 0),
                notes         = form.notes.data,
            )
            gain_loss = float(disposal.gain_loss)
            direction = 'Profit on disposal' if gain_loss >= 0 else 'Loss on disposal'
            flash(
                f'Asset disposed. {direction}: R{abs(gain_loss):,.2f}',
                'success' if gain_loss >= 0 else 'warning'
            )
        except ValueError as e:
            flash(str(e), 'danger')
    return redirect(url_for('asset.asset_detail', asset_id=asset_id))
 
 
# ─────────────────────────────────────────────────────────────────────────────
# IMPAIRMENT
# ─────────────────────────────────────────────────────────────────────────────
 
@asset_bp.route('/<int:asset_id>/impair', methods=['POST'])
def impair_asset(asset_id):
    form = ImpairmentForm()
    if form.validate_on_submit():
        try:
            ImpairmentService.impair(
                asset_id               = asset_id,
                impairment_date        = form.impairment_date.data,
                impairment_amount      = float(form.impairment_amount.data),
                reason                 = form.reason.data,
                revised_useful_life_months = form.revised_useful_life_months.data,
                revised_residual_value     = float(form.revised_residual_value.data) if form.revised_residual_value.data else None,
            )
            flash('Impairment posted. Monthly depreciation has been recalculated.', 'success')
        except ValueError as e:
            flash(str(e), 'danger')
    return redirect(url_for('asset.asset_detail', asset_id=asset_id))
 
 
@asset_bp.route('/impairments/<int:impairment_id>/reverse', methods=['POST'])
def reverse_impairment(impairment_id):
    form = ImpairmentReversalForm()
    if form.validate_on_submit():
        try:
            ImpairmentService.reverse(
                impairment_id   = impairment_id,
                reversal_date   = form.reversal_date.data,
                reversal_amount = float(form.reversal_amount.data),
                reversal_reason = form.reversal_reason.data,
            )
            flash('Impairment reversal posted.', 'success')
        except ValueError as e:
            flash(str(e), 'danger')
    return redirect(request.referrer or url_for('asset.list_assets'))
 
 
# ─────────────────────────────────────────────────────────────────────────────
# REPORTS
# ─────────────────────────────────────────────────────────────────────────────
 
@asset_bp.route('/reports')
def reports():
    filter_form = ReportFilterForm(request.args)
    as_at_date  = filter_form.as_at_date.data or date.today()
    cat_id      = filter_form.category_id.data or None
    status      = filter_form.status.data or None
 
    q = Asset.query.join(AssetCategory)
    if cat_id:
        q = q.filter(Asset.category_id == int(cat_id))
    if status:
        q = q.filter(Asset.status == status)
    assets = q.order_by(AssetCategory.name, Asset.asset_code).all()
 
    # For each asset, calculate as-at figures
    register_rows = []
    for a in assets:
        schedule = DepreciationService.get_schedule(a.id)
        accum, nbv = _calculate_as_at(a, as_at_date, schedule)
        register_rows.append({
            'asset':    a,
            'accum_dep': accum,
            'nbv':       nbv,
        })
 
    total_cost  = sum(float(r['asset'].cost) for r in register_rows)
    total_accum = sum(float(r['accum_dep']) for r in register_rows)
    total_nbv   = sum(float(r['nbv']) for r in register_rows)
 
    return render_template(
        'asset/reports.html',
        filter_form=filter_form,
        register_rows=register_rows,
        as_at_date=as_at_date,
        total_cost=total_cost,
        total_accum=total_accum,
        total_nbv=total_nbv,
    )
 
 
@asset_bp.route('/reports/auc-spares')
def report_auc_spares():
    data = AssetReportService.auc_and_spares()
    return render_template('asset/auc_spares.html', **data)