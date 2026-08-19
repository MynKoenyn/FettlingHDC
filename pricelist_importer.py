"""
Price list importer
====================

Loads prices for one PriceListPeriod from a CSV or Excel file built on the
template that `build_template_csv()` produces (Price Lists → a period →
Import → Download CSV Template).

Behaviour worth knowing:

  * Upsert, not append. A row is matched to an existing PriceListEntry by
    (period, customer, product). Matched rows have their price updated, the
    rest are inserted, so the same file can be re-sent after a correction.
  * Customers and products are never created here — they must already exist.
    A customer is matched by name, scoped to the period's division (the same
    rule the "Add or update a price" form enforces). A product is matched
    first by Product Code, falling back to Name. Unresolved rows are
    reported and skipped; the rest of the file still loads.
  * Price is required on every row — a blank or unusable price skips the row.
"""

import csv
import io

from app import db
from models import Customer, PriceListEntry, Product
from product_importer import clean, norm_code, norm_header, norm_name, parse_decimal, read_table


# ── Column headers of the CSV template ───────────────────────────────────────
TEMPLATE_HEADERS = ["Customer", "Product Code", "Product Name", "Price"]

# Normalised header → field. Exact matches only.
FIELD_ALIASES = {
    "customer":       "customer",
    "customername":   "customer",

    "productcode":    "product_code",
    "code":           "product_code",
    "castingnumber":  "product_code",
    "castingno":      "product_code",

    "product":        "product_name",
    "productname":    "product_name",
    "description":    "product_name",

    "price":          "price",
    "unitprice":      "price",
    "catalogueprice": "price",
}

IGNORED_HEADERS = {"", "id", "period", "periodid", "periodlabel"}


def map_headers(headers):
    """Returns (index → field, list of unrecognised headers)."""
    field_map, unknown = {}, []
    for idx, raw in enumerate(headers):
        key = norm_header(raw)
        if key in IGNORED_HEADERS:
            continue
        if key in FIELD_ALIASES:
            field_map[idx] = FIELD_ALIASES[key]
            continue
        unknown.append(str(raw).strip())
    return field_map, unknown


# ── The import itself ────────────────────────────────────────────────────────

class PriceListImportResult:
    """What one upload did, for the flash message and the import screen."""

    def __init__(self):
        self.total = 0
        self.created = 0
        self.updated = 0
        self.skipped = 0
        self.unknown_columns = []
        self.row_issues = []      # (row number, customer/product label, message)

    @property
    def touched(self):
        return self.created + self.updated


def import_price_list_entries(period, file_storage):
    """
    Load prices for `period` from an uploaded sheet. Returns a
    PriceListImportResult.

    Raises ValueError when the file itself is unusable (no header, no
    Customer/Product/Price columns) — a problem with the file rather than a
    row.
    """
    result = PriceListImportResult()

    headers, rows = read_table(file_storage)
    if not headers:
        raise ValueError("That file has no readable header row.")

    field_map, unknown = map_headers(headers)
    result.unknown_columns = unknown

    present = set(field_map.values())
    if "customer" not in present:
        raise ValueError("No 'Customer' column found. Download the CSV template and check the column headings match.")
    if not {"product_code", "product_name"} & present:
        raise ValueError("No 'Product Code' or 'Product Name' column found. Download the CSV template and check the column headings match.")
    if "price" not in present:
        raise ValueError("No 'Price' column found. Download the CSV template and check the column headings match.")

    customers = {
        norm_name(c.name): c
        for c in Customer.query.filter_by(division_id=period.division_id).all()
        if c.name
    }

    by_code, by_name = {}, {}
    for product in Product.query.all():
        code = norm_code(product.product_code)
        if code:
            by_code.setdefault(code, product)
        by_name.setdefault(norm_name(product.name), product)

    existing = {
        (e.customer_id, e.product_id): e
        for e in PriceListEntry.query.filter_by(period_id=period.id).all()
    }

    for row_no, row in enumerate(rows, start=2):     # row 1 is the header
        result.total += 1

        values = {}
        for idx, field in field_map.items():
            values[field] = row[idx] if idx < len(row) else None

        customer_name = clean(values.get("customer"))
        product_code = clean(values.get("product_code"))
        product_name = clean(values.get("product_name"))
        label = customer_name or "?"

        if not customer_name:
            result.skipped += 1
            result.row_issues.append((row_no, label, "No customer given — row skipped."))
            continue

        customer = customers.get(norm_name(customer_name))
        if not customer:
            result.skipped += 1
            result.row_issues.append((
                row_no, customer_name,
                f"Customer '{customer_name}' is not in this period's division — row skipped."
            ))
            continue

        product = by_code.get(norm_code(product_code)) if product_code else None
        if product is None and product_name:
            product = by_name.get(norm_name(product_name))
        if product is None:
            result.skipped += 1
            result.row_issues.append((
                row_no, customer_name,
                f"Product '{product_code or product_name}' was not found — row skipped."
            ))
            continue

        price = parse_decimal(clean(values.get("price")))
        if price is None:
            result.skipped += 1
            result.row_issues.append((row_no, customer_name, "Price is missing or not a number — row skipped."))
            continue
        if price < 0:
            result.skipped += 1
            result.row_issues.append((row_no, customer_name, "Price cannot be negative — row skipped."))
            continue

        key = (customer.id, product.id)
        entry = existing.get(key)
        if entry:
            entry.price = price
            result.updated += 1
        else:
            entry = PriceListEntry(
                period_id=period.id,
                customer_id=customer.id,
                product_id=product.id,
                price=price,
            )
            db.session.add(entry)
            existing[key] = entry
            result.created += 1

    if result.touched:
        db.session.commit()
    else:
        db.session.rollback()

    return result


# ── CSV template / export ────────────────────────────────────────────────────

def build_template_csv(period, sample=True):
    """The blank import sheet for a period, with worked example rows drawn
    from customers/products already in the period's division."""
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(TEMPLATE_HEADERS)

    if sample:
        customers = (
            Customer.query.filter_by(division_id=period.division_id)
            .order_by(Customer.name).limit(2).all()
        )
        for customer in customers:
            writer.writerow([customer.name, "", "Example Product", "0.00"])

    return buffer.getvalue()


def build_export_csv(period):
    """The current prices for a period in template layout — edit and re-send."""
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(TEMPLATE_HEADERS)

    entries = (
        PriceListEntry.query
        .filter_by(period_id=period.id)
        .join(Customer, PriceListEntry.customer_id == Customer.id)
        .join(Product, PriceListEntry.product_id == Product.id)
        .order_by(Customer.name, Product.name)
        .all()
    )
    for e in entries:
        writer.writerow([
            e.customer.name,
            e.product.product_code or "",
            e.product.name,
            f"{e.price:.2f}",
        ])

    return buffer.getvalue()
