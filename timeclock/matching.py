"""
Time clock module — matching clock employees to personnel
=========================================================

The clock keeps its own employee numbers. On a real report 65 of 66 of them
are our clock numbers exactly, so the automatic pass does nearly all of it;
the point of this module is what happens to the rest.

Three passes, most trustworthy first:

  1. a remembered link  — someone already decided this number, once
  2. the clock number   — the number equals a Personnel clock number
  3. the name           — and only when exactly one person can be meant

Anything left is reported as unmatched rather than guessed. A wrong match is
worse than no match: it puts one person's hours against another's name, and
nothing downstream would ever question it.

Re-matching is safe to run at any time. It only ever fills in employees that
are still undecided, so a correction made by hand is never undone by a later
pass — see `rematch_batch`.
"""

import re
from datetime import datetime

from app import db
from models import Personnel
from timeclock.models import (
    ClockEmployee,
    ClockEmployeeLink,
    MATCH_CLOCKNO,
    MATCH_IGNORED,
    MATCH_LINK,
    MATCH_NAME,
    MATCH_MANUAL,
    MATCH_NONE,
)


# ── Normalisation ────────────────────────────────────────────────────────────

def norm_number(value):
    """
    A clock/employee number reduced to what actually identifies it.

    Case and surrounding space never mean anything, and neither do leading
    zeros — the clock prints 0127 for what we hold as 127. Everything else is
    left alone, because these numbers are not all numeric: M400, 18b/804 and
    HDA M1/801 are all real clock numbers here.
    """
    text = str(value or "").strip().lower()
    if not text:
        return ""
    stripped = text.lstrip("0")
    return stripped or text


def name_tokens(*parts):
    """
    A name broken into the words that identify it, initials kept apart.

    Returns (words, initials): words are the two-letter-and-longer pieces, and
    initials the single letters. The clock prints names every which way —
    'P. MNGUNI', 'D.M. MKHABELA', 'ZOLA V GXABUZA', 'DUBAZANA INNOCENT' with
    the surname first — so order is no help and only the pieces are used.
    """
    blob = " ".join(str(p or "") for p in parts).lower()
    blob = re.sub(r"[^a-z\s'-]", " ", blob)
    words, initials = set(), set()
    for piece in blob.split():
        piece = piece.strip("-'")
        if len(piece) > 1:
            words.add(piece)
        elif piece:
            initials.add(piece)
    return words, initials


# ── The personnel index the passes read ──────────────────────────────────────

class PersonnelIndex:
    """Everyone we could match to, keyed the ways a report might name them."""

    def __init__(self, people=None):
        self.people = list(people if people is not None else Personnel.query.all())

        self.by_number = {}
        self.by_surname = {}
        self.profiles = {}

        for person in self.people:
            key = norm_number(person.clockno)
            if key:
                self.by_number.setdefault(key, []).append(person)

            words, initials = name_tokens(person.name, person.surname)
            surname_words, _ = name_tokens(person.surname)
            self.profiles[person.id] = (words, initials, surname_words)

            for word in (surname_words or words):
                self.by_surname.setdefault(word, []).append(person)

    @staticmethod
    def _prefer_active(candidates):
        """
        Active people win a tie over inactive ones.

        Clock numbers get reissued when someone leaves, so the same number can
        sit on two personnel records. The person still employed is the one who
        worked the shift.
        """
        active = [p for p in candidates if p.status is not False]
        return active if active else list(candidates)

    def by_clock_number(self, emp_no):
        """(person, note) for an exact clock-number hit, else (None, note)."""
        candidates = self.by_number.get(norm_number(emp_no), [])
        if not candidates:
            return None, None
        if len(candidates) == 1:
            return candidates[0], None

        preferred = self._prefer_active(candidates)
        if len(preferred) == 1:
            return preferred[0], "Clock number is on more than one record — took the active one."
        names = ", ".join(f"{p.name} {p.surname or ''}".strip() for p in preferred)
        return None, f"Clock number {emp_no} is on more than one active record ({names})."

    def by_name(self, emp_name):
        """
        (person, note) for a name that can only mean one person.

        A candidate has to share the surname, and then agree on a first name or
        at least on its initial. 'P. MNGUNI' matches Phumzile Mnguni; if there
        were also a Petros Mnguni it matches neither, and says so.
        """
        words, initials = name_tokens(emp_name)
        if not words:
            return None, None

        candidates = []
        for word in words:
            candidates.extend(self.by_surname.get(word, []))

        seen, unique = set(), []
        for person in candidates:
            if person.id not in seen:
                seen.add(person.id)
                unique.append(person)
        if not unique:
            return None, None

        agreed = []
        for person in unique:
            p_words, p_initials, p_surname = self.profiles[person.id]

            # The whole surname has to be there — 'De Jager' is not 'Jager'.
            if p_surname and not p_surname <= words:
                continue

            rest = words - p_surname
            if rest & p_words:
                agreed.append(person)               # a first name in common
                continue
            first_letters = {w[0] for w in (p_words - p_surname)}
            if initials & first_letters:
                agreed.append(person)               # 'P.' for Phumzile
                continue
            if not rest and not initials and len(unique) == 1:
                agreed.append(person)               # surname alone, only one of them

        agreed = self._prefer_active(agreed) if len(agreed) > 1 else agreed

        if len(agreed) == 1:
            return agreed[0], None
        if len(agreed) > 1:
            names = ", ".join(f"{p.name} {p.surname or ''}".strip() for p in agreed)
            return None, f"'{emp_name}' could be any of {names} — matched by hand."
        return None, None


