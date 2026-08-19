"""
Turbo Time report parser
========================

Reads the printed reports exported by MSSQL TURBO TIME PRO — the clock system —
and turns them into plain Python objects. No database, no Flask: this module is
the one place that understands the files' shape, so it can be run and checked
on its own.

Two reports, one layout
-----------------------
Turbo Time prints two reports on the same grid, and both load here:

  * **CLOCKED TIMES REPORT** ("Full Clocking Report") — every day of the period
    for every employee, worked or not. This is the one to use: it is the whole
    attendance register, so absences, short days and odd clockings are all in
    it, and each person's totals reconcile with the days printed above them.

  * **OVERTIME REPORT** — the same grid but only the days that carried
    overtime. Useful, but it is a subset: a person's NORMAL HOURS line covers
    days that were never printed, so normal time cannot be reconciled against
    it. `ClockReport.check()` knows the difference and only checks what can be.

Which file, and why not the .XLS
--------------------------------
Turbo Time offers each report as .TXT and as .XLS. The .XLS is not an Excel
file at all — it is an OpenDocument spreadsheet with an .xls name, which Excel
warns about and neither openpyxl nor xlrd will open, so reading it would mean a
new dependency to load a file that carries *less* than the text one: the
employee header arrives split across merged cells padded with `<text:s/>`
markup that has to be unpicked before it reads as a name. The .TXT is the same
report at fixed column positions, complete, and needs nothing extra.

The columns are not guessed. They come from the ruler the spreadsheet version
preserves as real cell boundaries, verified against every data line of both
reports. Splitting on whitespace instead would break on the two columns that
legitimately contain spaces — SHIFT ("M-T 7-5") and DESCRIPTION ("Late in") —
and on the days where a column is blank rather than zero.

What a day looks like
---------------------
    EMP # :  238   NAME : MBONGISENI N HLADIZE   DEPT : ...   COST C : F/DRIV
    ------------------------------------------------------------------------
     2026/07/20  Mon  M-T 7-5  06h32  16h16   8.75  0.00 ...   8.75  8.75  1
     2026/07/21  Tue  M-T 7-5  06h43  07h01   0.02  0.00 ...   0.02  8.75  0  ODD Clocking
                  06h43  07h01
                  17h03  --:--
     PAYROLL # :238                           40.00  7.02 ...  54.03 40.00  7

Clock in, clock out, the normal time in that day and the overtime beside it —
that pairing is the point of the import. `--:--` is a punch that never
happened. Where a day was clocked more than twice the report prints the full
punch list underneath it, and those lines are kept too (see ClockPunchRow):
they are precisely the days payroll has to look at, and the day line above
shows only the first pair.
"""

import re
from dataclasses import dataclass, field
from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation


# ── Column layout ────────────────────────────────────────────────────────────
#
# Both reports are identical from the date through to the shift count. Only the
# VARIANCE column differs in width — the clocked-times report gives it two more
# characters, which pushes DESCRIPTION along — so that part is measured off the
# header line of the file in hand rather than assumed.
BASE_COLUMNS = [
    ("date",     1,  14),
    ("day",     14,  20),
    ("shift",   20,  29),
    ("time_in", 29,  37),
    ("time_out",37,  45),
    ("nt",      45,  52),
    ("ot1",     52,  59),
    ("ot2",     59,  66),
    ("ot3",     66,  73),
    ("ot4",     73,  80),
    ("total",   80,  89),
    ("target",  89,  98),
    ("shifts",  98, 102),
]
VARIANCE_START = 102
DEFAULT_VARIANCE_END = 112          # the overtime report's width

_HEADER_RE   = re.compile(r"\bSHIFT\b.*\bTARGET\b.*\bVARIANCE\b")
_VARIANCE_RE = re.compile(r"\bVARIANCE\b")

# The label columns of the employee header, wherever they fall on the line.
# The clocked-times report prints "EMP # :", the overtime one "EMP. # :".
_LABEL_RE = re.compile(r"(EMP\.?\s*#|NAME|DEPT|COST\s*C)\s*:", re.IGNORECASE)
_EMP_RE   = re.compile(r"^\s*EMP\.?\s*#\s*:", re.IGNORECASE)

