"""
Overtime calculation — shared by the requested and the actual side
==================================================================

One place for "how many hours is this?" and "what does it pay?", so a
captured actual is worked out exactly the way the original request was:
same midnight-crossing rule, same weekend/public-holiday multiplier.
"""

from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP


def get_sa_public_holidays(year):
    """The set of South African public holiday dates for a year, Easter included."""
    fixed = [
        date(year, 1, 1),    # New Year's Day
        date(year, 3, 21),   # Human Rights Day
        date(year, 4, 27),   # Freedom Day
        date(year, 5, 1),    # Workers' Day
        date(year, 6, 16),   # Youth Day
        date(year, 8, 9),    # National Women's Day
        date(year, 9, 24),   # Heritage Day
        date(year, 12, 16),  # Day of Reconciliation
        date(year, 12, 25),  # Christmas Day
        date(year, 12, 26),  # Day of Goodwill
    ]

    def easter(y):
        a = y % 19
        b = y // 100
        c = y % 100
        d = b // 4
        e = b % 4
        f = (b + 8) // 25
        g = (b - f + 1) // 3
        h = (19 * a + b - d - g + 15) % 30
        i = c // 4
        k = c % 4
        l = (32 + 2 * e + 2 * i - h - k) % 7
        m = (a + 11 * h + 22 * l) // 451
        month = (h + l - 7 * m + 114) // 31
        day = ((h + l - 7 * m + 114) % 31) + 1
        return date(y, month, day)

    easter_sunday = easter(year)
    good_friday = easter_sunday - timedelta(days=2)
    family_day = easter_sunday + timedelta(days=1)

    holidays = set(fixed + [good_friday, family_day])

    # A fixed holiday landing on Sunday is observed the following Monday.
    for h in list(holidays):
        if h.weekday() == 6:
            holidays.add(h + timedelta(days=1))

    return holidays


def _span(start_time, end_time):
    """
    The two times as datetimes on a nominal day, with the end pushed to the
    next day when it is at or before the start (a shift running past midnight).
    """
    start_dt = datetime.combine(date.today(), start_time)
    end_dt = datetime.combine(date.today(), end_time)
    if end_dt <= start_dt:
        end_dt += timedelta(days=1)
    return start_dt, end_dt


def compute_hours(start_time, end_time):
    """
    Hours between two times as a 2dp Decimal, treating end <= start as the
    shift running past midnight. None if either time is missing.
    """
    if not start_time or not end_time:
        return None
    start_dt, end_dt = _span(start_time, end_time)
    hours = Decimal((end_dt - start_dt).total_seconds()) / Decimal(3600)
    return hours.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def deduct_minutes(hours, *deductions):
    """
    Hours less any unpaid minutes, as a 2dp Decimal floored at zero.

    The deductions are plain minute totals — an unpaid lunch, and time lost to
    clocking in late or leaving early. They are entered as minutes rather than
    as times because that is how a supervisor knows them: nobody records the
    clock time a break started, they know it was half an hour.

    Hours of None passes straight through.
    """
    total = sum(m or 0 for m in deductions)
    if hours is None:
        return None
    if not total:
        return hours
    net = Decimal(hours) - (Decimal(total) / Decimal(60))
    if net < 0:
        net = Decimal(0)
    return net.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def overtime_multiplier(ot_date):
    """2.0 on Sundays and public holidays, 1.5 otherwise (incl. Saturday)."""
    if ot_date is None:
        return Decimal("1.5")
    if ot_date in get_sa_public_holidays(ot_date.year) or ot_date.weekday() == 6:
        return Decimal("2.0")
    return Decimal("1.5")


def compute_amount(rate, hours, multiplier):
    """rate × hours × multiplier, to 2dp. None when rate or hours is missing."""
    if rate is None or hours is None:
        return None
    amount = Decimal(rate) * Decimal(hours) * Decimal(multiplier)
    return amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
