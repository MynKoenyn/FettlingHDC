from collections import deque
from decimal import Decimal, InvalidOperation
import csv
import io
import json
import logging
import re

from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify, Response
from flask_login import login_required
from sqlalchemy import desc, and_, or_
from datetime import datetime, timedelta, date
from dateutil import parser

from app import app, db, csrf
from access.guards import require_perm
from models import Personnel
from furnace.models import Furnace, MetalGrade, FurnaceEntry, FurnaceTapTime, SpectroResult, TinCopperCalculation
from furnace.forms import (FurnaceForm, MetalGradeForm, FurnaceEntryForm, EntryFilterForm,
                            TinCopperForm, TinCopperFilterForm, ReportForm)
from furnace import meltcontrol_db

furnace_bp = Blueprint('furnace', __name__, url_prefix='/furnace')


# ══════════════════════════════════════════════════════════════════════
# In-memory log ring buffer, for the /furnace/logs and /furnace/live-logs
# ops pages. Local to this module — the main app doesn't have one.
# ══════════════════════════════════════════════════════════════════════
MAX_LOGS = 200
log_buffer = deque(maxlen=MAX_LOGS)
_IGNORE_LOG_PATHS = ["/live-logs", "/favicon.ico", "/entries/autosave/"]


class _MemoryLogHandler(logging.Handler):
    def emit(self, record):
        entry = f"[{record.levelname}] {record.getMessage()}"
        if any(path in entry for path in _IGNORE_LOG_PATHS):
            return
        log_buffer.append(entry)


_mem_handler = _MemoryLogHandler()
_mem_handler.setLevel(logging.INFO)
logging.getLogger().addHandler(_mem_handler)


# Custom Jinja2 filters
@app.template_filter('from_json')
def from_json_filter(value):
    """Parse JSON string to Python object"""
    if not value:
        return []
    try:
        return json.loads(value)
    except Exception:
        return []


@app.template_filter('parse_iso_time')
def parse_iso_time_filter(value):
    """Parse ISO timestamp to readable time"""
    try:
        dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return dt.strftime('%H:%M:%S')
    except Exception:
        return value


@app.template_filter('temp_cell')
def temp_cell_filter(value):
    """Render a raw vw_measurement_details cell value for the Temp Data table."""
    if value is None:
        return ''
    if isinstance(value, bool):
        return 'Yes' if value else 'No'
    if isinstance(value, datetime):
        return value.strftime('%Y-%m-%d %H:%M:%S')
    if isinstance(value, date):
        return value.strftime('%Y-%m-%d')
    if isinstance(value, (float, Decimal)):
        text_val = f'{value:.4f}'.rstrip('0').rstrip('.')
        return text_val if text_val else '0'
    return value


# ══════════════════════════════════════════════════════════════════════
# Dashboard
# ══════════════════════════════════════════════════════════════════════
@furnace_bp.route('/')
@require_perm('furnace', 'view')
def dashboard():
    """Furnace module overview"""
    total_furnaces = Furnace.query.filter_by(status='Active').count()
    # Scoped to melting personnel, not every active HDC employee.
    total_personnel = Personnel.query.filter(
        Personnel.furnace_role.isnot(None), Personnel.status.is_(True)
    ).count()
    total_grades = MetalGrade.query.count()
    total_entries = FurnaceEntry.query.count()

    today = datetime.now().date()
    today_entries = FurnaceEntry.query.filter_by(date=today).count()

    furnaces_data = []
    furnaces = Furnace.query.filter_by(status='Active').all()

    for furnace in furnaces:
        today_entries_for_furnace = FurnaceEntry.query.filter_by(
            furnace_id=furnace.id,
            date=today
        ).all()

        accumulated_materials = {
            'cast_iron': sum(entry.cast_iron or 0 for entry in today_entries_for_furnace),
            'steel_scrap': sum(entry.steel_scrap or 0 for entry in today_entries_for_furnace),
            'pig_iron': sum(entry.pig_iron or 0 for entry in today_entries_for_furnace),
            'recarb': sum(entry.recarb or 0 for entry in today_entries_for_furnace),
            'ferrosilicon': sum(entry.ferrosilicon or 0 for entry in today_entries_for_furnace),
            'ferromanganese': sum(entry.ferromanganese or 0 for entry in today_entries_for_furnace),
            'iron_sulfide': sum(entry.iron_sulfide or 0 for entry in today_entries_for_furnace),
            'additional_recarb': sum(entry.additional_recarb or 0 for entry in today_entries_for_furnace),
            'additional_fesi': sum(entry.additional_fesi or 0 for entry in today_entries_for_furnace),
            'additional_femn': sum(entry.additional_femn or 0 for entry in today_entries_for_furnace),
            'additional_iron_sulfide': sum(entry.additional_iron_sulfide or 0 for entry in today_entries_for_furnace),
            'tin': sum(entry.tin or 0 for entry in today_entries_for_furnace),
            'copper': sum(entry.copper or 0 for entry in today_entries_for_furnace),
        }

        current_entry = FurnaceEntry.query.filter_by(
            furnace_id=furnace.id,
            status="In Progress"
        ).order_by(FurnaceEntry.start_charging_time.desc()).first()

        furnaces_data.append({
            'furnace': furnace,
            'accumulated_materials': accumulated_materials,
            'total_today': sum(accumulated_materials.values()),
            'entries_today': len(today_entries_for_furnace),
            'current_entry_id': current_entry.id if current_entry else None
        })

    all_furnaces_totals = {
        'cast_iron': sum(f['accumulated_materials']['cast_iron'] for f in furnaces_data),
        'steel_scrap': sum(f['accumulated_materials']['steel_scrap'] for f in furnaces_data),
        'pig_iron': sum(f['accumulated_materials']['pig_iron'] for f in furnaces_data),
        'recarb': sum(f['accumulated_materials']['recarb'] for f in furnaces_data),
        'ferrosilicon': sum(f['accumulated_materials']['ferrosilicon'] for f in furnaces_data),
        'ferromanganese': sum(f['accumulated_materials']['ferromanganese'] for f in furnaces_data),
        'iron_sulfide': sum(f['accumulated_materials']['iron_sulfide'] for f in furnaces_data),
        'additional_recarb': sum(f['accumulated_materials']['additional_recarb'] for f in furnaces_data),
        'additional_fesi': sum(f['accumulated_materials']['additional_fesi'] for f in furnaces_data),
        'additional_femn': sum(f['accumulated_materials']['additional_femn'] for f in furnaces_data),
        'additional_iron_sulfide': sum(f['accumulated_materials']['additional_iron_sulfide'] for f in furnaces_data),
        'tin': sum(f['accumulated_materials']['tin'] for f in furnaces_data),
        'copper': sum(f['accumulated_materials']['copper'] for f in furnaces_data),
    }

    all_furnaces_total = sum(all_furnaces_totals.values())

    return render_template('furnace/dashboard.html',
                            total_furnaces=total_furnaces,
                            total_personnel=total_personnel,
                            total_grades=total_grades,
                            total_entries=total_entries,
                            today_entries=today_entries,
                            furnaces_data=furnaces_data,
                            all_furnaces_totals=all_furnaces_totals,
                            all_furnaces_total=all_furnaces_total)


# ══════════════════════════════════════════════════════════════════════
# Furnaces
# ══════════════════════════════════════════════════════════════════════
@furnace_bp.route('/furnaces')
@require_perm('furnace', 'view')
def furnaces():
    """List all furnaces"""
    furnaces = Furnace.query.order_by(Furnace.name).all()
    return render_template('furnace/furnaces.html', furnaces=furnaces)


@furnace_bp.route('/furnaces/new', methods=['GET', 'POST'])
@require_perm('furnace', 'admin')
def new_furnace():
    """Create new furnace"""
    form = FurnaceForm()
    if form.validate_on_submit():
        furnace = Furnace(
            name=form.name.data,
            capacity=form.capacity.data,
            capacity_unit=form.capacity_unit.data,
            current_lining_number=form.current_lining_number.data,
            status=form.status.data
        )
        db.session.add(furnace)
        db.session.commit()
        flash(f'Furnace "{furnace.name}" has been created successfully!', 'success')
        return redirect(url_for('furnace.furnaces'))
    return render_template('furnace/furnace_form.html', form=form, title='Add New Furnace')


