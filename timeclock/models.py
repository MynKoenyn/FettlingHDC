"""
Time clock module — models
==========================

What one Turbo Time report becomes once it is loaded:

    ClockImportBatch          one uploaded file
      └── ClockEmployee       one person on that report (their period totals)
            └── ClockDay      one printed day — clocked in/out, normal + overtime
                  └── ClockPunch   the full punch list, on an odd clocking

    ClockEmployeeLink         emp. number → Personnel, remembered between imports

Three things shape this design, all of them asked for up front:

*Reverse.* A batch owns its employees and their days by cascade, so undoing an
import is one delete and it cannot reach anything loaded before it. Nothing
here writes to `personnel` or `overtime_requests`, so a reversal leaves no
trace anywhere else either.

*Edit.* Every day row keeps `source_line` — the exact text the report printed.
An edited row can therefore always be put back to what the file said, without
trusting a second copy of the numbers, and `is_edited` shows at a glance which
rows are no longer what the clock reported.

*Match, and re-match.* The clock knows people by their own employee number,
which is usually but not always our clock number. Matching is decided once per
person per batch (not per day) and stored on ClockEmployee, so re-running it
touches 66 decisions rather than 157 rows. A match made by hand is written to
ClockEmployeeLink as well, which is what stops the same person having to be
matched again on next week's report.
"""

from datetime import datetime
from decimal import Decimal

from app import db
from timeclock.parser import DEFAULT_VARIANCE_END


# ── How a clock employee came to be linked to a personnel record ─────────────
MATCH_NONE    = "none"       # nothing matched — needs a decision
MATCH_CLOCKNO = "clockno"    # emp. number equals a Personnel clock number
MATCH_LINK    = "link"       # a remembered link from an earlier import
MATCH_NAME    = "name"       # matched on name, uniquely
MATCH_MANUAL  = "manual"     # chosen by hand on the batch screen
MATCH_IGNORED = "ignored"    # deliberately left unlinked (not our employee)

MATCH_LABELS = {
    MATCH_NONE:    "Not matched",
    MATCH_CLOCKNO: "Clock number",
    MATCH_LINK:    "Remembered link",
    MATCH_NAME:    "Name",
    MATCH_MANUAL:  "Matched by hand",
    MATCH_IGNORED: "Ignored",
}

# The ones that mean "this row has a person against it".
MATCHED_METHODS = (MATCH_CLOCKNO, MATCH_LINK, MATCH_NAME, MATCH_MANUAL)


