from collections import namedtuple
from datetime import datetime
from app import db

# An actual addition is acceptable while it stays within +/-10% of the required
# amount. The bound is inclusive: required 10 kg accepts 9.0 - 11.0 kg.
ADDITION_TOLERANCE = 0.10

# The furnace_role choices offered on the shared Personnel edit form
# (overtime/routes.py's _personnel_form) and used to filter the melt
# technician / furnace operator pickers on furnace entries.
FURNACE_ROLES = ["Melt Technician", "Furnace Operator"]

AdditionCheck = namedtuple('AdditionCheck', ['status', 'required', 'low', 'high'])


def check_addition(required, actual):
    """Compare an actual Sn/Cu addition against the calculated requirement.

    Returns an AdditionCheck with status 'ok' (within tolerance) or 'off'
    (outside it), or None when there is nothing to compare. A negative
    requirement means the melt is already at/above target, so it counts as 0 -
    anything added then is off.
    """
    if actual is None:
        return None
    required = required if required and required > 0 else 0
    if required == 0:
        return AdditionCheck('ok' if actual == 0 else 'off', 0, 0, 0)
    margin = required * ADDITION_TOLERANCE
    # Guard the inclusive bound against float noise (0.35 * 0.1 is not exact).
    status = 'ok' if abs(actual - required) <= margin + 1e-9 else 'off'
    return AdditionCheck(status, required, required - margin, required + margin)


class Furnace(db.Model):
    __tablename__ = 'furnaces'

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False, unique=True)
    capacity = db.Column(db.Float)
    capacity_unit = db.Column(db.String(20), default='tons')
    current_lining_number = db.Column(db.Integer, default=1)
    status = db.Column(db.String(50), default='Active')
    created_at = db.Column(db.DateTime, default=datetime.now)

    entries = db.relationship('FurnaceEntry', backref='furnace_ref', lazy=True)

    def __repr__(self):
        return f'<Furnace {self.name}>'


class MetalGrade(db.Model):
    __tablename__ = 'metal_grades'

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False, unique=True)
    description = db.Column(db.Text)
    notes = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.now)

    entries = db.relationship('FurnaceEntry', backref='metal_grade_ref', lazy=True)

    def __repr__(self):
        return f'<MetalGrade {self.name}>'


