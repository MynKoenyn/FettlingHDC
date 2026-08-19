from datetime import date, datetime, timedelta
from itertools import groupby

from flask import Blueprint, flash, jsonify, make_response, redirect, render_template, request, url_for
from sqlalchemy import func
from weasyprint import HTML

from access.guards import require_perm
from app import db
from dailyproduction.forms import ProductionEntryForm
from dailyproduction.models import (
    Machine,
    ProductionEntry,
    ProductionTarget,
    RemarkCategory,
    Shift,
)
from dailyproduction.services import get_current_shift, get_shift_hours
from models import Personnel, User

daily_production_bp = Blueprint("daily_production", __name__, url_prefix="/dailyproduction")

# Production "types" shown as tiles on the Daily Production hub page. Each
# entry here is one production area with its own dashboard/entry/history —
# HDA Core Production is the first; add more as they come online.
PRODUCTION_TYPES = [
    {
        "key": "hda-core-production",
        "name": "HDA Core Production",
        "description": "Hourly core production by machine and shift for the HDA / Core Blower division.",
        "icon": "bi-clipboard-data",
        "color": "#2C5282",
        "endpoint": "daily_production.hda_dashboard",
    },
]


# ══════════════════════════════════════════════════════════════
# Helpers
# ══════════════════════════════════════════════════════════════
def get_missing_hours_for_machine(entry_date, shift, machine):
    """Hours (0-23) still missing for a given date/shift/machine."""
    all_hours = get_shift_hours(shift)
    missing_hours = []
    for hour in all_hours:
        check_date = entry_date
        if shift == Shift.SHIFT_3 and hour < 6:
            check_date += timedelta(days=1)
        exists = ProductionEntry.query.filter_by(
            date=check_date, shift=shift, hour=hour, machine=machine
        ).first()
        if not exists:
            missing_hours.append(hour)
    return missing_hours


def get_missing_production_entries():
    """Missing hours for today across the Lauds machines, collapsed into ranges."""
    now = datetime.now()
    lauds_machines = [Machine.LAUDS_1, Machine.LAUDS_2, Machine.LAUDS_3, Machine.LAUDS_4, Machine.LAUDS_5]

    shift_hours = {
        Shift.SHIFT_1: range(6, 14),
        Shift.SHIFT_2: range(14, 22),
        Shift.SHIFT_3: list(range(22, 24)) + list(range(0, 6)),
    }

    missing_dict = {}
    for shift, hours in shift_hours.items():
        for hour in hours:
            entry_date = now.date()
            if hour < 6:
                entry_date += timedelta(days=1)
            for machine in lauds_machines:
                exists = ProductionEntry.query.filter_by(
                    date=entry_date, shift=shift, hour=hour, machine=machine
                ).first()
                if not exists:
                    key = (machine, shift, entry_date)
                    missing_dict.setdefault(key, []).append(hour)

    collapsed_entries = []
    for (machine, shift, entry_date), hours in missing_dict.items():
        hours.sort()
        ranges = []
        for _, g in groupby(enumerate(hours), lambda ix: ix[0] - ix[1]):
            group = list(g)
            ranges.append((group[0][1], group[-1][1]))
        range_str = ", ".join(f"{start:02d}:00 - {(end + 1) % 24:02d}:00" for start, end in ranges)
        collapsed_entries.append(
            {
                "machine": machine.value,
                "shift": shift.value.replace("_", " ").title(),
                "date": entry_date,
                "hours": range_str,
            }
        )
    return collapsed_entries


# ══════════════════════════════════════════════════════════════
# Daily Production hub — picks a production type
# ══════════════════════════════════════════════════════════════
@daily_production_bp.route("/dashboard")
@require_perm("dailyproduction", "view")
def dashboard():
    return render_template("dailyproduction/hub.html", production_types=PRODUCTION_TYPES)


