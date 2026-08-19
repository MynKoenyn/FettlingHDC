"""
Scrap module — models
=====================

Two sources of scrap feed the same tables:

  * EXTERNAL — the customer's own reject report (e.g. the HDA monthly scrap
    sheet) imported from CSV / Excel. One row per machined part + batch +
    reject date, with the reject quantity split across defect columns.
  * INTERNAL — scrap picked up in-house, captured manually on the same
    structure so internal and external figures report side by side.

Every entry is linked to a Customer and (where the part number can be
matched) a Product, so scrap reports slice the same way the rest of the
system does.

A row that repeats one already loaded is not thrown away — it is parked as a
ScrapPendingDuplicate for someone to allow or reject, because the same part,
batch and date can legitimately be rejected twice.
"""

import json
from datetime import datetime

from app import db


SOURCE_EXTERNAL = "external"
SOURCE_INTERNAL = "internal"

SOURCE_CHOICES = [
    (SOURCE_EXTERNAL, "External (customer report)"),
    (SOURCE_INTERNAL, "Internal (captured in-house)"),
]

SOURCE_LABELS = dict(SOURCE_CHOICES)


# ── Reject-reason scope ──────────────────────────────────────────────────────
# A reason may belong to the customer's report (external), to the in-house
# capture form (internal), or to both. External and internal scrap are graded
# against different defect lists, so each side only offers its own reasons.
SCOPE_EXTERNAL = "external"
SCOPE_INTERNAL = "internal"
SCOPE_BOTH     = "both"

SCOPE_CHOICES = [
    (SCOPE_EXTERNAL, "External only (customer reports)"),
    (SCOPE_INTERNAL, "Internal only (in-house capture)"),
    (SCOPE_BOTH,     "Both"),
]

SCOPE_LABELS = dict(SCOPE_CHOICES)


# ── Duplicate review ─────────────────────────────────────────────────────────
DUP_PENDING  = "pending"    # waiting on a decision
DUP_ALLOWED  = "allowed"    # imported anyway
DUP_REJECTED = "rejected"   # discarded

DUP_STATUS_LABELS = {
    DUP_PENDING:  "Awaiting decision",
    DUP_ALLOWED:  "Allowed — imported",
    DUP_REJECTED: "Rejected — discarded",
}

CLASH_EXISTING = "existing"  # matches a row already in the system
CLASH_FILE     = "file"      # the same row appears twice in this one file


# ======================================================
# DEFECT CATALOGUE
# ======================================================
class ScrapDefect(db.Model):
    """
    One reject reason — i.e. one defect column on the customer's scrap sheet.

    `name` doubles as the column header used for CSV import/export, so the
    importer can line a spreadsheet's columns up with these rows. `aliases`
    holds extra comma-separated spellings a customer might use for the same
    defect.
    """
    __tablename__ = "scrap_defects"

    id          = db.Column(db.Integer, primary_key=True)
    code        = db.Column(db.String(10), unique=True, nullable=False)
    name        = db.Column(db.String(120), nullable=False)
    description = db.Column(db.String(255))
    aliases     = db.Column(db.String(255))
    sort_order  = db.Column(db.Integer, default=0)
    active      = db.Column(db.Boolean, default=True, nullable=False)
    # Which side this reason is offered on. Existing rows backfill to 'external'
    # (the catalogue started as the customer machining-reject columns); the
    # in-house foundry reasons are seeded as 'internal'.
    applies_to  = db.Column(
        db.String(10), nullable=False,
        default=SCOPE_BOTH, server_default=SCOPE_EXTERNAL,
    )

    lines = db.relationship(
        "ScrapEntryDefect",
        back_populates="defect",
        cascade="all, delete-orphan"
    )

    @property
    def alias_list(self):
        return [a.strip() for a in (self.aliases or "").split(",") if a.strip()]

    @property
    def scope_label(self):
        return SCOPE_LABELS.get(self.applies_to, self.applies_to)

    def __repr__(self):
        return f"<ScrapDefect {self.code} {self.name}>"