_DATE_RE    = re.compile(r"^\s*(\d{4}/\d{2}/\d{2})\b")
# 07h05, and the odd 05N43 the clock prints on a continuation punch. The
# separator is kept on the raw text rather than interpreted — it is not
# documented anywhere we have, and guessing at it would be worse than showing
# what was printed.
_TIME_RE    = re.compile(r"^(\d{1,2})[hHnN](\d{2})$")
_NO_PUNCH   = {"--:--", "--h--", "-", ""}
_RULE_RE    = re.compile(r"^\s*-{10,}")
_PAGE_RE    = re.compile(r"^\s*Page\s+\d+\s*$", re.IGNORECASE)
_COST_CC_RE = re.compile(r"^(?P<name>.*?)\s*\(\s*(?P<code>[^)]*?)\s*\)\s*$")

# An extra punch pair printed under a day that was clocked more than twice.
_PUNCH_RE = re.compile(
    r"^\s{6,}(?P<in>\d{2}[hHnN]\d{2}|--:--)\s+(?P<out>\d{2}[hHnN]\d{2}|--:--)\s*$"
)

# " NORMAL TIME   : R  15786.05    82.06%"  /  " LOST TIME     :    -164.07"
_MONEY_RE = re.compile(
    r"^\s*(?P<label>[A-Z0-9][A-Z0-9 @./]*?)\s*:\s*R?\s*(?P<value>-?[\d.]+)\s*(?P<pct>-?[\d.]+%)?\s*$"
)

# The foot prints lost time as three lines, the last of which carries only the
# percentage with no label of its own — it belongs to the LOST COST above it.
_ORPHAN_PCT_RE = re.compile(r"^\s*:\s*(?P<value>-?[\d.]+)\s*%\s*$")

DATE_FORMATS = ["%Y/%m/%d", "%d/%m/%Y", "%Y-%m-%d"]


def column_layout(header_line=None):
    """
    The column boundaries for one report, measured off its header line.

    Everything up to the shift count is fixed; VARIANCE is as wide as its
    heading plus the two spaces that follow it, and DESCRIPTION is the rest of
    the line. Falls back to the overtime report's widths when there is no
    header to measure.
    """
    variance_end = DEFAULT_VARIANCE_END
    if header_line:
        match = _VARIANCE_RE.search(header_line)
        if match:
            variance_end = max(match.end() + 2, VARIANCE_START + 1)
    return (list(BASE_COLUMNS)
            + [("variance", VARIANCE_START, variance_end),
               ("description", variance_end, None)])


DEFAULT_COLUMNS = column_layout()


# ── Small parsers ────────────────────────────────────────────────────────────

def parse_report_date(text):
    """A date off the report, in any of the layouts Turbo Time prints."""
    text = (text or "").strip()
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def parse_clock_time(text):
    """
    '07h05' → time(7, 5). None when there was no punch.

    `--:--` is how the clock prints a punch that never happened — someone who
    clocked in and never clocked out, or a day not worked at all. That is data,
    not an error, and the row still carries the hours the clock worked out.
    """
    text = (text or "").strip()
    if not text or text in _NO_PUNCH:
        return None
    m = _TIME_RE.match(text)
    if not m:
        return None
    hour, minute = int(m.group(1)), int(m.group(2))
    if minute > 59:
        return None
    # 24h00 is how some clock systems print midnight at the end of a shift.
    if hour == 24 and minute == 0:
        return time(0, 0)
    if hour > 23:
        return None
    return time(hour, minute)


def parse_hours(text):
    """A decimal hours figure. None when the cell is blank."""
    text = (text or "").strip()
    if not text:
        return None
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def parse_int(text):
    text = (text or "").strip()
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        return None


def _cells(line, columns=None):
    """One report line cut into its fixed-width columns, each stripped."""
    return {name: line[start:end].strip()
            for name, start, end in (columns or DEFAULT_COLUMNS)}


def _split_labelled(line):
    """
    The employee header read by its labels rather than by position, so a
    longer name that shifts DEPT along the line still parses.

        'EMP # : 238  NAME : M N HLADIZE  DEPT : HDC  COST C : F/DRIV'
        → {'EMP#': '238', 'NAME': 'M N HLADIZE', 'DEPT': 'HDC', 'COSTC': 'F/DRIV'}
    """
    marks = [
        (m.start(), m.end(), re.sub(r"[^A-Z#]", "", m.group(1).upper()))
        for m in _LABEL_RE.finditer(line)
    ]
    out = {}
    for i, (_start, end, key) in enumerate(marks):
        stop = marks[i + 1][0] if i + 1 < len(marks) else len(line)
        out[key] = line[end:stop].strip()
    return out