# ══════════════════════════════════════════════════════════════
# HDA Core Production — dashboard
# ══════════════════════════════════════════════════════════════
@daily_production_bp.route("/hda-core-production/dashboard")
@require_perm("dailyproduction", "view")
def hda_dashboard():
    selected_date_str = request.args.get("date")
    selected_date = (
        datetime.strptime(selected_date_str, "%Y-%m-%d").date() if selected_date_str else date.today()
    )
    current_date = date.today()

    entries_today = ProductionEntry.query.filter(ProductionEntry.production_date == selected_date).all()

    machine_totals = {}
    hourly_data_query = {}
    shift_totals = {}
    for entry in entries_today:
        machine_totals[entry.machine] = machine_totals.get(entry.machine, 0) + entry.cores_produced
        hourly_data_query.setdefault(entry.hour, []).append(entry.cores_produced)
        shift_totals[entry.shift] = shift_totals.get(entry.shift, 0) + entry.cores_produced

    hourly_data = [
        {"hour": f"{hour:02d}:00-{(hour + 1) % 24:02d}:00", "avg_cores": sum(values) / len(values)}
        for hour, values in sorted(hourly_data_query.items())
    ]

    recent_entries = (
        ProductionEntry.query.filter(ProductionEntry.production_date >= selected_date - timedelta(days=7))
        .order_by(ProductionEntry.production_date.desc(), ProductionEntry.hour.desc())
        .limit(10)
        .all()
    )

    today_total = sum(entry.cores_produced for entry in entries_today)
    current_shift = get_current_shift()

    machine_targets_query = (
        db.session.query(ProductionTarget.machine, func.sum(ProductionTarget.hourly_target).label("daily_target"))
        .group_by(ProductionTarget.machine)
        .all()
    )
    machine_targets = {mt.machine: mt.daily_target for mt in machine_targets_query}

    machine_performance = []
    for machine in Machine:
        actual = machine_totals.get(machine, 0)
        target = machine_targets.get(machine, 0)
        machine_performance.append(
            {
                "machine": machine,
                "actual": actual,
                "target": target,
                "deficit_surplus": actual - target,
                "performance_pct": (actual / target * 100) if target > 0 else 0,
            }
        )

    shift_targets = []
    for shift in Shift:
        shift_hours = get_shift_hours(shift)
        target_total = 0
        for machine in Machine:
            for hour in shift_hours:
                target_row = ProductionTarget.query.filter_by(machine=machine, hour=hour).first()
                if target_row:
                    target_total += target_row.hourly_target
        actual_total = shift_totals.get(shift, 0)
        shift_targets.append(
            {
                "shift": shift,
                "actual": actual_total,
                "target": target_total,
                "deficit_surplus": actual_total - target_total,
                "performance_pct": (actual_total / target_total * 100) if target_total > 0 else 0,
            }
        )

    hourly_performance = []
    for entry in entries_today:
        target_row = ProductionTarget.query.filter_by(machine=entry.machine, hour=entry.hour).first()
        target_cores = target_row.hourly_target if target_row else 0
        hourly_performance.append(
            {
                "hour": f"{entry.hour:02d}:00-{(entry.hour + 1) % 24:02d}:00",
                "date": entry.production_date,
                "machine": entry.machine,
                "actual": entry.cores_produced,
                "target": target_cores,
                "defects": entry.defects,
                "deficit_surplus": entry.cores_produced - target_cores,
                "performance_pct": (entry.cores_produced / target_cores * 100) if target_cores > 0 else 0,
                "operator": entry.operator,
            }
        )
    hourly_performance.sort(key=lambda x: (x["hour"], x["machine"].value))

    total_target_today = sum(mp["target"] for mp in machine_performance)
    overall_deficit_surplus = today_total - total_target_today
    overall_performance_pct = (today_total / total_target_today * 100) if total_target_today > 0 else 0

    return render_template(
        "dailyproduction/hda/dashboard.html",
        machine_performance=machine_performance,
        hourly_data=hourly_data,
        shift_targets=shift_targets,
        hourly_performance=hourly_performance,
        recent_entries=recent_entries,
        today_total=today_total,
        total_target_today=total_target_today,
        overall_deficit_surplus=overall_deficit_surplus,
        overall_performance_pct=overall_performance_pct,
        current_shift=current_shift,
        today=selected_date,
        current_date=current_date,
    )


