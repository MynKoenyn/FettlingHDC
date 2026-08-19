"""
Scrap module — valuation
========================

Putting a rand value on scrap means pricing each line at the price that was in
force *on the day it was scrapped*, not today's price. A reject booked in
March must still value at March's price after the July list lands, or last
year's numbers move every time a new price list is loaded.

The lookup chain for one scrap line is:

  1. the price list entry for the period covering the entry date, for that
     line's customer + product;
  2. the same lookup against the product's own primary customer, for entries
     that carry no customer of their own;
  3. the product's catalogue price (Product.price);
  4. nothing — the line is counted as unpriced and left out of the rand total.

`price_lookup()` answers a whole report in a handful of queries rather than
two per line, which is what makes it usable over a year of scrap.
"""

from decimal import Decimal

from models import Customer, PriceListEntry, PriceListPeriod, Product

# Where a unit price came from — reported so a rand figure can be trusted
PRICE_LIST    = "list"      # the price list covering that date
PRICE_PRODUCT = "product"   # catalogue fallback
PRICE_NONE    = None        # could not be priced


class PriceLookup:
    """
    Resolved unit prices for a set of (date, customer, product) keys.

    Built by `price_lookup()`; ask it for `value(key, qty)` or `unit(key)`.
    """

    def __init__(self, prices):
        self._prices = prices   # {(date, customer_id, product_id): (Decimal, source)}

    def unit(self, entry_date, customer_id, product_id):
        """(price, source) for one line — (None, None) when it cannot be priced."""
        return self._prices.get((entry_date, customer_id, product_id), (None, PRICE_NONE))

    def value(self, entry_date, customer_id, product_id, qty):
        """
        (rand value, priced qty, unpriced qty, list-priced qty, fallback-priced
        qty) for `qty` units of that line.

        Splitting the quantity is what lets a report say how much of its scrap
        the rand figure actually covers, instead of quietly under-reporting —
        and which of that was priced off the list versus the product's
        catalogue price, so a report can flag values that lean on the fallback.
        """
        price, source = self.unit(entry_date, customer_id, product_id)
        if price is None:
            return Decimal("0"), 0, qty, 0, 0
        list_qty = qty if source == PRICE_LIST else 0
        fallback_qty = qty if source == PRICE_PRODUCT else 0
        return Decimal(price) * qty, qty, 0, list_qty, fallback_qty


def price_lookup(keys):
    """
    Resolve unit prices for an iterable of (entry_date, customer_id, product_id).

    Returns a PriceLookup. Keys with no product are never priced — scrap that
    matched no product has no price to look up.
    """
    keys = {k for k in keys if k[2]}          # no product → nothing to price
    if not keys:
        return PriceLookup({})

    product_ids  = {k[2] for k in keys}
    products     = {p.id: p for p in Product.query.filter(Product.id.in_(product_ids)).all()}

    # An entry with no customer of its own prices against the product's primary
    # customer, which is the customer the price list is keyed on anyway.
    def customer_for(key):
        _, customer_id, product_id = key
        if customer_id:
            return customer_id
        product = products.get(product_id)
        return product.customer_id if product else None

    customer_ids = {c for c in (customer_for(k) for k in keys) if c}
    customers    = ({c.id: c for c in Customer.query.filter(Customer.id.in_(customer_ids)).all()}
                    if customer_ids else {})

    # ── Periods, once per division ──
    division_ids = {c.division_id for c in customers.values() if c.division_id}
    periods_by_division = {}
    if division_ids:
        rows = (
            PriceListPeriod.query
            .filter(PriceListPeriod.division_id.in_(division_ids))
            .order_by(PriceListPeriod.start_date.desc())
            .all()
        )
        for period in rows:
            periods_by_division.setdefault(period.division_id, []).append(period)

    def period_for(entry_date, customer_id):
        """The period covering that date, mirroring models.get_price_period."""
        customer = customers.get(customer_id)
        if customer is None or not customer.division_id or entry_date is None:
            return None
        for period in periods_by_division.get(customer.division_id, ()):
            if period.start_date <= entry_date <= period.end_date:
                return period      # already sorted start_date desc — newest wins
        return None

    # ── One query for every price list entry any of these keys could hit ──
    wanted = {}      # key -> (period_id, customer_id, product_id)
    for key in keys:
        customer_id = customer_for(key)
        period = period_for(key[0], customer_id)
        if period is not None:
            wanted[key] = (period.id, customer_id, key[2])

    price_by_key = {}
    if wanted:
        entries = PriceListEntry.query.filter(
            PriceListEntry.period_id.in_({w[0] for w in wanted.values()}),
            PriceListEntry.customer_id.in_({w[1] for w in wanted.values()}),
            PriceListEntry.product_id.in_({w[2] for w in wanted.values()}),
        ).all()
        price_by_key = {
            (e.period_id, e.customer_id, e.product_id): e.price for e in entries
        }

    prices = {}
    for key in keys:
        price = price_by_key.get(wanted.get(key))
        if price is not None:
            prices[key] = (Decimal(price), PRICE_LIST)
            continue
        product = products.get(key[2])
        if product is not None and product.price is not None:
            prices[key] = (Decimal(product.price), PRICE_PRODUCT)

    return PriceLookup(prices)
