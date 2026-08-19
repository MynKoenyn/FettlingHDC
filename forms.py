from flask_wtf import FlaskForm
from wtforms import (
    StringField,
    PasswordField,
    IntegerField,
    SelectField,
    SelectMultipleField,
    DecimalField,
    TextAreaField,
    DateField,
    SubmitField,
    FileField,
    BooleanField
)
from wtforms.validators import DataRequired, Length, NumberRange, Optional, EqualTo
from flask_wtf.file import FileField, FileRequired, FileAllowed

from models import PRODUCT_GRADES

# ---------------- LOGIN ----------------
class LoginForm(FlaskForm):
    username = StringField(
        "Username",
        validators=[DataRequired(), Length(max=50)]
    )

    password = PasswordField(
        "Password",
        validators=[DataRequired(), Length(max=255)]
    )

    submit = SubmitField("Login")


# ---------------- USER ----------------
class CreateUserForm(FlaskForm):
    name        = StringField("Full Name",   validators=[DataRequired(), Length(max=100)])
    username    = StringField("Username",    validators=[DataRequired(), Length(max=50)])
    password    = PasswordField("Password",  validators=[DataRequired(), Length(min=6)])
    confirm     = PasswordField("Confirm Password",
                                validators=[DataRequired(), EqualTo("password", message="Passwords must match.")])
    role_id     = SelectField("Role",        coerce=int, validators=[DataRequired()])
    division_id = SelectField("Division",    coerce=int, validators=[Optional()])
    department_id = SelectField("Department",coerce=int, validators=[Optional()])
    active      = BooleanField("Active",     default=True)
    submit      = SubmitField("Create User")


class EditUserForm(FlaskForm):
    name          = StringField("Full Name",        validators=[DataRequired(), Length(max=100)])
    username      = StringField("Username",         validators=[DataRequired(), Length(max=50)])
    password      = PasswordField("New Password",   validators=[Optional(), Length(min=6)])
    confirm       = PasswordField("Confirm Password", validators=[Optional()])
    role_id       = SelectField("Role",             coerce=int, validators=[DataRequired()])
    division_id   = SelectField("Division",         coerce=int, validators=[Optional()])
    department_id = SelectField("Department",       coerce=int, validators=[Optional()])
    active        = BooleanField("Active")
    submit        = SubmitField("Save Changes")


class RoleForm(FlaskForm):
    name   = StringField("Role Name", validators=[DataRequired(), Length(max=50)])
    submit = SubmitField("Add Role")

# ---------------- ORG STRUCTURE ----------------
class DivisionForm(FlaskForm):
    code = StringField("Code",         validators=[DataRequired(), Length(max=10)])
    name = StringField("Division Name", validators=[DataRequired(), Length(max=100)])
    submit = SubmitField("Save Division")


class DepartmentForm(FlaskForm):
    name        = StringField("Department Name", validators=[DataRequired(), Length(max=100)])
    # Every department must sit under a division — the FK is NOT NULL.
    division_id = SelectField("Division", coerce=int, validators=[DataRequired()])
    submit      = SubmitField("Save Department")


# ---------------- SUPPLIER ----------------
class SupplierForm(FlaskForm):
    name = StringField(
        "Customer Name",
        validators=[DataRequired(), Length(max=100)]
    )
    code = StringField(
        "Customer Code",
        validators=[Optional(), Length(max=50)]
    )
    # Division decides which price-list calendar applies (HDC annual / HDA quarterly)
    division_id = SelectField(
        "Division",
        coerce=int,
        validators=[Optional()]
    )
    active = BooleanField("Active", default=True)
    submit = SubmitField("Save")


# ---------------- PRICE LISTS ----------------
class PriceListPeriodForm(FlaskForm):
    division_id = SelectField("Division", coerce=int, validators=[DataRequired()])
    label       = StringField("Label", validators=[DataRequired(), Length(max=100)])
    start_date  = DateField("Start Date", validators=[DataRequired()])
    end_date    = DateField("End Date", validators=[DataRequired()])
    submit      = SubmitField("Save Period")

    def validate_end_date(self, field):
        from wtforms.validators import ValidationError
        if self.start_date.data and field.data and field.data < self.start_date.data:
            raise ValidationError("End date must be on or after the start date.")