class FurnaceEntry(db.Model):
    __tablename__ = 'furnace_entries'

    id = db.Column(db.Integer, primary_key=True)
    date = db.Column(db.Date, nullable=False, default=datetime.now().date)
    heat_number = db.Column(db.String(50))

    furnace_id = db.Column(db.Integer, db.ForeignKey('furnaces.id'))
    metal_grade_id = db.Column(db.Integer, db.ForeignKey('metal_grades.id'))
    # Both point at the shared Personnel table (not a furnace-only one) —
    # melting-division staff are the same personnel every other module sees.
    melt_technician_id = db.Column(db.Integer, db.ForeignKey('personnel.id'))
    furnace_operator_id = db.Column(db.Integer, db.ForeignKey('personnel.id'))

    melt_technician = db.relationship('Personnel', foreign_keys=[melt_technician_id],
                                       backref='melt_technician_entries')
    furnace_operator = db.relationship('Personnel', foreign_keys=[furnace_operator_id],
                                        backref='furnace_operator_entries')

    lining_number = db.Column(db.Integer)

    # Base materials (all default to 0)
    cast_iron = db.Column(db.Float, default=0.0)
    steel_scrap = db.Column(db.Float, default=0.0)
    pig_iron = db.Column(db.Float)
    recarb = db.Column(db.Float)
    ferrosilicon = db.Column(db.Float)
    ferromanganese = db.Column(db.Float)
    iron_sulfide = db.Column(db.Float)

    # Additional materials
    additional_recarb = db.Column(db.Float)
    additional_fesi = db.Column(db.Float)
    additional_femn = db.Column(db.Float)
    additional_iron_sulfide = db.Column(db.Float)
    tin = db.Column(db.Float)
    copper = db.Column(db.Float)

    # Process details
    melt_temperature = db.Column(db.Float)
    inoculate_used = db.Column(db.String(10))  # 'Yes' or 'No'
    remarks = db.Column(db.Text)

    # Timestamps
    start_charging_time = db.Column(db.DateTime)
    additions_added_time = db.Column(db.DateTime)
    tap_times = db.Column(db.Text)  # JSON string for multiple tap times (legacy)
    end_melt_time = db.Column(db.DateTime)

    # Meta
    created_at = db.Column(db.DateTime, default=datetime.now)
    updated_at = db.Column(db.DateTime, default=datetime.now, onupdate=datetime.now)
    last_activity_at = db.Column(db.DateTime)
    completed_at = db.Column(db.DateTime)
    status = db.Column(db.String(15), default='In Progress')  # 'In Progress' or 'Completed'

    tap_events = db.relationship("FurnaceTapTime", backref="furnace_entry",
                                  cascade="all, delete-orphan", passive_deletes=True, lazy=True)

    def __repr__(self):
        return f'<FurnaceEntry {self.heat_number}>'

    @property
    def total_base_materials(self):
        return (
            (self.cast_iron or 0) +
            (self.steel_scrap or 0) +
            (self.pig_iron or 0) +
            (self.recarb or 0) +
            (self.ferrosilicon or 0) +
            (self.ferromanganese or 0) +
            (self.iron_sulfide or 0)
        )

    @property
    def total_additional_materials(self):
        return (
            (self.additional_recarb or 0) +
            (self.additional_fesi or 0) +
            (self.additional_femn or 0) +
            (self.additional_iron_sulfide or 0) +
            (self.tin or 0) +
            (self.copper or 0)
        )

    @property
    def total_materials(self):
        return self.total_base_materials + self.total_additional_materials

    @staticmethod
    def format_duration_seconds(seconds):
        """Format a seconds value as 'Hh Mm Ss'. None/negative shows as 0h 0m 0s."""
        total_seconds = int(seconds) if seconds else 0
        if total_seconds < 0:
            total_seconds = 0
        hours, remainder = divmod(total_seconds, 3600)
        minutes, secs = divmod(remainder, 60)
        return f"{hours}h {minutes}m {secs}s"

    @staticmethod
    def _duration_seconds(start, end):
        """Seconds between two datetimes, or None if either is missing or the result is negative."""
        if not start or not end:
            return None
        seconds = (end - start).total_seconds()
        return seconds if seconds >= 0 else None

    @property
    def sorted_tap_times(self):
        return sorted(t.tap_time for t in self.tap_events if t.tap_time)

    @property
    def first_tap_time(self):
        times = self.sorted_tap_times
        return times[0] if times else None

    @property
    def last_tap_time(self):
        times = self.sorted_tap_times
        return times[-1] if times else None

    @property
    def melt_time_seconds(self):
        """Start Charging -> End Melt, in seconds (None if not computable)."""
        return self._duration_seconds(self.start_charging_time, self.end_melt_time)

    @property
    def corrections_seconds(self):
        """End Melt -> first tap, in seconds (None if not computable)."""
        return self._duration_seconds(self.end_melt_time, self.first_tap_time)

    @property
    def furnace_emptying_seconds(self):
        """First tap -> last tap, in seconds (None if not computable)."""
        return self._duration_seconds(self.first_tap_time, self.last_tap_time)

    @property
    def full_melt_seconds(self):
        """Start Charging -> last tap, in seconds (None if not computable)."""
        return self._duration_seconds(self.start_charging_time, self.last_tap_time)

    @property
    def melt_time(self):
        return self.format_duration_seconds(self.melt_time_seconds)

    @property
    def corrections_time(self):
        return self.format_duration_seconds(self.corrections_seconds)

    @property
    def furnace_emptying_time(self):
        return self.format_duration_seconds(self.furnace_emptying_seconds)

    @property
    def full_melt_time(self):
        return self.format_duration_seconds(self.full_melt_seconds)


class FurnaceTapTime(db.Model):
    __tablename__ = 'furnace_tap_times'

    id = db.Column(db.Integer, primary_key=True)
    entry_id = db.Column(db.Integer, db.ForeignKey('furnace_entries.id', ondelete='CASCADE'), nullable=False)

    tap_time = db.Column(db.DateTime, nullable=False)
    temperature = db.Column(db.Float, nullable=True)
    innoculate = db.Column(db.String(20), nullable=True)
    department = db.Column(db.String(30), nullable=True)

    def __repr__(self):
        return f'<TapTime {self.tap_time}>'


