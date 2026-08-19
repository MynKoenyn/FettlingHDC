"""
scrap/routes.py
Blueprint: scrap
Prefix:    /scrap

External scrap comes in by import (customer reject reports); internal scrap
is captured by hand. Both land in ScrapEntry, so every report can show them
separately or together.
"""

import csv
import io
import json
import math
from calendar import month_abbr
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from flask import (
    Blueprint, Response, abort, current_app, flash, g, redirect, render_template, request, url_for
)
from flask_login import current_user, login_required
from sqlalchemy import case, func
from weasyprint import HTML

from app import db
from access.guards import require_perm
from models import Customer, Division, Personnel, Product, active_products
from scrap.forms import (
    DeleteForm, InternalScrapForm, ScrapDefectForm, ScrapDispatchForm, ScrapImportForm
)
from scrap.importer import (
    allow_duplicate,
    build_product_index,
    build_template_csv,
    import_external_report,
    match_product,
    reject_duplicate,
)
from scrap.models import (
    DUP_PENDING,
    SCOPE_EXTERNAL,
    SCOPE_INTERNAL,
    SOURCE_CHOICES,
    SOURCE_EXTERNAL,
    SOURCE_INTERNAL,
    ScrapDefect,
    ScrapDispatch,
    ScrapDispatchBatch,
    ScrapEntry,
    ScrapEntryDefect,
    ScrapImportBatch,
    ScrapPendingDuplicate,
    active_defects,
)
from scrap.valuation import price_lookup

scrap_bp = Blueprint("scrap", __name__, url_prefix="/scrap")


# ─────────────────────────────────────────────
#  DIVISION SCOPING  (HDA vs HDC)
# ─────────────────────────────────────────────
# Every screen except the hub is reached as .../scrap/<division>/... — HDA
# trades as a single customer, HDC has its own customer list, so almost
# nothing (dashboard, capture, import, entries, reports) makes sense without
# knowing which one you're looking at. The report screens additionally take
# division="both" to step outside the split.
#
# `division` never reaches a view function as an argument — the blueprint's
# url_value_preprocessor below pops it off the matched URL into g.division /
# g.division_obj, and url_defaults puts it back on every url_for('scrap.*')
# call automatically, so templates and redirects don't have to pass it.

REPORT_ENDPOINTS = {
    "scrap.reports", "scrap.reports_export", "scrap.balance_report",
    "scrap.reason_report", "scrap.reason_report_export", "scrap.reason_report_pdf",
}


def _endpoint_takes_division(endpoint):
    rules = list(current_app.url_map.iter_rules(endpoint))
    return bool(rules) and "division" in rules[0].arguments


def _resolve_division(code):
    code = (code or "").lower()
    if code == "both":
        return None, "both"
    if code not in ("hda", "hdc"):
        abort(404)
    division = Division.query.filter(func.lower(Division.code) == code).first()
    if division is None:
        abort(404)
    return division, code


@scrap_bp.url_value_preprocessor
def _pull_division(endpoint, values):
    if values and "division" in values:
        code = values.pop("division")
        if code == "both" and endpoint not in REPORT_ENDPOINTS:
            abort(404)
        g.division_obj, g.division = _resolve_division(code)
    else:
        g.division = None
        g.division_obj = None


@scrap_bp.url_defaults
def _add_division(endpoint, values):
    if "division" in values:
        return
    division = getattr(g, "division", None)
    if division and _endpoint_takes_division(endpoint):
        values["division"] = division


def _division_label():
    return {"hda": "HDA", "hdc": "HDC"}.get(g.division, "HDA + HDC")


def _division_customer_ids():
    """Subquery of customer ids in the active division — None when unscoped
    ('both', or a global screen the division hook never touched)."""
    if getattr(g, "division_obj", None) is None:
        return None
    return db.session.query(Customer.id).filter(Customer.division_id == g.division_obj.id)


def _division_customers():
    query = Customer.query
    customer_ids = _division_customer_ids()
    if customer_ids is not None:
        query = query.filter(Customer.id.in_(customer_ids))
    return query.order_by(Customer.name).all()


def _locked_customer():
    """The one customer this division trades as, when there's exactly one —
    lets an HDA screen skip the picker and go straight to it. Falls back to
    a normal (division-scoped) picker the day that stops being true."""
    customers = _division_customers()
    return customers[0] if len(customers) == 1 else None


# ── HDA cage-based dispatch — dispatcher picker ──────────────────────────────
# The clock number of the usual dispatcher — defaults the picker so the
# common case needs no typing, but it's still a normal dropdown, changeable
# for whoever actually loaded the truck.
DEFAULT_DISPATCHER_CLOCKNO = "HDA042"


def _hda_dispatcher_choices():
    hda = Division.query.filter(func.lower(Division.code) == "hda").first()
    if hda is None:
        return [(0, "— Select —")]
    people = (
        Personnel.query.filter_by(division_id=hda.id, status=True)
        .order_by(Personnel.name, Personnel.surname)
        .all()
    )
    return [(0, "— Select —")] + [
        (p.id, f"{p.name} {p.surname or ''}".strip()) for p in people
    ]


def _default_dispatcher_id():
    person = Personnel.query.filter_by(clockno=DEFAULT_DISPATCHER_CLOCKNO, status=True).first()
    return person.id if person else 0


@scrap_bp.context_processor
def _inject_division():
    """Every scrap template can read `division` / `division_label` directly —
    saves threading it through every render_template() call by hand."""
    division = getattr(g, "division", None)
    return {
        "division": division,
        "division_obj": getattr(g, "division_obj", None),
        "division_label": _division_label() if division else None,
        "is_report_page": request.endpoint in REPORT_ENDPOINTS,
    }


# ─────────────────────────────────────────────
#  TEMPLATE FILTERS
# ─────────────────────────────────────────────

# Space-grouped rands read best, but a plain space lets the browser wrap
# "R 12 556.00" onto two lines mid-figure. Both separators are no-break spaces
# so an amount always stays on one line whatever the column width.
NBSP = " "


@scrap_bp.app_template_filter("rand")
def rand(value, decimals=2):
    """R 1 234 567.89 - space grouped, the way the rest of the plant reads money."""
    if value is None:
        return "—"
    return "R" + NBSP + f"{Decimal(value):,.{decimals}f}".replace(",", NBSP)


@scrap_bp.app_template_filter("rand_short")
def rand_short(value):
    """Compact rand for KPI tiles - R 1.2m / R 345k / R 890."""
    if value is None:
        return "—"
    amount = Decimal(value)
    for cutoff, suffix in ((1_000_000, "m"), (1_000, "k")):
        if abs(amount) >= cutoff:
            return "R" + NBSP + f"{amount / cutoff:,.1f}{suffix}".replace(",", NBSP)
    return "R" + NBSP + f"{amount:,.0f}".replace(",", NBSP)


# ─────────────────────────────────────────────
#  HELPERS
# ─────────────────────────────────────────────

def _customer_choices(include_blank=True, blank_label="— None —"):
    choices = [(c.id, c.name) for c in _division_customers()]
    return ([(0, blank_label)] + choices) if include_blank else choices


def _product_label(product):
    """Name plus product code / simplified code, so the search box can match any of them."""
    codes = [c for c in (product.product_code, product.simplified_code) if c]
    return product.name + (f" ({' / '.join(codes)})" if codes else "")


def _product_choices():
    # Deactivated products stay off the picker; entries already linked to one
    # keep their link and still report correctly.
    products = active_products().all()
    return [(0, "— Not linked —")] + [
        (p.id, _product_label(p)) for p in products
    ]


def _products_by_customer():
    """{customer_id: [{id, name}]} for the customer → product select filtering."""
    mapping = {}
    for product in active_products().all():
        customer_ids = set()
        if product.customer_id:
            customer_ids.add(product.customer_id)
        for customer in product.linked_customers:
            customer_ids.add(customer.id)
        label = _product_label(product)
        for cid in customer_ids:
            mapping.setdefault(cid, []).append({"id": product.id, "name": label})
    return mapping


def _product_casting_numbers():
    """{product_id: casting_no} used to auto-fill the casting number field."""
    return {p.id: p.product_code for p in active_products().all() if p.product_code}


def _product_details():
    """{product_id: {"grade": ..., "supplier_code": ...}} for the product info panel
    on the internal scrap capture screen."""
    return {
        p.id: {"grade": p.grade or "", "supplier_code": p.supplier_code or ""}
        for p in active_products().all()
    }


def _product_drawing_levels():
    """{product_id: drawing_level} — auto-fills a cage line's Drawing Level
    on HDA's dispatch screen, same mechanism as _product_casting_numbers()."""
    return {p.id: p.drawing_level for p in active_products().all() if p.drawing_level}


def _read_filters():
    """Filter values shared by the entry list and the reports."""
    return {
        "source":      request.args.get("source", "").strip(),
        "customer_id": request.args.get("customer_id", type=int),
        "product_id":  request.args.getlist("product_id", type=int),
        "start_date":  request.args.get("start_date", "").strip(),
        "end_date":    request.args.get("end_date", "").strip(),
        "year":        request.args.get("year", type=int),
        "batch_no":    request.args.get("batch_no", "").strip(),
    }


def _link_args(filters, **extra):
    """
    Filter values flattened for url_for — blanks dropped, reasons and products
    repeated (url_for turns a list value into one query param per item).
    """
    args = {
        "source":      filters.get("source") or "",
        "customer_id": filters.get("customer_id") or "",
        "product_id":  filters.get("product_id") or "",
        "start_date":  filters.get("start_date") or "",
        "end_date":    filters.get("end_date") or "",
        "year":        filters.get("year") or "",
        "batch_no":    filters.get("batch_no") or "",
    }
    args.update(extra)
    return args


def _apply_filters(query, filters):
    customer_ids = _division_customer_ids()
    if customer_ids is not None:
        query = query.filter(ScrapEntry.customer_id.in_(customer_ids))
    if filters.get("source") in (SOURCE_EXTERNAL, SOURCE_INTERNAL):
        query = query.filter(ScrapEntry.source == filters["source"])
    if filters.get("customer_id"):
        query = query.filter(ScrapEntry.customer_id == filters["customer_id"])
    if filters.get("product_id"):
        query = query.filter(ScrapEntry.product_id.in_(filters["product_id"]))
    if filters.get("start_date"):
        query = query.filter(ScrapEntry.entry_date >= filters["start_date"])
    if filters.get("end_date"):
        query = query.filter(ScrapEntry.entry_date <= filters["end_date"])
    if filters.get("year"):
        query = query.filter(func.extract("year", ScrapEntry.entry_date) == filters["year"])
    if filters.get("batch_no"):
        query = query.filter(ScrapEntry.batch_no.ilike(f"%{filters['batch_no']}%"))
    return query


def _pct(scrap, base):
    return (scrap * 100.0 / base) if base else None


