"""
Follow-up fix to coretrack_data_migration.py.

That migration created a new User login for every CoreTrack operator it
couldn't match by name/username. But per plant convention only supervisors
get logins -- operators are shop-floor workers tracked in Personnel, the
same way the furnace and timeclock modules already reference them. This
script:

  1. Repoints production_entries.operator_id from those throwaway User rows
     onto the real Personnel record for the same person (all HDA / Core
     Blower), swapping the operator_id foreign key from users(id) to
     personnel(id) in the process. supervisor_id is untouched -- stanton,
     johnson and vincent really are supervisors (they only ever appear as
     supervisor_id, never operator_id) and keep their logins.
  2. Deletes the now-redundant operator User logins (and their
     dailyproduction UserPermission grants) once their entries have been
     repointed. Every one of them has never logged in (last_login IS NULL),
     confirming they were never actually used as accounts.

'marnis' (User Marnis Beyers, id 50) doesn't match anyone in Personnel --
per Armand, the real person is Clemence Mabela (HDA313); all of Marnis's
entries go there instead.

Not part of the running app -- run by hand, once:

    venv\\Scripts\\python.exe coretrack_operator_personnel_fix.py            # dry run (default)
    venv\\Scripts\\python.exe coretrack_operator_personnel_fix.py --commit   # actually writes

Safe to re-run: the FK-constraint swap is skipped once operator_id already
references personnel, and the per-user UPDATE/DELETE statements are no-ops
once their rows are already gone.
"""
import argparse
import sys
import os

from sqlalchemy import inspect, text

# user.id (the throwaway CoreTrack login) -> personnel.id (the real person)
USER_TO_PERSONNEL = {
    32: 212,  # elroy       -> Elroy Erroll Malgas       HDA049
    35: 213,  # nicholas    -> Nicholas T Khanyile        HDA056
    36: 215,  # sipho       -> Sipho Bhekuyise Shange     HDA061
    37: 276,  # melikhaya   -> Melikhaya Sigudla          HDA344
    38: 230,  # phineas     -> Phineas Mlandleni Dlongolo HDA108
    39: 279,  # sikhumbuzo  -> Sikhumbuzo Sipho Ntanda    HDA347
    40: 214,  # mhlabunzima -> Mhlabunzima Masikane       HDA058
    41: 229,  # victress    -> Nolulama Victress Sigudla  HDA107
    42: 245,  # lindiwe     -> Lindiwe Cynthia Msimango   HDA262
    43: 250,  # sizwe       -> Sizwe Dunywa               HDA292
    44: 225,  # thembinkosi -> Thembinkosi Emmanuel Ndzimande HDA103
    45: 209,  # eugine      -> Eugene David Snyman        HDA030
    46: 253,  # cyril       -> Cyril Mlamuli Nhlengethwa  HDA295
    47: 242,  # alfred      -> Alfred Masinga Malaza      HDA136
    48: 235,  # simpandla   -> Sipamandla Dlayedwa        HDA114
    49: 228,  # michael     -> Mahlatsi Michael Sootho    HDA106
    50: 260,  # marnis      -> Clemence Mabela (per Armand, not a name match) HDA313
    51: 273,  # vika        -> Vika Derrick Mbatha        HDA339
    10: 95,   # sammy       -> Samantha Rautenbach (existing User kept; only her operator entries move) HDA V3/811
}

# Users deleted once their entries are repointed -- every one of these has
# never logged in. sammy (10) keeps her User account; only her operator
# entries move to Personnel.
USERS_TO_DELETE = [32, 35, 36, 37, 38, 39, 40, 41, 42, 43, 44, 45, 46, 47, 48, 49, 50, 51]

FK_NAME = "production_entries_operator_id_fkey"


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--commit", action="store_true",
                         help="Actually write to the database. Default is dry-run (report only).")
    args = parser.parse_args()

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from app import app, db
    from models import User, Personnel
    from dailyproduction.models import ProductionEntry

    with app.app_context():
        insp = inspect(db.engine)
        fk_target = next(
            (fk["referred_table"] for fk in insp.get_foreign_keys("production_entries")
             if fk["constrained_columns"] == ["operator_id"]),
            None,
        )
        constraint_needs_swap = fk_target != "personnel"

        print("=" * 78)
        print("PLAN")
        print("=" * 78)
        print(f"  operator_id FK currently references: {fk_target!r}"
              f" ({'will swap to personnel' if constraint_needs_swap else 'already personnel, no swap needed'})")
        print()

        total_rows = 0
        for uid, pid in USER_TO_PERSONNEL.items():
            u = User.query.get(uid)
            p = Personnel.query.get(pid)
            count = ProductionEntry.query.filter_by(operator_id=uid).count()
            total_rows += count
            uname = u.name if u else f"(user {uid} already gone)"
            pname = f"{p.name} {p.surname or ''}".strip() if p else f"(personnel {pid} MISSING)"
            print(f"  {uname:<24} (user #{uid:<3}) -> {pname:<28} (personnel #{pid}) : {count} entries")
        print(f"  -> {total_rows} production_entries to repoint")

        print()
        print(f"  {len(USERS_TO_DELETE)} User logins to delete (none have ever logged in):")
        for uid in USERS_TO_DELETE:
            u = User.query.get(uid)
            if u:
                print(f"    id={uid:<3} {u.username:<14} {u.name}")
            else:
                print(f"    id={uid:<3} already deleted")

        if not args.commit:
            print()
            print("Dry run only -- nothing was written. Re-run with --commit to apply.")
            return

        print()
        print("Writing changes...")

        if constraint_needs_swap:
            db.session.execute(text(
                f"ALTER TABLE production_entries DROP CONSTRAINT IF EXISTS {FK_NAME}"
            ))

        moved = 0
        for uid, pid in USER_TO_PERSONNEL.items():
            result = db.session.execute(
                text("UPDATE production_entries SET operator_id = :pid WHERE operator_id = :uid"),
                {"pid": pid, "uid": uid},
            )
            moved += result.rowcount
        print(f"  production_entries.operator_id repointed: {moved} rows")

        if constraint_needs_swap:
            db.session.execute(text(
                f"ALTER TABLE production_entries ADD CONSTRAINT {FK_NAME} "
                f"FOREIGN KEY (operator_id) REFERENCES personnel(id)"
            ))
            print("  operator_id FK constraint now references personnel(id)")

        deleted_perms = db.session.execute(
            text("DELETE FROM user_permissions WHERE user_id = ANY(:ids)"),
            {"ids": USERS_TO_DELETE},
        ).rowcount
        deleted_users = db.session.execute(
            text("DELETE FROM users WHERE id = ANY(:ids)"),
            {"ids": USERS_TO_DELETE},
        ).rowcount
        print(f"  user_permissions deleted: {deleted_perms}")
        print(f"  users deleted: {deleted_users}")

        db.session.commit()
        print()
        print("Done.")


if __name__ == "__main__":
    main()
