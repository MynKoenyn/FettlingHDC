"""
stocktake/routes.py
Blueprint: stocktake
Prefix:    /stocktake
"""

from datetime import date, timedelta
from decimal import Decimal

from flask import (
    Blueprint, render_template, redirect, url_for,
    flash, request, jsonify, abort
)
from sqlalchemy import func, case
from wtforms.fields import form

from flask_login import current_user

from app import db
from models import *
from access.guards import user_can, require_perm
from stocktake.models import (
    StocktakeHeader, StocktakeLine, SectionEnum,
    StocktakeBinLine, BinTypeEnum, BIN_TYPE_META,
)
from stocktake.forms import (
    StocktakeHeaderForm, BarcodeEntryForm, DeleteForm,
    BinEntryForm, BinRateForm,
)

stocktake_bp = Blueprint("stocktake", __name__, url_prefix="/stocktake")


def bin_line_value(bin_line, rate):
    """Rand value of a bin line at the given BinRate (or None if no rate)."""
    if rate is None:
        return None
    return float(bin_line.weight_kg) * float(rate.rate_per_kg)


# ─────────────────────────────────────────────
#  HELPERS
# ─────────────────────────────────────────────

def _dept_choices():
    """Return [(id, 'Division – Dept'), ...] for SelectField."""
    depts = (
        db.session.query(Department)
        .join(Division)
        .order_by(Division.code, Department.name)
        .all()
    )
    return [(d.id, f"{d.division.code} – {d.name}") for d in depts]


# ─────────────────────────────────────────────
#  DASHBOARD
# ─────────────────────────────────────────────

@stocktake_bp.route("/")
@require_perm("stocktake", "view")
def dashboard():
    today = date.today()
    thirty_days_ago = today - timedelta(days=30)

    # ── Today's sessions ──────────────────────
    todays_headers = (
        StocktakeHeader.query
        .filter_by(date=today)
        .join(Department)
        .order_by(Department.name)
        .all()
    )

    # ── Product totals (TODAY) ────────────────
    product_totals = (
        db.session.query(
            StocktakeLine.product_id,
            func.sum(StocktakeLine.count_value).label("count_total"),
            func.max(StocktakeLine.current_stock).label("book_value"),
        )
        .select_from(StocktakeLine)  # ✅ anchor
        .join(StocktakeHeader, StocktakeLine.header_id == StocktakeHeader.id)
        .filter(StocktakeHeader.date == today)
        .group_by(StocktakeLine.product_id)
    ).subquery()

    # ── Today's totals ────────────────────────
    today_stats = (
        db.session.query(
            func.count(product_totals.c.product_id).label("products"),
            func.sum(product_totals.c.book_value).label("book_total"),
            func.sum(product_totals.c.count_total).label("count_total"),
            func.sum(
                product_totals.c.count_total - product_totals.c.book_value
            ).label("variance"),
        )
    ).one()

    # ── Dept + Division totals (FIXED) ────────
    dept_product_totals = (
        db.session.query(
            StocktakeLine.product_id,
            Department.name.label("dept"),
            Division.code.label("division"),
            func.sum(StocktakeLine.count_value).label("count_total"),
            func.max(StocktakeLine.current_stock).label("book_value"),
        )
        .select_from(StocktakeLine)  # ✅ anchor
        .join(StocktakeHeader, StocktakeLine.header_id == StocktakeHeader.id)
        .join(Department, StocktakeHeader.department_id == Department.id)
        .join(Division, Department.division_id == Division.id)
        .filter(StocktakeHeader.date == today)
        .group_by(
            StocktakeLine.product_id,
            Department.name,
            Division.code,
        )
    ).subquery()

    dept_variance = (
        db.session.query(
            dept_product_totals.c.dept,
            dept_product_totals.c.division,
            func.sum(
                dept_product_totals.c.count_total
                - dept_product_totals.c.book_value
            ).label("variance"),
            func.count(dept_product_totals.c.product_id).label("products"),
        )
        .group_by(
            dept_product_totals.c.dept,
            dept_product_totals.c.division,
        )
        .all()
    )

    # ── Top variances (last 30 days) FIXED ────
    product_30 = (
        db.session.query(
            StocktakeLine.product_id,
            StocktakeLine.customer_id,
            func.sum(StocktakeLine.count_value).label("count_total"),
            func.max(StocktakeLine.current_stock).label("book_value"),
        )
        .select_from(StocktakeLine)  # ✅ anchor
        .join(StocktakeHeader, StocktakeLine.header_id == StocktakeHeader.id)
        .filter(StocktakeHeader.date >= thirty_days_ago)
        .group_by(
            StocktakeLine.product_id,
            StocktakeLine.customer_id,
        )
    ).subquery()

    top_variances = (
        db.session.query(
            Product.name.label("product"),
            Customer.name.label("customer"),
            (
                product_30.c.count_total - product_30.c.book_value
            ).label("variance"),
        )
        .join(Product, Product.id == product_30.c.product_id)
        .join(Customer, Customer.id == product_30.c.customer_id)
        .order_by(
            func.abs(
                product_30.c.count_total - product_30.c.book_value
            ).desc()
        )
        .limit(10)
        .all()
    )

    # ── Recent history ────────────────────────
    recent_headers = (
        StocktakeHeader.query
        .filter(StocktakeHeader.date >= today - timedelta(days=14))
        .filter(StocktakeHeader.date != today)
        .join(Department)
        .order_by(StocktakeHeader.date.desc(), Department.name)
        .limit(20)
        .all()
    )

    return render_template(
        "stocktake/dashboard.html",
        today=today,
        todays_headers=todays_headers,
        today_stats=today_stats,
        dept_variance=dept_variance,
        top_variances=top_variances,
        recent_headers=recent_headers,
    )