def split_cost_centre(text):
    """'F/DRIV (10)' → ('F/DRIV', '10'). The bracketed code is often absent."""
    text = (text or "").strip()
    if not text:
        return None, None
    m = _COST_CC_RE.match(text)
    if not m:
        return text, None
    return (m.group("name").strip() or None), (m.group("code").strip() or None)


def _subtotal_label(stripped):
    """
    The label off a subtotal line, tidied.

    The clocked-times report labels it 'PAYROLL # :369' — the number repeats
    the employee header, so it is dropped and the kind of subtotal kept. The
    overtime report labels it 'NORMAL HOURS'.
    """
    label = re.split(r"\s{2,}", stripped, maxsplit=1)[0].strip()
    if label.upper().startswith("PAYROLL"):
        return "PAYROLL #"
    return label


# ── The parsed shapes ────────────────────────────────────────────────────────

@dataclass
class ClockPunchRow:
    """
    One in/out pair off the punch list printed under an odd clocking.

    The raw text is kept beside the parsed time because the clock sometimes
    prints '05N43' instead of '05h43' on these lines, and what that N means is
    not documented anywhere we have. Showing what was printed is honest;
    inventing a meaning for it would not be.
    """
    sequence: int
    source_row: int
    source_line: str
    raw_in: str = ""
    raw_out: str = ""
    time_in: time = None
    time_out: time = None

    @property
    def is_odd(self):
        """True when the clock printed this pair in its unusual N form."""
        return "n" in (self.raw_in + self.raw_out).lower()


@dataclass
class ClockDayRow:
    """One printed day — what a person worked, and the overtime in it."""
    source_row: int
    source_line: str
    work_date: date
    day_name: str = ""
    shift: str = ""
    time_in: time = None
    time_out: time = None
    normal_hours: Decimal = None
    ot1_hours: Decimal = None
    ot2_hours: Decimal = None
    ot3_hours: Decimal = None
    ot4_hours: Decimal = None
    total_hours: Decimal = None
    target_hours: Decimal = None
    shifts: int = None
    variance_hours: Decimal = None
    description: str = ""
    punches: list = field(default_factory=list)

    @property
    def overtime_hours(self):
        """The four overtime bands added up — the paid-at-a-premium hours."""
        return sum((h or Decimal("0")) for h in
                   (self.ot1_hours, self.ot2_hours, self.ot3_hours, self.ot4_hours))

    @property
    def worked(self):
        """Whether anything was worked — a day off prints zeros throughout."""
        return bool(self.total_hours) or self.time_in is not None


@dataclass
class ClockEmployeeBlock:
    """One employee's section: their day lines and their period subtotal."""
    source_row: int
    emp_no: str
    emp_name: str = ""
    dept_text: str = ""
    cost_centre: str = None
    cost_centre_code: str = None
    days: list = field(default_factory=list)

    # The subtotal line(s) printed under the days. Turbo Time prints one per
    # category, so they are summed and the labels kept alongside.
    subtotal_labels: list = field(default_factory=list)
    normal_hours: Decimal = None
    ot1_hours: Decimal = None
    ot2_hours: Decimal = None
    ot3_hours: Decimal = None
    ot4_hours: Decimal = None
    total_hours: Decimal = None
    target_hours: Decimal = None
    shifts: int = None
    variance_hours: Decimal = None

    @property
    def overtime_hours(self):
        return sum((h or Decimal("0")) for h in
                   (self.ot1_hours, self.ot2_hours, self.ot3_hours, self.ot4_hours))

    @property
    def days_overtime_hours(self):
        """Overtime added up off the printed days."""
        return sum((d.overtime_hours for d in self.days), Decimal("0"))

    @property
    def days_normal_hours(self):
        return sum(((d.normal_hours or Decimal("0")) for d in self.days), Decimal("0"))

    def add_subtotal(self, label, cells):
        """Fold one subtotal line into the employee's period figures."""
        if label and label not in self.subtotal_labels:
            self.subtotal_labels.append(label)
        for attr, key in (("normal_hours", "nt"), ("ot1_hours", "ot1"),
                          ("ot2_hours", "ot2"), ("ot3_hours", "ot3"),
                          ("ot4_hours", "ot4"), ("total_hours", "total"),
                          ("target_hours", "target"), ("variance_hours", "variance")):
            value = parse_hours(cells.get(key))
            if value is None:
                continue
            current = getattr(self, attr)
            setattr(self, attr, value if current is None else current + value)

        shifts = parse_int(cells.get("shifts"))
        if shifts is not None:
            self.shifts = shifts if self.shifts is None else self.shifts + shifts