# ======================================================
# IMPORT BATCH  (one uploaded external report)
# ======================================================
class ScrapImportBatch(db.Model):
    """
    Audit record for one external scrap report upload.

    Keeping the batch lets us show exactly what a file did — imported vs
    ignored-as-duplicate vs unmatched — and lets a bad upload be undone in
    one click without touching anything imported before it.
    """
    __tablename__ = "scrap_import_batches"

    id           = db.Column(db.Integer, primary_key=True)
    filename     = db.Column(db.String(255))
    customer_id  = db.Column(db.Integer, db.ForeignKey("customers.id"), nullable=True)
    imported_at  = db.Column(db.DateTime, default=datetime.now)
    imported_by  = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)

    rows_total     = db.Column(db.Integer, default=0)   # data rows read from the file
    rows_imported  = db.Column(db.Integer, default=0)   # entries created (incl. allowed duplicates)
    rows_duplicate = db.Column(db.Integer, default=0)   # repeats found — held for review
    rows_skipped   = db.Column(db.Integer, default=0)   # unusable (no date / no qty)
    rows_unmatched = db.Column(db.Integer, default=0)   # imported, but no Product matched

    notes = db.Column(db.Text)   # unrecognised columns, row-level warnings

    customer = db.relationship("Customer")
    user     = db.relationship("User")
    entries  = db.relationship(
        "ScrapEntry",
        back_populates="batch",
        cascade="all, delete-orphan"
    )
    duplicates = db.relationship(
        "ScrapPendingDuplicate",
        back_populates="batch",
        cascade="all, delete-orphan",
        order_by="ScrapPendingDuplicate.source_row",
    )

    def _dupes(self, status):
        return sum(1 for d in self.duplicates if d.status == status)

    @property
    def duplicates_pending(self):
        return self._dupes(DUP_PENDING)

    @property
    def duplicates_allowed(self):
        return self._dupes(DUP_ALLOWED)

    @property
    def duplicates_rejected(self):
        return self._dupes(DUP_REJECTED)

    def __repr__(self):
        return f"<ScrapImportBatch {self.id} {self.filename}>"


# ======================================================
# SCRAP ENTRY
# ======================================================
class ScrapEntry(db.Model):
    """
    One scrap/reject line — one part, one batch, one date.

    External rows carry the full set of figures off the customer sheet;
    internal rows use the same shape but typically only fill entry_date,
    customer, product, qty_packed and qty_scrap.
    """
    __tablename__ = "scrap_entries"

    id     = db.Column(db.Integer, primary_key=True)
    source = db.Column(db.String(10), nullable=False, default=SOURCE_EXTERNAL, index=True)

    entry_date  = db.Column(db.Date, nullable=False, index=True)
    customer_id = db.Column(db.Integer, db.ForeignKey("customers.id"), nullable=True, index=True)
    product_id  = db.Column(db.Integer, db.ForeignKey("products.id"), nullable=True, index=True)

    # Raw identifiers exactly as they appear on the source report. Kept even
    # when a Product matched, so an unmatched row is never a lost row.
    machined_part_no = db.Column(db.String(60))
    casting_no       = db.Column(db.String(60))
    batch_no         = db.Column(db.String(40))

    qty_booked   = db.Column(db.Integer, default=0)   # QTY Booked
    qty_received = db.Column(db.Integer, default=0)   # Receiving QTY
    qty_scrap    = db.Column(db.Integer, default=0, nullable=False)
    # QTY Machined on the customer sheet (external rows only) — already the
    # customer's total, rejects included, so it's the reject % denominator
    # as-is. NULL/unused on internal rows.
    qty_machined = db.Column(db.Integer, default=0)
    # Quantity packed/inspected on internal rows only. Good units — the total
    # produced for the reject % is this plus qty_scrap (see total_qty below).
    qty_packed = db.Column(db.Integer, default=0)

    notes = db.Column(db.String(255))

    # Provenance — set for imported rows only
    batch_id    = db.Column(db.Integer, db.ForeignKey("scrap_import_batches.id"), nullable=True)
    source_row  = db.Column(db.Integer)
    # sha1 of customer + part + casting + batch + date. Unique, so re-importing
    # the same report cannot silently double up — a repeat is held for review
    # instead (see ScrapPendingDuplicate), and one that is allowed through is
    # stored under a suffixed key. NULL for internal rows (Postgres treats
    # NULLs as distinct), which may legitimately repeat.
    dedupe_key  = db.Column(db.String(64), unique=True)

    created_at = db.Column(db.DateTime, default=datetime.now)
    created_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)

    customer = db.relationship("Customer")
    product  = db.relationship("Product")
    batch    = db.relationship("ScrapImportBatch", back_populates="entries")
    user     = db.relationship("User", foreign_keys=[created_by])

    defect_lines = db.relationship(
        "ScrapEntryDefect",
        back_populates="entry",
        cascade="all, delete-orphan"
    )

    @property
    def entered_qty(self):
        """The raw quantity captured for this row — qty_machined on external
        rows, qty_packed on internal rows. For display, not for the reject %
        (see total_qty)."""
        return self.qty_packed if self.source == SOURCE_INTERNAL else self.qty_machined

    @property
    def total_qty(self):
        """
        Denominator for the reject percentage.

        External rows: qty_machined already is the customer's total machined
        (rejects included), so it's used as-is. Internal rows: qty_packed
        holds the good units only, so the total produced is packed + scrapped.
        """
        if self.source == SOURCE_INTERNAL:
            return (self.qty_packed or 0) + (self.qty_scrap or 0)
        return self.qty_machined or 0

    @property
    def reject_pct(self):
        """Scrap as a percentage of the total quantity produced, or None."""
        base = self.total_qty
        if not base:
            return None
        return (self.qty_scrap or 0) * 100.0 / base

    @property
    def defect_total(self):
        """Sum of the defect breakdown — should reconcile to qty_scrap."""
        return sum(line.qty or 0 for line in self.defect_lines)

    @property
    def is_reconciled(self):
        return self.defect_total == (self.qty_scrap or 0)

    @property
    def source_label(self):
        return SOURCE_LABELS.get(self.source, self.source)

    def __repr__(self):
        return f"<ScrapEntry {self.source} {self.entry_date} scrap={self.qty_scrap}>"