# ======================================================
# IMPORT BATCH  (one uploaded clock report)
# ======================================================
class ClockImportBatch(db.Model):
    """One Turbo Time report, loaded."""

    __tablename__ = "clock_import_batches"

    id       = db.Column(db.Integer, primary_key=True)
    filename = db.Column(db.String(255))

    # Which report this was and when the clock produced it — not when we
    # loaded it. Two uploads of the same export are the same data.
    report_kind      = db.Column(db.String(60))
    report_system    = db.Column(db.String(120))
    report_company   = db.Column(db.String(120))
    report_generated_at = db.Column(db.DateTime)
    period_start     = db.Column(db.Date, index=True)
    period_end       = db.Column(db.Date, index=True)

    # Where this report's VARIANCE column ended. The two reports print that
    # column at different widths, so a stored line has to be cut the way the
    # report it came off was cut — see ClockDay.source_values().
    report_variance_end = db.Column(db.Integer, default=DEFAULT_VARIANCE_END,
                                    server_default=str(DEFAULT_VARIANCE_END))

    # sha1 of the file's bytes. Not unique — the same report may legitimately
    # be loaded again after a reversal — but it lets the upload screen warn
    # that this exact file has been in before.
    file_sha1 = db.Column(db.String(40), index=True)

    imported_at = db.Column(db.DateTime, default=datetime.now)
    imported_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)

    rows_total     = db.Column(db.Integer, default=0)   # day lines read
    rows_imported  = db.Column(db.Integer, default=0)   # day rows created
    rows_skipped   = db.Column(db.Integer, default=0)   # unreadable lines
    employees_total     = db.Column(db.Integer, default=0)
    employees_matched   = db.Column(db.Integer, default=0)
    employees_unmatched = db.Column(db.Integer, default=0)

    # The report's own GRAND TOTAL line, kept so the screen can show what we
    # loaded against what the clock said it printed.
    file_normal_hours   = db.Column(db.Numeric(10, 2))
    file_overtime_hours = db.Column(db.Numeric(10, 2))
    file_total_hours    = db.Column(db.Numeric(10, 2))
    file_target_hours   = db.Column(db.Numeric(10, 2))
    file_shifts         = db.Column(db.Integer)

    # The cost summary at the foot of the report. Behind the `rates`
    # permission wherever it is shown, the way overtime treats its amounts.
    file_cost_normal   = db.Column(db.Numeric(12, 2))
    file_cost_overtime = db.Column(db.Numeric(12, 2))
    file_cost_total    = db.Column(db.Numeric(12, 2))

    # Parser warnings and reconciliation notes, one per line.
    notes = db.Column(db.Text)

    user = db.relationship("User")
    employees = db.relationship(
        "ClockEmployee",
        back_populates="batch",
        cascade="all, delete-orphan",
        order_by="ClockEmployee.emp_name",
    )

    @property
    def note_lines(self):
        return [line for line in (self.notes or "").splitlines() if line.strip()]

    @property
    def is_full_clocking(self):
        """
        True for the clocked-times report — every day of the period, worked or
        not. The overtime report prints only the days that carried overtime, so
        its normal-hours figures are a subset and are labelled as such.
        """
        return "CLOCK" in (self.report_kind or "").upper()

    @property
    def period_label(self):
        if not self.period_start:
            return "—"
        if self.period_end and self.period_end != self.period_start:
            return (f"{self.period_start.strftime('%d %b')} – "
                    f"{self.period_end.strftime('%d %b %Y')}")
        return self.period_start.strftime("%d %b %Y")

    @property
    def overtime_hours(self):
        """Overtime across everything actually loaded."""
        return sum((e.days_overtime_hours for e in self.employees), Decimal("0"))

    @property
    def normal_hours(self):
        """Normal time across everything actually loaded."""
        return sum((e.days_normal_hours for e in self.employees), Decimal("0"))

    @property
    def total_hours(self):
        return self.normal_hours + self.overtime_hours

    @property
    def edited_days(self):
        return sum(1 for e in self.employees for d in e.days if d.is_edited)

    @property
    def odd_clockings(self):
        """Days the clock printed a full punch list for — the ones to look at."""
        return sum(1 for e in self.employees for d in e.days if d.punches)

    @property
    def reconciles(self):
        """
        Whether the overtime we loaded agrees with the report's own total.

        Loosely compared: the clock prints its grand total into the same narrow
        columns as a single day, so each band loses its last digit on a
        five-figure total (6440.4 for what is really 6440.4x). None when the
        file carried no total to check against.
        """
        if self.file_overtime_hours is None:
            return None
        return abs(Decimal(self.file_overtime_hours) - self.overtime_hours) <= Decimal("1.0")

    def __repr__(self):
        return f"<ClockImportBatch {self.id} {self.filename}>"


