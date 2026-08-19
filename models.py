from datetime import datetime, date
from decimal import Decimal
from app import db
from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash
from overtime.models import *


# ======================================================
# USERS
# ======================================================
class User(UserMixin, db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(50), unique=True, nullable=False)
    name = db.Column(db.String(100), nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)

    role_id = db.Column(db.Integer, db.ForeignKey("roles.id"), nullable=False)
    department_id = db.Column(db.Integer, db.ForeignKey("departments.id"))
    division_id = db.Column(db.Integer, db.ForeignKey("divisions.id"))

    active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.now)
    last_login = db.Column(db.DateTime)

    # relationships
    role = db.relationship("Role", back_populates="users")
    department = db.relationship("Department", back_populates="users")
    division = db.relationship("Division", back_populates="users")

    entries = db.relationship(
        "FettlingEntry",
        back_populates="user",
        cascade="all, delete-orphan"
    )

    # Permission relationship (physical join model)
    user_permissions = db.relationship(
        "UserPermission",
        foreign_keys="UserPermission.user_id",
        back_populates="user",
        cascade="all, delete-orphan"
    )

    managed_personnel = db.relationship(
        "PersonnelManager",
        back_populates="manager",
        cascade="all, delete-orphan"
    )

    # password helpers
    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

    def has_permission(self, module, action):
        """Check if this user has a specific permission."""
        return any(
            up.permission.module == module and up.permission.action == action
            for up in self.user_permissions
        )

    def __repr__(self):
        return f"<User {self.name}>"
    @property
    def is_admin(self):
        return self.role is not None and self.role.name.lower() in ('admin', 'administrator')

# ======================================================
# ROLES
# ======================================================
class Role(db.Model):
    __tablename__ = "roles"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(50), unique=True, nullable=False)

    users = db.relationship(
        "User",
        back_populates="role",
        cascade="all, delete"
    )

    def __repr__(self):
        return f"<Role {self.name}>"


# ======================================================
# PERMISSIONS  (seed table — one row per module/action combo)
# ======================================================
class Permission(db.Model):
    __tablename__ = "permissions"

    id     = db.Column(db.Integer, primary_key=True)
    module = db.Column(db.String(50), nullable=False)   # e.g. 'overtime', 'procurement'
    action = db.Column(db.String(50), nullable=False)   # e.g. 'view', 'request', 'approve', 'admin'
    label  = db.Column(db.String(120))                  # Human-readable label for admin UI

    __table_args__ = (
        db.UniqueConstraint("module", "action", name="uq_permission_module_action"),
    )

    # back-reference to assignments
    user_permissions = db.relationship(
        "UserPermission",
        back_populates="permission",
        cascade="all, delete-orphan"
    )

    def __repr__(self):
        return f"<Permission {self.module}.{self.action}>"


# ======================================================
# USER PERMISSIONS  (physical many-to-many join table)
# ======================================================
class UserPermission(db.Model):
    __tablename__ = "user_permissions"

    id            = db.Column(db.Integer, primary_key=True)
    user_id       = db.Column(db.Integer, db.ForeignKey("users.id"),       nullable=False)
    permission_id = db.Column(db.Integer, db.ForeignKey("permissions.id"), nullable=False)
    granted_by    = db.Column(db.Integer, db.ForeignKey("users.id"),       nullable=True)
    granted_at    = db.Column(db.DateTime, default=datetime.now)

    __table_args__ = (
        db.UniqueConstraint("user_id", "permission_id", name="uq_user_permission"),
    )

    # relationships
    user       = db.relationship("User",       foreign_keys=[user_id],    back_populates="user_permissions")
    permission = db.relationship("Permission", foreign_keys=[permission_id], back_populates="user_permissions")
    grantor    = db.relationship("User",       foreign_keys=[granted_by])

    def __repr__(self):
        return f"<UserPermission user={self.user_id} perm={self.permission_id}>"


# ======================================================
# PRODUCT ↔ CUSTOMER  (many-to-many "also linked to")
# ======================================================
# A product has ONE primary customer (Product.customer_id, kept for
# Fettling/Stocktake reporting) and may ALSO be linked to any number of
# additional customers through this association table.
product_customers = db.Table(
    "product_customers",
    db.Column("product_id", db.Integer,
              db.ForeignKey("products.id", ondelete="CASCADE"), primary_key=True),
    db.Column("customer_id", db.Integer,
              db.ForeignKey("customers.id", ondelete="CASCADE"), primary_key=True),
)