# ======================================================
# DISPATCH BATCH  (one truck/load — HDA's cage-tracked dispatch only)
# ======================================================
class ScrapDispatchBatch(db.Model):
    """
    One dispatch header — a single truck/load, invoiced and dispatched
    together. Only HDA's cage-based dispatch capture creates these; HDC's
    simple dispatch flow keeps writing standalone ScrapDispatch rows with no
    batch_id, exactly as before.

    Totals (qty / weight / cage count) are never stored — always summed live
    off `lines` — so they can't drift out of sync when a line is edited.
    """
    __tablename__ = "scrap_dispatch_batches"

    id = db.Column(db.Integer, primary_key=True)

    dispatch_date = db.Column(db.Date, nullable=False, index=True)
    customer_id   = db.Column(db.Integer, db.ForeignKey("customers.id"), nullable=True, index=True)

    invoice_no    = db.Column(db.String(50))
    dispatcher_id = db.Column(db.Integer, db.ForeignKey("personnel.id"), nullable=True)
    # Manually counted — doesn't necessarily equal the cage count (a heavy
    # cage can take more than one bag), so it isn't derived from the lines.
    total_black_bags = db.Column(db.Integer, default=0)

    notes = db.Column(db.String(255))

    created_at = db.Column(db.DateTime, default=datetime.now)
    created_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)

    customer   = db.relationship("Customer")
    dispatcher = db.relationship("Personnel")
    user       = db.relationship("User", foreign_keys=[created_by])

    lines = db.relationship(
        "ScrapDispatch",
        back_populates="batch",
        cascade="all, delete-orphan",
        order_by="ScrapDispatch.cage_no",
    )

    @property
    def total_qty(self):
        return sum(line.qty_dispatched or 0 for line in self.lines)

    @property
    def total_weight(self):
        return sum((line.weight or 0) for line in self.lines)

    @property
    def total_cages(self):
        return len(self.lines)

    def __repr__(self):
        return f"<ScrapDispatchBatch {self.dispatch_date} invoice={self.invoice_no}>"


