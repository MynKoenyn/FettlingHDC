"""
Standalone connection to the MeltControl Postgres database (a different
server than the main app's DATABASE_URL). Deliberately kept separate from
the Flask-SQLAlchemy `db` object in app.py so the two databases never share
a session/engine.
"""
import os
from datetime import timedelta

from sqlalchemy import create_engine, text

_engine = None
_all_columns_cache = None

VIEW_NAME = "vw_measurement_details"


def get_meltcontrol_engine():
    global _engine
    if _engine is None:
        url = os.environ.get("MELTCONTROL_DATABASE_URL")
        if not url:
            raise RuntimeError("MELTCONTROL_DATABASE_URL is not set in .env")
        _engine = create_engine(url, pool_pre_ping=True, pool_recycle=300)
    return _engine


def fetch_all_columns():
    """All column names defined by the view, in view order. Cached per
    process since the view's schema doesn't change at runtime."""
    global _all_columns_cache
    if _all_columns_cache is None:
        engine = get_meltcontrol_engine()
        with engine.connect() as conn:
            result = conn.execute(text(f'SELECT * FROM {VIEW_NAME} WHERE 1=0'))
            _all_columns_cache = list(result.keys())
    return _all_columns_cache


def fetch_stations():
    """Distinct StationName values from the view, for the filter dropdown."""
    engine = get_meltcontrol_engine()
    with engine.connect() as conn:
        result = conn.execute(text(
            f'SELECT DISTINCT "StationName" FROM {VIEW_NAME} '
            f'WHERE "StationName" IS NOT NULL ORDER BY "StationName"'
        ))
        return [row[0] for row in result]


def fetch_measurements(date_from, date_to, station_name=None, select_columns=None, limit=None):
    """
    Rows from vw_measurement_details for TimeStamp within
    [date_from, date_to] inclusive, optionally filtered by StationName and
    restricted to select_columns (a subset of fetch_all_columns()).

    Returns (columns, rows) - columns is the ordered list of column names
    actually selected, rows are SQLAlchemy Row objects.
    """
    engine = get_meltcontrol_engine()

    if select_columns:
        safe_cols = [c for c in select_columns if '"' not in c]
        select_clause = ', '.join(f'"{c}"' for c in safe_cols) if safe_cols else '*'
    else:
        select_clause = '*'

    sql = (
        f'SELECT {select_clause} FROM {VIEW_NAME} '
        f'WHERE "TimeStamp" >= :date_from AND "TimeStamp" < :date_to_exclusive'
    )
    params = {
        "date_from": date_from,
        "date_to_exclusive": date_to + timedelta(days=1),
    }
    if station_name:
        sql += ' AND "StationName" = :station_name'
        params["station_name"] = station_name
    sql += ' ORDER BY "TimeStamp" DESC'
    if limit:
        sql += ' LIMIT :row_limit'
        params["row_limit"] = limit

    with engine.connect() as conn:
        result = conn.execute(text(sql), params)
        columns = list(result.keys())
        rows = result.fetchall()

    return columns, rows