# ─────────────────────────────────────────────
#  CREATE / OPEN STOCKTAKE SESSION
# ─────────────────────────────────────────────

@stocktake_bp.route("/new", methods=["GET", "POST"])
@require_perm("stocktake", "capture")
def new_header():
    form = StocktakeHeaderForm()
    form.department_id.choices = [
        (id, name) for id, name in _dept_choices() if id in [1, 2, 3, 4]
        ]
    if form.validate_on_submit():
        # Prevent duplicate session for same date + dept + section
        existing = StocktakeHeader.query.filter_by(
            date=form.date.data,
            department_id=form.department_id.data,
            section=SectionEnum(form.section.data),
        ).first()
        if existing:
            flash(f"A stocktake already exists for that date / department / section. Redirecting to it.", "warning")
            return redirect(url_for("stocktake.entry", header_id=existing.id))

        header = StocktakeHeader(
            date=form.date.data,
            department_id=form.department_id.data,
            section=SectionEnum(form.section.data),
            notes=form.notes.data,
        )
        db.session.add(header)
        db.session.commit()
        flash("Stocktake session opened.", "success")
        return redirect(url_for("stocktake.entry", header_id=header.id))

    return render_template("stocktake/new_header.html", form=form)


# ─────────────────────────────────────────────
#  ENTRY FORM  (scan + count)
# ─────────────────────────────────────────────

