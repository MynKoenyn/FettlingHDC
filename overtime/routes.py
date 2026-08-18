from flask import Blueprint, render_template, redirect, url_for, flash, request, abort, Response
from flask_login import login_required, current_user
from functools import wraps
from sqlalchemy import or_, and_
from datetime import datetime, date, timedelta
from decimal import Decimal, InvalidOperation
from uuid import uuid4
import math, calendar, os
from collections import defaultdict
from werkzeug.utils import secure_filename

from overtime.calc import (
    get_sa_public_holidays,
    compute_hours,
    overtime_multiplier,
    compute_amount,
    deduct_minutes,
)

overtime_bp = Blueprint("overtime", __name__,
                        url_prefix="/overtime",
                        template_folder="templates")

# ── lazy imports to avoid circular deps ────────────────────────────────────
def _db():
    from app import db
    return db

def _models():
    from models import (OvertimeRequest, Personnel, Division,
                        Department, Permission, UserPermission, User)
    return OvertimeRequest, Personnel, Division, Department, Permission, UserPermission, User

# ══════════════════════════════════════════════════════════════════════
# PERMISSION HELPERS
# ══════════════════════════════════════════════════════════════════════

def has_permission(user, module, action):
    """Literal check — does the user hold this exact grant? (No admin bypass.)"""
    if not user.is_authenticated:
        return False
    return user.has_permission(module, action)


def can(module, action):
    """
    Effective check — honours the admin bypass and the unrestricted-account
    rule. Use this to decide what a user may actually do or see.
    """
    from access.guards import user_can
    return user_can(current_user, module, action)


def can_see_money():
    """Whether the current user may see rates and calculated Rand amounts."""
    return can("overtime", "rates")


