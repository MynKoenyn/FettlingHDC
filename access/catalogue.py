"""
Access control — the permission catalogue
=========================================

One place that lists every function in the system a user can be granted or
denied. Rows are seeded into the `permissions` table on startup, and the
Access Control screens tick them on and off per user.

Adding a new screen? Add its (module, action, label) here and it appears on
the permission matrix automatically — no template changes needed.
"""

from app import db
from models import Permission


# ── Module metadata — drives the grouping, icons and order in the UI ─────────
# module: (label, icon, group)
MODULE_META = {
    "customers":      ("Customers",        "bi-people",              "Master Data"),
    "products":       ("Products",         "bi-box-seam",            "Master Data"),
    "pricelists":     ("Price Lists",      "bi-tags",                "Master Data"),
    "personnel":      ("Personnel",        "bi-person-vcard",        "Master Data"),
    "managers":       ("Heads",            "bi-diagram-3",           "Master Data"),
    "org":            ("Org Structure",    "bi-diagram-2",           "Master Data"),

    "fettling":       ("Fettling",         "bi-tools",               "Production"),
    "dailyproduction": ("Daily Production", "bi-clipboard-data",     "Production"),
    "stocktake":      ("Stocktake",        "bi-boxes",               "Production"),
    "scrap":          ("Scrap",            "bi-trash3",              "Production"),
    "furnace":        ("Furnace",          "bi-fire",                "Production"),

    "overtime":       ("Overtime",         "bi-clock-history",       "Operations"),
    "timeclock":      ("Time Clock",       "bi-stopwatch",           "Operations"),
    "assets":         ("Assets",           "bi-hdd-stack",           "Operations"),
    "procurement":    ("Procurement",      "bi-cart",                "Operations"),
    "inventory":      ("Inventory",        "bi-archive",             "Operations"),

    "users":          ("Users",            "bi-person-gear",         "Administration"),
    "access":         ("Access Control",   "bi-shield-lock",         "Administration"),
}

GROUP_ORDER = ["Production", "Master Data", "Operations", "Administration"]


# ── The catalogue itself — (module, action, label) ───────────────────────────
PERMISSION_CATALOGUE = [
    # ── Master data ──────────────────────────────────────────────
    ("customers",  "view",    "View the customer list"),
    ("customers",  "edit",    "Add and edit customers"),

    ("products",   "view",    "View the product list"),
    ("products",   "edit",    "Add and edit products, codes and prices"),
    ("products",   "import",  "Import and export the product list as CSV/Excel"),

    ("pricelists", "view",    "View price lists and look up historical prices"),
    ("pricelists", "edit",    "Create periods, set prices and copy prices forward"),
    ("pricelists", "import",  "Import and export a period's prices as CSV/Excel"),

    ("personnel",  "view",    "View personnel records"),
    ("personnel",  "edit",    "Add and edit personnel"),

    ("managers",   "view",    "View head assignments"),
    ("managers",   "edit",    "Assign who may request and approve for whom (Heads)"),

    ("org",        "view",    "View divisions and departments"),
    ("org",        "edit",    "Add, rename, merge and delete divisions and departments"),

    # ── Production ───────────────────────────────────────────────
    ("fettling",   "view",    "View fettling entries and reports"),
    ("fettling",   "capture", "Capture fettling production"),

    ("dailyproduction", "view",    "View daily production and reports"),
    ("dailyproduction", "capture", "Capture daily production"),
    ("dailyproduction", "admin",   "Manage HDA Core Production targets"),

    ("stocktake",  "view",    "View stocktakes and variances"),
    ("stocktake",  "capture", "Open sessions and capture counts"),
    ("stocktake",  "value",   "View the Rand value of stock counts, priced from the price list"),

    ("scrap",      "view",    "View scrap entries and reports"),
    ("scrap",      "capture", "Capture internal scrap"),
    ("scrap",      "import",  "Import external customer scrap reports"),
    ("scrap",      "admin",   "Manage reject reasons and reverse imports"),

    ("furnace",    "view",    "View furnace entries, reports and lab results"),
    ("furnace",    "capture", "Capture melt entries, tap times and tin/copper additions"),
    ("furnace",    "admin",   "Manage furnaces, metal grades and administer the furnace module"),

    # ── Operations ───────────────────────────────────────────────
    ("overtime",   "view",    "View overtime requests"),
    ("overtime",   "request", "Submit overtime requests for personnel"),
    ("overtime",   "approve", "Approve or reject overtime requests"),
    ("overtime",   "actual",  "Capture and edit actual overtime worked"),
    ("overtime",   "all_personnel", "Choose from all personnel, not just their own division"),
    ("overtime",   "rates",   "View overtime rates and calculated amounts"),
    ("overtime",   "admin",   "Administer overtime module"),

    ("timeclock",  "view",    "View imported clock hours"),
    ("timeclock",  "import",  "Import Turbo Time clock reports"),
    ("timeclock",  "edit",    "Correct imported rows and match employees to personnel"),
    ("timeclock",  "rates",   "View the cost figures printed on the clock report"),
    ("timeclock",  "admin",   "Reverse clock imports"),

    ("assets",     "view",    "View fixed assets"),
    ("assets",     "edit",    "Edit asset records and run depreciation"),
    ("assets",     "admin",   "Administer assets module"),

    ("procurement", "view",    "View purchase orders"),
    ("procurement", "request", "Submit purchase requisitions"),
    ("procurement", "approve", "Approve purchase orders"),
    ("procurement", "admin",   "Administer procurement module"),

    ("inventory",  "view",    "View inventory"),
    ("inventory",  "edit",    "Edit inventory records"),
    ("inventory",  "admin",   "Administer inventory module"),

    # ── Administration ───────────────────────────────────────────
    ("users",      "view",    "View user accounts"),
    ("users",      "edit",    "Create and edit user accounts"),

    ("access",     "view",    "View who has access to what"),
    ("access",     "admin",   "Grant and revoke permissions"),
]


