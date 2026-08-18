import os
import json
import uuid
from flask import render_template, request, redirect, url_for, session, flash, Response
from flask_login import login_user, logout_user, current_user, login_required
from app import app , db, WINDOWS_LICENSE_DIR, PRODUCT_IMAGE_DIR, PRODUCT_PPAP_DIR, validate_license, license_required
from werkzeug.utils import secure_filename
from models import *
from forms import *
from datetime import date, datetime
from sqlalchemy import func, or_
from access.guards import require_perm, user_can
from product_importer import build_export_csv, build_template_csv, import_products
import pricelist_importer
import json



@app.route("/")
@license_required
def index():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard"))
    return redirect(url_for("login"))


@app.route("/login", methods=["GET", "POST"])
@license_required
def login():
    if request.method == "POST":
        username = request.form.get("username")
        password = request.form.get("password")
        remember = bool(request.form.get("remember"))

        # Find user by username
        user = User.query.filter_by(username=username).first()

        # Check password using the model's check_password method
        if user and user.check_password(password):
            # Log in the user
            login_user(user, remember=remember)

            # Save in session (optional)
            session["user_id"] = user.id
            session["username"] = user.username

            flash(f"Welcome, {user.username}!", "success")
            return redirect(url_for("dashboard"))
        else:
            flash("Invalid username or password", "danger")
            return render_template("login.html")

    # GET request
    return render_template("login.html")

@app.route("/dashboard")
@login_required
@license_required
def dashboard():
    return render_template("dashboard.html", today=date.today())

@app.route("/customers", methods=["GET", "POST"])
@login_required
@license_required
@require_perm("customers", "view")
def customers():
    form = SupplierForm()  # <-- create the form
    _populate_division_choices(form)
    may_edit = user_can(current_user, "customers", "edit")

    if form.validate_on_submit() and not may_edit:
        flash("You do not have permission to add customers.", "danger")
    elif form.validate_on_submit():
        name = form.name.data.strip()
        # Check if customer already exists (case-insensitive)
        existing_customer = Customer.query.filter(
            func.lower(Customer.name) == name.lower()
        ).first()
        if existing_customer:
            flash(f"Customer '{name}' already exists (ID {existing_customer.id}).", "warning")
        else:
            new_customer = Customer(
                name=name,
                code=(form.code.data or "").strip() or None,
                division_id=form.division_id.data or None,
                active=form.active.data
            )
            db.session.add(new_customer)
            db.session.commit()
            flash(f"Customer '{name}' added successfully (ID {new_customer.id}).", "success")
            return redirect(url_for("customers"))

    # Search + paginate — was a single unpaginated table of every customer.
    search = request.args.get("q", "").strip()
    page = request.args.get("page", 1, type=int)

    customers_query = Customer.query
    if search:
        like = f"%{search}%"
        customers_query = customers_query.filter(
            db.or_(Customer.name.ilike(like), Customer.code.ilike(like))
        )
    customers_page = customers_query.order_by(Customer.name).paginate(
        page=page, per_page=25, error_out=False
    )
    total_customers = Customer.query.count()

    # Product count per customer — counts a product whether it is linked as the
    # PRIMARY customer or via the many-to-many "also linked" relationship.
    # UNION (not UNION ALL) de-duplicates products linked both ways.
    primary_pairs = (
        db.session.query(
            Product.id.label("product_id"),
            Product.customer_id.label("customer_id"),
        )
        .filter(Product.customer_id.isnot(None))
    )
    linked_pairs = db.session.query(
        product_customers.c.product_id.label("product_id"),
        product_customers.c.customer_id.label("customer_id"),
    )
    pairs = primary_pairs.union(linked_pairs).subquery()
    product_counts = dict(
        db.session.query(pairs.c.customer_id, func.count(pairs.c.product_id))
        .group_by(pairs.c.customer_id)
        .all()
    )

    # Pass the form into the template
    return render_template(
        "customers.html",
        form=form,
        customers=customers_page,
        total_customers=total_customers,
        search=search,
        product_counts=product_counts,
        may_edit=may_edit
    )


@app.route("/customers/<int:customer_id>/edit", methods=["GET", "POST"])
@login_required
@license_required
@require_perm("customers", "edit")
def edit_customer(customer_id):
    customer = Customer.query.get_or_404(customer_id)
    form = SupplierForm(obj=customer)
    _populate_division_choices(form)

    if form.validate_on_submit():
        name = form.name.data.strip()
        # Case-insensitive duplicate check, excluding this customer
        existing = Customer.query.filter(
            func.lower(Customer.name) == name.lower(),
            Customer.id != customer.id
        ).first()
        if existing:
            flash(f"Another customer named '{name}' already exists (ID {existing.id}).", "warning")
        else:
            customer.name = name
            customer.code = (form.code.data or "").strip() or None
            customer.division_id = form.division_id.data or None
            customer.active = form.active.data
            db.session.commit()
            flash(f"Customer '{name}' updated successfully (ID {customer.id}).", "success")
            return redirect(url_for("customers"))

    if request.method == "GET":
        form.division_id.data = customer.division_id or 0

    return render_template("edit_customer.html", form=form, customer=customer)


def _populate_division_choices(form):
    divisions = Division.query.order_by(Division.code).all()
    form.division_id.choices = [(0, "— None —")] + [(d.id, f"{d.code} — {d.name}") for d in divisions]


# ══════════════════════════════════════════════════════════════
# PRICE LISTS  (effective-dated pricing)
# ══════════════════════════════════════════════════════════════