@dataclass
class ClockReport:
    """A whole parsed report."""
    system: str = ""
    company: str = ""
    report_kind: str = ""
    generated_at: datetime = None
    period_start: date = None
    period_end: date = None
    employees: list = field(default_factory=list)

    # Where this report's VARIANCE column ends, so a stored line can be cut
    # the same way again when it is read back.
    variance_end: int = DEFAULT_VARIANCE_END

    # The report's own GRAND TOTAL line, and the cost summary under it.
    grand: dict = field(default_factory=dict)
    costs: dict = field(default_factory=dict)

    warnings: list = field(default_factory=list)
    # Day lines that looked like data but could not be read.
    skipped: int = 0

    @property
    def is_full_clocking(self):
        """
        True for the clocked-times report — every day of the period, worked or
        not — as against the overtime report, which prints only the days that
        carried overtime.
        """
        return "CLOCK" in (self.report_kind or "").upper()

    @property
    def day_count(self):
        return sum(len(e.days) for e in self.employees)

    @property
    def punch_count(self):
        return sum(len(d.punches) for e in self.employees for d in e.days)

    @property
    def overtime_hours(self):
        return sum((e.days_overtime_hours for e in self.employees), Decimal("0"))

    @property
    def normal_hours(self):
        return sum((e.days_normal_hours for e in self.employees), Decimal("0"))

    def check(self):
        """
        Reconcile what we parsed against the totals the report prints itself,
        and return the discrepancies as readable lines.

        Overtime is always checked. Normal time is checked only on the full
        clocked-times report: an overtime report prints just the days that
        carried overtime, so its per-employee NORMAL HOURS subtotal covers days
        that were never printed and can never equal the day lines.

        Both are compared with a little slack. The clock rounds each band to
        two places and its subtotals do not always add back exactly — a real
        report is out by a cent of an hour here and there — and the GRAND TOTAL
        is printed into the same narrow columns as a single day, so a
        five-figure total loses its last digit (6440.4 for what is really
        6440.4x).
        """
        problems = []
        row_slack = Decimal("0.05")
        grand_slack = Decimal("1.0")

        checks = [("overtime", lambda e: e.overtime_hours, lambda e: e.days_overtime_hours)]
        if self.is_full_clocking:
            checks.append(("normal time",
                           lambda e: e.normal_hours or Decimal("0"),
                           lambda e: e.days_normal_hours))

        for what, stated_of, parsed_of in checks:
            for emp in self.employees:
                if not emp.subtotal_labels:
                    continue
                stated, parsed = stated_of(emp), parsed_of(emp)
                if abs(stated - parsed) > row_slack:
                    problems.append(
                        f"Emp {emp.emp_no} ({emp.emp_name}): {what} on the day lines "
                        f"({parsed}) does not match the report's own subtotal ({stated})."
                    )

        stated_grand = self.grand.get("overtime")
        if stated_grand is not None:
            parsed_grand = self.overtime_hours
            if abs(stated_grand - parsed_grand) > grand_slack:
                problems.append(
                    f"Grand total overtime on the report ({stated_grand}) does not "
                    f"match the sum of the day lines ({parsed_grand})."
                )

        return problems


# ── The parse itself ─────────────────────────────────────────────────────────