@stocktake_bp.route("/<int:header_id>/entry", methods=["GET", "POST"])
@require_perm("stocktake", "capture")
def entry(header_id):
    header = StocktakeHeader.query.get_or_404(header_id)

    form = BarcodeEntryForm()
    delete_form = DeleteForm()
    if request.method == "Post":
        print("Form data:", request.form)  # Debugging line to check form data
        print("Form errors:", form.errors)  # Debugging line to check form validation errors
        print("Customer ID:", form.customer_id.data)  # Debugging line to check customer_id field
        print("Product ID:", form.product_id.data)    # Debugging line to check product_id field
        print("Count Value:", form.count_value.data)  # Debugging line to check count_value field
        print("Line Notes:", form.line_notes.data)    # Debugging line to check line_notes field
    # ---------------------------------------------------
    # POST
    # ---------------------------------------------------
    if form.validate_on_submit():

        product_id  = int(form.product_id.data)
        customer_id  = int(form.customer_id.data)
        count_value  = form.count_value.data
        line_notes   = form.line_notes.data

        department_id = header.department_id
        section        = header.section

        product = Product.query.get_or_404(product_id)

        # ---------------------------------------------------
        # 1. CHECK: SAME PRODUCT IN DIFFERENT SECTION
        # ---------------------------------------------------
        conflict = (
            db.session.query(StocktakeLine)
            .join(StocktakeHeader)
            .filter(
                StocktakeLine.product_id == product_id,
                StocktakeHeader.department_id == department_id,
                StocktakeHeader.section != section
            )
            .first()
        )

        if conflict:
            flash(
                f"❌ Product already counted in Section {conflict.header.section.value}. "
                f"Cannot update from Section {section.value}.",
                "warning"
            )
            return redirect(url_for("stocktake.entry", header_id=header_id))

        # ---------------------------------------------------
        # 2. CHECK: SAME PRODUCT IN SAME SECTION (UPDATE ONLY)
        # ---------------------------------------------------
        existing_line = (
            StocktakeLine.query
            .filter_by(
                header_id=header_id,
                product_id=product_id
            )
            .first()
        )

        if existing_line:
            existing_line.count_value = count_value
            existing_line.line_notes  = line_notes
            db.session.commit()

            flash("Line updated (already exists in this session).", "info")
            return redirect(url_for("stocktake.entry", header_id=header_id))

        # ---------------------------------------------------
        # 3. CREATE NEW LINE
        # ---------------------------------------------------
        line = StocktakeLine(
            header_id=header_id,
            customer_id=customer_id,
            product_id=product_id,
            current_stock=product.stockamount,
            count_value=count_value,
            line_notes=line_notes,
        )

        db.session.add(line)
        db.session.commit()

        flash("Line added.", "success")
        return redirect(url_for("stocktake.entry", header_id=header_id))

    # ---------------------------------------------------
    # GET LINES
    # ---------------------------------------------------
    lines = (
        StocktakeLine.query
        .filter_by(header_id=header_id)
        .join(Product)
        .join(Customer)
        .order_by(StocktakeLine.id.desc())
        .all()
    )

    # ---------------------------------------------------
    # HDA BIN STOCK (only meaningful for the HDA department)
    # ---------------------------------------------------
    bin_form = BinEntryForm()
    bin_rate = get_bin_rate_for(header.date)
    bin_lines_by_type = {bl.bin_type: bl for bl in header.bin_lines}
    bin_rows = [
        {
            "type": bt,
            "label": meta["label"],
            "weight_per_bin": meta["weight_kg"],
            "yield_pct": meta["yield_pct"],
            "line": bin_lines_by_type.get(bt),
            "value": bin_line_value(bin_lines_by_type[bt], bin_rate) if bt in bin_lines_by_type else None,
        }
        for bt, meta in BIN_TYPE_META.items()
    ]
    bin_total_value = sum(r["value"] for r in bin_rows if r["value"] is not None)

    return render_template(
        "stocktake/entry.html",
        header=header,
        delete_form=delete_form,
        form=form,
        lines=lines,
        bin_form=bin_form,
        bin_rate=bin_rate,
        bin_rows=bin_rows,
        bin_total_value=bin_total_value,
    )


# ─────────────────────────────────────────────
#  HDA BIN STOCK — add / update bin count
# ─────────────────────────────────────────────

@stocktake_bp.route("/<int:header_id>/bin-entry", methods=["POST"])
@require_perm("stocktake", "capture")
def bin_entry(header_id):
    header = StocktakeHeader.query.get_or_404(header_id)
    form = BinEntryForm()

    if form.validate_on_submit():
        try:
            bin_type = BinTypeEnum(form.bin_type.data)
        except ValueError:
            flash("Unknown bin type.", "danger")
            return redirect(url_for("stocktake.entry", header_id=header_id))

        existing = StocktakeBinLine.query.filter_by(
            header_id=header_id, bin_type=bin_type
        ).first()

        if existing:
            existing.bin_count = form.bin_count.data
            existing.notes = form.notes.data
        else:
            db.session.add(StocktakeBinLine(
                header_id=header_id,
                bin_type=bin_type,
                bin_count=form.bin_count.data,
                notes=form.notes.data,
            ))
        db.session.commit()
        flash(f"{BIN_TYPE_META[bin_type]['label']} bin count saved.", "success")
    else:
        flash("Could not save bin count — check the value entered.", "warning")

    return redirect(url_for("stocktake.entry", header_id=header_id))