def _generate_periods(division, cadence, start_year):
    """
    Create price list periods for a division's July→June year.
    Idempotent: skips any period whose start date already exists.
    """
    if cadence == "annual":
        specs = [(
            date(start_year, 7, 1),
            date(start_year + 1, 6, 30),
            f"{division.code} {start_year}/{str(start_year + 1)[-2:]}",
        )]
    else:  # quarterly, aligned to the same Jul→Jun year
        specs = [
            (date(start_year, 7, 1),      date(start_year, 9, 30),      f"{division.code} {start_year} Q3"),
            (date(start_year, 10, 1),     date(start_year, 12, 31),     f"{division.code} {start_year} Q4"),
            (date(start_year + 1, 1, 1),  date(start_year + 1, 3, 31),  f"{division.code} {start_year + 1} Q1"),
            (date(start_year + 1, 4, 1),  date(start_year + 1, 6, 30),  f"{division.code} {start_year + 1} Q2"),
        ]

    created, skipped = [], []
    for start, end, label in specs:
        exists = PriceListPeriod.query.filter_by(
            division_id=division.id, start_date=start
        ).first()
        if exists:
            skipped.append(exists.label)
            continue
        db.session.add(PriceListPeriod(
            division_id=division.id, label=label, start_date=start, end_date=end
        ))
        created.append(label)

    db.session.commit()
    return created, skipped


@app.route("/pricelists", methods=["GET", "POST"])
@login_required
@license_required
@require_perm("pricelists", "view")
def price_lists():
    gen_form = GeneratePeriodsForm()
    divisions = Division.query.order_by(Division.code).all()
    gen_form.division_id.choices = [(d.id, f"{d.code} — {d.name}") for d in divisions]

    if gen_form.validate_on_submit() and not user_can(current_user, "pricelists", "edit"):
        flash("You do not have permission to generate price list periods.", "danger")
        return redirect(url_for("price_lists"))

    if gen_form.validate_on_submit():
        division = Division.query.get(gen_form.division_id.data)
        if not division:
            flash("Please select a valid division.", "danger")
        else:
            created, skipped = _generate_periods(division, gen_form.cadence.data, gen_form.year.data)
            if created:
                flash(f"Created {len(created)} period(s): {', '.join(created)}.", "success")
            if skipped:
                flash(f"Skipped {len(skipped)} period(s) that already exist: {', '.join(skipped)}.", "info")
            if not created and not skipped:
                flash("Nothing to generate.", "info")
        return redirect(url_for("price_lists"))

    if request.method == "GET":
        gen_form.year.data = date.today().year if date.today().month >= 7 else date.today().year - 1

    periods = (
        PriceListPeriod.query
        .order_by(PriceListPeriod.division_id, PriceListPeriod.start_date.desc())
        .all()
    )
    entry_counts = dict(
        db.session.query(PriceListEntry.period_id, func.count(PriceListEntry.id))
        .group_by(PriceListEntry.period_id)
        .all()
    )
    today = date.today()

    return render_template(
        "pricelists.html",
        periods=periods,
        gen_form=gen_form,
        entry_counts=entry_counts,
        today=today,
    )


@app.route("/pricelists/new", methods=["GET", "POST"])
@app.route("/pricelists/<int:period_id>/edit", methods=["GET", "POST"])
@login_required
@license_required
@require_perm("pricelists", "edit")
def price_list_period_form(period_id=None):
    period = PriceListPeriod.query.get_or_404(period_id) if period_id else None
    form = PriceListPeriodForm(obj=period)
    divisions = Division.query.order_by(Division.code).all()
    form.division_id.choices = [(d.id, f"{d.code} — {d.name}") for d in divisions]

    if form.validate_on_submit():
        clash = PriceListPeriod.query.filter(
            PriceListPeriod.division_id == form.division_id.data,
            PriceListPeriod.start_date <= form.end_date.data,
            PriceListPeriod.end_date >= form.start_date.data,
        )
        if period:
            clash = clash.filter(PriceListPeriod.id != period.id)
        clash = clash.first()

        if clash:
            flash(f"That date range overlaps an existing period: {clash.label}.", "danger")
        else:
            if period is None:
                period = PriceListPeriod()
                db.session.add(period)
            period.division_id = form.division_id.data
            period.label       = form.label.data.strip()
            period.start_date  = form.start_date.data
            period.end_date    = form.end_date.data
            db.session.commit()
            flash(f"Price list period '{period.label}' saved.", "success")
            return redirect(url_for("price_list_detail", period_id=period.id))

    return render_template("pricelist_form.html", form=form, period=period)


@app.route("/pricelists/<int:period_id>", methods=["GET", "POST"])
@login_required
@license_required
@require_perm("pricelists", "view")
def price_list_detail(period_id):
    period = PriceListPeriod.query.get_or_404(period_id)

    form = PriceListEntryForm()
    # Only customers in this period's division can be priced on it
    customers_in_div = (
        Customer.query.filter_by(division_id=period.division_id)
        .order_by(Customer.name).all()
    )
    form.customer_id.choices = [(c.id, c.name) for c in customers_in_div]
    # New prices are only set against products still in use; prices already
    # captured for a deactivated product stay on the period.
    form.product_id.choices = [
        (p.id, f"{p.name}" + (f" ({p.product_code})" if p.product_code else ""))
        for p in active_products().all()
    ]

    if form.validate_on_submit() and not user_can(current_user, "pricelists", "edit"):
        flash("You do not have permission to change prices.", "danger")
        return redirect(url_for("price_list_detail", period_id=period.id))

    if form.validate_on_submit():
        # Upsert — re-adding the same customer+product updates its price
        entry = PriceListEntry.query.filter_by(
            period_id=period.id,
            customer_id=form.customer_id.data,
            product_id=form.product_id.data,
        ).first()
        if entry:
            entry.price = form.price.data
            flash("Price updated.", "success")
        else:
            db.session.add(PriceListEntry(
                period_id=period.id,
                customer_id=form.customer_id.data,
                product_id=form.product_id.data,
                price=form.price.data,
            ))
            flash("Price added.", "success")
        db.session.commit()
        return redirect(url_for("price_list_detail", period_id=period.id))

    entries = (
        PriceListEntry.query
        .filter_by(period_id=period.id)
        .join(Customer, PriceListEntry.customer_id == Customer.id)
        .join(Product, PriceListEntry.product_id == Product.id)
        .order_by(Customer.name, Product.name)
        .all()
    )

    return render_template(
        "pricelist_detail.html",
        period=period,
        entries=entries,
        form=form,
        has_customers=bool(customers_in_div),
    )


