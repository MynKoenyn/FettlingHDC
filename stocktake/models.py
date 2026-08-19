import enum
from datetime import date, datetime
from app import db


class SectionEnum(str, enum.Enum):
    A = "A"
    B = "B"
    C = "C"
    D = "D"
    E = "E"
    F = "F"


# ---------------- STOCKTAKE HEADER ----------------
class StocktakeHeader(db.Model):
    """One stocktake event per department per date."""
    __tablename__ = "stocktake_headers"

    id            = db.Column(db.Integer, primary_key=True)
    date          = db.Column(db.Date, nullable=False, default=date.today)
    department_id = db.Column(db.Integer, db.ForeignKey("departments.id"), nullable=False)
    personnel_id  = db.Column(db.Integer, db.ForeignKey("personnel.id"), nullable=True)
    section       = db.Column(db.Enum(SectionEnum), nullable=False)
    notes         = db.Column(db.Text)
    created_at    = db.Column(db.DateTime, default=datetime.now)
    created_by    = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)

    # relationships
    department = db.relationship("Department", back_populates="stocktakes")
    personnel = db.relationship("Personnel", back_populates="stocktakes")
    user       = db.relationship("User")
    lines      = db.relationship(
        "StocktakeLine", back_populates="header", cascade="all, delete-orphan"
    )
    bin_lines  = db.relationship(
        "StocktakeBinLine", back_populates="header", cascade="all, delete-orphan"
    )

    def __repr__(self):
        return f"<StocktakeHeader {self.date} dept={self.department_id}>"


# ---------------- STOCKTAKE LINE ----------------
class StocktakeLine(db.Model):
    """One line per product counted within a stocktake."""
    __tablename__ = "stocktake_lines"

    id          = db.Column(db.Integer, primary_key=True)
    header_id   = db.Column(db.Integer, db.ForeignKey("stocktake_headers.id"), nullable=False)
    customer_id = db.Column(db.Integer, db.ForeignKey("customers.id"), nullable=False)
    product_id  = db.Column(db.Integer, db.ForeignKey("products.id"), nullable=False)

    # stock figures
    current_stock = db.Column(db.Numeric(12, 3), nullable=False, default=0)  # system / book qty
    count_value   = db.Column(db.Numeric(12, 3), nullable=False, default=0)  # physical count qty
    variance      = db.Column(db.Numeric(12, 3),
                              db.Computed("count_value - current_stock", persisted=True))
    @property
    def variance(self):
        return (self.count_value or 0) - (self.current_stock or 0)
    line_notes = db.Column(db.String(255))

    # relationships
    header   = db.relationship("StocktakeHeader", back_populates="lines")
    customer = db.relationship("Customer")
    product  = db.relationship("Product")

    def __repr__(self):
        return f"<StocktakeLine header={self.header_id} product={self.product_id}>"


# ---------------- HDA BIN STOCK ----------------
class BinTypeEnum(str, enum.Enum):
    UNPACKED_FETTLING = "unpacked_fettling"  # Clean Stock
    CASTBIN           = "castbin"            # WIP Stock


# Fixed business rule per bin type: weight per bin, and the yield fraction
# applied to Castbin to account for sand/sprue removed before it becomes
# clean stock. Unpacked & Fettling bins are already clean, so yield is 100%.
BIN_TYPE_META = {
    BinTypeEnum.UNPACKED_FETTLING: {"label": "Unpacked & Fettling", "weight_kg": 500, "yield_pct": 1.00},
    BinTypeEnum.CASTBIN:           {"label": "Castbin",             "weight_kg": 300, "yield_pct": 0.65},
}


class StocktakeBinLine(db.Model):
    """One bin-count line (per bin type) within an HDA stocktake session."""
    __tablename__ = "stocktake_bin_lines"

    id         = db.Column(db.Integer, primary_key=True)
    header_id  = db.Column(db.Integer, db.ForeignKey("stocktake_headers.id"), nullable=False)
    bin_type   = db.Column(db.Enum(BinTypeEnum), nullable=False)
    bin_count  = db.Column(db.Integer, nullable=False, default=0)
    notes      = db.Column(db.String(255))
    created_at = db.Column(db.DateTime, default=datetime.now)

    __table_args__ = (
        db.UniqueConstraint("header_id", "bin_type", name="uq_bin_line_header_type"),
    )

    header = db.relationship("StocktakeHeader", back_populates="bin_lines")

    @property
    def weight_kg(self):
        meta = BIN_TYPE_META[self.bin_type]
        return (self.bin_count or 0) * meta["weight_kg"] * meta["yield_pct"]

    @property
    def label(self):
        return BIN_TYPE_META[self.bin_type]["label"]

    def __repr__(self):
        return f"<StocktakeBinLine header={self.header_id} type={self.bin_type}>"