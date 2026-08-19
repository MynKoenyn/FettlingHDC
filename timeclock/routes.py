"""
Blueprint: timeclock
====================

Loading the clock system's report, and everything that has to be possible
afterwards: look at what came in, correct a row, decide who an unrecognised
employee number belongs to, and undo the whole import if the wrong file went
up.

Money — the cost summary the report prints at its foot — sits behind
`timeclock/rates`, the way overtime keeps its rand amounts behind
`overtime/rates`. Everything else about a person's hours is visible to anyone
who may view the module.
"""

import csv
import io
from datetime import date, datetime, timedelta

from flask import (
    Blueprint, Response, current_app, flash, redirect, render_template, request, url_for
)
from flask_login import current_user, login_required
from sqlalchemy import func, or_
from sqlalchemy.orm import joinedload

from app import db
from access.guards import require_perm, user_can
from models import Personnel
from timeclock.forms import ClockDayForm, ClockImportForm, ConfirmForm, MatchForm
from timeclock.importer import import_clock_report, overlapping_batches
from timeclock.matching import forget, rematch_batch, refresh_counts, set_manual_match
from timeclock.models import (
    ClockDay,
    ClockEmployee,
    ClockEmployeeLink,
    ClockImportBatch,
    MATCH_IGNORED,
)

timeclock_bp = Blueprint("timeclock", __name__, url_prefix="/timeclock")


def can_see_money():
    """Whether the current user may see the report's cost summary."""
    return user_can(current_user, "timeclock", "rates")


def _personnel_choices(include_blank=True):
    """The personnel picker used on the manual-match form."""
    people = (
        Personnel.query
        .order_by(Personnel.status.desc(), Personnel.surname, Personnel.name)
        .all()
    )
    choices = [(0, "— not one of ours —")] if include_blank else []
    for person in people:
        full = f"{person.surname or ''}, {person.name}".strip(", ")
        label = f"{full} ({person.clockno})"
        if person.status is False:
            label += " · inactive"
        choices.append((person.id, label))
    return choices


# ══════════════════════════════════════════════════════════════════════
# IMPORT HISTORY
# ══════════════════════════════════════════════════════════════════════

@timeclock_bp.route("/")
@login_required
@require_perm("timeclock", "view")
def index():
    batches = (
        ClockImportBatch.query
        .order_by(ClockImportBatch.imported_at.desc())
        .all()
    )

    unmatched = sum(b.employees_unmatched or 0 for b in batches)
    return render_template(
        "timeclock/batches.html",
        batches=batches,
        unmatched=unmatched,
        confirm_form=ConfirmForm(),
        show_money=can_see_money(),
        can_import=user_can(current_user, "timeclock", "import"),
        can_edit=user_can(current_user, "timeclock", "edit"),
    )


@timeclock_bp.route("/import", methods=["GET", "POST"])
@login_required
@require_perm("timeclock", "import")
def import_report():
    form = ClockImportForm()
    result = None

    if form.validate_on_submit():
        try:
            result = import_clock_report(form.file.data, user_id=current_user.id)
        except ValueError as exc:
            # Raised deliberately for a file we cannot use — the message is
            # written to be read by whoever uploaded it.
            db.session.rollback()
            flash(f"Import failed: {exc}", "danger")
            return redirect(url_for("timeclock.import_report"))
        except Exception:                             # noqa: BLE001
            # Anything else is a bug rather than a bad file. Log it in full and
            # tell the user something useful without putting a stack trace's
            # worth of internals on the screen.
            db.session.rollback()
            current_app.logger.exception("Clock report import failed")
            flash(
                "Import failed — that file could not be read. It has been logged; "
                "check it is the .TXT export straight out of Turbo Time.",
                "danger",
            )
            return redirect(url_for("timeclock.import_report"))

        batch = result.batch
        flash(
            f"Imported {batch.report_kind or 'report'} — {result.days} day row(s) for "
            f"{result.employees} employee(s) covering {batch.period_label}.",
            "success",
        )
        if not result.is_full_clocking:
            flash(
                "That was the Overtime Report, which prints only the days that "
                "carried overtime. For every day worked — and the days not "
                "worked — import the Full Clocking Report instead.",
                "info",
            )
        if result.punches:
            flash(
                f"{batch.odd_clockings} day(s) were clocked more than twice; the full "
                f"punch list ({result.punches} pairs) came in with them and is shown "
                "under each of those days.",
                "info",
            )
        if result.unmatched:
            flash(
                f"{result.unmatched} employee(s) could not be matched to a personnel "
                "record — match them below so their hours are attributed.",
                "warning",
            )
        for note in batch.note_lines:
            flash(note, "info")

        return redirect(url_for("timeclock.batch_detail", batch_id=batch.id))

    for field, errors in form.errors.items():
        for error in errors:
            flash(error, "danger")

    recent = (
        ClockImportBatch.query
        .order_by(ClockImportBatch.imported_at.desc())
        .limit(5)
        .all()
    )
    return render_template("timeclock/import.html", form=form, recent=recent)


