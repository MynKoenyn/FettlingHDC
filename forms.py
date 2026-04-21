from flask_wtf import FlaskForm
from wtforms import (
    StringField,
    PasswordField,
    IntegerField,
    SelectField,
    DateField,
    SubmitField,
    FileField
)
from wtforms.validators import DataRequired, Length, NumberRange, Optional
from flask_wtf.file import FileField, FileRequired, FileAllowed

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
class UserForm(FlaskForm):
    username = StringField(
        "Username",
        validators=[DataRequired(), Length(max=50)]
    )

    password = PasswordField(
        "Password",
        validators=[DataRequired(), Length(max=255)]
    )

    submit = SubmitField("Save")


# ---------------- SUPPLIER ----------------
class SupplierForm(FlaskForm):
    name = StringField(
        "Customer Name",
        validators=[DataRequired(), Length(max=100)]
    )
    submit = SubmitField("Save")


# ---------------- PRODUCT ----------------
class ProductForm(FlaskForm):
    name = StringField(
        "Product Name",
        validators=[DataRequired(), Length(max=100)]
    )

    customer_id = SelectField(
        "Customer",
        coerce=int,
        validators=[Optional()]  # matches DB (nullable FK)
    )

    submit = SubmitField("Save")




class LicenseUploadForm(FlaskForm):
    file = FileField('License File', validators=[
        FileRequired(),                       # <-- ensures a file is uploaded
        FileAllowed(['json'], 'JSON files only!')  # <-- only JSON allowed
    ])
    submit = SubmitField('Upload License') 