from flask_wtf import FlaskForm
from wtforms import (
    StringField,
    PasswordField,
    IntegerField,
    SelectField,
    DateField,
    SubmitField
)
from wtforms.validators import DataRequired, Length, NumberRange, Optional


# ---------------- FETTLING ENTRY ----------------
class EntryForm(FlaskForm):
    entry_date = DateField("Entry Date", format="%Y-%m-%d", validators=[DataRequired()])
    submit = SubmitField("Save Entries")