@furnace_bp.route('/furnaces/<int:id>/edit', methods=['GET', 'POST'])
@require_perm('furnace', 'admin')
def edit_furnace(id):
    """Edit existing furnace"""
    furnace = Furnace.query.get_or_404(id)
    form = FurnaceForm(obj=furnace)
    if form.validate_on_submit():
        form.populate_obj(furnace)
        db.session.commit()
        flash(f'Furnace "{furnace.name}" has been updated successfully!', 'success')
        return redirect(url_for('furnace.furnaces'))
    return render_template('furnace/furnace_form.html', form=form, title='Edit Furnace', furnace=furnace)


@furnace_bp.route('/furnaces/<int:id>/delete', methods=['POST'])
@require_perm('furnace', 'admin')
def delete_furnace(id):
    """Deactivate furnace"""
    furnace = Furnace.query.get_or_404(id)
    furnace.status = 'Inactive'
    db.session.commit()
    flash(f'Furnace "{furnace.name}" has been deactivated.', 'warning')
    return redirect(url_for('furnace.furnaces'))


# ══════════════════════════════════════════════════════════════════════
# Metal Grades
# ══════════════════════════════════════════════════════════════════════
@furnace_bp.route('/metal-grades')
@require_perm('furnace', 'view')
def metal_grades():
    """List all metal grades"""
    grades = MetalGrade.query.order_by(MetalGrade.name).all()
    return render_template('furnace/metal_grades.html', grades=grades)


@furnace_bp.route('/metal-grades/new', methods=['GET', 'POST'])
@require_perm('furnace', 'admin')
def new_metal_grade():
    """Create new metal grade"""
    form = MetalGradeForm()
    if form.validate_on_submit():
        grade = MetalGrade(
            name=form.name.data,
            description=form.description.data,
            notes=form.notes.data
        )
        db.session.add(grade)
        db.session.commit()
        flash(f'Metal grade "{grade.name}" has been created successfully!', 'success')
        return redirect(url_for('furnace.metal_grades'))
    return render_template('furnace/metal_grade_form.html', form=form, title='Add New Metal Grade')


@furnace_bp.route('/metal-grades/<int:id>/edit', methods=['GET', 'POST'])
@require_perm('furnace', 'admin')
def edit_metal_grade(id):
    """Edit existing metal grade"""
    grade = MetalGrade.query.get_or_404(id)
    form = MetalGradeForm(obj=grade)
    if form.validate_on_submit():
        form.populate_obj(grade)
        db.session.commit()
        flash(f'Metal grade "{grade.name}" has been updated successfully!', 'success')
        return redirect(url_for('furnace.metal_grades'))
    return render_template('furnace/metal_grade_form.html', form=form, title='Edit Metal Grade', grade=grade)


@furnace_bp.route('/metal-grades/<int:id>/delete', methods=['POST'])
@require_perm('furnace', 'admin')
def delete_metal_grade(id):
    """Delete metal grade"""
    grade = MetalGrade.query.get_or_404(id)
    db.session.delete(grade)
    db.session.commit()
    flash(f'Metal grade "{grade.name}" has been removed.', 'warning')
    return redirect(url_for('furnace.metal_grades'))


# ══════════════════════════════════════════════════════════════════════
# Entries
# ══════════════════════════════════════════════════════════════════════
@furnace_bp.route('/entries')
@require_perm('furnace', 'view')
def entries():
    """List all entries with filtering"""
    form = EntryFilterForm(request.args)

    query = FurnaceEntry.query

    if form.furnace_id.data and form.furnace_id.data != 0:
        query = query.filter(FurnaceEntry.furnace_id == form.furnace_id.data)

    if form.metal_grade_id.data and form.metal_grade_id.data != 0:
        query = query.filter(FurnaceEntry.metal_grade_id == form.metal_grade_id.data)

    if form.date_from.data:
        query = query.filter(FurnaceEntry.date >= form.date_from.data)

    if form.date_to.data:
        query = query.filter(FurnaceEntry.date <= form.date_to.data)

    # total_materials is a Python @property (derived from several columns),
    # not a mapped column, so summing it means pulling the filtered rows —
    # still far cheaper than rendering all of them into the page.
    total_materials = sum(e.total_materials for e in query)

    query = query.order_by(desc(FurnaceEntry.date), desc(FurnaceEntry.created_at))
    page = request.args.get('page', 1, type=int)
    entries_page = query.paginate(page=page, per_page=50, error_out=False)

    furnaces = Furnace.query.all()

    return render_template('furnace/entries.html', entries=entries_page,
                            total_materials=total_materials, form=form, furnaces=furnaces)


@furnace_bp.route('/entries/new', methods=['POST'])
@require_perm('furnace', 'capture')
def new_entry():
    """Create new furnace entry after furnace selection"""
    furnace_id = request.form.get('furnace_id')

    if not furnace_id:
        flash("Please select a furnace.", "error")
        return redirect(url_for('furnace.entries'))

    furnace = Furnace.query.get_or_404(furnace_id)

    existing = FurnaceEntry.query.filter(
        FurnaceEntry.furnace_id == furnace.id,
        FurnaceEntry.status != "Completed"
    ).first()

    if existing:
        flash("There is already an entry in progress for this furnace.", "warning")
        return redirect(url_for('furnace.edit_entry', id=existing.id))

    entry = FurnaceEntry(
        date=datetime.now().date(),
        furnace_id=furnace.id,
        heat_number=0,
        lining_number=furnace.current_lining_number,
        status="In Progress",
        last_activity_at=datetime.now(),
        cast_iron=1700,
        steel_scrap=300,
        pig_iron=0
    )

    db.session.add(entry)
    db.session.commit()

    return redirect(url_for('furnace.edit_entry', id=entry.id))


@furnace_bp.route('/entries/<int:id>/edit', methods=['GET', 'POST'])
@require_perm('furnace', 'capture')
def edit_entry(id):
    """Edit existing furnace entry with auto-save and full POST handling"""
    entry = FurnaceEntry.query.get_or_404(id)
    form = FurnaceEntryForm(obj=entry)

    app.logger.debug(f"Accessing edit_entry id={id}, method={request.method}")

    try:
        tap_events_parsed = [
            {'time': t.tap_time.isoformat() if t.tap_time else None,
             'temp': t.temperature,
             'innoculate': t.innoculate or '',
             'department': t.department or ''}
            for t in entry.tap_events
        ]
        app.logger.debug(f"Initial tap_times from DB (parsed): {tap_events_parsed}")
    except Exception as e:
        app.logger.warning(f"Error preparing tap_events: {e}")
        tap_events_parsed = []

    if form.validate_on_submit():
        app.logger.debug(f"Form validated for entry {id}")

        required_fields = {
            'furnace_id': 'furnace',
            'metal_grade_id': 'metal grade',
            'melt_technician_id': 'melt technician',
            'furnace_operator_id': 'furnace operator'
        }
        for field, label in required_fields.items():
            value = getattr(form, field).data
            app.logger.debug(f"Required field {field} value: {value}")
            if not value or value == 0:
                flash(f"⚠️ Please select {label}.", "error")
                return render_template(
                    'furnace/entry_form.html', form=form, title='Edit Furnace Entry', entry=entry
                )

        entry.heat_number = form.heat_number.data or entry.heat_number
        entry.furnace_id = int(form.furnace_id.data)
        entry.metal_grade_id = int(form.metal_grade_id.data)
        entry.melt_technician_id = int(form.melt_technician_id.data)
        entry.furnace_operator_id = int(form.furnace_operator_id.data)
        entry.lining_number = form.lining_number.data or entry.lining_number
        entry.status = "Completed"
        entry.last_activity_at = datetime.now()

        material_fields = [
            'cast_iron', 'steel_scrap', 'pig_iron', 'recarb', 'ferrosilicon',
            'ferromanganese', 'iron_sulfide', 'additional_recarb', 'additional_fesi',
            'additional_femn', 'additional_iron_sulfide', 'tin', 'copper'
        ]
        for field in material_fields:
            value = getattr(form, field).data
            if value in (None, '', []):
                continue
            try:
                setattr(entry, field, float(value))
            except (TypeError, ValueError) as e:
                app.logger.warning(f"Invalid material {field}: {value} ({e})")

        entry.melt_temperature = form.melt_temperature.data or None
        entry.remarks = form.remarks.data or ''

        ts_fields = ['start_charging_time', 'additions_added_time', 'end_melt_time']
        for ts_field in ts_fields:
            ts_value = getattr(form, ts_field).data
            if ts_value:
                try:
                    setattr(entry, ts_field, parser.isoparse(ts_value))
                except Exception as e:
                    app.logger.error(f"Error parsing {ts_field}: {ts_value} ({e})")

        furnace = Furnace.query.get(entry.furnace_id)
        if furnace:
            old_lining = furnace.current_lining_number or 0
            furnace.current_lining_number = old_lining + 1
            app.logger.debug(f"Furnace lining incremented: {old_lining} -> {furnace.current_lining_number}")

        try:
            db.session.add(entry)
            db.session.commit()
            flash(f'Furnace entry "{entry.heat_number}" saved successfully! '
                  f'Furnace lining number: {furnace.current_lining_number if furnace else "N/A"}',
                  'success')
            return redirect(url_for('furnace.entries'))
        except Exception as e:
            db.session.rollback()
            app.logger.error(f"Full session commit failed for entry {entry.id}: {e}", exc_info=True)
            flash('Error saving entry. Please check all fields and try again.', 'error')
            return render_template('furnace/entry_form.html', form=form, title='Edit Furnace Entry', entry=entry)

    else:
        app.logger.debug(f"Form validation failed for entry {id}: {form.errors}")

    return render_template(
        'furnace/entry_form.html',
        form=form,
        title='Edit Furnace Entry',
        entry=entry,
        entry_id=entry.id,
        tap_times_json=json.dumps(tap_events_parsed)
    )