def seed_permissions():
    """
    Insert any catalogue rows missing from the permissions table, and refresh
    the labels of the ones already there.

    Safe on every startup — nothing is deleted, so a permission removed from
    the catalogue keeps working until it is cleaned up by hand.
    """
    existing = {(p.module, p.action): p for p in Permission.query.all()}

    created = 0
    for module, action, label in PERMISSION_CATALOGUE:
        current = existing.get((module, action))
        if current is None:
            db.session.add(Permission(module=module, action=action, label=label))
            created += 1
        elif current.label != label:
            current.label = label

    db.session.commit()
    return created


def module_label(module):
    return MODULE_META.get(module, (module.replace("_", " ").title(),))[0]


def module_icon(module):
    meta = MODULE_META.get(module)
    return meta[1] if meta else "bi-square"


def module_group(module):
    meta = MODULE_META.get(module)
    return meta[2] if meta else "Other"


def grouped_permissions():
    """
    The catalogue as it is rendered on the permission screens:

        [(group, [(module, label, icon, [Permission, ...]), ...]), ...]

    Driven off the database rather than the constant, so permissions added by
    hand still show up.
    """
    permissions = Permission.query.order_by(Permission.module, Permission.id).all()

    by_module = {}
    for perm in permissions:
        by_module.setdefault(perm.module, []).append(perm)

    # Keep each module's actions in catalogue order, unknown ones last
    catalogue_order = {
        (module, action): idx
        for idx, (module, action, _) in enumerate(PERMISSION_CATALOGUE)
    }
    for module, perms in by_module.items():
        perms.sort(key=lambda p: catalogue_order.get((p.module, p.action), 999))

    groups = {}
    for module, perms in by_module.items():
        groups.setdefault(module_group(module), []).append(
            (module, module_label(module), module_icon(module), perms)
        )

    ordered = []
    for group in GROUP_ORDER:
        if group in groups:
            ordered.append((group, sorted(groups.pop(group), key=lambda row: row[1])))
    for group in sorted(groups):
        ordered.append((group, sorted(groups[group], key=lambda row: row[1])))

    return ordered