# ======================================================
# PRODUCT ↔ DEPARTMENT  (many-to-many)
# ======================================================
# A product may be worked in any number of departments (e.g. a casting that
# passes through both Fettling and Machining).
product_departments = db.Table(
    "product_departments",
    db.Column("product_id", db.Integer,
              db.ForeignKey("products.id", ondelete="CASCADE"), primary_key=True),
    db.Column("department_id", db.Integer,
              db.ForeignKey("departments.id", ondelete="CASCADE"), primary_key=True),
)


# ======================================================
# CUSTOMERS
# ======================================================
class Customer(db.Model):
    __tablename__ = "customers"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)

    # Customer's own reference code (their account/customer number), not ours.
    code = db.Column(db.String(50))

    # Which division this customer trades under (HDC / HDA).
    # Determines which price-list calendar applies to them.
    division_id = db.Column(db.Integer, db.ForeignKey("divisions.id"))
    division = db.relationship("Division", back_populates="customers")

    # Inactive customers stay on every record that already references them —
    # history is never rewritten — but can be hidden from "add new" pickers.
    active = db.Column(db.Boolean, default=True, nullable=False,
                       server_default=db.text("true"))

    # Primary (one-to-many) — products whose primary customer is this one
    products = db.relationship(
        "Product",
        back_populates="customer",
        cascade="all, delete-orphan"
    )

    # Many-to-many — products additionally linked to this customer
    linked_products = db.relationship(
        "Product",
        secondary=product_customers,
        back_populates="linked_customers"
    )

    def __repr__(self):
        return f"<Customer {self.name}>"


# ======================================================
# PRODUCTS
# ======================================================
# Material grades a product can be cast in.
PRODUCT_GRADES = ["GG30", "GG25", "SG50","SG70", "SG40", "SG60"]


class Product(db.Model):
    __tablename__ = "products"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)

    # Supplier / catalogue details
    product_code = db.Column(db.String(50))
    supplier_code = db.Column(db.String(50))
    # Short form of the product code with the leading part number dropped
    # (e.g. "1203 VB2 00" → "VB2 00"). Typed by hand; also indexed by the scrap
    # importer so a reject row carrying only the short code still links.
    simplified_code = db.Column(db.String(50))
    supplier_description = db.Column(db.String(255))
    grade = db.Column(db.String(10))
    # Drawing revision level, e.g. "AA", "OOO", "AB" — HDA's dispatch capture
    # snapshots this onto each cage line at dispatch time.
    drawing_level = db.Column(db.String(10))
    price = db.Column(db.Numeric(10, 2))

    # Cast weight in kilograms — the divisor behind price-per-kg.
    weight = db.Column(db.Numeric(10, 3))

    # Primary customer (used by Fettling & Stocktake reporting)
    customer_id = db.Column(
        db.Integer,
        db.ForeignKey("customers.id")
    )

    barcode = db.Column(db.String(25))
    stockamount = db.Column(db.Integer, default=0)

    # Is this product held as stock (counted in stocktakes), or made to order?
    is_stock_item = db.Column(db.Boolean, default=True, nullable=False,
                              server_default=db.text("true"))

    # Inactive products drop out of the product pickers but stay on every
    # record that already references them — history is never rewritten.
    active = db.Column(db.Boolean, default=True, nullable=False,
                       server_default=db.text("true"))

    # PPAP document — one PDF per product (stored filename under
    # static/documents/ppap/, never the original upload name).
    ppap_filename = db.Column(db.String(255))
    ppap_uploaded_at = db.Column(db.DateTime)

    customer = db.relationship("Customer", back_populates="products")

    # Additional customers linked via many-to-many
    linked_customers = db.relationship(
        "Customer",
        secondary=product_customers,
        back_populates="linked_products"
    )

    # Departments this product is worked in (many-to-many)
    departments = db.relationship(
        "Department",
        secondary=product_departments,
        back_populates="products",
        order_by="Department.name"
    )

    entries = db.relationship(
        "FettlingEntry",
        back_populates="product",
        cascade="all, delete-orphan"
    )

    images = db.relationship(
        "ProductImage",
        back_populates="product",
        cascade="all, delete-orphan",
        order_by="ProductImage.id"
    )

    # ---- Display-only derived values -------------------------------
    # These are never stored; they are worked out on the fly from the
    # price list and the cast weight. For a page listing many products,
    # use get_current_prices_for_products() instead — these properties
    # query per product.

    @property
    def current_price(self):
        """Today's price-list price for this product's primary customer."""
        if not self.customer_id:
            return None
        price, _ = get_price_for(date.today(), self.customer_id, self.id)
        return price

    @property
    def effective_price(self):
        """Price list price when there is one, otherwise the catalogue price."""
        current = self.current_price
        return current if current is not None else self.price

    @property
    def price_per_kg(self):
        return compute_price_per_kg(self.effective_price, self.weight)

    def __repr__(self):
        return f"<Product {self.name}>"