@app.route("/pricelists/<int:period_id>/copy-forward", methods=["POST"])
@login_required
@license_required
@require_perm("pricelists", "edit")
def price_list_copy_forward(period_id):
    """Copy every price from the previous period of the same division into this one."""
    period = PriceListPeriod.query.get_or_404(period_id)

    previous = (
        PriceListPeriod.query
        .filter(
            PriceListPeriod.division_id == period.division_id,
            PriceListPeriod.start_date < period.start_date,
        )
        .order_by(PriceListPeriod.start_date.desc())
        .first()
    )
    if not previous:
        flash("There is no earlier period for this division to copy from.", "warning")
        return redirect(url_for("price_list_detail", period_id=period.id))

    existing_pairs = {
        (e.customer_id, e.product_id)
        for e in PriceListEntry.query.filter_by(period_id=period.id).all()
    }

    copied = 0
    for src in PriceListEntry.query.filter_by(period_id=previous.id).all():
        if (src.customer_id, src.product_id) in existing_pairs:
            continue
        db.session.add(PriceListEntry(
            period_id=period.id,
            customer_id=src.customer_id,
            product_id=src.product_id,
            price=src.price,
        ))
        copied += 1

    db.session.commit()
    if copied:
        flash(f"Copied {copied} price(s) forward from '{previous.label}'. Adjust them as needed.", "success")
    else:
        flash(f"Nothing to copy — every price from '{previous.label}' already exists here.", "info")
    return redirect(url_for("price_list_detail", period_id=period.id))


@app.route("/pricelists/entries/<int:entry_id>/delete", methods=["POST"])
@login_required
@license_required
@require_perm("pricelists", "edit")
def price_list_entry_delete(entry_id):
    entry = PriceListEntry.query.get_or_404(entry_id)
    period_id = entry.period_id
    db.session.delete(entry)
    db.session.commit()
    flash("Price removed.", "success")
    return redirect(url_for("price_list_detail", period_id=period_id))


@app.route("/pricelists/<int:period_id>/import", methods=["GET", "POST"])
@login_required
@license_required
@require_perm("pricelists", "import")
def price_list_import(period_id):
    period = PriceListPeriod.query.get_or_404(period_id)
    form = PriceListImportForm()
    result = None

    if form.validate_on_submit():
        try:
            result = pricelist_importer.import_price_list_entries(period, form.file.data)
        except ValueError as exc:
            flash(f"Import failed: {exc}", "danger")
            return redirect(url_for("price_list_import", period_id=period.id))

        if result.touched:
            flash(
                f"{result.created} price(s) added, {result.updated} updated"
                + (f", {result.skipped} row(s) skipped." if result.skipped else "."),
                "success"
            )
        else:
            flash("Nothing was imported — no usable rows in that file.", "warning")

        if result.unknown_columns:
            flash(
                "Columns ignored (not part of the template): "
                + ", ".join(result.unknown_columns),
                "info"
            )

    return render_template(
        "pricelist_import.html", form=form, period=period, result=result
    )


@app.route("/pricelists/<int:period_id>/import/template.csv")
@login_required
@license_required
@require_perm("pricelists", "import")
def price_list_import_template(period_id):
    period = PriceListPeriod.query.get_or_404(period_id)
    return Response(
        pricelist_importer.build_template_csv(period),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename=pricelist_{period.id}_template.csv"},
    )


@app.route("/pricelists/<int:period_id>/export.csv")
@login_required
@license_required
@require_perm("pricelists", "import")
def price_list_export_csv(period_id):
    period = PriceListPeriod.query.get_or_404(period_id)
    return Response(
        pricelist_importer.build_export_csv(period),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename=pricelist_{period.id}.csv"},
    )


@app.route("/pricelists/lookup", methods=["GET", "POST"])
@login_required
@license_required
@require_perm("pricelists", "view")
def price_lookup():
    """'What was the price on this date?' — the historical pricing lookup."""
    form = PriceLookupForm()
    form.customer_id.choices = [
        (c.id, f"{c.name}" + (f" [{c.division.code}]" if c.division else " [no division]"))
        for c in Customer.query.order_by(Customer.name).all()
    ]
    form.product_id.choices = [
        (p.id, f"{p.name}" + (f" ({p.product_code})" if p.product_code else ""))
        for p in Product.query.order_by(Product.name).all()
    ]

    result = None
    if form.validate_on_submit():
        customer = Customer.query.get(form.customer_id.data)
        product = Product.query.get(form.product_id.data)
        price, period = get_price_for(form.lookup_date.data, form.customer_id.data, form.product_id.data)
        result = {
            "date": form.lookup_date.data,
            "customer": customer,
            "product": product,
            "period": period,
            "price": price,
            "fallback": product.price if product else None,
        }

    if request.method == "GET":
        form.lookup_date.data = date.today()

    return render_template("price_lookup.html", form=form, result=result)