# ======================================================
# DISPATCH  (packed stock leaving the site)
# ======================================================
class ScrapDispatch(db.Model):
    """
    One dispatch — a truck picking up packed stock for one part on one date.

    Nets against ScrapEntry.qty_packed (source=internal) to give the
    running "packed but not yet dispatched" balance per product. The balance
    itself is never stored — always summed live from these two tables — so
    it can't drift out of sync when an old entry is edited or deleted.

    HDA's cage-based dispatch capture links rows together via `batch_id` and
    fills in the cage_no/trenstar_no/weight/head_numbers/drawing_level
    columns, plus four physical-process checks ticked per cage; HDC's simple
    dispatch flow leaves all of those null/false.
    customer_id/dispatch_date are kept in step with the parent batch even
    when one is set, since the packed-balance and report queries filter
    directly on this table's own columns.
    """
    __tablename__ = "scrap_dispatches"

    id = db.Column(db.Integer, primary_key=True)

    dispatch_date = db.Column(db.Date, nullable=False, index=True)
    customer_id   = db.Column(db.Integer, db.ForeignKey("customers.id"), nullable=True, index=True)
    product_id    = db.Column(db.Integer, db.ForeignKey("products.id"), nullable=True, index=True)

    qty_dispatched = db.Column(db.Integer, nullable=False)

    notes = db.Column(db.String(255))

    # HDA cage-based dispatch only — null/false for HDC's plain rows
    batch_id      = db.Column(db.Integer, db.ForeignKey("scrap_dispatch_batches.id"), nullable=True, index=True)
    cage_no       = db.Column(db.Integer)          # 1, 2, 3… assigned by line order within the batch
    trenstar_no   = db.Column(db.String(40))       # Trenstar cage tag — typed or scanned
    weight        = db.Column(db.Numeric(10, 3))
    head_numbers  = db.Column(db.String(120))
    drawing_level = db.Column(db.String(10))       # snapshot of Product.drawing_level at dispatch time

    # Four physical-process confirmations, ticked per cage — recorded as-is,
    # never gate saving the dispatch.
    blue_card_confirmed           = db.Column(db.Boolean, default=False, nullable=False)
    black_bag_confirmed           = db.Column(db.Boolean, default=False, nullable=False)
    cage_packed_half_confirmed    = db.Column(db.Boolean, default=False, nullable=False)
    weighbridge_printed_confirmed = db.Column(db.Boolean, default=False, nullable=False)

    created_at = db.Column(db.DateTime, default=datetime.now)
    created_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)

    customer = db.relationship("Customer")
    product  = db.relationship("Product")
    user     = db.relationship("User", foreign_keys=[created_by])
    batch    = db.relationship("ScrapDispatchBatch", back_populates="lines")

    def __repr__(self):
        return f"<ScrapDispatch {self.dispatch_date} product={self.product_id} qty={self.qty_dispatched}>"


# ======================================================
# SCRAP ENTRY ↔ DEFECT  (the breakdown across reject reasons)
# ======================================================
class ScrapEntryDefect(db.Model):
    __tablename__ = "scrap_entry_defects"

    id        = db.Column(db.Integer, primary_key=True)
    entry_id  = db.Column(db.Integer, db.ForeignKey("scrap_entries.id", ondelete="CASCADE"), nullable=False)
    defect_id = db.Column(db.Integer, db.ForeignKey("scrap_defects.id"), nullable=False)
    qty       = db.Column(db.Integer, default=0, nullable=False)

    __table_args__ = (
        db.UniqueConstraint("entry_id", "defect_id", name="uq_scrap_entry_defect"),
    )

    entry  = db.relationship("ScrapEntry", back_populates="defect_lines")
    defect = db.relationship("ScrapDefect", back_populates="lines")

    def __repr__(self):
        return f"<ScrapEntryDefect entry={self.entry_id} defect={self.defect_id} qty={self.qty}>"


