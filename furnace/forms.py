from flask_wtf import FlaskForm
from wtforms import StringField, FloatField, IntegerField, SelectField, TextAreaField, DateField, TimeField, SubmitField
from wtforms.validators import DataRequired, Optional, NumberRange, Length, Regexp, InputRequired
from datetime import datetime
from models import Personnel
from furnace.models import Furnace, MetalGrade
from flask import request


class FurnaceForm(FlaskForm):
    name = StringField('Furnace Name', validators=[DataRequired(), Length(max=100)])
    capacity = FloatField('Capacity', validators=[Optional(), NumberRange(min=0)])
    capacity_unit = SelectField('Unit', choices=[('tons', 'Tons'), ('kg', 'Kilograms')], default='tons')
    current_lining_number = IntegerField('Current Lining Number', validators=[Optional(), NumberRange(min=1)], default=1)
    status = SelectField('Status', choices=[('Active', 'Active'), ('Maintenance', 'Maintenance'), ('Inactive', 'Inactive')], default='Active')
    submit = SubmitField('Save Furnace')


class MetalGradeForm(FlaskForm):
    name = StringField('Grade Name', validators=[DataRequired(), Length(max=100)])
    description = TextAreaField('Description', validators=[Optional()])
    notes = TextAreaField('Notes', validators=[Optional()])
    submit = SubmitField('Save Metal Grade')


class FurnaceEntryForm(FlaskForm):
    # Basic information
    date = DateField('Date', validators=[DataRequired()], default=datetime.now().date)
    heat_number = StringField('Heat Number', validators=[DataRequired(), Length(max=50), Regexp(r'^\d+$', message="Heat number must contain digits only (e.g. 0408).")])
    furnace_id = SelectField('Furnace', coerce=str, validators=[DataRequired(message="Please select a furnace.")])
    metal_grade_id = SelectField('Metal Grade', coerce=str, validators=[DataRequired(message="Please select a metal grade.")])
    melt_technician_id = SelectField('Melt Technician', coerce=str, validators=[DataRequired(message="Please select a melt technician.")])
    furnace_operator_id = SelectField('Furnace Operator', coerce=str, validators=[DataRequired(message="Please select a furnace operator.")])
    lining_number = IntegerField('Lining Number', validators=[Optional(), NumberRange(min=1)])

    # Base materials
    cast_iron = FloatField('Cast Iron (kg)', validators=[InputRequired(), NumberRange(min=0)], default=1700.0)
    steel_scrap = FloatField('Steel Scrap (kg)', validators=[InputRequired(), NumberRange(min=0)], default=300.0)
    pig_iron = FloatField('Pig Iron (kg)', validators=[InputRequired(), NumberRange(min=0)], default=0.0)
    recarb = FloatField('Recarb (kg)', validators=[InputRequired(), NumberRange(min=0)])
    ferrosilicon = FloatField('Ferrosilicon (kg)', validators=[InputRequired(), NumberRange(min=0)])
    ferromanganese = FloatField('Ferromanganese (kg)', validators=[InputRequired(), NumberRange(min=0)])
    iron_sulfide = FloatField('Iron Sulfide (kg)', validators=[InputRequired(), NumberRange(min=0)])

    # Additional materials
    additional_recarb = FloatField('Additional Recarb (kg)', validators=[InputRequired(), NumberRange(min=0)])
    additional_fesi = FloatField('Additional FeSi (kg)', validators=[InputRequired(), NumberRange(min=0)])
    additional_femn = FloatField('Additional FeMn (kg)', validators=[InputRequired(), NumberRange(min=0)])
    additional_iron_sulfide = FloatField('Additional Iron Sulfide (kg)', validators=[InputRequired(), NumberRange(min=0)])
    tin = FloatField('Tin (kg)', validators=[InputRequired(), NumberRange(min=0)])
    copper = FloatField('Copper (kg)', validators=[InputRequired(), NumberRange(min=0)])

    # Process details
    melt_temperature = FloatField('Melt Temperature (°C)', validators=[Optional(), NumberRange(min=1300, max=1560, message="Melt temperature must be between 1300°C and 1560°C.")])

    remarks = TextAreaField('Remarks', validators=[Optional()])

    # Timestamp fields (hidden, filled by JavaScript)
    start_charging_time = StringField('Start Charging Time', validators=[Optional()])
    additions_added_time = StringField('Additions Added Time', validators=[Optional()])
    tap_times = StringField('Tap Times', validators=[Optional()])
    end_melt_time = StringField('End Melt Time', validators=[Optional()])

    submit = SubmitField('Save Entry')

    def __init__(self, *args, **kwargs):
        super(FurnaceEntryForm, self).__init__(*args, **kwargs)
        # Populate choices from database
        self.furnace_id.choices = [("", 'Select Furnace')] + [(f.id, f.name) for f in Furnace.query.filter_by(status='Active').all()]
        self.metal_grade_id.choices = [("", 'Select Metal Grade')] + [(g.id, g.name) for g in MetalGrade.query.all()]

        # Filter shared Personnel by furnace_role, active only
        melt_techs = Personnel.query.filter_by(furnace_role='Melt Technician', status=True).all()
        operators = Personnel.query.filter_by(furnace_role='Furnace Operator', status=True).all()

        self.melt_technician_id.choices = [("", 'Select Melt Technician')] + [(p.id, f"{p.name} ({p.clockno})") for p in melt_techs]
        self.furnace_operator_id.choices = [("", 'Select Furnace Operator')] + [(p.id, f"{p.name} ({p.clockno})") for p in operators]

        for field_name in ['start_charging_time', 'additions_added_time', 'tap_times', 'end_melt_time']:
            if request.method == 'POST' and field_name in request.form:
                getattr(self, field_name).data = request.form.get(field_name)


