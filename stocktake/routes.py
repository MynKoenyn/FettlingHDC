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

from app import db
from models import *
from stocktake.models import StocktakeHeader, StocktakeLine, SectionEnum
from stocktake.forms import StocktakeHeaderForm, BarcodeEntryForm, DeleteForm

stocktake_bp = Blueprint("stocktake", __name__, url_prefix="/stocktake")


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

    return render_template(
        "stocktake/entry.html",
        header=header,
        delete_form=delete_form,
        form=form,
        lines=lines,
    )
 
 
# ─────────────────────────────────────────────
#  DELETE LINE
# ─────────────────────────────────────────────

@stocktake_bp.route("/line/<int:line_id>/delete", methods=["POST"])
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
        header_stats[h.id] = stats

    # ---------------------------------------------------
    # PIVOT QUERY (PRODUCT → DEPARTMENT BREAKDOWN)
    # ---------------------------------------------------
    rows = (
        db.session.query(
            Customer.name.label("customer"),
            Product.id.label("product_id"),
            Product.name.label("product"),
            StocktakeHeader.department_id.label("department_id"),
            func.sum(StocktakeLine.current_stock).label("book_stock"),
            func.sum(StocktakeLine.count_value).label("count_total"),
        )
        .join(StocktakeHeader, StocktakeLine.header_id == StocktakeHeader.id)
        .join(Customer, StocktakeLine.customer_id == Customer.id)
        .join(Product, StocktakeLine.product_id == Product.id)
        .filter(StocktakeHeader.date == selected_date)
        .group_by(
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
                "variance": 0,
            }

        report[pid]["departments"][r.department_id] = {
            "book": float(r.book_stock or 0),
            "count": float(r.count_total or 0),
        }

        
        report[pid]["total_count"] += float(r.count_total or 0)

    # ---------------------------------------------------
    # FINAL VARIANCE CALCULATION
    # ---------------------------------------------------
    for pid, d in report.items():
        d["variance"] = d["total_count"] - d["total_book"]

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

        today=date.today(),
        prev_date=prev_date,
        next_date=next_date,
    )


# ─────────────────────────────────────────────
#  AJAX — barcode / product lookup
# ─────────────────────────────────────────────

@stocktake_bp.route("/api/product-lookup")
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
            db.or_(
                Product.name.ilike(f"%{q}%"),
                Product.barcode.ilike(f"%{q}%"),   # add barcode col if present
                Customer.name.ilike(f"%{q}%"),
            )
        )
        .limit(20)
        .all()
    )

    return jsonify([
        {
            "product_id":    p.id,
            "product_name":  p.name,
            "customer_id":   c.id,
            "customer_name": c.name,
        }
        for p, c in results
    ])