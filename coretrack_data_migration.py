"""
One-off migration of CoreTrackIndustrial's live data (its own Postgres
database, `CoreBlower`) into this app's database, now that it has been
merged in as the "HDA Core Production" module under Daily Production.

Not part of the running app — run by hand, once:

    venv\\Scripts\\python.exe coretrack_data_migration.py            # dry run (default) — prints the plan, writes nothing
    venv\\Scripts\\python.exe coretrack_data_migration.py --commit   # actually writes

Source connection defaults to the real CoreTrackIndustrial database;
override with the CORETRACK_LEGACY_DATABASE_URL env var if it has moved.

Matching rules:
  - Users: matched to an existing account first by case-insensitive
    username, then by case-insensitive full name ("first_name last_name"
    vs. User.name); 'admin' always maps to this app's own 'admin' account.
    Unmatched source users become new User rows on the existing "User"
    role (never this app's admin role, even if their CoreTrackIndustrial
    role was 'admin') with dailyproduction.* permissions granted
    explicitly instead. Password hashes are copied as-is — both apps hash
    with werkzeug's default method, so migrated accounts keep working
    with their old password.
  - Production targets and production entries are copied straight across
    with operator_id/supervisor_id rewritten through the user id map built
    above. Shift/Machine/RemarkCategory values are identical strings in
    both apps, so no value translation is needed.

Refuses --commit if production_entries or production_targets in the target
already has rows, so it can't accidentally double-import.

Known gap: CoreTrackIndustrial's own populate_targets.py seeded a legacy
combined 'top_bottom' machine that predates the Top/Bottom split in the
Machine enum, so the source production_targets table has no rows for
'top' or 'bottom' — nothing to migrate for those two machines. Capture
their targets by hand afterwards via Daily Production > HDA Core
Production > Targets.
"""
import argparse
import os
import sys
from datetime import datetime

from sqlalchemy import create_engine, text

SOURCE_URL = os.environ.get(
    "CORETRACK_LEGACY_DATABASE_URL",
    "postgresql://postgres:HDC51986@192.168.1.235:5432/CoreBlower",
)


def fetch_all(engine, sql):
    with engine.connect() as conn:
        return conn.execute(text(sql)).fetchall()


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--commit", action="store_true",
                         help="Actually write to the target database. Default is dry-run (report only).")
    args = parser.parse_args()

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from app import app, db
    from models import User, Role, Permission, UserPermission
    from dailyproduction.models import ProductionEntry, ProductionTarget

    source = create_engine(SOURCE_URL)

    with app.app_context():
        if args.commit and (ProductionEntry.query.count() > 0 or ProductionTarget.query.count() > 0):
            print("production_entries or production_targets already has rows in the target "
                  "database — aborting to avoid double-importing. Nothing was written.")
            sys.exit(1)

        admin_user = User.query.filter_by(username="admin").first()
        user_role = Role.query.filter_by(name="User").first()
        if not admin_user or not user_role:
            print("Missing one of: target 'admin' user, 'User' role. Aborting.")
            sys.exit(1)

        # ── 1. Users ─────────────────────────────────────────────────────
        src_users = fetch_all(source, """
            SELECT id, username, email, password_hash, first_name, last_name, role,
                   active, created_at, last_login
            FROM users ORDER BY id
        """)
        target_users_by_username = {u.username.strip().lower(): u for u in User.query.all()}
        target_users_by_name = {u.name.strip().lower(): u for u in User.query.all() if u.name}

        user_id_map = {}       # source user id -> target user id
        user_report = []       # (source_username, full_name, action, target_username, source_role)
        for su in src_users:
            full_name = f"{su.first_name} {su.last_name}".strip()
            if su.username.lower() == "admin":
                match = admin_user
            else:
                match = (target_users_by_username.get(su.username.strip().lower())
                         or target_users_by_name.get(full_name.lower()))
            if match:
                user_id_map[su.id] = match.id
                user_report.append((su.username, full_name, "matched", match.username, su.role))
            else:
                user_report.append((su.username, full_name, "new", None, su.role))

        # ── 2. Production targets ───────────────────────────────────────
        src_targets = fetch_all(source, """
            SELECT machine, hour, hourly_target, shift_target, created_at, updated_at
            FROM production_targets ORDER BY machine, hour
        """)

        # ── 3. Production entries ───────────────────────────────────────
        src_entries = fetch_all(source, """
            SELECT id, date, production_date, shift, hour, machine, cores_produced, defects,
                   remark_category, remark_text, downtime_minutes, operator_id, supervisor_id,
                   created_at, updated_at
            FROM production_entries ORDER BY id
        """)

        # ══════════════════════════════════════════════════════════════
        # Report
        # ══════════════════════════════════════════════════════════════
        print("=" * 78)
        print("USERS", f"({len(user_report)} in source)")
        print("=" * 78)
        for src_username, full_name, action, target_username, source_role in user_report:
            grants = "dailyproduction.view + dailyproduction.capture" + (
                " + dailyproduction.admin" if source_role == "admin" else ""
            )
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
        print("STRAIGHT COPY")
        print("=" * 78)
        print(f"  production_targets        : {len(src_targets)}")
        print(f"  production_entries         : {len(src_entries)}")
        missing_machines = {"top", "bottom"} - {t.machine for t in src_targets}
        if missing_machines:
            print(f"  (source has no targets at all for: {', '.join(sorted(missing_machines))} — capture these by hand afterwards)")

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
            grant(target_id, "dailyproduction", "view")
            grant(target_id, "dailyproduction", "capture")
            if su.role == "admin":
                grant(target_id, "dailyproduction", "admin")

        db.session.flush()

        # 2. Production targets (bulk)
        target_rows = [
            {
                "machine": t.machine, "hour": t.hour, "hourly_target": t.hourly_target,
                "shift_target": t.shift_target, "created_at": t.created_at, "updated_at": t.updated_at,
            }
            for t in src_targets
        ]
        if target_rows:
            db.session.bulk_insert_mappings(ProductionTarget, target_rows)
        print(f"  production_targets: {len(target_rows)} inserted")

        # 3. Production entries (bulk) — operator/supervisor rewritten through the user id map
        skipped = 0
        entry_rows = []
        for e in src_entries:
            operator_id = user_id_map.get(e.operator_id)
            supervisor_id = user_id_map.get(e.supervisor_id) if e.supervisor_id else None
            if operator_id is None:
                skipped += 1
                continue
            entry_rows.append({
                "date": e.date, "production_date": e.production_date, "shift": e.shift,
                "hour": e.hour, "machine": e.machine, "cores_produced": e.cores_produced,
                "defects": e.defects, "remark_category": e.remark_category,
                "remark_text": e.remark_text, "downtime_minutes": e.downtime_minutes,
                "operator_id": operator_id, "supervisor_id": supervisor_id,
                "created_at": e.created_at, "updated_at": e.updated_at,
            })
        if entry_rows:
            db.session.bulk_insert_mappings(ProductionEntry, entry_rows)
        print(f"  production_entries: {len(entry_rows)} inserted"
              + (f", {skipped} skipped (no matching operator)" if skipped else ""))

        db.session.commit()
        print()
        print("Done.")


if __name__ == "__main__":
    main()