def permission_required(module, action):
    """
    Route decorator — 403 if the user may not perform the action.

    Uses the app-wide guard (admin bypass + the unrestricted-account rule) so
    overtime enforces access the same way every other module does, and the
    sidebar's can()-driven links never point somewhere the user is then
    bounced from.
    """
    def decorator(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            if not can(module, action):
                abort(403)
            return f(*args, **kwargs)
        return decorated
    return decorator


# ══════════════════════════════════════════════════════════════════════
# SCOPING HELPER
# ══════════════════════════════════════════════════════════════════════

def get_requestable_personnel(requester):
    """
    Returns Personnel the requester is allowed to submit or capture overtime
    for:
      - Admins, and anyone holding overtime/all_personnel: everyone
      - Others: personnel explicitly assigned to them in personnel_managers,
        OR personnel in the requester's own division.

    The all_personnel grant exists for people who capture on behalf of the
    whole company (payroll, HR) and so cannot be described by a division or a
    list of manager assignments. It is checked literally rather than through
    can(), so the unrestricted-account rule does not silently widen the picker
    for every account that predates access control.

    There used to be an extra fallback that added anyone in a department named
    "Maintenance" regardless of division — a stopgap from before manager
    assignments were used. That leaked HDC's Maintenance staff into every
    other division's picker (e.g. an HDA user saw HDC Maintenance), so it has
    been removed. A maintenance manager now gets their people the same way
    everyone else does: through a Managers assignment, or by those people
    sitting in the manager's division.
    """
    OvertimeRequest, Personnel, Division, Department, Permission, UserPermission, User = _models()
    from models import PersonnelManager

    if requester.is_admin or has_permission(requester, "overtime", "all_personnel"):
        return Personnel.query.order_by(Personnel.name).all()

    filters = []

    # --- Explicit manager grants (Managers screen) ---
    managed_ids = (
        _db().session.query(PersonnelManager.personnel_id)
        .filter(PersonnelManager.manager_id == requester.id)
    )
    filters.append(Personnel.id.in_(managed_ids))

    # --- Everyone in the requester's own division ---
    if requester.division_id:
        filters.append(Personnel.division_id == requester.division_id)

    return Personnel.query.filter(or_(*filters)).order_by(Personnel.name).all()


# ══════════════════════════════════════════════════════════════════════
# SEED  (call once from app context)
# ══════════════════════════════════════════════════════════════════════

def seed_permissions():
    """
    Deprecated shim — the permission catalogue now lives in access/catalogue.py
    so every module's functions are defined in one place. Kept because older
    code imports seed_permissions from here.
    """
    from access.catalogue import seed_permissions as _seed
    return _seed()


# ══════════════════════════════════════════════════════════════════════
# ROUTES
# ══════════════════════════════════════════════════════════════════════

@overtime_bp.route("/")
@login_required
@permission_required("overtime", "view")
def index():
    OvertimeRequest, Personnel, Division, Department, Permission, UserPermission, User = _models()

    # Directors / approvers see everything; everyone else sees their own work
    # — what they submitted and what they captured. A standalone actual has no
    # requester at all, so without the capture side of that test the person who
    # captured it could not see, or even open, the entry they had just made.
    query = OvertimeRequest.query
    if not (has_permission(current_user, "overtime", "approve") or
            has_permission(current_user, "overtime", "admin")):
        query = query.filter(or_(
            OvertimeRequest.requested_by == current_user.id,
            OvertimeRequest.actual_captured_by == current_user.id,
        ))

    # Pending first, then newest to oldest
    requests_qs = query.order_by(
        (OvertimeRequest.status == "pending").desc(),
        OvertimeRequest.created_at.desc()
    ).all()

    pending_count = sum(1 for r in requests_qs if r.entry_type == "request" and r.status == "pending")

    from pagination_utils import ManualPagination
    page = request.args.get("page", 1, type=int)
    requests_page = ManualPagination(requests_qs, page=page, per_page=50)

    return render_template(
        "overtime/index.html",
        requests=requests_qs,
        requests_page=requests_page,
        pending_count=pending_count,
        show_money=can_see_money(),
        can_capture=can("overtime", "actual"),
    )


MAX_PERSONNEL_SECTIONS = 30


@overtime_bp.route("/new", methods=["GET", "POST"])
@login_required
@permission_required("overtime", "request")
def new_request():
    """
    Raise overtime requests for one or more personnel in a single submit.

    The form holds one section per personnel member — its own dates, times
    and reason — and each section becomes its own batch. Batches are never
    shared across personnel, on purpose: a week raised for one person and a
    week raised for another, in the same submit, are approved, viewed and
    captured entirely separately. Opening one person's request only ever
    shows that person's dates.

    Within a section: any number of days, and up to two periods on each.
    Every period is its own record so it can be decided on its own — an
    approver might allow the 04:00-06:00 stint and turn down the 14:00-16:00
    one on the same day.

    A request is the clock-on to clock-off window with no lunch break taken
    off. See the note on OvertimeRequest.authorisation for why.
    """
    from app import db
    OvertimeRequest, Personnel, Division, Department, Permission, UserPermission, User = _models()

    personnel_list = get_requestable_personnel(current_user)
    allowed_ids = {p.id for p in personnel_list}

    # Departments behind the "load a whole department" quick-add — only ones
    # that actually have someone the requester may submit for, since offering
    # an empty department would just produce a "nothing to add" click.
    departments = sorted(
        {p.department for p in personnel_list if p.department},
        key=lambda d: ((d.division.code if d.division else ""), d.name)
    )

    def render_form(prefill):
        return render_template("overtime/new_request.html",
                               personnel_list=personnel_list,
                               departments=departments,
                               holidays=_holiday_isos(date.today().year),
                               prefill=prefill,
                               max_sections=MAX_PERSONNEL_SECTIONS)

    if request.method == "POST":
        form = request.form

        sections_prefill = []          # handed back to the form on an error
        planned_by_section = []        # (person, reason, planned) — ready to write
        errors = []

        for i in range(MAX_PERSONNEL_SECTIONS):
            personnel_id = form.get(f"personnel_id_{i}", type=int)
            reason = form.get(f"reason_{i}", "").strip()
            days_prefill = _collect_days(form, ns=i, lunch=False)

            if not personnel_id and not reason and not days_prefill:
                continue  # this section was never opened — not part of this submission

            label = f"Personnel #{len(sections_prefill) + 1}"
            sections_prefill.append({"personnel_id": personnel_id or "", "reason": reason,
                                     "days": days_prefill})

            person = None
            if not personnel_id:
                errors.append(f"{label}: please select a personnel member.")
            elif personnel_id not in allowed_ids:
                errors.append(f"{label}: you are not authorised to submit overtime for that personnel member.")
            else:
                person = Personnel.query.get(personnel_id)
                if not person:
                    errors.append(f"{label}: personnel member not found.")

            if not days_prefill:
                errors.append(f"{label}: please select at least one date.")

            planned, day_errors = _plan_days(form, days_prefill, ns=i, lunch=False)
            errors.extend(f"{label} — {e}" for e in day_errors)

            if person is not None and not day_errors and days_prefill:
                planned_by_section.append((person, reason, planned))

        if not sections_prefill:
            errors.append("Add at least one personnel member.")

        if errors:
            for e in errors:
                flash(e, "danger")
            return render_form({"sections": sections_prefill or
                                [{"personnel_id": "", "reason": "", "days": []}]})

        batches = []   # (person, created rows) — one batch per personnel
        for person, reason, planned in planned_by_section:
            batch_id = _new_batch_id()
            created = []
            for on, start, end, _break_minutes, _late_minutes in planned:
                hours = compute_hours(start, end)
                multiplier = overtime_multiplier(on)

                ot = OvertimeRequest(
                    entry_type="request",
                    batch_id=batch_id,
                    requested_by=current_user.id,
                    personnel_id=person.id,
                    division_id=person.division_id,
                    department_id=person.department_id,
                    overtime_date=on,
                    start_time=start,
                    end_time=end,
                    hours=hours,
                    reason=reason or None,
                    status="pending",
                    overtime_amount=compute_amount(person.rate, hours, multiplier),
                )
                db.session.add(ot)
                created.append(ot)
            batches.append((person, created))

        db.session.commit()

        show_money = can_see_money()
        for person, created in batches:
            total_hours = sum(float(ot.hours or 0) for ot in created)
            days_n = len({ot.overtime_date for ot in created})
            msg = (f"Overtime requested for {person.name} — {len(created)} "
                   f"period{'s' if len(created) != 1 else ''} over {days_n} "
                   f"day{'s' if days_n != 1 else ''}, {total_hours:.2f} hrs")
            if show_money:
                total_amount = sum(float(ot.overtime_amount or 0) for ot in created)
                msg += f" | R{total_amount:,.2f}"
            flash(msg, "success")

        if len(batches) == 1:
            return redirect(url_for("overtime.view_request", request_id=batches[0][1][0].id))
        return redirect(url_for("overtime.index"))

    return render_form({"sections": [{"personnel_id": "", "reason": "", "days": []}]})


# ══════════════════════════════════════════════════════════════════════
# THE BATCH  (everything one submit created, worked as a unit)
# ══════════════════════════════════════════════════════════════════════

def _batch_entries(ot):
    """
    Every record raised or captured in the same submit as `ot`, in day order.

    Ordering by id within a day keeps the periods in the order they were
    entered, so the stint before the shift stays above the one after it.
    Records from before batches existed have no batch and stand alone.
    """
    OvertimeRequest = _models()[0]
    if not ot.batch_id:
        return [ot]
    return (OvertimeRequest.query
            .filter(OvertimeRequest.batch_id == ot.batch_id)
            .order_by(OvertimeRequest.overtime_date.asc(), OvertimeRequest.id.asc())
            .all())


def _group_by_day(entries):
    """The batch as [{date, entries}, …] so the screen can show a block a day."""
    days = []
    for ot in entries:
        if not days or days[-1]["date"] != ot.overtime_date:
            days.append({"date": ot.overtime_date, "entries": []})
        days[-1]["entries"].append(ot)
    return days


def _batch_totals(entries):
    """Requested, actual and unauthorised hours across the whole batch."""
    live = [ot for ot in entries
            if not (ot.entry_type == "request" and ot.status == "rejected" and not ot.has_actual)]
    return {
        "periods": len(entries),
        "days": len({ot.overtime_date for ot in entries}),
        "pending": sum(1 for ot in entries
                       if ot.entry_type == "request" and ot.status == "pending"),
        "awaiting_capture": sum(1 for ot in entries if ot.is_approved and not ot.has_actual),
        "req_hours": sum(float(ot.hours or 0) for ot in live),
        "act_hours": sum(float(ot.actual_hours or 0) for ot in live),
        "req_amount": sum(float(ot.overtime_amount or 0) for ot in live),
        "act_amount": sum(float(ot.actual_amount or 0) for ot in live),
        "unauthorised_hours": sum(float(ot.unauthorised_hours) for ot in live),
    }


def _may_see(entries):
    """
    Approvers and admins see everything. Everyone else sees a batch they had a
    hand in — raised any of it, or captured any of it. A standalone actual has
    no requester at all, so without the capture side of this test the person
    who captured it could not open the entry they had just made.
    """
    if (has_permission(current_user, "overtime", "approve") or
            has_permission(current_user, "overtime", "admin")):
        return True
    return any(ot.requested_by == current_user.id or
               ot.actual_captured_by == current_user.id for ot in entries)


@overtime_bp.route("/<int:request_id>")
@login_required
@permission_required("overtime", "view")
def view_request(request_id):
    """
    Open one period and get the whole submission it belongs to.

    A week of overtime is ten periods — two on each of five days — and they are
    raised, approved and captured together, so opening any one of them shows
    all ten rather than a single row with no context.
    """
    OvertimeRequest, Personnel, Division, Department, Permission, UserPermission, User = _models()

    ot = OvertimeRequest.query.get_or_404(request_id)
    entries = _batch_entries(ot)

    if not _may_see(entries):
        abort(403)

    return render_template("overtime/view_request.html",
                           ot=ot,
                           entries=entries,
                           days=_group_by_day(entries),
                           totals=_batch_totals(entries),
                           can_approve=can("overtime", "approve"),
                           can_capture=can("overtime", "actual"),
                           show_money=can_see_money())


@overtime_bp.route("/batch/decide", methods=["POST"])
@login_required
@permission_required("overtime", "approve")
def decide_batch():
    """
    Approve or reject the ticked periods in one go.

    Each period is decided on its own — an approver can allow the stint before
    the shift and turn down the one after it on the same day — but doing that
    one page at a time for a ten-period week is what stops people using it, so
    the whole set is decided in a single post.
    """
    from app import db
    OvertimeRequest = _models()[0]

    action    = request.form.get("action")
    notes     = request.form.get("approval_notes", "").strip()
    return_to = request.form.get("return_to", type=int)
    ids       = request.form.getlist("entry_ids", type=int)

    if action not in ("approve", "reject"):
        flash("Invalid action.", "danger")
        return redirect(url_for("overtime.view_request", request_id=return_to))

    if not ids:
        flash("Tick the periods to " + action + " first.", "warning")
        return redirect(url_for("overtime.view_request", request_id=return_to))

    entries = OvertimeRequest.query.filter(OvertimeRequest.id.in_(ids)).all()

    decided, skipped = 0, 0
    for ot in entries:
        # Only a period still waiting on a decision can be decided.
        if ot.entry_type != "request" or ot.status != "pending":
            skipped += 1
            continue
        ot.status         = "approved" if action == "approve" else "rejected"
        ot.approved_by    = current_user.id
        ot.approved_at    = datetime.now()
        ot.approval_notes = notes or None
        decided += 1

    db.session.commit()

    if decided:
        word = "approved" if action == "approve" else "rejected"
        flash(f"{decided} period{'s' if decided != 1 else ''} {word}.",
              "success" if action == "approve" else "danger")
    if skipped:
        flash(f"{skipped} period{'s' if skipped != 1 else ''} had already been "
              f"actioned and {'were' if skipped != 1 else 'was'} left alone.", "warning")

    return redirect(url_for("overtime.view_request", request_id=return_to or ids[0]))


# ══════════════════════════════════════════════════════════════════════
# ACTUAL OVERTIME  (capture what was really worked)
# ══════════════════════════════════════════════════════════════════════

def _holiday_isos(around_year):
    """Public holidays as ISO strings for the calendar to mark as double time."""
    out = []
    for year in (around_year - 1, around_year, around_year + 1):
        out.extend(d.isoformat() for d in get_sa_public_holidays(year))
    return sorted(out)


def _parse_time_pair(start_raw, end_raw, label, what):
    """
    One start → end pair off a form.

    Returns ((start, end), errors); the pair is None when both sides were left
    blank, which every caller treats as "not entered" rather than an error.
    """
    start_raw = (start_raw or "").strip()
    end_raw   = (end_raw or "").strip()

    if not start_raw and not end_raw:
        return None, []

    if not start_raw or not end_raw:
        return None, [f"{label}: the {what} needs both a start and an end time."]

    try:
        start = datetime.strptime(start_raw, "%H:%M").time()
        end   = datetime.strptime(end_raw, "%H:%M").time()
    except ValueError:
        return None, [f"{label}: the {what} has an invalid time."]

    if start == end:
        return None, [f"{label}: the {what} starts and ends at the same time."]

    return (start, end), []


def _parse_minutes(raw, label, what):
    """
    One unpaid deduction, in whole minutes.

    Returns (minutes, errors). Blank reads as nothing deducted. Minutes rather
    than a start and an end time because that is how a supervisor knows it: the
    clock time a break began is not recorded anywhere, but everyone knows it
    ran half an hour.
    """
    raw = (raw or "").strip()
    if not raw:
        return 0, []

    try:
        minutes = int(raw)
    except ValueError:
        return 0, [f"{label}: the {what} must be a whole number of minutes."]

    if minutes < 0:
        return 0, [f"{label}: the {what} cannot be negative."]
    if minutes > 24 * 60:
        return 0, [f"{label}: the {what} is longer than a day."]

    return minutes, []


def _check_deductions(start, end, break_minutes, late_minutes, label, what):
    """
    The period is a sane length, and something is left after the deductions.

    Returns errors only. Deducting the whole period leaves nothing to pay,
    which is a typo rather than an intention worth saving.
    """
    hours = compute_hours(start, end)
    if hours is None or hours <= 0 or hours > 24:
        return [f"{label}: the {what} is not a valid shift length."]

    if (break_minutes or late_minutes) and \
            deduct_minutes(hours, break_minutes, late_minutes) <= 0:
        return [f"{label}: the deductions take up the whole {what}, "
                f"leaving no overtime to pay."]

    return []


def _dates_field(ns=None):
    """Name of the hidden 'dates' field for this picker (see `ns` below)."""
    return "dates" if ns is None else f"dates_{ns}"


def _time_field(field, iso, ns=None):
    """
    Name of one time/deduction field for one day.

    `ns` namespaces every field to one picker among several on the same page
    — New Request lets several personnel be raised in one submit, one day
    picker each, and without a namespace their fields would collide (two
    people both working 2026-08-05 would fight over the same `s1_2026-08-05`
    input). Left as None, the field is named exactly as a lone picker always
    has, so New Actual and any single-picker page are untouched.
    """
    return f"{field}_{iso}" if ns is None else f"{field}_{ns}_{iso}"


def _parse_day_ranges(form, iso, day_label, ns=None, lunch=True):
    """
    Read one day's two time ranges off the day picker, each with its own unpaid
    minutes.

    Returns (ranges, errors), each range a (start, end, break_minutes,
    late_minutes) tuple. A range whose start and end are both blank is ignored
    — the second range is only there for people who worked twice in a day
    (before the shift and after it), so most days leave it empty.

    The deductions belong to the range rather than to the day, so a lunch taken
    during the morning stint does not quietly come off the evening one.

    `lunch` is off for requests. Whoever raises a request has no way of knowing
    whether a break will be taken or how long it is — that differs by division
    and by shift — so a request is the clock-on to clock-off window and the
    deductions only happen when the actual is captured.
    """
    ranges, errors = [], []

    for slot, name in (("1", "first"), ("2", "second")):
        pair, pair_errors = _parse_time_pair(
            form.get(_time_field(f"s{slot}", iso, ns)), form.get(_time_field(f"e{slot}", iso, ns)),
            day_label, f"{name} range")
        errors.extend(pair_errors)
        if not pair:
            continue

        start, end = pair

        break_minutes = late_minutes = 0
        if lunch:
            break_minutes, break_errors = _parse_minutes(
                form.get(_time_field(f"b{slot}", iso, ns)), day_label, f"{name} range lunch break")
            late_minutes, late_errors = _parse_minutes(
                form.get(_time_field(f"l{slot}", iso, ns)), day_label, f"{name} range late in / early out")
            errors.extend(break_errors)
            errors.extend(late_errors)

        deduction_errors = _check_deductions(start, end, break_minutes, late_minutes,
                                             day_label, f"{name} range")
        errors.extend(deduction_errors)
        if deduction_errors:
            continue

        ranges.append((start, end, break_minutes, late_minutes))

    return ranges, errors


def _collect_days(form, ns=None, lunch=True):
    """
    The days posted by the picker with the times typed against each, in the
    shape the form wants handed back after an error so nothing is re-entered.
    """
    fields = ["s1", "e1", "s2", "e2"] + (["b1", "l1", "b2", "l2"] if lunch else [])
    days = []
    for iso in dict.fromkeys(d.strip() for d in form.getlist(_dates_field(ns)) if d.strip()):
        day = {"date": iso}
        for field in fields:
            day[field] = form.get(_time_field(field, iso, ns), "")
        days.append(day)
    return days


def _plan_days(form, days, ns=None, lunch=True):
    """
    Validate every posted day up front and return the periods to write as
    (date, start, end, break_minutes, late_minutes), alongside any errors.

    Nothing is written until the whole submission validates, so a typo on the
    last day cannot leave the first four saved.
    """
    planned, errors = [], []

    for day in days:
        iso = day["date"]
        try:
            on = datetime.strptime(iso, "%Y-%m-%d").date()
        except ValueError:
            errors.append(f"Invalid date: {iso}.")
            continue

        label = on.strftime("%a %d %b %Y")
        ranges, range_errors = _parse_day_ranges(form, iso, label, ns=ns, lunch=lunch)
        errors.extend(range_errors)

        if not ranges and not range_errors:
            errors.append(f"{label}: enter a start and an end time, or remove the date.")

        planned.extend((on,) + period for period in ranges)

    return planned, errors


def _new_batch_id():
    """A fresh reference shared by everything one submit creates."""
    return uuid4().hex


@overtime_bp.route("/actual/new", methods=["GET", "POST"])
@login_required
@permission_required("overtime", "actual")
def new_actual():
    """
    Capture standalone actuals — overtime worked with no prior request.

    One personnel member, as many dates as were worked, and up to two time
    ranges per date, because the common pattern is a stint before the shift
    and another after it (e.g. 04:00–06:00 and 14:00–16:00 around a 06:00–14:00
    day). Every filled range becomes its own record, so one submit can create
    several entries and a person can hold two on the same day.

    Nothing is written until every date validates, so a typo on the last day
    cannot leave the first four saved.
    """
    from app import db
    OvertimeRequest, Personnel, Division, Department, Permission, UserPermission, User = _models()

    personnel_list = get_requestable_personnel(current_user)

    def render_form(prefill):
        return render_template("overtime/new_actual.html",
                               personnel_list=personnel_list,
                               show_money=can_see_money(),
                               holidays=_holiday_isos(date.today().year),
                               prefill=prefill)

    if request.method == "POST":
        form = request.form
        personnel_id = form.get("personnel_id", type=int)
        notes        = form.get("actual_notes", "").strip()

        # What the user typed, handed straight back to the form on an error so
        # nothing has to be re-entered.
        prefill = {
            "personnel_id": personnel_id or "",
            "notes": notes,
            "days": _collect_days(form),
        }

        errors = []

        person = None
        allowed_ids = {p.id for p in personnel_list}
        if not personnel_id:
            errors.append("Please select a personnel member.")
        elif personnel_id not in allowed_ids:
            errors.append("You are not authorised to capture overtime for that personnel member.")
        else:
            person = Personnel.query.get(personnel_id)
            if not person:
                errors.append("Personnel member not found.")

        if not prefill["days"]:
            errors.append("Please select at least one date.")

        planned, day_errors = _plan_days(form, prefill["days"])
        errors.extend(day_errors)

        if errors:
            for e in errors:
                flash(e, "danger")
            return render_form(prefill)

        batch_id = _new_batch_id()
        created = []
        for on, start, end, break_minutes, late_minutes in planned:
            ot = OvertimeRequest(
                entry_type="actual",
                batch_id=batch_id,
                division_id=person.division_id,
                department_id=person.department_id,
                overtime_date=on,
                status="captured",
                actual_start_time=start,
                actual_end_time=end,
                actual_break_minutes=break_minutes,
                actual_late_minutes=late_minutes,
                actual_notes=notes or None,
                actual_captured_by=current_user.id,
                actual_captured_at=datetime.now(),
            )
            # Set the relationship (not just the id) so recalc can read the
            # rate while the record is still transient.
            ot.personnel = person
            ot.recalc_actual()
            db.session.add(ot)
            created.append(ot)

        db.session.commit()

        days = len({ot.overtime_date for ot in created})
        if len(created) == 1:
            flash(f"Actual overtime captured for {person.name}.", "success")
        else:
            flash(f"Captured {len(created)} actual overtime entries for "
                  f"{person.name} across {days} day{'s' if days != 1 else ''}.", "success")
        return redirect(url_for("overtime.view_request", request_id=created[0].id))

    return render_form({"personnel_id": "", "notes": "", "days": []})


def _parse_actual_period(form, suffix, label):
    """
    One captured period — the time worked and the unpaid minutes off it.

    Returns ((start, end, break_minutes, late_minutes) or None, errors). None
    with no errors means the period was left blank, which the caller reads as
    "nothing was worked here" rather than a mistake.
    """
    pair, errors = _parse_time_pair(form.get(f"as_{suffix}"), form.get(f"ae_{suffix}"),
                                    label, "time worked")

    break_minutes, break_errors = _parse_minutes(
        form.get(f"brk_{suffix}"), label, "lunch break")
    late_minutes, late_errors = _parse_minutes(
        form.get(f"late_{suffix}"), label, "late in / early out")
    errors.extend(break_errors)
    errors.extend(late_errors)

    if pair is None:
        if (break_minutes or late_minutes) and not errors:
            errors.append(f"{label}: minutes were deducted with no time worked.")
        return None, errors
    if errors:
        return None, errors

    start, end = pair
    errors.extend(_check_deductions(start, end, break_minutes, late_minutes,
                                    label, "time worked"))
    if errors:
        return None, errors

    return (start, end, break_minutes, late_minutes), errors


def _check_amount(raw, label):
    """A hand-typed Rand amount, checked before anything is written."""
    raw = (raw or "").strip()
    if not raw:
        return []
    try:
        if Decimal(raw) < 0:
            return [f"{label}: the amount cannot be negative."]
    except (ValueError, InvalidOperation):
        return [f"{label}: the amount must be a number."]
    return []


def _actual_prefill(ot):
    """
    What the capture form starts with for one period.

    An actual already captured comes back as it stands. Otherwise an approved
    period is seeded with the times that were approved — most overtime is
    worked as authorised, so the capturer confirms it and edits only the ones
    that differed. Periods that were not approved start blank: nothing about
    them has been agreed, so there is nothing to suggest.
    """
    if ot.has_actual:
        return {"as": ot.actual_start_time.strftime("%H:%M") if ot.actual_start_time else "",
                "ae": ot.actual_end_time.strftime("%H:%M") if ot.actual_end_time else ""}
    if ot.is_approved:
        return {"as": ot.start_time.strftime("%H:%M") if ot.start_time else "",
                "ae": ot.end_time.strftime("%H:%M") if ot.end_time else ""}
    return {"as": "", "ae": ""}


@overtime_bp.route("/<int:request_id>/capture", methods=["GET", "POST"])
@login_required
@permission_required("overtime", "actual")
def capture_batch(request_id):
    """
    Fill in what was actually worked, for a whole submission at once.

    Every approved period is listed with the approved times already filled in,
    so the capturer confirms them and edits only what differed, adding the
    lunch break that came off. Periods that were rejected, or are still waiting
    on a decision, are listed too and can be captured: people do work overtime
    nobody signed off, and refusing to record it would only keep it off the
    reports. Unauthorised work is flagged, never blocked.

    Work on a day nobody requested at all goes in at the bottom as an extra
    period, saved as its own record, which reads as 'not requested'.
    """
    from app import db
    OvertimeRequest, Personnel, Division, Department, Permission, UserPermission, User = _models()

    ot = OvertimeRequest.query.get_or_404(request_id)
    entries = _batch_entries(ot)
    if not _may_see(entries):
        abort(403)

    person = ot.personnel

    def label_for(entry):
        when = entry.overtime_date.strftime("%a %d %b %Y")
        if entry.start_time and entry.end_time:
            return f"{when} {entry.start_time:%H:%M}-{entry.end_time:%H:%M}"
        return when

    def render_form(prefill):
        return render_template("overtime/capture_batch.html",
                               ot=ot,
                               entries=entries,
                               days=_group_by_day(entries),
                               prefill=prefill,
                               show_money=can_see_money())

    if request.method == "POST":
        form  = request.form
        notes = form.get("actual_notes", "").strip()

        extra_keys = [k.strip() for k in form.getlist("extras") if k.strip()]

        prefill = {
            "notes": notes,
            "periods": {
                str(e.id): {"as": form.get(f"as_{e.id}", ""), "ae": form.get(f"ae_{e.id}", ""),
                            "brk": form.get(f"brk_{e.id}", ""), "late": form.get(f"late_{e.id}", ""),
                            "amt": form.get(f"amt_{e.id}", "")}
                for e in entries
            },
            "extras": [
                {"key": key,
                 "date": form.get(f"xd_{key}", ""),
                 "as": form.get(f"as_x{key}", ""), "ae": form.get(f"ae_x{key}", ""),
                 "brk": form.get(f"brk_x{key}", ""), "late": form.get(f"late_x{key}", "")}
                for key in extra_keys
            ],
        }

        errors = []

        captured = []      # (entry, period or None)
        for entry in entries:
            label = label_for(entry)
            period, period_errors = _parse_actual_period(form, str(entry.id), label)
            errors.extend(period_errors)
            if can_see_money():
                errors.extend(_check_amount(form.get(f"amt_{entry.id}"), label))
            captured.append((entry, period))

        added = []         # (date, start, end, break_minutes)
        for extra in prefill["extras"]:
            raw = extra["date"].strip()
            if not raw and not any(extra[f] for f in ("as", "ae", "brk", "late")):
                continue          # a row the user added and then left alone
            if not raw:
                errors.append("An extra period was entered with no date.")
                continue
            try:
                on = datetime.strptime(raw, "%Y-%m-%d").date()
            except ValueError:
                errors.append(f"Invalid date on an extra period: {raw}.")
                continue

            label = on.strftime("%a %d %b %Y") + " (extra)"
            period, period_errors = _parse_actual_period(form, "x" + extra["key"], label)
            errors.extend(period_errors)
            if period is None and not period_errors:
                errors.append(f"{label}: enter a start and an end time, or clear the date.")
                continue
            if period:
                added.append((on,) + period)

        if errors:
            for e in errors:
                flash(e, "danger")
            return render_form(prefill)

        cleared = saved = 0
        for entry, period in captured:
            if period is None:
                # Blanked out on purpose — drop an actual captured by mistake
                # and leave the requested side of the record alone.
                if entry.has_actual:
                    entry.actual_start_time = entry.actual_end_time = None
                    entry.actual_break_minutes = 0
                    entry.actual_late_minutes = 0
                    entry.actual_hours = entry.actual_multiplier = None
                    entry.actual_amount = None
                    entry.actual_amount_overridden = False
                    entry.actual_captured_by = entry.actual_captured_at = None
                    cleared += 1
                continue

            start, end, break_minutes, late_minutes = period
            entry.actual_start_time    = start
            entry.actual_end_time      = end
            entry.actual_break_minutes = break_minutes
            entry.actual_late_minutes  = late_minutes
            entry.actual_notes         = notes or None
            entry.actual_captured_by   = current_user.id
            entry.actual_captured_at   = datetime.now()

            # A hand-typed amount only sticks for users who may see money, and
            # only until someone clears it and asks for the calculation back.
            if can_see_money() and form.get(f"reset_{entry.id}"):
                entry.actual_amount_overridden = False
                entry.recalc_actual(force_amount=True)
            else:
                entry.recalc_actual()
                amount_raw = form.get(f"amt_{entry.id}", "").strip()
                if amount_raw and can_see_money():
                    manual = Decimal(amount_raw)
                    if entry.actual_amount is None or manual != Decimal(entry.actual_amount):
                        entry.actual_amount = manual
                        entry.actual_amount_overridden = True

            saved += 1

        for on, start, end, break_minutes, late_minutes in added:
            extra = OvertimeRequest(
                entry_type="actual",
                batch_id=ot.batch_id,
                division_id=person.division_id,
                department_id=person.department_id,
                overtime_date=on,
                status="captured",
                actual_start_time=start,
                actual_end_time=end,
                actual_break_minutes=break_minutes,
                actual_late_minutes=late_minutes,
                actual_notes=notes or None,
                actual_captured_by=current_user.id,
                actual_captured_at=datetime.now(),
            )
            extra.personnel = person
            extra.recalc_actual()
            db.session.add(extra)

        db.session.commit()

        parts = []
        if saved:
            parts.append(f"{saved} period{'s' if saved != 1 else ''} captured")
        if added:
            parts.append(f"{len(added)} not requested")
        if cleared:
            parts.append(f"{cleared} cleared")

        if parts:
            flash(f"Actual overtime for {person.name} — " + ", ".join(parts) + ".", "success")
        else:
            flash("Nothing was captured.", "warning")

        return redirect(url_for("overtime.view_request", request_id=ot.id))

    return render_form({
        "notes": "",
        "periods": {
            str(e.id): dict(
                _actual_prefill(e),
                brk=str(e.actual_break_minutes or "") if e.has_actual else "",
                late=str(e.actual_late_minutes or "") if e.has_actual else "",
                amt=("%.2f" % float(e.actual_amount)
                     if e.actual_amount_overridden and e.actual_amount is not None else ""),
            )
            for e in entries
        },
        "extras": [],
    })


# ══════════════════════════════════════════════════════════════════════
# ADMIN — Permission management
# ══════════════════════════════════════════════════════════════════════

@overtime_bp.route("/admin/permissions")
@login_required
@permission_required("overtime", "admin")
def admin_permissions():
    OvertimeRequest, Personnel, Division, Department, Permission, UserPermission, User = _models()

    users = User.query.filter_by(active=True).order_by(User.name).all()
    permissions = Permission.query.order_by(Permission.module, Permission.action).all()

    # Build a set of (user_id, permission_id) for quick lookup in template
    granted = {(up.user_id, up.permission_id) for up in UserPermission.query.all()}

    return render_template("overtime/admin_permissions.html",
                           users=users,
                           permissions=permissions,
                           granted=granted)


@overtime_bp.route("/admin/permissions/grant", methods=["POST"])
@login_required
@permission_required("overtime", "admin")
def grant_permission():
    from app import db
    OvertimeRequest, Personnel, Division, Department, Permission, UserPermission, User = _models()

    user_id       = request.form.get("user_id", type=int)
    permission_id = request.form.get("permission_id", type=int)

    if not user_id or not permission_id:
        flash("Invalid request.", "danger")
        return redirect(url_for("overtime.admin_permissions"))

    exists = UserPermission.query.filter_by(
        user_id=user_id, permission_id=permission_id
    ).first()

    if not exists:
        up = UserPermission(
            user_id       = user_id,
            permission_id = permission_id,
            granted_by    = current_user.id,
            granted_at    = datetime.utcnow(),
        )
        db.session.add(up)
        db.session.commit()
        flash("Permission granted.", "success")
    else:
        flash("User already has that permission.", "info")

    return redirect(url_for("overtime.admin_permissions"))


@overtime_bp.route("/admin/permissions/revoke", methods=["POST"])
@login_required
@permission_required("overtime", "admin")
def revoke_permission():
    from app import db
    OvertimeRequest, Personnel, Division, Department, Permission, UserPermission, User = _models()

    user_id       = request.form.get("user_id", type=int)
    permission_id = request.form.get("permission_id", type=int)

    up = UserPermission.query.filter_by(
        user_id=user_id, permission_id=permission_id
    ).first()

    if up:
        db.session.delete(up)
        db.session.commit()
        flash("Permission revoked.", "success")
    else:
        flash("Permission not found.", "warning")

    return redirect(url_for("overtime.admin_permissions"))


# ══════════════════════════════════════════════════════════════════════
# ADMIN — Personnel & Managers  (master data required for overtime)
# ══════════════════════════════════════════════════════════════════════

# ---- Personnel ----

@overtime_bp.route("/personnel")
@login_required
@permission_required("personnel", "view")
def personnel_list():
    OvertimeRequest, Personnel, Division, Department, Permission, UserPermission, User = _models()

    search = request.args.get("search", "").strip()
    query = Personnel.query
    if search:
        like = f"%{search}%"
        query = query.filter(or_(
            Personnel.name.ilike(like),
            Personnel.surname.ilike(like),
            Personnel.clockno.ilike(like),
        ))
    personnel = query.order_by(Personnel.name, Personnel.surname).all()
    return render_template("overtime/personnel_list.html", personnel=personnel, search=search)


# Categorical palette for job-title colour coding on the organogram (team
# columns) — fixed hue order per the dataviz colour formula, cycled if a
# division/department has more distinct job titles than slots.
ORGANOGRAM_JOB_COLORS = [
    "#2a78d6",  # blue
    "#eb6834",  # orange
    "#1baf7a",  # aqua
    "#eda100",  # yellow
    "#e87ba4",  # magenta
    "#008300",  # green
    "#4a3aa7",  # violet
    "#e34948",  # red
]


@overtime_bp.route("/personnel/organogram")
@login_required
@permission_required("personnel", "view")
def personnel_organogram():
    OvertimeRequest, Personnel, Division, Department, Permission, UserPermission, User = _models()

    division_id = request.args.get("division_id", type=int)
    department_id = request.args.get("department_id", type=int)

    def parent_id_of(p):
        """
        Where this person sits in the Director → Head → Production
        Superintendent → Supervisor → Personnel chain. A person's tier is
        decided by their own role tag, highest first — someone ticked both
        Head and Supervisor is placed as a Head (reporting to their
        Director), not a Supervisor.

        Every tier below the top falls back through whichever link is
        actually set (a Supervisor with no Superintendent reports straight
        to their Head; a plain employee with neither reports straight to
        whichever of Superintendent/Head/Director is set) rather than only
        ever checking the one field for its "normal" tier — otherwise
        skipping a tier drops that person out of the chart entirely.
        """
        if p.is_director:
            return None
        if p.is_head:
            return p.director_id
        if p.is_superintendent:
            return p.head_id
        if p.is_supervisor:
            return p.superintendent_id or p.head_id
        return p.supervisor_id or p.superintendent_id or p.head_id or p.director_id

    query = Personnel.query
    if division_id:
        query = query.filter(Personnel.division_id == division_id)
    if department_id:
        query = query.filter(Personnel.department_id == department_id)
    all_personnel = query.order_by(Personnel.name, Personnel.surname).all()
    by_id = {p.id: p for p in all_personnel}

    children = defaultdict(list)
    roots = []
    for p in all_personnel:
        pid = parent_id_of(p)
        if pid and pid in by_id:
            children[pid].append(p)
        else:
            roots.append(p)

    tree_roots = sorted(
        (p for p in roots if p.is_director or children.get(p.id)),
        key=lambda p: (0 if p.is_director else 1, p.name, p.surname or "")
    )

    # ------------------------------------------------------------------
    # Recursive org-chart layout, built node by node:
    #   - a node's Head/Superintendent/Director children ("sub_heads")
    #     render as their own nested boxes just below it, each laid out
    #     horizontally next to their siblings — e.g. a Head reporting to
    #     another Head appears under that parent Head rather than the top
    #     row, and a Production Superintendent appears under its Head the
    #     same way.
    #   - its Supervisor children ("teams") split out horizontally too,
    #     each as a column of that Supervisor's people (flattened; a
    #     Supervisor found deeper down starts its own column rather than
    #     nesting further).
    #   - any plain personnel reporting straight to this node with no
    #     Supervisor in between ("loose") sit alongside the team columns
    #     in an unheaded column of their own.
    # ------------------------------------------------------------------
    def sort_key(p):
        return (p.job_description or "", p.name, p.surname or "")

    def build_team(lead, teams_out, seen):
        if lead.id in seen:
            return {"lead": lead, "members": []}
        seen = seen | {lead.id}
        members = []

        def collect(p, seen_local):
            for c in children.get(p.id, []):
                if c.id in seen_local:
                    continue  # cycle guard — bad data shouldn't hang the page
                if c.is_supervisor:
                    teams_out.append(build_team(c, teams_out, seen_local | {c.id}))
                else:
                    members.append(c)
                    collect(c, seen_local | {c.id})

        collect(lead, seen)
        members.sort(key=sort_key)
        return {"lead": lead, "members": members}

    def build_node(p, seen):
        if p.id in seen:
            return {"person": p, "sub_heads": [], "teams": [], "loose": []}
        seen = seen | {p.id}
        kids = children.get(p.id, [])

        sub_heads = [
            build_node(k, seen) for k in kids
            if k.is_head or k.is_director or k.is_superintendent
        ]

        teams = []
        for k in kids:
            if k.is_supervisor:
                teams.append(build_team(k, teams, seen))

        loose = sorted(
            (k for k in kids
             if not (k.is_head or k.is_director or k.is_superintendent or k.is_supervisor)),
            key=sort_key
        )

        return {"person": p, "sub_heads": sub_heads, "teams": teams, "loose": loose}

    branches = [build_node(root, set()) for root in tree_roots]

    tree_root_ids = {p.id for p in tree_roots}
    unassigned = sorted(
        (p for p in roots if p.id not in tree_root_ids),
        key=lambda p: (p.name, p.surname or "")
    )

    # Stable colour per distinct job title, alphabetical so the same title
    # always lands on the same slot for a given filter selection.
    job_titles = sorted({
        (p.job_description or "").strip()
        for p in all_personnel
        if (p.job_description or "").strip()
    })
    job_colors = {
        title: ORGANOGRAM_JOB_COLORS[i % len(ORGANOGRAM_JOB_COLORS)]
        for i, title in enumerate(job_titles)
    }

    divisions = Division.query.order_by(Division.name).all()
    department_query = Department.query
    if division_id:
        department_query = department_query.filter(Department.division_id == division_id)
    departments = department_query.order_by(Department.name).all()

    return render_template(
        "overtime/organogram.html",
        branches=branches,
        unassigned=unassigned,
        job_colors=job_colors,
        divisions=divisions,
        departments=departments,
        division_id=division_id,
        department_id=department_id,
    )


PERSONNEL_PHOTO_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}