# ══════════════════════════════════════════════════════════════
# HDA Core Production — entry capture
# ══════════════════════════════════════════════════════════════
@daily_production_bp.route("/hda-core-production/entry", methods=["GET", "POST"])
@require_perm("dailyproduction", "capture")
def hda_entry():
    form = ProductionEntryForm()

    if form.validate_on_submit():
        selected_datetime = datetime.combine(form.date.data, datetime.min.time()).replace(hour=form.hour.data)
        if selected_datetime > datetime.now() + timedelta(hours=0.5):
            flash("Selected date/hour cannot be ahead of time. Please make sure you have the correct date selected.", "warning")
            return redirect(url_for("daily_production.hda_entry"))

        existing_entry = ProductionEntry.query.filter_by(
            date=form.date.data,
            shift=Shift(form.shift.data),
            hour=form.hour.data,
            machine=Machine(form.machine.data),
        ).first()
        if existing_entry:
            flash("Entry already exists for this date, shift, hour, and machine.", "warning")
            return redirect(url_for("daily_production.hda_entry"))

        new_entry = ProductionEntry(
            date=form.date.data,
            shift=Shift(form.shift.data),
            hour=form.hour.data,
            machine=Machine(form.machine.data),
            cores_produced=form.cores_produced.data,
            downtime_minutes=form.downtime_minutes.data,
            defects=form.defects.data,
            remark_category=RemarkCategory(form.remark_category.data) if form.remark_category.data else None,
            remark_text=form.remark_text.data or None,
            operator_id=form.operator_id.data,
            supervisor_id=form.supervisor_id.data or None,
        )
        new_entry.production_date = new_entry.calculate_production_date()
        db.session.add(new_entry)
        db.session.commit()

        flash("Production entry saved successfully!", "success")
        return redirect(url_for("daily_production.hda_entry"))

    missing_entries = get_missing_production_entries()
    return render_template("dailyproduction/hda/entry.html", form=form, missing_entries=missing_entries)


@daily_production_bp.route("/hda-core-production/api/missing_hours")
@require_perm("dailyproduction", "capture")
def hda_api_missing_hours():
    date_str = request.args.get("date")
    shift_str = request.args.get("shift")
    machine_str = request.args.get("machine")

    if not (date_str and shift_str and machine_str):
        return jsonify({"missing_hours": []})

    date_obj = datetime.strptime(date_str, "%Y-%m-%d").date()
    shift = Shift(shift_str)
    machine = Machine(machine_str)

    return jsonify({"missing_hours": get_missing_hours_for_machine(date_obj, shift, machine)})


# ══════════════════════════════════════════════════════════════
# HDA Core Production — history
# ══════════════════════════════════════════════════════════════
@daily_production_bp.route("/hda-core-production/history")
@require_perm("dailyproduction", "view")
def hda_history():
    page = request.args.get("page", 1, type=int)
    date_filter = request.args.get("date", "")
    shift_filter = request.args.get("shift", "")
    machine_filter = request.args.get("machine", "")

    query = ProductionEntry.query

    if date_filter:
        try:
            query = query.filter(ProductionEntry.date == datetime.strptime(date_filter, "%Y-%m-%d").date())
        except ValueError:
            pass
    if shift_filter:
        try:
            query = query.filter(ProductionEntry.shift == Shift(shift_filter))
        except ValueError:
            pass
    if machine_filter:
        try:
            query = query.filter(ProductionEntry.machine == Machine(machine_filter))
        except ValueError:
            pass

    entries = query.order_by(ProductionEntry.date.desc(), ProductionEntry.hour.desc()).paginate(
        page=page, per_page=20, error_out=False
    )

    return render_template(
        "dailyproduction/hda/history.html",
        entries=entries,
        date_filter=date_filter,
        shift_filter=shift_filter,
        machine_filter=machine_filter,
        shifts=Shift,
        machines=Machine,
    )


