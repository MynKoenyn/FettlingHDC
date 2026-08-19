"""
One-off migration of FurnaceTracker's live data (its own Postgres database,
`furnace_management`) into this app's database, now that the furnace module
has been merged in as a blueprint sharing this app's User/Personnel tables.

Not part of the running app — run by hand, once:

    venv\\Scripts\\python.exe furnace_data_migration.py            # dry run (default) — prints the plan, writes nothing
    venv\\Scripts\\python.exe furnace_data_migration.py --commit   # actually writes

Source connection defaults to the real FurnaceTracker database; override with
the FURNACE_LEGACY_DATABASE_URL env var if it has moved.

Matching rules (see the merge conversation for why):
  - Users: matched to an existing account by case-insensitive full name
    ("first_name last_name" vs. User.name); 'admin' always maps to this app's
    own 'admin' account. Unmatched source users become new User rows on the
    existing "User" role (never this app's admin role, even if their
    FurnaceTracker role was 'admin') with furnace.* permissions granted
    explicitly instead.
  - Personnel: matched by clock number, trying both the raw value and an
    "HDA"-prefixed variant. Unmatched personnel become new rows under the
    MELTING division/department.
  - Furnaces, metal grades, furnace entries, tap times, spectro results and
    tin/copper calculations are copied straight across with their foreign
    keys rewritten through the id maps built above.

Refuses --commit if furnace_entries in the target already has rows, so it
can't accidentally double-import.
"""
import argparse
import os
import sys
from datetime import datetime

from sqlalchemy import create_engine, text

SOURCE_URL = os.environ.get(
    "FURNACE_LEGACY_DATABASE_URL",
    "postgresql://postgres:HDC51986@localhost:5432/furnace_management",
)


