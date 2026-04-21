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
from flask_login import LoginManager
from sqlalchemy.orm import DeclarativeBase
from werkzeug.middleware.proxy_fix import ProxyFix
from cryptography.hazmat.primitives import serialization, hashes
from cryptography.hazmat.primitives.asymmetric import padding
from flask_wtf import CSRFProtect   # ← ADD THIS


WINDOWS_LICENSE_DIR = r"C:\ProgramData\Foundry Analytics\Procure Flow"
for folder in [WINDOWS_LICENSE_DIR]:
    if not os.path.exists(folder):
        os.makedirs(folder)
# Configure logging for debugging
logging.basicConfig(level=logging.DEBUG)
class Base(DeclarativeBase):
    pass

db = SQLAlchemy(model_class=Base)

# create the app
app = Flask(__name__)
csrf = CSRFProtect(app)   # ← AND THIS
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
app.config["WTF_CSRF_TIME_LIMIT"] = 60 * 60 * 8  # 8 hours
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(hours=8)
app.config["SESSION_REFRESH_EACH_REQUEST"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config['LICENSE_FILE'] = os.path.join(WINDOWS_LICENSE_DIR, "license.json")
db.init_app(app)

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

with app.app_context():
    import models
    import routes
    from fettling import routes as fettling_bp
    from dailyproduction import routes as daily_production_bp
    from asset import routes as asset_bp
    from stocktake import routes as stocktake_bp
    app.register_blueprint(fettling_bp.fettling_bp)
    app.register_blueprint(daily_production_bp.daily_production_bp) 
    app.register_blueprint(asset_bp.asset_bp)
    app.register_blueprint(stocktake_bp.stocktake_bp)
    db.create_all()