class GeneratePeriodsForm(FlaskForm):
    division_id = SelectField("Division", coerce=int, validators=[DataRequired()])
    cadence     = SelectField(
        "Cadence",
        choices=[
            ("annual",    "Annual — 1 Jul to 30 Jun"),
            ("quarterly", "Quarterly — every 3 months"),
        ],
        validators=[DataRequired()]
    )
    year = IntegerField(
        "Year starting July",
        validators=[DataRequired(), NumberRange(min=2000, max=2100)]
    )
    submit = SubmitField("Generate Periods")


class PriceListEntryForm(FlaskForm):
    customer_id = SelectField("Customer", coerce=int, validators=[DataRequired()])
    product_id  = SelectField("Product",  coerce=int, validators=[DataRequired()])
    price       = DecimalField("Price", places=2, validators=[DataRequired(), NumberRange(min=0)])
    submit      = SubmitField("Save Price")


class PriceListImportForm(FlaskForm):
    """Upload a price sheet built on the CSV template for one period."""

    file = FileField(
        "Price File",
        validators=[
            FileRequired(message="Choose a CSV or Excel file to import."),
            FileAllowed(["csv", "xlsx", "xlsm"], "CSV or Excel files only."),
        ],
    )
    submit = SubmitField("Import Prices")


class PriceLookupForm(FlaskForm):
    lookup_date = DateField("Date", validators=[DataRequired()])
    customer_id = SelectField("Customer", coerce=int, validators=[DataRequired()])
    product_id  = SelectField("Product",  coerce=int, validators=[DataRequired()])
    submit      = SubmitField("Look Up Price")


# ---------------- PRODUCT ----------------
class ProductForm(FlaskForm):
    name = StringField(
        "Product Name",
        validators=[DataRequired(), Length(max=100)]
    )

    product_code = StringField(
        "Product Code",
        validators=[Optional(), Length(max=50)]
    )

    supplier_code = StringField(
        "Supplier Code",
        validators=[Optional(), Length(max=50)]
    )

    simplified_code = StringField(
        "Simplified Code",
        validators=[Optional(), Length(max=50)],
        description="Short form of the code, e.g. 1203 VB2 00 → VB2 00.",
    )

    supplier_description = StringField(
        "Supplier Description",
        validators=[Optional(), Length(max=255)]
    )

    # Material grade. "" == not specified.
    grade = SelectField(
        "Grade",
        choices=[("", "— None —")] + [(g, g) for g in PRODUCT_GRADES],
        validators=[Optional()]
    )

    # Drawing revision level, e.g. "AA", "OOO", "AB" — free text, no fixed list.
    drawing_level = StringField(
        "Drawing Level",
        validators=[Optional(), Length(max=10)]
    )

    price = DecimalField(
        "Price",
        places=2,
        validators=[Optional(), NumberRange(min=0, message="Price cannot be negative.")]
    )

    # Cast weight in kg — price per kg is worked out from this.
    weight = DecimalField(
        "Weight (kg)",
        places=3,
        validators=[Optional(), NumberRange(min=0, message="Weight cannot be negative.")]
    )

    # Departments this product is worked in (many-to-many)
    departments = SelectMultipleField(
        "Departments",
        coerce=int,
        validators=[Optional()]
    )

    # Held as stock and counted in stocktakes, or made to order?
    is_stock_item = BooleanField("Stock Item", default=True)

    # Off = keep the record and its history, but drop it from the pickers.
    active = BooleanField("Active", default=True)

    # Primary customer (used by Fettling / Stocktake). 0 == none.
    customer_id = SelectField(
        "Primary Customer",
        coerce=int,
        validators=[Optional()]  # matches DB (nullable FK)
    )

    # Additional many-to-many customer links
    linked_customers = SelectMultipleField(
        "Also Linked Customers",
        coerce=int,
        validators=[Optional()]
    )

    submit = SubmitField("Save")




class ProductImportForm(FlaskForm):
    """Upload a product sheet built on the CSV template."""

    file = FileField(
        "Product File",
        validators=[
            FileRequired(message="Choose a CSV or Excel file to import."),
            FileAllowed(["csv", "xlsx", "xlsm"], "CSV or Excel files only."),
        ],
    )
    submit = SubmitField("Import Products")


class LicenseUploadForm(FlaskForm):
    file = FileField('License File', validators=[
        FileRequired(),                       # <-- ensures a file is uploaded
        FileAllowed(['json'], 'JSON files only!')  # <-- only JSON allowed
    ])
    submit = SubmitField('Upload License') 