from datetime import datetime
from app import db
from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash


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
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
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

    # password helpers
    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

    def __repr__(self):
        return f"<User {self.username}>"
    
    def __repr__(self):
        return f"<User {self.name}>"


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
# CUSTOMERS
# ======================================================
class Customer(db.Model):
    __tablename__ = "customers"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)

    products = db.relationship(
        "Product",
        back_populates="customer",
        cascade="all, delete-orphan"
    )

    def __repr__(self):
        return f"<Customer {self.name}>"


# ======================================================
# PRODUCTS
# ======================================================
class Product(db.Model):
    __tablename__ = "products"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)

    customer_id = db.Column(
        db.Integer,
        db.ForeignKey("customers.id")
    )

    barcode = db.Column(db.String(25))
    stockamount = db.Column(db.Integer, default=0)

    customer = db.relationship("Customer", back_populates="products")

    entries = db.relationship(
        "FettlingEntry",
        back_populates="product",
        cascade="all, delete-orphan"
    )

    def __repr__(self):
        return f"<Product {self.name}>"


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

    def __repr__(self):
        return f"<Department {self.name}>"


# ======================================================
# PERSONNEL
# ======================================================
class Personnel(db.Model):
    __tablename__ = "personnel"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    clockno = db.Column(db.String(20), unique=True, nullable=False)

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

    def __repr__(self):
        return f"<Personnel {self.name}>"