def _packed_balance(customer_id=None, product_id=None, as_of=None):
    """
    {product_id: {"packed": int, "dispatched": int, "balance": int}}

    `product_id`, when given, is a list of ids — the balance report can now
    be scoped to more than one product at once.

    packed = total qty_packed captured on internal scrap entries;
    dispatched = total qty_dispatched recorded against that product.
    balance is never stored — always summed live from both tables — so it
    can't drift out of sync when an old entry or dispatch is edited or
    deleted. `as_of` caps both sums to entry_date/dispatch_date <= as_of, so
    a balance can be read "as it stood" on a given day.
    """
    packed_q = (
        db.session.query(ScrapEntry.product_id,
                          func.coalesce(func.sum(ScrapEntry.qty_packed), 0))
        .filter(ScrapEntry.source == SOURCE_INTERNAL, ScrapEntry.product_id.isnot(None))
    )
    dispatched_q = (
        db.session.query(ScrapDispatch.product_id,
                          func.coalesce(func.sum(ScrapDispatch.qty_dispatched), 0))
        .filter(ScrapDispatch.product_id.isnot(None))
    )
    if customer_id:
        packed_q = packed_q.filter(ScrapEntry.customer_id == customer_id)
        dispatched_q = dispatched_q.filter(ScrapDispatch.customer_id == customer_id)
    else:
        customer_ids = _division_customer_ids()
        if customer_ids is not None:
            packed_q = packed_q.filter(ScrapEntry.customer_id.in_(customer_ids))
            dispatched_q = dispatched_q.filter(ScrapDispatch.customer_id.in_(customer_ids))
    if product_id:
        packed_q = packed_q.filter(ScrapEntry.product_id.in_(product_id))
        dispatched_q = dispatched_q.filter(ScrapDispatch.product_id.in_(product_id))
    if as_of:
        packed_q = packed_q.filter(ScrapEntry.entry_date <= as_of)
        dispatched_q = dispatched_q.filter(ScrapDispatch.dispatch_date <= as_of)

    packed = dict(packed_q.group_by(ScrapEntry.product_id).all())
    dispatched = dict(dispatched_q.group_by(ScrapDispatch.product_id).all())

    result = {}
    for pid in set(packed) | set(dispatched):
        p, d = int(packed.get(pid, 0)), int(dispatched.get(pid, 0))
        result[pid] = {"packed": p, "dispatched": d, "balance": p - d}
    return result


def _apply_dispatch_filters(query, filters):
    customer_ids = _division_customer_ids()
    if customer_ids is not None:
        query = query.filter(ScrapDispatch.customer_id.in_(customer_ids))
    if filters.get("customer_id"):
        query = query.filter(ScrapDispatch.customer_id == filters["customer_id"])
    if filters.get("product_id"):
        query = query.filter(ScrapDispatch.product_id.in_(filters["product_id"]))
    if filters.get("start_date"):
        query = query.filter(ScrapDispatch.dispatch_date >= filters["start_date"])
    if filters.get("end_date"):
        query = query.filter(ScrapDispatch.dispatch_date <= filters["end_date"])
    return query


# Conditional sums — used to split any aggregate by source in one pass
_EXTERNAL_SCRAP = func.coalesce(
    func.sum(case((ScrapEntry.source == SOURCE_EXTERNAL, ScrapEntry.qty_scrap), else_=0)), 0
)
_INTERNAL_SCRAP = func.coalesce(
    func.sum(case((ScrapEntry.source == SOURCE_INTERNAL, ScrapEntry.qty_scrap), else_=0)), 0
)
# qty_packed only ever holds a value on internal rows, so a plain sum is
# already scoped to them — this is the internal side of "total produced"
# (qty_machined is the external side; see ScrapEntry.total_qty).
_PACKED_SUM = func.coalesce(func.sum(ScrapEntry.qty_packed), 0)


# ── Rand value ───────────────────────────────────────────────────────────────
# Scrap is valued line by line at the price that applied on its own entry date,
# so a report of last March keeps March's prices after a new list is loaded.
# Both queries below group only as finely as pricing needs — one row per date +
# customer + part — and every report folds those rows into its own grouping.

def _blank_value():
    return {
        "value": Decimal("0"), "priced_qty": 0, "unpriced_qty": 0,
        "list_qty": 0, "fallback_qty": 0,
    }


def _add_value(bucket, row):
    bucket["value"]        += row["value"]
    bucket["priced_qty"]   += row["priced_qty"]
    bucket["unpriced_qty"] += row["unpriced_qty"]
    bucket["list_qty"]     += row["list_qty"]
    bucket["fallback_qty"] += row["fallback_qty"]


def _price_basis(bucket):
    """
    'list', 'fallback', 'mixed' or None — which price source a valued bucket
    leans on, so a report can flag figures that rest on the catalogue price
    rather than an actual price list entry.
    """
    has_list = bool(bucket["list_qty"])
    has_fallback = bool(bucket["fallback_qty"])
    if has_list and has_fallback:
        return "mixed"
    if has_fallback:
        return "fallback"
    if has_list:
        return "list"
    return None


def _priced_scrap(filters):
    """Filtered scrap, priced. One row per date + customer + part."""
    group_cols = [
        ScrapEntry.entry_date, ScrapEntry.customer_id, ScrapEntry.product_id,
        ScrapEntry.casting_no, ScrapEntry.machined_part_no,
    ]
    rows = _apply_filters(
        db.session.query(
            *group_cols,
            func.coalesce(func.sum(ScrapEntry.qty_scrap), 0).label("qty"),
        ),
        filters,
    ).group_by(*group_cols).all()

    lookup = price_lookup((r.entry_date, r.customer_id, r.product_id) for r in rows)

    priced = []
    for row in rows:
        qty = int(row.qty or 0)
        value, ok, missing, list_qty, fallback_qty = lookup.value(
            row.entry_date, row.customer_id, row.product_id, qty
        )
        priced.append({
            "entry_date": row.entry_date,
            "product_id": row.product_id,
            "casting_no": row.casting_no,
            "machined_part_no": row.machined_part_no,
            "qty": qty,
            "value": value,
            "priced_qty": ok,
            "unpriced_qty": missing,
            "list_qty": list_qty,
            "fallback_qty": fallback_qty,
        })
    return priced


def _priced_defects(filters, reason_ids=None):
    """The same scrap split by reject reason, priced the same way."""
    group_cols = [
        ScrapEntry.entry_date, ScrapEntry.customer_id, ScrapEntry.product_id,
        ScrapEntry.casting_no, ScrapEntry.machined_part_no,
        ScrapEntryDefect.defect_id,
    ]
    query = _apply_filters(
        db.session.query(
            *group_cols,
            func.coalesce(func.sum(ScrapEntryDefect.qty), 0).label("qty"),
        )
        .select_from(ScrapEntryDefect)
        .join(ScrapEntry, ScrapEntryDefect.entry_id == ScrapEntry.id),
        filters,
    )
    if reason_ids is not None:
        query = query.filter(ScrapEntryDefect.defect_id.in_(list(reason_ids) or [-1]))

    rows = query.group_by(*group_cols).all()
    lookup = price_lookup((r.entry_date, r.customer_id, r.product_id) for r in rows)

    priced = []
    for row in rows:
        qty = int(row.qty or 0)
        if not qty:
            continue
        value, ok, missing, list_qty, fallback_qty = lookup.value(
            row.entry_date, row.customer_id, row.product_id, qty
        )
        priced.append({
            "entry_date": row.entry_date,
            "product_id": row.product_id,
            "casting_no": row.casting_no,
            "machined_part_no": row.machined_part_no,
            "defect_id": row.defect_id,
            "qty": qty,
            "value": value,
            "priced_qty": ok,
            "unpriced_qty": missing,
            "list_qty": list_qty,
            "fallback_qty": fallback_qty,
        })
    return priced


def _available_years():
    query = db.session.query(func.extract("year", ScrapEntry.entry_date)).distinct()
    customer_ids = _division_customer_ids()
    if customer_ids is not None:
        query = query.filter(ScrapEntry.customer_id.in_(customer_ids))
    rows = query.order_by(func.extract("year", ScrapEntry.entry_date).desc()).all()
    return [int(r[0]) for r in rows if r[0] is not None]


# ─────────────────────────────────────────────
#  HUB  (choose HDA or HDC)
# ─────────────────────────────────────────────

@scrap_bp.route("/")
@login_required
@require_perm("scrap", "view")
def hub():
    today = date.today()
    year_start = date(today.year, 1, 1)

    def ytd_scrap(code):
        division = Division.query.filter(func.lower(Division.code) == code).first()
        if division is None:
            return None, 0
        customer_ids = db.session.query(Customer.id).filter(Customer.division_id == division.id)
        qty = (
            db.session.query(func.coalesce(func.sum(ScrapEntry.qty_scrap), 0))
            .filter(
                ScrapEntry.entry_date >= year_start,
                ScrapEntry.entry_date <= today,
                ScrapEntry.customer_id.in_(customer_ids),
            )
            .scalar()
        )
        return division, int(qty or 0)

    tiles = []
    for code, name, description, icon, color in (
        ("hda", "HDA Scrap", "Schaffler South Africa — HDA's single customer.", "bi-building", "#7c3aed"),
        ("hdc", "HDC Scrap", "External reject reports and in-house scrap across every HDC customer.", "bi-diagram-3", "#2563eb"),
    ):
        division, ytd = ytd_scrap(code)
        tiles.append({
            "code": code, "name": name, "description": description,
            "icon": icon, "color": color, "ytd_scrap": ytd, "available": division is not None,
        })

    return render_template("scrap/hub.html", tiles=tiles, today=today)


# ─────────────────────────────────────────────
#  DASHBOARD
# ─────────────────────────────────────────────

@scrap_bp.route("/<division>/dashboard")
@login_required
@require_perm("scrap", "view")
def dashboard():
    today = date.today()
    customer_ids = _division_customer_ids()

    def totals_for(start, end):
        query = (
            db.session.query(
                func.coalesce(func.sum(ScrapEntry.qty_scrap), 0),
                func.coalesce(func.sum(ScrapEntry.qty_machined), 0),
                _PACKED_SUM,
                _EXTERNAL_SCRAP,
                _INTERNAL_SCRAP,
                func.count(ScrapEntry.id),
            )
            .filter(ScrapEntry.entry_date >= start, ScrapEntry.entry_date <= end)
        )
        if customer_ids is not None:
            query = query.filter(ScrapEntry.customer_id.in_(customer_ids))
        row = query.one()
        scrap_qty, machined, packed, external, internal, entries = (int(v or 0) for v in row)
        return {
            "scrap": scrap_qty,
            "machined": machined,
            "packed": packed,
            "external": external,
            "internal": internal,
            "entries": entries,
            # machined (external total) + packed (internal good units) +
            # internal's own scrap added back on — see ScrapEntry.total_qty.
            "pct": _pct(scrap_qty, machined + packed + internal),
        }

    month_start = today.replace(day=1)
    ytd = totals_for(date(today.year, 1, 1), date(today.year, 12, 31))
    mtd = totals_for(month_start, today)

    # Top reject reasons this year
    top_defects_q = (
        db.session.query(ScrapDefect.name, func.sum(ScrapEntryDefect.qty).label("qty"))
        .join(ScrapEntryDefect, ScrapEntryDefect.defect_id == ScrapDefect.id)
        .join(ScrapEntry, ScrapEntryDefect.entry_id == ScrapEntry.id)
        .filter(func.extract("year", ScrapEntry.entry_date) == today.year)
    )
    if customer_ids is not None:
        top_defects_q = top_defects_q.filter(ScrapEntry.customer_id.in_(customer_ids))
    top_defects = (
        top_defects_q.group_by(ScrapDefect.name)
        .order_by(func.sum(ScrapEntryDefect.qty).desc())
        .limit(8)
        .all()
    )
    defect_max = max((int(row.qty or 0) for row in top_defects), default=0)

    # Scrap by month for the current year — external vs internal
    month_rows_q = (
        db.session.query(
            func.extract("month", ScrapEntry.entry_date).label("month"),
            _EXTERNAL_SCRAP.label("external"),
            _INTERNAL_SCRAP.label("internal"),
        )
        .filter(func.extract("year", ScrapEntry.entry_date) == today.year)
    )
    if customer_ids is not None:
        month_rows_q = month_rows_q.filter(ScrapEntry.customer_id.in_(customer_ids))
    month_rows = (
        month_rows_q.group_by(func.extract("month", ScrapEntry.entry_date))
        .order_by(func.extract("month", ScrapEntry.entry_date))
        .all()
    )
    by_month = {int(r.month): (int(r.external or 0), int(r.internal or 0)) for r in month_rows}
    month_series = [
        {
            "label": month_abbr[m],
            "external": by_month.get(m, (0, 0))[0],
            "internal": by_month.get(m, (0, 0))[1],
        }
        for m in range(1, 13)
    ]
    month_max = max((m["external"] + m["internal"] for m in month_series), default=0)

    recent_imports_q = ScrapImportBatch.query
    recent_entries_q = ScrapEntry.query
    unmatched_q = ScrapEntry.query.filter(
        ScrapEntry.product_id.is_(None), ScrapEntry.source == SOURCE_EXTERNAL
    )
    if customer_ids is not None:
        recent_imports_q = recent_imports_q.filter(ScrapImportBatch.customer_id.in_(customer_ids))
        recent_entries_q = recent_entries_q.filter(ScrapEntry.customer_id.in_(customer_ids))
        unmatched_q = unmatched_q.filter(ScrapEntry.customer_id.in_(customer_ids))

    recent_imports = (
        recent_imports_q
        .order_by(ScrapImportBatch.imported_at.desc())
        .limit(5)
        .all()
    )
    recent_entries = (
        recent_entries_q
        .order_by(ScrapEntry.entry_date.desc(), ScrapEntry.id.desc())
        .limit(10)
        .all()
    )
    unmatched_count = unmatched_q.count()

    return render_template(
        "scrap/dashboard.html",
        today=today,
        ytd=ytd,
        mtd=mtd,
        top_defects=top_defects,
        defect_max=defect_max,
        month_series=month_series,
        month_max=month_max,
        recent_imports=recent_imports,
        recent_entries=recent_entries,
        unmatched_count=unmatched_count,
    )