# ─────────────────────────────────────────────
#  HDA BIN RATE — quarterly R/KG rate history
# ─────────────────────────────────────────────

@stocktake_bp.route("/bin-rates", methods=["GET", "POST"])
def bin_rates():
    if not user_can(current_user, "stocktake", "value"):
        abort(403)

    form = BinRateForm()
    if form.validate_on_submit():
        existing = BinRate.query.filter_by(effective_date=form.effective_date.data).first()
        if existing:
            existing.rate_per_kg = form.rate_per_kg.data
            flash("Rate updated for that effective date.", "info")
        else:
            db.session.add(BinRate(
                effective_date=form.effective_date.data,
                rate_per_kg=form.rate_per_kg.data,
                created_by=current_user.id,
            ))
            flash("New bin rate saved.", "success")
        db.session.commit()
        return redirect(url_for("stocktake.bin_rates"))

    rates = BinRate.query.order_by(BinRate.effective_date.desc()).all()
    current_rate = get_bin_rate_for(date.today())

    return render_template(
        "stocktake/bin_rates.html",
        form=form,
        rates=rates,
        current_rate=current_rate,
    )
 
 
# ─────────────────────────────────────────────
#  DELETE LINE
# ─────────────────────────────────────────────

@stocktake_bp.route("/line/<int:line_id>/delete", methods=["POST"])
@require_perm("stocktake", "capture")
def delete_line(line_id):
    line = StocktakeLine.query.get_or_404(line_id)
    header_id = line.header_id
    db.session.delete(line)
    db.session.commit()
    flash("Line removed.", "warning")
    return redirect(url_for("stocktake.entry", header_id=header_id))


# ─────────────────────────────────────────────
#  DAILY LIST  (read-only summary for a date)
# ─────────────────────────────────────────────

