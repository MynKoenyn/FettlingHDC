from flask import render_template, request, redirect, url_for, session, flash, Blueprint
from flask_login import login_user, logout_user, current_user, login_required
from app import app , db
from fettling.models import *
from models import *
from fettling.forms import *
from datetime import date
import json


fettling_bp = Blueprint('fettling', __name__, url_prefix="/fettling")

@fettling_bp.route("/dashboard")
def dashboard():
    if "user_id" not in session:
        return redirect(url_for("login"))

    # Fetch last 10 entries for dashboard
    recent_activity = (
        FettlingEntry.query
        .order_by(FettlingEntry.entry_date.desc())
        .limit(10)
        .all()
    )

    # Optional: include related info like product and customer
    activity_list = []
    for entry in recent_activity:
        activity_list.append({
            "date": entry.entry_date,
            "product": entry.product.name if entry.product else "N/A",
            "customer": entry.product.customer.name if entry.product and entry.product.customer else "N/A",
            "quantity": entry.quantity,
            "user": entry.user.username if entry.user else "N/A"
        })

    return render_template("fettling/dashboard.html", recent_activity=activity_list)



@fettling_bp.route("/entry", methods=["GET", "POST"])
@login_required
def entry():
    form = EntryForm()
    
    # Fetch customers and products
    customers = Customer.query.order_by(Customer.name).all()
    products = Product.query.order_by(Product.name).all()
    
    # Build products_by_customer for JS filtering
    products_by_customer = {}
    for p in products:
        products_by_customer.setdefault(p.customer_id, []).append({
            "id": p.id,
            "name": p.name
        })

    if form.validate_on_submit():
        entry_date = form.entry_date.data
        entries_to_add = []

        # Loop through potential 50 rows
        for i in range(1, 51):
            product_id = request.form.get(f"product_id_{i}")
            quantity = request.form.get(f"quantity_{i}")

            # Only insert if a product is selected and quantity > 0
            if product_id and quantity:
                try:
                    quantity_int = int(quantity)
                    if quantity_int > 0:
                        entry = FettlingEntry(
                            entry_date=entry_date,
                            product_id=int(product_id),
                            quantity=quantity_int,
                            user_id=current_user.id
                        )
                        entries_to_add.append(entry)
                except ValueError:
                    # skip invalid quantities
                    continue

        if entries_to_add:
            db.session.add_all(entries_to_add)
            db.session.commit()

        return redirect(url_for("dashboard"))

    return render_template(
        "fettling/entry.html",
        form=form,
        customers=customers,
        products_by_customer=json.dumps(products_by_customer),
        today=date.today()
    )


from sqlalchemy import func

@fettling_bp.route("/report")
@login_required
def report():
    start_date = request.args.get("start_date")
    end_date = request.args.get("end_date")
    customer_id = request.args.get("customer_id")
    product_id = request.args.get("product_id")

    customers = Customer.query.order_by(Customer.name).all()
    products = Product.query.order_by(Product.name).all()

    # Base query with explicit joins
    query = db.session.query(
        FettlingEntry.entry_date,
        Customer.name.label("customer_name"),
        Product.name.label("product_name"),
        FettlingEntry.quantity
    ).select_from(FettlingEntry)\
     .join(Product, FettlingEntry.product_id == Product.id)\
     .join(Customer, Product.customer_id == Customer.id)

    # Apply filters
    if start_date:
        query = query.filter(FettlingEntry.entry_date >= start_date)
    if end_date:
        query = query.filter(FettlingEntry.entry_date <= end_date)
    if customer_id:
        query = query.filter(Customer.id == customer_id)
    if product_id:
        query = query.filter(Product.id == product_id)

    report_data = query.order_by(
        FettlingEntry.entry_date.desc(),
        Customer.name,
        Product.name
    ).all()

    # Aggregate total quantity per day for chart
    chart_query = db.session.query(
        FettlingEntry.entry_date,
        func.sum(FettlingEntry.quantity).label("total_qty")
    ).select_from(FettlingEntry)

    if start_date:
        chart_query = chart_query.filter(FettlingEntry.entry_date >= start_date)
    if end_date:
        chart_query = chart_query.filter(FettlingEntry.entry_date <= end_date)
    if customer_id:
        chart_query = chart_query.join(Product).filter(Product.customer_id == customer_id)
    if product_id:
        chart_query = chart_query.filter(FettlingEntry.product_id == product_id)

    chart_data = chart_query.group_by(FettlingEntry.entry_date).order_by(FettlingEntry.entry_date).all()

    # Prepare lists for Chart.js
    chart_labels = [row[0].strftime("%Y-%m-%d") for row in chart_data]
    chart_values = [row[1] for row in chart_data]

    return render_template(
        "fettling/report.html",
        report_data=report_data,
        customers=customers,
        products=products,
        filters={
            "start_date": start_date,
            "end_date": end_date,
            "customer_id": customer_id,
            "product_id": product_id
        },
        chart_labels=chart_labels,
        chart_values=chart_values
    )