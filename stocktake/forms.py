from flask_wtf import FlaskForm
from wtforms import (
    StringField, SelectField, DateField, TextAreaField,
    HiddenField, DecimalField, SubmitField
)
from wtforms.validators import DataRequired, Optional, NumberRange
from datetime import date


class StocktakeHeaderForm(FlaskForm):
    """Creates / opens a stocktake session."""
    date          = DateField("Date", default=date.today, validators=[DataRequired()])
    department_id = SelectField("Department", coerce=int, validators=[DataRequired()])
    section       = SelectField(
        "Section",
        choices=[("A","A"),("B","B"),("C","C"),("D","D"),("E","E"),("F","F")],
        validators=[DataRequired()]
    )
    notes         = TextAreaField("Notes", validators=[Optional()])
    submit        = SubmitField("Open Stocktake")


class BarcodeEntryForm(FlaskForm):
    """
    Barcode-scan entry form.
    The barcode field is auto-focused; on scan/enter the JS
    fires an AJAX lookup and populates the hidden fields + display labels.
    The user then types the count value and hits Add.
    """
    barcode       = StringField("Scan Barcode / Search", validators=[Optional()])
    customer_id   = HiddenField(validators=[DataRequired()])
    product_id    = HiddenField(validators=[DataRequired()])
    count_value   = DecimalField(
        "Count Value", places=3,
        validators=[DataRequired(), NumberRange(min=0)]
    )
    line_notes    = StringField("Line Notes", validators=[Optional()])
    submit        = SubmitField("Add Line")

class DeleteForm(FlaskForm):
    """Empty form used for delete actions, just to get CSRF protection."""
    submit = SubmitField("Delete")