@stocktake_bp.route("/list")
@require_perm("stocktake", "view")
def daily_list():

    selected_date_str = request.args.get("date", date.today().isoformat())

    try:
        selected_date = date.fromisoformat(selected_date_str)
    except ValueError:
        selected_date = date.today()

    # ---------------------------------------------------
    # ORIGINAL HEADER VIEW (KEEP AS IS)
    # ---------------------------------------------------
    headers = (
        StocktakeHeader.query
        .filter_by(date=selected_date)
        .join(Department)
        .order_by(Department.name, StocktakeHeader.section)
        .all()
    )

    header_stats = {}
    for h in headers:
        stats = (
            db.session.query(
                func.count(StocktakeLine.id).label("lines"),
                func.coalesce(
                    func.sum(StocktakeLine.count_value - StocktakeLine.current_stock), 0
                ).label("variance"),
            )
            .filter(StocktakeLine.header_id == h.id)
            .one()
        )

        # Price each line using the price list period covering the session's date.
        value_total = 0.0
        for line in h.lines:
            price, _period = get_price_for(h.date, line.customer_id, line.product_id)
            line.value = float(line.count_value or 0) * float(price) if price is not None else None
            value_total += line.value or 0.0

        # HDA bin stock — priced off the bin rate history, not the price list.
        bin_rate = get_bin_rate_for(h.date)
        bin_value_total = 0.0
        for bl in h.bin_lines:
            bl.value = bin_line_value(bl, bin_rate)
            bin_value_total += bl.value or 0.0
        value_total += bin_value_total

        header_stats[h.id] = {
            "lines": stats.lines,
            "variance": stats.variance,
            "value": value_total,
            "bin_value": bin_value_total,
        }

    # ---------------------------------------------------
    # PIVOT QUERY (PRODUCT → DEPARTMENT BREAKDOWN)
    # ---------------------------------------------------
    rows = (
        db.session.query(
            Customer.id.label("customer_id"),
            Customer.name.label("customer"),
            Product.id.label("product_id"),
            Product.simplified_code.label("product"),
            StocktakeHeader.department_id.label("department_id"),
            func.sum(StocktakeLine.current_stock).label("book_stock"),
            func.sum(StocktakeLine.count_value).label("count_total"),
        )
        .join(StocktakeHeader, StocktakeLine.header_id == StocktakeHeader.id)
        .join(Customer, StocktakeLine.customer_id == Customer.id)
        .join(Product, StocktakeLine.product_id == Product.id)
        .filter(StocktakeHeader.date == selected_date)
        .group_by(
            Customer.id,
            Customer.name,
            Product.id,
            Product.name,
            StocktakeHeader.department_id,
        )
        .all()
    )

    # ---------------------------------------------------
    # BUILD PIVOT STRUCTURE
    # ---------------------------------------------------
    report = {}

    for r in rows:

        pid = r.product_id

        if pid not in report:
            report[pid] = {
                "customer": r.customer,
                "product": r.product,
                "departments": {},  # dept_id → value
                "total_book": float(r.book_stock or 0),  # ✔ SET ONCE ONLY
                "total_count": 0,
                "total_value": 0.0,
                "has_price": False,
                "variance": 0,
            }

        report[pid]["departments"][r.department_id] = {
            "book": float(r.book_stock or 0),
            "count": float(r.count_total or 0),
        }


        report[pid]["total_count"] += float(r.count_total or 0)

        # Price the count using the price list period covering the stocktake date.
        price, _period = get_price_for(selected_date, r.customer_id, pid)
        if price is not None:
            report[pid]["total_value"] += float(r.count_total or 0) * float(price)
            report[pid]["has_price"] = True

    # ---------------------------------------------------
    # FINAL VARIANCE CALCULATION
    # ---------------------------------------------------
    for pid, d in report.items():
        d["variance"] = d["total_count"] - d["total_book"]

    report_total_value = sum(d["total_value"] for d in report.values())

    # ---------------------------------------------------
    # PIVOT TOTALS ROW (department columns + book/count/variance/value)
    # ---------------------------------------------------
    dept_totals = {1: 0.0, 2: 0.0, 3: 0.0, 4: 0.0}
    for d in report.values():
        for dept_id, vals in d["departments"].items():
            if dept_id in dept_totals:
                dept_totals[dept_id] += vals["count"]

    report_totals = {
        "departments": dept_totals,
        "book":        sum(d["total_book"] for d in report.values()),
        "count":       sum(d["total_count"] for d in report.values()),
        "variance":    sum(d["variance"] for d in report.values()),
        "value":       report_total_value,
    }

    # ---------------------------------------------------
    # NAVIGATION
    # ---------------------------------------------------
    prev_date = selected_date - timedelta(days=1)
    next_date = selected_date + timedelta(days=1)

    return render_template(
        "stocktake/daily_list.html",

        # existing system (UNCHANGED)
        selected_date=selected_date,
        headers=headers,
        header_stats=header_stats,

        # NEW PIVOT REPORT
        report=report,
        report_total_value=report_total_value,
        report_totals=report_totals,

        today=date.today(),
        prev_date=prev_date,
        next_date=next_date,
    )


# ─────────────────────────────────────────────
#  VALUE REPORT  (filterable: date range, department, customer, product)
# ─────────────────────────────────────────────