# ─────────────────────────────────────────────
#  EXTERNAL — IMPORT
# ─────────────────────────────────────────────

@scrap_bp.route("/<division>/import", methods=["GET", "POST"])
@login_required
@require_perm("scrap", "import")
def import_report():
    form = ScrapImportForm()
    locked_customer = _locked_customer() if g.division == "hda" else None
    if locked_customer:
        form.customer_id.choices = [(locked_customer.id, locked_customer.name)]
    else:
        form.customer_id.choices = _customer_choices(include_blank=True, blank_label="— Select customer —")

    if request.method == "GET" and locked_customer:
        form.customer_id.data = locked_customer.id

    if form.validate_on_submit():
        if not form.customer_id.data:
            flash("Choose the customer whose report this is.", "warning")
            return redirect(url_for("scrap.import_report"))

        try:
            result = import_external_report(
                form.file.data,
                customer_id=form.customer_id.data,
                user_id=current_user.id,
                default_date=form.default_date.data,
            )
        except ValueError as exc:
            db.session.rollback()
            flash(str(exc), "danger")
            return redirect(url_for("scrap.import_report"))
        except Exception as exc:                      # noqa: BLE001 — surface, don't crash
            db.session.rollback()
            flash(f"Import failed: {exc}", "danger")
            return redirect(url_for("scrap.import_report"))

        summary = (
            f"{result.imported} row(s) imported, "
            f"{result.duplicates} duplicate(s) held for review, "
            f"{result.skipped} row(s) skipped."
        )
        flash(summary, "success" if result.imported else "info")

        if result.duplicates:
            flash(
                f"{result.duplicates} row(s) repeat one already loaded and were not "
                "imported. Review each one below and allow or reject it.",
                "warning",
            )
        if result.unmatched:
            flash(
                f"{result.unmatched} row(s) imported without a product link — "
                "the part number matched no product. Link them from the batch screen.",
                "warning",
            )
        if result.unknown_columns:
            flash(
                "Columns not recognised and ignored: " + ", ".join(result.unknown_columns),
                "info",
            )

        if result.batch:
            # review=1 opens the duplicate decision dialog straight away
            return redirect(url_for(
                "scrap.import_batch",
                batch_id=result.batch.id,
                review=1 if result.duplicates else None,
            ))
        return redirect(url_for("scrap.import_report"))

    recent_imports_q = ScrapImportBatch.query
    customer_ids = _division_customer_ids()
    if customer_ids is not None:
        recent_imports_q = recent_imports_q.filter(ScrapImportBatch.customer_id.in_(customer_ids))
    recent_imports = (
        recent_imports_q
        .order_by(ScrapImportBatch.imported_at.desc())
        .limit(10)
        .all()
    )

    return render_template(
        "scrap/import.html",
        form=form,
        defects=active_defects(SCOPE_EXTERNAL),
        recent_imports=recent_imports,
        locked_customer=locked_customer,
    )


@scrap_bp.route("/<division>/import/template.csv")
@login_required
@require_perm("scrap", "view")
def import_template():
    """The blank import sheet — core columns plus one column per reject reason."""
    csv_text = build_template_csv()
    return Response(
        csv_text,
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=external_scrap_import_template.csv"},
    )


@scrap_bp.route("/<division>/imports")
@login_required
@require_perm("scrap", "view")
def import_batches():
    batches = (
        ScrapImportBatch.query
        .order_by(ScrapImportBatch.imported_at.desc())
        .all()
    )
    return render_template("scrap/batches.html", batches=batches, delete_form=DeleteForm())


@scrap_bp.route("/<division>/imports/<int:batch_id>")
@login_required
@require_perm("scrap", "view")
def import_batch(batch_id):
    batch = ScrapImportBatch.query.get_or_404(batch_id)
    entries = (
        ScrapEntry.query
        .filter_by(batch_id=batch.id)
        .order_by(ScrapEntry.source_row)
        .all()
    )

    duplicates = (
        ScrapPendingDuplicate.query
        .filter_by(batch_id=batch.id)
        .order_by(ScrapPendingDuplicate.source_row)
        .all()
    )
    pending = [d for d in duplicates if d.is_pending]

    return render_template(
        "scrap/batch_detail.html",
        batch=batch,
        entries=entries,
        defects=active_defects(SCOPE_EXTERNAL),
        duplicates=duplicates,
        pending_duplicates=pending,
        defects_by_id={d.id: d for d in ScrapDefect.query.all()},
        # Opened by the import redirect and after each decision, so the run of
        # duplicates is worked through in one go rather than hunted for.
        open_review=bool(pending) and request.args.get("review") == "1",
        delete_form=DeleteForm(),
        product_choices=_product_choices(),
    )


# ── Duplicate decisions ──────────────────────────────────────────────────────

def _back_to_review(batch_id):
    return redirect(url_for("scrap.import_batch", batch_id=batch_id, review=1))


@scrap_bp.route("/<division>/imports/<int:batch_id>/duplicates/<int:dup_id>", methods=["POST"])
@login_required
@require_perm("scrap", "import")
def resolve_duplicate(batch_id, dup_id):
    """Allow or reject one held duplicate row."""
    form = DeleteForm()
    if not form.validate_on_submit():
        flash("Could not verify that request. Please try again.", "danger")
        return _back_to_review(batch_id)

    pending = ScrapPendingDuplicate.query.filter_by(
        id=dup_id, batch_id=batch_id
    ).first_or_404()

    if not pending.is_pending:
        flash(f"Row {pending.source_row} was already {pending.status}.", "info")
        return _back_to_review(batch_id)

    decision = (request.form.get("decision") or "").strip().lower()

    if decision == "allow":
        entry = allow_duplicate(pending, user_id=current_user.id)
        db.session.commit()
        flash(
            f"Row {pending.source_row} imported — "
            f"{entry.qty_scrap} scrap on {entry.entry_date.strftime('%d %b %Y')}"
            + (f", linked to {entry.product.name}." if entry.product
               else ", no product matched."),
            "success",
        )
    elif decision == "reject":
        reject_duplicate(pending, user_id=current_user.id)
        db.session.commit()
        flash(f"Row {pending.source_row} rejected — it was not imported.", "info")
    else:
        flash("Choose allow or reject for that row.", "warning")

    return _back_to_review(batch_id)


@scrap_bp.route("/<division>/imports/<int:batch_id>/duplicates/all", methods=["POST"])
@login_required
@require_perm("scrap", "import")
def resolve_duplicates_bulk(batch_id):
    """Apply one decision to every duplicate still awaiting one."""
    form = DeleteForm()
    if not form.validate_on_submit():
        flash("Could not verify that request. Please try again.", "danger")
        return _back_to_review(batch_id)

    ScrapImportBatch.query.get_or_404(batch_id)
    pending_rows = (
        ScrapPendingDuplicate.query
        .filter_by(batch_id=batch_id, status=DUP_PENDING)
        .order_by(ScrapPendingDuplicate.source_row)
        .all()
    )
    if not pending_rows:
        flash("No duplicates are waiting on a decision.", "info")
        return redirect(url_for("scrap.import_batch", batch_id=batch_id))

    decision = (request.form.get("decision") or "").strip().lower()

    if decision == "allow":
        unmatched = 0
        for pending in pending_rows:
            entry = allow_duplicate(pending, user_id=current_user.id)
            if entry is not None and entry.product_id is None:
                unmatched += 1
        db.session.commit()
        flash(f"{len(pending_rows)} duplicate row(s) imported.", "success")
        if unmatched:
            flash(
                f"{unmatched} of them matched no product — link them below.",
                "warning",
            )
    elif decision == "reject":
        for pending in pending_rows:
            reject_duplicate(pending, user_id=current_user.id)
        db.session.commit()
        flash(f"{len(pending_rows)} duplicate row(s) rejected.", "info")
    else:
        flash("Choose allow or reject.", "warning")
        return _back_to_review(batch_id)

    return redirect(url_for("scrap.import_batch", batch_id=batch_id))


@scrap_bp.route("/<division>/imports/<int:batch_id>/delete", methods=["POST"])
@login_required
@require_perm("scrap", "admin")
def delete_batch(batch_id):
    form = DeleteForm()
    if not form.validate_on_submit():
        flash("Could not verify that request. Please try again.", "danger")
        return redirect(url_for("scrap.import_batches"))

    batch = ScrapImportBatch.query.get_or_404(batch_id)
    count = len(batch.entries)
    db.session.delete(batch)          # cascades to its entries and their defects
    db.session.commit()
    flash(f"Import reversed — {count} entry(ies) removed.", "warning")
    return redirect(url_for("scrap.import_batches"))


# ─────────────────────────────────────────────
#  INTERNAL — MANUAL CAPTURE
# ─────────────────────────────────────────────