# ======================================================
# CLOCK EMPLOYEE  (one person on one report)
# ======================================================
class ClockEmployee(db.Model):
    """
    One employee's block on the report — who the clock says they are, their
    totals for the period, and which personnel record they are matched to.

    The subtotal figures are the clock's own for the whole period, so
    `normal_hours` covers days that were never printed (an overtime report
    prints only the days that carried overtime). `days_*` below are what we
    actually loaded. Both are kept because they answer different questions:
    "what did this person work this period" and "what is on this report".
    """

    __tablename__ = "clock_employees"
    __table_args__ = (
        db.UniqueConstraint("batch_id", "emp_no", name="uq_clock_employee_batch_emp"),
    )

    id       = db.Column(db.Integer, primary_key=True)
    batch_id = db.Column(db.Integer,
                         db.ForeignKey("clock_import_batches.id", ondelete="CASCADE"),
                         nullable=False, index=True)
    source_row = db.Column(db.Integer)

    # Exactly as the clock printed them. Kept even once matched, so an
    # unmatched employee is never a lost employee.
    emp_no           = db.Column(db.String(30), nullable=False, index=True)
    emp_name         = db.Column(db.String(120))
    dept_text        = db.Column(db.String(120))
    cost_centre      = db.Column(db.String(80))
    cost_centre_code = db.Column(db.String(20))

    # ── The clock's period subtotal ──
    subtotal_labels = db.Column(db.String(120))   # "NORMAL HOURS", "SATURDAY", …
    normal_hours    = db.Column(db.Numeric(7, 2))
    ot1_hours       = db.Column(db.Numeric(7, 2))
    ot2_hours       = db.Column(db.Numeric(7, 2))
    ot3_hours       = db.Column(db.Numeric(7, 2))
    ot4_hours       = db.Column(db.Numeric(7, 2))
    total_hours     = db.Column(db.Numeric(7, 2))
    target_hours    = db.Column(db.Numeric(7, 2))
    shifts          = db.Column(db.Integer)
    variance_hours  = db.Column(db.Numeric(7, 2))

    # ── The match ──
    personnel_id = db.Column(db.Integer, db.ForeignKey("personnel.id"),
                             nullable=True, index=True)
    match_method = db.Column(db.String(10), nullable=False, default=MATCH_NONE,
                             server_default=MATCH_NONE)
    # Filled in when a person had to choose — an audit trail for the decisions
    # the automatic pass could not make.
    matched_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    matched_at = db.Column(db.DateTime)
    match_note = db.Column(db.String(255))

    batch     = db.relationship("ClockImportBatch", back_populates="employees")
    personnel = db.relationship("Personnel")
    matcher   = db.relationship("User", foreign_keys=[matched_by])
    days = db.relationship(
        "ClockDay",
        back_populates="employee",
        cascade="all, delete-orphan",
        order_by="ClockDay.work_date",
    )

    # ── Convenience ───────────────────────────────────────────────────
    @property
    def is_matched(self):
        return self.personnel_id is not None

    @property
    def is_ignored(self):
        return self.match_method == MATCH_IGNORED

    @property
    def needs_match(self):
        """Still waiting on a decision — not matched and not deliberately left."""
        return not self.is_matched and not self.is_ignored

    @property
    def match_label(self):
        return MATCH_LABELS.get(self.match_method, self.match_method)

    @property
    def overtime_hours(self):
        """The clock's period overtime — its four bands added up."""
        return sum((h or Decimal("0")) for h in
                   (self.ot1_hours, self.ot2_hours, self.ot3_hours, self.ot4_hours))

    @property
    def days_overtime_hours(self):
        return sum((d.overtime_hours for d in self.days), Decimal("0"))

    @property
    def days_normal_hours(self):
        return sum((d.normal_hours or Decimal("0")) for d in self.days)

    @property
    def display_name(self):
        """The matched person's name where there is one, else the clock's."""
        if self.personnel:
            return f"{self.personnel.name} {self.personnel.surname or ''}".strip()
        return self.emp_name or self.emp_no

    def link_to(self, person, method=MATCH_MANUAL, user_id=None, note=None):
        """Attach this employee to a personnel record. The only writer of a match."""
        self.personnel_id = person.id if person else None
        self.match_method = method
        self.matched_by = user_id
        self.matched_at = datetime.now() if user_id else None
        self.match_note = note

    def clear_match(self):
        self.personnel_id = None
        self.match_method = MATCH_NONE
        self.matched_by = None
        self.matched_at = None
        self.match_note = None

    def __repr__(self):
        return f"<ClockEmployee {self.emp_no} {self.emp_name} → {self.personnel_id}>"