@furnace_bp.route('/entries/autosave/<int:entry_id>', methods=['POST'])
@require_perm('furnace', 'capture')
def autosave_entry(entry_id):
    """Auto-save a furnace entry, including tap times (safe + idempotent)"""
    entry = FurnaceEntry.query.get_or_404(entry_id)

    try:
        int_fields = [
            'heat_number', 'lining_number', 'furnace_id', 'metal_grade_id',
            'melt_technician_id', 'furnace_operator_id'
        ]

        for field in int_fields:
            value = request.form.get(field)
            if value not in (None, '', 'null'):
                try:
                    setattr(entry, field, int(value))
                except ValueError:
                    app.logger.warning(f"[AUTOSAVE] Invalid int for {field}: {value}")

        float_fields = [
            'cast_iron', 'steel_scrap', 'pig_iron', 'recarb', 'ferrosilicon',
            'ferromanganese', 'iron_sulfide', 'additional_recarb', 'additional_fesi',
            'additional_femn', 'additional_iron_sulfide', 'tin', 'copper',
            'melt_temperature'
        ]

        for field in float_fields:
            value = request.form.get(field)
            if value in (None, '', 'null'):
                setattr(entry, field, None)
                continue
            try:
                setattr(entry, field, float(value))
            except (TypeError, ValueError):
                app.logger.warning(f"[AUTOSAVE] Invalid float for {field}: {value}")
                setattr(entry, field, None)

        remarks = request.form.get('remarks')
        if remarks is not None:
            entry.remarks = remarks

        status = request.form.get('status')
        if status in ['In Progress', 'Completed']:
            entry.status = status

        for ts_field in ['start_charging_time', 'additions_added_time', 'end_melt_time']:
            ts_value = request.form.get(ts_field)
            if ts_value:
                try:
                    setattr(entry, ts_field, parser.isoparse(ts_value))
                except Exception:
                    app.logger.warning(f"[AUTOSAVE] Invalid timestamp {ts_field}: {ts_value}")

        tap_data = request.form.get('tap_times') or "[]"
        try:
            tap_list = json.loads(tap_data)
        except Exception as e:
            tap_list = []
            app.logger.warning(f"[AUTOSAVE] Invalid tap_times JSON: {tap_data} ({e})")

        # Delete old taps first to prevent duplicates
        FurnaceTapTime.query.filter_by(entry_id=entry.id).delete()

        for tap in tap_list:
            try:
                tap_time_parsed = parser.isoparse(tap['time'])
                temperature = float(tap.get('temp') or 0.0)
                innoculate = tap.get('innoculate') or ''
                department = tap.get('department') or ''
                tap_record = FurnaceTapTime(
                    entry_id=entry.id,
                    tap_time=tap_time_parsed,
                    temperature=temperature,
                    innoculate=innoculate,
                    department=department
                )
                db.session.add(tap_record)
            except Exception as e:
                app.logger.warning(f"[AUTOSAVE] Skipping invalid tap {tap}: {e}")

        entry.last_activity_at = datetime.now()

        db.session.add(entry)
        db.session.commit()

        return jsonify({"success": True})

    except Exception as e:
        db.session.rollback()
        app.logger.error(f"[AUTOSAVE] Failed for entry {entry_id}: {e}", exc_info=True)
        return jsonify({"success": False, "error": str(e)})


@furnace_bp.route("/lab-linker/<int:entry_id>", methods=["GET", "POST"])
@require_perm('furnace', 'capture')
def lab_linker(entry_id):
    entry = FurnaceEntry.query.get_or_404(entry_id)

    base_results = SpectroResult.query.filter_by(sample_type="BASE", entry_id=None) \
        .order_by(SpectroResult.measure_date.desc()).all()
    final_results = SpectroResult.query.filter_by(sample_type="FINAL", entry_id=None) \
        .order_by(SpectroResult.measure_date.desc()).all()

    if request.method == "POST":
        base_id = request.form.get("base_id")
        final_id = request.form.get("final_id")

        if base_id:
            base = SpectroResult.query.get(base_id)
            if base:
                base.entry_id = entry.id

        if final_id:
            final = SpectroResult.query.get(final_id)
            if final:
                final.entry_id = entry.id

        db.session.commit()
        return redirect(url_for("furnace.entries"))

    return render_template(
        "furnace/lab_linker.html",
        entry=entry,
        base_results=base_results,
        final_results=final_results
    )


@furnace_bp.route('/entries/<int:id>/timestamp/<action>', methods=['POST'])
@require_perm('furnace', 'capture')
def update_timestamp(id, action):
    """Update timestamp for specific action"""
    entry = FurnaceEntry.query.get_or_404(id)
    current_time = datetime.now()

    if action == 'start_charging':
        entry.start_charging_time = current_time
    elif action == 'additions_added':
        entry.additions_added_time = current_time
    elif action == 'add_tap':
        entry.tap_time = current_time
    elif action == 'end_melt':
        entry.end_melt_time = current_time

    entry.updated_at = current_time
    db.session.commit()

    flash(f'Timestamp for {action.replace("_", " ").title()} recorded successfully!', 'success')
    return redirect(url_for('furnace.edit_entry', id=id))


# ══════════════════════════════════════════════════════════════════════
# Gantt-chart timeline helpers (pure functions, no DB writes)
# ══════════════════════════════════════════════════════════════════════
def _fmt_clock(dt):
    return dt.strftime('%H:%M') if dt else '—'


def _entry_window(entry, start=None):
    """(start, end) of an entry's active span. Start defaults to start_charging_time;
    end falls back through last tap -> end melt -> additions -> start so a not-yet-tapped
    entry still has a (zero-width) window."""
    start = start or entry.start_charging_time
    if not start:
        return None, None
    end = entry.last_tap_time or entry.end_melt_time or entry.additions_added_time or start
    return start, end


def _event_markers(start, additions_added_time, end_melt_time, tap_times):
    """The four key process points for one entry, shape-coded by 'kind'."""
    markers = [{'kind': 'start', 'time': start, 'label': 'Start Charging'}]
    if additions_added_time:
        markers.append({'kind': 'additions', 'time': additions_added_time, 'label': 'Additions Added'})
    if end_melt_time:
        markers.append({'kind': 'endmelt', 'time': end_melt_time, 'label': 'End Melt'})
    for i, tap in enumerate(tap_times):
        markers.append({'kind': 'tap', 'time': tap, 'label': f'Tap {i + 1}'})
    return markers


def _place_bar(start, end, markers, furnace_name, heat_number, domain_start, domain_end):
    """Percentage geometry + clip flags + in-window markers for a single bar, placed
    within the visible [domain_start, domain_end] window. Shared by the single-entry
    timeline (one bar per row) and the activity report (many bars per furnace lane)."""
    span = (domain_end - domain_start).total_seconds() or 1

    def frac(dt):
        return (dt - domain_start).total_seconds() / span

    clip_start = max(start, domain_start)
    clip_end = min(end, domain_end)
    start_label = _fmt_clock(start)
    end_label = _fmt_clock(end)
    return {
        'x': round(frac(clip_start) * 100, 1),
        'w': max(round((frac(clip_end) - frac(clip_start)) * 100, 1), 1.2),
        'clipped_start': start < domain_start,
        'clipped_end': end > domain_end,
        'start_label': start_label,
        'end_label': end_label,
        'heat_number': heat_number,
        'title': f"{furnace_name} · Heat {heat_number} · {start_label}–{end_label}",
        'marker_points': [
            {'x': round(frac(m['time']) * 100, 1), 'kind': m['kind'], 'label': m['label'], 'time': _fmt_clock(m['time'])}
            for m in (markers or [])
            if domain_start <= m['time'] <= domain_end
        ],
    }


