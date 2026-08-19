from datetime import datetime, timedelta
from enum import Enum

from sqlalchemy import event

from app import db


class Shift(Enum):
    SHIFT_1 = "shift_1"  # 06:00-14:00
    SHIFT_2 = "shift_2"  # 14:00-22:00
    SHIFT_3 = "shift_3"  # 22:00-06:00


class Machine(Enum):
    LAUDS_1 = "lauds_1"
    LAUDS_2 = "lauds_2"
    LAUDS_3 = "lauds_3"
    LAUDS_4 = "lauds_4"
    LAUDS_5 = "lauds_5"
    TOP = "top"
    BOTTOM = "bottom"
    VICK = "vick"


class RemarkCategory(Enum):
    MECHANICAL = "mechanical"
    CHEMICAL = "chemical"
    SAND = "sand"
    CLEANING = "cleaning"
    TOOL = "tool"
    OPERATOR = "operator"


SHIFT_HOURS = {
    Shift.SHIFT_1: range(6, 14),
    Shift.SHIFT_2: range(14, 22),
    Shift.SHIFT_3: list(range(22, 24)) + list(range(0, 6)),
}

SHIFT_LABELS = {
    Shift.SHIFT_1: "Shift 1 (06:00-14:00)",
    Shift.SHIFT_2: "Shift 2 (14:00-22:00)",
    Shift.SHIFT_3: "Shift 3 (22:00-06:00)",
}

MACHINE_LABELS = {
    Machine.LAUDS_1: "Lauds 1",
    Machine.LAUDS_2: "Lauds 2",
    Machine.LAUDS_3: "Lauds 3",
    Machine.LAUDS_4: "Lauds 4",
    Machine.LAUDS_5: "Lauds 5",
    Machine.TOP: "Top",
    Machine.BOTTOM: "Bottom",
    Machine.VICK: "Vick",
}


class ProductionEntry(db.Model):
    __tablename__ = "production_entries"

    id = db.Column(db.Integer, primary_key=True)
    date = db.Column(db.Date, nullable=False)
    production_date = db.Column(db.Date, nullable=False)  # Date the shift's production is attributed to
    shift = db.Column(db.Enum(Shift, name="dp_shift"), nullable=False)
    hour = db.Column(db.Integer, nullable=False)  # 0-23
    machine = db.Column(db.Enum(Machine, name="dp_machine"), nullable=False)
    cores_produced = db.Column(db.Integer, default=0, nullable=False)
    defects = db.Column(db.Integer, default=0)
    remark_category = db.Column(db.Enum(RemarkCategory, name="dp_remark_category"), nullable=True)
    remark_text = db.Column(db.Text, nullable=True)
    downtime_minutes = db.Column(db.Integer, default=0)

    # Operators are shop-floor Personnel (no login); supervisors are Users.
    operator_id = db.Column(db.Integer, db.ForeignKey("personnel.id"), nullable=False)
    supervisor_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)

    created_at = db.Column(db.DateTime, default=datetime.now)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    operator = db.relationship("Personnel", foreign_keys=[operator_id])
    supervisor = db.relationship("User", foreign_keys=[supervisor_id])

    __table_args__ = (
        db.UniqueConstraint("date", "shift", "hour", "machine", name="unique_production_entry"),
    )

    @property
    def shift_display(self):
        return SHIFT_LABELS.get(self.shift, "Unknown Shift")

    @property
    def machine_display(self):
        return MACHINE_LABELS.get(self.machine, "Unknown Machine")

    def calculate_production_date(self):
        if not self.date or self.hour is None:
            return self.date
        if 0 <= self.hour <= 5:
            return self.date - timedelta(days=1)
        return self.date

    def __repr__(self):
        return f"<ProductionEntry {self.date} {self.shift} {self.machine}>"


@event.listens_for(ProductionEntry, "before_insert")
def _set_production_date_before_insert(mapper, connection, target):
    target.production_date = target.calculate_production_date()


@event.listens_for(ProductionEntry, "before_update")
def _set_production_date_before_update(mapper, connection, target):
    target.production_date = target.calculate_production_date()


class ProductionTarget(db.Model):
    __tablename__ = "production_targets"

    id = db.Column(db.Integer, primary_key=True)
    machine = db.Column(db.Enum(Machine, name="dp_machine"), nullable=False)
    hour = db.Column(db.Integer, nullable=False)  # 0-23
    hourly_target = db.Column(db.Integer, nullable=False)
    shift_target = db.Column(db.Integer, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.now)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        db.UniqueConstraint("machine", "hour", name="unique_machine_hour_target"),
    )

    def __repr__(self):
        return f"<ProductionTarget {self.machine.value} Hour {self.hour}: {self.hourly_target} cores>"