def fetch_all(engine, sql):
    with engine.connect() as conn:
        return conn.execute(text(sql)).fetchall()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--commit", action="store_true",
                         help="Actually write to the target database. Default is dry-run (report only).")
    args = parser.parse_args()

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from app import app, db
    from models import User, Personnel, Role, Division, Department, Permission, UserPermission
    from furnace.models import Furnace, MetalGrade, FurnaceEntry, FurnaceTapTime, SpectroResult, TinCopperCalculation

    source = create_engine(SOURCE_URL)

    with app.app_context():
        if args.commit and FurnaceEntry.query.count() > 0:
            print("furnace_entries already has rows in the target database — aborting "
                  "to avoid double-importing. Nothing was written.")
            sys.exit(1)

        admin_user = User.query.filter_by(username="admin").first()
        user_role = Role.query.filter_by(name="User").first()
        melting_division = Division.query.filter_by(code="MEL").first()
        melting_dept = (
            Department.query.filter_by(name="MELTING", division_id=melting_division.id).first()
            if melting_division else None
        )
        if not admin_user or not user_role or not melting_division or not melting_dept:
            print("Missing one of: target 'admin' user, 'User' role, 'MEL' division, "
                  "'MELTING' department under it. Aborting.")
            sys.exit(1)

        # ── 1. Users ─────────────────────────────────────────────────────
        src_users = fetch_all(source, """
            SELECT id, username, password_hash, first_name, last_name, role,
                   active, created_at, last_login
            FROM users ORDER BY id
        """)
        target_users_by_name = {u.name.strip().lower(): u for u in User.query.all() if u.name}

        user_id_map = {}       # source user id -> target user id
        user_report = []       # (source_username, full_name, action, target_username, source_role)
        for su in src_users:
            full_name = f"{su.first_name} {su.last_name}".strip()
            if su.username.lower() == "admin":
                match = admin_user
            else:
                match = target_users_by_name.get(full_name.lower())
            if match:
                user_id_map[su.id] = match.id
                user_report.append((su.username, full_name, "matched", match.username, su.role))
            else:
                user_report.append((su.username, full_name, "new", None, su.role))

        # ── 2. Personnel ────────────────────────────────────────────────
        src_personnel = fetch_all(source, """
            SELECT id, name, role, clock_number, active, created_at
            FROM personnel ORDER BY id
        """)
        target_personnel_by_clockno = {p.clockno: p for p in Personnel.query.all()}

        personnel_id_map = {}
        personnel_report = []  # (source_name, source_clockno, action, matched_clockno, matched_name)
        for sp in src_personnel:
            match = (target_personnel_by_clockno.get(sp.clock_number)
                     or target_personnel_by_clockno.get(f"HDA{sp.clock_number}"))
            if match:
                personnel_id_map[sp.id] = match.id
                personnel_report.append((sp.name, sp.clock_number, "matched", match.clockno, match.name))
            else:
                personnel_report.append((sp.name, sp.clock_number, "new", None, None))

        # ── 3. Furnaces & metal grades ──────────────────────────────────
        src_furnaces = fetch_all(source, """
            SELECT id, name, capacity, capacity_unit, current_lining_number, status, created_at
            FROM furnaces ORDER BY id
        """)
        src_grades = fetch_all(source, """
            SELECT id, name, description, notes, created_at FROM metal_grades ORDER BY id
        """)

        # ── 4-6. Row counts for the report (full fetch happens only in --commit) ──
        counts = {}
        for label, table in [
            ("furnace_entries", "furnace_entries"),
            ("furnace_tap_times", "furnace_tap_times"),
            ("spectro_results", "spectro_results"),
            ("tin_copper_calculations", "tin_copper_calculations"),
        ]:
            counts[label] = fetch_all(source, f"SELECT COUNT(*) AS n FROM {table}")[0].n

        # ══════════════════════════════════════════════════════════════
        # Report
        # ══════════════════════════════════════════════════════════════
        print("=" * 78)
        print("USERS", f"({len(user_report)} in source)")
        print("=" * 78)
        for src_username, full_name, action, target_username, source_role in user_report:
            grants = "furnace.view + furnace.capture" + (" + furnace.admin" if source_role == "admin" else "")
            if action == "matched":
                if target_username == "admin":
                    print(f"  MATCH   {src_username:<12} ({full_name:<24}) -> existing user 'admin' (already holds every permission, no change)")
                else:
                    print(f"  MATCH   {src_username:<12} ({full_name:<24}) -> existing user '{target_username}', grants: {grants}")
            else:
                print(f"  NEW     {src_username:<12} ({full_name:<24}) -> new user, role='User', grants: {grants}")
        n_new_users = sum(1 for r in user_report if r[2] == "new")
        print(f"  -> {len(user_report) - n_new_users} matched, {n_new_users} new users to create")

        print()
        print("=" * 78)
        print("PERSONNEL", f"({len(personnel_report)} in source)")
        print("=" * 78)
        for name, clockno, action, matched_clockno, matched_name in personnel_report:
            if action == "matched":
                print(f"  MATCH   {name:<20} ({clockno:<8}) -> existing personnel '{matched_name}' ({matched_clockno})")
            else:
                print(f"  NEW     {name:<20} ({clockno:<8}) -> new personnel under MELTING / {melting_dept.name}")
        n_new_personnel = sum(1 for r in personnel_report if r[2] == "new")
        print(f"  -> {len(personnel_report) - n_new_personnel} matched, {n_new_personnel} new personnel to create")

        print()
        print("=" * 78)
        print("STRAIGHT COPY")
        print("=" * 78)
        print(f"  furnaces                 : {len(src_furnaces)}")
        print(f"  metal_grades              : {len(src_grades)}")
        print(f"  furnace_entries           : {counts['furnace_entries']}")
        print(f"  furnace_tap_times         : {counts['furnace_tap_times']}")
        print(f"  spectro_results           : {counts['spectro_results']}")
        print(f"  tin_copper_calculations   : {counts['tin_copper_calculations']}")

        if not args.commit:
            print()
            print("Dry run only — nothing was written. Re-run with --commit to apply.")
            return

        # ══════════════════════════════════════════════════════════════
        # Commit
        # ══════════════════════════════════════════════════════════════
        print()
        print("Writing changes...")

        def grant(user_id, module, action_name):
            perm = Permission.query.filter_by(module=module, action=action_name).first()
            if not perm:
                return
            exists = UserPermission.query.filter_by(user_id=user_id, permission_id=perm.id).first()
            if not exists:
                db.session.add(UserPermission(
                    user_id=user_id, permission_id=perm.id,
                    granted_by=admin_user.id, granted_at=datetime.now(),
                ))

        # 1. Users
        for su in src_users:
            if su.id not in user_id_map:
                base_username = su.username
                candidate = base_username
                suffix = 1
                while User.query.filter_by(username=candidate).first():
                    suffix += 1
                    candidate = f"{base_username}{suffix}"
                new_user = User(
                    username=candidate,
                    name=f"{su.first_name} {su.last_name}".strip(),
                    password_hash=su.password_hash,
                    role_id=user_role.id,
                    active=su.active,
                    created_at=su.created_at,
                    last_login=su.last_login,
                )
                db.session.add(new_user)
                db.session.flush()
                user_id_map[su.id] = new_user.id

            if su.username.lower() == "admin":
                continue  # already holds every permission via app.py's startup grant
            target_id = user_id_map[su.id]
            grant(target_id, "furnace", "view")
            grant(target_id, "furnace", "capture")
            if su.role == "admin":
                grant(target_id, "furnace", "admin")

        # 2. Personnel
        for sp in src_personnel:
            if sp.id in personnel_id_map:
                existing = Personnel.query.get(personnel_id_map[sp.id])
                if not existing.furnace_role:
                    existing.furnace_role = sp.role
                continue
            new_p = Personnel(
                name=sp.name,
                surname="",  # not known from FurnaceTracker; DB requires NOT NULL
                clockno=sp.clock_number,
                department_id=melting_dept.id,
                division_id=melting_division.id,
                furnace_role=sp.role,
                status=sp.active,
            )
            db.session.add(new_p)
            db.session.flush()
            personnel_id_map[sp.id] = new_p.id

        # 3. Furnaces & metal grades
        furnace_id_map = {}
        for sf in src_furnaces:
            nf = Furnace(name=sf.name, capacity=sf.capacity, capacity_unit=sf.capacity_unit,
                         current_lining_number=sf.current_lining_number, status=sf.status,
                         created_at=sf.created_at)
            db.session.add(nf)
            db.session.flush()
            furnace_id_map[sf.id] = nf.id

        grade_id_map = {}
        for sg in src_grades:
            ng = MetalGrade(name=sg.name, description=sg.description, notes=sg.notes,
                            created_at=sg.created_at)
            db.session.add(ng)
            db.session.flush()
            grade_id_map[sg.id] = ng.id

        # 4. Furnace entries (need new ids up front, for tap times / spectro results)
        entry_cols = """
            id, date, heat_number, furnace_id, metal_grade_id, melt_technician_id,
            furnace_operator_id, lining_number, cast_iron, steel_scrap, pig_iron,
            recarb, ferrosilicon, ferromanganese, iron_sulfide, additional_recarb,
            additional_fesi, additional_femn, additional_iron_sulfide, tin, copper,
            melt_temperature, inoculate_used, remarks, start_charging_time,
            additions_added_time, tap_times, end_melt_time, created_at, updated_at,
            last_activity_at, completed_at, status
        """
        src_entries = fetch_all(source, f"SELECT {entry_cols} FROM furnace_entries ORDER BY id")

        entry_id_map = {}
        new_entries = []
        for se in src_entries:
            ne = FurnaceEntry(
                date=se.date, heat_number=se.heat_number,
                furnace_id=furnace_id_map.get(se.furnace_id),
                metal_grade_id=grade_id_map.get(se.metal_grade_id),
                melt_technician_id=personnel_id_map.get(se.melt_technician_id),
                furnace_operator_id=personnel_id_map.get(se.furnace_operator_id),
                lining_number=se.lining_number, cast_iron=se.cast_iron, steel_scrap=se.steel_scrap,
                pig_iron=se.pig_iron, recarb=se.recarb, ferrosilicon=se.ferrosilicon,
                ferromanganese=se.ferromanganese, iron_sulfide=se.iron_sulfide,
                additional_recarb=se.additional_recarb, additional_fesi=se.additional_fesi,
                additional_femn=se.additional_femn, additional_iron_sulfide=se.additional_iron_sulfide,
                tin=se.tin, copper=se.copper, melt_temperature=se.melt_temperature,
                inoculate_used=se.inoculate_used, remarks=se.remarks,
                start_charging_time=se.start_charging_time, additions_added_time=se.additions_added_time,
                tap_times=se.tap_times, end_melt_time=se.end_melt_time, created_at=se.created_at,
                updated_at=se.updated_at, last_activity_at=se.last_activity_at,
                completed_at=se.completed_at, status=se.status,
            )
            db.session.add(ne)
            new_entries.append((se.id, ne))
        db.session.flush()
        for old_id, ne in new_entries:
            entry_id_map[old_id] = ne.id
        print(f"  furnace_entries: {len(new_entries)} inserted")

        # 5. Tap times (bulk)
        src_taps = fetch_all(source, """
            SELECT entry_id, tap_time, temperature, innoculate, department
            FROM furnace_tap_times ORDER BY id
        """)
        tap_rows = [
            {
                "entry_id": entry_id_map[t.entry_id],
                "tap_time": t.tap_time, "temperature": t.temperature,
                "innoculate": t.innoculate, "department": t.department,
            }
            for t in src_taps if t.entry_id in entry_id_map
        ]
        if tap_rows:
            db.session.bulk_insert_mappings(FurnaceTapTime, tap_rows)
        print(f"  furnace_tap_times: {len(tap_rows)} inserted")

        # 6. Tin/copper calculations (bulk)
        src_tc = fetch_all(source, """
            SELECT date, heat_number, operator_id, furnace_id, metal_grade_id, weight,
                   base_tin, tin_to_be_added, tin_added, base_copper, copper_to_be_added,
                   copper_added, starting_tin, starting_copper, tin_issued, copper_issued,
                   created_at, updated_at
            FROM tin_copper_calculations ORDER BY id
        """)
        tc_rows = [
            {
                "date": r.date, "heat_number": r.heat_number,
                "operator_id": personnel_id_map.get(r.operator_id),
                "furnace_id": furnace_id_map.get(r.furnace_id),
                "metal_grade_id": grade_id_map.get(r.metal_grade_id),
                "weight": r.weight, "base_tin": r.base_tin, "tin_to_be_added": r.tin_to_be_added,
                "tin_added": r.tin_added, "base_copper": r.base_copper,
                "copper_to_be_added": r.copper_to_be_added, "copper_added": r.copper_added,
                "starting_tin": r.starting_tin, "starting_copper": r.starting_copper,
                "tin_issued": r.tin_issued, "copper_issued": r.copper_issued,
                "created_at": r.created_at, "updated_at": r.updated_at,
            }
            for r in src_tc
        ]
        if tc_rows:
            db.session.bulk_insert_mappings(TinCopperCalculation, tc_rows)
        print(f"  tin_copper_calculations: {len(tc_rows)} inserted")

        # 7. Spectro results (bulk) — entry_id left NULL where the source had none
        element_cols = [
            "ele_c", "ele_si", "ele_mn", "ele_p", "ele_s", "ele_cr", "ele_mo", "ele_ni",
            "ele_al", "ele_co", "ele_cu", "ele_nb", "ele_ti", "ele_v", "ele_w", "ele_pb",
            "ele_sn", "ele_mg", "ele_as", "ele_zr", "ele_bi", "ele_ce", "ele_sb", "ele_se",
            "ele_te", "ele_b", "ele_zn", "ele_la", "ele_n", "ele_fe",
        ]
        src_spectro = fetch_all(source, f"""
            SELECT entry_id, measure_date, measure_time, method_name, calc_mode,
                   melt_technician, grade_id, heat_number, plant, furnace, sample_type,
                   pot_number, metal_grade, cu_addition, sn_addition, created_at,
                   {", ".join(element_cols)}
            FROM spectro_results ORDER BY id
        """)
        spectro_rows = []
        for r in src_spectro:
            row = {
                "entry_id": entry_id_map.get(r.entry_id) if r.entry_id else None,
                "measure_date": r.measure_date, "measure_time": r.measure_time,
                "method_name": r.method_name, "calc_mode": r.calc_mode,
                "melt_technician": r.melt_technician, "grade_id": r.grade_id,
                "heat_number": r.heat_number, "plant": r.plant, "furnace": r.furnace,
                "sample_type": r.sample_type, "pot_number": r.pot_number,
                "metal_grade": r.metal_grade, "cu_addition": r.cu_addition,
                "sn_addition": r.sn_addition, "created_at": r.created_at,
            }
            for col in element_cols:
                row[col] = getattr(r, col)
            spectro_rows.append(row)
        if spectro_rows:
            db.session.bulk_insert_mappings(SpectroResult, spectro_rows)
        print(f"  spectro_results: {len(spectro_rows)} inserted")

        db.session.commit()
        print()
        print("Done.")


if __name__ == "__main__":
    main()