def _gantt_gridlines(domain_start, domain_end, multi_day=None, dates_on_ticks=True):
    """Tick lines across the window. The interval widens with the span so a multi-day
    range doesn't cram 72 hourly labels together. Ticks are anchored to midnight so a
    day boundary always lands on a tick (and gets a heavier line).

    When `dates_on_ticks` is set and the window spans multiple days, midnight ticks carry
    a bold date label instead of a clock time (used by the single-entry timeline). The
    activity report turns this off and shows dates in a separate centered day band
    instead — see `_gantt_day_bands` — which also labels partial days at the edges.

    `multi_day` decides whether to show those on-tick date labels; if not given it's
    inferred from the domain, but callers should pass it based on the real range."""
    span_seconds = (domain_end - domain_start).total_seconds() or 1
    span_hours = span_seconds / 3600
    if span_hours <= 12:
        step = 1
    elif span_hours <= 30:
        step = 3
    elif span_hours <= 120:
        step = 6
    else:
        step = 24
    if multi_day is None:
        multi_day = domain_end.date() != domain_start.date()

    gridlines = []
    tick = domain_start.replace(hour=0, minute=0, second=0, microsecond=0)
    while tick < domain_start:
        tick += timedelta(hours=step)
    while tick <= domain_end:
        is_day_start = tick.hour == 0
        gridlines.append({
            'x': round((tick - domain_start).total_seconds() / span_seconds * 100, 1),
            'label': tick.strftime('%H:%M'),
            'is_day_start': is_day_start,
            'day_label': tick.strftime('%a %d %b') if (dates_on_ticks and multi_day and is_day_start) else None,
        })
        tick += timedelta(hours=step)
    return gridlines


def _gantt_day_bands(domain_start, domain_end):
    """One centered date label per calendar day visible in the window, for a two-tier
    axis (dates on top, hour ticks below). Each day's label is centered over that day's
    *visible* slice, so a partial day at either edge is still labelled — unless its
    visible sliver is too thin to hold text (e.g. a few minutes of padding), which is
    skipped to avoid a cramped stray date."""
    span_seconds = (domain_end - domain_start).total_seconds() or 1
    bands = []
    day = domain_start.replace(hour=0, minute=0, second=0, microsecond=0)
    while day <= domain_end:
        next_day = day + timedelta(days=1)
        vis_start = max(day, domain_start)
        vis_end = min(next_day, domain_end)
        width_pct = (vis_end - vis_start).total_seconds() / span_seconds * 100
        if width_pct >= 4:
            center = vis_start + (vis_end - vis_start) / 2
            bands.append({
                'x': round((center - domain_start).total_seconds() / span_seconds * 100, 1),
                'label': day.strftime('%a %d %b'),
            })
        day = next_day
    return bands


def _layout_gantt_rows(rows, domain_start, domain_end):
    """Add per-row geometry to rows carrying start/end/markers (one bar each), for the
    single-entry timeline. Mutates and returns rows plus the gridlines."""
    for r in rows:
        r.update(_place_bar(r['start'], r['end'], r.get('markers'),
                             r['furnace_name'], r['heat_number'], domain_start, domain_end))
    return rows, _gantt_gridlines(domain_start, domain_end)


def _build_furnace_gantt(entry, timeline_start, timeline_end, concurrent_entries):
    """Chart data for a small, stationary (non-scrolling) timeline: this entry's
    process span, plus any other-furnace entries whose active window overlaps it."""
    if not timeline_start:
        return None

    timeline_end = timeline_end or timeline_start
    entry_span = (timeline_end - timeline_start).total_seconds()
    padding = max(entry_span * 0.15, 900)
    domain_start = timeline_start - timedelta(seconds=padding)
    domain_end = timeline_end + timedelta(seconds=padding)

    rows = [{
        'is_current': True,
        'color_class': 'gantt-color-current',
        'muted': False,
        'furnace_name': entry.furnace_ref.name if entry.furnace_ref else 'Unassigned',
        'heat_number': entry.heat_number or '—',
        'start': timeline_start,
        'end': timeline_end,
        'markers': _event_markers(timeline_start, entry.additions_added_time, entry.end_melt_time, entry.sorted_tap_times),
    }]

    for c in concurrent_entries:
        other = c['entry']
        rows.append({
            'is_current': False,
            'color_class': 'gantt-color-other',
            'muted': True,
            'furnace_name': other.furnace_ref.name if other.furnace_ref else 'Unassigned',
            'heat_number': other.heat_number or '—',
            'start': c['start'],
            'end': c['end'],
            'markers': _event_markers(c['start'], other.additions_added_time, other.end_melt_time, other.sorted_tap_times),
        })

    rows, gridlines = _layout_gantt_rows(rows, domain_start, domain_end)
    return {'rows': rows, 'gridlines': gridlines}


def _furnace_color_class(furnace_id):
    """Stable categorical color slot for a furnace, keyed off the furnace id."""
    return f"gantt-color-{((furnace_id - 1) % 8) + 1}" if furnace_id else "gantt-color-other"


def _build_activity_gantt(entries, range_start_dt, range_end_dt):
    """Swim-lane timeline for a date range: one lane per furnace, every heat that
    overlaps the range shown as a bar in its furnace's lane, colored per furnace."""
    placed = []
    for e in entries:
        start, end = _entry_window(e)
        if not start:
            continue
        if start <= range_end_dt and end >= range_start_dt:
            placed.append((e.furnace_ref, start, end, e))

    if not placed:
        return None

    lo = max(range_start_dt, min(p[1] for p in placed))
    hi = min(range_end_dt, max(p[2] for p in placed))
    if hi <= lo:
        hi = lo + timedelta(hours=1)
    pad = max((hi - lo).total_seconds() * 0.03, 600)
    domain_start = lo - timedelta(seconds=pad)
    domain_end = hi + timedelta(seconds=pad)

    lanes_by_id = {}
    for furnace, start, end, e in placed:
        fid = furnace.id if furnace else 0
        lane = lanes_by_id.get(fid)
        if lane is None:
            lane = {
                'furnace_id': fid,
                'furnace_name': furnace.name if furnace else 'Unassigned',
                'color_class': _furnace_color_class(fid),
                'segments': [],
            }
            lanes_by_id[fid] = lane
        markers = _event_markers(start, e.additions_added_time, e.end_melt_time, e.sorted_tap_times)
        lane['segments'].append(
            _place_bar(start, end, markers, lane['furnace_name'], e.heat_number or '—', domain_start, domain_end)
        )

    lanes = sorted(lanes_by_id.values(), key=lambda ln: ln['furnace_id'])
    for lane in lanes:
        lane['entry_count'] = len(lane['segments'])

    MARKER_BUDGET = 45
    densest_lane = max(
        (sum(len(s['marker_points']) for s in lane['segments']) for lane in lanes),
        default=0,
    )
    show_markers = densest_lane <= MARKER_BUDGET
    if not show_markers:
        for lane in lanes:
            for s in lane['segments']:
                s['marker_points'] = []

    multi_day = hi.date() != lo.date()

    return {
        'lanes': lanes,
        'gridlines': _gantt_gridlines(domain_start, domain_end, multi_day=multi_day, dates_on_ticks=False),
        'day_bands': _gantt_day_bands(domain_start, domain_end) if multi_day else [],
        'entry_count': len(placed),
        'show_markers': show_markers,
        'multi_day': multi_day,
    }