def _apply_product_form(product, form):
    """Copy validated ProductForm data onto a Product (create or edit)."""
    product.name = form.name.data.strip()
    product.product_code = (form.product_code.data or "").strip() or None
    product.supplier_code = (form.supplier_code.data or "").strip() or None
    product.simplified_code = (form.simplified_code.data or "").strip() or None
    product.supplier_description = (form.supplier_description.data or "").strip() or None
    # "" == "— None —"
    product.grade = form.grade.data or None
    product.drawing_level = (form.drawing_level.data or "").strip() or None
    product.price = form.price.data
    product.weight = form.weight.data
    product.is_stock_item = bool(form.is_stock_item.data)
    product.active = bool(form.active.data)
    # 0 == "— None —"
    product.customer_id = form.customer_id.data or None
    # Many-to-many "also linked" customers
    if form.linked_customers.data:
        product.linked_customers = Customer.query.filter(
            Customer.id.in_(form.linked_customers.data)
        ).all()
    else:
        product.linked_customers = []
    # Many-to-many departments
    if form.departments.data:
        product.departments = Department.query.filter(
            Department.id.in_(form.departments.data)
        ).all()
    else:
        product.departments = []


def _populate_customer_choices(form):
    customers = Customer.query.order_by(Customer.name).all()
    form.customer_id.choices = [(0, "— None —")] + [(c.id, c.name) for c in customers]
    form.linked_customers.choices = [(c.id, c.name) for c in customers]


# Departments a product may be worked in — the only options on the Product
# form's Department picker. The rest of the department catalogue (used by
# Personnel, Stocktake and Users) is left untouched. Match is on name, upper-
# cased, so it survives the "UNIT 1" vs "Unit 1" casing in the data.
PRODUCT_DEPARTMENT_NAMES = ["HDA", "UNIT 1", "UNIT 2", "UNIT 3", "FLOOR MOULDING"]


def _populate_department_choices(form):
    order = {name: i for i, name in enumerate(PRODUCT_DEPARTMENT_NAMES)}
    departments = [
        d for d in Department.query.all()
        if (d.name or "").strip().upper() in order
    ]
    # Keep the intended order: HDA, Unit 1-3, Floor Moulding.
    departments.sort(key=lambda d: order.get((d.name or "").strip().upper(), 999))
    form.departments.choices = [(d.id, d.name) for d in departments]


@app.route("/products", methods=["GET", "POST"])
@login_required
@license_required
@require_perm("products", "view")
def products_crud():
    form = ProductForm()
    _populate_customer_choices(form)
    _populate_department_choices(form)
    may_edit = user_can(current_user, "products", "edit")

    if form.validate_on_submit() and not may_edit:
        flash("You do not have permission to add products.", "danger")
    elif form.validate_on_submit():
        product = Product()
        _apply_product_form(product, form)
        db.session.add(product)
        db.session.commit()
        flash(f"Product '{product.name}' added successfully (ID {product.id}).", "success")
        return redirect(url_for("products_crud"))

    # Search + paginate — was a single unpaginated table of every product.
    search = request.args.get("q", "").strip()
    page = request.args.get("page", 1, type=int)

    products_query = Product.query
    if search:
        like = f"%{search}%"
        products_query = products_query.filter(db.or_(
            Product.name.ilike(like),
            Product.product_code.ilike(like),
            Product.supplier_code.ilike(like),
            Product.simplified_code.ilike(like),
        ))
    products_page = products_query.order_by(Product.name).paginate(
        page=page, per_page=50, error_out=False
    )
    products = products_page.items
    total_products = Product.query.count()
    active_count = Product.query.filter(Product.active.isnot(False)).count()
    stock_count = Product.query.filter(Product.is_stock_item.isnot(False)).count()
    priced_count = Product.query.filter(Product.price.isnot(None)).count()

    # Display-only columns. Batched here rather than read off each product so
    # the table doesn't fire a price lookup per row.
    current_prices = get_current_prices_for_products(products)
    prices_per_kg = {
        p.id: compute_price_per_kg(current_prices.get(p.id, p.price), p.weight)
        for p in products
    }

    return render_template(
        "products.html",
        form=form,
        products=products_page,
        search=search,
        total_products=total_products,
        active_count=active_count,
        stock_count=stock_count,
        priced_count=priced_count,
        current_prices=current_prices,
        prices_per_kg=prices_per_kg,
        may_edit=may_edit
    )


PRODUCT_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}


def _save_product_images(product):
    """Persist any files posted under the "product_images" input.

    Stored under a generated name (product id + random hex) rather than the
    original filename, so re-uploading a photo taken straight off a phone
    ("IMG_0001.jpg") never collides with another product's.
    """
    for file in request.files.getlist("product_images"):
        if not file or not file.filename:
            continue
        ext = os.path.splitext(file.filename)[1].lower()
        if ext not in PRODUCT_IMAGE_EXTENSIONS:
            flash(f"Skipped '{file.filename}' — not a supported image type.", "warning")
            continue
        stored_name = f"{product.id}_{uuid.uuid4().hex}{ext}"
        file.save(os.path.join(PRODUCT_IMAGE_DIR, secure_filename(stored_name)))
        db.session.add(ProductImage(product_id=product.id, filename=stored_name))


def _save_product_ppap(product):
    """Persist a single PDF posted under the "product_ppap" input.

    Replaces any PPAP file already on the product — the old file is removed
    from disk so uploads don't accumulate orphaned PDFs.
    """
    file = request.files.get("product_ppap")
    if not file or not file.filename:
        return
    ext = os.path.splitext(file.filename)[1].lower()
    if ext != ".pdf":
        flash(f"Skipped '{file.filename}' — PPAP must be a PDF file.", "warning")
        return
    if product.ppap_filename:
        old_path = os.path.join(PRODUCT_PPAP_DIR, product.ppap_filename)
        if os.path.exists(old_path):
            os.remove(old_path)
    stored_name = f"{product.id}_{uuid.uuid4().hex}{ext}"
    file.save(os.path.join(PRODUCT_PPAP_DIR, secure_filename(stored_name)))
    product.ppap_filename = stored_name
    product.ppap_uploaded_at = datetime.now()


