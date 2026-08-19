import os
from functools import wraps
import logging
from dotenv import load_dotenv
load_dotenv()
import logging
import json
from datetime import datetime, timedelta
from flask import Flask, session, render_template, request, redirect, url_for, flash
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, current_user
from sqlalchemy.orm import DeclarativeBase
from werkzeug.middleware.proxy_fix import ProxyFix
from cryptography.hazmat.primitives import serialization, hashes
from cryptography.hazmat.primitives.asymmetric import padding
from flask_wtf import CSRFProtect
from flask_migrate import Migrate


WINDOWS_LICENSE_DIR = r"C:\ProgramData\Foundry Analytics\Procure Flow"
PRODUCT_IMAGE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "images", "products")
PERSONNEL_PHOTO_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "images", "personnel")
PRODUCT_PPAP_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "documents", "ppap")
for folder in [WINDOWS_LICENSE_DIR, PRODUCT_IMAGE_DIR, PERSONNEL_PHOTO_DIR, PRODUCT_PPAP_DIR]:
    if not os.path.exists(folder):
        os.makedirs(folder)

logging.basicConfig(level=logging.DEBUG)

class Base(DeclarativeBase):
    pass

db = SQLAlchemy(model_class=Base)

# create the app
app = Flask(__name__)
csrf = CSRFProtect(app)
app.secret_key = os.environ.get("SESSION_SECRET", "dev-secret-key-change-in-production")
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)

# configure the database
app.config["SQLALCHEMY_DATABASE_URI"] = os.environ["DATABASE_URL"]
app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
    "pool_recycle": 300,
    "pool_pre_ping": True,
}
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