@furnace_bp.route('/reports/furnace-activity')
@require_perm('furnace', 'view')
def furnace_activity_report():
    """Standalone report: a per-furnace swim-lane timeline of every heat whose active
    window falls within a selected date range, each furnace in its own color."""
    today = datetime.now().date()

    date_from_raw = request.args.get('date_from')
    date_to_raw = request.args.get('date_to')
    try:
        date_from = datetime.strptime(date_from_raw, '%Y-%m-%d').date() if date_from_raw else today
    except ValueError:
        date_from = today
    try:
        date_to = datetime.strptime(date_to_raw, '%Y-%m-%d').date() if date_to_raw else today
    except ValueError:
        date_to = today
    if date_to < date_from:
        date_from, date_to = date_to, date_from

    range_start_dt = datetime.combine(date_from, datetime.min.time())
    range_end_dt = datetime.combine(date_to, datetime.max.time())

    entries = FurnaceEntry.query.filter(
        FurnaceEntry.furnace_id.isnot(None),
        FurnaceEntry.start_charging_time.isnot(None),
        FurnaceEntry.date.between(date_from - timedelta(days=1), date_to + timedelta(days=1))
    ).order_by(FurnaceEntry.start_charging_time).all()

    activity = _build_activity_gantt(entries, range_start_dt, range_end_dt)

    single_day = date_from == date_to
    return render_template(
        'furnace/furnace_activity_report.html',
        activity=activity,
        date_from=date_from.isoformat(),
        date_to=date_to.isoformat(),
        date_from_display=date_from.strftime('%d %b %Y'),
        date_to_display=date_to.strftime('%d %b %Y'),
        single_day=single_day,
        generated_at=datetime.now().strftime('%d %b %Y %H:%M'),
    )


@furnace_bp.route('/entries/<int:id>')
@require_perm('furnace', 'view')
def entry_detail(id):
    """View entry details"""
    entry = FurnaceEntry.query.get_or_404(id)

    tap_times = FurnaceTapTime.query.filter_by(entry_id=entry.id).order_by(FurnaceTapTime.tap_time).all()
    spectro_results = SpectroResult.query.filter_by(entry_id=entry.id).order_by(SpectroResult.measure_date, SpectroResult.measure_time).all()

    taps_with_gaps = []
    previous_tap_time = None
    for tap in tap_times:
        gap_display = None
        if previous_tap_time and tap.tap_time:
            gap_display = FurnaceEntry.format_duration_seconds((tap.tap_time - previous_tap_time).total_seconds())
        taps_with_gaps.append((tap, gap_display))
        previous_tap_time = tap.tap_time

    timeline_start = entry.start_charging_time
    timeline_end = entry.last_tap_time or entry.end_melt_time or entry.additions_added_time or timeline_start

    concurrent_entries = []
    if timeline_start and entry.furnace_id:
        candidates = FurnaceEntry.query.filter(
            FurnaceEntry.id != entry.id,
            FurnaceEntry.furnace_id.isnot(None),
            FurnaceEntry.furnace_id != entry.furnace_id,
            FurnaceEntry.date.between(entry.date - timedelta(days=1), entry.date + timedelta(days=1))
        ).all()

        for other in candidates:
            other_start = other.start_charging_time
            if not other_start:
                continue
            other_end = other.last_tap_time or other.end_melt_time or other.additions_added_time or other_start
            if other_start <= timeline_end and other_end >= timeline_start:
                concurrent_entries.append({'entry': other, 'start': other_start, 'end': other_end})

        concurrent_entries.sort(key=lambda c: c['start'])

    gantt = _build_furnace_gantt(entry, timeline_start, timeline_end, concurrent_entries)

    return render_template(
        'furnace/entry_detail.html',
        entry=entry,
        tap_times=tap_times,
        taps_with_gaps=taps_with_gaps,
        spectro_results=spectro_results,
        concurrent_entries=concurrent_entries,
        gantt=gantt,
    )


@furnace_bp.route('/history')
@require_perm('furnace', 'view')
def history():
    """View historical entries (read-only)"""
    form = EntryFilterForm(request.args)

    query = FurnaceEntry.query

    if form.furnace_id.data and form.furnace_id.data != 0:
        query = query.filter(FurnaceEntry.furnace_id == form.furnace_id.data)

    if form.metal_grade_id.data and form.metal_grade_id.data != 0:
        query = query.filter(FurnaceEntry.metal_grade_id == form.metal_grade_id.data)

    if form.date_from.data:
        query = query.filter(FurnaceEntry.date >= form.date_from.data)

    if form.date_to.data:
        query = query.filter(FurnaceEntry.date <= form.date_to.data)

    all_matching = query.all()
    total_materials = sum(e.total_materials for e in all_matching)
    avg_materials = (total_materials / len(all_matching)) if all_matching else 0

    query = query.order_by(desc(FurnaceEntry.date), desc(FurnaceEntry.created_at))
    page = request.args.get('page', 1, type=int)
    entries_page = query.paginate(page=page, per_page=50, error_out=False)

    return render_template('furnace/history.html', entries=entries_page,
                            total_materials=total_materials, avg_materials=avg_materials, form=form)


# ══════════════════════════════════════════════════════════════════════
# Reports
# ══════════════════════════════════════════════════════════════════════
ELEMENT_COLUMNS = [
    "C", "Si", "Mn", "P", "S", "Cr", "Mo", "Ni", "Al", "Co",
    "Cu", "Nb", "Ti", "V", "W", "Pb", "Sn", "Mg", "As", "Zr",
    "Bi", "Ce", "Sb", "Se", "Te", "B", "Zn", "La", "N", "Fe"
]

ELEMENT_NAMES = {
    "C": "Carbon", "Si": "Silicon", "Mn": "Manganese", "P": "Phosphorus",
    "S": "Sulfur", "Cr": "Chromium", "Mo": "Molybdenum", "Ni": "Nickel",
    "Al": "Aluminium", "Co": "Cobalt", "Cu": "Copper", "Nb": "Niobium",
    "Ti": "Titanium", "V": "Vanadium", "W": "Tungsten", "Pb": "Lead",
    "Sn": "Tin", "Mg": "Magnesium", "As": "Arsenic", "Zr": "Zirconium",
    "Bi": "Bismuth", "Ce": "Cerium", "Sb": "Antimony", "Se": "Selenium",
    "Te": "Tellurium", "B": "Boron", "Zn": "Zinc", "La": "Lanthanum",
    "N": "Nitrogen", "Fe": "Iron"
}