@scrap_bp.route("/<division>/internal/new", methods=["GET", "POST"])
@scrap_bp.route("/<division>/internal/<int:entry_id>/edit", methods=["GET", "POST"])
@login_required
@require_perm("scrap", "capture")
def internal_entry(entry_id=None):
    entry = ScrapEntry.query.get_or_404(entry_id) if entry_id else None
    if entry and entry.source != SOURCE_INTERNAL:
        flash("Imported entries are edited by re-importing the report.", "warning")
        return redirect(url_for("scrap.entries"))

    form = InternalScrapForm(obj=entry)
    locked_customer = _locked_customer() if g.division == "hda" else None
    form.customer_id.choices = (
        [(locked_customer.id, locked_customer.name)] if locked_customer else _customer_choices()
    )
    form.product_id.choices = _product_choices()

    defects = active_defects(SCOPE_INTERNAL)

    if form.validate_on_submit():
        # Defect quantities are rendered from the catalogue, so read them off the raw form
        defect_qtys = {}
        for defect in defects:
            raw = (request.form.get(f"defect_{defect.id}") or "").strip()
            if not raw:
                continue
            try:
                qty = int(float(raw))
            except ValueError:
                continue
            if qty > 0:
                defect_qtys[defect.id] = qty

        if entry is None:
            entry = ScrapEntry(source=SOURCE_INTERNAL, created_by=current_user.id)
            db.session.add(entry)

        entry.entry_date   = form.entry_date.data
        entry.customer_id  = locked_customer.id if locked_customer else (form.customer_id.data or None)
        entry.product_id   = form.product_id.data or None
        entry.casting_no   = (form.casting_no.data or "").strip() or None
        entry.batch_no     = (form.batch_no.data or "").strip() or None
        entry.qty_packed   = form.qty_packed.data or 0
        entry.qty_scrap    = form.qty_scrap.data or 0
        entry.notes        = (form.notes.data or "").strip() or None

        # Machined part number mirrors the product code when one is linked
        if entry.product_id and not entry.machined_part_no:
            product = Product.query.get(entry.product_id)
            entry.machined_part_no = product.product_code if product else None

        db.session.flush()

        ScrapEntryDefect.query.filter_by(entry_id=entry.id).delete()
        for defect_id, qty in defect_qtys.items():
            db.session.add(ScrapEntryDefect(entry_id=entry.id, defect_id=defect_id, qty=qty))

        db.session.commit()

        breakdown_total = sum(defect_qtys.values())
        if breakdown_total and breakdown_total != entry.qty_scrap:
            flash(
                f"Saved — note the reason breakdown ({breakdown_total}) does not match "
                f"the scrap quantity ({entry.qty_scrap}).",
                "warning",
            )
        else:
            flash("Internal scrap entry saved.", "success")
        return redirect(url_for("scrap.entries", source=SOURCE_INTERNAL))

    if request.method == "GET":
        if entry:
            form.customer_id.data = entry.customer_id or 0
            form.product_id.data = entry.product_id or 0
        else:
            form.entry_date.data = date.today()
            if locked_customer:
                form.customer_id.data = locked_customer.id

    existing_qtys = {line.defect_id: line.qty for line in entry.defect_lines} if entry else {}

    balances = _packed_balance()
    if entry and entry.product_id:
        # The entry being edited is part of its own packed total, so back it
        # out — the hint should read as it stood before this entry, not
        # double-count the very row someone is looking at.
        bucket = balances.get(entry.product_id)
        if bucket:
            bucket = dict(bucket)
            bucket["packed"] -= entry.qty_packed or 0
            bucket["balance"] -= entry.qty_packed or 0
            balances[entry.product_id] = bucket

    return render_template(
        "scrap/internal_form.html",
        form=form,
        entry=entry,
        defects=defects,
        existing_qtys=existing_qtys,
        products_by_customer=json.dumps(_products_by_customer()),
        product_casting_numbers=json.dumps(_product_casting_numbers()),
        product_balances=json.dumps({str(k): v["balance"] for k, v in balances.items()}),
        product_details=json.dumps(_product_details()),
        locked_customer=locked_customer,
    )


# ─────────────────────────────────────────────
#  DISPATCH  (packed stock leaving the site)
# ─────────────────────────────────────────────

def _read_dispatch_lines(formdata):
    """
    [(product_id, qty), ...] off line_product_<n> / line_qty_<n> pairs — the
    rows added on the batch dispatch screen. Blank or zero-qty lines (an
    unused row left on the form) are dropped rather than erroring.
    """
    lines = []
    for key in formdata:
        if not key.startswith("line_product_"):
            continue
        idx = key[len("line_product_"):]
        product_id = formdata.get(f"line_product_{idx}", type=int) or 0
        qty = formdata.get(f"line_qty_{idx}", type=int) or 0
        if product_id and qty > 0:
            lines.append((product_id, qty))
    return lines


def _read_cage_lines(formdata):
    """
    [{"product_id", "qty", "weight", "trenstar_no", "head_numbers",
      "drawing_level", "blue_card_confirmed", "black_bag_confirmed",
      "cage_packed_half_confirmed", "weighbridge_printed_confirmed"}, ...]
    off line_product_<n> / line_qty_<n> / line_weight_<n> / line_trenstar_<n>
    / line_head_<n> / line_drawing_<n> / line_<check>_<n> — the cage rows on
    HDA's dispatch screen, in submission (DOM) order. A row needs a product
    and a quantity to count; everything else is optional. A checkbox counts
    as ticked if its key is present at all (unticked checkboxes simply don't
    submit). cage_no isn't read here — the caller assigns it from order.
    """
    lines = []
    for key in formdata:
        if not key.startswith("line_product_"):
            continue
        idx = key[len("line_product_"):]
        product_id = formdata.get(f"line_product_{idx}", type=int) or 0
        qty = formdata.get(f"line_qty_{idx}", type=int) or 0
        if not product_id or qty <= 0:
            continue

        weight_raw = (formdata.get(f"line_weight_{idx}") or "").strip()
        try:
            weight = Decimal(weight_raw) if weight_raw else None
        except InvalidOperation:
            weight = None

        lines.append({
            "product_id": product_id,
            "qty": qty,
            "weight": weight,
            "trenstar_no": (formdata.get(f"line_trenstar_{idx}") or "").strip() or None,
            "head_numbers": (formdata.get(f"line_head_{idx}") or "").strip() or None,
            "drawing_level": (formdata.get(f"line_drawing_{idx}") or "").strip() or None,
            "blue_card_confirmed": f"line_blue_card_{idx}" in formdata,
            "black_bag_confirmed": f"line_black_bag_{idx}" in formdata,
            "cage_packed_half_confirmed": f"line_packed_half_{idx}" in formdata,
            "weighbridge_printed_confirmed": f"line_weighbridge_{idx}" in formdata,
        })
    return lines


def _flash_balance_warnings(product_ids):
    """Flag any product whose packed balance has gone negative after saving."""
    ids = list(dict.fromkeys(pid for pid in product_ids if pid))
    if not ids:
        return
    balances = _packed_balance()
    products = {p.id: p for p in Product.query.filter(Product.id.in_(ids)).all()}
    for pid in ids:
        balance = balances.get(pid, {}).get("balance", 0)
        if balance < 0:
            name = products[pid].name if pid in products else f"product #{pid}"
            flash(
                f"Note: {name} packed balance is now {balance:,}. "
                "Check the packed quantity was captured for everything on that truck.",
                "warning",
            )


def _save_cage_lines(batch, lines):
    """Create one ScrapDispatch row per cage line, numbered by order, mirrored
    onto the batch's own customer/date so packed-balance and report queries
    (which filter directly on ScrapDispatch) keep working unchanged."""
    created = []
    for cage_no, line in enumerate(lines, start=1):
        row = ScrapDispatch(
            dispatch_date=batch.dispatch_date,
            customer_id=batch.customer_id,
            product_id=line["product_id"],
            qty_dispatched=line["qty"],
            batch_id=batch.id,
            cage_no=cage_no,
            trenstar_no=line["trenstar_no"],
            weight=line["weight"],
            head_numbers=line["head_numbers"],
            drawing_level=line["drawing_level"],
            blue_card_confirmed=line["blue_card_confirmed"],
            black_bag_confirmed=line["black_bag_confirmed"],
            cage_packed_half_confirmed=line["cage_packed_half_confirmed"],
            weighbridge_printed_confirmed=line["weighbridge_printed_confirmed"],
            created_by=current_user.id,
        )
        db.session.add(row)
        created.append(row)
    return created


@scrap_bp.route("/<division>/dispatch/new", methods=["GET", "POST"])
@scrap_bp.route("/<division>/dispatch/<int:dispatch_id>/edit", methods=["GET", "POST"])
@login_required
@require_perm("scrap", "capture")
def dispatch_entry(dispatch_id=None):
    dispatch = ScrapDispatch.query.get_or_404(dispatch_id) if dispatch_id else None

    # HDA's cage rows are edited as a whole batch, not one row at a time.
    if dispatch is not None and dispatch.batch_id and g.division == "hda":
        return redirect(url_for("scrap.dispatch_batch_edit", batch_id=dispatch.batch_id))

    form = ScrapDispatchForm(obj=dispatch)
    locked_customer = _locked_customer() if g.division == "hda" else None
    form.customer_id.choices = (
        [(locked_customer.id, locked_customer.name)] if locked_customer else _customer_choices()
    )
    form.product_id.choices = _product_choices()
    # Always set — an unset SelectField.choices raises on validate() even
    # when nothing was submitted for it, so HDC's screens need this too.
    form.dispatcher_id.choices = _hda_dispatcher_choices() if g.division == "hda" else []

    is_hda_new = g.division == "hda" and dispatch is None

    if form.validate_on_submit():
        if dispatch is not None:
            # Editing one existing line — product and quantity are required here
            # even though the form itself allows them blank for the batch case.
            if not form.product_id.data:
                form.product_id.errors.append("Choose the part that was dispatched.")
            if not form.qty_dispatched.data:
                form.qty_dispatched.errors.append("Dispatched quantity must be at least 1.")

            if not form.product_id.errors and not form.qty_dispatched.errors:
                dispatch.dispatch_date  = form.dispatch_date.data
                dispatch.customer_id    = locked_customer.id if locked_customer else (form.customer_id.data or None)
                dispatch.product_id     = form.product_id.data
                dispatch.qty_dispatched = form.qty_dispatched.data
                dispatch.notes          = (form.notes.data or "").strip() or None
                db.session.commit()

                # Over-dispatch is allowed — the truck's quantity is ground
                # truth, packed-entry capture is more likely to be the side
                # running late — but flag it so it doesn't slip past unnoticed.
                flash("Dispatch saved.", "success")
                _flash_balance_warnings([dispatch.product_id])
                return redirect(url_for("scrap.dispatches"))
        elif is_hda_new:
            # New HDA dispatch — one truck, logged cage by cage.
            lines = _read_cage_lines(request.form)
            if not lines:
                flash("Add at least one cage with a product and quantity.", "danger")
            else:
                batch = ScrapDispatchBatch(
                    dispatch_date=form.dispatch_date.data,
                    customer_id=locked_customer.id if locked_customer else None,
                    invoice_no=(form.invoice_no.data or "").strip() or None,
                    dispatcher_id=form.dispatcher_id.data or None,
                    total_black_bags=form.total_black_bags.data or 0,
                    notes=(form.notes.data or "").strip() or None,
                    created_by=current_user.id,
                )
                db.session.add(batch)
                db.session.flush()  # need batch.id for the cage lines

                created = _save_cage_lines(batch, lines)
                db.session.commit()

                flash(
                    f"Dispatch saved — {batch.total_cages} cage{'s' if batch.total_cages != 1 else ''}, "
                    f"{batch.total_qty:,} unit(s), {batch.total_weight:,.1f} kg.",
                    "success",
                )
                _flash_balance_warnings([r.product_id for r in created])
                return redirect(url_for("scrap.dispatches"))
        else:
            # New HDC dispatch — one or more product lines off the same truck.
            lines = _read_dispatch_lines(request.form)
            if not lines:
                flash("Add at least one product and quantity.", "danger")
            else:
                created = []
                for product_id, qty in lines:
                    row = ScrapDispatch(
                        dispatch_date=form.dispatch_date.data,
                        customer_id=locked_customer.id if locked_customer else (form.customer_id.data or None),
                        product_id=product_id,
                        qty_dispatched=qty,
                        notes=(form.notes.data or "").strip() or None,
                        created_by=current_user.id,
                    )
                    db.session.add(row)
                    created.append(row)
                db.session.commit()

                flash(
                    f"Saved {len(created)} dispatch line{'s' if len(created) != 1 else ''}.",
                    "success",
                )
                _flash_balance_warnings([r.product_id for r in created])
                return redirect(url_for("scrap.dispatches"))

    if request.method == "GET":
        if dispatch:
            form.customer_id.data = dispatch.customer_id or 0
            form.product_id.data = dispatch.product_id or 0
        else:
            form.dispatch_date.data = date.today()
            if locked_customer:
                form.customer_id.data = locked_customer.id
            if is_hda_new:
                form.dispatcher_id.data = _default_dispatcher_id()

    balances = _packed_balance()
    if dispatch and dispatch.product_id:
        # The dispatch being edited is already netted into its own product's
        # balance, so back its quantity out — the hint should read as it
        # stood before this dispatch, not double-count the row being edited.
        bucket = balances.get(dispatch.product_id)
        if bucket:
            bucket = dict(bucket)
            bucket["dispatched"] -= dispatch.qty_dispatched or 0
            bucket["balance"] += dispatch.qty_dispatched or 0
            balances[dispatch.product_id] = bucket

    product_options = [{"value": "0", "text": "— Select a product —"}] + [
        {"value": str(pid), "text": label} for pid, label in _product_choices() if pid
    ]

    return render_template(
        "scrap/dispatch_form.html",
        form=form,
        dispatch=dispatch,
        batch=None,
        products_by_customer=json.dumps(_products_by_customer()),
        product_balances=json.dumps({str(k): v["balance"] for k, v in balances.items()}),
        product_drawing_levels=json.dumps(_product_drawing_levels()),
        product_choices=json.dumps(product_options),
        initial_cage_lines=json.dumps([]),
        locked_customer=locked_customer,
    )