class ProductImage(db.Model):
    __tablename__ = "product_images"

    id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(
        db.Integer,
        db.ForeignKey("products.id"),
        nullable=False
    )
    # Stored filename on disk (static/images/products/<filename>) — never the
    # original upload name, so two products' "photo1.jpg" can't collide.
    filename = db.Column(db.String(255), nullable=False)
    uploaded_at = db.Column(db.DateTime, default=datetime.now)

    product = db.relationship("Product", back_populates="images")

    def __repr__(self):
        return f"<ProductImage {self.filename}>"


def compute_price_per_kg(price, weight):
    """Price ÷ weight, or None when either is missing (or the weight is zero)."""
    if price is None or not weight:
        return None
    return Decimal(price) / Decimal(weight)


def active_products():
    """
    The products that belong in a picker, by name.

    Deactivating a product only takes it off the pickers — entries, stocktake
    lines and price list rows that already point at it are left alone, so
    reports and history still read correctly. Rows predating the column have
    a NULL flag and count as active.
    """
    return (
        Product.query
        .filter(Product.active.isnot(False))
        .order_by(Product.name)
    )


# ======================================================
# DIVISIONS
# ======================================================
class Division(db.Model):
    __tablename__ = "divisions"

    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(10), unique=True, nullable=False)
    name = db.Column(db.String(100), nullable=False)

    departments = db.relationship(
        "Department",
        back_populates="division",
        cascade="all, delete-orphan"
    )

    personnel = db.relationship(
        "Personnel",
        back_populates="division",
        cascade="all, delete-orphan"
    )

    users = db.relationship(
        "User",
        back_populates="division"
    )

    customers = db.relationship(
        "Customer",
        back_populates="division"
    )

    price_periods = db.relationship(
        "PriceListPeriod",
        back_populates="division",
        cascade="all, delete-orphan"
    )

    def __repr__(self):
        return f"<Division {self.code}>"


# ======================================================
# DEPARTMENTS
# ======================================================
class Department(db.Model):
    __tablename__ = "departments"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)

    division_id = db.Column(
        db.Integer,
        db.ForeignKey("divisions.id"),
        nullable=False
    )

    division = db.relationship(
        "Division",
        back_populates="departments"
    )

    personnel = db.relationship(
        "Personnel",
        back_populates="department",
        cascade="all, delete-orphan"
    )

    users = db.relationship(
        "User",
        back_populates="department"
    )

    stocktakes = db.relationship(
        "StocktakeHeader",
        back_populates="department"
    )

    # Products worked in this department (many-to-many)
    products = db.relationship(
        "Product",
        secondary=product_departments,
        back_populates="departments"
    )

    def __repr__(self):
        return f"<Department {self.name}>"


# ======================================================
# PERSONNEL
# ======================================================
# Pay groups — which payroll group a person sits in. Kept as a short list so
# the dropdown, the importer and any grouping/reporting all agree.
PAY_GROUPS = ["Wages", "Salary"]

# Personnel icon tiles — "quick pick" icon + accent colour pairs, the same
# ones used for the module cards on the main dashboard. Selecting one sets
# both the icon and its colour together, in one click.
PERSONNEL_ICONS = [
    {"icon": "bi-tools",           "color": "#2563eb", "label": "Fettling"},
    {"icon": "bi-clipboard-data",  "color": "#0891b2", "label": "Daily Production"},
    {"icon": "bi-boxes",           "color": "#7c3aed", "label": "Stocktake"},
    {"icon": "bi-fire",            "color": "#b45309", "label": "Furnace"},
    {"icon": "bi-trash3",          "color": "#dc2626", "label": "Scrap"},
    {"icon": "bi-clock-history",   "color": "#d97706", "label": "Overtime"},
    {"icon": "bi-hdd-stack",       "color": "#059669", "label": "Assets"},
    {"icon": "bi-stopwatch",       "color": "#0369a1", "label": "Time Clock"},
    {"icon": "bi-people",          "color": "#db2777", "label": "Customers"},
    {"icon": "bi-box-seam",        "color": "#4f46e5", "label": "Products"},
    {"icon": "bi-tags",            "color": "#9333ea", "label": "Price Lists"},
    {"icon": "bi-person-gear",     "color": "#475569", "label": "Users"},
    {"icon": "bi-shield-lock",     "color": "#0f766e", "label": "Access"},
    {"icon": "bi-person-vcard",    "color": "#0d9488", "label": "Personnel"},
    {"icon": "bi-diagram-3",       "color": "#ea580c", "label": "Managers"},
]