class EntryFilterForm(FlaskForm):
    furnace_id = SelectField('Furnace', coerce=int)
    metal_grade_id = SelectField('Metal Grade', coerce=int)
    date_from = DateField('From Date', validators=[Optional()])
    date_to = DateField('To Date', validators=[Optional()])

    def __init__(self, *args, **kwargs):
        super(EntryFilterForm, self).__init__(*args, **kwargs)
        self.furnace_id.choices = [(0, 'All Furnaces')] + [(f.id, f.name) for f in Furnace.query.all()]
        self.metal_grade_id.choices = [(0, 'All Metal Grades')] + [(g.id, g.name) for g in MetalGrade.query.all()]


class TinCopperForm(FlaskForm):
    date = DateField('Date', validators=[DataRequired()], default=datetime.now().date)
    heat_number = StringField('Heat No', validators=[Optional(), Length(max=50)])
    operator_id = SelectField('Operator', coerce=str, validators=[DataRequired(message="Please select an operator.")])
    furnace_id = SelectField('Furnace', coerce=str, validators=[DataRequired(message="Please select a furnace.")])
    metal_grade_id = SelectField('Grade', coerce=str, validators=[DataRequired(message="Please select a grade.")])
    weight = SelectField('Weight (kg)', choices=[('2000', '2000'), ('500', '500'), ('250', '250')], validators=[DataRequired()])

    base_tin = FloatField('Base Tin (%)', validators=[Optional(), NumberRange(min=0)])
    tin_added = FloatField('Tin Added (kg)', validators=[Optional(), NumberRange(min=0)])

    base_copper = FloatField('Base Copper (%)', validators=[Optional(), NumberRange(min=0)])
    copper_added = FloatField('Copper Added (kg)', validators=[Optional(), NumberRange(min=0)])

    starting_tin = FloatField('Starting Tin (kg)', validators=[Optional(), NumberRange(min=0)])
    starting_copper = FloatField('Starting Copper (kg)', validators=[Optional(), NumberRange(min=0)])
    tin_issued = FloatField('Tin Issued (kg)', validators=[Optional(), NumberRange(min=0)])
    copper_issued = FloatField('Copper Issued (kg)', validators=[Optional(), NumberRange(min=0)])

    submit = SubmitField('Save')

    def __init__(self, *args, **kwargs):
        super(TinCopperForm, self).__init__(*args, **kwargs)
        melt_techs = Personnel.query.filter_by(furnace_role='Melt Technician', status=True).all()
        self.operator_id.choices = [("", 'Select Operator')] + [(str(p.id), f"{p.name} ({p.clockno})") for p in melt_techs]
        self.furnace_id.choices = [("", 'Select Furnace')] + [(str(f.id), f.name) for f in Furnace.query.filter_by(status='Active').all()]
        self.metal_grade_id.choices = [("", 'Select Grade')] + [(str(g.id), g.name) for g in MetalGrade.query.all()]


class TinCopperFilterForm(FlaskForm):
    furnace_id = SelectField('Furnace', coerce=int)
    metal_grade_id = SelectField('Grade', coerce=int)
    date_from = DateField('From', validators=[Optional()])
    date_to = DateField('To', validators=[Optional()])

    def __init__(self, *args, **kwargs):
        super(TinCopperFilterForm, self).__init__(*args, **kwargs)
        self.furnace_id.choices = [(0, 'All Furnaces')] + [(f.id, f.name) for f in Furnace.query.all()]
        self.metal_grade_id.choices = [(0, 'All Grades')] + [(g.id, g.name) for g in MetalGrade.query.all()]


class ReportForm(FlaskForm):
    date_from = DateField('From Date', validators=[DataRequired()])
    date_to = DateField('To Date', validators=[DataRequired()])
    time_from = TimeField('Time From', validators=[Optional()])
    time_to = TimeField('Time To', validators=[Optional()])
    furnace_id = SelectField('Furnace', coerce=int)
    submit = SubmitField('Generate Report')

    def __init__(self, *args, **kwargs):
        super(ReportForm, self).__init__(*args, **kwargs)
        self.furnace_id.choices = [(0, 'All Furnaces')] + [(f.id, f.name) for f in Furnace.query.all()]