def _save_personnel_photo(person, remove_existing=False):
    """Persist a file posted under the "photo" input, replacing (or
    clearing) whatever this person already has.

    Stored under a generated name (person id + random hex) rather than the
    original filename, same as ProductImage, so two people's "IMG_0001.jpg"
    never collide. Requires person.id to already be set — call after a
    flush/commit on a brand-new record.
    """
    from app import PERSONNEL_PHOTO_DIR

    file = request.files.get("photo")
    has_new = bool(file and file.filename)
    if not has_new and not remove_existing:
        return

    if person.photo:
        old_path = os.path.join(PERSONNEL_PHOTO_DIR, person.photo)
        if os.path.exists(old_path):
            os.remove(old_path)
        person.photo = None

    if has_new:
        ext = os.path.splitext(file.filename)[1].lower()
        if ext not in PERSONNEL_PHOTO_EXTENSIONS:
            flash(f"Photo skipped — '{file.filename}' is not a supported image type.", "warning")
            return
        stored_name = f"{person.id}_{uuid4().hex}{ext}"
        file.save(os.path.join(PERSONNEL_PHOTO_DIR, secure_filename(stored_name)))
        person.photo = stored_name


@overtime_bp.route("/personnel/new", methods=["GET", "POST"])
@login_required
@permission_required("personnel", "edit")
def personnel_new():
    return _personnel_form(None)