# Colour swatches offered alongside the "browse more icons" search — the
# same accent colours as the quick picks above, deduplicated, so an icon
# picked from the full Bootstrap Icons set can still be given a consistent
# tile colour. Order preserved from PERSONNEL_ICONS.
ICON_COLOR_SWATCHES = list(dict.fromkeys(i["color"] for i in PERSONNEL_ICONS))

# The full Bootstrap Icons name list (no "bi-" prefix), loaded once from the
# same JSON the bootstrap-icons package ships, so the "browse more icons"
# search has the complete ~2000-icon set to offer — not just the 15 quick
# picks above. Source: static/bootstrap-icons.json (matches the
# bootstrap-icons version linked in overtime/base.html).
import json as _json
import os as _os

try:
    with open(_os.path.join(_os.path.dirname(__file__), "static", "bootstrap-icons.json"),
              encoding="utf-8") as _f:
        BOOTSTRAP_ICON_NAMES = _json.load(_f)
except (OSError, ValueError):
    BOOTSTRAP_ICON_NAMES = []

# Every valid "bi-xxx" class name, for server-side validation of whatever
# the picker submits.
ALL_ICON_KEYS = {f"bi-{name}" for name in BOOTSTRAP_ICON_NAMES}


class Personnel(db.Model):
    __tablename__ = "personnel"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    clockno = db.Column(db.String(20), unique=True, nullable=False)

    # Payroll group — "Wages" or "Salary" (see PAY_GROUPS). Discerns which
    # group a person belongs to for grouping and reporting.
    pay_group = db.Column(db.String(20))

    department_id = db.Column(
        db.Integer,
        db.ForeignKey("departments.id"),
        nullable=False
    )

    division_id = db.Column(
        db.Integer,
        db.ForeignKey("divisions.id"),
        nullable=False
    )
    surname = db.Column(db.String(100))
    id_no = db.Column(db.String(50))
    joined = db.Column(db.Date)
    rate = db.Column(db.Numeric(10, 2))
    status = db.Column(db.Boolean, default=True)
    status_date = db.Column(db.Date)
    jobgrade = db.Column(db.String(3))
    gender = db.Column(db.String(10))
    race = db.Column(db.String(20))
    job_description = db.Column(db.String(150))

    # Icon tile for this person, e.g. on the organogram — any Bootstrap
    # Icons class name (see ALL_ICON_KEYS) with a tile colour from
    # ICON_COLOR_SWATCHES. Both are picked together in the form and stored
    # as chosen, since icon is no longer limited to the 15 quick picks.
    icon = db.Column(db.String(40))
    icon_color = db.Column(db.String(7))

    # Profile photo — stored filename under static/images/personnel/, never
    # the original upload name (see overtime.routes._save_personnel_photo).
    # Shown in place of the icon tile wherever one is set.
    photo = db.Column(db.String(255))

    # Furnace-module role — 'Melt Technician' or 'Furnace Operator'. Only set
    # for melting-division personnel who appear on furnace entries; unrelated
    # to jobgrade/job_description.
    furnace_role = db.Column(db.String(30))

    # Role tags — org-chart flags, independent of the Heads (PersonnelManager)
    # assignment, which grants an actual user login overtime rights.
    # Chain (top to bottom): Director -> Head -> Production Superintendent
    # -> Supervisor -> Personnel.
    is_supervisor = db.Column(db.Boolean, nullable=False, default=False)
    is_superintendent = db.Column(db.Boolean, nullable=False, default=False)
    is_head = db.Column(db.Boolean, nullable=False, default=False)
    is_director = db.Column(db.Boolean, nullable=False, default=False)

    supervisor_id = db.Column(
        db.Integer,
        db.ForeignKey("personnel.id"),
        nullable=True
    )

    supervisor = db.relationship(
        "Personnel",
        remote_side="Personnel.id",
        backref="direct_reports",
        foreign_keys=[supervisor_id]
    )

    # The Production Superintendent this person reports to — sits between
    # Head and Supervisor on the org chart. Only people ticked
    # is_superintendent are offered in the picker.
    superintendent_id = db.Column(
        db.Integer,
        db.ForeignKey("personnel.id"),
        nullable=True
    )

    superintendent = db.relationship(
        "Personnel",
        remote_side="Personnel.id",
        backref="superintended_personnel",
        foreign_keys=[superintendent_id]
    )

    # The person who heads this person, e.g. for org-chart / escalation
    # purposes. Only people ticked is_head are offered in the picker.
    head_id = db.Column(
        db.Integer,
        db.ForeignKey("personnel.id"),
        nullable=True
    )

    head = db.relationship(
        "Personnel",
        remote_side="Personnel.id",
        backref="headed_personnel",
        foreign_keys=[head_id]
    )

    # The director this person's Head reports to, e.g. for org-chart /
    # escalation purposes. Only people ticked is_director are offered.
    director_id = db.Column(
        db.Integer,
        db.ForeignKey("personnel.id"),
        nullable=True
    )

    director = db.relationship(
        "Personnel",
        remote_side="Personnel.id",
        backref="directed_personnel",
        foreign_keys=[director_id]
    )

    managers = db.relationship(
        "PersonnelManager",
        back_populates="personnel",
        cascade="all, delete-orphan"
    )

    department = db.relationship(
        "Department",
        back_populates="personnel"
    )

    division = db.relationship(
        "Division",
        back_populates="personnel"
    )

    stocktakes = db.relationship(
        "StocktakeHeader",
        back_populates="personnel"
    )

    overtime_requests = db.relationship(
        "OvertimeRequest",
        back_populates="personnel",
        cascade="all, delete-orphan"
    )

    def __repr__(self):
        return f"<Personnel {self.name}>"