# ======================================================
# CLOCK DAY  (one printed day for one person)
# ======================================================
class ClockDay(db.Model):
    """
    One day off the report: when they clocked in and out, the normal time in
    that day and the overtime beside it.

    That pairing is the point of the import — normal hours and overtime hours
    for the same person on the same day, as the clock recorded them, rather
    than overtime on its own with no context for what it sat on top of.
    """

    __tablename__ = "clock_days"

    id          = db.Column(db.Integer, primary_key=True)
    employee_id = db.Column(db.Integer,
                            db.ForeignKey("clock_employees.id", ondelete="CASCADE"),
                            nullable=False, index=True)
    source_row  = db.Column(db.Integer)
    # The exact line the report printed. This is what `revert` reads back, so
    # an edit is never one-way.
    source_line = db.Column(db.Text)

    work_date = db.Column(db.Date, nullable=False, index=True)
    day_name  = db.Column(db.String(10))
    shift     = db.Column(db.String(20))

    time_in   = db.Column(db.Time)
    time_out  = db.Column(db.Time)

    normal_hours   = db.Column(db.Numeric(7, 2))
    ot1_hours      = db.Column(db.Numeric(7, 2))
    ot2_hours      = db.Column(db.Numeric(7, 2))
    ot3_hours      = db.Column(db.Numeric(7, 2))
    ot4_hours      = db.Column(db.Numeric(7, 2))
    total_hours    = db.Column(db.Numeric(7, 2))
    target_hours   = db.Column(db.Numeric(7, 2))
    shifts         = db.Column(db.Integer)
    variance_hours = db.Column(db.Numeric(7, 2))
    description    = db.Column(db.String(60))

    # ── Editing ──
    edited_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    edited_at = db.Column(db.DateTime)
    edit_note = db.Column(db.String(255))

    employee = db.relationship("ClockEmployee", back_populates="days")
    editor   = db.relationship("User", foreign_keys=[edited_by])
    punches  = db.relationship(
        "ClockPunch",
        back_populates="day",
        cascade="all, delete-orphan",
        order_by="ClockPunch.sequence",
    )

    # The fields an edit may touch, and that a revert puts back.
    EDITABLE_FIELDS = (
        "time_in", "time_out", "normal_hours",
        "ot1_hours", "ot2_hours", "ot3_hours", "ot4_hours",
        "total_hours", "description",
    )

    @property
    def is_edited(self):
        return self.edited_at is not None

    @property
    def overtime_hours(self):
        """The four overtime bands added up."""
        return sum((h or Decimal("0")) for h in
                   (self.ot1_hours, self.ot2_hours, self.ot3_hours, self.ot4_hours))

    @property
    def personnel(self):
        return self.employee.personnel if self.employee else None

    @property
    def worked(self):
        """
        Whether anything was worked. A day off prints zeros and `--:--` right
        across — which is real information on the full clocking report, and the
        reason to load it rather than the overtime one.
        """
        return bool(self.total_hours) or self.time_in is not None

    @property
    def worked_label(self):
        """'07:05 – 17:21', or a dash where a punch never happened."""
        start = self.time_in.strftime("%H:%M") if self.time_in else "—"
        end = self.time_out.strftime("%H:%M") if self.time_out else "—"
        return f"{start} – {end}"

    @property
    def is_odd_clocking(self):
        """
        True where the clock printed a full punch list under this day.

        It does that when a day was clocked more than twice, and the day line
        above then shows only the first pair — so these are the days where the
        summary line is not the whole story.
        """
        return bool(self.punches)

    def source_values(self):
        """
        The values the report printed for this row, re-read from its line.

        The line is cut using the layout of the report it came off — the two
        reports differ in the width of the VARIANCE column, so cutting a
        clocked-times line with the overtime widths would put the description
        in the wrong place.
        """
        from timeclock.parser import reparse_day_line
        batch = self.employee.batch if self.employee else None
        variance_end = (batch.report_variance_end if batch and batch.report_variance_end
                        else DEFAULT_VARIANCE_END)
        return reparse_day_line(self.source_line, variance_end)

    @staticmethod
    def _stored(field, value):
        """
        One re-read source value in the shape the import would have stored it.

        The parser hands back exactly what was printed, which for an empty
        description is `''`; the import stores that as NULL. Without putting
        the two through the same funnel, a reverted row would compare unequal
        to the file it was just restored from and stay flagged as edited.
        """
        if field == "description":
            return (value or "").strip()[:60] or None
        return value

    @property
    def differs_from_source(self):
        """
        Whether the row's figures still say what the file said.

        Read off the stored line rather than off the edited flag, so a row
        edited back to its original values stops being flagged.
        """
        original = self.source_values()
        if original is None:
            return False
        return any(getattr(self, f) != self._stored(f, getattr(original, f))
                   for f in self.EDITABLE_FIELDS)

    def revert(self):
        """
        Put every editable field back to what the report printed.

        Returns False when the source line cannot be re-read — which should not
        happen, but a row whose provenance is gone is better left alone than
        silently blanked.
        """
        original = self.source_values()
        if original is None:
            return False
        for f in self.EDITABLE_FIELDS:
            setattr(self, f, self._stored(f, getattr(original, f)))
        self.edited_by = None
        self.edited_at = None
        self.edit_note = None
        return True

    def __repr__(self):
        return f"<ClockDay {self.work_date} emp={self.employee_id}>"


