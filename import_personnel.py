"""
Two-pass personnel import from the HR spreadsheet.

Usage:
    python import_personnel.py <path-to-xlsx> [--dry-run]

Pass 1: upsert personnel rows (matched by EMP NO / clockno), resolving
        Division and Department by name.
Pass 2: resolve Supervisor (against personnel) and Manager (against users),
        then set personnel.supervisor_id and insert personnel_managers rows.

Unresolved links never abort the import — they are written to
import_exceptions.csv for manual fix-up, after which the script can simply
be re-run (it is idempotent).
"""
import sys
import csv
from datetime import datetime

import pandas as pd

from app import app, db
from models import (Personnel, PersonnelManager, Division, Department, User)

EXCEPTIONS_FILE = "import_exceptions.csv"

COLUMN_MAP = {
    "EMP NO": "clockno",
    "EMP NO.": "clockno",
    "SURNAME": "surname",
    "FIRST NAMES": "name",
    "ID NUMBER": "id_no",
    "GENDER": "gender",
    "RACE": "race",
    "HDC GRADE": "jobgrade",
    "DIVISION": "division",
    "HDC JOB DESCRIPTION": "job_description",
    "DEPARTMENT": "department",
    "SUPERVISOR": "supervisor",
    "MANAGER": "manager",
    "DATE JOINED": "joined",
}