# ---- Session + CSRF lifetime config ----
app.config["WTF_CSRF_TIME_LIMIT"] = 60 * 60 * 8   # 8 hours
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=7)
app.config["SESSION_REFRESH_EACH_REQUEST"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["REMEMBER_COOKIE_DURATION"] = timedelta(days=30)
app.config["REMEMBER_COOKIE_HTTPONLY"] = True
app.config["REMEMBER_COOKIE_SAMESITE"] = "Lax"
app.config['LICENSE_FILE'] = os.path.join(WINDOWS_LICENSE_DIR, "license.json")

db.init_app(app)
migrate = Migrate(app, db)
# Initialize Flask-Login
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'
login_manager.login_message = 'Please log in to access this page.'
login_manager.login_message_category = 'info'

@login_manager.user_loader
def load_user(user_id):
    from models import User
    return User.query.get(int(user_id))

@app.before_request
def make_session_permanent():
    session.permanent = True


# ══════════════════════════════════════════════════════════════
# GLOBAL CONTEXT PROCESSOR  — injects has_perm() into every template
# ══════════════════════════════════════════════════════════════
@app.context_processor
def inject_permissions():
    def has_perm(module, action):
        """Literal check — does this user hold the stored permission?"""
        if not current_user.is_authenticated:
            return False
        return current_user.has_permission(module, action)

    def can(module, action="view"):
        """
        Effective check — the one that governs what a user may reach.

        Unlike has_perm() this honours the admin bypass and the unrestricted
        rule for accounts that have never had permissions configured. Use it
        to show or hide navigation and action buttons.
        """
        from access.guards import user_can
        return user_can(current_user, module, action)

    def can_any(module, *actions):
        from access.guards import user_can
        return any(user_can(current_user, module, a) for a in (actions or ("view",)))

    return dict(has_perm=has_perm, can=can, can_any=can_any)


@app.context_processor
def inject_footer():
    """Values used by the footer on every base template."""
    return dict(current_year=datetime.now().year)


# ══════════════════════════════════════════════════════════════
# LICENSE HELPERS
# ══════════════════════════════════════════════════════════════
PUBLIC_KEY_PATH = os.path.join(WINDOWS_LICENSE_DIR, "public_key.pem")

def load_license():
    path = app.config['LICENSE_FILE']
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r") as f:
            return json.load(f)
    except:
        return None

def validate_license():
    lic = load_license()
    today = datetime.today().date()
    if not lic:
        return False, "License file missing"
    try:
        data = json.loads(lic["data"])
        expiry_date = datetime.strptime(data["expiry"], "%Y-%m-%d").date()
        if expiry_date < today:
            return False, "License expired"
        pub_key = serialization.load_pem_public_key(open(PUBLIC_KEY_PATH, "rb").read())
        pub_key.verify(
            bytes.fromhex(lic["signature"]),
            lic["data"].encode("utf-8"),
            padding.PKCS1v15(),
            hashes.SHA256()
        )
        return True, "OK"
    except Exception as ex:
        import traceback
        print("🛑 License validation exception:", traceback.format_exc())
        return False, f"License invalid: {ex}"

def license_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        valid, msg = validate_license()
        if not valid:
            flash(msg, "danger")
            return redirect(url_for("upload_license"))
        return f(*args, **kwargs)
    return wrapper


# ══════════════════════════════════════════════════════════════
# BLUEPRINTS + DB INIT
# ══════════════════════════════════════════════════════════════
with app.app_context():
    import models
    import routes
    from fettling import routes as fettling_bp
    from dailyproduction import routes as daily_production_bp
    from asset import routes as asset_bp
    from stocktake import routes as stocktake_bp
    from scrap import routes as scrap_bp
    from access import routes as access_bp
    from access.catalogue import seed_permissions
    from overtime.routes import overtime_bp
    from timeclock.routes import timeclock_bp
    from furnace import routes as furnace_bp

    app.register_blueprint(fettling_bp.fettling_bp)
    app.register_blueprint(daily_production_bp.daily_production_bp)
    app.register_blueprint(asset_bp.asset_bp)
    app.register_blueprint(stocktake_bp.stocktake_bp)
    app.register_blueprint(scrap_bp.scrap_bp)
    app.register_blueprint(access_bp.access_bp)
    app.register_blueprint(overtime_bp)
    app.register_blueprint(timeclock_bp)
    app.register_blueprint(furnace_bp.furnace_bp)

    # ──────────────────────────────────────────────────────────────
    # AUTO-SYNC DB SCHEMA ON STARTUP
    # Keeps the database in step with the models so you never have to
    # run a manual migration after pulling changes. Safe & idempotent.
    # ──────────────────────────────────────────────────────────────
    from sqlalchemy import text as _sql_text

    # 1) Create any missing tables (e.g. the product_customers M2M table)
    db.create_all()

    # 2) create_all() can't add columns to tables that already exist, so
    #    ensure newer columns are present (Postgres IF NOT EXISTS).
    #    Each statement runs in its own transaction so one failure can't
    #    block the rest.
    _schema_statements = [
        "ALTER TABLE products ADD COLUMN IF NOT EXISTS product_code VARCHAR(50)",
        "ALTER TABLE products ADD COLUMN IF NOT EXISTS supplier_code VARCHAR(50)",
        "ALTER TABLE products ADD COLUMN IF NOT EXISTS simplified_code VARCHAR(50)",
        "ALTER TABLE products ADD COLUMN IF NOT EXISTS supplier_description VARCHAR(255)",
        "ALTER TABLE products ADD COLUMN IF NOT EXISTS grade VARCHAR(10)",
        "ALTER TABLE products ADD COLUMN IF NOT EXISTS price NUMERIC(10, 2)",
        "ALTER TABLE products ADD COLUMN IF NOT EXISTS weight NUMERIC(10, 3)",
        "ALTER TABLE products ADD COLUMN IF NOT EXISTS is_stock_item BOOLEAN NOT NULL DEFAULT TRUE",
        "ALTER TABLE products ADD COLUMN IF NOT EXISTS active BOOLEAN NOT NULL DEFAULT TRUE",
        "ALTER TABLE customers ADD COLUMN IF NOT EXISTS division_id INTEGER",
        "ALTER TABLE customers ADD COLUMN IF NOT EXISTS active BOOLEAN NOT NULL DEFAULT TRUE",
        "ALTER TABLE customers ADD COLUMN IF NOT EXISTS code VARCHAR(50)",
        # Reject reasons split into external / internal / both. Existing rows
        # were the customer machining-reject columns, so backfill to 'external'.
        "ALTER TABLE scrap_defects ADD COLUMN IF NOT EXISTS applies_to VARCHAR(10) NOT NULL DEFAULT 'external'",
        # Internal scrap capture used to reuse qty_machined (meant for the
        # customer's machined total on external rows) to mean "quantity
        # packed" — give it its own column and move existing internal rows
        # over, since qty_machined no longer applies to them.
        "ALTER TABLE scrap_entries ADD COLUMN IF NOT EXISTS qty_packed INTEGER DEFAULT 0",
        "UPDATE scrap_entries SET qty_packed = qty_machined, qty_machined = NULL "
        "WHERE source = 'internal' AND qty_machined IS NOT NULL",

        # Overtime — the actual-hours side, plus a request/actual discriminator.
        "ALTER TABLE overtime_requests ADD COLUMN IF NOT EXISTS entry_type VARCHAR(10) NOT NULL DEFAULT 'request'",
        # Everything created by one submit shares a batch, so a week of
        # overtime is opened, approved and captured as a unit.
        "ALTER TABLE overtime_requests ADD COLUMN IF NOT EXISTS batch_id VARCHAR(36)",
        "CREATE INDEX IF NOT EXISTS ix_overtime_requests_batch_id ON overtime_requests (batch_id)",
        "ALTER TABLE overtime_requests ADD COLUMN IF NOT EXISTS actual_start_time TIME",
        "ALTER TABLE overtime_requests ADD COLUMN IF NOT EXISTS actual_end_time TIME",
        "ALTER TABLE overtime_requests ADD COLUMN IF NOT EXISTS actual_break_minutes INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE overtime_requests ADD COLUMN IF NOT EXISTS actual_late_minutes INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE overtime_requests ADD COLUMN IF NOT EXISTS actual_hours NUMERIC(5, 2)",
        "ALTER TABLE overtime_requests ADD COLUMN IF NOT EXISTS actual_multiplier NUMERIC(4, 2)",
        "ALTER TABLE overtime_requests ADD COLUMN IF NOT EXISTS actual_amount NUMERIC(10, 2)",
        "ALTER TABLE overtime_requests ADD COLUMN IF NOT EXISTS actual_amount_overridden BOOLEAN NOT NULL DEFAULT FALSE",
        "ALTER TABLE overtime_requests ADD COLUMN IF NOT EXISTS actual_notes TEXT",
        "ALTER TABLE overtime_requests ADD COLUMN IF NOT EXISTS actual_captured_by INTEGER",
        "ALTER TABLE overtime_requests ADD COLUMN IF NOT EXISTS actual_captured_at TIMESTAMP",
        # A standalone actual has no requested side, so those columns must relax.
        "ALTER TABLE overtime_requests ALTER COLUMN requested_by DROP NOT NULL",
        "ALTER TABLE overtime_requests ALTER COLUMN start_time DROP NOT NULL",
        "ALTER TABLE overtime_requests ALTER COLUMN end_time DROP NOT NULL",
        "ALTER TABLE overtime_requests ALTER COLUMN hours DROP NOT NULL",
        "ALTER TABLE overtime_requests ALTER COLUMN overtime_amount DROP NOT NULL",

        # Personnel — payroll group (Wages / Salary).
        "ALTER TABLE personnel ADD COLUMN IF NOT EXISTS pay_group VARCHAR(20)",
        # Personnel — org-chart role tags (Supervisor / Production
        # Superintendent / Head / Director).
        "ALTER TABLE personnel ADD COLUMN IF NOT EXISTS is_supervisor BOOLEAN NOT NULL DEFAULT FALSE",
        "ALTER TABLE personnel ADD COLUMN IF NOT EXISTS is_superintendent BOOLEAN NOT NULL DEFAULT FALSE",
        "ALTER TABLE personnel ADD COLUMN IF NOT EXISTS is_head BOOLEAN NOT NULL DEFAULT FALSE",
        "ALTER TABLE personnel ADD COLUMN IF NOT EXISTS is_director BOOLEAN NOT NULL DEFAULT FALSE",
        # Personnel — the Production Superintendent this person reports to,
        # between Head and Supervisor (org-chart, picker restricted to
        # personnel ticked is_superintendent).
        "ALTER TABLE personnel ADD COLUMN IF NOT EXISTS superintendent_id INTEGER REFERENCES personnel(id)",
        # Personnel — the Head this person reports to (org-chart, picker
        # restricted to personnel ticked is_head).
        "ALTER TABLE personnel ADD COLUMN IF NOT EXISTS head_id INTEGER REFERENCES personnel(id)",
        # Personnel — the Director above the Head (org-chart, picker
        # restricted to personnel ticked is_director).
        "ALTER TABLE personnel ADD COLUMN IF NOT EXISTS director_id INTEGER REFERENCES personnel(id)",
        # Personnel — icon tile (any Bootstrap Icons class) and its colour.
        "ALTER TABLE personnel ADD COLUMN IF NOT EXISTS icon VARCHAR(40)",
        "ALTER TABLE personnel ADD COLUMN IF NOT EXISTS icon_color VARCHAR(7)",
        # Personnel — profile photo (stored filename under static/images/personnel/).
        "ALTER TABLE personnel ADD COLUMN IF NOT EXISTS photo VARCHAR(255)",
        # Backfill colour for rows saved back when icon was one of the 15
        # dashboard quick picks (the only choice before "browse more icons").
        "UPDATE personnel SET icon_color = '#2563eb' WHERE icon = 'bi-tools' AND icon_color IS NULL",
        "UPDATE personnel SET icon_color = '#0891b2' WHERE icon = 'bi-clipboard-data' AND icon_color IS NULL",
        "UPDATE personnel SET icon_color = '#7c3aed' WHERE icon = 'bi-boxes' AND icon_color IS NULL",
        "UPDATE personnel SET icon_color = '#b45309' WHERE icon = 'bi-fire' AND icon_color IS NULL",
        "UPDATE personnel SET icon_color = '#dc2626' WHERE icon = 'bi-trash3' AND icon_color IS NULL",
        "UPDATE personnel SET icon_color = '#d97706' WHERE icon = 'bi-clock-history' AND icon_color IS NULL",
        "UPDATE personnel SET icon_color = '#059669' WHERE icon = 'bi-hdd-stack' AND icon_color IS NULL",
        "UPDATE personnel SET icon_color = '#0369a1' WHERE icon = 'bi-stopwatch' AND icon_color IS NULL",
        "UPDATE personnel SET icon_color = '#db2777' WHERE icon = 'bi-people' AND icon_color IS NULL",
        "UPDATE personnel SET icon_color = '#4f46e5' WHERE icon = 'bi-box-seam' AND icon_color IS NULL",
        "UPDATE personnel SET icon_color = '#9333ea' WHERE icon = 'bi-tags' AND icon_color IS NULL",
        "UPDATE personnel SET icon_color = '#475569' WHERE icon = 'bi-person-gear' AND icon_color IS NULL",
        "UPDATE personnel SET icon_color = '#0f766e' WHERE icon = 'bi-shield-lock' AND icon_color IS NULL",
        "UPDATE personnel SET icon_color = '#0d9488' WHERE icon = 'bi-person-vcard' AND icon_color IS NULL",
        "UPDATE personnel SET icon_color = '#ea580c' WHERE icon = 'bi-diagram-3' AND icon_color IS NULL",

        # Time clock — the two Turbo Time reports print the VARIANCE column at
        # different widths, so a batch records which layout its lines were cut
        # with (see ClockDay.source_values()).
        "ALTER TABLE clock_import_batches ADD COLUMN IF NOT EXISTS report_variance_end INTEGER DEFAULT 112",

        # Drawing revision level, e.g. "AA" / "OOO" / "AB" — captured on the
        # product and snapshotted onto each HDA dispatch cage line.
        "ALTER TABLE products ADD COLUMN IF NOT EXISTS drawing_level VARCHAR(10)",

        # HDA cage-based dispatch — scrap_dispatch_batches itself is created by
        # db.create_all() above; these are the new columns on the existing
        # scrap_dispatches line table (null for HDC's plain dispatch rows).
        "ALTER TABLE scrap_dispatches ADD COLUMN IF NOT EXISTS batch_id INTEGER REFERENCES scrap_dispatch_batches(id)",
        "ALTER TABLE scrap_dispatches ADD COLUMN IF NOT EXISTS cage_no INTEGER",
        "ALTER TABLE scrap_dispatches ADD COLUMN IF NOT EXISTS trenstar_no VARCHAR(40)",
        "ALTER TABLE scrap_dispatches ADD COLUMN IF NOT EXISTS weight NUMERIC(10, 3)",
        "ALTER TABLE scrap_dispatches ADD COLUMN IF NOT EXISTS head_numbers VARCHAR(120)",
        "ALTER TABLE scrap_dispatches ADD COLUMN IF NOT EXISTS drawing_level VARCHAR(10)",
        # Four physical-process confirmations, ticked per cage (not per load —
        # an earlier build of this feature had them on the batch header;
        # the DROPs below clean up that shape for anyone who already has it).
        "ALTER TABLE scrap_dispatches ADD COLUMN IF NOT EXISTS blue_card_confirmed BOOLEAN NOT NULL DEFAULT FALSE",
        "ALTER TABLE scrap_dispatches ADD COLUMN IF NOT EXISTS black_bag_confirmed BOOLEAN NOT NULL DEFAULT FALSE",
        "ALTER TABLE scrap_dispatches ADD COLUMN IF NOT EXISTS cage_packed_half_confirmed BOOLEAN NOT NULL DEFAULT FALSE",
        "ALTER TABLE scrap_dispatches ADD COLUMN IF NOT EXISTS weighbridge_printed_confirmed BOOLEAN NOT NULL DEFAULT FALSE",
        "ALTER TABLE scrap_dispatch_batches DROP COLUMN IF EXISTS blue_card_confirmed",
        "ALTER TABLE scrap_dispatch_batches DROP COLUMN IF EXISTS black_bag_confirmed",
        "ALTER TABLE scrap_dispatch_batches DROP COLUMN IF EXISTS cage_packed_half_confirmed",
        "ALTER TABLE scrap_dispatch_batches DROP COLUMN IF EXISTS weighbridge_printed_confirmed",

        # Product PPAP document — one PDF per product, stored under
        # static/documents/ppap/ (see PRODUCT_PPAP_DIR).
        "ALTER TABLE products ADD COLUMN IF NOT EXISTS ppap_filename VARCHAR(255)",
        "ALTER TABLE products ADD COLUMN IF NOT EXISTS ppap_uploaded_at TIMESTAMP",
    ]
    for _stmt in _schema_statements:
        try:
            with db.engine.begin() as _conn:
                _conn.execute(_sql_text(_stmt))
        except Exception as _ex:
            logging.warning("Schema auto-sync skipped [%s]: %s", _stmt, _ex)
    logging.info("Schema auto-sync complete.")

from app import app, db
from models import User, Permission, UserPermission
from datetime import datetime

with app.app_context():
    db.create_all()

    # ── Seed reference data ───────────────────────────────────────
    # Permissions must be seeded BEFORE the admin grant below, so a newly
    # added permission is granted to the admin account on the same startup.
    seed_permissions()

    # Scrap reject reasons — the defect columns of the customer report
    from scrap.models import seed_scrap_defects
    seed_scrap_defects()

    # ── Keep the admin account holding every permission ───────────
    # Administrators bypass permission checks anyway (see access/guards.py);
    # this keeps the stored grants complete so the matrix reads correctly.
    user = User.query.filter_by(username='admin').first()
    if user is None:
        logging.warning("No 'admin' user found — skipping the permission grant.")
    else:
        all_perms = Permission.query.all()
        held = {
            up.permission_id
            for up in UserPermission.query.filter_by(user_id=user.id).all()
        }

        granted = 0
        for perm in all_perms:
            if perm.id in held:
                continue
            db.session.add(UserPermission(
                user_id=user.id,
                permission_id=perm.id,
                granted_by=user.id,
                granted_at=datetime.now()
            ))
            granted += 1

        db.session.commit()
        logging.info("Granted %s of %s permissions to %s", granted, len(all_perms), user.name)