def parse_report(text):
    """
    Parse a Turbo Time report into a ClockReport.

    Raises ValueError when the file is not one of these reports at all —
    a problem with the file rather than with a line in it.
    """
    if isinstance(text, bytes):
        text = decode(text)

    lines = text.splitlines()
    report = ClockReport()
    columns = DEFAULT_COLUMNS
    employees = []
    by_emp_no = {}
    current = None          # the employee block being read
    current_day = None      # the day a punch list would belong to
    seen_column_header = False

    for row_no, raw in enumerate(lines, start=1):
        line = raw.rstrip("\r\n")
        stripped = line.strip()

        if not stripped or _RULE_RE.match(line) or _PAGE_RE.match(line):
            continue

        # ── Report header ────────────────────────────────────────────
        if "TURBO TIME" in stripped.upper() and not report.system:
            report.system = stripped
            continue

        if stripped.upper().startswith("COMPANY NAME"):
            report.company = stripped.split(":", 1)[-1].strip()
            continue

        if "REPORT" in stripped.upper() and "DATE:" in stripped.upper() and not report.report_kind:
            kind, _, when = stripped.partition("DATE:")
            report.report_kind = kind.strip()
            report.generated_at = _parse_datetime(when.strip())
            continue

        if stripped.upper().startswith("FROM:"):
            m = re.match(r"FROM\s*:\s*(\S+)\s+TO\s*:\s*(\S+)", stripped, re.IGNORECASE)
            if m:
                report.period_start = parse_report_date(m.group(1))
                report.period_end = parse_report_date(m.group(2))
            continue

        # The repeated column header. Measured the first time it appears — it
        # is what says how wide this report's VARIANCE column is — then skipped.
        if _HEADER_RE.search(line):
            if not seen_column_header:
                columns = column_layout(line)
                report.variance_end = columns[-1][1]
                seen_column_header = True
            current_day = None
            continue

        # ── Employee header ──────────────────────────────────────────
        if _EMP_RE.match(line):
            current_day = None
            parts = _split_labelled(line)
            emp_no = (parts.get("EMP#") or "").strip()
            if not emp_no:
                report.warnings.append(f"Line {row_no}: employee header with no number — skipped.")
                current = None
                continue

            existing = by_emp_no.get(emp_no)
            if existing is not None:
                # The same person printed again further down (their block ran
                # over a page break). Keep adding to the one record rather than
                # creating a second.
                current = existing
                continue

            name, code = split_cost_centre(parts.get("COSTC"))
            current = ClockEmployeeBlock(
                source_row=row_no,
                emp_no=emp_no,
                emp_name=(parts.get("NAME") or "").strip(),
                dept_text=(parts.get("DEPT") or "").strip(),
                cost_centre=name,
                cost_centre_code=code,
            )
            employees.append(current)
            by_emp_no[emp_no] = current
            continue

        # ── Grand total and the cost summary under it ────────────────
        if stripped.upper().startswith("GRAND TOTAL"):
            current, current_day = None, None
            continue

        if stripped.upper().startswith("TOTAL ->"):
            cells = _cells(line, columns)
            report.grand = {
                "normal":   parse_hours(cells["nt"]),
                "ot1":      parse_hours(cells["ot1"]),
                "ot2":      parse_hours(cells["ot2"]),
                "ot3":      parse_hours(cells["ot3"]),
                "ot4":      parse_hours(cells["ot4"]),
                "total":    parse_hours(cells["total"]),
                "target":   parse_hours(cells["target"]),
                "shifts":   parse_int(cells["shifts"]),
                "variance": parse_hours(cells["variance"]),
            }
            report.grand["overtime"] = sum(
                (report.grand[k] or Decimal("0")) for k in ("ot1", "ot2", "ot3", "ot4")
            )
            current, current_day = None, None
            continue

        # ── Day lines ────────────────────────────────────────────────
        if _DATE_RE.match(line):
            cells = _cells(line, columns)
            work_date = parse_report_date(cells["date"])
            if work_date is None:
                report.warnings.append(f"Line {row_no}: unreadable date '{cells['date']}' — skipped.")
                report.skipped += 1
                current_day = None
                continue
            if current is None:
                report.warnings.append(f"Line {row_no}: day line before any employee header — skipped.")
                report.skipped += 1
                current_day = None
                continue

            current_day = ClockDayRow(
                source_row=row_no,
                source_line=line,
                work_date=work_date,
                day_name=cells["day"],
                shift=cells["shift"],
                time_in=parse_clock_time(cells["time_in"]),
                time_out=parse_clock_time(cells["time_out"]),
                normal_hours=parse_hours(cells["nt"]),
                ot1_hours=parse_hours(cells["ot1"]),
                ot2_hours=parse_hours(cells["ot2"]),
                ot3_hours=parse_hours(cells["ot3"]),
                ot4_hours=parse_hours(cells["ot4"]),
                total_hours=parse_hours(cells["total"]),
                target_hours=parse_hours(cells["target"]),
                shifts=parse_int(cells["shifts"]),
                variance_hours=parse_hours(cells["variance"]),
                description=cells["description"],
            )
            current.days.append(current_day)
            continue

        # ── The punch list under an odd clocking ─────────────────────
        punch = _PUNCH_RE.match(line)
        if punch:
            if current_day is None:
                report.warnings.append(
                    f"Line {row_no}: clocking times with no day above them — skipped.")
                continue
            raw_in = punch.group("in").strip()
            raw_out = punch.group("out").strip()
            current_day.punches.append(ClockPunchRow(
                sequence=len(current_day.punches) + 1,
                source_row=row_no,
                source_line=line,
                raw_in=raw_in,
                raw_out=raw_out,
                time_in=parse_clock_time(raw_in),
                time_out=parse_clock_time(raw_out),
            ))
            continue

        # ── The money summary at the foot ────────────────────────────
        orphan = _ORPHAN_PCT_RE.match(line)
        if orphan and current is None:
            value = parse_hours(orphan.group("value"))
            if value is not None:
                report.costs["LOST PCT"] = value
            continue

        money = _MONEY_RE.match(line)
        if money and current is None:
            label = re.sub(r"\s+", " ", money.group("label")).strip().upper()
            value = parse_hours(money.group("value"))
            if value is not None:
                report.costs[label] = value
            continue

        # ── Per-employee subtotal ────────────────────────────────────
        # "NORMAL HOURS" on the overtime report, "PAYROLL # :369" on the
        # clocked-times one. Recognised by its shape: a label where the date
        # goes, and real figures in the hours columns.
        cells = _cells(line, columns)
        numeric = [parse_hours(cells[k]) for k in ("nt", "ot1", "total", "target")]
        if current is not None and stripped and sum(v is not None for v in numeric) >= 3:
            current.add_subtotal(_subtotal_label(stripped), cells)
            current_day = None
            continue

        report.warnings.append(f"Line {row_no}: not understood — ignored ({stripped[:60]!r}).")

    if not employees or not seen_column_header:
        raise ValueError(
            "That file does not look like a Turbo Time report — no employee "
            "blocks were found. Export the Full Clocking Report (or the "
            "Overtime Report) as .TXT from Turbo Time and upload that."
        )

    report.employees = employees
    return report