@app.route("/products/<int:product_id>/edit", methods=["GET", "POST"])
@login_required
@license_required
@require_perm("products", "edit")
def edit_product(product_id):
    product = Product.query.get_or_404(product_id)
    form = ProductForm(obj=product)
    _populate_customer_choices(form)
    _populate_department_choices(form)

    if form.validate_on_submit():
        _apply_product_form(product, form)
        _save_product_images(product)
        _save_product_ppap(product)
        db.session.commit()
        flash(f"Product '{product.name}' updated successfully (ID {product.id}).", "success")
        return redirect(url_for("edit_product", product_id=product.id))

    # Pre-select current customers / departments / grade on GET
    if request.method == "GET":
        form.customer_id.data = product.customer_id or 0
        form.linked_customers.data = [c.id for c in product.linked_customers]
        form.departments.data = [d.id for d in product.departments]
        form.grade.data = product.grade or ""

    current_price, price_period = (None, None)
    if product.customer_id:
        current_price, price_period = get_price_for(
            date.today(), product.customer_id, product.id
        )

    return render_template(
        "edit_product.html",
        form=form,
        product=product,
        current_price=current_price,
        price_period=price_period,
        price_per_kg=compute_price_per_kg(
            current_price if current_price is not None else product.price,
            product.weight
        ),
    )


@app.route("/products/<int:product_id>/images/<int:image_id>/delete", methods=["POST"])
@login_required
@license_required
@require_perm("products", "edit")
def delete_product_image(product_id, image_id):
    image = ProductImage.query.filter_by(id=image_id, product_id=product_id).first_or_404()
    path = os.path.join(PRODUCT_IMAGE_DIR, image.filename)
    if os.path.exists(path):
        os.remove(path)
    db.session.delete(image)
    db.session.commit()
    flash("Image removed.", "success")
    return redirect(url_for("edit_product", product_id=product_id))


@app.route("/products/<int:product_id>/ppap/delete", methods=["POST"])
@login_required
@license_required
@require_perm("products", "edit")
def delete_product_ppap(product_id):
    product = Product.query.get_or_404(product_id)
    if product.ppap_filename:
        path = os.path.join(PRODUCT_PPAP_DIR, product.ppap_filename)
        if os.path.exists(path):
            os.remove(path)
        product.ppap_filename = None
        product.ppap_uploaded_at = None
        db.session.commit()
        flash("PPAP document removed.", "success")
    return redirect(url_for("edit_product", product_id=product_id))

# ══════════════════════════════════════════════════════════════
#  PRODUCTS — CSV / EXCEL IMPORT
# ══════════════════════════════════════════════════════════════
@app.route("/products/import", methods=["GET", "POST"])
@login_required
@license_required
@require_perm("products", "import")
def import_products_view():
    form = ProductImportForm()
    result = None

    if form.validate_on_submit():
        try:
            result = import_products(form.file.data)
        except ValueError as exc:
            flash(f"Import failed: {exc}", "danger")
            return redirect(url_for("import_products_view"))

        if result.touched:
            flash(
                f"{result.created} product(s) added, {result.updated} updated"
                + (f", {result.skipped} row(s) skipped." if result.skipped else "."),
                "success"
            )
        else:
            flash("Nothing was imported — no usable rows in that file.", "warning")

        if result.unknown_columns:
            flash(
                "Columns ignored (not part of the template): "
                + ", ".join(result.unknown_columns),
                "info"
            )

    return render_template(
        "product_import.html", form=form, result=result, grades=PRODUCT_GRADES
    )