# ======================================================
# PERSONNEL MANAGERS (who may request/approve OT for whom)
# ======================================================
class PriceListPeriod(db.Model):
    """
    An effective-dated price list window for a division.

    HDC is updated annually (1 Jul → 30 Jun); HDA quarterly (every 3 months).
    Any dated event (production, scrap, stocktake) falls inside exactly one
    period per division, which is how we price it historically.
    """
    __tablename__ = "price_list_periods"

    id          = db.Column(db.Integer, primary_key=True)
    division_id = db.Column(db.Integer, db.ForeignKey("divisions.id"), nullable=False)
    label       = db.Column(db.String(100), nullable=False)
    start_date  = db.Column(db.Date, nullable=False)
    end_date    = db.Column(db.Date, nullable=False)
    created_at  = db.Column(db.DateTime, default=datetime.now)

    __table_args__ = (
        db.UniqueConstraint("division_id", "start_date", name="uq_price_period_division_start"),
    )

    division = db.relationship("Division", back_populates="price_periods")
    entries = db.relationship(
        "PriceListEntry",
        back_populates="period",
        cascade="all, delete-orphan"
    )

    def covers(self, target_date):
        return self.start_date <= target_date <= self.end_date

    def __repr__(self):
        return f"<PriceListPeriod {self.label}>"


class PriceListEntry(db.Model):
    """One price, for one customer + product, inside one price list period."""
    __tablename__ = "price_list_entries"

    id          = db.Column(db.Integer, primary_key=True)
    period_id   = db.Column(db.Integer, db.ForeignKey("price_list_periods.id", ondelete="CASCADE"), nullable=False)
    customer_id = db.Column(db.Integer, db.ForeignKey("customers.id"), nullable=False)
    product_id  = db.Column(db.Integer, db.ForeignKey("products.id"), nullable=False)
    price       = db.Column(db.Numeric(10, 2), nullable=False)

    __table_args__ = (
        db.UniqueConstraint("period_id", "customer_id", "product_id", name="uq_price_entry"),
    )

    period   = db.relationship("PriceListPeriod", back_populates="entries")
    customer = db.relationship("Customer")
    product  = db.relationship("Product")

    def __repr__(self):
        return f"<PriceListEntry period={self.period_id} cust={self.customer_id} prod={self.product_id}>"