@overtime_bp.route("/personnel/<int:personnel_id>/edit", methods=["GET", "POST"])
@login_required
@permission_required("personnel", "edit")
def personnel_edit(personnel_id):
    Personnel = _models()[1]
    person = Personnel.query.get_or_404(personnel_id)
    return _personnel_form(person)


def _personnel_form(person):
    """Shared create/edit handler for a Personnel record."""
    from app import db
    from models import PersonnelManager
    OvertimeRequest, Personnel, Division, Department, Permission, UserPermission, User = _models()

    departments = Department.query.order_by(Department.name).all()
    can_edit_heads = can("managers", "edit")
    all_users = User.query.filter_by(active=True).order_by(User.name).all() if can_edit_heads else []
    assigned_head_ids = (
        {pm.manager_id for pm in person.managers} if person else set()
    )

    # Supervisor / Superintendent / Head / Director pickers only offer
    # personnel ticked with that role (a person can't supervise, head or
    # direct themselves). A Head's "reports to" can be either a Director or
    # another Head — e.g. Stanton (Head) reporting to Juan (also just a
    # Head), not a Director — so that picker offers both.
    sup_query = Personnel.query.filter_by(is_supervisor=True).order_by(Personnel.name, Personnel.surname)
    superintendent_query = Personnel.query.filter_by(is_superintendent=True).order_by(Personnel.name, Personnel.surname)
    head_query = Personnel.query.filter_by(is_head=True).order_by(Personnel.name, Personnel.surname)
    director_query = Personnel.query.filter(
        or_(Personnel.is_director == True, Personnel.is_head == True)  # noqa: E712
    ).order_by(Personnel.name, Personnel.surname)
    if person:
        sup_query = sup_query.filter(Personnel.id != person.id)
        superintendent_query = superintendent_query.filter(Personnel.id != person.id)
        head_query = head_query.filter(Personnel.id != person.id)
        director_query = director_query.filter(Personnel.id != person.id)
    supervisors = sup_query.all()
    superintendents = superintendent_query.all()
    heads = head_query.all()
    directors = director_query.all()

    if request.method == "POST":
        name            = request.form.get("name", "").strip()
        surname         = request.form.get("surname", "").strip()
        clockno         = request.form.get("clockno", "").strip()
        department_id   = request.form.get("department_id", type=int)
        supervisor_id   = request.form.get("supervisor_id", type=int)
        superintendent_id = request.form.get("superintendent_id", type=int)
        head_id         = request.form.get("head_id", type=int)
        director_id     = request.form.get("director_id", type=int)
        jobgrade        = request.form.get("jobgrade", "").strip()
        rate_raw        = request.form.get("rate", "").strip()
        id_no           = request.form.get("id_no", "").strip()
        gender          = request.form.get("gender", "").strip()
        joined_raw      = request.form.get("joined", "").strip()
        job_description = request.form.get("job_description", "").strip()
        pay_group       = request.form.get("pay_group", "").strip()
        furnace_role    = request.form.get("furnace_role", "").strip()
        icon            = request.form.get("icon", "").strip()
        icon_color      = request.form.get("icon_color", "").strip()
        status          = bool(request.form.get("status"))
        is_supervisor   = bool(request.form.get("is_supervisor"))
        is_superintendent = bool(request.form.get("is_superintendent"))
        is_head         = bool(request.form.get("is_head"))
        is_director     = bool(request.form.get("is_director"))
        head_ids        = set(request.form.getlist("head_ids", type=int)) if can_edit_heads else assigned_head_ids
        assigned_head_ids = head_ids

        errors = []
        if not name:
            errors.append("First name is required.")
        if not clockno:
            errors.append("Clock number is required.")

        dept = Department.query.get(department_id) if department_id else None
        if not dept:
            errors.append("Please select a department.")

        from models import ALL_ICON_KEYS, ICON_COLOR_SWATCHES
        if icon and icon not in ALL_ICON_KEYS:
            errors.append("Invalid icon selection.")
        if icon_color and icon_color not in ICON_COLOR_SWATCHES:
            errors.append("Invalid icon colour selection.")
        if icon and not icon_color:
            icon_color = ICON_COLOR_SWATCHES[0]

        if clockno:
            dupe = Personnel.query.filter(Personnel.clockno == clockno)
            if person:
                dupe = dupe.filter(Personnel.id != person.id)
            if dupe.first():
                errors.append(f"Clock number '{clockno}' is already in use.")

        rate = None
        if rate_raw:
            try:
                rate = float(rate_raw)
                if rate < 0:
                    errors.append("Rate cannot be negative.")
            except ValueError:
                errors.append("Rate must be a number.")

        joined = None
        if joined_raw:
            try:
                joined = datetime.strptime(joined_raw, "%Y-%m-%d").date()
            except ValueError:
                errors.append("Invalid joined date.")

        if errors:
            from models import PAY_GROUPS, PERSONNEL_ICONS, ICON_COLOR_SWATCHES
            from furnace.models import FURNACE_ROLES
            for e in errors:
                flash(e, "danger")
            return render_template("overtime/personnel_form.html",
                                   person=person, departments=departments,
                                   supervisors=supervisors, superintendents=superintendents,
                                   heads=heads, directors=directors,
                                   all_users=all_users, assigned_head_ids=assigned_head_ids,
                                   can_edit_heads=can_edit_heads,
                                   pay_groups=PAY_GROUPS, furnace_roles=FURNACE_ROLES,
                                   personnel_icons=PERSONNEL_ICONS,
                                   icon_color_swatches=ICON_COLOR_SWATCHES,
                                   form_data=request.form)

        created = person is None
        if created:
            person = Personnel(clockno=clockno)
            db.session.add(person)

        person.name            = name
        person.surname         = surname or None
        person.clockno         = clockno
        person.department_id   = dept.id
        person.division_id     = dept.division_id
        # A person can't be their own supervisor/superintendent/head/director; 0 / blank clears it.
        person.supervisor_id   = supervisor_id if (supervisor_id and (not person.id or supervisor_id != person.id)) else None
        person.superintendent_id = superintendent_id if (superintendent_id and (not person.id or superintendent_id != person.id)) else None
        person.head_id         = head_id if (head_id and (not person.id or head_id != person.id)) else None
        person.director_id     = director_id if (director_id and (not person.id or director_id != person.id)) else None
        person.jobgrade        = jobgrade[:3] if jobgrade else None
        person.rate            = rate
        person.id_no           = id_no or None
        person.gender          = gender or None
        person.joined          = joined
        person.job_description = job_description or None
        person.pay_group       = pay_group or None
        person.furnace_role    = furnace_role or None
        person.icon            = icon or None
        person.icon_color      = icon_color or None
        person.status          = status
        person.is_supervisor   = is_supervisor
        person.is_superintendent = is_superintendent
        person.is_head         = is_head
        person.is_director     = is_director

        if created:
            db.session.flush()   # assigns person.id, needed to name the photo file
        _save_personnel_photo(person, remove_existing=bool(request.form.get("remove_photo")))

        db.session.commit()

        # Sync Head (PersonnelManager) grants — only for users allowed to
        # manage that assignment, so a personnel-edit-only account can't use
        # this form to grant itself/others overtime rights for this person.
        if can_edit_heads:
            existing = {pm.manager_id: pm for pm in
                        PersonnelManager.query.filter_by(personnel_id=person.id).all()}
            for uid in head_ids - existing.keys():
                db.session.add(PersonnelManager(personnel_id=person.id, manager_id=uid))
            for uid, pm in existing.items():
                if uid not in head_ids:
                    db.session.delete(pm)
            db.session.commit()

        full = f"{person.name} {person.surname or ''}".strip()
        flash(f"Personnel '{full}' {'added' if created else 'updated'} successfully.", "success")
        return redirect(url_for("overtime.personnel_list"))

    from models import PAY_GROUPS, PERSONNEL_ICONS, ICON_COLOR_SWATCHES
    from furnace.models import FURNACE_ROLES
    return render_template("overtime/personnel_form.html",
                           person=person, departments=departments,
                           supervisors=supervisors, superintendents=superintendents,
                           heads=heads, directors=directors,
                           all_users=all_users, assigned_head_ids=assigned_head_ids,
                           can_edit_heads=can_edit_heads,
                           pay_groups=PAY_GROUPS, furnace_roles=FURNACE_ROLES,
                           personnel_icons=PERSONNEL_ICONS,
                           icon_color_swatches=ICON_COLOR_SWATCHES, form_data={})