@furnace_bp.route('/reports', methods=['GET', 'POST'])
@require_perm('furnace', 'view')
def reports():
    """Generate reports"""
    form = ReportForm()
    report_data = None
    spectro_data = None

    if form.validate_on_submit():
        query = FurnaceEntry.query.filter(
            FurnaceEntry.date >= form.date_from.data,
            FurnaceEntry.date <= form.date_to.data,
            FurnaceEntry.status != 'In Progress'
        )

        if form.furnace_id.data and form.furnace_id.data != 0:
            query = query.filter(FurnaceEntry.furnace_id == form.furnace_id.data)

        entries = query.order_by(FurnaceEntry.date).all()

        time_from = form.time_from.data
        time_to = form.time_to.data
        if time_from and time_to and time_from != time_to:
            if time_from < time_to:
                entries = [
                    e for e in entries
                    if e.start_charging_time and time_from <= e.start_charging_time.time() <= time_to
                ]
            else:
                entries = [
                    e for e in entries
                    if e.start_charging_time and (e.start_charging_time.time() >= time_from or e.start_charging_time.time() <= time_to)
                ]

        if entries:
            total_entries = len(entries)
            total_materials = sum(entry.total_materials or 0 for entry in entries)
            avg_materials = total_materials / total_entries if total_entries > 0 else 0

            material_totals = {
                'cast_iron': sum(entry.cast_iron or 0 for entry in entries),
                'steel_scrap': sum(entry.steel_scrap or 0 for entry in entries),
                'pig_iron': sum(entry.pig_iron or 0 for entry in entries),
                'recarb': sum(entry.recarb or 0 for entry in entries),
                'ferrosilicon': sum(entry.ferrosilicon or 0 for entry in entries),
                'ferromanganese': sum(entry.ferromanganese or 0 for entry in entries),
                'iron_sulfide': sum(entry.iron_sulfide or 0 for entry in entries),
                'additional_recarb': sum(entry.additional_recarb or 0 for entry in entries),
                'additional_fesi': sum(entry.additional_fesi or 0 for entry in entries),
                'additional_femn': sum(entry.additional_femn or 0 for entry in entries),
                'additional_iron_sulfide': sum(entry.additional_iron_sulfide or 0 for entry in entries),
                'tin': sum(entry.tin or 0 for entry in entries),
                'copper': sum(entry.copper or 0 for entry in entries)
            }

            furnace_usage = {}
            for entry in entries:
                furnace_name = entry.furnace_ref.name if entry.furnace_ref else "Unknown"
                furnace_usage[furnace_name] = furnace_usage.get(furnace_name, 0) + 1

            duration_seconds_by_metric = {
                'melt_time': [e.melt_time_seconds for e in entries if e.melt_time_seconds is not None],
                'corrections': [e.corrections_seconds for e in entries if e.corrections_seconds is not None],
                'furnace_emptying': [e.furnace_emptying_seconds for e in entries if e.furnace_emptying_seconds is not None],
                'full_melt': [e.full_melt_seconds for e in entries if e.full_melt_seconds is not None],
            }
            duration_stats = {}
            for metric, values in duration_seconds_by_metric.items():
                duration_stats[metric] = {
                    'count': len(values),
                    'avg': FurnaceEntry.format_duration_seconds(sum(values) / len(values) if values else None),
                    'min': FurnaceEntry.format_duration_seconds(min(values) if values else None),
                    'max': FurnaceEntry.format_duration_seconds(max(values) if values else None),
                }

            # Melt Cycle: first-tap-to-first-tap gap between consecutive entries on the
            # same furnace. Gaps longer than the non-working threshold (e.g. weekends,
            # furnace idle) are excluded from the average/min/max but still counted.
            NON_WORKING_GAP_SECONDS = 12 * 3600
            entries_by_furnace = {}
            for entry in entries:
                if entry.first_tap_time:
                    entries_by_furnace.setdefault(entry.furnace_id, []).append(entry)

            melt_cycle_seconds = []
            melt_cycle_excluded = 0
            for furnace_entries in entries_by_furnace.values():
                furnace_entries.sort(key=lambda e: e.first_tap_time)
                for prev_entry, next_entry in zip(furnace_entries, furnace_entries[1:]):
                    gap = (next_entry.first_tap_time - prev_entry.first_tap_time).total_seconds()
                    if gap > NON_WORKING_GAP_SECONDS:
                        melt_cycle_excluded += 1
                    else:
                        melt_cycle_seconds.append(gap)

            duration_stats['melt_cycle'] = {
                'count': len(melt_cycle_seconds),
                'excluded': melt_cycle_excluded,
                'avg': FurnaceEntry.format_duration_seconds(sum(melt_cycle_seconds) / len(melt_cycle_seconds) if melt_cycle_seconds else None),
                'min': FurnaceEntry.format_duration_seconds(min(melt_cycle_seconds) if melt_cycle_seconds else None),
                'max': FurnaceEntry.format_duration_seconds(max(melt_cycle_seconds) if melt_cycle_seconds else None),
            }

            raw_spectro = SpectroResult.query.filter(
                SpectroResult.measure_date >= form.date_from.data,
                SpectroResult.measure_date <= form.date_to.data
            ).order_by(SpectroResult.measure_date, SpectroResult.measure_time).all()

            spectro_data = []
            for r in raw_spectro:
                elements = {col: getattr(r, "ele_" + col.lower()) or 0 for col in ELEMENT_COLUMNS}
                spectro_data.append({
                    "heat_number": r.heat_number,
                    "measure_date": r.measure_date,
                    "measure_time": r.measure_time,
                    "furnace": r.furnace,
                    "pot_number": r.pot_number,
                    "melt_technician": r.melt_technician,
                    "grade_id": r.grade_id,
                    "sample_type": r.sample_type,
                    "cu_addition": float(r.cu_addition or 0),
                    "sn_addition": float(r.sn_addition or 0),
                    "elements": elements
                })

            report_data = {
                'entries': entries,
                'total_entries': total_entries,
                'total_materials': total_materials,
                'avg_materials': avg_materials,
                'material_totals': material_totals,
                'furnace_usage': furnace_usage,
                'duration_stats': duration_stats,
                'date_range': f"{form.date_from.data} to {form.date_to.data}",
                'time_range': f"{time_from.strftime('%H:%M')} to {time_to.strftime('%H:%M')}" if time_from and time_to else None
            }

    return render_template(
        'furnace/reports.html',
        form=form,
        report_data=report_data,
        spectro_data=spectro_data,
        ELEMENT_COLUMNS=ELEMENT_COLUMNS,
        ELEMENT_NAMES=ELEMENT_NAMES
    )


@furnace_bp.route('/reports/export_spectro')
@require_perm('furnace', 'view')
def export_spectro():
    """Export the date-filtered lab (spectro) results as a CSV download."""
    date_from = request.args.get('date_from')
    date_to = request.args.get('date_to')

    query = SpectroResult.query
    if date_from:
        try:
            query = query.filter(
                SpectroResult.measure_date >= datetime.strptime(date_from, '%Y-%m-%d').date()
            )
        except ValueError:
            pass
    if date_to:
        try:
            query = query.filter(
                SpectroResult.measure_date <= datetime.strptime(date_to, '%Y-%m-%d').date()
            )
        except ValueError:
            pass

    results = query.order_by(
        SpectroResult.measure_date, SpectroResult.measure_time
    ).all()

    output = io.StringIO()
    writer = csv.writer(output)

    header = ['Heat', 'Date', 'Time', 'Furnace', 'Technician', 'Grade', 'Sample Type']
    header += [ELEMENT_NAMES[col] for col in ELEMENT_COLUMNS]
    writer.writerow(header)

    for r in results:
        row = [
            r.heat_number, r.measure_date, r.measure_time, r.furnace,
            r.melt_technician, r.grade_id, r.sample_type,
        ]
        row += [getattr(r, 'ele_' + col.lower()) or 0 for col in ELEMENT_COLUMNS]
        writer.writerow(row)

    filename = f"lab_results_{date_from or 'all'}_to_{date_to or 'all'}.csv"
    return Response(
        output.getvalue(),
        mimetype='text/csv',
        headers={'Content-Disposition': f'attachment; filename={filename}'}
    )


# ══════════════════════════════════════════════════════════════════════
# Temp Data (MeltControl vw_measurement_details, separate Postgres server)
# ══════════════════════════════════════════════════════════════════════
TEMP_DATA_DISPLAY_LIMIT = 500


def _parse_temp_data_filters():
    """Read date_from/date_to/station_name from the query string, defaulting
    the date range to today."""
    today = datetime.now().date()

    date_from_raw = request.args.get('date_from')
    date_to_raw = request.args.get('date_to')
    station_name = request.args.get('station_name') or None

    try:
        date_from = datetime.strptime(date_from_raw, '%Y-%m-%d').date() if date_from_raw else today
    except ValueError:
        date_from = today

    try:
        date_to = datetime.strptime(date_to_raw, '%Y-%m-%d').date() if date_to_raw else today
    except ValueError:
        date_to = today

    return date_from, date_to, station_name


def _parse_temp_data_columns(all_columns):
    """Read the selected column checkboxes from the query string.

    'columns_submitted' is a hidden field always sent with the filter form,
    so we can tell "form submitted with nothing checked" (columns_submitted
    present, columns empty -> falls back to all) apart from "first visit,
    no filters applied yet" (columns_submitted absent -> defaults to all).
    """
    if 'columns_submitted' in request.args:
        selected = [c for c in request.args.getlist('columns') if c in all_columns]
        if not selected:
            selected = list(all_columns)
    else:
        selected = list(all_columns)
    return selected


@furnace_bp.route('/temp_data')
@require_perm('furnace', 'view')
def temp_data():
    """Display Temp Data pulled live from the MeltControl vw_measurement_details view."""
    date_from, date_to, station_name = _parse_temp_data_filters()

    columns, rows, stations, all_columns = [], [], [], []
    chart_points = []
    error = None

    try:
        stations = meltcontrol_db.fetch_stations()
        all_columns = meltcontrol_db.fetch_all_columns()
    except Exception as e:
        logging.exception("Failed to load MeltControl metadata")
        error = f"Could not connect to the MeltControl database: {e}"

    selected_columns = _parse_temp_data_columns(all_columns)

    if error is None:
        try:
            columns, rows = meltcontrol_db.fetch_measurements(
                date_from, date_to, station_name,
                select_columns=selected_columns, limit=TEMP_DATA_DISPLAY_LIMIT
            )
            _, chart_rows = meltcontrol_db.fetch_measurements(
                date_from, date_to, station_name,
                select_columns=["TimeStamp", "StationName", "Temp"], limit=TEMP_DATA_DISPLAY_LIMIT
            )
            for ts, st, temp in chart_rows:
                if ts is not None and temp is not None:
                    chart_points.append({"t": int(ts.timestamp() * 1000), "s": st, "v": float(temp)})
        except Exception as e:
            logging.exception("Failed to load MeltControl measurement data")
            error = f"Could not load temperature data: {e}"

    return render_template(
        'furnace/temp_data.html',
        columns=columns,
        rows=rows,
        stations=stations,
        all_columns=all_columns,
        selected_columns=selected_columns,
        date_from=date_from,
        date_to=date_to,
        station_name=station_name,
        error=error,
        display_limit=TEMP_DATA_DISPLAY_LIMIT,
        row_count=len(rows),
        chart_points=chart_points,
    )