# ======================================================
# CLOCK PUNCH  (one in/out pair on a day clocked more than twice)
# ======================================================
class ClockPunch(db.Model):
    """
    One in/out pair off the punch list the report prints under an odd clocking.

    Most days are two punches and the day row holds them. Where someone clocked
    in and out more than once — went home sick and came back, a missed punch
    followed by a real one — the clock flags the day "ODD Clocking" and prints
    every pair underneath it, while the day line above shows only the first.
    Dropping those would lose exactly the days payroll has to look at, so they
    are kept whole.

    The raw text is stored beside the parsed time because the clock sometimes
    prints '05N43' rather than '05h43' on these lines, and what that N means is
    not documented anywhere we have. Showing what was printed is honest;
    inventing a meaning for it would not be.
    """

    __tablename__ = "clock_punches"

    id       = db.Column(db.Integer, primary_key=True)
    day_id   = db.Column(db.Integer, db.ForeignKey("clock_days.id", ondelete="CASCADE"),
                         nullable=False, index=True)
    sequence = db.Column(db.Integer, nullable=False, default=1)

    source_row  = db.Column(db.Integer)
    source_line = db.Column(db.Text)

    raw_in   = db.Column(db.String(10))
    raw_out  = db.Column(db.String(10))
    time_in  = db.Column(db.Time)
    time_out = db.Column(db.Time)

    day = db.relationship("ClockDay", back_populates="punches")

    @property
    def is_odd(self):
        """True where the clock printed this pair in its unusual N form."""
        return "n" in ((self.raw_in or "") + (self.raw_out or "")).lower()

    @property
    def label(self):
        """'06:43 – 07:01', showing the raw text where it would not parse."""
        start = self.time_in.strftime("%H:%M") if self.time_in else (self.raw_in or "—")
        end = self.time_out.strftime("%H:%M") if self.time_out else (self.raw_out or "—")
        return f"{start} – {end}"

    def __repr__(self):
        return f"<ClockPunch day={self.day_id} #{self.sequence} {self.label}>"


# ======================================================
# REMEMBERED LINK  (emp. number → personnel, across imports)
# ======================================================
class ClockEmployeeLink(db.Model):
    """
    A match made by hand, kept so it never has to be made again.

    The clock's employee number is usually our clock number, which is why the
    automatic pass matches almost everyone. It is the handful it cannot — a
    number typed differently on the clock, someone carried over from an old
    numbering — that would otherwise have to be matched by hand on every
    weekly report. Matching one of those once writes a row here, and every
    later import picks it up.
    """

    __tablename__ = "clock_employee_links"

    id     = db.Column(db.Integer, primary_key=True)
    emp_no = db.Column(db.String(30), nullable=False, unique=True, index=True)

    # Null means "this number is deliberately not one of ours" — a contractor
    # or a visitor on the clock. Remembered too, so it stops being reported as
    # unmatched on every future import.
    personnel_id = db.Column(db.Integer, db.ForeignKey("personnel.id"), nullable=True)

    emp_name   = db.Column(db.String(120))   # last name the clock printed for it
    created_at = db.Column(db.DateTime, default=datetime.now)
    created_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    note       = db.Column(db.String(255))

    personnel = db.relationship("Personnel")
    user      = db.relationship("User", foreign_keys=[created_by])

    @property
    def is_ignore(self):
        return self.personnel_id is None

    def __repr__(self):
        return f"<ClockEmployeeLink {self.emp_no} → {self.personnel_id}>"