# ---- Personnel import / export (CSV & Excel) ----

@overtime_bp.route("/personnel/import", methods=["GET", "POST"])
@login_required
@permission_required("personnel", "edit")
def personnel_import():
    from personnel_importer import import_personnel as run_import

    result = None
    if request.method == "POST":
        file = request.files.get("file")
        if not file or not file.filename:
            flash("Choose a CSV or Excel file to import.", "danger")
            return redirect(url_for("overtime.personnel_import"))
        if not file.filename.lower().endswith((".csv", ".xlsx", ".xlsm")):
            flash("CSV or Excel files only.", "danger")
            return redirect(url_for("overtime.personnel_import"))

        try:
            result = run_import(file)
        except ValueError as exc:
            flash(f"Import failed: {exc}", "danger")
            return redirect(url_for("overtime.personnel_import"))

        if result.touched:
            flash(
                f"{result.created} personnel added, {result.updated} updated"
                + (f", {result.skipped} row(s) skipped." if result.skipped else "."),
                "success"
            )
        else:
            flash("Nothing was imported — no usable rows in that file.", "warning")

        if result.unknown_columns:
            flash("Columns ignored (not part of the template): "
                  + ", ".join(result.unknown_columns), "info")

    from models import PAY_GROUPS
    return render_template("overtime/personnel_import.html", result=result, pay_groups=PAY_GROUPS)