@furnace_bp.route('/temp_data/export')
@require_perm('furnace', 'view')
def export_temp_data():
    """Export the currently filtered Temp Data (no row limit) as a CSV download."""
    date_from, date_to, station_name = _parse_temp_data_filters()

    try:
        all_columns = meltcontrol_db.fetch_all_columns()
        selected_columns = _parse_temp_data_columns(all_columns)
        columns, rows = meltcontrol_db.fetch_measurements(
            date_from, date_to, station_name, select_columns=selected_columns, limit=None
        )
    except Exception as e:
        logging.exception("Failed to export MeltControl measurement data")
        flash(f"Could not export temperature data: {e}", "danger")
        return redirect(url_for(
            'furnace.temp_data', date_from=date_from.isoformat(), date_to=date_to.isoformat(),
            station_name=station_name or ''
        ))

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(columns)
    writer.writerows(rows)

    safe_station = re.sub(r'[^A-Za-z0-9]+', '_', station_name).strip('_') if station_name else 'all_stations'
    filename = f"temp_data_{date_from}_to_{date_to}_{safe_station}.csv"

    # UTF-8 BOM so Excel renders non-ASCII column headers (e.g. TE_A (°)) correctly
    return Response(
        '﻿' + output.getvalue(),
        mimetype='text/csv',
        headers={'Content-Disposition': f'attachment; filename={filename}'}
    )


@furnace_bp.route('/api/furnace/<int:furnace_id>')
@require_perm('furnace', 'view')
def get_furnace_data(furnace_id):
    """Get furnace data for AJAX requests"""
    furnace = Furnace.query.get_or_404(furnace_id)
    return {
        'id': furnace.id,
        'name': furnace.name,
        'current_lining_number': furnace.current_lining_number,
        'capacity': furnace.capacity,
        'status': furnace.status
    }


# ══════════════════════════════════════════════════════════════════════
# External device ingestion — no login, CSRF-exempt (hardware POSTs here)
# ══════════════════════════════════════════════════════════════════════
def safe_decimal(value, default=Decimal("0.0")):
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return default