# ======================================================
# PENDING DUPLICATE  (an imported row that repeats one already loaded)
# ======================================================
class ScrapPendingDuplicate(db.Model):
    """
    A row the importer would not load because its dedupe key was already taken.

    The same part, batch and reject date can genuinely be rejected twice — a
    second reject on a re-worked batch, or a correction sent through on a
    later report — so the row is parked here with everything needed to create
    the entry, and someone decides: allow it in, or reject it.

    Nothing is lost either way — a rejected row stays here with who rejected
    it and when, which is the record of why the file's row count and the
    entries it created differ.
    """
    __tablename__ = "scrap_pending_duplicates"

    id       = db.Column(db.Integer, primary_key=True)
    batch_id = db.Column(db.Integer, db.ForeignKey("scrap_import_batches.id"),
                         nullable=False, index=True)

    source_row = db.Column(db.Integer)              # line in the uploaded file
    dedupe_key = db.Column(db.String(64), index=True)
    clash      = db.Column(db.String(10), nullable=False, default=CLASH_EXISTING)
    # The entry it collides with — nulled if that entry is later deleted
    existing_entry_id = db.Column(
        db.Integer, db.ForeignKey("scrap_entries.id", ondelete="SET NULL"), nullable=True
    )

    # ── The row exactly as it was read, so allowing it needs no re-upload ──
    customer_id      = db.Column(db.Integer, db.ForeignKey("customers.id"), nullable=True)
    entry_date       = db.Column(db.Date, nullable=False)
    machined_part_no = db.Column(db.String(60))
    casting_no       = db.Column(db.String(60))
    batch_no         = db.Column(db.String(40))

    qty_booked   = db.Column(db.Integer, default=0)
    qty_received = db.Column(db.Integer, default=0)
    qty_scrap    = db.Column(db.Integer, default=0, nullable=False)
    qty_machined = db.Column(db.Integer, default=0)

    notes        = db.Column(db.String(255))
    defects_json = db.Column(db.Text)   # {"<defect_id>": qty}

    status      = db.Column(db.String(10), nullable=False, default=DUP_PENDING, index=True)
    detected_at = db.Column(db.DateTime, default=datetime.now)
    resolved_at = db.Column(db.DateTime)
    resolved_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    created_entry_id = db.Column(
        db.Integer, db.ForeignKey("scrap_entries.id", ondelete="SET NULL"), nullable=True
    )

    batch    = db.relationship("ScrapImportBatch", back_populates="duplicates")
    customer = db.relationship("Customer")
    resolver = db.relationship("User", foreign_keys=[resolved_by])

    existing_entry = db.relationship("ScrapEntry", foreign_keys=[existing_entry_id])
    created_entry  = db.relationship("ScrapEntry", foreign_keys=[created_entry_id])

    @property
    def defect_qtys(self):
        """{defect_id: qty} as read off the file."""
        try:
            raw = json.loads(self.defects_json or "{}")
        except (TypeError, ValueError):
            return {}
        return {int(k): int(v) for k, v in raw.items() if v}

    @property
    def defect_total(self):
        return sum(self.defect_qtys.values())

    @property
    def is_pending(self):
        return self.status == DUP_PENDING

    @property
    def status_label(self):
        return DUP_STATUS_LABELS.get(self.status, self.status)

    @property
    def clash_label(self):
        return ("Repeated in this file" if self.clash == CLASH_FILE
                else "Already in the system")

    def __repr__(self):
        return f"<ScrapPendingDuplicate row={self.source_row} {self.status}>"


# ======================================================
# DEFECT SEED  — the columns of the HDA machining reject report
# ======================================================
# (code, name, description, aliases)
DEFAULT_DEFECTS = [
    ("OB",  "Out of balance (OB)",   "Too heavy to balance",                     "outofbalance,OOB"),
    ("NC",  "Non Clean-up (NC)",     "Raw casting visible on machined surface",  "noncleanup,non clean up"),
    ("BF",  "Face (BF)",             "Blow hole on friction face",               "blow hole face,blowhole friction face"),
    ("BB",  "Bore (BB)",             "Blow hole on friction surface",            "blow hole bore,blowhole bore"),
    ("BFU", "Fulcrum (BF)",          "Blow hole on fulcrum",                     "blow hole fulcrum,blowhole fulcrum"),
    ("BL",  "Lugs (BL)",             "Blow hole on lug",                         "blow hole lug,blowhole lugs"),
    ("IF",  "Face (IF)",             "Inclusion on friction face",               "inclusion face"),
    ("IB",  "Bore (IB)",             "Inclusion in the bore",                    "inclusion bore"),
    ("DC",  "Deformed Casting",      "",                                         "deformed"),
    ("BCI", "Bore Chip Impact marks", "",                                        "bore chip"),
    ("ODC", "OD Chip Impact marks",  "",                                         "od chip"),
    ("LCI", "Lug Chip impact marks", "",                                         "lug chip"),
    ("IDS", "Ingate Draw Sunken area", "",                                       "ingate draw,sunken area"),
    ("FPS", "Foamlike Porocity",     "Shrinkage porosity",                       "porosity,shrinkage,foamlike porosity"),
    ("RB",  "Raised Batch",          "Raised batch number",                      "raised batch #,raised batch no"),
    ("LB",  "Lumps / Build-up",      "",                                         "lumps,build up,buildup"),
    ("MET", "Metalurgical",          "",                                         "metallurgical,metallurgy"),
    ("CBM", "Cracked/Broken",        "",                                         "cracked,broken,cracked broken missing"),
    ("RS",  "Rough surface",         "",                                         "rough"),
    ("MP",  "Mix Part",              "",                                         "mixed part,mix parts"),
    ("RST", "Rust",                  "",                                         "rusted"),
]