def _parse_datetime(text):
    """'2026/07/29 08:27:29' → datetime, or None."""
    text = (text or "").strip()
    for fmt in ("%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y/%m/%d"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def decode(data):
    """Report bytes as text. These files are plain 8-bit output from the clock."""
    for encoding in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1", errors="replace")


def reparse_day_line(line, variance_end=DEFAULT_VARIANCE_END):
    """
    One stored day line read back into a ClockDayRow.

    This is what "revert" leans on: the exact text of the line is kept on every
    imported row, so an edit can always be undone by re-reading the file's own
    words rather than by trusting a second copy of the values. `variance_end`
    comes off the batch, because it is a property of the report the line was
    printed on.
    """
    if not line:
        return None
    columns = (list(BASE_COLUMNS)
               + [("variance", VARIANCE_START, variance_end),
                  ("description", variance_end, None)])
    cells = _cells(line, columns)
    work_date = parse_report_date(cells["date"])
    if work_date is None:
        return None
    return ClockDayRow(
        source_row=0,
        source_line=line,
        work_date=work_date,
        day_name=cells["day"],
        shift=cells["shift"],
        time_in=parse_clock_time(cells["time_in"]),
        time_out=parse_clock_time(cells["time_out"]),
        normal_hours=parse_hours(cells["nt"]),
        ot1_hours=parse_hours(cells["ot1"]),
        ot2_hours=parse_hours(cells["ot2"]),
        ot3_hours=parse_hours(cells["ot3"]),
        ot4_hours=parse_hours(cells["ot4"]),
        total_hours=parse_hours(cells["total"]),
        target_hours=parse_hours(cells["target"]),
        shifts=parse_int(cells["shifts"]),
        variance_hours=parse_hours(cells["variance"]),
        description=cells["description"],
    )