@daily_production_bp.route("/hda-core-production/api/production/charts")
@require_perm("dailyproduction", "view")
def hda_production_charts():
    chart_type = request.args.get("type", "hourly")
    days = request.args.get("days", 7, type=int)

    end_date = date.today()
    start_date = end_date - timedelta(days=days - 1)

    if chart_type == "hourly":
        data = (
            db.session.query(ProductionEntry.hour, func.avg(ProductionEntry.cores_produced).label("avg_cores"))
            .filter(ProductionEntry.date >= start_date, ProductionEntry.date <= end_date)
            .group_by(ProductionEntry.hour)
            .all()
        )
        return jsonify({"labels": [f"{hour:02d}:00" for hour, _ in data], "data": [float(v) for _, v in data]})

    if chart_type == "machine":
        data = (
            db.session.query(ProductionEntry.machine, func.sum(ProductionEntry.cores_produced).label("total_cores"))
            .filter(ProductionEntry.date >= start_date, ProductionEntry.date <= end_date)
            .group_by(ProductionEntry.machine)
            .all()
        )
        return jsonify(
            {
                "labels": [machine.value.replace("_", " ").title() for machine, _ in data],
                "data": [int(v) for _, v in data],
            }
        )

    if chart_type == "daily":
        data = (
            db.session.query(ProductionEntry.date, func.sum(ProductionEntry.cores_produced).label("total_cores"))
            .filter(ProductionEntry.date >= start_date, ProductionEntry.date <= end_date)
            .group_by(ProductionEntry.date)
            .all()
        )
        return jsonify({"labels": [str(d) for d, _ in data], "data": [int(v) for _, v in data]})

    return jsonify({"error": "Invalid chart type"}), 400


# ══════════════════════════════════════════════════════════════
# HDA Core Production — reports
# ══════════════════════════════════════════════════════════════
@daily_production_bp.route("/hda-core-production/reports")
@require_perm("dailyproduction", "view")
def hda_reports_page():
    return render_template(
        "dailyproduction/hda/reports.html",
        machines=[m.value for m in Machine],
        shifts=[s.value for s in Shift],
        remark_categories=[r.value for r in RemarkCategory],
        today=date.today(),
    )