@scrap_bp.route("/<division>/dispatch/batch/<int:batch_id>/edit", methods=["GET", "POST"])
@login_required
@require_perm("scrap", "capture")
def dispatch_batch_edit(batch_id):
    """Edit an HDA cage dispatch as a whole — every cage line is replaced on
    save, same delete-then-recreate pattern internal_entry uses for its
    reject-reason breakdown."""
    batch = ScrapDispatchBatch.query.get_or_404(batch_id)

    form = ScrapDispatchForm(obj=batch)
    locked_customer = _locked_customer() if g.division == "hda" else None
    form.customer_id.choices = (
        [(locked_customer.id, locked_customer.name)] if locked_customer else _customer_choices()
    )
    form.product_id.choices = _product_choices()
    form.dispatcher_id.choices = _hda_dispatcher_choices()

    if form.validate_on_submit():
        lines = _read_cage_lines(request.form)
        if not lines:
            flash("Add at least one cage with a product and quantity.", "danger")
        else:
            batch.dispatch_date = form.dispatch_date.data
            batch.customer_id   = locked_customer.id if locked_customer else (form.customer_id.data or None)
            batch.invoice_no    = (form.invoice_no.data or "").strip() or None
            batch.dispatcher_id = form.dispatcher_id.data or None
            batch.total_black_bags = form.total_black_bags.data or 0
            batch.notes = (form.notes.data or "").strip() or None

            ScrapDispatch.query.filter_by(batch_id=batch.id).delete()
            db.session.flush()

            created = _save_cage_lines(batch, lines)
            db.session.commit()

            flash(
                f"Dispatch updated — {batch.total_cages} cage{'s' if batch.total_cages != 1 else ''}, "
                f"{batch.total_qty:,} unit(s), {batch.total_weight:,.1f} kg.",
                "success",
            )
            _flash_balance_warnings([r.product_id for r in created])
            return redirect(url_for("scrap.dispatches"))

    balances = _packed_balance()
    # The batch being edited is already netted into these balances, so back
    # each of its lines out — the hint should read as it stood before this
    # dispatch, not double-count the rows being edited.
    for line in batch.lines:
        if not line.product_id:
            continue
        bucket = balances.get(line.product_id)
        if bucket:
            bucket = dict(bucket)
            bucket["dispatched"] -= line.qty_dispatched or 0
            bucket["balance"] += line.qty_dispatched or 0
            balances[line.product_id] = bucket

    product_options = [{"value": "0", "text": "— Select a product —"}] + [
        {"value": str(pid), "text": label} for pid, label in _product_choices() if pid
    ]

    initial_cage_lines = [
        {
            "product_id": line.product_id or 0,
            "qty": line.qty_dispatched or 0,
            "weight": str(line.weight) if line.weight is not None else "",
            "trenstar_no": line.trenstar_no or "",
            "head_numbers": line.head_numbers or "",
            "drawing_level": line.drawing_level or "",
            "blue_card_confirmed": bool(line.blue_card_confirmed),
            "black_bag_confirmed": bool(line.black_bag_confirmed),
            "cage_packed_half_confirmed": bool(line.cage_packed_half_confirmed),
            "weighbridge_printed_confirmed": bool(line.weighbridge_printed_confirmed),
        }
        for line in batch.lines
    ]

    return render_template(
        "scrap/dispatch_form.html",
        form=form,
        dispatch=None,
        batch=batch,
        products_by_customer=json.dumps(_products_by_customer()),
        product_balances=json.dumps({str(k): v["balance"] for k, v in balances.items()}),
        product_drawing_levels=json.dumps(_product_drawing_levels()),
        product_choices=json.dumps(product_options),
        initial_cage_lines=json.dumps(initial_cage_lines),
        locked_customer=locked_customer,
    )


@scrap_bp.route("/<division>/dispatch")
@login_required
@require_perm("scrap", "view")
def dispatches():
    filters = _read_filters()

    query = _apply_dispatch_filters(ScrapDispatch.query, filters)
    rows = (
        query.order_by(ScrapDispatch.dispatch_date.desc(), ScrapDispatch.id.desc())
        .limit(1000)
        .all()
    )

    totals = {
        "dispatches": len(rows),
        "qty": sum(r.qty_dispatched or 0 for r in rows),
    }

    return render_template(
        "scrap/dispatches.html",
        rows=rows,
        totals=totals,
        filters=filters,
        customers=_division_customers(),
        products=Product.query.order_by(Product.name).all(),
        delete_form=DeleteForm(),
    )


@scrap_bp.route("/<division>/dispatch/<int:dispatch_id>/delete", methods=["POST"])
@login_required
@require_perm("scrap", "admin")
def delete_dispatch(dispatch_id):
    form = DeleteForm()
    if not form.validate_on_submit():
        flash("Could not verify that request. Please try again.", "danger")
        return redirect(url_for("scrap.dispatches"))

    dispatch = ScrapDispatch.query.get_or_404(dispatch_id)
    db.session.delete(dispatch)
    db.session.commit()
    flash("Dispatch deleted.", "warning")
    return redirect(request.referrer or url_for("scrap.dispatches"))


# ─────────────────────────────────────────────
#  ENTRIES  (browse both sources)
# ─────────────────────────────────────────────

@scrap_bp.route("/<division>/entries")
@login_required
@require_perm("scrap", "view")
def entries():
    filters = _read_filters()
    unmatched_only = request.args.get("unmatched") == "1"

    query = _apply_filters(ScrapEntry.query, filters)
    if unmatched_only:
        query = query.filter(ScrapEntry.product_id.is_(None))

    rows = (
        query.order_by(ScrapEntry.entry_date.desc(), ScrapEntry.id.desc())
        .limit(1000)
        .all()
    )

    totals = {
        "entries": len(rows),
        "scrap": sum(r.qty_scrap or 0 for r in rows),
        # entered_qty is qty_machined (external) or qty_packed (internal) —
        # each row's own figure, matching the "Machined / Packed" column.
        "machined": sum(r.entered_qty or 0 for r in rows),
    }
    # total_qty adds internal's scrap back on for the denominator (see
    # ScrapEntry.total_qty), so a mix of sources still nets a correct %.
    totals["pct"] = _pct(totals["scrap"], sum(r.total_qty for r in rows))

    return render_template(
        "scrap/entries.html",
        rows=rows,
        totals=totals,
        filters=filters,
        unmatched_only=unmatched_only,
        customers=_division_customers(),
        products=Product.query.order_by(Product.name).all(),
        source_choices=SOURCE_CHOICES,
        delete_form=DeleteForm(),
        product_choices=_product_choices(),
    )


@scrap_bp.route("/<division>/entries/<int:entry_id>/delete", methods=["POST"])
@login_required
@require_perm("scrap", "admin")
def delete_entry(entry_id):
    form = DeleteForm()
    if not form.validate_on_submit():
        flash("Could not verify that request. Please try again.", "danger")
        return redirect(url_for("scrap.entries"))

    entry = ScrapEntry.query.get_or_404(entry_id)
    db.session.delete(entry)
    db.session.commit()
    flash("Scrap entry deleted.", "warning")
    return redirect(request.referrer or url_for("scrap.entries"))


@scrap_bp.route("/<division>/entries/rematch", methods=["POST"])
@login_required
@require_perm("scrap", "capture")
def rematch_products():
    """
    Re-run product matching over every entry that has no product linked.

    Rows import even when their part number matches nothing, so this is what
    you run after loading the products — it links the backlog in one pass
    instead of picking through it by hand.
    """
    form = DeleteForm()
    if not form.validate_on_submit():
        flash("Could not verify that request. Please try again.", "danger")
        return redirect(url_for("scrap.entries"))

    unlinked_q = ScrapEntry.query.filter(ScrapEntry.product_id.is_(None))
    customer_ids = _division_customer_ids()
    if customer_ids is not None:
        unlinked_q = unlinked_q.filter(ScrapEntry.customer_id.in_(customer_ids))
    unlinked = unlinked_q.all()
    if not unlinked:
        flash("Every scrap entry is already linked to a product.", "info")
        return redirect(request.referrer or url_for("scrap.entries"))

    # One index per customer — matching prefers that customer's own products
    indexes = {}
    linked = 0
    for entry in unlinked:
        if entry.customer_id not in indexes:
            indexes[entry.customer_id] = build_product_index(entry.customer_id)

        product = match_product(
            indexes[entry.customer_id], entry.casting_no, entry.machined_part_no
        )
        if product:
            entry.product_id = product.id
            linked += 1

    db.session.commit()

    if linked:
        flash(
            f"Linked {linked} of {len(unlinked)} unlinked entry(ies) to a product.",
            "success",
        )
    else:
        flash(
            f"No matches found for the {len(unlinked)} unlinked entry(ies). Check that "
            "the casting or machined part numbers exist as a product code, supplier "
            "code or product name.",
            "warning",
        )
    return redirect(request.referrer or url_for("scrap.entries"))


@scrap_bp.route("/<division>/entries/<int:entry_id>/link", methods=["POST"])
@login_required
@require_perm("scrap", "capture")
def link_entry_product(entry_id):
    """Attach a product to a row whose part number matched nothing on import."""
    form = DeleteForm()
    if not form.validate_on_submit():
        flash("Could not verify that request. Please try again.", "danger")
        return redirect(url_for("scrap.entries"))

    entry = ScrapEntry.query.get_or_404(entry_id)
    product_id = request.form.get("product_id", type=int)

    entry.product_id = product_id or None
    db.session.commit()

    if product_id:
        flash(f"Linked to {entry.product.name}.", "success")
    else:
        flash("Product link cleared.", "info")
    return redirect(request.referrer or url_for("scrap.entries"))


# ─────────────────────────────────────────────
#  REPORTS  (monthly / yearly)
# ─────────────────────────────────────────────