@furnace_bp.route("/api/data", methods=["POST"])
@csrf.exempt
def receive_data():
    try:
        data = request.get_json()
        if not data:
            logging.warning("No JSON data provided")
            return jsonify({"error": "No JSON data provided"}), 400

        logging.info(f"Received data: {data}")
        melt_id = data.get("melt_id", None)
        logging.info(f"Received Melt ID: {melt_id}")

        measure_date = data.get("measure_date")
        measure_time = data.get("measure_time")
        furnace_name = data.get("furnace")
        if not measure_date or not measure_time:
            logging.warning("Missing measure_date or measure_time")
            return jsonify({"error": "Missing date or time"}), 400

        measure_dt = datetime.strptime(f"{measure_date} {measure_time}", "%d.%m.%Y %H:%M:%S")

        elements = data.get("elements", {})
        db_elements = {}
        for e in ELEMENT_COLUMNS:
            val = elements.get(e.lower())
            if val in [None, '']:
                db_elements[f"ele_{e.lower()}"] = None
            else:
                try:
                    db_elements[f"ele_{e.lower()}"] = Decimal(str(val))
                except (InvalidOperation, ValueError):
                    logging.warning(f"Invalid element value for {e}: {val}")
                    db_elements[f"ele_{e.lower()}"] = None

        cu_add = safe_decimal(data.get("cu_addition", 0))
        sn_add = safe_decimal(data.get("sn_addition", 0))

        entry_id = None

        logging.info(
            f"[LINK START] Attempting to link lab result | "
            f"Furnace='{furnace_name}' | measure_dt={measure_dt}"
        )

        furnace = Furnace.query.filter_by(name=furnace_name).first()

        if not furnace:
            logging.warning(f"[LINK FAIL] Furnace '{furnace_name}' not found in database")
        else:
            logging.info(f"[LINK INFO] Furnace found | id={furnace.id} | name={furnace.name}")

            candidate_entries = (
                FurnaceEntry.query
                .filter(FurnaceEntry.furnace_id == furnace.id, FurnaceEntry.status == "In Progress")
                .order_by(FurnaceEntry.created_at.desc())
                .all()
            )

            if not candidate_entries:
                logging.warning(f"[LINK FAIL] No FurnaceEntry records exist for furnace '{furnace.name}'")
            else:
                matched_entry = None

                for e in candidate_entries:
                    delta = measure_dt - e.created_at
                    within_3_hours = delta.total_seconds() >= 0 and delta.total_seconds() <= 10800

                    activity_valid = (
                        e.last_activity_at is None
                        or e.last_activity_at >= measure_dt
                    )

                    logging.info(
                        f"[CANDIDATE CHECK] EntryID={e.id} | created_at={e.created_at} | "
                        f"last_activity_at={e.last_activity_at} | time_diff={delta}"
                    )
                    logging.info(
                        f"    Conditions -> created_before_measure={e.created_at <= measure_dt} | "
                        f"within_3_hours={within_3_hours} | activity_valid={activity_valid}"
                    )

                    if e.created_at <= measure_dt and within_3_hours and activity_valid:
                        matched_entry = e
                        break

                if matched_entry:
                    entry_id = matched_entry.id
                    logging.info(
                        f"[LINK SUCCESS] Linked SpectroResult to FurnaceEntry "
                        f"id={entry_id} | created_at={matched_entry.created_at}"
                    )
                else:
                    logging.warning(
                        f"[LINK FAIL] No entry satisfied all conditions "
                        f"for furnace '{furnace.name}' at {measure_dt}"
                    )

        record = SpectroResult(
            entry_id=entry_id,
            measure_date=measure_dt.date(),
            measure_time=measure_dt.time(),
            melt_technician=data.get("melt_technician"),
            grade_id=data.get("grade_id"),
            heat_number=data.get("heat_number"),
            plant=data.get("plant"),
            furnace=furnace_name,
            sample_type=data.get("sample_type"),
            pot_number=data.get("pot_number"),
            metal_grade=data.get("metal_grade"),
            cu_addition=cu_add,
            sn_addition=sn_add,
            **db_elements
        )

        db.session.add(record)
        db.session.commit()
        logging.info(f"Spectro data saved successfully: id={record.id}, entry_id={entry_id}")

        return jsonify({"status": "success", "id": record.id, "entry_id": entry_id})

    except Exception as e:
        db.session.rollback()
        logging.exception(f"Error saving spectro data: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500


# ══════════════════════════════════════════════════════════════════════
# Tin & Copper Calculation
# ══════════════════════════════════════════════════════════════════════
def _calc_tin_to_be_added(grade_name, base_tin, weight):
    if base_tin is None:
        return None
    if grade_name in ('GG25', 'GG30'):
        return (0.075 - base_tin) * weight / 100
    if grade_name in ('SG60', 'SG50'):
        return 0.0
    return base_tin


def _calc_copper_to_be_added(grade_name, base_copper, weight):
    if base_copper is None:
        return None
    targets = {'GG25': 0.2, 'GG30': 0.9, 'SG60': 0.45, 'SG50': 0.4}
    if grade_name in targets:
        return (targets[grade_name] - base_copper) * weight / 99
    return base_copper


def _get_daily_issued(target_date, exclude_id=None):
    """Return the record that already carries issued values for a given date, or None."""
    q = TinCopperCalculation.query.filter(
        TinCopperCalculation.date == target_date,
        or_(
            and_(TinCopperCalculation.tin_issued.isnot(None), TinCopperCalculation.tin_issued > 0),
            and_(TinCopperCalculation.copper_issued.isnot(None), TinCopperCalculation.copper_issued > 0),
        )
    )
    if exclude_id:
        q = q.filter(TinCopperCalculation.id != exclude_id)
    return q.first()


@furnace_bp.route('/tin-copper')
@require_perm('furnace', 'view')
def tin_copper():
    filter_form = TinCopperFilterForm(request.args, meta={'csrf': False})
    query = TinCopperCalculation.query

    furnace_id = request.args.get('furnace_id', 0, type=int)
    grade_id = request.args.get('metal_grade_id', 0, type=int)
    date_from = request.args.get('date_from')
    date_to = request.args.get('date_to')

    if furnace_id:
        query = query.filter(TinCopperCalculation.furnace_id == furnace_id)
    if grade_id:
        query = query.filter(TinCopperCalculation.metal_grade_id == grade_id)
    if date_from:
        try:
            query = query.filter(TinCopperCalculation.date >= datetime.strptime(date_from, '%Y-%m-%d').date())
        except ValueError:
            pass
    if date_to:
        try:
            query = query.filter(TinCopperCalculation.date <= datetime.strptime(date_to, '%Y-%m-%d').date())
        except ValueError:
            pass

    records = query.order_by(desc(TinCopperCalculation.date), desc(TinCopperCalculation.id)).all()

    def _to_add(value):
        """A negative 'to be added' amount means the melt is already at/above target -> counts as 0."""
        return value if value and value > 0 else 0

    totals = {
        'weight': sum(r.weight or 0 for r in records),
        'tin_to_be_added': sum(_to_add(r.tin_to_be_added) for r in records),
        'tin_added': sum(r.tin_added or 0 for r in records),
        'copper_to_be_added': sum(_to_add(r.copper_to_be_added) for r in records),
        'copper_added': sum(r.copper_added or 0 for r in records),
        'tin_issued': sum(r.tin_issued or 0 for r in records),
        'copper_issued': sum(r.copper_issued or 0 for r in records),
    }

    return render_template('furnace/tin_copper.html', records=records, filter_form=filter_form, totals=totals)


@furnace_bp.route('/tin-copper/new', methods=['GET', 'POST'])
@require_perm('furnace', 'capture')
def tin_copper_new():
    form = TinCopperForm()
    grades = MetalGrade.query.all()

    if request.method == 'GET':
        last = TinCopperCalculation.query.order_by(
            desc(TinCopperCalculation.date), desc(TinCopperCalculation.id)
        ).first()
        if last:
            form.starting_tin.data = round(
                (last.starting_tin or 0) + (last.tin_issued or 0) - (last.tin_added or 0), 4
            )
            form.starting_copper.data = round(
                (last.starting_copper or 0) + (last.copper_issued or 0) - (last.copper_added or 0), 4
            )

    if form.validate_on_submit():
        existing = _get_daily_issued(form.date.data)
        if existing:
            if form.tin_issued.data and form.tin_issued.data > 0:
                flash(f'Tin Issued already recorded for {form.date.data.strftime("%d/%m/%Y")} on record #{existing.id}. Only one entry per day is allowed.', 'danger')
            if form.copper_issued.data and form.copper_issued.data > 0:
                flash(f'Copper Issued already recorded for {form.date.data.strftime("%d/%m/%Y")} on record #{existing.id}. Only one entry per day is allowed.', 'danger')
            if (form.tin_issued.data and form.tin_issued.data > 0) or (form.copper_issued.data and form.copper_issued.data > 0):
                daily_issued = existing
                return render_template('furnace/tin_copper_form.html', form=form, grades=grades, record=None, daily_issued=daily_issued)

        grade = MetalGrade.query.get(int(form.metal_grade_id.data))
        weight = int(form.weight.data)
        grade_name = grade.name if grade else ''

        tin_calc = _calc_tin_to_be_added(grade_name, form.base_tin.data, weight)
        copper_calc = _calc_copper_to_be_added(grade_name, form.base_copper.data, weight)

        record = TinCopperCalculation(
            date=form.date.data,
            heat_number=form.heat_number.data or None,
            operator_id=int(form.operator_id.data) if form.operator_id.data else None,
            furnace_id=int(form.furnace_id.data) if form.furnace_id.data else None,
            metal_grade_id=int(form.metal_grade_id.data) if form.metal_grade_id.data else None,
            weight=weight,
            base_tin=form.base_tin.data,
            tin_to_be_added=tin_calc,
            tin_added=form.tin_added.data,
            base_copper=form.base_copper.data,
            copper_to_be_added=copper_calc,
            copper_added=form.copper_added.data,
            starting_tin=form.starting_tin.data,
            starting_copper=form.starting_copper.data,
            tin_issued=form.tin_issued.data,
            copper_issued=form.copper_issued.data,
        )
        db.session.add(record)
        db.session.commit()
        flash('Record saved successfully.', 'success')
        return redirect(url_for('furnace.tin_copper'))

    daily_issued = _get_daily_issued(form.date.data or date.today())
    return render_template('furnace/tin_copper_form.html', form=form, grades=grades, record=None, daily_issued=daily_issued)


@furnace_bp.route('/tin-copper/<int:record_id>/edit', methods=['GET', 'POST'])
@require_perm('furnace', 'capture')
def tin_copper_edit(record_id):
    record = TinCopperCalculation.query.get_or_404(record_id)
    form = TinCopperForm(obj=record)
    grades = MetalGrade.query.all()

    if request.method == 'GET':
        form.weight.data = str(record.weight)
        if record.operator_id:
            form.operator_id.data = str(record.operator_id)
        if record.furnace_id:
            form.furnace_id.data = str(record.furnace_id)
        if record.metal_grade_id:
            form.metal_grade_id.data = str(record.metal_grade_id)

    if form.validate_on_submit():
        existing = _get_daily_issued(form.date.data, exclude_id=record.id)
        if existing:
            if form.tin_issued.data and form.tin_issued.data > 0:
                flash(f'Tin Issued already recorded for {form.date.data.strftime("%d/%m/%Y")} on record #{existing.id}. Only one entry per day is allowed.', 'danger')
            if form.copper_issued.data and form.copper_issued.data > 0:
                flash(f'Copper Issued already recorded for {form.date.data.strftime("%d/%m/%Y")} on record #{existing.id}. Only one entry per day is allowed.', 'danger')
            if (form.tin_issued.data and form.tin_issued.data > 0) or (form.copper_issued.data and form.copper_issued.data > 0):
                return render_template('furnace/tin_copper_form.html', form=form, grades=grades, record=record, daily_issued=existing)

        grade = MetalGrade.query.get(int(form.metal_grade_id.data))
        weight = int(form.weight.data)
        grade_name = grade.name if grade else ''

        tin_calc = _calc_tin_to_be_added(grade_name, form.base_tin.data, weight)
        copper_calc = _calc_copper_to_be_added(grade_name, form.base_copper.data, weight)

        record.date = form.date.data
        record.heat_number = form.heat_number.data or None
        record.operator_id = int(form.operator_id.data) if form.operator_id.data else None
        record.furnace_id = int(form.furnace_id.data) if form.furnace_id.data else None
        record.metal_grade_id = int(form.metal_grade_id.data) if form.metal_grade_id.data else None
        record.weight = weight
        record.base_tin = form.base_tin.data
        record.tin_to_be_added = tin_calc
        record.tin_added = form.tin_added.data
        record.base_copper = form.base_copper.data
        record.copper_to_be_added = copper_calc
        record.copper_added = form.copper_added.data
        record.starting_tin = form.starting_tin.data
        record.starting_copper = form.starting_copper.data
        record.tin_issued = form.tin_issued.data
        record.copper_issued = form.copper_issued.data
        record.updated_at = datetime.now()

        db.session.commit()
        flash('Record updated successfully.', 'success')
        return redirect(url_for('furnace.tin_copper'))

    daily_issued = _get_daily_issued(record.date, exclude_id=record.id)
    return render_template('furnace/tin_copper_form.html', form=form, grades=grades, record=record, daily_issued=daily_issued)


@furnace_bp.route('/tin-copper/<int:record_id>/delete', methods=['POST'])
@require_perm('furnace', 'admin')
def tin_copper_delete(record_id):
    record = TinCopperCalculation.query.get_or_404(record_id)
    db.session.delete(record)
    db.session.commit()
    flash('Record deleted.', 'success')
    return redirect(url_for('furnace.tin_copper'))


@furnace_bp.route('/manual')
@require_perm('furnace', 'view')
def manual():
    """User manual covering every feature of the system"""
    return render_template('furnace/manual.html')


@furnace_bp.route("/logs")
@require_perm('furnace', 'admin')
def logs_page():
    return render_template("furnace/status.html")


@furnace_bp.route("/live-logs")
@require_perm('furnace', 'admin')
def live_logs():
    return jsonify(list(log_buffer))