def reports_data_internal(report_type, start_date, end_date, shift=None, hour_from=None, hour_to=None,
                           machine=None, operator_id=None, remark_category=None):
    def apply_filters(query):
        query = query.filter(ProductionEntry.production_date.between(start_date, end_date))
        if shift:
            query = query.filter(ProductionEntry.shift == shift)
        if hour_from is not None and hour_to is not None:
            query = query.filter(ProductionEntry.hour.between(hour_from, hour_to))
        if machine:
            query = query.filter(ProductionEntry.machine == machine)
        if operator_id:
            query = query.filter(ProductionEntry.operator_id == operator_id)
        if remark_category:
            query = query.filter(ProductionEntry.remark_category == remark_category)
        return query

    if report_type == "machine_perf":
        query = apply_filters(
            db.session.query(
                ProductionEntry.machine,
                func.sum(ProductionEntry.cores_produced).label("total_cores"),
                func.sum(ProductionEntry.defects).label("total_defects"),
                func.sum(ProductionEntry.downtime_minutes).label("total_downtime"),
            )
        )
        data = query.group_by(ProductionEntry.machine).all()
        return [
            {"machine": row.machine.value, "cores_produced": row.total_cores, "defects": row.total_defects, "downtime": row.total_downtime}
            for row in data
        ]

    if report_type == "operator_perf":
        query = apply_filters(
            db.session.query(
                ProductionEntry.operator_id,
                Personnel.name,
                Personnel.surname,
                func.sum(ProductionEntry.cores_produced).label("total_cores"),
                func.sum(ProductionEntry.defects).label("total_defects"),
                func.sum(ProductionEntry.downtime_minutes).label("total_downtime"),
            ).join(Personnel, ProductionEntry.operator_id == Personnel.id)
        )
        data = query.group_by(ProductionEntry.operator_id, Personnel.name, Personnel.surname).all()
        return [
            {
                "operator_name": f"{row.name} {row.surname or ''}".strip(),
                "cores_produced": row.total_cores,
                "defects": row.total_defects,
                "downtime": row.total_downtime,
            }
            for row in data
        ]

    if report_type == "operator_machine":
        query = apply_filters(
            db.session.query(
                ProductionEntry.operator_id,
                Personnel.name,
                Personnel.surname,
                ProductionEntry.machine,
                func.sum(ProductionEntry.cores_produced).label("total_cores"),
                func.sum(ProductionEntry.defects).label("total_defects"),
                func.sum(ProductionEntry.downtime_minutes).label("total_downtime"),
                func.count(ProductionEntry.id).label("hours_worked"),
            ).join(Personnel, ProductionEntry.operator_id == Personnel.id)
        )
        data = query.group_by(ProductionEntry.operator_id, Personnel.name, Personnel.surname, ProductionEntry.machine).all()
        return [
            {
                "operator_name": f"{row.name} {row.surname or ''}".strip(),
                "machine": row.machine.value,
                "cores_produced": row.total_cores,
                "defects": row.total_defects,
                "downtime": row.total_downtime,
                "hours_worked": row.hours_worked,
            }
            for row in data
        ]

    if report_type == "downtime":
        query = apply_filters(
            db.session.query(ProductionEntry.remark_category, func.sum(ProductionEntry.downtime_minutes).label("total_downtime"))
        )
        data = query.group_by(ProductionEntry.remark_category).all()
        return [
            {"remark_category": row.remark_category.value if row.remark_category else "Uncategorized", "downtime_minutes": row.total_downtime}
            for row in data
        ]

    if report_type == "defects":
        query = apply_filters(
            db.session.query(ProductionEntry.remark_category, func.sum(ProductionEntry.defects).label("total_defects"))
        )
        data = query.group_by(ProductionEntry.remark_category).all()
        return [
            {"remark_category": row.remark_category.value if row.remark_category else "Uncategorized", "defects": row.total_defects}
            for row in data
        ]

    if report_type == "shift_agg":
        query = apply_filters(
            db.session.query(
                ProductionEntry.shift,
                func.sum(ProductionEntry.cores_produced).label("total_cores"),
                func.sum(ProductionEntry.defects).label("total_defects"),
                func.sum(ProductionEntry.downtime_minutes).label("total_downtime"),
            )
        )
        data = query.group_by(ProductionEntry.shift).all()
        return [
            {"shift": row.shift.value, "cores_produced": row.total_cores, "defects": row.total_defects, "downtime": row.total_downtime}
            for row in data
        ]

    if report_type == "normalized":
        query = apply_filters(
            db.session.query(
                ProductionEntry.shift,
                ProductionEntry.hour,
                ProductionEntry.machine,
                func.sum(ProductionEntry.cores_produced).label("total_cores"),
            )
        )
        data = query.group_by(ProductionEntry.shift, ProductionEntry.hour, ProductionEntry.machine).all()
        return [
            {"shift": row.shift.value, "hour": row.hour, "machine": row.machine.value, "cores_produced": row.total_cores}
            for row in data
        ]

    if report_type == "daily_prod":
        query = apply_filters(
            db.session.query(
                ProductionEntry.production_date,
                ProductionEntry.shift,
                func.sum(ProductionEntry.cores_produced).label("shift_cores"),
            )
        )
        data = query.group_by(ProductionEntry.production_date, ProductionEntry.shift).order_by(
            ProductionEntry.production_date, ProductionEntry.shift
        ).all()

        daily_totals = {}
        for row in data:
            date_str = row.production_date.strftime("%d-%m-%Y")
            bucket = daily_totals.setdefault(date_str, {"total_cores": 0, "shifts": []})
            bucket["shifts"].append({"shift": row.shift.value, "cores_produced": row.shift_cores})
            bucket["total_cores"] += row.shift_cores

        return [{"date": d, "total_cores": v["total_cores"], "shifts": v["shifts"]} for d, v in daily_totals.items()]

    return []