# ======================================================
# INTERNAL DEFECT SEED — foundry scrap reasons captured in-house
# ======================================================
# Different list to the external report above: these are the casting defects
# the foundry scraps against, numbered as they appear on the shop sheet. Codes
# are the sheet's own numbers (Bubble gas has none, so it takes "BG"). Names
# are kept verbatim; rename any on the Reject Reasons screen.
# (code, name, description, aliases)
DEFAULT_INTERNAL_DEFECTS = [
    ("1",  "MISRUN",          "", ""),
    ("2",  "COLD LAP",        "", ""),
    ("3",  "SHORT SCRAP",     "", ""),
    ("4",  "DROSS",           "", ""),
    ("5",  "SLAG",            "", ""),
    ("6",  "DRAWN",           "", ""),
    ("7",  "HOT TEAR",        "", ""),
    ("8",  "FAULTY METAL",    "", ""),
    ("9",  "PINHOLES",        "", ""),
    ("10", "ROUGH",           "", ""),
    ("11", "MISPL. CORE",     "", ""),
    ("12", "FAULTY CORE",     "", ""),
    ("13", "CRUSHED",         "", ""),
    ("14", "SAND",            "", ""),
    ("15", "DROPPED",         "", ""),
    ("16", "OFFSET",          "", ""),
    ("17", "BLOWN",           "", ""),
    ("18", "SWOLLEN",         "", ""),
    ("19", "SCABBED",         "", ""),
    ("20", "RUN OUT",         "", ""),
    ("21", "STRIPP. HOT",     "", ""),
    ("22", "DAM. PATTERN",    "", ""),
    ("23", "BROKEN IN",       "", ""),
    ("24", "CRACKED",         "", ""),
    ("25", "DISTORTED",       "", ""),
    ("26", "BURNT",           "", ""),
    ("27", "AD GRIND",        "", ""),
    ("28", "BAD GALV.",       "", ""),
    ("29", "BAD MACH.",       "", ""),
    ("30", "VARY",            "", ""),
    ("31", "TRIAL",           "", ""),
    ("32", "DESTRUCTIVE",     "", ""),
    ("33", "FAULTY CHAM",     "", ""),
    ("34", "DIE HEAD SCRAP",  "", ""),
    ("35", "SETTING",         "", ""),
    ("36", "LEAKER",          "", ""),
    ("BG", "Bubble gas",      "", ""),
    ("37", "CHIPS",           "", ""),
]


def _seed_defect_list(rows, applies_to, sort_offset=0):
    """Insert any reasons in `rows` that aren't there yet. Returns the count."""
    created = 0
    for order, (code, name, description, aliases) in enumerate(rows, start=1):
        if ScrapDefect.query.filter_by(code=code).first():
            continue
        db.session.add(ScrapDefect(
            code=code,
            name=name,
            description=description or None,
            aliases=aliases or None,
            sort_order=sort_offset + order,
            active=True,
            applies_to=applies_to,
        ))
        created += 1
    return created


def seed_scrap_defects():
    """
    Insert the standard reject reasons if they aren't there yet.

    Safe to call on every startup — existing rows are left alone so any
    edits made in the Defects screen survive a restart. Internal reasons are
    offset in the sort order so the two lists stay grouped on the admin screen.
    """
    created  = _seed_defect_list(DEFAULT_DEFECTS, SCOPE_EXTERNAL)
    created += _seed_defect_list(DEFAULT_INTERNAL_DEFECTS, SCOPE_INTERNAL, sort_offset=100)

    if created:
        db.session.commit()
    return created


def active_defects(scope=None):
    """
    Active reject reasons in report-column order.

      scope='external' → reasons graded on customer imports (external + both)
      scope='internal' → reasons offered on the capture form (internal + both)
      scope=None       → the whole active catalogue (admin screen)
    """
    query = ScrapDefect.query.filter_by(active=True)
    if scope == SCOPE_EXTERNAL:
        query = query.filter(ScrapDefect.applies_to.in_([SCOPE_EXTERNAL, SCOPE_BOTH]))
    elif scope == SCOPE_INTERNAL:
        query = query.filter(ScrapDefect.applies_to.in_([SCOPE_INTERNAL, SCOPE_BOTH]))
    return query.order_by(ScrapDefect.sort_order, ScrapDefect.id).all()
