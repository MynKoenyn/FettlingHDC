import os
import json
from flask import render_template, request, redirect, url_for, session, flash
from flask_login import login_user, logout_user, current_user, login_required
from app import app , db, WINDOWS_LICENSE_DIR, validate_license, license_required
from werkzeug.utils import secure_filename
from models import *
from forms import *
from datetime import date
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

        # Find user by username
        user = User.query.filter_by(username=username).first()

        # Check password using the model's check_password method
        if user and user.check_password(password):
            # Log in the user
            login_user(user)

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
    if "user_id" not in session:
        return redirect(url_for("login"))



    return render_template("dashboard.html")

@app.route("/customers", methods=["GET", "POST"])
@login_required
@license_required
def customers():
    form = SupplierForm()  # <-- create the form

    if form.validate_on_submit():
        # Check if customer already exists
        existing_customer = Customer.query.filter_by(name=form.name.data.strip()).first()
        if existing_customer:
            flash(f"Customer '{form.name.data}' already exists.", "warning")
        else:
            new_customer = Customer(name=form.name.data.strip())
            db.session.add(new_customer)
            db.session.commit()
            flash(f"Customer '{form.name.data}' added successfully!", "success")
            return redirect(url_for("customers"))

    # Fetch all customers for the table
    customers_list = Customer.query.order_by(Customer.name).all()

    # Pass the form into the template
    return render_template(
        "customers.html",
        form=form,
        customers=customers_list
    )


@app.route("/products", methods=["GET", "POST"])
@login_required
@license_required
def products_crud():
    form = ProductForm()

    # Populate customer choices
    form.customer_id.choices = [(s.id, s.name) for s in Customer.query.order_by(Customer.name).all()]

    if form.validate_on_submit():
        # Create new product
        product = Product(
            name=form.name.data.strip(),
            customer_id=form.customer_id.data
        )
        db.session.add(product)
        db.session.commit()
        flash(f"Product '{product.name}' added successfully!", "success")
        return redirect(url_for("products_crud"))

    # Fetch all products for the table
    products = Product.query.order_by(Product.name).all()

    return render_template(
        "products.html",
        form=form,
        products=products
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