class SpectroResult(db.Model):
    __tablename__ = 'spectro_results'

    id = db.Column(db.Integer, primary_key=True)
    entry_id = db.Column(
        db.Integer,
        db.ForeignKey('furnace_entries.id', ondelete='CASCADE'),
        nullable=True  # can be null initially — linked later via the lab linker
    )
    entry = db.relationship("FurnaceEntry", backref=db.backref("spectro_results", lazy=True))

    # General data
    measure_date = db.Column(db.Date, nullable=False)
    measure_time = db.Column(db.Time, nullable=False)
    method_name = db.Column(db.Text, nullable=True)
    calc_mode = db.Column(db.Text, nullable=True)

    # Operator and metadata
    melt_technician = db.Column(db.Text, nullable=True)
    grade_id = db.Column(db.Text, nullable=True)
    heat_number = db.Column(db.Text, nullable=True)
    plant = db.Column(db.Text, nullable=True)
    furnace = db.Column(db.Text, nullable=True)

    # Base or Final
    sample_type = db.Column(db.Text, nullable=True)
    pot_number = db.Column(db.Text, nullable=True)
    metal_grade = db.Column(db.Text, nullable=True)

    cu_addition = db.Column(db.Numeric(10, 3), nullable=True)
    sn_addition = db.Column(db.Numeric(10, 3), nullable=True)

    # Results (all in %)
    ele_c = db.Column(db.Numeric(10, 4), nullable=True)
    ele_si = db.Column(db.Numeric(10, 4), nullable=True)
    ele_mn = db.Column(db.Numeric(10, 4), nullable=True)
    ele_p = db.Column(db.Numeric(10, 4), nullable=True)
    ele_s = db.Column(db.Numeric(10, 4), nullable=True)
    ele_cr = db.Column(db.Numeric(10, 4), nullable=True)
    ele_mo = db.Column(db.Numeric(10, 4), nullable=True)
    ele_ni = db.Column(db.Numeric(10, 4), nullable=True)
    ele_al = db.Column(db.Numeric(10, 4), nullable=True)
    ele_co = db.Column(db.Numeric(10, 4), nullable=True)
    ele_cu = db.Column(db.Numeric(10, 4), nullable=True)
    ele_nb = db.Column(db.Numeric(10, 4), nullable=True)
    ele_ti = db.Column(db.Numeric(10, 4), nullable=True)
    ele_v = db.Column(db.Numeric(10, 4), nullable=True)
    ele_w = db.Column(db.Numeric(10, 4), nullable=True)
    ele_pb = db.Column(db.Numeric(10, 4), nullable=True)
    ele_sn = db.Column(db.Numeric(10, 4), nullable=True)
    ele_mg = db.Column(db.Numeric(10, 4), nullable=True)
    ele_as = db.Column(db.Numeric(10, 4), nullable=True)
    ele_zr = db.Column(db.Numeric(10, 4), nullable=True)
    ele_bi = db.Column(db.Numeric(10, 4), nullable=True)
    ele_ce = db.Column(db.Numeric(10, 4), nullable=True)
    ele_sb = db.Column(db.Numeric(10, 4), nullable=True)
    ele_se = db.Column(db.Numeric(10, 4), nullable=True)
    ele_te = db.Column(db.Numeric(10, 4), nullable=True)
    ele_b = db.Column(db.Numeric(10, 4), nullable=True)
    ele_zn = db.Column(db.Numeric(10, 4), nullable=True)
    ele_la = db.Column(db.Numeric(10, 4), nullable=True)
    ele_n = db.Column(db.Numeric(10, 4), nullable=True)
    ele_fe = db.Column(db.Numeric(10, 4), nullable=True)

    created_at = db.Column(db.DateTime, server_default=db.func.now())

    def __repr__(self):
        return f'<SpectroResult heat_number={self.heat_number} date={self.measure_date}>'


class TinCopperCalculation(db.Model):
    __tablename__ = 'tin_copper_calculations'

    id = db.Column(db.Integer, primary_key=True)
    date = db.Column(db.Date, nullable=False, default=datetime.now().date)
    heat_number = db.Column(db.String(50), nullable=True)

    operator_id = db.Column(db.Integer, db.ForeignKey('personnel.id'), nullable=True)
    furnace_id = db.Column(db.Integer, db.ForeignKey('furnaces.id'), nullable=True)
    metal_grade_id = db.Column(db.Integer, db.ForeignKey('metal_grades.id'), nullable=True)

    weight = db.Column(db.Integer, nullable=False)  # 250, 500, or 2000 kg

    # Tin tracking
    base_tin = db.Column(db.Float, nullable=True)
    tin_to_be_added = db.Column(db.Float, nullable=True)   # server-calculated
    tin_added = db.Column(db.Float, nullable=True)          # manual entry

    # Copper tracking
    base_copper = db.Column(db.Float, nullable=True)
    copper_to_be_added = db.Column(db.Float, nullable=True)  # server-calculated
    copper_added = db.Column(db.Float, nullable=True)        # manual entry

    # Stock levels
    starting_tin = db.Column(db.Float, nullable=True)
    starting_copper = db.Column(db.Float, nullable=True)
    tin_issued = db.Column(db.Float, nullable=True)
    copper_issued = db.Column(db.Float, nullable=True)

    created_at = db.Column(db.DateTime, default=datetime.now)
    updated_at = db.Column(db.DateTime, default=datetime.now, onupdate=datetime.now)

    operator = db.relationship('Personnel', foreign_keys=[operator_id], backref='tc_calc_entries')
    furnace_rel = db.relationship('Furnace', foreign_keys=[furnace_id], backref='tc_calc_entries')
    metal_grade_rel = db.relationship('MetalGrade', foreign_keys=[metal_grade_id], backref='tc_calc_entries')

    @property
    def tin_check(self):
        """Tolerance check of tin_added against tin_to_be_added (None if N/A)."""
        return check_addition(self.tin_to_be_added, self.tin_added)

    @property
    def copper_check(self):
        """Tolerance check of copper_added against copper_to_be_added (None if N/A)."""
        return check_addition(self.copper_to_be_added, self.copper_added)

    def __repr__(self):
        return f'<TinCopperCalculation {self.heat_number} {self.date}>'