# ══════════════════════════════════════════════════════════════════════
# ONE BATCH
# ══════════════════════════════════════════════════════════════════════

# A full week's clocking report is 175 people × 7 days. Rendering every
# employee's day table at once is a multi-megabyte page the browser struggles
# with, so the list is paged.
EMPLOYEES_PER_PAGE = 25


@timeclock_bp.route("/batch/<int:batch_id>")
@login_required
@require_perm("timeclock", "view")
def batch_detail(batch_id):
    batch = ClockImportBatch.query.get_or_404(batch_id)

    employees = (
        ClockEmployee.query
        .options(joinedload(ClockEmployee.days).joinedload(ClockDay.punches),
                 joinedload(ClockEmployee.personnel))
        .filter(ClockEmployee.batch_id == batch.id)
        .all()
    )
    # Unmatched first — they are the ones needing a decision — then by name.
    employees.sort(key=lambda e: (not e.needs_match, (e.emp_name or e.emp_no).lower()))

    unmatched_only = request.args.get("unmatched") == "1"
    if unmatched_only:
        employees = [e for e in employees if e.needs_match]

    total = len(employees)
    pages = max(1, -(-total // EMPLOYEES_PER_PAGE))
    page = min(max(request.args.get("page", type=int) or 1, 1), pages)
    start = (page - 1) * EMPLOYEES_PER_PAGE
    shown = employees[start:start + EMPLOYEES_PER_PAGE]

    match_form = MatchForm()
    match_form.personnel_id.choices = _personnel_choices()

    return render_template(
        "timeclock/batch_detail.html",
        batch=batch,
        employees=shown,
        employees_total=total,
        page=page,
        pages=pages,
        first_shown=start + 1 if total else 0,
        last_shown=min(start + EMPLOYEES_PER_PAGE, total),
        unmatched_only=unmatched_only,
        confirm_form=ConfirmForm(),
        match_form=match_form,
        show_money=can_see_money(),
        can_edit=user_can(current_user, "timeclock", "edit"),
        can_admin=user_can(current_user, "timeclock", "admin"),
        overlaps=overlapping_batches(batch.period_start, batch.period_end, exclude_id=batch.id),
    )


@timeclock_bp.route("/batch/<int:batch_id>/delete", methods=["POST"])
@login_required
@require_perm("timeclock", "admin")
def delete_batch(batch_id):
    """
    Reverse an import.

    The batch owns its employees and their days by cascade, and nothing outside
    the module's own tables was ever written, so this removes the import
    completely and cannot reach anything loaded before it. The remembered
    matching links are deliberately left behind — they are decisions about who
    a number belongs to, not data from this file.
    """
    form = ConfirmForm()
    if not form.validate_on_submit():
        flash("Could not reverse that import — please try again.", "danger")
        return redirect(url_for("timeclock.batch_detail", batch_id=batch_id))

    batch = ClockImportBatch.query.get_or_404(batch_id)
    days = batch.rows_imported or 0
    people = batch.employees_total or 0
    label = batch.period_label

    db.session.delete(batch)
    db.session.commit()

    flash(
        f"Import #{batch_id} reversed — {days} day row(s) for {people} employee(s) "
        f"covering {label} removed.",
        "warning",
    )
    return redirect(url_for("timeclock.index"))


@timeclock_bp.route("/batch/<int:batch_id>/rematch", methods=["POST"])
@login_required
@require_perm("timeclock", "edit")
def rematch(batch_id):
    """
    Run the matching passes again — after personnel were added, or a link made.

    By default only the undecided employees are touched, so a match made by
    hand survives. Ticking "redo every employee" re-runs the lot, for when the
    personnel master itself was wrong.
    """
    form = ConfirmForm()
    if not form.validate_on_submit():
        flash("Could not re-match — please try again.", "danger")
        return redirect(url_for("timeclock.batch_detail", batch_id=batch_id))

    batch = ClockImportBatch.query.get_or_404(batch_id)
    include_decided = request.form.get("include_decided") == "1"

    looked_at, newly = rematch_batch(batch, include_decided=include_decided)
    db.session.commit()

    if not looked_at:
        flash("Every employee on this import is already matched.", "info")
    elif newly:
        flash(f"Re-matched {looked_at} employee(s) — {newly} newly matched.", "success")
    else:
        flash(
            f"Looked at {looked_at} employee(s); none of them could be matched "
            "automatically. Match them by hand, or add the missing personnel first.",
            "warning",
        )

    return redirect(url_for("timeclock.batch_detail", batch_id=batch.id))


# ══════════════════════════════════════════════════════════════════════
# MATCHING ONE EMPLOYEE
# ══════════════════════════════════════════════════════════════════════

@timeclock_bp.route("/employee/<int:employee_id>/match", methods=["POST"])
@login_required
@require_perm("timeclock", "edit")
def match_employee(employee_id):
    employee = ClockEmployee.query.get_or_404(employee_id)

    form = MatchForm()
    form.personnel_id.choices = _personnel_choices()
    if not form.validate_on_submit():
        flash("Could not save that match — please try again.", "danger")
        return redirect(url_for("timeclock.batch_detail", batch_id=employee.batch_id))

    personnel_id = form.personnel_id.data or 0
    person = Personnel.query.get(personnel_id) if personnel_id else None
    if personnel_id and person is None:
        flash("That personnel record no longer exists.", "danger")
        return redirect(url_for("timeclock.batch_detail", batch_id=employee.batch_id))

    others = set_manual_match(
        employee, person,
        user_id=current_user.id,
        remember=bool(form.remember.data),
        apply_to_other_batches=bool(form.apply_to_other_batches.data),
    )
    refresh_counts(employee.batch)
    db.session.commit()

    who = f"{person.name} {person.surname or ''}".strip() if person else "not one of ours"
    message = f"{employee.emp_no} ({employee.emp_name or '—'}) → {who}."
    if form.remember.data:
        message += " Future imports will match this number on their own."
    if others:
        message += f" {others} row(s) on other imports updated too."
    flash(message, "success")

    return redirect(url_for("timeclock.batch_detail", batch_id=employee.batch_id,
                            unmatched=request.form.get("return_unmatched") or None))


@timeclock_bp.route("/employee/<int:employee_id>/unmatch", methods=["POST"])
@login_required
@require_perm("timeclock", "edit")
def unmatch_employee(employee_id):
    """Undo a match, putting the employee back to undecided."""
    form = ConfirmForm()
    employee = ClockEmployee.query.get_or_404(employee_id)

    if not form.validate_on_submit():
        flash("Could not clear that match — please try again.", "danger")
        return redirect(url_for("timeclock.batch_detail", batch_id=employee.batch_id))

    employee.clear_match()
    if request.form.get("forget") == "1":
        forget(employee.emp_no)
    refresh_counts(employee.batch)
    db.session.commit()

    flash(f"Match cleared for {employee.emp_no} ({employee.emp_name or '—'}).", "warning")
    return redirect(url_for("timeclock.batch_detail", batch_id=employee.batch_id))


# ══════════════════════════════════════════════════════════════════════
# EDITING A DAY ROW
# ══════════════════════════════════════════════════════════════════════

@timeclock_bp.route("/day/<int:day_id>/edit", methods=["GET", "POST"])
@login_required
@require_perm("timeclock", "edit")
def edit_day(day_id):
    """
    Correct one imported day.

    What the report printed is kept alongside on the screen and never
    overwritten, so the correction can always be read against the original and
    put back with Revert.
    """
    day = ClockDay.query.get_or_404(day_id)
    form = ClockDayForm(obj=day if request.method == "GET" else None)

    if form.validate_on_submit():
        for field in ClockDay.EDITABLE_FIELDS:
            setattr(day, field, getattr(form, field).data)
        day.description = (form.description.data or "").strip()[:60] or None

        if day.differs_from_source:
            day.edited_by = current_user.id
            day.edited_at = datetime.now()
            day.edit_note = (form.edit_note.data or "").strip()[:255] or None
        else:
            # Edited back to exactly what the file said — no longer a change.
            day.edited_by = None
            day.edited_at = None
            day.edit_note = None

        db.session.commit()
        flash(
            f"{day.work_date.strftime('%a %d %b %Y')} saved for "
            f"{day.employee.display_name}.",
            "success",
        )
        return redirect(url_for("timeclock.batch_detail",
                                batch_id=day.employee.batch_id) + f"#emp-{day.employee_id}")

    for field, errors in form.errors.items():
        for error in errors:
            flash(f"{field.replace('_', ' ').title()}: {error}", "danger")

    return render_template(
        "timeclock/day_form.html",
        day=day,
        form=form,
        original=day.source_values(),
        confirm_form=ConfirmForm(),
    )


@timeclock_bp.route("/day/<int:day_id>/revert", methods=["POST"])
@login_required
@require_perm("timeclock", "edit")
def revert_day(day_id):
    """Put an edited row back to exactly what the report printed."""
    form = ConfirmForm()
    day = ClockDay.query.get_or_404(day_id)

    if not form.validate_on_submit():
        flash("Could not revert that row — please try again.", "danger")
        return redirect(url_for("timeclock.edit_day", day_id=day_id))

    if not day.revert():
        flash(
            "That row cannot be reverted — the original line from the file is "
            "no longer on it.",
            "danger",
        )
        return redirect(url_for("timeclock.edit_day", day_id=day_id))

    db.session.commit()
    flash(
        f"{day.work_date.strftime('%a %d %b %Y')} put back to what the report said.",
        "success",
    )
    return redirect(url_for("timeclock.batch_detail",
                            batch_id=day.employee.batch_id) + f"#emp-{day.employee_id}")


# ══════════════════════════════════════════════════════════════════════
# WHAT CAME IN  (across every batch)
# ══════════════════════════════════════════════════════════════════════

def _day_filters(args):
    """The filters off the query string, defaulting to the last fortnight."""
    today = date.today()
    default_start = today - timedelta(days=today.weekday() + 7)
    default_end = default_start + timedelta(days=13)

    def parse_arg(name, fallback):
        raw = args.get(name)
        if not raw:
            return fallback
        try:
            return datetime.strptime(raw, "%Y-%m-%d").date()
        except ValueError:
            return fallback

    start_date = parse_arg("start", default_start)
    end_date = parse_arg("end", default_end)
    if start_date > end_date:
        start_date, end_date = default_start, default_end

    return {
        "start_date": start_date,
        "end_date": end_date,
        "search": (args.get("search") or "").strip(),
        "person": (args.get("person") or "").strip(),
        "unmatched_only": args.get("unmatched") == "1",
        "overtime_only": args.get("overtime") == "1",
        "worked_only": args.get("worked") == "1",
        "odd_only": args.get("odd") == "1",
    }


def _person_options(filters):
    """
    One entry per distinct person clocked in the filtered date range, keyed so
    the dropdown can select an exact person rather than a substring — typing
    "55" in the old free-text box also matched clock number "155", which is
    exactly what a select-by-identity avoids.
    """
    rows = (
        db.session.query(
            ClockEmployee.emp_no, ClockEmployee.emp_name, ClockEmployee.personnel_id,
            Personnel.name, Personnel.surname, Personnel.clockno,
        )
        .join(ClockDay, ClockDay.employee_id == ClockEmployee.id)
        .outerjoin(Personnel, ClockEmployee.personnel_id == Personnel.id)
        .filter(ClockDay.work_date >= filters["start_date"], ClockDay.work_date <= filters["end_date"])
        .distinct()
        .all()
    )

    seen = {}
    for emp_no, emp_name, personnel_id, p_name, p_surname, p_clockno in rows:
        if personnel_id:
            key = f"p:{personnel_id}"
            label = f"{p_name} {p_surname or ''}".strip()
            clock = p_clockno or emp_no
        else:
            key = f"e:{emp_no}"
            label = emp_name or emp_no
            clock = emp_no
        seen.setdefault(key, f"{label} ({clock})")

    return sorted(
        ({"value": k, "label": v} for k, v in seen.items()),
        key=lambda o: o["label"].lower(),
    )


def _filtered_days(filters):
    """The day rows those filters select, oldest first."""
    query = (
        ClockDay.query
        .join(ClockEmployee, ClockDay.employee_id == ClockEmployee.id)
        .options(joinedload(ClockDay.employee).joinedload(ClockEmployee.personnel))
        .filter(ClockDay.work_date >= filters["start_date"],
                ClockDay.work_date <= filters["end_date"])
    )

    if filters["person"]:
        # An exact identity match — the dropdown is the fix for "55" also
        # catching clock number "155" under the old substring search.
        value = filters["person"]
        if value.startswith("p:"):
            try:
                personnel_id = int(value[2:])
            except ValueError:
                personnel_id = None
            query = query.filter(ClockEmployee.personnel_id == personnel_id)
        elif value.startswith("e:"):
            query = query.filter(ClockEmployee.emp_no == value[2:],
                                 ClockEmployee.personnel_id.is_(None))
    elif filters["search"]:
        like = f"%{filters['search']}%"
        query = query.outerjoin(Personnel, ClockEmployee.personnel_id == Personnel.id)
        query = query.filter(or_(
            ClockEmployee.emp_name.ilike(like),
            ClockEmployee.emp_no.ilike(like),
            Personnel.name.ilike(like),
            Personnel.surname.ilike(like),
            Personnel.clockno.ilike(like),
        ))

    if filters["unmatched_only"]:
        query = query.filter(ClockEmployee.personnel_id.is_(None),
                             ClockEmployee.match_method != MATCH_IGNORED)

    if filters["worked_only"]:
        # A day nobody clocked prints as zeros right across on the full
        # clocking report. Kept on import because an absence is information,
        # filtered here when someone only wants the days that were worked.
        query = query.filter(or_(ClockDay.total_hours > 0, ClockDay.time_in.isnot(None)))

    rows = query.order_by(ClockDay.work_date.asc(), ClockEmployee.emp_name.asc()).all()

    # Applied in Python rather than SQL — overtime is the four bands added up
    # and an odd clocking is a child row, both properties rather than columns.
    if filters["overtime_only"]:
        rows = [r for r in rows if r.overtime_hours]
    if filters["odd_only"]:
        rows = [r for r in rows if r.punches]
    return rows


@timeclock_bp.route("/days")
@login_required
@require_perm("timeclock", "view")
def days():
    """
    Every loaded day, filterable — the view that answers "what did this person
    actually work, and how much of it was overtime".
    """
    filters = _day_filters(request.args)
    rows = _filtered_days(filters)

    totals = {
        "days": len(rows),
        "people": len({r.employee_id for r in rows}),
        "worked": sum(1 for r in rows if r.worked),
        "absent": sum(1 for r in rows if not r.worked),
        "odd": sum(1 for r in rows if r.punches),
        "normal": sum((r.normal_hours or 0) for r in rows),
        "overtime": sum(r.overtime_hours for r in rows),
        "total": sum((r.total_hours or 0) for r in rows),
    }

    return render_template(
        "timeclock/days.html",
        rows=rows,
        totals=totals,
        person_options=_person_options(filters),
        can_edit=user_can(current_user, "timeclock", "edit"),
        **filters,
    )


@timeclock_bp.route("/days.csv")
@login_required
@require_perm("timeclock", "view")
def days_csv():
    """The same list as a CSV, for payroll and for checking against the file."""
    filters = _day_filters(request.args)
    rows = _filtered_days(filters)

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow([
        "Date", "Day", "Clock No (report)", "Name (report)", "Matched Personnel",
        "Personnel Clock No", "Department", "Cost Centre", "Shift", "In", "Out",
        "Normal Hours", "OT1", "OT2", "OT3", "OT4", "Overtime Hours",
        "Total Hours", "Target Hours", "Variance", "Worked", "Description",
        "All Punches", "Edited", "Batch",
    ])
    for row in rows:
        person = row.personnel
        writer.writerow([
            row.work_date.strftime("%Y-%m-%d"),
            row.day_name or "",
            row.employee.emp_no,
            row.employee.emp_name or "",
            f"{person.name} {person.surname or ''}".strip() if person else "",
            person.clockno if person else "",
            row.employee.dept_text or "",
            row.employee.cost_centre or "",
            row.shift or "",
            row.time_in.strftime("%H:%M") if row.time_in else "",
            row.time_out.strftime("%H:%M") if row.time_out else "",
            row.normal_hours if row.normal_hours is not None else "",
            row.ot1_hours if row.ot1_hours is not None else "",
            row.ot2_hours if row.ot2_hours is not None else "",
            row.ot3_hours if row.ot3_hours is not None else "",
            row.ot4_hours if row.ot4_hours is not None else "",
            row.overtime_hours,
            row.total_hours if row.total_hours is not None else "",
            row.target_hours if row.target_hours is not None else "",
            row.variance_hours if row.variance_hours is not None else "",
            "Yes" if row.worked else "No",
            row.description or "",
            # The full punch list where the day was clocked more than twice;
            # blank on an ordinary day, whose two punches are the In/Out above.
            " | ".join(p.label for p in row.punches),
            "Yes" if row.is_edited else "",
            row.employee.batch_id,
        ])

    filename = (f"clock_days_{filters['start_date']:%Y%m%d}"
                f"_{filters['end_date']:%Y%m%d}.csv")
    return Response(
        buffer.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


# ══════════════════════════════════════════════════════════════════════
# REMEMBERED LINKS
# ══════════════════════════════════════════════════════════════════════

@timeclock_bp.route("/links")
@login_required
@require_perm("timeclock", "edit")
def links():
    """
    The employee numbers that were decided by hand, and stick.

    Worth being able to see and undo: a link made in error would keep putting
    one person's hours against another's name on every future import, and
    nothing else in the flow would ever ask about it again.
    """
    rows = (
        ClockEmployeeLink.query
        .order_by(ClockEmployeeLink.emp_no)
        .all()
    )
    usage = dict(
        db.session.query(ClockEmployee.emp_no, func.count(ClockEmployee.id))
        .group_by(ClockEmployee.emp_no)
        .all()
    )
    return render_template("timeclock/links.html", links=rows, usage=usage,
                           confirm_form=ConfirmForm())


@timeclock_bp.route("/links/<int:link_id>/delete", methods=["POST"])
@login_required
@require_perm("timeclock", "edit")
def delete_link(link_id):
    form = ConfirmForm()
    if not form.validate_on_submit():
        flash("Could not remove that link — please try again.", "danger")
        return redirect(url_for("timeclock.links"))

    link = ClockEmployeeLink.query.get_or_404(link_id)
    emp_no = link.emp_no
    db.session.delete(link)
    db.session.commit()

    flash(f"Link for {emp_no} removed — the next import will decide it afresh.", "warning")
    return redirect(url_for("timeclock.links"))