def get_price_period(target_date, division_id):
    """The price list period covering target_date for a division, or None."""
    if not division_id or not target_date:
        return None
    return (
        PriceListPeriod.query
        .filter(
            PriceListPeriod.division_id == division_id,
            PriceListPeriod.start_date <= target_date,
            PriceListPeriod.end_date >= target_date,
        )
        .order_by(PriceListPeriod.start_date.desc())
        .first()
    )


def get_price_for(target_date, customer_id, product_id):
    """
    Look up the price that was in force for a customer + product on a date.

    Returns (price, period):
      - (Decimal, period) when a price was found
      - (None, period)    when the period exists but has no price for that pair
      - (None, None)      when no period covers that date for the customer's division
    """
    customer = Customer.query.get(customer_id) if customer_id else None
    if customer is None or customer.division_id is None:
        return None, None

    period = get_price_period(target_date, customer.division_id)
    if period is None:
        return None, None

    entry = PriceListEntry.query.filter_by(
        period_id=period.id,
        customer_id=customer_id,
        product_id=product_id,
    ).first()
    return (entry.price if entry else None), period


def get_current_prices_for_products(products, target_date=None):
    """
    Today's price-list price for a whole list of products, keyed by product id.

    Same answer as Product.current_price, but batched into a handful of
    queries instead of two per product — use it for the product list.
    Products with no primary customer, or whose division has no period
    covering the date, or which carry no price in that period, are simply
    absent from the returned dict.
    """
    target_date = target_date or date.today()

    customer_by_product = {p.id: p.customer_id for p in products if p.customer_id}
    if not customer_by_product:
        return {}

    division_by_customer = {
        c.id: c.division_id
        for c in Customer.query.filter(
            Customer.id.in_(set(customer_by_product.values()))
        ).all()
    }

    # Only ever a handful of divisions, so one period lookup each.
    period_by_division = {}
    for division_id in {d for d in division_by_customer.values() if d}:
        period = get_price_period(target_date, division_id)
        if period is not None:
            period_by_division[division_id] = period

    wanted = []  # (period_id, customer_id, product_id)
    for product_id, customer_id in customer_by_product.items():
        period = period_by_division.get(division_by_customer.get(customer_id))
        if period is not None:
            wanted.append((period.id, customer_id, product_id))
    if not wanted:
        return {}

    entries = PriceListEntry.query.filter(
        PriceListEntry.period_id.in_({key[0] for key in wanted}),
        PriceListEntry.customer_id.in_({key[1] for key in wanted}),
        PriceListEntry.product_id.in_({key[2] for key in wanted}),
    ).all()
    price_by_key = {
        (e.period_id, e.customer_id, e.product_id): e.price for e in entries
    }

    prices = {}
    for key in wanted:
        price = price_by_key.get(key)
        if price is not None:
            prices[key[2]] = price
    return prices


class BinRate(db.Model):
    """
    A flat R/KG rate for HDA bin stock (Clean Stock / WIP Stock), re-entered
    manually every ~3 months. Unlike PriceListPeriod/PriceListEntry, there's
    no customer/product dimension — one rate applies to all bins in force
    from its effective_date until the next entry supersedes it.
    """
    __tablename__ = "bin_rates"

    id             = db.Column(db.Integer, primary_key=True)
    effective_date = db.Column(db.Date, nullable=False, unique=True)
    rate_per_kg    = db.Column(db.Numeric(10, 2), nullable=False)
    created_at     = db.Column(db.DateTime, default=datetime.now)
    created_by     = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)

    user = db.relationship("User")

    def __repr__(self):
        return f"<BinRate {self.effective_date} {self.rate_per_kg}/kg>"


def get_bin_rate_for(target_date):
    """The latest BinRate effective on or before target_date, or None."""
    if not target_date:
        return None
    return (
        BinRate.query
        .filter(BinRate.effective_date <= target_date)
        .order_by(BinRate.effective_date.desc())
        .first()
    )


class PersonnelManager(db.Model):
    __tablename__ = "personnel_managers"
    __table_args__ = (
        db.UniqueConstraint("personnel_id", "manager_id", name="uq_personnel_manager"),
    )

    id = db.Column(db.Integer, primary_key=True)
    personnel_id = db.Column(db.Integer, db.ForeignKey("personnel.id"), nullable=False)
    manager_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)

    personnel = db.relationship("Personnel", back_populates="managers")
    manager = db.relationship("User", back_populates="managed_personnel")

    def __repr__(self):
        return f"<PersonnelManager personnel={self.personnel_id} manager={self.manager_id}>"


