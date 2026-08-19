from datetime import date

from flask_wtf import FlaskForm
from wtforms import IntegerField, SelectField, SubmitField, TextAreaField
from wtforms.fields import DateField
from wtforms.validators import DataRequired, InputRequired, Length, NumberRange, Optional

from access.guards import user_can
from dailyproduction.models import Machine, RemarkCategory, Shift
from models import Department, Personnel, User


class ProductionEntryForm(FlaskForm):
    date = DateField("Date", validators=[DataRequired()], default=date.today)
    shift = SelectField(
        "Shift",
        choices=[(s.value, s.value.replace("_", " ").title()) for s in Shift],
        validators=[DataRequired()],
    )
    hour = SelectField(
        "Hour",
        choices=[(i, f"{i:02d}:00 - {i + 1:02d}:00") for i in range(24)],
        coerce=int,
        validators=[InputRequired()],
        validate_choice=False,
    )
    machine = SelectField(
        "Machine",
        choices=[(m.value, m.value.replace("_", " ").title()) for m in Machine],
        validators=[DataRequired()],
    )
    cores_produced = IntegerField(
        "Cores Produced",
        validators=[InputRequired(), NumberRange(min=0, max=10000)],
    )
    defects = IntegerField(
        "Defects",
        validators=[Optional(), NumberRange(min=0, max=1000)],
        default=0,
    )
    downtime_minutes = IntegerField(
        "Downtime (minutes)",
        validators=[Optional(), NumberRange(min=0, max=60)],
        default=0,
    )
    remark_category = SelectField(
        "Remark Category",
        choices=[("", "No Issues")] + [(r.value, r.value.replace("_", " ").title()) for r in RemarkCategory],
        validators=[Optional()],
    )
    remark_text = TextAreaField("Remark Details", validators=[Optional(), Length(max=500)])
    operator_id = SelectField(
        "Operator",
        coerce=lambda x: int(x) if x else None,
        validators=[DataRequired()],
    )
    supervisor_id = SelectField(
        "Supervisor",
        coerce=lambda x: int(x) if x else None,
        validators=[DataRequired()],
    )
    submit = SubmitField("Save Entry")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Supervisors log in (Users) and are picked from whoever can capture
        # dailyproduction entries — grant that via Access > Manage Users to
        # make someone selectable here. Operators are shop-floor Personnel
        # and don't need an account.
        active_users = User.query.filter_by(active=True).order_by(User.name).all()
        supervisors = [u for u in active_users if user_can(u, "dailyproduction", "capture")]
        self.supervisor_id.choices = [("", "Select")] + [(u.id, u.name) for u in supervisors]

        operators_query = Personnel.query.filter_by(status=True)
        core_blower = Department.query.filter_by(name="Core Blower").first()
        if core_blower:
            operators_query = operators_query.filter_by(department_id=core_blower.id)
        operators = operators_query.order_by(Personnel.name).all()
        self.operator_id.choices = [("", "Select")] + [
            (p.id, f"{p.name} {p.surname or ''}".strip()) for p in operators
        ]
