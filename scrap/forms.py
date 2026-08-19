"""
Scrap module — WTForms

External scrap arrives by file upload (ScrapImportForm); internal scrap is
typed in on the capture form (InternalScrapForm). The defect breakdown on the
capture form is rendered from the ScrapDefect catalogue rather than declared
here, so adding a reject reason needs no code change — routes read those
quantities off request.form as `defect_<id>`.
"""

from flask_wtf import FlaskForm
from flask_wtf.file import FileAllowed, FileField, FileRequired
from wtforms import (
    BooleanField,
    DateField,
    IntegerField,
    SelectField,
    StringField,
    SubmitField,
    TextAreaField,
)
from wtforms.validators import DataRequired, InputRequired, Length, NumberRange, Optional

from scrap.models import SCOPE_BOTH, SCOPE_CHOICES


class ScrapImportForm(FlaskForm):
    """Upload a customer's external reject report."""

    customer_id = SelectField("Customer", coerce=int, validators=[DataRequired()])
    file = FileField(
        "Report File",
        validators=[
            FileRequired(message="Choose a CSV or Excel file to import."),
            FileAllowed(["csv", "xlsx", "xlsm"], "CSV or Excel files only."),
        ],
    )
    default_date = DateField(
        "Fallback Reject Date",
        validators=[Optional()],
        description="Used only for rows whose Reject Date cell is blank.",
    )
    submit = SubmitField("Import Report")


class InternalScrapForm(FlaskForm):
    """Capture one internal scrap line by hand."""

    entry_date  = DateField("Scrap Date", validators=[DataRequired()])
    customer_id = SelectField("Customer", coerce=int, validators=[Optional()])
    product_id  = SelectField("Product", coerce=int, validators=[Optional()])

    casting_no = StringField("Casting Number", validators=[Optional(), Length(max=60)])
    batch_no   = StringField("Heat Number", validators=[Optional(), Length(max=40)])

    total_packed = IntegerField(
        "Total Packed",
        validators=[Optional(), NumberRange(min=0)],
        description="Total units packed so far, outstanding balance included — "
                     "Quantity Packed is worked out from this minus the balance below.",
    )
    qty_packed = IntegerField(
        "Quantity Packed",
        validators=[Optional(), NumberRange(min=0)],
        description="Quantity packed or inspected — the base for the reject %. "
                     "Filled in automatically once Total Packed is entered.",
    )
    qty_scrap = IntegerField(
        "Qty Scrapped",
        validators=[InputRequired(message="Enter a scrap quantity (0 if nothing was scrapped)."),
                    NumberRange(min=0)],
    )

    notes  = TextAreaField("Notes", validators=[Optional(), Length(max=255)])
    submit = SubmitField("Save Scrap Entry")


class ScrapDispatchForm(FlaskForm):
    """
    Capture a dispatch — a truck picking up packed stock, one or more parts.

    product_id / qty_dispatched are only used when editing a single existing
    HDC dispatch row (the route enforces they're filled in then). A new
    dispatch is otherwise captured as one or more line items instead, read
    off request.form as line_product_<n> / line_qty_<n> / … — the same
    "extra inputs not declared here" pattern the internal scrap form uses
    for its defect breakdown. HDA's cage lines additionally carry
    line_weight_<n> / line_trenstar_<n> / line_head_<n> / line_drawing_<n>
    and four line_<check>_<n> checkboxes, all read the same way.

    invoice_no / dispatcher_id / total_black_bags only apply to HDA's
    cage-based dispatch — left unrendered (and unused) on HDC's screens.
    """

    dispatch_date = DateField("Dispatch Date", validators=[DataRequired()])
    customer_id   = SelectField("Customer", coerce=int, validators=[Optional()])
    product_id    = SelectField("Product", coerce=int, validators=[Optional()])

    qty_dispatched = IntegerField(
        "Quantity Dispatched",
        validators=[Optional(), NumberRange(min=1, message="Dispatched quantity must be at least 1.")],
    )

    # ── HDA cage-based dispatch header — unused on HDC's screens ──
    invoice_no = StringField("Invoice Number", validators=[Optional(), Length(max=50)])
    dispatcher_id = SelectField("Dispatcher", coerce=int, validators=[Optional()])
    total_black_bags = IntegerField(
        "Total Black Bags",
        validators=[Optional(), NumberRange(min=0)],
        description="Manually counted — a heavy cage can take more than one bag.",
    )

    notes  = TextAreaField("Notes", validators=[Optional(), Length(max=255)])
    submit = SubmitField("Save Dispatch")


class ScrapDefectForm(FlaskForm):
    """Add or edit a reject reason."""

    code = StringField("Code", validators=[DataRequired(), Length(max=10)])
    name = StringField(
        "Name",
        validators=[DataRequired(), Length(max=120)],
        description="Also the column heading used for CSV import and export.",
    )
    description = StringField("Description", validators=[Optional(), Length(max=255)])
    aliases = StringField(
        "Alternative Headings",
        validators=[Optional(), Length(max=255)],
        description="Comma-separated spellings a customer's sheet may use.",
    )
    applies_to = SelectField(
        "Used on",
        choices=SCOPE_CHOICES,
        default=SCOPE_BOTH,
        validators=[DataRequired()],
        description="Which scrap side offers this reason.",
    )
    sort_order = IntegerField("Sort Order", validators=[Optional(), NumberRange(min=0)])
    active = BooleanField("Active", default=True)
    submit = SubmitField("Save Reason")


class DeleteForm(FlaskForm):
    """CSRF-protected confirm for destructive posts."""

    submit = SubmitField("Delete")