@stocktake_bp.route("/report")
@require_perm("stocktake", "value")
def value_report():
    today = date.today()

    date_from_str = request.args.get("date_from", "")
    date_to_str   = request.args.get("date_to", "")
    department_id = request.args.get("department_id", type=int)
    customer_id   = request.args.get("customer_id", type=int)
    product_q     = request.args.get("product", "").strip()

    try:
        date_from = date.fromisoformat(date_from_str) if date_from_str else today - timedelta(days=30)
    except ValueError:
        date_from = today - timedelta(days=30)
    try:
        date_to = date.fromisoformat(date_to_str) if date_to_str else today
    except ValueError:
        date_to = today

    query = (
        db.session.query(StocktakeLine, StocktakeHeader, Product, Customer)
        .join(StocktakeHeader, StocktakeLine.header_id == StocktakeHeader.id)
        .join(Product, StocktakeLine.product_id == Product.id)
        .join(Customer, StocktakeLine.customer_id == Customer.id)
        .filter(StocktakeHeader.date >= date_from, StocktakeHeader.date <= date_to)
    )
    if department_id:
        query = query.filter(StocktakeHeader.department_id == department_id)
    if customer_id:
        query = query.filter(StocktakeLine.customer_id == customer_id)
    if product_q:
        like = f"%{product_q}%"
        query = query.filter(
            db.or_(
                Product.name.ilike(like),
                Product.simplified_code.ilike(like),
                Product.product_code.ilike(like),
            )
        )

    results = query.order_by(StocktakeHeader.date, Product.simplified_code).all()

    # ── Aggregate per product, pricing each line off its own session date ──
    by_product = {}
    for line, header, product, customer in results:
        pid = product.id
        d = by_product.setdefault(pid, {
            "product":   product.simplified_code or product.name,
            "customer":  customer.name,
            "book":      0.0,
            "count":     0.0,
            "value":     0.0,
            "has_price": False,
            "lines":     0,
        })
        d["book"]  += float(line.current_stock or 0)
        d["count"] += float(line.count_value or 0)
        d["lines"] += 1

        price, _period = get_price_for(header.date, line.customer_id, pid)
        if price is not None:
            d["value"] += float(line.count_value or 0) * float(price)
            d["has_price"] = True

    for d in by_product.values():
        d["variance"] = d["count"] - d["book"]

    report_rows = sorted(by_product.values(), key=lambda x: x["product"] or "")

    # ── HDA bin stock, aggregated per bin type over the same date range ──
    bin_query = (
        db.session.query(StocktakeBinLine, StocktakeHeader)
        .join(StocktakeHeader, StocktakeBinLine.header_id == StocktakeHeader.id)
        .filter(StocktakeHeader.date >= date_from, StocktakeHeader.date <= date_to)
    )
    if department_id:
        bin_query = bin_query.filter(StocktakeHeader.department_id == department_id)

    bin_by_type = {}
    for bl, header in bin_query.all():
        d = bin_by_type.setdefault(bl.bin_type, {
            "label":    bl.label,
            "bins":     0,
            "weight":   0.0,
            "value":    0.0,
            "has_rate": False,
        })
        d["bins"]   += bl.bin_count or 0
        d["weight"] += float(bl.weight_kg)

        value = bin_line_value(bl, get_bin_rate_for(header.date))
        if value is not None:
            d["value"] += value
            d["has_rate"] = True

    bin_rows = sorted(bin_by_type.values(), key=lambda x: x["label"])
    bin_total_value = sum(r["value"] for r in bin_rows)

    totals = {
        "book":     sum(r["book"] for r in report_rows),
        "count":    sum(r["count"] for r in report_rows),
        "variance": sum(r["variance"] for r in report_rows),
        "value":    sum(r["value"] for r in report_rows),
    }
    grand_total_value = totals["value"] + bin_total_value

    departments = _dept_choices()
    customers = Customer.query.order_by(Customer.name).all()

    return render_template(
        "stocktake/report.html",
        rows=report_rows,
        totals=totals,
        bin_rows=bin_rows,
        bin_total_value=bin_total_value,
        grand_total_value=grand_total_value,
        date_from=date_from,
        date_to=date_to,
        department_id=department_id,
        customer_id=customer_id,
        product_q=product_q,
        departments=departments,
        customers=customers,
    )


# ─────────────────────────────────────────────
#  AJAX — barcode / product lookup
# ─────────────────────────────────────────────

@stocktake_bp.route("/api/product-lookup")
@require_perm("stocktake", "capture")
def product_lookup():
    """
    GET /stocktake/api/product-lookup?q=<barcode_or_name>
    Returns JSON list of matching products for the typeahead / barcode scan.
    """
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify([])

    results = (
        db.session.query(Product, Customer)
        .join(Customer, Product.customer_id == Customer.id)
        .filter(
            # Deactivated products are not offered for counting.
            Product.active.isnot(False),
            db.or_(
                Product.name.ilike(f"%{q}%"),
                Product.barcode.ilike(f"%{q}%"),   # add barcode col if present
                Product.product_code.ilike(f"%{q}%"),
                Product.simplified_code.ilike(f"%{q}%"),
                Customer.name.ilike(f"%{q}%"),
            )
        )
        .limit(20)
        .all()
    )

    return jsonify([
        {
            "product_id":      p.id,
            "product_name":    p.name,
            "product_code":    p.product_code or "",
            "simplified_code": p.simplified_code or "",
            "customer_id":     c.id,
            "customer_name":   c.name,
            "images": [
                url_for("static", filename=f"images/products/{img.filename}")
                for img in p.images
            ],
        }
        for p, c in results
    ])