def _build_report(period, filters):
    """Aggregate scrap by month or year, plus defect and product breakdowns."""
    year_col = func.extract("year", ScrapEntry.entry_date)
    month_col = func.extract("month", ScrapEntry.entry_date)

    group_cols = [year_col] if period == "year" else [year_col, month_col]
    select_cols = [year_col.label("year")]
    if period != "year":
        select_cols.append(month_col.label("month"))

    period_query = _apply_filters(
        db.session.query(
            *select_cols,
            func.count(ScrapEntry.id).label("entries"),
            func.coalesce(func.sum(ScrapEntry.qty_machined), 0).label("machined"),
            _PACKED_SUM.label("packed"),
            func.coalesce(func.sum(ScrapEntry.qty_scrap), 0).label("scrap"),
            _EXTERNAL_SCRAP.label("external"),
            _INTERNAL_SCRAP.label("internal"),
        ),
        filters,
    ).group_by(*group_cols).order_by(*group_cols)

    # ── Rand value, folded onto each grouping ──
    priced_scrap = _priced_scrap(filters)

    value_by_period = {}
    value_by_part = {}
    for row in priced_scrap:
        entry_date = row["entry_date"]
        period_key = (entry_date.year,
                      None if period == "year" else entry_date.month)
        _add_value(value_by_period.setdefault(period_key, _blank_value()), row)
        part_key = _part_key(row["product_id"], row["casting_no"], row["machined_part_no"])
        _add_value(value_by_part.setdefault(part_key, _blank_value()), row)

    periods = []
    for row in period_query.all():
        year = int(row.year)
        month = int(row.month) if period != "year" else None
        scrap_qty = int(row.scrap or 0)
        internal = int(row.internal or 0)
        # "machined" here means total produced — qty_machined (external) plus
        # qty_packed (internal); the pct denominator also adds internal's own
        # scrap back on (see ScrapEntry.total_qty).
        machined = int(row.machined or 0) + int(row.packed or 0)
        valued = value_by_period.get((year, month), _blank_value())
        periods.append({
            "year": year,
            "month": month,
            "label": f"{month_abbr[month]} {year}" if month else str(year),
            "entries": int(row.entries or 0),
            "machined": machined,
            "scrap": scrap_qty,
            "external": int(row.external or 0),
            "internal": internal,
            "pct": _pct(scrap_qty, machined + internal),
            "value": valued["value"],
            "unpriced_qty": valued["unpriced_qty"],
            "price_basis": _price_basis(valued),
        })

    # Reject reasons over the same filtered set, valued the same way
    defect_values = {}
    for row in _priced_defects(filters):
        bucket = defect_values.setdefault(
            row["defect_id"], dict(_blank_value(), qty=0)
        )
        bucket["qty"] += row["qty"]
        _add_value(bucket, row)

    catalogue = {d.id: d for d in ScrapDefect.query.all()}
    defect_rows = []
    for defect_id, bucket in defect_values.items():
        defect = catalogue.get(defect_id)
        if defect is None or not bucket["qty"]:
            continue
        defect_rows.append({
            "code": defect.code,
            "name": defect.name,
            "qty": bucket["qty"],
            "value": bucket["value"],
            "unpriced_qty": bucket["unpriced_qty"],
            "price_basis": _price_basis(bucket),
        })
    defect_rows.sort(key=lambda r: r["qty"], reverse=True)

    defect_total = sum(r["qty"] for r in defect_rows)
    for row in defect_rows:
        row["share"] = _pct(row["qty"], defect_total) or 0

    # Per-part breakdown.
    # Linked entries roll up by product, so the same part counts as one line no
    # matter how its casting / machined number was typed on each row (the import
    # source spells it a few ways — "1 202 LOL 00" vs "1 202 L0L 00"). Unlinked
    # entries have no product to roll up under, so they group by their raw
    # casting / machined number instead.
    linked_query = _apply_filters(
        db.session.query(
            ScrapEntry.product_id,
            func.coalesce(func.sum(ScrapEntry.qty_machined), 0).label("machined"),
            _PACKED_SUM.label("packed"),
            func.coalesce(func.sum(ScrapEntry.qty_scrap), 0).label("scrap"),
            _INTERNAL_SCRAP.label("internal_scrap"),
        ),
        filters,
    ).filter(ScrapEntry.product_id.isnot(None)).group_by(ScrapEntry.product_id)

    unlinked_query = _apply_filters(
        db.session.query(
            ScrapEntry.casting_no,
            ScrapEntry.machined_part_no,
            func.coalesce(func.sum(ScrapEntry.qty_machined), 0).label("machined"),
            _PACKED_SUM.label("packed"),
            func.coalesce(func.sum(ScrapEntry.qty_scrap), 0).label("scrap"),
            _INTERNAL_SCRAP.label("internal_scrap"),
        ),
        filters,
    ).filter(ScrapEntry.product_id.is_(None)).group_by(
        ScrapEntry.casting_no, ScrapEntry.machined_part_no
    )

    products = {p.id: p for p in Product.query.all()}
    part_rows = []
    for row in linked_query.all():
        product = products.get(row.product_id)
        scrap_qty = int(row.scrap or 0)
        machined = int(row.machined or 0) + int(row.packed or 0)
        valued = value_by_part.get(_part_key(row.product_id, None, None), _blank_value())
        part_rows.append({
            "product": product.name if product else None,
            "casting_no": product.product_code if product else None,
            "machined_part_no": product.supplier_code if product else None,
            "machined": machined,
            "scrap": scrap_qty,
            "pct": _pct(scrap_qty, machined + int(row.internal_scrap or 0)),
            "value": valued["value"],
            "unpriced_qty": valued["unpriced_qty"],
            "price_basis": _price_basis(valued),
        })
    for row in unlinked_query.all():
        scrap_qty = int(row.scrap or 0)
        machined = int(row.machined or 0) + int(row.packed or 0)
        valued = value_by_part.get(
            _part_key(None, row.casting_no, row.machined_part_no), _blank_value()
        )
        part_rows.append({
            "product": None,
            "casting_no": row.casting_no,
            "machined_part_no": row.machined_part_no,
            "machined": machined,
            "scrap": scrap_qty,
            "pct": _pct(scrap_qty, machined + int(row.internal_scrap or 0)),
            "value": valued["value"],
            "unpriced_qty": valued["unpriced_qty"],
            "price_basis": _price_basis(valued),
        })

    part_rows.sort(key=lambda r: r["scrap"], reverse=True)
    part_rows = part_rows[:100]

    totals = {
        "entries": sum(p["entries"] for p in periods),
        "machined": sum(p["machined"] for p in periods),
        "scrap": sum(p["scrap"] for p in periods),
        "external": sum(p["external"] for p in periods),
        "internal": sum(p["internal"] for p in periods),
        # Summed off the priced rows, not off `periods` — the part table is
        # capped at 100 rows and would under-count the rand total.
        "value": sum((r["value"] for r in priced_scrap), Decimal("0")),
        "unpriced_qty": sum(r["unpriced_qty"] for r in priced_scrap),
    }
    totals["pct"] = _pct(totals["scrap"], totals["machined"] + totals["internal"])
    totals["priced_pct"] = _pct(
        totals["scrap"] - totals["unpriced_qty"], totals["scrap"]
    )

    return periods, defect_rows, part_rows, totals


@scrap_bp.route("/<division>/reports")
@login_required
@require_perm("scrap", "view")
def reports():
    period = request.args.get("period", "month")
    if period not in ("month", "year"):
        period = "month"

    filters = _read_filters()
    periods, defect_rows, part_rows, totals = _build_report(period, filters)
    period_max = max((p["scrap"] for p in periods), default=0)

    return render_template(
        "scrap/report.html",
        period=period,
        filters=filters,
        periods=periods,
        period_max=period_max,
        defect_rows=defect_rows,
        part_rows=part_rows,
        totals=totals,
        customers=_division_customers(),
        products=Product.query.order_by(Product.name).all(),
        source_choices=SOURCE_CHOICES,
        years=_available_years(),
    )


# ─────────────────────────────────────────────
#  REPORTS  (packed balance — outstanding right now)
# ─────────────────────────────────────────────

@scrap_bp.route("/<division>/reports/balance")
@login_required
@require_perm("scrap", "view")
def balance_report():
    filters = _read_filters()
    as_of = filters.get("end_date") or None

    balances = _packed_balance(
        customer_id=filters.get("customer_id"),
        product_id=filters.get("product_id"),
        as_of=as_of,
    )

    products = {p.id: p for p in Product.query.all()}
    rows = []
    for product_id, bucket in balances.items():
        if not bucket["packed"] and not bucket["dispatched"]:
            continue
        product = products.get(product_id)
        rows.append({
            "product": product.name if product else None,
            "casting_no": product.product_code if product else None,
            "packed": bucket["packed"],
            "dispatched": bucket["dispatched"],
            "balance": bucket["balance"],
        })
    rows.sort(key=lambda r: r["balance"], reverse=True)

    totals = {
        "packed": sum(r["packed"] for r in rows),
        "dispatched": sum(r["dispatched"] for r in rows),
        "balance": sum(r["balance"] for r in rows),
    }

    return render_template(
        "scrap/balance_report.html",
        filters=filters,
        rows=rows,
        totals=totals,
        customers=_division_customers(),
        products=Product.query.order_by(Product.name).all(),
    )


# ─────────────────────────────────────────────
#  REPORTS  (reason mix by part)
# ─────────────────────────────────────────────

# Basis for the per-reason share shown against each part.
#   selected → the shares across the chosen reasons add up to 100%, which is
#              the "40% OB / 60% Deformed" reading of a two-reason report.
#   scrap    → each share is measured against everything scrapped on that part,
#              so the columns fall short of 100% when other reasons are in play.
BASIS_SELECTED = "selected"
BASIS_SCRAP    = "scrap"

SORT_PCT   = "pct"      # worst reject % first — the default ranking
SORT_SCRAP = "scrap"
SORT_VALUE = "value"    # most rands lost first
SORT_PART  = "part"

# Reason columns shown before anything is picked, worst first
DEFAULT_REASON_COLUMNS = 8

# Cycled per reason column so a part's mix bar reads the same in every row.
REASON_COLOURS = [
    "#2563eb", "#dc2626", "#d97706", "#059669", "#7c3aed", "#0891b2",
    "#db2777", "#65a30d", "#ea580c", "#4f46e5", "#0d9488", "#b45309",
]


def _part_key(product_id, casting_no, machined_part_no):
    """
    Group key that folds a part's rows together.

    A linked row keys on its product, so the same part counts once however its
    numbers were typed on each source row. An unlinked row has no product to
    fold under, so it keys on the raw numbers as they were imported.
    """
    if product_id:
        return ("p", product_id)
    return ("r", (casting_no or "").strip().upper(), (machined_part_no or "").strip().upper())