@daily_production_bp.route("/hda-core-production/reports/data")
@require_perm("dailyproduction", "view")
def hda_reports_data():
    def parse_date(name, default=None):
        val = request.args.get(name)
        return datetime.strptime(val, "%Y-%m-%d").date() if val else default

    data = reports_data_internal(
        report_type=request.args.get("report_type", "machine_perf"),
        start_date=parse_date("start_date", date.today()),
        end_date=parse_date("end_date", date.today()),
        shift=request.args.get("shift"),
        hour_from=request.args.get("hour_from", type=int),
        hour_to=request.args.get("hour_to", type=int),
        machine=request.args.get("machine"),
        operator_id=request.args.get("operator_id", type=int),
        remark_category=request.args.get("remark_category"),
    )
    return jsonify(data)


@daily_production_bp.route("/hda-core-production/reports/export/pdf", methods=["POST"])
@require_perm("dailyproduction", "view")
def hda_export_report_pdf():
    report_type = request.form.get("report_type")
    start_date = datetime.strptime(request.form.get("start_date"), "%Y-%m-%d").date()
    end_date = datetime.strptime(request.form.get("end_date"), "%Y-%m-%d").date()

    data = reports_data_internal(
        report_type=report_type,
        start_date=start_date,
        end_date=end_date,
        shift=request.form.get("shift"),
        machine=request.form.get("machine"),
    )

    html_out = render_template(
        "dailyproduction/hda/reports_pdf.html",
        report_type=report_type,
        start_date=start_date,
        end_date=end_date,
        data=data,
        chart_image=request.form.get("chart_image"),
    )

    pdf = HTML(string=html_out).write_pdf()
    response = make_response(pdf)
    response.headers["Content-Type"] = "application/pdf"
    response.headers["Content-Disposition"] = f"inline; filename={report_type}_report.pdf"
    return response


# ══════════════════════════════════════════════════════════════
# HDA Core Production — targets admin
# ══════════════════════════════════════════════════════════════
@daily_production_bp.route("/hda-core-production/targets", methods=["GET", "POST"])
@require_perm("dailyproduction", "admin")
def hda_targets():
    machine_value = request.args.get("machine", Machine.LAUDS_1.value)
    try:
        selected_machine = Machine(machine_value)
    except ValueError:
        selected_machine = Machine.LAUDS_1

    if request.method == "POST":
        posted_machine = Machine(request.form.get("machine", selected_machine.value))
        total = 0
        for hour in range(24):
            raw = request.form.get(f"hourly_target_{hour}", "0").strip()
            hourly_target = int(raw) if raw else 0
            total += hourly_target
            row = ProductionTarget.query.filter_by(machine=posted_machine, hour=hour).first()
            if row is None:
                row = ProductionTarget(machine=posted_machine, hour=hour, hourly_target=0, shift_target=0)
                db.session.add(row)
            row.hourly_target = hourly_target
        # shift_target is stored per row — keep every row for this machine in sync
        for row in ProductionTarget.query.filter_by(machine=posted_machine).all():
            row.shift_target = total
        db.session.commit()
        flash(f"Targets updated for {posted_machine.value.replace('_', ' ').title()}.", "success")
        return redirect(url_for("daily_production.hda_targets", machine=posted_machine.value))

    existing = {row.hour: row for row in ProductionTarget.query.filter_by(machine=selected_machine).all()}
    hourly_rows = [
        {"hour": hour, "hourly_target": existing[hour].hourly_target if hour in existing else 0}
        for hour in range(24)
    ]

    return render_template(
        "dailyproduction/hda/targets.html",
        machines=Machine,
        selected_machine=selected_machine,
        hourly_rows=hourly_rows,
    )