@overtime_bp.route("/personnel/import/template.csv")
@login_required
@permission_required("personnel", "edit")
def personnel_import_template():
    from personnel_importer import build_template_csv
    return Response(
        build_template_csv(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=personnel_import_template.csv"},
    )


@overtime_bp.route("/personnel/export.csv")
@login_required
@permission_required("personnel", "view")
def personnel_export():
    from personnel_importer import build_export_csv
    return Response(
        build_export_csv(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=personnel.csv"},
    )


# ---- Managers (who may request / approve OT for which personnel) ----

@overtime_bp.route("/managers")
@login_required
@permission_required("managers", "view")
def managers_list():
    OvertimeRequest, Personnel, Division, Department, Permission, UserPermission, User = _models()
    from models import PersonnelManager

    grants = (
        PersonnelManager.query
        .join(Personnel, PersonnelManager.personnel_id == Personnel.id)
        .order_by(Personnel.name, Personnel.surname)
        .all()
    )
    personnel = Personnel.query.order_by(Personnel.name, Personnel.surname).all()
    users = User.query.filter_by(active=True).order_by(User.name).all()
    return render_template("overtime/managers.html",
                           grants=grants, personnel=personnel, users=users)


@overtime_bp.route("/managers/add", methods=["POST"])
@login_required
@permission_required("managers", "edit")
def managers_add():
    from app import db
    from models import PersonnelManager

    personnel_id = request.form.get("personnel_id", type=int)
    manager_id   = request.form.get("manager_id", type=int)

    if not personnel_id or not manager_id:
        flash("Select both a personnel member and a manager.", "danger")
        return redirect(url_for("overtime.managers_list"))

    exists = PersonnelManager.query.filter_by(
        personnel_id=personnel_id, manager_id=manager_id
    ).first()
    if exists:
        flash("That manager is already assigned to that personnel member.", "info")
    else:
        db.session.add(PersonnelManager(personnel_id=personnel_id, manager_id=manager_id))
        db.session.commit()
        flash("Manager assigned successfully.", "success")

    return redirect(url_for("overtime.managers_list"))


@overtime_bp.route("/managers/<int:grant_id>/delete", methods=["POST"])
@login_required
@permission_required("managers", "edit")
def managers_delete(grant_id):
    from app import db
    from models import PersonnelManager

    grant = PersonnelManager.query.get_or_404(grant_id)
    db.session.delete(grant)
    db.session.commit()
    flash("Manager assignment removed.", "success")
    return redirect(url_for("overtime.managers_list"))


@overtime_bp.route("/reports/weekly")
@login_required
@permission_required("overtime", "view")
def weekly_report():
    OvertimeRequest, Personnel, Division, Department, Permission, UserPermission, User = _models()

    from datetime import date, datetime, timedelta
    from collections import defaultdict

    # ---------------------------------------------------
    # Date range (default = current Monday to Sunday)
    # ---------------------------------------------------
    today = date.today()
    default_start = today - timedelta(days=today.weekday())
    default_end = default_start + timedelta(days=6)

    start_str = request.args.get("start")
    end_str = request.args.get("end")

    try:
        start_date = (
            datetime.strptime(start_str, "%Y-%m-%d").date()
            if start_str else default_start
        )
        end_date = (
            datetime.strptime(end_str, "%Y-%m-%d").date()
            if end_str else default_end
        )
    except ValueError:
        start_date = default_start
        end_date = default_end

    if start_date > end_date:
        start_date = default_start
        end_date = default_end

    # ---------------------------------------------------
    # Fetch approved overtime, and anything actually worked
    # ---------------------------------------------------
    # Approved requests give the authorised side of the week. Captured actuals
    # come in whether or not they were ever approved — a week that only counts
    # what was signed off hides exactly the hours worth looking at.
    requests = (
        OvertimeRequest.query
        .filter(
            OvertimeRequest.overtime_date >= start_date,
            OvertimeRequest.overtime_date <= end_date,
            or_(
                and_(
                    OvertimeRequest.status == "approved",
                    OvertimeRequest.approved_by != None,
                    OvertimeRequest.approved_at != None,
                ),
                OvertimeRequest.actual_hours != None,
            ),
        )
        .order_by(
            OvertimeRequest.overtime_date.asc(),
            OvertimeRequest.id.asc()
        )
        .all()
    )

    # ---------------------------------------------------
    # Build Weekly Structure
    # ---------------------------------------------------
    weeks = {}

    for ot in requests:
        iso = ot.overtime_date.isocalendar()
        key = (iso[0], iso[1])   # year, week number

        # Create week group
        if key not in weeks:
            monday = ot.overtime_date - timedelta(days=ot.overtime_date.weekday())
            dates = [monday + timedelta(days=i) for i in range(7)]

            weeks[key] = {
                "label": f"Week {iso[1]}, {iso[0]} ({monday.strftime('%d %b')} – {(monday + timedelta(days=6)).strftime('%d %b %Y')})",
                "dates": dates,
                "people": {},
            }

        week = weeks[key]
        pid = ot.personnel_id

        # Create employee row
        if pid not in week["people"]:
            week["people"][pid] = {
                "obj": ot.personnel,
                "hours": defaultdict(lambda: None),        # approved
                "act_hours": defaultdict(lambda: None),    # actually worked
                "unauthorised": defaultdict(float),
                "times": defaultdict(list),
                "total_hours": 0,
                "total_act_hours": 0,
                "total_amount": 0,
                "total_act_amount": 0,
                "total_unauthorised": 0,
            }

        row = week["people"][pid]
        work_day = ot.overtime_date

        # ---------------------------------------------
        # The approved side
        # ---------------------------------------------
        if ot.is_approved and ot.hours is not None:
            row["hours"][work_day] = (row["hours"][work_day] or 0) + float(ot.hours)
            row["total_hours"] += float(ot.hours)
            row["total_amount"] += float(ot.overtime_amount or 0)

        # ---------------------------------------------
        # What was actually worked
        # ---------------------------------------------
        if ot.has_actual:
            row["act_hours"][work_day] = (row["act_hours"][work_day] or 0) + float(ot.actual_hours)
            row["total_act_hours"] += float(ot.actual_hours)
            row["total_act_amount"] += float(ot.actual_amount or 0)

            unauthorised = float(ot.unauthorised_hours)
            row["unauthorised"][work_day] += unauthorised
            row["total_unauthorised"] += unauthorised

        # ---------------------------------------------
        # Time ranges — several periods a day are normal
        # ---------------------------------------------
        if ot.has_actual:
            start_time, end_time, kind = ot.actual_start_time, ot.actual_end_time, "actual"
        else:
            start_time, end_time, kind = ot.start_time, ot.end_time, "approved"

        if start_time or end_time:
            row["times"][work_day].append({
                "start": start_time.strftime("%H:%M") if start_time else "",
                "end": end_time.strftime("%H:%M") if end_time else "",
                "kind": kind,
                "authorisation": ot.authorisation,
                "label": ot.authorisation_label,
                "break_minutes": ot.actual_break_minutes or 0,
                "late_minutes": ot.actual_late_minutes or 0,
                "deducted_minutes": ot.actual_deducted_minutes,
            })

    # ---------------------------------------------------
    # Sort Employees by Name
    # ---------------------------------------------------
    for week in weeks.values():
        week["people"] = dict(
            sorted(
                week["people"].items(),
                key=lambda x: (
                    x[1]["obj"].full_name
                    if hasattr(x[1]["obj"], "full_name")
                    else x[1]["obj"].name
                )
            )
        )

    # ---------------------------------------------------
    # Sort Weeks Chronologically
    # ---------------------------------------------------
    sorted_weeks = [weeks[k] for k in sorted(weeks.keys())]

    # ---------------------------------------------------
    # Render
    # ---------------------------------------------------
    return render_template(
        "overtime/reports_weekly.html",
        sorted_weeks=sorted_weeks,
        start_date=start_date,
        end_date=end_date,
        show_money=can_see_money(),
    )


# ══════════════════════════════════════════════════════════════════════
# REPORT — Actual vs Requested
# ══════════════════════════════════════════════════════════════════════

@overtime_bp.route("/reports/actual")
@login_required
@permission_required("overtime", "view")
def actual_report():
    """
    Requested against actual, per person, over a date range.

    Every record whose date falls in the range and that has either a
    requested side or a captured actual appears, grouped by person with
    per-person and grand totals, and a variance on each.
    """
    OvertimeRequest, Personnel, Division, Department, Permission, UserPermission, User = _models()

    today = date.today()
    default_start = today - timedelta(days=today.weekday())
    default_end = default_start + timedelta(days=6)

    start_str = request.args.get("start")
    end_str = request.args.get("end")
    try:
        start_date = datetime.strptime(start_str, "%Y-%m-%d").date() if start_str else default_start
        end_date = datetime.strptime(end_str, "%Y-%m-%d").date() if end_str else default_end
    except ValueError:
        start_date, end_date = default_start, default_end
    if start_date > end_date:
        start_date, end_date = default_start, default_end

    records = (
        OvertimeRequest.query
        .filter(
            OvertimeRequest.overtime_date >= start_date,
            OvertimeRequest.overtime_date <= end_date,
            # A standalone actual, or a request (approved requests carry the
            # requested figures; a pending one with an actual still counts).
            or_(
                OvertimeRequest.actual_hours.isnot(None),
                OvertimeRequest.entry_type == "request",
            ),
        )
        .order_by(OvertimeRequest.overtime_date.asc(), OvertimeRequest.start_time.asc())
        .all()
    )

    def fnum(v):
        return float(v) if v is not None else None

    groups = {}
    for ot in records:
        # A rejected request that never became an actual is not real overtime.
        if ot.entry_type == "request" and ot.status == "rejected" and not ot.has_actual:
            continue

        pid = ot.personnel_id
        if pid not in groups:
            groups[pid] = {
                "obj": ot.personnel,
                "rows": [],
                "req_hours": 0.0, "act_hours": 0.0,
                "req_amount": 0.0, "act_amount": 0.0,
                "unauthorised": 0.0,
            }
        g = groups[pid]

        req_hours = fnum(ot.hours) if ot.has_request else None
        act_hours = fnum(ot.actual_hours)
        req_amount = fnum(ot.overtime_amount) if ot.has_request else None
        act_amount = fnum(ot.actual_amount)

        unauthorised = float(ot.unauthorised_hours)

        g["rows"].append({
            "id": ot.id,
            "date": ot.overtime_date,
            "entry_type": ot.entry_type,
            "status": ot.status,
            "has_request": ot.has_request,
            "has_actual": ot.has_actual,
            "req_hours": req_hours,
            "act_hours": act_hours,
            "req_amount": req_amount,
            "act_amount": act_amount,
            "var_hours": fnum(ot.variance_hours),
            "var_amount": fnum(ot.variance_amount),
            "req_time": (ot.start_time, ot.end_time),
            "act_time": (ot.actual_start_time, ot.actual_end_time),
            "break_minutes": ot.actual_break_minutes or 0,
            "late_minutes": ot.actual_late_minutes or 0,
            "deducted_minutes": ot.actual_deducted_minutes,
            "authorisation": ot.authorisation,
            "authorisation_label": ot.authorisation_label,
            "unauthorised": unauthorised,
        })

        g["req_hours"] += req_hours or 0
        g["act_hours"] += act_hours or 0
        g["req_amount"] += req_amount or 0
        g["act_amount"] += act_amount or 0
        g["unauthorised"] += unauthorised

    people = sorted(
        groups.values(),
        key=lambda g: (g["obj"].name or "", g["obj"].surname or "") if g["obj"] else ("", ""),
    )

    grand = {
        "req_hours": sum(g["req_hours"] for g in people),
        "act_hours": sum(g["act_hours"] for g in people),
        "req_amount": sum(g["req_amount"] for g in people),
        "act_amount": sum(g["act_amount"] for g in people),
        "unauthorised": sum(g["unauthorised"] for g in people),
    }
    grand["var_hours"] = grand["act_hours"] - grand["req_hours"]
    grand["var_amount"] = grand["act_amount"] - grand["req_amount"]

    # "Show me only the hours nobody signed off" — the question the report
    # exists to answer, so it is a filter rather than something to eyeball.
    unauthorised_only = request.args.get("unauthorised") == "1"
    if unauthorised_only:
        for group in people:
            group["rows"] = [r for r in group["rows"] if r["unauthorised"]]
        people = [g for g in people if g["rows"]]

    return render_template(
        "overtime/reports_actual.html",
        people=people,
        grand=grand,
        start_date=start_date,
        end_date=end_date,
        unauthorised_only=unauthorised_only,
        show_money=can_see_money(),
    )