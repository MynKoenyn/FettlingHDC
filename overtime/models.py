from datetime import datetime
from decimal import Decimal
from app import db
from models import *
from overtime.calc import (compute_hours, overtime_multiplier, compute_amount,
                           deduct_minutes)


# ======================================================
# OVERTIME  (a request, an actual, or a request with its actual)
# ======================================================
class OvertimeRequest(db.Model):
    """
    One overtime record.

    Two sides, either of which may be present:

      * the *requested* side — someone asks for overtime to be worked, and it
        is approved or rejected (the original workflow); and
      * the *actual* side — what was really worked, captured afterwards.

    `entry_type` says which kind of record this is:
      * 'request' — raised as a request (may later gain an actual);
      * 'actual'  — a standalone actual, captured with no prior request.

    The request-side columns are nullable so a standalone actual can exist
    without them.
    """
    __tablename__ = "overtime_requests"

    id              = db.Column(db.Integer, primary_key=True)

    # 'request' (has a requested/approved side) or 'actual' (standalone actual)
    entry_type      = db.Column(db.String(10), default="request", nullable=False,
                                server_default="request")

    # Every record created by one submit shares a batch reference. A week of
    # overtime raised in one go is 10 rows — two periods on each of five days —
    # and the batch is what lets them be opened, approved and captured together
    # instead of one at a time. Null on anything captured before batches, and
    # on a one-off, which is simply a batch of one.
    batch_id        = db.Column(db.String(36), nullable=True, index=True)

    # Who submitted the request. Null on a standalone actual.
    requested_by    = db.Column(db.Integer, db.ForeignKey("users.id"),      nullable=True)

    # The personnel member the overtime is FOR
    personnel_id    = db.Column(db.Integer, db.ForeignKey("personnel.id"),  nullable=False)

    # Denormalised for easy filtering — taken from personnel at capture time
    division_id     = db.Column(db.Integer, db.ForeignKey("divisions.id"),  nullable=True)
    department_id   = db.Column(db.Integer, db.ForeignKey("departments.id"), nullable=True)

    # The date the overtime relates to (required for both sides)
    overtime_date   = db.Column(db.Date,    nullable=False)

    # ── Requested side (null on a standalone actual) ──
    start_time      = db.Column(db.Time,          nullable=True)
    end_time        = db.Column(db.Time,          nullable=True)
    hours           = db.Column(db.Numeric(5, 2), nullable=True)
    overtime_amount = db.Column(db.Numeric(10, 2), nullable=True)
    reason          = db.Column(db.Text,          nullable=True)

    # Workflow status of the requested side: pending | approved | rejected
    # (Left as 'pending' but meaningless on a standalone actual — read
    #  entry_type to tell the kinds apart.)
    status          = db.Column(db.String(20), default="pending", nullable=False)

    # Approval fields
    approved_by     = db.Column(db.Integer, db.ForeignKey("users.id"),      nullable=True)
    approved_at     = db.Column(db.DateTime, nullable=True)
    approval_notes  = db.Column(db.Text,    nullable=True)

    # ── Actual side (null until captured) ──
    actual_start_time = db.Column(db.Time,           nullable=True)
    actual_end_time   = db.Column(db.Time,           nullable=True)
    # ── Unpaid minutes that come off the range above ──
    # Both are plain minute totals rather than clock times, because that is how
    # a supervisor knows them: nobody records when a break started, they know
    # it ran half an hour. They are kept apart because they mean different
    # things — a break is time off, late in or early out is time not worked —
    # and management wants them told apart on a report.
    actual_break_minutes = db.Column(db.Integer, default=0, nullable=False,
                                     server_default=db.text("0"))
    actual_late_minutes  = db.Column(db.Integer, default=0, nullable=False,
                                     server_default=db.text("0"))
    # Net of both — this is what is paid and what the reports total.
    actual_hours      = db.Column(db.Numeric(5, 2),  nullable=True)
    actual_multiplier = db.Column(db.Numeric(4, 2),  nullable=True)
    actual_amount     = db.Column(db.Numeric(10, 2), nullable=True)
    # True once someone has typed an amount by hand — recalcs then leave it be.
    actual_amount_overridden = db.Column(db.Boolean, default=False, nullable=False,
                                         server_default=db.text("false"))
    actual_notes      = db.Column(db.Text,     nullable=True)
    actual_captured_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    actual_captured_at = db.Column(db.DateTime, nullable=True)

    created_at      = db.Column(db.DateTime, default=datetime.now)

    # relationships
    requester   = db.relationship("User",      foreign_keys=[requested_by])
    personnel   = db.relationship("Personnel", foreign_keys=[personnel_id], back_populates="overtime_requests")
    division    = db.relationship("Division",  foreign_keys=[division_id])
    department  = db.relationship("Department", foreign_keys=[department_id])
    approver    = db.relationship("User",      foreign_keys=[approved_by])
    actual_capturer = db.relationship("User",  foreign_keys=[actual_captured_by])

    # ── Convenience flags ─────────────────────────────────────────────
    @property
    def has_request(self):
        """True when this record carries a requested/approved side."""
        return self.entry_type == "request"

    @property
    def has_actual(self):
        """True once actual hours have been captured."""
        return self.actual_hours is not None

    @property
    def is_approved(self):
        return self.entry_type == "request" and self.status == "approved"

    @property
    def actual_gross_hours(self):
        """Hours from clock-on to clock-off, before the deductions come off."""
        return compute_hours(self.actual_start_time, self.actual_end_time)

    @property
    def actual_deducted_minutes(self):
        """Unpaid minutes taken off this period — the lunch plus late/early."""
        return (self.actual_break_minutes or 0) + (self.actual_late_minutes or 0)

    # ── Was this work authorised? ─────────────────────────────────────
    #
    # Overtime gets worked that nobody signed off: a day that was never
    # requested, one that was requested and turned down, one still sitting
    # unapproved when it was worked, and time that ran past the window that
    # was approved. Management needs to see all four, so none of them are
    # blocked at capture — the clock records what happened, approval records
    # what was allowed, and the gap between them is reported.
    #
    # Deliberately derived rather than stored, so it can never fall out of
    # step with the status and the hours it is read from.
    #
    # A request is raised without a lunch break — whoever raises it has no way
    # of knowing whether one will be taken, when, or how long the shift's
    # break is, and those differ by division and by shift. So requested hours
    # are clock-on to clock-off, while actual hours are net of the break that
    # was actually taken. Over-run is therefore measured on the *net* hours:
    # someone approved 04:00–06:00 who works 04:00–06:30 and takes a 30 minute
    # lunch has worked the two hours that were approved, and is not over-run.

    AUTHORISATION_LABELS = {
        "approved":   "Approved",
        "overrun":    "Over-run",
        "unapproved": "Not yet approved",
        "rejected":   "Worked after rejection",
        "unrequested": "Not requested",
    }

    @property
    def authorisation(self):
        """
        How the hours actually worked stand against what was signed off.
        None when nothing has been captured yet — there is no work to judge.
        """
        if not self.has_actual:
            return None

        if self.entry_type != "request":
            return "unrequested"

        if self.status == "rejected":
            return "rejected"
        if self.status != "approved":
            return "unapproved"

        if self.hours is not None and Decimal(self.actual_hours) > Decimal(self.hours):
            return "overrun"
        return "approved"

    @property
    def authorisation_label(self):
        return self.AUTHORISATION_LABELS.get(self.authorisation)

    @property
    def is_authorised(self):
        return self.authorisation in (None, "approved")

    @property
    def unauthorised_hours(self):
        """
        Hours worked that nobody approved — the whole entry when it was never
        approved at all, or just the excess when it ran past the approved
        window. Decimal('0.00') when the work was authorised.
        """
        state = self.authorisation
        if state in (None, "approved"):
            return Decimal("0.00")
        if state == "overrun":
            return (Decimal(self.actual_hours) - Decimal(self.hours)).quantize(Decimal("0.01"))
        return Decimal(self.actual_hours).quantize(Decimal("0.01"))

    # ── Requested vs actual variances (None when the actual isn't in yet) ──
    @property
    def variance_hours(self):
        if self.actual_hours is None or self.hours is None:
            return None
        return Decimal(self.actual_hours) - Decimal(self.hours)

    @property
    def variance_amount(self):
        if self.actual_amount is None or self.overtime_amount is None:
            return None
        return Decimal(self.actual_amount) - Decimal(self.overtime_amount)

    # ── Recalculation ─────────────────────────────────────────────────
    def recalc_actual(self, force_amount=False):
        """
        Refresh actual_hours, actual_multiplier and actual_amount from the
        captured times, the unpaid minutes and the person's rate.

        The amount is left alone when it has been overridden by hand, unless
        force_amount is set (used when the override is cleared).
        """
        gross = compute_hours(self.actual_start_time, self.actual_end_time)
        self.actual_hours = deduct_minutes(gross, self.actual_break_minutes,
                                           self.actual_late_minutes)
        self.actual_multiplier = overtime_multiplier(self.overtime_date)

        if self.actual_amount_overridden and not force_amount:
            return

        rate = self.personnel.rate if self.personnel else None
        self.actual_amount = compute_amount(rate, self.actual_hours, self.actual_multiplier)

    def __repr__(self):
        return f"<OvertimeRequest id={self.id} type={self.entry_type} status={self.status}>"