@app.route("/products/import/template.csv")
@login_required
@license_required
@require_perm("products", "import")
def product_import_template():
    """The blank import sheet, with two worked example rows."""
    return Response(
        build_template_csv(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=product_import_template.csv"},
    )


@app.route("/products/export.csv")
@login_required
@license_required
@require_perm("products", "import")
def product_export_csv():
    """The current list in template layout — edit it and send it back."""
    return Response(
        build_export_csv(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=products.csv"},
    )


@app.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("login"))


@app.route("/license/upload", methods=["GET", "POST"])
def upload_license():
    form = LicenseUploadForm()
    if form.validate_on_submit():
        file = form.file.data
        filename = secure_filename("license.json")  # always save as license.json
        save_path = os.path.join(WINDOWS_LICENSE_DIR, filename)
        file.save(save_path)

        # Optional: validate immediately
        valid, msg = validate_license()
        if not valid:
            flash(f"Uploaded license invalid: {msg}", "danger")
            return redirect(url_for("upload_license"))
        else:
            flash("License uploaded and validated successfully!", "success")
            return redirect(url_for("dashboard"))

    return render_template("license_upload.html", form=form)

@app.route("/users/new", methods=["GET", "POST"])
@login_required
@require_perm("users", "edit")
def create_user():

    Role       = db.Model.registry._class_registry.get("Role")
    Division   = db.Model.registry._class_registry.get("Division")
    Department = db.Model.registry._class_registry.get("Department")
    User       = db.Model.registry._class_registry.get("User")

    form = CreateUserForm()

    # Populate select choices
    form.role_id.choices       = [(r.id, r.name) for r in Role.query.order_by(Role.name).all()]
    form.division_id.choices   = [(0, "— None —")] + [(d.id, f"{d.code} — {d.name}") for d in Division.query.order_by(Division.name).all()]
    form.department_id.choices = [(0, "— None —")] + [(d.id, d.name) for d in Department.query.order_by(Department.name).all()]

    if form.validate_on_submit():
        # Check for duplicate username
        if User.query.filter_by(username=form.username.data.strip()).first():
            flash("Username already exists.", "danger")
            return render_template("create_user.html", form=form)

        user = User(
            name          = form.name.data.strip(),
            username      = form.username.data.strip().lower(),
            role_id       = form.role_id.data,
            division_id   = form.division_id.data   or None,
            department_id = form.department_id.data or None,
            active        = form.active.data,
        )
        user.set_password(form.password.data)
        db.session.add(user)
        db.session.commit()

        flash(f"User '{user.name}' created successfully.", "success")
        return redirect(url_for("list_users"))

    return render_template("create_user.html", form=form)

@app.route("/users")
@login_required
@require_perm("users", "view")
def list_users():
    from models import User
    
    search = request.args.get("search", "").strip()
    role_filter = request.args.get("role_id", type=int)

    query = User.query

    if search:
        query = query.filter(
            or_(
                User.name.ilike(f"%{search}%"),
                User.username.ilike(f"%{search}%"),
            )
        )
    if role_filter:
        query = query.filter(User.role_id == role_filter)

    users = query.order_by(User.name).all()
    roles = Role.query.order_by(Role.name).all()

    return render_template("list_users.html",
                           users=users,
                           roles=roles,
                           search=search,
                           role_filter=role_filter)

@app.route("/users/<int:user_id>/edit", methods=["GET", "POST"])
@login_required
@require_perm("users", "edit")
def edit_user(user_id):
    from models import User, Role, Division, Department
    from app import db

    user = User.query.get_or_404(user_id)

    form = EditUserForm(obj=user)

    form.role_id.choices       = [(r.id, r.name) for r in Role.query.order_by(Role.name).all()]
    form.division_id.choices   = [(0, "— None —")] + [(d.id, f"{d.code} — {d.name}") for d in Division.query.order_by(Division.name).all()]
    form.department_id.choices = [(0, "— None —")] + [(d.id, d.name) for d in Department.query.order_by(Department.name).all()]

    if form.validate_on_submit():
        # Check username not taken by another user
        existing = User.query.filter_by(username=form.username.data.strip()).first()
        if existing and existing.id != user.id:
            flash("Username already taken.", "danger")
            return render_template("edit_user.html", form=form, user=user)

        user.name          = form.name.data.strip()
        user.username      = form.username.data.strip().lower()
        user.role_id       = form.role_id.data
        user.division_id   = form.division_id.data   or None
        user.department_id = form.department_id.data or None
        user.active        = form.active.data

        # Only update password if a new one was provided
        if form.password.data:
            if form.password.data != form.confirm.data:
                flash("Passwords do not match.", "danger")
                return render_template("edit_user.html", form=form, user=user)
            user.set_password(form.password.data)

        db.session.commit()
        flash(f"User '{user.name}' updated successfully.", "success")
        return redirect(url_for("list_users"))

    # Pre-select current division/department (handle None → 0)
    if request.method == "GET":
        form.division_id.data   = user.division_id   or 0
        form.department_id.data = user.department_id or 0

    return render_template("edit_user.html", form=form, user=user)


# ══════════════════════════════════════════════════════════════
# USER ROLES
# ══════════════════════════════════════════════════════════════
# Roles label users by job function. What each account may actually do is set
# per user on the Access Control screen — a role carries no permissions of its
# own. The one exception is "admin", which bypasses every check (see
# access/guards), so it cannot be renamed or deleted here.

_ADMIN_ROLE_NAMES = ("admin", "administrator")


@app.route("/users/roles", methods=["GET", "POST"])
@login_required
@require_perm("users", "edit")
def manage_roles():
    form = RoleForm()
    if form.validate_on_submit():
        name = form.name.data.strip()
        if Role.query.filter(func.lower(Role.name) == name.lower()).first():
            flash(f"A role called '{name}' already exists.", "warning")
        else:
            db.session.add(Role(name=name))
            db.session.commit()
            flash(f"Role '{name}' added.", "success")
        return redirect(url_for("manage_roles"))

    roles = Role.query.order_by(Role.name).all()
    user_counts = dict(
        db.session.query(User.role_id, func.count(User.id))
        .group_by(User.role_id)
        .all()
    )
    return render_template(
        "manage_roles.html",
        form=form,
        roles=roles,
        user_counts=user_counts,
    )


@app.route("/users/roles/<int:role_id>/rename", methods=["POST"])
@login_required
@require_perm("users", "edit")
def rename_role(role_id):
    role = Role.query.get_or_404(role_id)
    if role.name.lower() in _ADMIN_ROLE_NAMES:
        flash("The admin role cannot be renamed — it controls system access.", "warning")
        return redirect(url_for("manage_roles"))

    name = (request.form.get("name") or "").strip()
    if not name:
        flash("A role needs a name.", "warning")
    elif Role.query.filter(func.lower(Role.name) == name.lower(), Role.id != role.id).first():
        flash(f"A role called '{name}' already exists.", "warning")
    else:
        role.name = name
        db.session.commit()
        flash("Role renamed.", "success")
    return redirect(url_for("manage_roles"))


@app.route("/users/roles/<int:role_id>/delete", methods=["POST"])
@login_required
@require_perm("users", "edit")
def delete_role(role_id):
    role = Role.query.get_or_404(role_id)
    if role.name.lower() in _ADMIN_ROLE_NAMES:
        flash("The admin role cannot be deleted.", "warning")
        return redirect(url_for("manage_roles"))

    # Role → users cascades delete, so a role still in use must not be removed:
    # move its users to another role first.
    in_use = User.query.filter_by(role_id=role.id).count()
    if in_use:
        flash(
            f"'{role.name}' still has {in_use} user(s). Move them to another role "
            "first — deleting a role would delete its users.",
            "danger",
        )
        return redirect(url_for("manage_roles"))

    name = role.name
    db.session.delete(role)
    db.session.commit()
    flash(f"Role '{name}' deleted.", "success")
    return redirect(url_for("manage_roles"))


# ══════════════════════════════════════════════════════════════
# ORG STRUCTURE  (divisions and departments)
# ══════════════════════════════════════════════════════════════
# Until now departments only ever appeared as dropdown choices — the only way
# to create one was the personnel import, which silently invents any name it
# does not recognise. These screens let that be cleaned up by hand: rename a
# typo, move a department to the right division, fold a duplicate into the
# real one, or delete one that was never used.


def _org_models():
    """StocktakeHeader lives in a blueprint that loads after this module."""
    from stocktake.models import StocktakeHeader
    return StocktakeHeader


def _department_usage(department):
    """Everything pointing at a department — what blocks a delete."""
    StocktakeHeader = _org_models()
    return {
        "personnel":  Personnel.query.filter_by(department_id=department.id).count(),
        "users":      User.query.filter_by(department_id=department.id).count(),
        "stocktakes": StocktakeHeader.query.filter_by(department_id=department.id).count(),
        "overtime":   OvertimeRequest.query.filter_by(department_id=department.id).count(),
    }


def _division_usage(division):
    """Everything pointing at a division — what blocks a delete."""
    return {
        "departments":   Department.query.filter_by(division_id=division.id).count(),
        "personnel":     Personnel.query.filter_by(division_id=division.id).count(),
        "users":         User.query.filter_by(division_id=division.id).count(),
        "customers":     Customer.query.filter_by(division_id=division.id).count(),
        "price_periods": PriceListPeriod.query.filter_by(division_id=division.id).count(),
    }


def _usage_summary(usage):
    """'3 personnel, 1 user' — the parts of a usage dict that are non-zero."""
    labels = {
        "departments":   ("department", "departments"),
        "personnel":     ("personnel record", "personnel records"),
        "users":         ("user", "users"),
        "stocktakes":    ("stocktake", "stocktakes"),
        "overtime":      ("overtime request", "overtime requests"),
        "customers":     ("customer", "customers"),
        "price_periods": ("price list period", "price list periods"),
    }
    parts = []
    for key, count in usage.items():
        if not count:
            continue
        singular, plural = labels.get(key, (key, key))
        parts.append(f"{count} {singular if count == 1 else plural}")
    return ", ".join(parts)


def _populate_department_division_choices(form):
    divisions = Division.query.order_by(Division.code).all()
    form.division_id.choices = [(d.id, f"{d.code} — {d.name}") for d in divisions]
    return divisions


@app.route("/org")
@login_required
@license_required
@require_perm("org", "view")
def org_structure():
    StocktakeHeader = _org_models()

    divisions   = Division.query.order_by(Division.code).all()
    departments = Department.query.order_by(Department.name).all()

    # One grouped query per dependency beats a count per department in a loop.
    def _counts(model, column):
        return dict(
            db.session.query(column, func.count(model.id)).group_by(column).all()
        )

    personnel_counts  = _counts(Personnel, Personnel.department_id)
    user_counts       = _counts(User, User.department_id)
    stocktake_counts  = _counts(StocktakeHeader, StocktakeHeader.department_id)
    overtime_counts   = _counts(OvertimeRequest, OvertimeRequest.department_id)

    by_division = {d.id: [] for d in divisions}
    orphans = []
    for dept in departments:
        row = {
            "dept":       dept,
            "personnel":  personnel_counts.get(dept.id, 0),
            "users":      user_counts.get(dept.id, 0),
            "stocktakes": stocktake_counts.get(dept.id, 0),
            "overtime":   overtime_counts.get(dept.id, 0),
        }
        row["in_use"] = any(row[k] for k in ("personnel", "users", "stocktakes", "overtime"))
        by_division.get(dept.division_id, orphans).append(row)

    # A department name used in more than one division is usually an import typo
    name_tally = {}
    for dept in departments:
        name_tally.setdefault(dept.name.strip().lower(), []).append(dept)
    duplicate_names = sorted(name for name, rows in name_tally.items() if len(rows) > 1)

    return render_template(
        "org.html",
        divisions=divisions,
        by_division=by_division,
        orphans=orphans,
        department_count=len(departments),
        personnel_total=sum(personnel_counts.values()),
        duplicate_names=duplicate_names,
        may_edit=user_can(current_user, "org", "edit"),
    )


# ── Divisions ────────────────────────────────────────────────────────────────

@app.route("/org/divisions/new", methods=["GET", "POST"])
@app.route("/org/divisions/<int:division_id>/edit", methods=["GET", "POST"])
@login_required
@license_required
@require_perm("org", "edit")
def org_division_form(division_id=None):
    division = Division.query.get_or_404(division_id) if division_id else None
    form = DivisionForm(obj=division)

    if form.validate_on_submit():
        code = form.code.data.strip().upper()
        name = form.name.data.strip()

        clash = Division.query.filter(
            func.lower(Division.code) == code.lower(),
            Division.id != (division.id if division else 0)
        ).first()
        if clash:
            flash(f"Division code '{code}' is already used by '{clash.name}'.", "warning")
        else:
            if division is None:
                division = Division(code=code, name=name)
                db.session.add(division)
                db.session.commit()
                flash(f"Division '{code} — {name}' created.", "success")
            else:
                division.code = code
                division.name = name
                db.session.commit()
                flash(f"Division '{code} — {name}' updated.", "success")
            return redirect(url_for("org_structure"))

    return render_template(
        "org_division_form.html",
        form=form,
        division=division,
        usage=_division_usage(division) if division else None,
    )


@app.route("/org/divisions/<int:division_id>/delete", methods=["POST"])
@login_required
@license_required
@require_perm("org", "edit")
def org_division_delete(division_id):
    division = Division.query.get_or_404(division_id)
    usage = _division_usage(division)

    # Division.departments and .personnel cascade delete-orphan, so a division
    # deleted while still in use would take live records with it.
    if any(usage.values()):
        flash(
            f"'{division.code} — {division.name}' still has {_usage_summary(usage)}. "
            "Move or delete those first.",
            "danger",
        )
        return redirect(url_for("org_division_form", division_id=division.id))

    label = f"{division.code} — {division.name}"
    db.session.delete(division)
    db.session.commit()
    flash(f"Division '{label}' deleted.", "success")
    return redirect(url_for("org_structure"))


# ── Departments ──────────────────────────────────────────────────────────────

@app.route("/org/departments/new", methods=["GET", "POST"])
@app.route("/org/departments/<int:department_id>/edit", methods=["GET", "POST"])
@login_required
@license_required
@require_perm("org", "edit")
def org_department_form(department_id=None):
    department = Department.query.get_or_404(department_id) if department_id else None
    form = DepartmentForm(obj=department)
    divisions = _populate_department_division_choices(form)

    if not divisions:
        flash("Create a division first — every department must belong to one.", "warning")
        return redirect(url_for("org_structure"))

    # Pre-select the division when arriving from a division's "Add" button
    if request.method == "GET" and department is None:
        form.division_id.data = request.args.get("division_id", type=int) or divisions[0].id

    if form.validate_on_submit():
        name = form.name.data.strip()
        division = Division.query.get(form.division_id.data)

        if division is None:
            flash("Please choose a valid division.", "danger")
        else:
            # Names only have to be unique inside a division — two divisions
            # may each legitimately run a "Fettling" department.
            clash = Department.query.filter(
                func.lower(Department.name) == name.lower(),
                Department.division_id == division.id,
                Department.id != (department.id if department else 0)
            ).first()
            if clash:
                flash(f"'{division.code}' already has a department called '{clash.name}'.", "warning")
            elif department is None:
                department = Department(name=name, division_id=division.id)
                db.session.add(department)
                db.session.commit()
                flash(f"Department '{name}' created under {division.code}.", "success")
                return redirect(url_for("org_structure"))
            else:
                moved = 0
                if department.division_id != division.id:
                    # Personnel and users carry their own division_id; keep it
                    # in step or they end up filed under the old division.
                    moved += Personnel.query.filter_by(department_id=department.id).update(
                        {"division_id": division.id}, synchronize_session=False)
                    moved += User.query.filter_by(department_id=department.id).update(
                        {"division_id": division.id}, synchronize_session=False)
                    moved += OvertimeRequest.query.filter_by(department_id=department.id).update(
                        {"division_id": division.id}, synchronize_session=False)

                department.name = name
                department.division_id = division.id
                db.session.commit()

                message = f"Department '{name}' updated."
                if moved:
                    message += f" {moved} linked record{'s' if moved != 1 else ''} moved to {division.code}."
                flash(message, "success")
                return redirect(url_for("org_structure"))

    usage = _department_usage(department) if department else None

    # Merge targets — any other department, so a duplicate can be folded into
    # the real one even when it was created under the wrong division.
    merge_targets = []
    if department:
        merge_targets = (
            Department.query
            .filter(Department.id != department.id)
            .order_by(Department.name)
            .all()
        )

    return render_template(
        "org_department_form.html",
        form=form,
        department=department,
        usage=usage,
        usage_summary=_usage_summary(usage) if usage else "",
        merge_targets=merge_targets,
    )


@app.route("/org/departments/<int:department_id>/delete", methods=["POST"])
@login_required
@license_required
@require_perm("org", "edit")
def org_department_delete(department_id):
    department = Department.query.get_or_404(department_id)
    usage = _department_usage(department)

    # Department.personnel cascades delete-orphan — deleting one still in use
    # would take its personnel records with it.
    if any(usage.values()):
        flash(
            f"'{department.name}' is still used by {_usage_summary(usage)}. "
            "Merge it into another department instead, or move those records first.",
            "danger",
        )
        return redirect(url_for("org_department_form", department_id=department.id))

    name = department.name
    db.session.delete(department)
    db.session.commit()
    flash(f"Department '{name}' deleted.", "success")
    return redirect(url_for("org_structure"))


@app.route("/org/departments/<int:department_id>/merge", methods=["POST"])
@login_required
@license_required
@require_perm("org", "edit")
def org_department_merge(department_id):
    StocktakeHeader = _org_models()

    source = Department.query.get_or_404(department_id)
    target = Department.query.get(request.form.get("target_id", type=int) or 0)

    if target is None or target.id == source.id:
        flash("Choose a different department to merge into.", "danger")
        return redirect(url_for("org_department_form", department_id=source.id))

    source_name, target_name = source.name, target.name
    target_division_id = target.division_id

    moved = 0
    moved += Personnel.query.filter_by(department_id=source.id).update(
        {"department_id": target.id, "division_id": target_division_id}, synchronize_session=False)
    moved += User.query.filter_by(department_id=source.id).update(
        {"department_id": target.id, "division_id": target_division_id}, synchronize_session=False)
    moved += StocktakeHeader.query.filter_by(department_id=source.id).update(
        {"department_id": target.id}, synchronize_session=False)
    moved += OvertimeRequest.query.filter_by(department_id=source.id).update(
        {"department_id": target.id, "division_id": target_division_id}, synchronize_session=False)
    db.session.commit()

    # Re-read before deleting: the bulk updates above bypassed the session, so
    # a stale Department.personnel collection could cascade-delete live rows.
    db.session.expire_all()
    source = Department.query.get(department_id)
    remaining = _department_usage(source)
    if any(remaining.values()):
        flash(
            f"Moved {moved} record(s) to '{target_name}', but '{source_name}' still has "
            f"{_usage_summary(remaining)} — it was left in place.",
            "warning",
        )
        return redirect(url_for("org_department_form", department_id=source.id))

    db.session.delete(source)
    db.session.commit()
    flash(
        f"'{source_name}' merged into '{target_name}' — "
        f"{moved} record{'s' if moved != 1 else ''} moved.",
        "success",
    )
    return redirect(url_for("org_structure"))