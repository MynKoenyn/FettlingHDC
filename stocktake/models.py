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
    section       = db.Column(db.Enum(SectionEnum), nullable=False)
    notes         = db.Column(db.Text)
    created_at    = db.Column(db.DateTime, default=datetime.now)
    created_by    = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)

    # relationships
    department = db.relationship("Department", back_populates="stocktakes")
    user       = db.relationship("User")
    lines      = db.relationship(
        "StocktakeLine", back_populates="header", cascade="all, delete-orphan"
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