def clean(value):
    """Normalise a cell to a stripped string, or None if empty/NaN."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    if pd.isna(value):
        return None
    text = str(value).strip()
    return text or None


def load_sheet(path):
    df = pd.read_excel(path, dtype=str)
    df.columns = [str(c).strip().upper().rstrip(".") for c in df.columns]

    rows = []
    for i, raw in df.iterrows():
        row = {}
        for src, dst in COLUMN_MAP.items():
            key = src.rstrip(".")
            if key in df.columns:
                row[dst] = clean(raw.get(key))
        row["_line"] = i + 2  # spreadsheet row number (1-based + header)
        if row.get("clockno"):
            rows.append(row)
    return rows


def parse_date(value):
    if not value:
        return None
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%Y/%m/%d", "%d %b %Y"):
        try:
            return datetime.strptime(value.split(" ")[0], fmt).date()
        except ValueError:
            continue
    try:
        return pd.to_datetime(value, dayfirst=True).date()
    except Exception:
        return None


def full_name_keys(surname, first_names):
    """Case-folded name variants used to match Supervisor/Manager by name."""
    keys = set()
    s = (surname or "").strip().lower()
    f = (first_names or "").strip().lower()
    if s and f:
        keys.add(f"{f} {s}")
        keys.add(f"{s} {f}")
        keys.add(f"{f.split(' ')[0]} {s}")
        keys.add(f"{s} {f.split(' ')[0]}")
    elif s or f:
        keys.add(s or f)
    return keys


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    path = sys.argv[1]
    dry_run = "--dry-run" in sys.argv

    rows = load_sheet(path)
    print(f"Loaded {len(rows)} rows from {path}")

    exceptions = []
    stats = {"inserted": 0, "updated": 0, "supervisors": 0, "managers": 0,
             "divisions_created": 0, "departments_created": 0}

    with app.app_context():
        # ------------------------------------------------------------------
        # PASS 1 — core personnel rows
        # ------------------------------------------------------------------
        divisions = {d.name.strip().lower(): d for d in Division.query.all()}
        departments = {d.name.strip().lower(): d for d in Department.query.all()}

        for row in rows:
            div_name = row.get("division")
            dep_name = row.get("department")

            division = divisions.get(div_name.lower()) if div_name else None
            if div_name and not division:
                division = Division(
                    code=div_name.upper()[:10],
                    name=div_name,
                )
                db.session.add(division)
                db.session.flush()
                divisions[div_name.lower()] = division
                stats["divisions_created"] += 1
                exceptions.append((row["_line"], row["clockno"], "division_created",
                                   f"Division '{div_name}' did not exist — created with code '{division.code}', please review"))

            department = departments.get(dep_name.lower()) if dep_name else None
            if dep_name and not department:
                if not division:
                    exceptions.append((row["_line"], row["clockno"], "no_division",
                                       f"Cannot create department '{dep_name}' without a division — row skipped"))
                    continue
                department = Department(name=dep_name, division_id=division.id)
                db.session.add(department)
                db.session.flush()
                departments[dep_name.lower()] = department
                stats["departments_created"] += 1
                exceptions.append((row["_line"], row["clockno"], "department_created",
                                   f"Department '{dep_name}' did not exist — created under '{division.name}', please review"))

            if not division or not department:
                exceptions.append((row["_line"], row["clockno"], "missing_org",
                                   "Row has no division/department — skipped"))
                continue

            person = Personnel.query.filter_by(clockno=row["clockno"]).first()
            if not person:
                person = Personnel(clockno=row["clockno"])
                db.session.add(person)
                stats["inserted"] += 1
            else:
                stats["updated"] += 1

            person.surname = row.get("surname") or person.surname or ""
            person.name = row.get("name") or person.name or ""
            person.id_no = row.get("id_no") or person.id_no
            person.gender = row.get("gender") or person.gender
            person.race = row.get("race") or person.race
            person.jobgrade = (row.get("jobgrade") or "")[:3] or person.jobgrade
            person.job_description = row.get("job_description") or person.job_description
            person.joined = parse_date(row.get("joined")) or person.joined
            person.division_id = division.id
            person.department_id = department.id

        db.session.flush()

        # ------------------------------------------------------------------
        # PASS 2 — resolve Supervisor and Manager links
        # ------------------------------------------------------------------
        people = Personnel.query.all()
        by_clockno = {p.clockno.strip().lower(): p for p in people if p.clockno}
        by_name = {}
        for p in people:
            for key in full_name_keys(p.surname, p.name):
                by_name.setdefault(key, p)

        users_by_name = {}
        for u in User.query.filter_by(active=True).all():
            users_by_name[u.name.strip().lower()] = u
            users_by_name[u.username.strip().lower()] = u

        for row in rows:
            person = by_clockno.get(row["clockno"].strip().lower())
            if not person:
                continue

            # ---- Supervisor → personnel.supervisor_id ----
            sup = row.get("supervisor")
            if sup:
                target = (by_clockno.get(sup.strip().lower())
                          or by_name.get(sup.strip().lower()))
                if target and target.id != person.id:
                    person.supervisor_id = target.id
                    stats["supervisors"] += 1
                elif not target:
                    exceptions.append((row["_line"], row["clockno"], "supervisor_unmatched",
                                       f"Supervisor '{sup}' not found in personnel"))

            # ---- Manager → personnel_managers (needs a User login) ----
            mgr = row.get("manager")
            if mgr:
                user = users_by_name.get(mgr.strip().lower())
                if user:
                    exists = PersonnelManager.query.filter_by(
                        personnel_id=person.id, manager_id=user.id
                    ).first()
                    if not exists:
                        db.session.add(PersonnelManager(
                            personnel_id=person.id, manager_id=user.id
                        ))
                        stats["managers"] += 1
                else:
                    exceptions.append((row["_line"], row["clockno"], "manager_no_login",
                                       f"Manager '{mgr}' has no user login — create the account, then re-run"))

        # ------------------------------------------------------------------
        # Commit + report
        # ------------------------------------------------------------------
        if dry_run:
            db.session.rollback()
            print("DRY RUN — nothing committed.")
        else:
            db.session.commit()

    print(f"\nPersonnel inserted : {stats['inserted']}")
    print(f"Personnel updated  : {stats['updated']}")
    print(f"Supervisors linked : {stats['supervisors']}")
    print(f"Manager grants     : {stats['managers']}")
    print(f"Divisions created  : {stats['divisions_created']}")
    print(f"Departments created: {stats['departments_created']}")
    print(f"Exceptions         : {len(exceptions)}")

    if exceptions:
        with open(EXCEPTIONS_FILE, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["row", "emp_no", "type", "detail"])
            writer.writerows(exceptions)
        print(f"\nExceptions written to {EXCEPTIONS_FILE}:")
        for line, clockno, kind, detail in exceptions[:20]:
            print(f"  row {line} [{clockno}] {kind}: {detail}")
        if len(exceptions) > 20:
            print(f"  ... and {len(exceptions) - 20} more (see {EXCEPTIONS_FILE})")


if __name__ == "__main__":
    main()
