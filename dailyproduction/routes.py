from flask import render_template, request, redirect, url_for, session, flash, Blueprint
from flask_login import login_user, logout_user, current_user, login_required
from app import app , db
from dailyproduction.models import *
from models import *
from dailyproduction.forms import *
from datetime import date
import json


daily_production_bp = Blueprint('daily_production', __name__, url_prefix="/dailyproduction")

@daily_production_bp.route("/dashboard")
@login_required
def dashboard():
    if "user_id" not in session:
        return redirect(url_for("login"))

    return render_template("dailyproduction/dashboard.html")

