"""
Minimal drop-in replacement for Flask-SQLAlchemy's Pagination object, for
routes that have to filter/sort in Python (e.g. on an @property the
database can't see) and so already hold the full matching list before it
can be paged. Exposes the same attributes/methods templates/_pagination.html
relies on, so those routes can still reuse that shared partial instead of
rendering every row on one page.
"""
import math


class ManualPagination:
    def __init__(self, items_all, page, per_page):
        self.total = len(items_all)
        self.per_page = per_page
        self.pages = max(1, math.ceil(self.total / per_page))
        self.page = min(max(1, page), self.pages)
        start = (self.page - 1) * per_page
        self.items = items_all[start:start + per_page]
        self.has_prev = self.page > 1
        self.has_next = self.page < self.pages
        self.prev_num = self.page - 1
        self.next_num = self.page + 1

    def iter_pages(self, left_edge=1, right_edge=1, left_current=2, right_current=2):
        last = 0
        for num in range(1, self.pages + 1):
            if (num <= left_edge
                    or (self.page - left_current <= num <= self.page + right_current)
                    or num > self.pages - right_edge):
                if last + 1 != num:
                    yield None
                yield num
                last = num
