"""
Time clock module — the import itself
=====================================

Parse a Turbo Time report, write it as a batch, and match its employees to
personnel — in one transaction, so a file either lands whole or not at all.

Nothing outside the module's own tables is written. That is deliberate: it
makes the import reversible by a single delete, and it keeps the clock's
version of a week separate from the overtime that was requested and approved
for it, which is what makes comparing the two worth doing later.
"""

import hashlib
from datetime import datetime
from decimal import Decimal

from app import db
from timeclock.matching import PersonnelIndex, load_links, match_employees, refresh_counts
from timeclock.models import ClockDay, ClockEmployee, ClockImportBatch, ClockPunch
from timeclock.parser import decode, parse_report

# The cost-summary keys the report prints, mapped to what we store. The
# overtime bands are added together — the split by band is on the hours.
_COST_NORMAL = "NORMAL TIME"
_COST_TOTAL = "TOTAL COST"


class ClockImportResult:
    """What one upload did, for the flash messages and the import screen."""

    def __init__(self, batch, report):
        self.batch = batch
        self.report = report

    @property
    def employees(self):
        return self.batch.employees_total or 0

    @property
    def days(self):
        return self.batch.rows_imported or 0

    @property
    def unmatched(self):
        return self.batch.employees_unmatched or 0

    @property
    def punches(self):
        return self.report.punch_count

    @property
    def is_full_clocking(self):
        return self.report.is_full_clocking

    @property
    def warnings(self):
        return self.report.warnings


def file_digest(data):
    return hashlib.sha1(data).hexdigest()


def previous_uploads(sha1):
    """Batches already holding this exact file — the upload screen warns on these."""
    if not sha1:
        return []
    return (ClockImportBatch.query
            .filter(ClockImportBatch.file_sha1 == sha1)
            .order_by(ClockImportBatch.imported_at.desc())
            .all())


def overlapping_batches(period_start, period_end, exclude_id=None):
    """
    Batches whose period overlaps this one.

    Loading two reports that cover the same days is not blocked — a report is
    often re-run after the clock is corrected — but it is worth saying so,
    because the hours would then be counted twice by anything reading across
    batches.
    """
    if not period_start or not period_end:
        return []
    query = ClockImportBatch.query.filter(
        ClockImportBatch.period_start <= period_end,
        ClockImportBatch.period_end >= period_start,
    )
    if exclude_id:
        query = query.filter(ClockImportBatch.id != exclude_id)
    return query.order_by(ClockImportBatch.imported_at.desc()).all()


def import_clock_report(file_storage, user_id=None):
    """
    Load one uploaded report. Returns a ClockImportResult.

    Raises ValueError when the file is not a Turbo Time report at all — a
    problem with the file rather than with a line in it.
    """
    data = file_storage.read()
    if not data:
        raise ValueError("That file is empty.")

    report = parse_report(decode(data))
    sha1 = file_digest(data)

    # Look for the clashes *before* the new batch exists. Adding it first would
    # autoflush it into the same queries, and every import would report itself
    # as a duplicate of itself.
    notes = list(report.warnings) + report.check()
    duplicates = previous_uploads(sha1)
    if duplicates:
        notes.append(
            "This exact file was already imported as batch "
            + ", ".join(f"#{b.id}" for b in duplicates) + "."
        )
    overlaps = overlapping_batches(report.period_start, report.period_end)
    if overlaps:
        notes.append(
            "The period overlaps batch "
            + ", ".join(f"#{b.id} ({b.period_label})" for b in overlaps)
            + " — the same hours may now be loaded twice."
        )

    batch = ClockImportBatch(
        filename=(getattr(file_storage, "filename", None) or "")[:255] or None,
        report_kind=(report.report_kind or "")[:60] or None,
        report_system=(report.system or "")[:120] or None,
        report_company=(report.company or "")[:120] or None,
        report_generated_at=report.generated_at,
        period_start=report.period_start,
        period_end=report.period_end,
        report_variance_end=report.variance_end,
        file_sha1=sha1,
        imported_by=user_id,
        imported_at=datetime.now(),
    )
    db.session.add(batch)

    employees = []
    for block in report.employees:
        employee = ClockEmployee(
            batch=batch,
            source_row=block.source_row,
            emp_no=block.emp_no[:30],
            emp_name=(block.emp_name or "")[:120] or None,
            dept_text=(block.dept_text or "")[:120] or None,
            cost_centre=(block.cost_centre or "")[:80] or None,
            cost_centre_code=(block.cost_centre_code or "")[:20] or None,
            subtotal_labels=", ".join(block.subtotal_labels)[:120] or None,
            normal_hours=block.normal_hours,
            ot1_hours=block.ot1_hours,
            ot2_hours=block.ot2_hours,
            ot3_hours=block.ot3_hours,
            ot4_hours=block.ot4_hours,
            total_hours=block.total_hours,
            target_hours=block.target_hours,
            shifts=block.shifts,
            variance_hours=block.variance_hours,
        )
        db.session.add(employee)
        employees.append(employee)

        for day in block.days:
            row = ClockDay(
                employee=employee,
                source_row=day.source_row,
                source_line=day.source_line,
                work_date=day.work_date,
                day_name=(day.day_name or "")[:10] or None,
                shift=(day.shift or "")[:20] or None,
                time_in=day.time_in,
                time_out=day.time_out,
                normal_hours=day.normal_hours,
                ot1_hours=day.ot1_hours,
                ot2_hours=day.ot2_hours,
                ot3_hours=day.ot3_hours,
                ot4_hours=day.ot4_hours,
                total_hours=day.total_hours,
                target_hours=day.target_hours,
                shifts=day.shifts,
                variance_hours=day.variance_hours,
                description=(day.description or "")[:60] or None,
            )
            db.session.add(row)

            # The full punch list, where the day was clocked more than twice.
            for punch in day.punches:
                db.session.add(ClockPunch(
                    day=row,
                    sequence=punch.sequence,
                    source_row=punch.source_row,
                    source_line=punch.source_line,
                    raw_in=(punch.raw_in or "")[:10] or None,
                    raw_out=(punch.raw_out or "")[:10] or None,
                    time_in=punch.time_in,
                    time_out=punch.time_out,
                ))

    # ── Match before committing, so the file and its matching land together ──
    match_employees(employees, index=PersonnelIndex(), links=load_links())

    batch.rows_total = report.day_count + report.skipped
    batch.rows_imported = report.day_count
    batch.rows_skipped = report.skipped
    refresh_counts(batch)

    grand = report.grand or {}
    batch.file_normal_hours = grand.get("normal")
    batch.file_overtime_hours = grand.get("overtime")
    batch.file_total_hours = grand.get("total")
    batch.file_target_hours = grand.get("target")
    batch.file_shifts = grand.get("shifts")

    costs = report.costs or {}
    batch.file_cost_normal = costs.get(_COST_NORMAL)
    batch.file_cost_total = costs.get(_COST_TOTAL)
    batch.file_cost_overtime = sum(
        (value for key, value in costs.items() if key.startswith("OT")),
        Decimal("0"),
    ) or None

    batch.notes = "\n".join(notes) or None

    db.session.commit()
    return ClockImportResult(batch, report)
