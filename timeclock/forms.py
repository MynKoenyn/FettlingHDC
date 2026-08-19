"""
Time clock module — WTForms

The upload, the day-row edit, and a bare confirm form that gives the various
one-button posts (reverse, re-match, revert) their CSRF token.
"""

from flask_wtf import FlaskForm
from flask_wtf.file import FileAllowed, FileField, FileRequired
from wtforms import (
    BooleanField,
    DecimalField,
    SelectField,
    StringField,
    SubmitField,
    TimeField,
)
from wtforms.validators import Length, NumberRange, Optional


class ClockImportForm(FlaskForm):
    """
    Upload one Turbo Time report.

    Either report loads — the parser works out which it is — but the Full
    Clocking Report is the one to use: it prints every day of the period,
    worked or not, so absences and short days come in with the rest. The
    Overtime Report only prints days that carried overtime.
    """

    file = FileField(
        "Report File",
        validators=[
            FileRequired(message="Choose the report .TXT file to import."),
            # Turbo Time also offers the report as ".XLS", but that file is an
            # OpenDocument spreadsheet under an Excel name — it carries the
            # same data in a messier form and needs a reader we do not ship.
            # The .TXT is the one to upload.
            FileAllowed(["txt"], "Export the report as .TXT from Turbo Time and upload that."),
        ],
    )
    submit = SubmitField("Import Report")


class ClockDayForm(FlaskForm):
    """
    Edit one imported day.

    The hours are open fields rather than a recalculation of the times: the
    clock applies its own shift rules, rounding and grace periods when it works
    out normal versus overtime, and second-guessing those here would silently
    disagree with the payroll figure the report was printed for. An edit is a
    correction to what the clock said, so it says what the corrected figure is.
    """

    time_in  = TimeField("Clock in", validators=[Optional()])
    time_out = TimeField("Clock out", validators=[Optional()])

    normal_hours = DecimalField("Normal hours", places=2,
                                validators=[Optional(), NumberRange(min=0, max=24)])
    ot1_hours = DecimalField("Overtime 1", places=2,
                             validators=[Optional(), NumberRange(min=0, max=24)])
    ot2_hours = DecimalField("Overtime 2", places=2,
                             validators=[Optional(), NumberRange(min=0, max=24)])
    ot3_hours = DecimalField("Overtime 3", places=2,
                             validators=[Optional(), NumberRange(min=0, max=24)])
    ot4_hours = DecimalField("Overtime 4", places=2,
                             validators=[Optional(), NumberRange(min=0, max=24)])
    total_hours = DecimalField("Total hours", places=2,
                               validators=[Optional(), NumberRange(min=0, max=24)])

    description = StringField("Description", validators=[Optional(), Length(max=60)])
    edit_note = StringField(
        "Why",
        validators=[Optional(), Length(max=255)],
        description="Kept against the row so the change can be explained later.",
    )

    submit = SubmitField("Save Row")


class MatchForm(FlaskForm):
    """Attach one clock employee to a personnel record by hand."""

    personnel_id = SelectField("Personnel", coerce=int, validators=[Optional()])
    remember = BooleanField(
        "Remember this for future imports",
        default=True,
        description="Writes the link so next week's report matches this number on its own.",
    )
    apply_to_other_batches = BooleanField(
        "Apply to other imports of the same number",
        description="Updates batches already loaded. Matches made by hand elsewhere are left alone.",
    )
    submit = SubmitField("Match")


class ConfirmForm(FlaskForm):
    """CSRF-protected confirm for the one-button posts."""

    submit = SubmitField("Confirm")
