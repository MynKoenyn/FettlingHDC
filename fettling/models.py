from datetime import datetime
from app import db
from werkzeug.security import generate_password_hash, check_password_hash


# ---------------- FETTLING ENTRIES ----------------
class FettlingEntry(db.Model):
    __tablename__ = "fettling_entries"

    id = db.Column(db.Integer, primary_key=True)
    entry_date = db.Column(db.Date, nullable=False)
    product_id = db.Column(db.Integer, db.ForeignKey("products.id"), nullable=True)
    quantity = db.Column(db.Integer, nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    # relationships
    product = db.relationship("Product", back_populates="entries")
    user = db.relationship("User", back_populates="entries")

    def __repr__(self):
        return f"<Entry {self.id} qty={self.quantity}>"