# ── The passes ───────────────────────────────────────────────────────────────

def load_links():
    """Remembered links, keyed by normalised employee number."""
    return {norm_number(link.emp_no): link for link in ClockEmployeeLink.query.all()}


def match_employee(employee, index, links):
    """
    Decide one clock employee. Sets the match on it and returns True when it
    ended up linked (or deliberately ignored).
    """
    link = links.get(norm_number(employee.emp_no))
    if link is not None:
        if link.personnel_id is None:
            employee.personnel_id = None
            employee.match_method = MATCH_IGNORED
            employee.match_note = link.note or "Remembered as not one of ours."
            return True
        employee.personnel_id = link.personnel_id
        employee.match_method = MATCH_LINK
        employee.match_note = link.note
        return True

    person, note = index.by_clock_number(employee.emp_no)
    if person is not None:
        employee.personnel_id = person.id
        employee.match_method = MATCH_CLOCKNO
        employee.match_note = note
        return True

    number_note = note

    person, note = index.by_name(employee.emp_name)
    if person is not None:
        employee.personnel_id = person.id
        employee.match_method = MATCH_NAME
        employee.match_note = note or (
            f"Matched on name — the clock's number ({employee.emp_no}) is not a "
            f"clock number here."
        )
        return True

    employee.personnel_id = None
    employee.match_method = MATCH_NONE
    employee.match_note = note or number_note
    return False


def match_employees(employees, index=None, links=None):
    """
    Run the passes over a list of ClockEmployee. Returns (matched, unmatched).

    Nothing is committed here — the caller decides when to write, so an import
    and its matching land in one transaction.
    """
    index = index or PersonnelIndex()
    links = load_links() if links is None else links

    matched = 0
    for employee in employees:
        if match_employee(employee, index, links):
            matched += 1
    return matched, len(employees) - matched


def rematch_batch(batch, include_decided=False):
    """
    Re-run matching over a batch, for after personnel have been added or a
    link created.

    Only the still-undecided employees are touched by default, so a match made
    by hand is never quietly replaced by a guess. `include_decided` re-runs
    everything — offered on the screen for when the personnel master itself was
    wrong, and clearly labelled there as overwriting manual matches.

    Returns (looked_at, newly_matched).
    """
    index = PersonnelIndex()
    links = load_links()

    if include_decided:
        candidates = list(batch.employees)
    else:
        candidates = [e for e in batch.employees if e.needs_match]

    newly = 0
    for employee in candidates:
        was_matched = employee.is_matched
        if match_employee(employee, index, links) and not was_matched:
            newly += 1

    refresh_counts(batch)
    return len(candidates), newly


def refresh_counts(batch):
    """Recount the matched/unmatched tallies stored on the batch."""
    batch.employees_total = len(batch.employees)
    batch.employees_matched = sum(1 for e in batch.employees if e.is_matched)
    batch.employees_unmatched = sum(1 for e in batch.employees if e.needs_match)


# ── Manual matching ──────────────────────────────────────────────────────────

def set_manual_match(employee, person, user_id, remember=True, apply_to_other_batches=False):
    """
    Attach a clock employee to a personnel record by hand.

    `person` of None means "not one of ours" — recorded as ignored rather than
    left looking undecided, so the unmatched count can legitimately reach zero.

    `remember` writes the decision to ClockEmployeeLink so next week's report
    picks it up without anyone doing this again. That is the whole reason the
    link table exists, so it is on by default.

    Returns the number of other batches also updated.
    """
    if person is None:
        employee.personnel_id = None
        employee.match_method = MATCH_IGNORED
        employee.match_note = "Marked as not one of ours."
    else:
        employee.link_to(person, method=MATCH_MANUAL, user_id=user_id)

    employee.matched_by = user_id
    employee.matched_at = datetime.now()

    if remember:
        _remember(employee.emp_no, person, employee.emp_name, user_id)

    if not apply_to_other_batches:
        return 0

    others = (
        ClockEmployee.query
        .filter(ClockEmployee.emp_no == employee.emp_no,
                ClockEmployee.id != employee.id)
        .all()
    )
    touched = 0
    for other in others:
        # Someone else's deliberate decision on another batch stands.
        if other.match_method == MATCH_MANUAL:
            continue
        if person is None:
            other.personnel_id = None
            other.match_method = MATCH_IGNORED
        else:
            other.link_to(person, method=MATCH_LINK, user_id=user_id)
        touched += 1
        refresh_counts(other.batch)

    return touched


def _remember(emp_no, person, emp_name, user_id):
    """Create or update the remembered link for an employee number."""
    key = str(emp_no or "").strip()
    if not key:
        return

    link = ClockEmployeeLink.query.filter_by(emp_no=key).first()
    if link is None:
        link = ClockEmployeeLink(emp_no=key, created_by=user_id)
        db.session.add(link)

    link.personnel_id = person.id if person else None
    link.emp_name = emp_name
    link.note = None if person else "Not one of ours."


def forget(emp_no):
    """Drop a remembered link. The next import decides that number afresh."""
    link = ClockEmployeeLink.query.filter_by(emp_no=str(emp_no or "").strip()).first()
    if link is None:
        return False
    db.session.delete(link)
    return True