def _build_reason_report(filters, reason_ids, basis=BASIS_SELECTED, sort=SORT_PCT):
    """
    Scrap per part split across the chosen reject reasons.

    Returns (parts, reasons, totals). `reasons` are the columns in report order;
    every part row carries a qty and a share for each of them.
    """
    if reason_ids:
        reasons = [d for d in active_defects() if d.id in reason_ids]
    else:
        # Nothing picked — the whole catalogue would be sixty-odd columns, so
        # open on the reasons that actually carry this selection's scrap.
        ranked = _apply_filters(
            db.session.query(
                ScrapEntryDefect.defect_id,
                func.coalesce(func.sum(ScrapEntryDefect.qty), 0).label("qty"),
            )
            .select_from(ScrapEntryDefect)
            .join(ScrapEntry, ScrapEntryDefect.entry_id == ScrapEntry.id),
            filters,
        ).group_by(ScrapEntryDefect.defect_id).order_by(
            func.coalesce(func.sum(ScrapEntryDefect.qty), 0).desc()
        ).limit(DEFAULT_REASON_COLUMNS).all()

        top_ids = {r.defect_id for r in ranked if int(r.qty or 0)}
        reasons = [d for d in active_defects() if d.id in top_ids]

    reason_index = {d.id: i for i, d in enumerate(reasons)}

    # ── Per-part machined / scrap totals ──
    totals_query = _apply_filters(
        db.session.query(
            ScrapEntry.product_id,
            ScrapEntry.casting_no,
            ScrapEntry.machined_part_no,
            func.count(ScrapEntry.id).label("entries"),
            func.coalesce(func.sum(ScrapEntry.qty_machined), 0).label("machined"),
            _PACKED_SUM.label("packed"),
            func.coalesce(func.sum(ScrapEntry.qty_scrap), 0).label("scrap"),
            _INTERNAL_SCRAP.label("internal_scrap"),
        ),
        filters,
    ).group_by(
        ScrapEntry.product_id, ScrapEntry.casting_no, ScrapEntry.machined_part_no
    )

    products = {p.id: p for p in Product.query.all()}
    parts = {}
    for row in totals_query.all():
        key = _part_key(row.product_id, row.casting_no, row.machined_part_no)
        part = parts.get(key)
        if part is None:
            product = products.get(row.product_id) if row.product_id else None
            part = parts[key] = {
                "product": product.name if product else None,
                "casting_no": product.product_code if product else row.casting_no,
                "machined_part_no": (product.supplier_code if product
                                     else row.machined_part_no),
                "entries": 0,
                "machined": 0,
                "scrap": 0,
                "internal_scrap": 0,
                "qtys": [0] * len(reasons),
                "col_values": [Decimal("0")] * len(reasons),
                "reason_total": 0,
                "reason_value": Decimal("0"),
                "value": Decimal("0"),
                "unpriced_qty": 0,
                "list_qty": 0,
                "fallback_qty": 0,
            }
        part["entries"]  += int(row.entries or 0)
        part["machined"] += int(row.machined or 0) + int(row.packed or 0)
        part["scrap"]    += int(row.scrap or 0)
        part["internal_scrap"] += int(row.internal_scrap or 0)

    # Rand value of everything scrapped on each part, at each line's own date
    for row in _priced_scrap(filters):
        part = parts.get(_part_key(row["product_id"], row["casting_no"], row["machined_part_no"]))
        if part is None:
            continue
        part["value"]        += row["value"]
        part["unpriced_qty"] += row["unpriced_qty"]
        part["list_qty"]     += row["list_qty"]
        part["fallback_qty"] += row["fallback_qty"]

    # ── The same parts split across the chosen reasons, priced per line ──
    for row in _priced_defects(filters, reason_index):
        part = parts.get(_part_key(row["product_id"], row["casting_no"], row["machined_part_no"]))
        if part is None:            # entry filtered out of the totals — skip
            continue
        column = reason_index[row["defect_id"]]
        part["qtys"][column]   += row["qty"]
        part["col_values"][column] += row["value"]
        part["reason_total"]   += row["qty"]
        part["reason_value"]   += row["value"]

    # Only parts actually scrapped for one of the chosen reasons — asking for OB
    # and Deformed should not list every part that was ever machined.
    part_rows = [p for p in parts.values() if p["reason_total"]]

    for part in part_rows:
        base = part["scrap"] if basis == BASIS_SCRAP else part["reason_total"]
        part["pct"] = _pct(part["scrap"], part["machined"] + part["internal_scrap"])
        part["shares"] = [(_pct(q, base) or 0) for q in part["qtys"]]
        # What the chosen reasons account for out of everything scrapped
        part["coverage"] = _pct(part["reason_total"], part["scrap"])
        part["price_basis"] = _price_basis(part)

    if sort == SORT_VALUE:
        part_rows.sort(key=lambda p: p["value"], reverse=True)
    elif sort == SORT_SCRAP:
        part_rows.sort(key=lambda p: p["scrap"], reverse=True)
    elif sort == SORT_PART:
        part_rows.sort(key=lambda p: (p["product"] or p["casting_no"] or "").upper())
    else:
        # Worst reject % first. A part with nothing machined has no % to rank on,
        # so it drops below the ranked rows rather than sorting as a zero.
        part_rows.sort(
            key=lambda p: (p["pct"] is not None, p["pct"] or 0, p["scrap"]),
            reverse=True,
        )

    totals = {
        "parts": len(part_rows),
        "entries": sum(p["entries"] for p in part_rows),
        "machined": sum(p["machined"] for p in part_rows),
        "internal_scrap": sum(p["internal_scrap"] for p in part_rows),
        "scrap": sum(p["scrap"] for p in part_rows),
        "reason_total": sum(p["reason_total"] for p in part_rows),
        "qtys": [sum(p["qtys"][i] for p in part_rows) for i in range(len(reasons))],
        "value": sum((p["value"] for p in part_rows), Decimal("0")),
        "reason_value": sum((p["reason_value"] for p in part_rows), Decimal("0")),
        "col_values": [sum((p["col_values"][i] for p in part_rows), Decimal("0"))
                   for i in range(len(reasons))],
        "unpriced_qty": sum(p["unpriced_qty"] for p in part_rows),
    }
    totals["pct"] = _pct(totals["scrap"], totals["machined"] + totals["internal_scrap"])
    totals["coverage"] = _pct(totals["reason_total"], totals["scrap"])
    totals["priced_pct"] = _pct(
        totals["scrap"] - totals["unpriced_qty"], totals["scrap"]
    )
    base = totals["scrap"] if basis == BASIS_SCRAP else totals["reason_total"]
    totals["shares"] = [(_pct(q, base) or 0) for q in totals["qtys"]]

    return part_rows, reasons, totals


def _reason_month_series(filters, reason_ids):
    """
    Total scrap qty (and scrap %) per calendar month across the given reasons,
    for the reason-mix trend line. One point per month between the earliest
    and latest month with data, so a quiet month shows as zero rather than
    the line skipping straight over it. Spans as many years as the filtered
    data covers.

    `pct` follows the same denominator as a part's `pct` in
    `_build_reason_report` — machined + packed + internal_scrap for that
    period — so it reads as "share of that month's production scrapped for
    these reasons," not a share of that month's total scrap.
    """
    if not reason_ids:
        return []

    year_col = func.extract("year", ScrapEntry.entry_date)
    month_col = func.extract("month", ScrapEntry.entry_date)

    rows = _apply_filters(
        db.session.query(
            year_col.label("year"),
            month_col.label("month"),
            func.coalesce(func.sum(ScrapEntryDefect.qty), 0).label("qty"),
        )
        .select_from(ScrapEntryDefect)
        .join(ScrapEntry, ScrapEntryDefect.entry_id == ScrapEntry.id),
        filters,
    ).filter(
        ScrapEntryDefect.defect_id.in_(list(reason_ids))
    ).group_by(year_col, month_col).all()

    by_period = {(int(r.year), int(r.month)): int(r.qty or 0) for r in rows}
    if not by_period:
        return []

    base_rows = _apply_filters(
        db.session.query(
            year_col.label("year"),
            month_col.label("month"),
            func.coalesce(func.sum(ScrapEntry.qty_machined), 0).label("machined"),
            _PACKED_SUM.label("packed"),
            _INTERNAL_SCRAP.label("internal_scrap"),
        ),
        filters,
    ).group_by(year_col, month_col).all()
    base_by_period = {
        (int(r.year), int(r.month)):
            int(r.machined or 0) + int(r.packed or 0) + int(r.internal_scrap or 0)
        for r in base_rows
    }

    keys = sorted(by_period.keys())
    (y, m), (y1, m1) = keys[0], keys[-1]

    series = []
    while (y, m) <= (y1, m1):
        qty = by_period.get((y, m), 0)
        series.append({
            "year": y,
            "month": m,
            "label": f"{month_abbr[m]} {y}",
            "qty": qty,
            "pct": _pct(qty, base_by_period.get((y, m), 0)) or 0,
        })
        m += 1
        if m > 12:
            m = 1
            y += 1
    return series


CHART_LINE = "line"
CHART_BAR = "bar"

METRIC_QTY = "qty"
METRIC_PCT = "pct"


def _read_reason_options():
    """Reason selection, share basis, ranking, chart type and plotted metric off the query string."""
    reason_ids = {int(v) for v in request.args.getlist("reason") if v.isdigit()}

    basis = request.args.get("basis", BASIS_SELECTED)
    if basis not in (BASIS_SELECTED, BASIS_SCRAP):
        basis = BASIS_SELECTED

    sort = request.args.get("sort", SORT_PCT)
    if sort not in (SORT_PCT, SORT_SCRAP, SORT_VALUE, SORT_PART):
        sort = SORT_PCT

    chart_type = request.args.get("chart_type", CHART_LINE)
    if chart_type not in (CHART_LINE, CHART_BAR):
        chart_type = CHART_LINE

    metric = request.args.get("metric", METRIC_QTY)
    if metric not in (METRIC_QTY, METRIC_PCT):
        metric = METRIC_QTY

    return reason_ids, basis, sort, chart_type, metric


@scrap_bp.route("/<division>/reports/reasons")
@login_required
@require_perm("scrap", "view")
def reason_report():
    filters = _read_filters()
    reason_ids, basis, sort, chart_type, metric = _read_reason_options()

    part_rows, reasons, totals = _build_reason_report(filters, reason_ids, basis, sort)
    month_series = _reason_month_series(filters, [d.id for d in reasons])

    return render_template(
        "scrap/reason_report.html",
        filters=filters,
        reason_ids=reason_ids,
        basis=basis,
        sort=sort,
        chart_type=chart_type,
        metric=metric,
        parts=part_rows,
        reasons=reasons,
        totals=totals,
        month_series=month_series,
        colours=REASON_COLOURS,
        all_reasons=active_defects(),
        scope_external=SCOPE_EXTERNAL,
        scope_internal=SCOPE_INTERNAL,
        customers=_division_customers(),
        products=Product.query.order_by(Product.name).all(),
        source_choices=SOURCE_CHOICES,
        years=_available_years(),
        link_args=_link_args(filters, basis=basis, sort=sort,
                             reason=sorted(reason_ids), chart_type=chart_type, metric=metric),
        period_link_args=_link_args(filters),
        chart_link_line=_link_args(filters, basis=basis, sort=sort,
                                   reason=sorted(reason_ids), chart_type=CHART_LINE, metric=metric),
        chart_link_bar=_link_args(filters, basis=basis, sort=sort,
                                  reason=sorted(reason_ids), chart_type=CHART_BAR, metric=metric),
        metric_link_qty=_link_args(filters, basis=basis, sort=sort,
                                   reason=sorted(reason_ids), chart_type=chart_type, metric=METRIC_QTY),
        metric_link_pct=_link_args(filters, basis=basis, sort=sort,
                                   reason=sorted(reason_ids), chart_type=chart_type, metric=METRIC_PCT),
    )


@scrap_bp.route("/<division>/reports/reasons/export.csv")
@login_required
@require_perm("scrap", "view")
def reason_report_export():
    filters = _read_filters()
    reason_ids, basis, sort, _chart_type, _metric = _read_reason_options()

    part_rows, reasons, totals = _build_reason_report(filters, reason_ids, basis, sort)

    buffer = io.StringIO()
    writer = csv.writer(buffer)

    writer.writerow(["Scrap report — reason mix by part"])
    writer.writerow([
        "Reasons",
        ", ".join(f"{d.code} {d.name}" for d in reasons) or "All",
    ])
    writer.writerow([
        "Share basis",
        "% of selected reasons" if basis == BASIS_SELECTED else "% of all scrap on the part",
    ])
    writer.writerow(["Period", f"{filters['start_date'] or 'start'} to {filters['end_date'] or 'today'}"])
    writer.writerow([])

    header = ["Product", "Casting Number", "Machined Part Number",
              "Qty Machined", "Qty Scrap", "% Reject", "Scrap Value (R)",
              "Selected Reason Qty", "Selected Reason Value (R)"]
    for defect in reasons:
        header += [f"{defect.code} Qty", f"{defect.code} %", f"{defect.code} Value (R)"]
    writer.writerow(header)

    for row in part_rows:
        line = [
            row["product"] or "", row["casting_no"] or "", row["machined_part_no"] or "",
            row["machined"], row["scrap"],
            f"{row['pct']:.2f}" if row["pct"] is not None else "",
            f"{row['value']:.2f}",
            row["reason_total"], f"{row['reason_value']:.2f}",
        ]
        for i in range(len(reasons)):
            line += [row["qtys"][i], f"{row['shares'][i]:.1f}", f"{row['col_values'][i]:.2f}"]
        writer.writerow(line)

    total_line = [
        "TOTAL", "", "", totals["machined"], totals["scrap"],
        f"{totals['pct']:.2f}" if totals["pct"] is not None else "",
        f"{totals['value']:.2f}",
        totals["reason_total"], f"{totals['reason_value']:.2f}",
    ]
    for i in range(len(reasons)):
        total_line += [totals["qtys"][i], f"{totals['shares'][i]:.1f}",
                       f"{totals['col_values'][i]:.2f}"]
    writer.writerow(total_line)

    if totals["unpriced_qty"]:
        writer.writerow([])
        writer.writerow([
            f"{totals['unpriced_qty']} scrapped unit(s) carry no price "
            "and are excluded from the rand values above."
        ])

    filename = f"scrap_reason_mix_{date.today().isoformat()}.csv"
    return Response(
        buffer.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


def _company_logo_url():
    """file:// URI for the PDF header logo, or None if it hasn't been added yet."""
    logo_path = Path(current_app.static_folder) / "images" / "hdc_logo.png"
    return logo_path.as_uri() if logo_path.exists() else None


def _nice_axis_step(max_value):
    """Round max_value/5 up to a 1/2/5×10^n step, so gridlines land on tidy numbers."""
    if max_value <= 0:
        return 1
    magnitude = 10 ** math.floor(math.log10(max_value / 5 or 1))
    for m in (1, 2, 5, 10):
        step = m * magnitude
        if step * 5 >= max_value:
            return step
    return magnitude * 10


def _month_trend_svg(series, chart_type=CHART_LINE, metric=METRIC_QTY, width=980, height=230):
    """
    Inline SVG chart matching the live page's Chart.js "Total by Month" —
    a PDF has no JS engine to run Chart.js against a <canvas>, so the same
    figures are drawn by hand as SVG, which WeasyPrint renders natively.
    """
    if not series:
        return ""

    pad_left, pad_right, pad_top, pad_bottom = 44, 10, 12, 48
    plot_w = width - pad_left - pad_right
    plot_h = height - pad_top - pad_bottom

    is_pct = metric == METRIC_PCT
    values = [(m["pct"] or 0) if is_pct else m["qty"] for m in series]

    if is_pct:
        axis_max = max(max(values), 1)
        axis_max = math.ceil(axis_max / 5) * 5 or 5
    else:
        step = _nice_axis_step(max(values))
        axis_max = step * 5
        while max(values) > axis_max:
            axis_max += step

    n = len(series)
    baseline = pad_top + plot_h

    def x_at(i):
        return pad_left + (plot_w * i / (n - 1) if n > 1 else plot_w / 2)

    def y_at(value):
        return pad_top + plot_h - (plot_h * value / axis_max if axis_max else 0)

    points = [(x_at(i), y_at(v)) for i, v in enumerate(values)]

    gridlines = []
    for g in range(6):
        val = axis_max * g / 5
        y = y_at(val)
        label = f"{val:.1f}%" if is_pct else f"{int(round(val)):,}"
        gridlines.append(
            f'<line x1="{pad_left}" y1="{y:.1f}" x2="{width - pad_right}" y2="{y:.1f}" '
            f'stroke="#eef2f7" stroke-width="1"/>'
            f'<text x="{pad_left - 8}" y="{y + 3:.1f}" font-size="9" fill="#94a3b8" '
            f'text-anchor="end">{label}</text>'
        )

    if chart_type == CHART_BAR:
        bar_w = (plot_w / n) * 0.6 if n else 0
        body = "".join(
            f'<rect x="{x - bar_w / 2:.1f}" y="{y:.1f}" width="{bar_w:.1f}" '
            f'height="{baseline - y:.1f}" rx="2" fill="#2563eb"/>'
            for x, y in points
        )
    else:
        line_path = "M " + " L ".join(f"{x:.1f},{y:.1f}" for x, y in points)
        area_path = (
            line_path
            + f" L {points[-1][0]:.1f},{baseline:.1f} L {points[0][0]:.1f},{baseline:.1f} Z"
        )
        dots = "".join(
            f'<circle cx="{x:.1f}" cy="{y:.1f}" r="2.4" fill="#2563eb"/>' for x, y in points
        )
        body = (
            f'<path d="{area_path}" fill="rgba(37,99,235,0.12)" stroke="none"/>'
            f'<path d="{line_path}" fill="none" stroke="#2563eb" stroke-width="2"/>'
            + dots
        )

    # Rotated so a long month range doesn't collide label-to-label — Chart.js
    # does the same thing on the live page once the axis gets crowded.
    labels = []
    for i, m in enumerate(series):
        x, _ = points[i]
        y = height - pad_bottom + 14
        labels.append(
            f'<text x="{x:.1f}" y="{y}" font-size="8" fill="#64748b" '
            f'text-anchor="end" transform="rotate(-40 {x:.1f} {y})">{m["label"]}</text>'
        )

    return (
        f'<svg viewBox="0 0 {width} {height}" xmlns="http://www.w3.org/2000/svg" '
        f'style="width:100%;height:auto;display:block;">'
        + "".join(gridlines)
        + body
        + "".join(labels)
        + "</svg>"
    )


@scrap_bp.route("/<division>/reports/reasons/export.pdf")
@login_required
@require_perm("scrap", "view")
def reason_report_pdf():
    filters = _read_filters()
    reason_ids, basis, sort, chart_type, metric = _read_reason_options()

    part_rows, reasons, totals = _build_reason_report(filters, reason_ids, basis, sort)
    month_series = _reason_month_series(filters, [d.id for d in reasons])

    customer = Customer.query.get(filters["customer_id"]) if filters.get("customer_id") else None
    product_ids = filters.get("product_id") or []
    if len(product_ids) == 1:
        product = Product.query.get(product_ids[0])
        product_name = product.name if product else None
    elif product_ids:
        product_name = f"{len(product_ids)} products"
    else:
        product_name = None

    html = render_template(
        "scrap/reason_report_pdf.html",
        filters=filters,
        basis=basis,
        chart_type=chart_type,
        metric=metric,
        parts=part_rows,
        reasons=reasons,
        totals=totals,
        month_series=month_series,
        month_svg=_month_trend_svg(month_series, chart_type=chart_type, metric=metric),
        colours=REASON_COLOURS,
        logo_url=_company_logo_url(),
        generated_at=datetime.now(),
        customer_name=customer.name if customer else None,
        product_name=product_name,
    )
    pdf_bytes = HTML(string=html, base_url=request.url_root).write_pdf()

    filename = f"scrap_reason_mix_{date.today().isoformat()}.pdf"
    return Response(
        pdf_bytes,
        mimetype="application/pdf",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@scrap_bp.route("/<division>/reports/export.csv")
@login_required
@require_perm("scrap", "view")
def reports_export():
    period = request.args.get("period", "month")
    if period not in ("month", "year"):
        period = "month"

    filters = _read_filters()
    periods, defect_rows, part_rows, totals = _build_report(period, filters)

    buffer = io.StringIO()
    writer = csv.writer(buffer)

    writer.writerow([f"Scrap report — by {period}"])
    writer.writerow([])
    writer.writerow(["Period", "Entries", "Qty Machined", "Qty Scrap",
                     "External Scrap", "Internal Scrap", "% Reject",
                     "Scrap Value (R)"])
    for row in periods:
        writer.writerow([
            row["label"], row["entries"], row["machined"], row["scrap"],
            row["external"], row["internal"],
            f"{row['pct']:.2f}" if row["pct"] is not None else "",
            f"{row['value']:.2f}",
        ])
    writer.writerow([
        "TOTAL", totals["entries"], totals["machined"], totals["scrap"],
        totals["external"], totals["internal"],
        f"{totals['pct']:.2f}" if totals["pct"] is not None else "",
        f"{totals['value']:.2f}",
    ])

    writer.writerow([])
    writer.writerow(["Reject reason", "Qty", "% of scrap", "Scrap Value (R)"])
    for row in defect_rows:
        writer.writerow([row["name"], row["qty"], f"{row['share']:.1f}",
                         f"{row['value']:.2f}"])

    writer.writerow([])
    writer.writerow(["Product", "Casting Number", "Machined Part Number",
                     "Qty Machined", "Qty Scrap", "% Reject", "Scrap Value (R)"])
    for row in part_rows:
        writer.writerow([
            row["product"] or "", row["casting_no"] or "", row["machined_part_no"] or "",
            row["machined"], row["scrap"],
            f"{row['pct']:.2f}" if row["pct"] is not None else "",
            f"{row['value']:.2f}",
        ])

    if totals["unpriced_qty"]:
        writer.writerow([])
        writer.writerow([
            f"{totals['unpriced_qty']} scrapped unit(s) carry no price "
            "and are excluded from the rand values above."
        ])

    filename = f"scrap_report_{period}ly_{date.today().isoformat()}.csv"
    return Response(
        buffer.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


# ─────────────────────────────────────────────
#  DEFECT CATALOGUE
# ─────────────────────────────────────────────

@scrap_bp.route("/<division>/defects", methods=["GET", "POST"])
@scrap_bp.route("/<division>/defects/<int:defect_id>/edit", methods=["GET", "POST"])
@login_required
@require_perm("scrap", "admin")
def defects(defect_id=None):
    defect = ScrapDefect.query.get_or_404(defect_id) if defect_id else None
    form = ScrapDefectForm(obj=defect)

    if form.validate_on_submit():
        code = form.code.data.strip().upper()
        clash = ScrapDefect.query.filter(ScrapDefect.code == code)
        if defect:
            clash = clash.filter(ScrapDefect.id != defect.id)

        if clash.first():
            flash(f"Reason code '{code}' is already in use.", "warning")
        else:
            if defect is None:
                defect = ScrapDefect()
                db.session.add(defect)
            defect.code        = code
            defect.name        = form.name.data.strip()
            defect.description = (form.description.data or "").strip() or None
            defect.aliases     = (form.aliases.data or "").strip() or None
            defect.applies_to  = form.applies_to.data
            defect.sort_order  = form.sort_order.data or 0
            defect.active      = form.active.data
            db.session.commit()
            flash(f"Reject reason '{defect.name}' saved.", "success")
            return redirect(url_for("scrap.defects"))

    all_defects = ScrapDefect.query.order_by(ScrapDefect.sort_order, ScrapDefect.id).all()

    usage = dict(
        db.session.query(ScrapEntryDefect.defect_id, func.sum(ScrapEntryDefect.qty))
        .group_by(ScrapEntryDefect.defect_id)
        .all()
    )

    return render_template(
        "scrap/defects.html",
        form=form,
        defect=defect,
        defects=all_defects,
        usage=usage,
    )
